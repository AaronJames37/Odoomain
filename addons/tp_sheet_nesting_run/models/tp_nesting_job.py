import base64
import json
import logging
import random
import time
from collections import Counter

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class TpNestingJob(models.Model):
    _inherit = "tp.nesting.job"

    def action_run_nesting(self):
        """Run the engine for this job, persist a tp.nesting.run, and create
        ONE mrp.production per distinct full-sheet SKU consumed.

        Atomic: if stock is insufficient for any SKU, raises UserError BEFORE
        creating any records.
        """
        self.ensure_one()
        parts = self._tp_website_panel_parts()
        if not parts:
            raise UserError(_(
                "This job has no linked website panel rows. Confirm the order "
                "or click 'Pull Website Jobs' first."
            ))

        # 1. Build engine inputs --------------------------------------------------
        cuts = self._tp_run_build_cuts(parts)
        sources = self._tp_run_build_sources()
        if not sources:
            raise UserError(_(
                "No tp.sheet.format records found for the source product(s) "
                "mapped to %s. Configure sheet formats first."
            ) % self.demand_product_id.display_name)
        kerf_mm = max(0, int(self.env.company.tp_nesting_kerf_mm or 3))
        trim_edge_mm = 0

        # 2. Run the v2 CutList-style pattern constructor ------------------------
        plan = self._tp_run_v2_pattern_constructor(
            cuts, sources, kerf_mm=kerf_mm, trim_edge_mm=trim_edge_mm,
            time_budget_s=self.env.company.tp_nesting_guillotine_seconds or 10,
        )
        if not plan.get("ok"):
            metrics = plan.get("metrics") or {}
            reason = metrics.get("infeasible_reason") or "unknown"
            err_cut = plan.get("error_cut") or {}
            msg = "Nesting engine could not pack: %s" % reason
            if err_cut:
                msg += " (failed on %sx%s)" % (
                    err_cut.get("width_mm"), err_cut.get("height_mm")
                )
            raise UserError(msg)

        bins = plan.get("bins") or []
        if not bins:
            raise UserError(_("Engine returned no bins. Nothing to do."))
        self._tp_run_assert_offcuts_available(bins, run=False)

        # 3. Group consumption by full-sheet product -----------------------------
        # Offcut bins consume existing scrap, not fresh stock, so they don't
        # drive fresh-sheet MO creation or the stock check below.
        product_to_qty = Counter()
        for bin_data in bins:
            src = bin_data.get("source") or {}
            if src.get("is_offcut"):
                continue
            product_id = src.get("product_id")
            if not product_id:
                continue
            product_to_qty[product_id] += 1

        if not product_to_qty:
            # All pieces fit on offcuts — no fresh sheets to cut. (Offcut
            # consumption is materialised separately when the run is processed.)
            _logger.info("Job %s nested entirely onto offcuts; no fresh-sheet MOs created.", self.name)

        # 4. Pre-flight stock check ----------------------------------------------
        Product = self.env["product.product"]
        Warehouse = self.env["stock.warehouse"]
        warehouse = Warehouse.search([("company_id", "=", self.env.company.id)], limit=1)
        if not warehouse:
            raise UserError(_("No warehouse configured for company %s.") % self.env.company.display_name)
        stock_location = warehouse.lot_stock_id

        shortages = []
        for product_id, qty in product_to_qty.items():
            product = Product.browse(product_id)
            available = product.with_context(location=stock_location.id).qty_available
            if available < qty:
                shortages.append(
                    "  - %s: need %d, have %.1f"
                    % (product.display_name, qty, available)
                )
        if shortages:
            raise UserError(_(
                "Insufficient stock to run this nest. Shortages:\n%s\n\n"
                "No records were created. Top up stock and retry."
            ) % "\n".join(shortages))

        # 5. Create the nesting run record --------------------------------------
        Run = self.env["tp.nesting.run"].sudo()
        # No ir.sequence is registered for tp.nesting.run, so build a name
        # from the job + a timestamp slug (avoids collisions across same-day runs).
        run_name = "NEST-%s-%s" % (
            self.name,
            fields.Datetime.now().strftime("%Y%m%d%H%M%S"),
        )
        run = Run.create({
            "name": run_name,
            "mo_id": False,  # will be set to the first MO below for back-compat
            "kerf_mm": kerf_mm,
            "trim_edge_mm": trim_edge_mm,
            "rotation_mode": "free",
            "engine_mode": "deterministic",
            "job_id": self.id,
            "state": "done",
        })

        # 6. Create one MO per distinct source product --------------------------
        # Each MO uses CUT-OPERATION (a service product) as its "produced"
        # product so the MO doesn't simultaneously produce the same sheet
        # it's consuming (which would net stock changes to zero).
        Mrp = self.env["mrp.production"].sudo()
        # The 'Production' stock location isn't always registered with an
        # xmlid, so look it up by usage instead.
        production_location = self.env["stock.location"].sudo().search(
            [("usage", "=", "production"), ("company_id", "in", [self.env.company.id, False])],
            limit=1,
        )
        if not production_location:
            raise UserError(_("No 'Production' stock location found for company %s.") % self.env.company.display_name)
        cut_op = Product.search([("default_code", "=", "CUT-OPERATION")], limit=1)
        if not cut_op:
            raise UserError(_(
                "Could not find a 'CUT-OPERATION' service product. "
                "Create one (type=service, default_code=CUT-OPERATION) before running."
            ))
        mos_created = self.env["mrp.production"]
        for product_id, qty in product_to_qty.items():
            sheet_product = Product.browse(product_id)
            # MO produces CUT-OPERATION (service), consumes the sheet.
            mo_vals = {
                "product_id": cut_op.id,
                "product_qty": qty,  # one CUT-OPERATION unit per sheet consumed
                "product_uom_id": cut_op.uom_id.id,
                "origin": "%s / %s / %s" % (
                    run.name,
                    self.sale_order_id.name or self.name,
                    sheet_product.default_code or sheet_product.display_name,
                ),
                "company_id": self.env.company.id,
                "x_tp_nesting_run_id": run.id,
                "move_raw_ids": [(0, 0, {
                    "product_id": sheet_product.id,
                    "product_uom_qty": qty,
                    "product_uom": sheet_product.uom_id.id,
                    "location_id": stock_location.id,
                    "location_dest_id": production_location.id,
                    "company_id": self.env.company.id,
                })],
            }
            mo = Mrp.create(mo_vals)
            try:
                mo.action_confirm()
            except Exception:
                _logger.exception("MO %s action_confirm failed for run %s", mo.name, run.name)
                # Don't abort — surface as a warning in the toast. The MO is
                # already created in draft; operator can confirm manually.
            mos_created |= mo

        # Set the legacy mo_id to the first MO for back-compat with code reading
        # run.mo_id directly.
        if mos_created:
            run.mo_id = mos_created[0].id

        # 7. Allocations + SVG + DXF (reuse sandbox helpers) -------------------
        # The sandbox helpers operate on a wizard record; we don't have one.
        # Use the same staticmethod-like helpers from the sandbox model — they
        # don't read self for anything we care about.
        Sandbox = self.env["tp.nesting.sandbox"].sudo()
        # tp.nesting.allocation rows are nice to have but not strictly needed
        # for the cost chain. Persist them so the run's form shows placements.
        self._tp_run_create_allocations(run, bins, parts)
        self._tp_run_finalize_metrics(run, plan)

        # Build DXF bundle (one DXF per sheet, packed in a zip) and store on
        # the run. We use a sandbox stub bound to this job so its helper
        # methods that read self.sale_order_id work.
        try:
            stub = Sandbox.create({
                "job_id": self.id,
                "sheet_format_id": self._tp_run_pick_any_sheet_format(sources),
            })
            dxf_bytes, dxf_name = stub._tp_build_dxf(bins=bins, parts=parts)
            # Override the filename to anchor on the run, not the SO.
            so_name = (self.sale_order_id.name or run.name).replace(" ", "_")
            dxf_name = "%s_%s.zip" % (run.name, so_name)
            stub.unlink()
        except Exception:
            _logger.exception("DXF build failed for run %s", run.name)
            dxf_bytes, dxf_name = b"", ""
        if dxf_bytes:
            run.write({
                "x_tp_dxf_bytes": base64.b64encode(dxf_bytes),
                "x_tp_dxf_filename": dxf_name,
            })

        # 8. Notify and open the run --------------------------------------------
        sheet_count = sum(product_to_qty.values())
        message = _(
            "Run %(run)s: placed %(panels)d panels onto %(sheets)d sheets "
            "across %(mos)d MOs."
        ) % {
            "run": run.name,
            "panels": sum(len(b.get("placements") or []) for b in bins),
            "sheets": sheet_count,
            "mos": len(mos_created),
        }
        _logger.info(message)
        return {
            "type": "ir.actions.act_window",
            "res_model": "tp.nesting.run",
            "res_id": run.id,
            "view_mode": "form",
            "target": "current",
        }

    # ------------------------------------------------------------------
    # Engine input helpers (kept private here so we don't tie this module
    # to internals of the sandbox transient model).
    # ------------------------------------------------------------------
    @api.model
    def _tp_run_build_cuts(self, parts):
        # Honor the company flag: when 'Include Cut-Out Parts In Nesting' is
        # off, drop panels with cut_outs before they reach the engine.
        kept, dropped = self._tp_partition_cutout_parts(parts)
        if dropped:
            _logger.info(
                "tp_nesting_include_cutout_parts disabled — skipping %d cut-out part(s): %s",
                len(dropped),
                ", ".join(p.part_key for p in dropped),
            )
        cuts = []
        for part in kept:
            for inst in range(max(1, int(part.quantity or 1))):
                cuts.append({
                    "width_mm": int(part.width_mm or 0),
                    "height_mm": int(part.height_mm or 0),
                    "source_mo_id": 0,
                    "source_so_line_id": part.sale_order_line_id.id if part.sale_order_line_id else 0,
                    "source_web_cut_part_id": part.id,
                    "_run_instance_index": inst,
                })
        cuts.sort(key=lambda c: c["width_mm"] * c["height_mm"], reverse=True)
        return cuts

    def _tp_run_build_sources(self):
        """Resolve eligible sheet formats for this job's demand product via
        tp.nesting.source.map → tp.sheet.format on the source product."""
        self.ensure_one()
        SM = self.env["tp.nesting.source.map"].sudo()
        SF = self.env["tp.sheet.format"].sudo()

        maps = SM.search([
            ("demand_product_id", "=", self.demand_product_id.id),
            ("active", "=", True),
        ])
        source_products = maps.mapped("source_product_id")
        if not source_products:
            return []

        formats = SF.search([
            ("product_id", "in", source_products.ids),
            ("active", "=", True),
        ])
        if not formats:
            return []

        sources = []
        for sheet in formats:
            width = int(sheet.width_mm or 0)
            height = int(sheet.height_mm or 0)
            area = float(width * height)
            unit_cost = float(sheet.landed_cost or sheet.product_id.standard_price or 0.0)
            sources.append({
                "kind": "sheet_format",
                "stable_id": "sheet_format:%s" % sheet.id,
                "id": sheet.id,
                "record": sheet,
                "product_id": sheet.product_id.id,
                "lot_id": 0,
                "width_mm": width,
                "height_mm": height,
                "area_mm2": area,
                "unit_cost": unit_cost,
                "effective_cost_per_area": (unit_cost / area) if area > 0 else 0.0,
            })
        return sources

    @api.model
    def _tp_run_pick_any_sheet_format(self, sources):
        """Pick an arbitrary tp.sheet.format id from the engine source list.
        Used only to satisfy the sandbox stub's required field — the actual
        sheet selection inside the DXF builder comes from the bins."""
        for s in sources:
            sid = s.get("id")
            if sid:
                return sid
        return False

    def _tp_run_offcut_sources(self, product, material_identity):
        """Compatible, available offcuts for this material, shaped as engine
        *lot* sources (finite, use-once bins). Reuses the MO-path compatibility
        filter so preview and MO runs draw from the same offcut pool. Tagged
        is_offcut for rendering; kind=sheet_lot so the engine packs and scores
        them like any other fixed-size bin — utilisation decides, not reuse."""
        Mo = self.env["mrp.production"].sudo()
        offcuts = Mo._tp_material_compatible_offcuts(product, material_identity or {})
        sources = []
        for offcut in offcuts:
            width = int(offcut.width_mm or 0)
            height = int(offcut.height_mm or 0)
            area = float(width * height)
            sources.append({
                "kind": "sheet_lot",
                "is_offcut": True,
                "stable_id": "offcut:%s" % offcut.id,
                "id": offcut.id,
                "offcut_ref": offcut.offcut_ref or 0,
                "record": offcut,
                "product_id": offcut.product_id.id if offcut.product_id else 0,
                "lot_id": offcut.lot_id.id if offcut.lot_id else 0,
                "width_mm": width,
                "height_mm": height,
                "area_mm2": area,
                "unit_cost": float(offcut.remaining_value or 0.0),
                "effective_cost_per_area": float((offcut.remaining_value or 0.0) / area) if area > 0 else 0.0,
            })
        return sources

    @api.model
    def _tp_run_offcut_ids_from_bins(self, bins):
        offcut_ids = []
        seen = set()
        for bin_data in bins or []:
            source = bin_data.get("source") or {}
            if not source.get("is_offcut"):
                continue
            offcut_id = source.get("id")
            if offcut_id and offcut_id not in seen:
                seen.add(offcut_id)
                offcut_ids.append(offcut_id)
        return offcut_ids

    @api.model
    def _tp_run_assert_offcuts_available(self, bins, run=False):
        offcut_ids = self._tp_run_offcut_ids_from_bins(bins)
        if not offcut_ids:
            return
        Offcut = self.env["tp.offcut"].sudo()
        for offcut in Offcut.browse(offcut_ids):
            if not offcut.exists():
                raise UserError(_("A selected offcut no longer exists. Re-preview the nesting run."))
            same_run = (
                run
                and "reservation_run_id" in offcut._fields
                and offcut.reservation_run_id.id == run.id
            )
            same_mo = run and run.mo_id and offcut.reserved_mo_id.id == run.mo_id.id
            if offcut.state != "available" and not same_run and not same_mo:
                label = offcut.offcut_ref or offcut.display_name
                raise UserError(
                    _(
                        "Offcut %(offcut)s is no longer available. "
                        "It may have been reserved by another nesting run. "
                        "Refresh the wizard and preview again."
                    )
                    % {"offcut": label}
                )

    @api.model
    def _tp_plan_sheets_and_waste(self, plan):
        """Sheets used and wasted FRESH-sheet area. Offcuts are free scrap that
        would otherwise be stored or binned, so the area left over on an offcut
        is NOT counted as waste — only unused area on freshly-cut full sheets is.
        This makes "fresh sheet + offcuts" rank better than "more fresh sheets"
        whenever it actually saves fresh material, which is the goal."""
        fresh_src_total = 0.0
        fresh_used_total = 0.0
        fresh_bins = 0
        for b in (plan.get("bins") or []):
            s = b.get("source") or {}
            if s.get("is_offcut"):
                continue  # offcut leftover isn't waste
            fresh_bins += 1
            fresh_src_total += float(s.get("width_mm") or 0) * float(s.get("height_mm") or 0)
            for p in (b.get("placements") or []):
                fresh_used_total += float(p.get("fit_w") or 0) * float(p.get("fit_h") or 0)
        return fresh_bins, max(0.0, fresh_src_total - fresh_used_total)

    @api.model
    def _tp_count_guillotine_cuts(self, plan):
        """Factory-facing saw operation count for the whole plan."""
        return int(self._tp_plan_saw_metrics(plan).get("saw_operations") or 0)

    @api.model
    def _tp_plan_saw_metrics(self, plan):
        from odoo.addons.tp_sheet_nesting.models.services.tp_guillotine_cuts import (
            panel_saw_cut_metrics,
        )

        totals = {
            "saw_operations": 0,
            "line_operations": 0,
            "trim_cuts": 0,
            "separation_cuts": 0,
            "fence_settings": 0,
            "sheet_metrics": [],
        }
        fence_values = 0
        for idx, b in enumerate((plan or {}).get("bins") or []):
            src = b.get("source") or {}
            sheet_w = int(src.get("width_mm") or 0)
            sheet_h = int(src.get("height_mm") or 0)
            rects = [
                (
                    int(p.get("x") or 0),
                    int(p.get("y") or 0),
                    int(p.get("fit_w") or 0),
                    int(p.get("fit_h") or 0),
                )
                for p in (b.get("placements") or [])
            ]
            metrics = panel_saw_cut_metrics(rects, sheet_w, sheet_h)
            free_rects = [
                (
                    int(r.get("x") or 0),
                    int(r.get("y") or 0),
                    int(r.get("w") or r.get("width_mm") or 0),
                    int(r.get("h") or r.get("height_mm") or 0),
                )
                for r in (b.get("free_rects") or [])
            ]
            cutlist_operations = (
                max(0, len(rects) + len(free_rects) - 1)
                if rects and free_rects
                else int(metrics.get("saw_operations") or 0)
            )
            totals["saw_operations"] += int(cutlist_operations)
            totals["line_operations"] += int(metrics.get("saw_operations") or 0)
            totals["trim_cuts"] += int(metrics.get("trim_cuts") or 0)
            totals["separation_cuts"] += int(metrics.get("separation_cuts") or 0)
            fence_values += int(metrics.get("fence_settings") or 0)
            totals["sheet_metrics"].append(
                {
                    "sheet_index": idx,
                    "source_label": (
                        (src.get("record") and src["record"].display_name)
                        or src.get("stable_id")
                        or src.get("kind")
                        or ""
                    ),
                    "saw_operations": int(cutlist_operations),
                    "line_operations": int(metrics.get("saw_operations") or 0),
                    "trim_cuts": int(metrics.get("trim_cuts") or 0),
                    "separation_cuts": int(metrics.get("separation_cuts") or 0),
                    "fence_settings": int(metrics.get("fence_settings") or 0),
                    "orientation": metrics.get("orientation") or "",
                }
            )
        totals["fence_settings"] = fence_values
        return totals

    @api.model
    def _tp_plan_grouping(self, plan):
        """Measure how SCATTERED same-size pieces are. For each distinct piece
        size, take the bounding box of all its instances and compare to their
        combined area: a tight block (instances packed together) has bbox≈area;
        scattered instances have a much larger bbox. Returns summed wasted-bbox
        area (lower = pieces of the same size grouped into tidy blocks, the
        CutList look). Pure tie-break — never overrides offcut quality."""
        from collections import defaultdict
        waste = 0
        for b in (plan.get("bins") or []):
            by_size = defaultdict(list)
            for p in (b.get("placements") or []):
                key = tuple(sorted((int(p.get("fit_w") or 0), int(p.get("fit_h") or 0))))
                by_size[key].append(p)
            for key, pls in by_size.items():
                if len(pls) < 2:
                    continue
                x0 = min(int(p.get("x") or 0) for p in pls)
                y0 = min(int(p.get("y") or 0) for p in pls)
                x1 = max(int(p.get("x") or 0) + int(p.get("fit_w") or 0) for p in pls)
                y1 = max(int(p.get("y") or 0) + int(p.get("fit_h") or 0) for p in pls)
                bbox = (x1 - x0) * (y1 - y0)
                used = sum(int(p.get("fit_w") or 0) * int(p.get("fit_h") or 0) for p in pls)
                waste += max(0, bbox - used)
        return waste // 1000

    @api.model
    def _tp_plan_is_guillotine(self, plan):
        """True only if EVERY sheet's layout can be cut on a panel saw — i.e. the
        pieces can be separated by a recursive sequence of straight edge-to-edge
        cuts. Candidate generators must never win unless their placements can be
        separated by rip/crosscut operations."""
        tol = 1

        def cuttable(rects, x0, y0, x1, y1):
            inside = [r for r in rects
                      if r[0] >= x0 - tol and r[1] >= y0 - tol
                      and r[0] + r[2] <= x1 + tol and r[1] + r[3] <= y1 + tol]
            if len(inside) <= 1:
                return True
            xs = {r[0] for r in inside} | {r[0] + r[2] for r in inside}
            for cx in sorted(xs):
                if x0 + tol < cx < x1 - tol and all(
                        not (r[0] + tol < cx < r[0] + r[2] - tol) for r in inside):
                    left = [r for r in inside if r[0] + r[2] <= cx + tol]
                    right = [r for r in inside if r[0] >= cx - tol]
                    if left and right and len(left) + len(right) == len(inside):
                        return cuttable(left, x0, y0, cx, y1) and cuttable(right, cx, y0, x1, y1)
            ys = {r[1] for r in inside} | {r[1] + r[3] for r in inside}
            for cy in sorted(ys):
                if y0 + tol < cy < y1 - tol and all(
                        not (r[1] + tol < cy < r[1] + r[3] - tol) for r in inside):
                    bottom = [r for r in inside if r[1] + r[3] <= cy + tol]
                    top = [r for r in inside if r[1] >= cy - tol]
                    if bottom and top and len(bottom) + len(top) == len(inside):
                        return cuttable(bottom, x0, y0, x1, cy) and cuttable(top, x0, cy, x1, y1)
            return False

        for b in (plan.get("bins") or []):
            src = b.get("source") or {}
            W = int(src.get("width_mm") or 0)
            H = int(src.get("height_mm") or 0)
            rects = [(int(p.get("x") or 0), int(p.get("y") or 0),
                      int(p.get("fit_w") or 0), int(p.get("fit_h") or 0))
                     for p in (b.get("placements") or [])]
            if rects and not cuttable(rects, 0, 0, W, H):
                return False
        return True

    @api.model
    def _tp_plan_offcut_quality(self, plan):
        """Score the OFFCUTS this layout produces by NET REUSABLE SCRAP. The whole
        point is leftover material you can reuse: a fat squarish offcut is an
        asset, a thin strip is waste. For each fresh sheet we derive the leftover
        rectangles a guillotine cut yields (ignoring slivers), count a squarish
        offcut (aspect <= 2.5) as +area and a strip (aspect > 2.5) as -area, and
        return -net so LOWER is better (more usable scrap, fewer/larger strips).
        This naturally trades off a small strip against a big square offcut — the
        user keeps a layout with one tiny strip if its other offcut is huge.
        Offcut bins (reused scrap) are excluded — only fresh sheets leave new
        offcuts."""
        from odoo.addons.tp_sheet_nesting.models.services.tp_guillotine_cuts import (
            offcut_rects,
        )
        STRIP_RATIO = 2.5
        MIN_SIDE = 80  # ignore slivers below 80mm — not a usable offcut
        strip_area = 0   # area lost to thin (aspect>2.5) offcuts — the thing to kill
        square_area = 0  # area in fat reusable offcuts
        count = 0
        for b in (plan.get("bins") or []):
            src = b.get("source") or {}
            if src.get("is_offcut"):
                continue
            W = int(src.get("width_mm") or 0)
            H = int(src.get("height_mm") or 0)
            rects = [(int(p.get("x") or 0), int(p.get("y") or 0),
                      int(p.get("fit_w") or 0), int(p.get("fit_h") or 0))
                     for p in (b.get("placements") or [])]
            for (x, y, w, h) in offcut_rects(rects, W, H, min_side=MIN_SIDE):
                if w <= 0 or h <= 0:
                    continue
                count += 1
                if (max(w, h) / float(min(w, h))) <= STRIP_RATIO:
                    square_area += w * h
                else:
                    strip_area += w * h
        # Ranking (all lower-is-better):
        #   1. STRIP AREA — minimise material trapped in useless thin strips
        #      (the 10:1 sliver the user hates). This is the dominant defect.
        #   2. OFFCUT COUNT — then consolidate into the fewest pieces (one
        #      gigantic reusable sheet, the 10mm CutList ideal).
        #   3. -SQUARE AREA — among those, prefer more total reusable area.
        return (strip_area // 1000, count, -(square_area // 1000))

    @api.model
    def _tp_plan_compactness(self, plan):
        """Tie-breaker that pulls pieces into a tight bottom-left corner block,
        the way CutListOptimiser does. Ranked by the pieces' bounding-box
        LONGEST EXTENT first (keeping one dimension tight leaves a single clean
        full-height/width offcut strip), then box area, then the sum of each
        piece's distance from the origin. Lower is more compact. Sits below
        utilisation in the score, so it only chooses between equally-efficient
        layouts — it never trades away yield, just gives fewer, larger,
        rectangular, reusable offcuts."""
        longest = 0
        bbox_area = 0
        offset_sum = 0
        for b in (plan.get("bins") or []):
            pls = b.get("placements") or []
            if not pls:
                continue
            x1 = max(int(p.get("x") or 0) + int(p.get("fit_w") or 0) for p in pls)
            y1 = max(int(p.get("y") or 0) + int(p.get("fit_h") or 0) for p in pls)
            longest = max(longest, x1, y1)
            bbox_area += x1 * y1
            offset_sum += sum(int(p.get("x") or 0) + int(p.get("y") or 0) for p in pls)
        # Bounding-box AREA first (the tightest overall block — matches CutList's
        # 10mm layout), then longest extent, then origin distance. Area-first +
        # cuts-as-next-key also keeps the 8mm rows: rows and columns tie on area,
        # so the fewer-cut rows win.
        return (bbox_area, longest, offset_sum)

    @api.model
    def _tp_pattern_constructor_plan(
        self,
        cuts,
        source,
        *,
        kerf_mm,
        time_budget_s=None,
        seed_count=None,
        beam_width=None,
        first_feasible=False,
        single_sheet_only=False,
        progress_callback=None,
        cancel_callback=None,
    ):
        """CutList-style deterministic pattern candidate for one fresh source.

        The constructor works in a tiny pure-Python geometry format. Convert it
        back into the live engine plan shape so the existing guillotine validator,
        offcut scorer, cut counter, SVG/DXF rendering, and allocation code can
        judge it like any other candidate.
        """
        if not cuts or not source:
            return None
        try:
            from odoo.addons.tp_sheet_nesting_run.models.services import tp_pattern_constructor
        except Exception:
            _logger.exception("Pattern constructor import failed")
            return None

        source_w = int(source.get("width_mm") or 0)
        source_h = int(source.get("height_mm") or 0)
        if source_w <= 0 or source_h <= 0:
            return None

        pieces = [
            {
                "id": idx,
                "w": int(c.get("width_mm") or 0),
                "h": int(c.get("height_mm") or 0),
            }
            for idx, c in enumerate(cuts)
        ]
        seed_count = max(1, int(seed_count or 20))
        beam_width = max(1, int(beam_width or 160))
        v2_metrics = {}

        def build_plan_from_sheets(sheets, seed=0, sc=None, metrics=None):
            bins = []
            for sheet in sheets or []:
                transpose = False
                if int(sheet.w) == source_w and int(sheet.h) == source_h:
                    transpose = False
                elif int(sheet.w) == source_h and int(sheet.h) == source_w:
                    transpose = True
                else:
                    return None

                placements = []
                for pl in sheet.placements:
                    try:
                        cut = cuts[int(pl["item"])]
                    except Exception:
                        return None
                    x = int(pl.get("x") or 0)
                    y = int(pl.get("y") or 0)
                    fit_w = int(pl.get("w") or 0)
                    fit_h = int(pl.get("h") or 0)
                    if transpose:
                        x, y, fit_w, fit_h = y, x, fit_h, fit_w
                    if x < 0 or y < 0 or x + fit_w > source_w or y + fit_h > source_h:
                        return None
                    placements.append({
                        "cut": dict(cut),
                        "_cut_index": int(pl["item"]),
                        "x": x,
                        "y": y,
                        "fit_w": fit_w,
                        "fit_h": fit_h,
                        "used_w": fit_w,
                        "used_h": fit_h,
                        "rotated": int(fit_w) != int(cut.get("width_mm") or 0),
                        "kernel": "guillotine",
                        "pattern_strategy": getattr(sheet, "strategy", ""),
                    })
                free_rects = []
                for free in getattr(sheet, "free", []) or []:
                    fx, fy, fw, fh = [int(value or 0) for value in free]
                    if transpose:
                        fx, fy, fw, fh = fy, fx, fh, fw
                    if fw > 0 and fh > 0:
                        free_rects.append({
                            "x": fx,
                            "y": fy,
                            "w": fw,
                            "h": fh,
                            "width_mm": fw,
                            "height_mm": fh,
                        })
                bins.append({
                    "source": source,
                    "placements": placements,
                    "free_rects": free_rects,
                    "pattern_strategy": getattr(sheet, "strategy", ""),
                })

            if not bins:
                return None
            metrics = dict(metrics or {})
            return {
                "ok": True,
                "bins": bins,
                "metrics": {
                    "infeasible_reason": "",
                    "pattern_constructor": True,
                    "pattern_constructor_seed": int(seed or 0),
                    "pattern_constructor_seed_count": seed_count,
                    "pattern_constructor_search_nodes": max(1, seed_count),
                    "pattern_constructor_score": repr(sc),
                    "pattern_engine_version": metrics.get("pattern_engine_version") or "v2_beam",
                    "beam_states_evaluated": int(metrics.get("beam_states_evaluated") or 0),
                    "pattern_candidates_evaluated": int(metrics.get("pattern_candidates_evaluated") or 0),
                    "time_budget_hit": bool(metrics.get("time_budget_hit")),
                    "best_pattern_strategy": metrics.get("best_pattern_strategy") or "",
                    "selected_sheet_orientation": metrics.get("selected_sheet_orientation") or "",
                    "strategy_runtime_ms": metrics.get("strategy_runtime_ms") or {},
                    "strategy_candidate_counts": metrics.get("strategy_candidate_counts") or {},
                    "best_full_dim_offcut_mm": metrics.get("best_full_dim_offcut_mm") or "",
                    "best_offcut_mm": metrics.get("best_offcut_mm") or "",
                    "candidate_plan_count": 1,
                    "search_nodes": max(
                        1,
                        seed_count,
                        int(metrics.get("pattern_candidates_evaluated") or 0),
                    ),
                },
                "order_name": "pattern_constructor",
            }

        def on_solver_progress(payload):
            if not progress_callback:
                return
            unplaced_items = payload.get("unplaced") or []
            if unplaced_items:
                return
            plan = build_plan_from_sheets(
                payload.get("sheets") or [],
                seed=0,
                sc=payload.get("score"),
                metrics=payload.get("stats") or {},
            )
            if plan and plan.get("ok"):
                progress_callback(plan, event=payload.get("event") or "best")

        try:
            if single_sheet_only:
                deadline = time.monotonic() + float(time_budget_s) if time_budget_s else None
                sheets, seed, sc, unplaced = tp_pattern_constructor.pack_single_sheet(
                    pieces,
                    source_w,
                    source_h,
                    kerf=int(kerf_mm or 0),
                    deadline=deadline,
                )
            else:
                sheets, seed, sc, unplaced = tp_pattern_constructor.search_v2_orientations(
                    pieces,
                    source_w,
                    source_h,
                    kerf=int(kerf_mm or 0),
                    n_seeds=seed_count,
                    time_budget_s=time_budget_s,
                    beam_width=beam_width,
                    first_feasible=bool(first_feasible),
                    progress_callback=on_solver_progress if progress_callback else None,
                    cancel_callback=cancel_callback,
                )
            v2_metrics = tp_pattern_constructor.last_search_v2_metrics()
        except MemoryError:
            _logger.error(
                "Pattern constructor exhausted memory for %d pieces on source %s",
                len(pieces),
                source.get("stable_id") or source.get("id") or "",
            )
            return None
        except Exception as exc:
            _logger.warning(
                "Pattern constructor failed for %d pieces on source %s: %s",
                len(pieces),
                source.get("stable_id") or source.get("id") or "",
                exc,
            )
            return None
        if unplaced and not single_sheet_only:
            return None
        if single_sheet_only and not sheets:
            return None

        if cancel_callback and cancel_callback():
            return None

        return build_plan_from_sheets(sheets, seed=seed, sc=sc, metrics=v2_metrics)

    @api.model
    def _tp_run_offcut_prepass(
        self,
        cuts,
        offcut_sources,
        *,
        kerf_mm,
        deadline=None,
        cancel_callback=None,
    ):
        """Use compatible physical offcuts once before opening fresh sheets."""
        remaining = [dict(c) for c in (cuts or [])]
        bins = []
        started_at = time.monotonic()
        min_usage_pct = max(0, int(self.env.company.tp_nesting_offcut_min_usage_pct or 0))
        metrics = {
            "offcut_sources_considered": 0,
            "offcut_bins_used": 0,
            "offcut_panels_placed": 0,
            "offcut_prepass_search_ms": 0,
            "offcut_prepass_candidates": 0,
        }

        sources = [
            source for source in (offcut_sources or [])
            if source and source.get("is_offcut") and int(source.get("width_mm") or 0) > 0 and int(source.get("height_mm") or 0) > 0
        ]
        sources.sort(key=lambda source: (
            float(source.get("area_mm2") or (int(source.get("width_mm") or 0) * int(source.get("height_mm") or 0))),
            int(source.get("width_mm") or 0),
            int(source.get("height_mm") or 0),
            int(source.get("id") or 0),
        ))

        for source in sources:
            if not remaining:
                break
            if cancel_callback and cancel_callback():
                break
            if deadline and time.monotonic() >= deadline:
                break

            metrics["offcut_sources_considered"] += 1
            source_area = float(source.get("width_mm") or 0) * float(source.get("height_mm") or 0)
            if source_area <= 0:
                continue

            source_budget = None
            if deadline:
                # The offcut pass is intentionally cheap; keep most of the
                # global budget for the fresh-sheet v2 search.
                source_budget = max(0.05, min(0.75, deadline - time.monotonic()))
                if source_budget <= 0:
                    break

            plan = self._tp_pattern_constructor_plan(
                remaining,
                source,
                kerf_mm=kerf_mm,
                time_budget_s=source_budget,
                seed_count=1,
                beam_width=1,
                single_sheet_only=True,
                cancel_callback=cancel_callback,
            )
            metrics["offcut_prepass_search_ms"] = max(
                metrics["offcut_prepass_search_ms"],
                max(1, int((time.monotonic() - started_at) * 1000)),
            )
            if not plan or not plan.get("ok") or not plan.get("bins"):
                continue
            offcut_bin = plan["bins"][0]
            placements = offcut_bin.get("placements") or []
            if not placements:
                continue

            placed_area = sum(
                float(p.get("fit_w") or 0) * float(p.get("fit_h") or 0)
                for p in placements
            )
            usage_pct = placed_area / source_area * 100.0 if source_area else 0.0
            if min_usage_pct and usage_pct + 0.0001 < min_usage_pct:
                continue
            if not self._tp_plan_is_guillotine({"bins": [offcut_bin]}):
                continue

            placed_indices = {
                int(p.get("_cut_index"))
                for p in placements
                if p.get("_cut_index") is not None
            }
            if not placed_indices:
                continue

            bins.append(offcut_bin)
            remaining = [
                cut for idx, cut in enumerate(remaining)
                if idx not in placed_indices
            ]
            plan_metrics = plan.get("metrics") or {}
            metrics["offcut_bins_used"] += 1
            metrics["offcut_panels_placed"] += len(placed_indices)
            metrics["offcut_prepass_candidates"] += int(plan_metrics.get("pattern_candidates_evaluated") or 0)

        metrics["offcut_prepass_search_ms"] = max(
            metrics["offcut_prepass_search_ms"],
            max(1, int((time.monotonic() - started_at) * 1000)) if sources else 0,
        )
        return bins, remaining, metrics

    def _tp_run_guillotine_saw_optimal(
        self,
        cuts,
        sources,
        *,
        kerf_mm,
        offcut_sources=None,
        time_budget_s=None,
        seed_count=None,
        beam_width=None,
        first_feasible=False,
        return_first_viable=False,
        progress_callback=None,
        cancel_callback=None,
    ):
        """Current source-of-truth panel-saw planner."""

        started_at = time.monotonic()

        base = [dict(c) for c in cuts]
        if not base:
            return {
                "ok": True,
                "bins": [],
                "metrics": {
                    "infeasible_reason": "",
                    "current_source_of_truth": True,
                    "pattern_constructor_only": True,
                },
                "order_name": "pattern_constructor",
            }

        candidates = []
        rejected_count = 0
        budget = float(time_budget_s or 0.0)
        deadline = started_at + budget if budget > 0 else None
        offcut_bins, base, offcut_metrics = self._tp_run_offcut_prepass(
            base,
            offcut_sources or [],
            kerf_mm=kerf_mm,
            deadline=deadline,
            cancel_callback=cancel_callback,
        )

        def merge_with_offcuts(plan):
            if not offcut_bins:
                return plan
            merged = dict(plan)
            merged["bins"] = [dict(b) for b in offcut_bins] + [
                dict(b) for b in (plan.get("bins") or [])
            ]
            metrics = dict(plan.get("metrics") or {})
            metrics.update({
                "offcut_sources_considered": int(offcut_metrics.get("offcut_sources_considered") or 0),
                "offcut_bins_used": int(offcut_metrics.get("offcut_bins_used") or 0),
                "offcut_panels_placed": int(offcut_metrics.get("offcut_panels_placed") or 0),
                "offcut_prepass_search_ms": int(offcut_metrics.get("offcut_prepass_search_ms") or 0),
                "offcut_prepass_candidates": int(offcut_metrics.get("offcut_prepass_candidates") or 0),
                "offcut_min_usage_pct": int(self.env.company.tp_nesting_offcut_min_usage_pct or 0),
            })
            metrics["pattern_candidates_evaluated"] = (
                int(metrics.get("pattern_candidates_evaluated") or 0)
                + int(offcut_metrics.get("offcut_prepass_candidates") or 0)
            )
            metrics["search_nodes"] = max(
                1,
                int(metrics.get("search_nodes") or 0)
                + int(offcut_metrics.get("offcut_prepass_candidates") or 0),
            )
            merged["metrics"] = metrics
            return merged

        def emit_progress_with_offcuts(plan, event=None):
            if not progress_callback:
                return
            progress_callback(merge_with_offcuts(plan), event=event)

        def score_plan(plan):
            sheets, waste = self._tp_plan_sheets_and_waste(plan)
            return (
                sheets,
                round(waste),
                self._tp_plan_offcut_quality(plan),
                self._tp_count_guillotine_cuts(plan),
                self._tp_plan_grouping(plan),
                self._tp_plan_compactness(plan),
            )

        def finalize_plan(best_plan):
            elapsed_ms = max(1, int((time.monotonic() - started_at) * 1000))
            best_plan = dict(best_plan)
            best_plan["bins"] = [dict(b) for b in (best_plan.get("bins") or [])]
            metrics = dict(best_plan.get("metrics") or {})
            fresh_sheets, fresh_waste = self._tp_plan_sheets_and_waste(best_plan)
            saw_metrics = self._tp_plan_saw_metrics(best_plan)
            metrics.update({
                "search_ms": max(elapsed_ms, int(metrics.get("search_ms") or 0)),
                "search_nodes": max(
                    1,
                    len(candidates),
                    int(metrics.get("search_nodes") or 0),
                ),
                "candidate_plan_count": max(
                    len(candidates),
                    int(metrics.get("candidate_plan_count") or 0),
                ),
                "rejected_plan_count": max(
                    rejected_count,
                    int(metrics.get("rejected_plan_count") or 0),
                ),
                "full_sheet_count": fresh_sheets,
                "fresh_waste_area_mm2": fresh_waste,
                "selected_order_name": "pattern_constructor",
                "current_source_of_truth": True,
                "pattern_constructor_only": True,
                "first_feasible": bool(first_feasible),
                "return_first_viable": bool(return_first_viable),
                "saw_fence_settings": int(saw_metrics.get("fence_settings") or 0),
                "saw_cut_lines": int(saw_metrics.get("saw_operations") or 0),
                "saw_trim_cuts": int(saw_metrics.get("trim_cuts") or 0),
                "saw_separation_cuts": int(saw_metrics.get("separation_cuts") or 0),
                "saw_sheet_metrics": saw_metrics.get("sheet_metrics") or [],
            })
            best_plan["metrics"] = metrics
            best_plan["order_name"] = "pattern_constructor"
            return best_plan

        search_sources = list(sources or [])
        if offcut_bins and not base:
            offcut_only_plan = merge_with_offcuts({
                "ok": True,
                "bins": [],
                "metrics": {
                    "infeasible_reason": "",
                    "pattern_constructor": True,
                    "pattern_engine_version": "v2_beam",
                    "search_nodes": 1,
                },
                "order_name": "pattern_constructor",
            })
            candidates.append((score_plan(offcut_only_plan), offcut_only_plan))
            search_sources = []

        for source in search_sources:
            if cancel_callback and cancel_callback():
                break
            if deadline and time.monotonic() >= deadline:
                break
            source_budget = None
            if deadline:
                source_budget = max(0.05, deadline - time.monotonic())
            if base:
                plan = self._tp_pattern_constructor_plan(
                    base,
                    source,
                    kerf_mm=kerf_mm,
                    time_budget_s=source_budget if deadline else time_budget_s,
                    seed_count=seed_count,
                    beam_width=beam_width,
                    first_feasible=first_feasible,
                    progress_callback=emit_progress_with_offcuts if progress_callback else None,
                    cancel_callback=cancel_callback,
                )
            else:
                plan = {
                    "ok": True,
                    "bins": [],
                    "metrics": {
                        "infeasible_reason": "",
                        "pattern_constructor": True,
                        "pattern_engine_version": "v2_beam",
                        "search_nodes": 1,
                    },
                    "order_name": "pattern_constructor",
                }
            if not plan or not plan.get("ok") or not plan.get("bins"):
                if offcut_bins and not base and plan and plan.get("ok"):
                    plan = merge_with_offcuts(plan)
                    candidates.append((score_plan(plan), plan))
                    break
                rejected_count += 1
                continue
            plan = merge_with_offcuts(plan)
            if not self._tp_plan_is_guillotine(plan):
                rejected_count += 1
                continue
            candidates.append((score_plan(plan), plan))
            if return_first_viable:
                return finalize_plan(plan)

        elapsed_ms = max(1, int((time.monotonic() - started_at) * 1000))
        if not candidates:
            return {
                "ok": False,
                "metrics": {
                    "infeasible_reason": "guillotine_no_solution",
                    "search_ms": elapsed_ms,
                    "search_nodes": max(1, len(sources)),
                    "candidate_plan_count": len(sources),
                    "rejected_plan_count": rejected_count,
                    "selected_order_name": "",
                    "current_source_of_truth": True,
                    "pattern_constructor_only": True,
                },
            }

        _score, best_plan = min(candidates, key=lambda item: item[0])
        return finalize_plan(best_plan)

    def _tp_run_v2_pattern_constructor(
        self,
        cuts,
        sources,
        *,
        kerf_mm=None,
        trim_edge_mm=None,
        offcut_sources=None,
        time_budget_s=None,
        seed_count=None,
        beam_width=None,
        first_feasible=False,
        return_first_viable=False,
        progress_callback=None,
        cancel_callback=None,
        **_ignored_options,
    ):
        """Run the single production solver: v2 CutList-style pattern constructor."""
        kerf_mm = max(0, int(kerf_mm if kerf_mm is not None else self.env.company.tp_nesting_kerf_mm or 3))

        budget = time_budget_s if time_budget_s else (self.env.company.tp_nesting_guillotine_seconds or 10)
        return self._tp_run_guillotine_saw_optimal(
            cuts,
            sources,
            kerf_mm=kerf_mm,
            offcut_sources=offcut_sources,
            time_budget_s=budget,
            seed_count=seed_count,
            beam_width=beam_width,
            first_feasible=first_feasible,
            return_first_viable=return_first_viable,
            progress_callback=progress_callback,
            cancel_callback=cancel_callback,
        )

    @api.model
    def _tp_run_create_allocations(self, run, bins, parts):
        """Persist tp.nesting.allocation rows for traceability. Not required
        for the cost chain but lets the run's form show placements."""
        self._tp_run_assert_offcuts_available(bins, run=run)
        Alloc = self.env["tp.nesting.allocation"].sudo()
        Format = self.env["tp.sheet.format"].sudo()
        parts_by_id = {p.id: p for p in parts}

        Offcut = self.env["tp.offcut"].sudo()
        reserved_offcut_ids = set()
        for bin_idx, bin_data in enumerate(bins):
            source = bin_data.get("source") or {}
            is_offcut = bool(source.get("is_offcut"))
            sheet_format = False
            offcut = False
            if is_offcut:
                offcut = Offcut.browse(source.get("id"))
                offcut = offcut if offcut.exists() else False
                if offcut and offcut.id not in reserved_offcut_ids:
                    offcut.action_set_reserved(run.mo_id.id if run.mo_id else False, run.id)
                    reserved_offcut_ids.add(offcut.id)
            else:
                sheet_format = Format.browse(source.get("id"))
                sheet_format = sheet_format if sheet_format.exists() else False
            placements = bin_data.get("placements") or []

            if is_offcut and offcut and offcut.offcut_ref:
                # Quote the simple sequential offcut ID so the operator knows
                # exactly which physical offcut to pull from the bin.
                bin_label = "Offcut #%d (%dx%dmm)" % (
                    offcut.offcut_ref, int(offcut.width_mm or 0), int(offcut.height_mm or 0),
                )
            else:
                bin_label = (
                    (source.get("record") and source["record"].display_name)
                    or ("offcut %d" if is_offcut else "sheet %d") % (bin_idx + 1)
                )
            bin_key = ("offcut:%s:%s" if is_offcut else "sheet_format:%s:%s") % (source.get("id"), bin_idx)

            for placement in placements:
                cut = placement.get("cut") or {}
                wcp_id = cut.get("source_web_cut_part_id")
                part = parts_by_id.get(wcp_id) if wcp_id else None
                fit_w = int(placement.get("fit_w") or 0)
                fit_h = int(placement.get("fit_h") or 0)
                Alloc.create({
                    "run_id": run.id,
                    "source_type": "offcut" if is_offcut else "sheet",
                    "source_offcut_id": offcut.id if offcut else False,
                    "source_sheet_format_id": sheet_format.id if sheet_format else False,
                    "source_so_line_id": part.sale_order_line_id.id if (part and part.sale_order_line_id) else False,
                    "web_cut_part_id": part.id if part else False,
                    "cut_width_mm": fit_w,
                    "cut_height_mm": fit_h,
                    "cut_quantity": 1,
                    "rotation_applied": bool(placement.get("rotated")),
                    "placed_x_mm": int(placement.get("x") or 0),
                    "placed_y_mm": int(placement.get("y") or 0),
                    "source_bin_key": bin_key,
                    "source_bin_label": bin_label,
                    "allocated_area_mm2": float(fit_w * fit_h),
                    "status": "reserved" if is_offcut else "allocated",
                })

    @api.model
    def _tp_run_finalize_metrics(self, run, plan):
        metrics = dict((plan or {}).get("metrics") or {})
        bins = (plan or {}).get("bins") or []
        fresh_source_area = 0.0
        fresh_placed_area = 0.0
        offcut_source_area = 0.0
        offcut_placed_area = 0.0
        placed_area = 0.0
        fresh_bin_count = 0
        for bin_data in bins:
            source = bin_data.get("source") or {}
            source_area = float(source.get("width_mm") or 0.0) * float(source.get("height_mm") or 0.0)
            is_offcut = bool(source.get("is_offcut"))
            if is_offcut:
                offcut_source_area += source_area
            else:
                fresh_bin_count += 1
                fresh_source_area += source_area
            for placement in bin_data.get("placements") or []:
                placement_area = float(placement.get("fit_w") or 0.0) * float(placement.get("fit_h") or 0.0)
                placed_area += placement_area
                if is_offcut:
                    offcut_placed_area += placement_area
                else:
                    fresh_placed_area += placement_area

        score_breakdown = metrics.get("score_breakdown") or {}
        debug_artifact = metrics.get("debug_artifact") or {}
        if not metrics.get("saw_cut_lines"):
            saw_metrics = self._tp_plan_saw_metrics({"bins": bins})
            metrics["saw_fence_settings"] = int(saw_metrics.get("fence_settings") or 0)
            metrics["saw_cut_lines"] = int(saw_metrics.get("saw_operations") or 0)
            metrics["saw_trim_cuts"] = int(saw_metrics.get("trim_cuts") or 0)
            metrics["saw_separation_cuts"] = int(saw_metrics.get("separation_cuts") or 0)
            metrics["saw_sheet_metrics"] = saw_metrics.get("sheet_metrics") or []
        saw_sheet_metrics = metrics.get("saw_sheet_metrics") or []
        if saw_sheet_metrics:
            debug_artifact = dict(debug_artifact or {})
            debug_artifact["saw_sheet_metrics"] = saw_sheet_metrics
        offcut_utilization_pct = (
            offcut_placed_area / offcut_source_area * 100.0
            if offcut_source_area
            else 0.0
        )
        run.sudo().write(
            {
                "search_nodes": int(metrics.get("search_nodes") or 0),
                "search_ms": int(metrics.get("search_ms") or 0),
                "waste_area_mm2_total": max(0.0, fresh_source_area - fresh_placed_area),
                "offcut_utilization_pct": offcut_utilization_pct,
                "full_sheet_count": int(metrics.get("full_sheet_count") or fresh_bin_count),
                "score": float(placed_area),
                "selected_order_name": metrics.get("selected_order_name") or "",
                "scoring_preset": metrics.get("policy_preset") or "",
                "candidate_plan_count": int(metrics.get("candidate_plan_count") or 0),
                "rejected_plan_count": int(metrics.get("rejected_plan_count") or 0),
                "score_breakdown_json": json.dumps(score_breakdown, sort_keys=True),
                "debug_artifact_json": json.dumps(debug_artifact, sort_keys=True) if debug_artifact else False,
            }
        )
        write_vals = {}
        if "x_tp_saw_operation_count" in run._fields:
            write_vals["x_tp_saw_operation_count"] = int(metrics.get("saw_cut_lines") or 0)
        if "x_tp_trim_cut_count" in run._fields:
            write_vals["x_tp_trim_cut_count"] = int(metrics.get("saw_trim_cuts") or 0)
        if "x_tp_fence_setting_count" in run._fields:
            write_vals["x_tp_fence_setting_count"] = int(metrics.get("saw_fence_settings") or 0)
        if write_vals:
            run.sudo().write(write_vals)
