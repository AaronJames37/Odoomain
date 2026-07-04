import base64
import html
import io
import logging
import math
import os
import time
import zipfile

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


# SVG palette for the bounding-box render. Distinct colors for visual variety.
PALETTE = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
    "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
]


class TpNestingSandbox(models.TransientModel):
    _name = "tp.nesting.sandbox"
    _description = "Nesting Sandbox (no MO)"

    job_id = fields.Many2one(
        "tp.nesting.job",
        string="Nesting Job",
        required=True,
        readonly=True,
        ondelete="cascade",
    )
    sale_order_id = fields.Many2one(
        related="job_id.sale_order_id",
        readonly=True,
        string="Sale Order",
    )
    demand_product_id = fields.Many2one(
        related="job_id.demand_product_id",
        readonly=True,
        string="Product",
    )

    sheet_format_id = fields.Many2one(
        "tp.sheet.format",
        string="Sheet to nest against",
        domain="[('product_id', '=', demand_product_id)]",
        required=True,
        help="Pick the sheet size to try fitting the panels onto. Defaults to the largest format for the product.",
    )
    kerf_mm = fields.Integer(
        string="Kerf (mm)",
        default=3,
        required=True,
        help="Tool kerf width in millimetres. Default 3mm matches the production engine setting.",
    )
    trim_edge_mm = fields.Integer(
        string="Trim Edge (mm, discontinued)",
        default=0,
        required=True,
        help="Deprecated. Trim edge is no longer used by the nesting solver.",
    )
    # Read-only outputs filled in by action_run.
    result_summary = fields.Text(string="Summary", readonly=True)
    result_svg = fields.Html(
        string="Nest Preview",
        readonly=True,
        sanitize=False,
    )
    state = fields.Selection(
        [("draft", "Draft"), ("done", "Done"), ("failed", "Failed")],
        default="draft",
        readonly=True,
    )

    # DXF bundle: one .dxf per sheet, packaged in a single .zip so the
    # operator drops each per-sheet file straight onto the CNC without
    # having to split a multi-sheet document.
    result_dxf_bytes = fields.Binary(
        string="DXF Bundle (.zip)",
        readonly=True,
        attachment=False,
    )
    result_dxf_filename = fields.Char(readonly=True)

    # ------------------------------------------------------------------
    # Defaults
    # ------------------------------------------------------------------
    @api.model
    def default_get(self, fields_list):
        vals = super().default_get(fields_list)
        if "kerf_mm" in fields_list:
            vals["kerf_mm"] = self.env.company.tp_nesting_kerf_mm or 3
        if "trim_edge_mm" in fields_list:
            vals["trim_edge_mm"] = 0
        active_id = self.env.context.get("active_id")
        active_model = self.env.context.get("active_model")
        if active_model == "tp.nesting.job" and active_id:
            job = self.env["tp.nesting.job"].browse(active_id)
            if job.exists():
                vals["job_id"] = job.id
                fmt = self._tp_default_sheet_format(job.demand_product_id)
                if fmt:
                    vals["sheet_format_id"] = fmt.id
        return vals

    @api.onchange("job_id")
    def _onchange_job_id_set_sheet_default(self):
        for wiz in self:
            if wiz.job_id and not wiz.sheet_format_id:
                fmt = self._tp_default_sheet_format(wiz.demand_product_id)
                if fmt:
                    wiz.sheet_format_id = fmt

    @api.model
    def _tp_default_sheet_format(self, product):
        if not product:
            return self.env["tp.sheet.format"]
        return self.env["tp.sheet.format"].search(
            [("product_id", "=", product.id)],
            order="width_mm desc, height_mm desc",
            limit=1,
        )

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------
    def action_run(self):
        self.ensure_one()
        if not self.sheet_format_id:
            raise UserError("Pick a sheet to nest against.")
        if not self.job_id:
            raise UserError("No nesting job selected.")

        all_parts = self.job_id._tp_website_panel_parts()
        if not all_parts:
            raise UserError(
                "This nesting job has no website panel rows to nest. "
                "Confirm the order first or click 'Pull Website Jobs'."
            )
        parts, excluded_cutout_parts = self.job_id._tp_partition_cutout_parts(all_parts)
        if not parts:
            raise UserError(
                "Every panel in this job has cut-outs and the company setting "
                "'Include Cut-Out Parts In Nesting' is disabled. Enable it in "
                "Settings → Sheet Nesting to nest these panels."
            )

        cuts = self._tp_build_cuts(parts)
        # Offer every active stock sheet of the same material + thickness as the
        # selected format, not just the one size, so the multi-size guillotine
        # optimiser can mix sizes (e.g. 1x3050 + 2x2440) and reach CutList-grade
        # utilisation — matching the website quote and production job. Falls back
        # to the single selected sheet if sibling discovery finds nothing.
        try:
            siblings = self.sheet_format_id._tp_quote_sibling_sheets(self.sheet_format_id)
        except Exception:  # noqa: BLE001 — degrade to the single selected sheet
            _logger.exception("Sandbox sibling-sheet discovery failed")
            siblings = self.sheet_format_id
        if not siblings:
            siblings = self.sheet_format_id
        sources = [self._tp_sheet_format_source(s) for s in siblings]

        t0 = time.perf_counter()
        try:
            best_plan = self.env["tp.nesting.job"]._tp_run_v2_pattern_constructor(
                cuts,
                sources,
                kerf_mm=max(0, int(self.kerf_mm or 0)),
                trim_edge_mm=0,
                time_budget_s=self.env.company.tp_nesting_guillotine_seconds or 10,
            )
        except Exception as exc:
            _logger.exception("Sandbox v2 nesting engine raised")
            best_plan = {"ok": False, "metrics": {"infeasible_reason": str(exc)}}
        best_ms = (time.perf_counter() - t0) * 1000.0
        if not best_plan.get("ok"):
            metrics = best_plan.get("metrics") or {}
            reason = metrics.get("infeasible_reason") or "unknown"
            err_cut = best_plan.get("error_cut") or {}
            msg = "V2 pattern constructor could not pack: %s" % reason
            if err_cut:
                msg += " (failed on %sx%s)" % (err_cut.get("width_mm"), err_cut.get("height_mm"))
            self.write({
                "state": "failed",
                "result_summary": msg,
                "result_svg": False,
                "result_dxf_bytes": False,
                "result_dxf_filename": False,
            })
            return self._reopen_form()

        best_kernel = "v2_pattern_constructor"
        comparison_lines = "Solver:         v2 pattern constructor (%.0f ms)" % best_ms

        bins = best_plan.get("bins") or []
        summary_base = self._tp_format_summary(cuts, bins, best_plan.get("metrics") or {})
        summary_parts = [summary_base]
        if excluded_cutout_parts:
            instances = sum(max(1, int(p.quantity or 1)) for p in excluded_cutout_parts)
            summary_parts.append(
                "Excluded %d cut-out panel%s (%d instance%s) — "
                "'Include Cut-Out Parts In Nesting' is disabled."
                % (
                    len(excluded_cutout_parts),
                    "" if len(excluded_cutout_parts) == 1 else "s",
                    instances,
                    "" if instances == 1 else "s",
                )
            )
        summary_parts.append(comparison_lines)
        summary = "\n\n".join(s for s in summary_parts if s)
        svg = self._tp_render_svg(bins=bins, cuts=cuts, parts=parts)
        dxf_bytes, dxf_name = self._tp_build_dxf(bins=bins, parts=parts)
        self.write({
            "state": "done",
            "result_summary": summary,
            "result_svg": svg,
            "result_dxf_bytes": base64.b64encode(dxf_bytes) if dxf_bytes else False,
            "result_dxf_filename": dxf_name,
        })
        return self._reopen_form()

    def action_download_dxf(self):
        self.ensure_one()
        if not self.result_dxf_bytes:
            raise UserError("Run the preview first to generate a DXF.")
        # Stream the binary field directly via /web/content with download=true.
        return {
            "type": "ir.actions.act_url",
            "url": (
                "/web/content/?model=%s&id=%s&field=result_dxf_bytes"
                "&filename_field=result_dxf_filename&download=true"
            ) % (self._name, self.id),
            "target": "self",
        }

    def _reopen_form(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
            "context": dict(self.env.context),
        }

    # ------------------------------------------------------------------
    # Engine input construction
    # ------------------------------------------------------------------
    @api.model
    def _tp_build_cuts(self, parts):
        cuts = []
        for part in parts:
            for instance in range(max(1, int(part.quantity or 1))):
                cuts.append({
                    "width_mm": int(part.width_mm or 0),
                    "height_mm": int(part.height_mm or 0),
                    "source_mo_id": 0,
                    "source_so_line_id": part.sale_order_line_id.id if part.sale_order_line_id else 0,
                    "source_web_cut_part_id": part.id,
                    "_sandbox_instance_index": instance,
                })
        cuts.sort(key=lambda c: c["width_mm"] * c["height_mm"], reverse=True)
        return cuts

    @api.model
    def _tp_sheet_format_source(self, sheet):
        width = int(sheet.width_mm or 0)
        height = int(sheet.height_mm or 0)
        area = float(width * height)
        unit_cost = float(getattr(sheet, "landed_cost", 0.0) or 0.0)
        return {
            "kind": "sheet_format",
            "stable_id": f"sandbox_sheet_format:{sheet.id}",
            "id": sheet.id,
            "record": sheet,
            "product_id": sheet.product_id.id if sheet.product_id else 0,
            "lot_id": 0,
            "width_mm": width,
            "height_mm": height,
            "area_mm2": area,
            "unit_cost": unit_cost,
            "effective_cost_per_area": (unit_cost / area) if area > 0 else 0.0,
        }

    # ------------------------------------------------------------------
    # Result formatting
    # ------------------------------------------------------------------
    @api.model
    def _tp_format_summary(self, cuts, bins, metrics):
        panels_requested = len(cuts)
        placed = sum(len(b.get("placements") or []) for b in bins)
        sheets_used = len(bins)
        saw_cuts = int(metrics.get("saw_cut_lines") or 0)
        if not saw_cuts:
            Job = self.env["tp.nesting.job"].sudo()
            saw_cuts = Job._tp_count_guillotine_cuts({"bins": bins})
        # Compute real waste from placements (engine metrics aren't always populated).
        src_area = 0.0
        used_area = 0.0
        for b in bins:
            s = b.get("source") or {}
            src_area += float(s.get("width_mm") or 0) * float(s.get("height_mm") or 0)
            for p in (b.get("placements") or []):
                used_area += float(p.get("fit_w") or 0) * float(p.get("fit_h") or 0)
        waste_area = max(0.0, src_area - used_area)
        waste_pct = (waste_area / src_area * 100.0) if src_area else 0.0
        utilisation = (used_area / src_area * 100.0) if src_area else 0.0

        lines = [
            f"Panels requested: {panels_requested}",
            f"Panels placed:    {placed}",
            f"Saw cuts:         {saw_cuts}",
            f"Sheets used:    {sheets_used}",
            f"Sheet area:     {src_area / 1_000_000.0:.4f} m²",
            f"Used area:      {used_area / 1_000_000.0:.4f} m²",
            f"Waste area:     {waste_area / 1_000_000.0:.4f} m² ({waste_pct:.2f}%)",
            f"Utilisation:    {utilisation:.2f}%",
        ]
        if metrics.get("search_ms"):
            lines.append(f"Engine time:    {metrics['search_ms']} ms")
        return "\n".join(lines)

    @api.model
    def _tp_render_svg(self, bins, cuts, parts):
        """Bounding-box SVG render of the nest result. CNC features
        (holes/cutouts/radii) overlaid in phase 2 — for now, just the
        placed rectangles, color-coded by source panel."""
        parts_by_id = {p.id: p for p in parts}
        chunks = []
        for bin_idx, bin_data in enumerate(bins):
            source = bin_data.get("source") or {}
            sheet_w = int(source.get("width_mm") or 0)
            sheet_h = int(source.get("height_mm") or 0)
            placements = bin_data.get("placements") or []
            if sheet_w <= 0 or sheet_h <= 0:
                continue
            base_label = (source.get("record") and source["record"].display_name) or f"Sheet {bin_idx+1}"
            if source.get("is_offcut"):
                ref = source.get("offcut_ref") or (source.get("record") and getattr(source["record"], "offcut_ref", 0))
                base_label = f"OFFCUT #{ref}" if ref else f"OFFCUT · {base_label}"
            chunks.append(self._tp_render_one_sheet(
                bin_idx=bin_idx,
                sheet_w=sheet_w,
                sheet_h=sheet_h,
                placements=placements,
                parts_by_id=parts_by_id,
                source_label=base_label,
            ))
        if not chunks:
            return "<div><em>Nothing placed.</em></div>"
        return "\n".join(chunks)

    @staticmethod
    def _tp_clip_cut_lines_around_panels(segments, rects, *, tol=1):
        """Remove any cut-guide segment portions that cross panel interiors."""
        panels = []
        for x, y, w, h in rects:
            x = int(x)
            y = int(y)
            w = int(w)
            h = int(h)
            if w > 0 and h > 0:
                panels.append((x, y, x + w, y + h))

        def subtract_interval(intervals, block_start, block_end):
            out = []
            block_start = int(block_start)
            block_end = int(block_end)
            if block_end < block_start:
                block_start, block_end = block_end, block_start
            for start, end in intervals:
                overlap_start = max(start, block_start)
                overlap_end = min(end, block_end)
                if overlap_end <= overlap_start + tol:
                    out.append((start, end))
                    continue
                if overlap_start > start + tol:
                    out.append((start, overlap_start))
                if end > overlap_end + tol:
                    out.append((overlap_end, end))
            return out

        clipped = []
        for x1, y1, x2, y2 in segments:
            x1 = int(round(x1))
            y1 = int(round(y1))
            x2 = int(round(x2))
            y2 = int(round(y2))
            if abs(x1 - x2) <= tol:
                x = x1
                start, end = sorted((y1, y2))
                intervals = [(start, end)]
                for px0, py0, px1, py1 in panels:
                    if px0 + tol < x < px1 - tol:
                        intervals = subtract_interval(intervals, py0, py1)
                        if not intervals:
                            break
                clipped.extend((x, a, x, b) for a, b in intervals if b > a + tol)
                continue

            if abs(y1 - y2) <= tol:
                y = y1
                start, end = sorted((x1, x2))
                intervals = [(start, end)]
                for px0, py0, px1, py1 in panels:
                    if py0 + tol < y < py1 - tol:
                        intervals = subtract_interval(intervals, px0, px1)
                        if not intervals:
                            break
                clipped.extend((a, y, b, y) for a, b in intervals if b > a + tol)
                continue

            clipped.append((x1, y1, x2, y2))

        seen = set()
        unique = []
        for seg in clipped:
            if seg in seen:
                continue
            seen.add(seg)
            unique.append(seg)
        return unique

    @api.model
    def _tp_render_one_sheet(self, bin_idx, sheet_w, sheet_h, placements, parts_by_id, source_label):
        # Pixel scale: aim for ~800px wide max.
        pixel_max = 800
        scale = pixel_max / max(sheet_w, sheet_h)
        svg_w = int(sheet_w * scale) + 40
        svg_h = int(sheet_h * scale) + 60

        # Sheet rect: drawn with screen coords (top-left origin) for SVG.
        # Panel-local (bottom-left, Y-up) is converted to screen
        # (top-left, Y-down) by: screen_y = sheet_h - (panel_y + h).
        parts_drawn = []
        for idx, p in enumerate(placements):
            x_mm = int(p.get("x") or 0)
            y_mm = int(p.get("y") or 0)
            w_mm = int(p.get("fit_w") or 0)
            h_mm = int(p.get("fit_h") or 0)
            rotated = bool(p.get("rotated"))
            cut = p.get("cut") or {}
            web_part_id = cut.get("source_web_cut_part_id")
            part = parts_by_id.get(web_part_id) if web_part_id else None
            color = PALETTE[idx % len(PALETTE)]
            screen_x = 20 + int(x_mm * scale)
            screen_y = 30 + int((sheet_h - y_mm - h_mm) * scale)
            screen_w = max(1, int(w_mm * scale))
            screen_h = max(1, int(h_mm * scale))

            label = ""
            if part:
                cnc_flag = " [CNC]" if getattr(part, "cnc_required", False) else ""
                so_name = (part.sale_order_id.name if part.sale_order_id else "") or part.odoo_order_name or "?"
                label = f"{so_name}{cnc_flag}"
                if rotated:
                    label += " ↻"

            # Dimension labels: width value runs horizontally along the panel's
            # width (bottom edge); length value runs vertically down the panel's
            # height (left edge, rotated -90°).
            cx = screen_x + screen_w / 2.0
            cy = screen_y + screen_h / 2.0
            width_label = (
                f'<text x="{cx:.1f}" y="{screen_y + screen_h - 4:.1f}" '
                f'text-anchor="middle" font-family="monospace" font-size="10" '
                f'fill="#222">{w_mm}</text>'
            )
            length_label = (
                f'<text x="{screen_x + 11:.1f}" y="{cy:.1f}" '
                f'text-anchor="middle" font-family="monospace" font-size="10" '
                f'fill="#222" transform="rotate(-90 {screen_x + 11:.1f} {cy:.1f})">{h_mm}</text>'
            )

            parts_drawn.append(
                f'<rect x="{screen_x}" y="{screen_y}" '
                f'width="{screen_w}" height="{screen_h}" '
                f'fill="{color}" fill-opacity="0.35" '
                f'stroke="{color}" stroke-width="1.5"/>'
                f'<text x="{screen_x + 4}" y="{screen_y + 14}" '
                f'font-family="monospace" font-size="11" fill="#222">{html.escape(label)}</text>'
                f'{width_label}{length_label}'
            )

            # CNC overlay: holes, cutouts, radii drawn on top of the rect.
            if part and getattr(part, "cnc_required", False):
                parts_drawn.append(self._tp_render_cnc_overlay(
                    part=part,
                    sheet_h_mm=sheet_h,
                    place_x_mm=x_mm,
                    place_y_mm=y_mm,
                    place_w_mm=w_mm,
                    place_h_mm=h_mm,
                    rotated=rotated,
                    scale=scale,
                ))

        sheet_rect = (
            f'<rect x="20" y="30" width="{int(sheet_w * scale)}" '
            f'height="{int(sheet_h * scale)}" fill="white" stroke="#444" stroke-width="2"/>'
        )

        # Subtle guillotine cut lines (rips + crosscuts), drawn under the labels.
        cut_lines_svg = []
        try:
            from odoo.addons.tp_sheet_nesting.models.services.tp_guillotine_cuts import (
                panel_saw_cut_lines,
            )
            rects = [(int(p.get("x") or 0), int(p.get("y") or 0),
                      int(p.get("fit_w") or 0), int(p.get("fit_h") or 0)) for p in placements]
            cut_segments = self._tp_clip_cut_lines_around_panels(
                panel_saw_cut_lines(rects, sheet_w, sheet_h),
                rects,
            )
            for (x1, y1, x2, y2) in cut_segments:
                sx1 = 20 + x1 * scale
                sy1 = 30 + (sheet_h - y1) * scale
                sx2 = 20 + x2 * scale
                sy2 = 30 + (sheet_h - y2) * scale
                cut_lines_svg.append(
                    f'<line x1="{sx1:.1f}" y1="{sy1:.1f}" x2="{sx2:.1f}" y2="{sy2:.1f}" '
                    f'stroke="#7a7a7a" stroke-width="0.6" stroke-dasharray="4 3" stroke-opacity="0.55"/>'
                )
        except Exception:  # noqa: BLE001 — cut overlay is decorative
            cut_lines_svg = []

        title = (
            f'<text x="20" y="20" font-family="sans-serif" font-size="13" fill="#222">'
            f'{html.escape(source_label)} — {sheet_w}×{sheet_h}mm — {len(placements)} pieces placed</text>'
        )

        return (
            f'<div style="margin-bottom:12px;">'
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="0 0 {svg_w} {svg_h}" '
            f'width="{svg_w}" height="{svg_h}" '
            f'style="background:#f8f8f8;border:1px solid #ddd;">'
            f'{title}{sheet_rect}{"".join(parts_drawn)}{"".join(cut_lines_svg)}'
            f'</svg></div>'
        )

    # ------------------------------------------------------------------
    # CNC feature overlay
    # ------------------------------------------------------------------
    # Coordinate conventions (all in mm unless noted):
    #
    #   Panel-local:   bottom-left = (0, 0), Y-up. Original (unrotated)
    #                  dimensions = part.width_mm × part.height_mm.
    #
    #   Sheet space:   bottom-left = (0, 0), Y-up. Engine emits placements
    #                  in this space.
    #
    #   Screen space:  top-left = (20, 30) inside the SVG viewBox (the
    #                  +20/+30 are the margins around the sheet rect),
    #                  Y-down. We flip with screen_y = sheet_h - y.
    #
    # A panel may be 'rotated' (placed 90° CCW). For a rotated panel the
    # feature transform from panel-local -> placed is:
    #     (px, py) -> (panel_h - py, px)
    # Otherwise it's the identity.
    @api.model
    def _tp_render_cnc_overlay(
        self,
        *,
        part,
        sheet_h_mm,
        place_x_mm,
        place_y_mm,
        place_w_mm,
        place_h_mm,
        rotated,
        scale,
    ):
        # Original (unrotated) panel dimensions.
        pw = int(part.width_mm or 0)
        ph = int(part.height_mm or 0)
        if pw <= 0 or ph <= 0:
            return ""

        # mm-space transform: panel-local (px, py) -> sheet-space (sx, sy).
        # Returned coords are still sheet-space (bottom-left, Y-up, mm).
        def panel_to_sheet(px, py):
            if rotated:
                lx = ph - py
                ly = px
            else:
                lx = px
                ly = py
            return place_x_mm + lx, place_y_mm + ly

        # mm-space -> screen-space (top-left, Y-down, pixels).
        def sheet_to_screen(sx, sy):
            screen_x = 20 + sx * scale
            screen_y = 30 + (sheet_h_mm - sy) * scale
            return screen_x, screen_y

        layers = []

        # --- holes (drilled circles) -------------------------------------
        for hole in (part.holes or []):
            if not isinstance(hole, dict):
                continue
            # Website may emit either x/y/diameter or xMm/yMm/diameterMm
            hx = float(hole.get("xMm") or hole.get("x") or 0)
            hy = float(hole.get("yMm") or hole.get("y") or 0)
            diam = float(
                hole.get("diameterMm")
                or hole.get("diameter")
                or hole.get("d")
                or 0
            )
            if diam <= 0:
                continue
            sx, sy = panel_to_sheet(hx, hy)
            cx_px, cy_px = sheet_to_screen(sx, sy)
            r_px = max(1.0, (diam / 2.0) * scale)
            layers.append(
                f'<circle cx="{cx_px:.2f}" cy="{cy_px:.2f}" r="{r_px:.2f}" '
                f'fill="none" stroke="#cc0000" stroke-width="1.5"/>'
            )

        # --- inner cutouts (rectangles drawn axis-aligned in panel-local) ---
        # When rotated, a panel-local axis-aligned rect maps to a still-
        # axis-aligned rect in sheet space (because the rotation is exactly 90°).
        # Cut-outs that cross the panel boundary are clipped to the panel
        # bbox so they don't visually leak onto neighbouring placed panels —
        # the DXF builder merges those into the outer perimeter instead.
        interior_pockets, edge_notches = self._tp_classify_cut_outs(
            part=part, pw=pw, ph=ph,
        )
        preview_rects = [
            (cx, cy, cx + cw, cy + ch) for (cx, cy, cw, ch) in interior_pockets
        ]
        preview_rects.extend(edge_notches)
        for (lx0, ly0, lx1, ly1) in preview_rects:
            sx1, sy1 = panel_to_sheet(lx0, ly0)
            sx2, sy2 = panel_to_sheet(lx1, ly1)
            lo_x_mm, hi_x_mm = sorted((sx1, sx2))
            lo_y_mm, hi_y_mm = sorted((sy1, sy2))
            top_left_x_px, top_left_y_px = sheet_to_screen(lo_x_mm, hi_y_mm)
            bot_right_x_px, bot_right_y_px = sheet_to_screen(hi_x_mm, lo_y_mm)
            w_px = max(1.0, bot_right_x_px - top_left_x_px)
            h_px = max(1.0, bot_right_y_px - top_left_y_px)
            layers.append(
                f'<rect x="{top_left_x_px:.2f}" y="{top_left_y_px:.2f}" '
                f'width="{w_px:.2f}" height="{h_px:.2f}" '
                f'fill="none" stroke="#0066cc" stroke-width="1.5" '
                f'stroke-dasharray="4 2"/>'
            )

        # --- corner radii: draw quarter-arc markers at the four corners ----
        # The website stores corner radii under one of several key shapes
        # (topLeftMm / topLeft / TL). We accept any of them.
        radii_dict = part.radii or {}
        if isinstance(radii_dict, dict):
            corner_keys = {
                "topLeft":     ("topLeftMm",     "topLeft",     "TL"),
                "topRight":    ("topRightMm",    "topRight",    "TR"),
                "bottomLeft":  ("bottomLeftMm",  "bottomLeft",  "BL"),
                "bottomRight": ("bottomRightMm", "bottomRight", "BR"),
            }
            # Where each corner sits in panel-local space (px, py).
            corner_local = {
                "topLeft":     (0,  ph),
                "topRight":    (pw, ph),
                "bottomLeft":  (0,  0),
                "bottomRight": (pw, 0),
            }
            for corner_name, key_aliases in corner_keys.items():
                r_mm = 0.0
                for k in key_aliases:
                    if k in radii_dict and radii_dict[k]:
                        try:
                            r_mm = float(radii_dict[k])
                            break
                        except (TypeError, ValueError):
                            continue
                if r_mm <= 0:
                    continue
                px, py = corner_local[corner_name]
                sx, sy = panel_to_sheet(px, py)
                ccx, ccy = sheet_to_screen(sx, sy)
                r_px = max(1.5, r_mm * scale)
                # Small filled marker at the corner — full arc would need
                # us to know the inward normal which is fiddly post-rotation.
                # For visual confirmation a marker is enough; the DXF later
                # will draw the actual radius geometry.
                layers.append(
                    f'<circle cx="{ccx:.2f}" cy="{ccy:.2f}" r="{r_px:.2f}" '
                    f'fill="#ff8800" fill-opacity="0.35" stroke="#aa5500" stroke-width="1"/>'
                )

        return "".join(layers)

    # ------------------------------------------------------------------
    # DXF builder
    # ------------------------------------------------------------------
    # DXF convention (matches what the operator's CAM expects):
    #   - Units: millimetres ($INSUNITS = 4)
    #   - Origin: bottom-left of the sheet, Y-up (right-handed)
    #   - ONE DXF FILE PER SHEET so the operator drops each straight on the
    #     CNC without having to split a multi-sheet file. We then bundle
    #     all per-sheet DXFs into a single ZIP for the user to download.
    #
    # Layers (same on every sheet's DXF):
    #   CMP_SHEET         — sheet boundary + text annotations (info only)
    #   CMP_CUT_OUTLINE   — outer profile of each panel (closed LWPOLYLINE)
    #   CMP_HOLES         — drill operations (CIRCLE entities)
    #   CMP_INNER_CUTOUTS — interior pockets/cutouts (closed LWPOLYLINE)
    @api.model
    def _tp_build_dxf(self, bins, parts):
        """Build a ZIP archive containing one DXF per sheet.

        Returns (zip_bytes, zip_filename). When there are no bins, returns
        (b"", "")."""
        if not bins:
            return b"", ""
        parts_by_id = {p.id: p for p in parts}

        # ezdxf's import-time config loader stats 'ezdxf.ini' in cwd, which
        # raises PermissionError when cwd is unreadable to the odoo user
        # (e.g. /root). Chdir to /tmp first; restore afterwards.
        prev_cwd = os.getcwd()
        try:
            os.chdir("/tmp")
            import ezdxf  # noqa: WPS433 — lazy import after chdir
        finally:
            try:
                os.chdir(prev_cwd)
            except OSError:
                pass

        so_name = (self.sale_order_id.name or "sandbox").replace(" ", "_")
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for bin_idx, bin_data in enumerate(bins):
                source = bin_data.get("source") or {}
                sheet_w = int(source.get("width_mm") or 0)
                sheet_h = int(source.get("height_mm") or 0)
                if sheet_w <= 0 or sheet_h <= 0:
                    continue

                dxf_bytes = self._tp_build_single_sheet_dxf(
                    ezdxf_module=ezdxf,
                    bin_data=bin_data,
                    bin_idx=bin_idx,
                    parts_by_id=parts_by_id,
                )
                entry_name = (
                    f"{so_name}_sheet_{bin_idx + 1:02d}_"
                    f"{sheet_w}x{sheet_h}.dxf"
                )
                zf.writestr(entry_name, dxf_bytes)

        return zip_buf.getvalue(), self._tp_dxf_filename()

    def _tp_dxf_filename(self):
        self.ensure_one()
        so_name = (self.sale_order_id.name or "sandbox").replace(" ", "_")
        return f"nest_preview_{so_name}.zip"

    @api.model
    def _tp_build_single_sheet_dxf(self, *, ezdxf_module, bin_data, bin_idx, parts_by_id):
        """Build the DXF byte string for a single sheet's bin."""
        source = bin_data.get("source") or {}
        sheet_w = int(source.get("width_mm") or 0)
        sheet_h = int(source.get("height_mm") or 0)
        sheet_label = (
            (source.get("record") and source["record"].display_name)
            or f"Sheet {bin_idx + 1}"
        )
        placements = bin_data.get("placements") or []

        doc = ezdxf_module.new(dxfversion="R2010", setup=True)
        doc.units = 4  # ezdxf.units.MM == 4
        doc.header["$INSUNITS"] = 4

        layers = doc.layers
        layers.add(name="CMP_SHEET",         color=8)
        layers.add(name="CMP_CUT_OUTLINE",   color=7)
        layers.add(name="CMP_HOLES",         color=1)
        layers.add(name="CMP_INNER_CUTOUTS", color=5)

        msp = doc.modelspace()
        self._tp_dxf_render_sheet(
            msp=msp,
            origin_x=0,
            origin_y=0,
            sheet_w=sheet_w,
            sheet_h=sheet_h,
            sheet_label=sheet_label,
            placements=placements,
            parts_by_id=parts_by_id,
        )

        buf = io.StringIO()
        doc.write(buf)
        return buf.getvalue().encode("utf-8")

    @api.model
    def _tp_dxf_render_sheet(
        self,
        *,
        msp,
        origin_x,
        origin_y,
        sheet_w,
        sheet_h,
        sheet_label,
        placements,
        parts_by_id,
    ):
        # Sheet outline (info layer; operator doesn't cut this)
        msp.add_lwpolyline(
            [
                (origin_x,           origin_y),
                (origin_x + sheet_w, origin_y),
                (origin_x + sheet_w, origin_y + sheet_h),
                (origin_x,           origin_y + sheet_h),
            ],
            close=True,
            dxfattribs={"layer": "CMP_SHEET"},
        )
        msp.add_text(
            f"{sheet_label}  {sheet_w}x{sheet_h}mm",
            height=20,
            dxfattribs={
                "layer": "CMP_SHEET",
                "insert": (origin_x + 10, origin_y + sheet_h + 10),
            },
        )

        for p in placements:
            x_mm = int(p.get("x") or 0)
            y_mm = int(p.get("y") or 0)
            w_mm = int(p.get("fit_w") or 0)
            h_mm = int(p.get("fit_h") or 0)
            rotated = bool(p.get("rotated"))
            cut = p.get("cut") or {}
            web_part_id = cut.get("source_web_cut_part_id")
            part = parts_by_id.get(web_part_id) if web_part_id else None

            self._tp_dxf_render_placed_panel(
                msp=msp,
                origin_x=origin_x,
                origin_y=origin_y,
                placed_x=x_mm,
                placed_y=y_mm,
                placed_w=w_mm,
                placed_h=h_mm,
                rotated=rotated,
                part=part,
            )

        # Guillotine cut lines on a dedicated layer. These rip the leftover
        # material into clean rectangular offcuts (instead of weird shapes) and
        # give the saw operator the exact straight cuts. Placements are in the
        # sheet's bottom-left Y-up frame, which is also the frame
        # panel_saw_cut_lines returns its segments in.
        try:
            from odoo.addons.tp_sheet_nesting.models.services.tp_guillotine_cuts import (
                panel_saw_cut_lines,
            )
            if "CMP_OFFCUT_RIP" not in msp.doc.layers:
                msp.doc.layers.add(name="CMP_OFFCUT_RIP", color=3)  # green
            rects = [
                (
                    int(p.get("x") or 0),
                    int(p.get("y") or 0),
                    int(p.get("fit_w") or 0),
                    int(p.get("fit_h") or 0),
                )
                for p in placements
            ]
            cut_segments = self._tp_clip_cut_lines_around_panels(
                panel_saw_cut_lines(rects, sheet_w, sheet_h),
                rects,
            )
            for (x1, y1, x2, y2) in cut_segments:
                msp.add_line(
                    (origin_x + x1, origin_y + y1),
                    (origin_x + x2, origin_y + y2),
                    dxfattribs={"layer": "CMP_OFFCUT_RIP"},
                )
        except Exception:  # noqa: BLE001 — cut overlay is supplementary
            _logger.exception("Failed to add guillotine offcut cut lines to DXF")

    @api.model
    def _tp_dxf_render_placed_panel(
        self,
        *,
        msp,
        origin_x,
        origin_y,
        placed_x,
        placed_y,
        placed_w,
        placed_h,
        rotated,
        part,
    ):
        # Panel-local origin to sheet space transform (mm, both bottom-left, Y-up).
        # If rotated, original (panel-local) -> placed by (px, py) -> (panel_h - py, px).
        original_pw = placed_h if rotated else placed_w  # original (unrotated) panel width
        original_ph = placed_w if rotated else placed_h

        def panel_to_world(px, py):
            if rotated:
                lx = original_ph - py
                ly = px
            else:
                lx = px
                ly = py
            return origin_x + placed_x + lx, origin_y + placed_y + ly

        # Split cut-outs into pockets that sit fully inside the panel and
        # notches that break through an edge. Edge notches get merged into
        # the outer cut outline below (so CAM sees a single perimeter
        # contour that traces around the notch); interior pockets remain
        # separate closed polylines on the inner-cutouts layer.
        interior_pockets, edge_notches = self._tp_classify_cut_outs(
            part=part, pw=original_pw, ph=original_ph,
        )

        # --- Outer cut outline ---
        cut_outline = self._tp_outline_for_panel(
            part=part,
            original_pw=original_pw,
            original_ph=original_ph,
            edge_notches=edge_notches,
        )
        # cut_outline is a list of (x_local, y_local, bulge) tuples in panel-local space.
        world_pts = []
        for (lx, ly, bulge) in cut_outline:
            wx, wy = panel_to_world(lx, ly)
            world_pts.append((wx, wy, bulge))
        msp.add_lwpolyline(
            world_pts,
            format="xyb",
            close=True,
            dxfattribs={"layer": "CMP_CUT_OUTLINE"},
        )

        if not part:
            return

        # --- Holes (CIRCLE entities so CAM detects them as drills) ---
        for hole in (part.holes or []):
            if not isinstance(hole, dict):
                continue
            try:
                hx = float(hole.get("xMm") or hole.get("x") or 0)
                hy = float(hole.get("yMm") or hole.get("y") or 0)
                diam = float(
                    hole.get("diameterMm")
                    or hole.get("diameter")
                    or hole.get("d")
                    or 0
                )
            except (TypeError, ValueError):
                continue
            if diam <= 0:
                continue
            wx, wy = panel_to_world(hx, hy)
            msp.add_circle(
                center=(wx, wy),
                radius=diam / 2.0,
                dxfattribs={"layer": "CMP_HOLES"},
            )

        # --- Inner cutouts (interior pockets only) ---
        for (cx, cy, cw, ch) in interior_pockets:
            corners = [
                (cx,      cy),
                (cx + cw, cy),
                (cx + cw, cy + ch),
                (cx,      cy + ch),
            ]
            world_corners = [panel_to_world(x, y) for (x, y) in corners]
            msp.add_lwpolyline(
                world_corners,
                close=True,
                dxfattribs={"layer": "CMP_INNER_CUTOUTS"},
            )

    @api.model
    def _tp_classify_cut_outs(self, *, part, pw, ph):
        """Sort a panel's cut_outs (panel-local) into interior pockets and
        edge notches. Returns (interior_pockets, edge_notches).

        - interior_pockets: list of (x, y, w, h) entirely inside the panel.
        - edge_notches: list of (x0, y0, x1, y1) clipped to [0,pw]x[0,ph],
          representing the in-panel footprint of cut-outs that cross the
          panel boundary. The caller is expected to subtract these from
          the outer cut outline rather than draw them as closed pockets.

        Cut-outs entirely outside the panel are dropped silently — those
        are website data bugs that historically rendered as 'random' blue
        rectangles floating in the sheet.
        """
        interior = []
        notches = []
        if not part:
            return interior, notches
        pw_f, ph_f = float(pw), float(ph)
        for co in (part.cut_outs or []):
            if not isinstance(co, dict):
                continue
            try:
                cx = float(co.get("xMm") or co.get("x") or 0)
                cy = float(co.get("yMm") or co.get("y") or 0)
                cw = float(co.get("widthMm") or co.get("width") or co.get("w") or 0)
                ch = float(co.get("heightMm") or co.get("height") or co.get("h") or 0)
            except (TypeError, ValueError):
                continue
            if cw <= 0 or ch <= 0:
                continue
            x0, y0, x1, y1 = cx, cy, cx + cw, cy + ch
            cx0 = max(0.0, x0)
            cy0 = max(0.0, y0)
            cx1 = min(pw_f, x1)
            cy1 = min(ph_f, y1)
            if cx1 - cx0 <= 1e-9 or cy1 - cy0 <= 1e-9:
                # No overlap with panel — drop (data bug).
                continue
            crosses_edge = (
                x0 < -1e-9 or y0 < -1e-9
                or x1 > pw_f + 1e-9 or y1 > ph_f + 1e-9
            )
            if crosses_edge:
                notches.append((cx0, cy0, cx1, cy1))
            else:
                interior.append((cx, cy, cw, ch))
        return interior, notches

    @api.model
    def _tp_outline_with_notches(self, *, original_pw, original_ph, radii, edge_notches):
        """Build a rectilinear panel outline with edge notches subtracted.
        Returns [(x, y, bulge), ...] CCW in panel-local coords, or an empty
        list if every cell ends up inside a notch.

        Corner radii are re-applied at corners that survive the notching
        (i.e., still 90° convex turns at their original corner location).
        Notched corners lose their radius — by the time CAM sees an L-cut at
        a corner, a fillet there would be ambiguous geometry."""
        pw_f = float(original_pw)
        ph_f = float(original_ph)

        xs = sorted({0.0, pw_f} | {n[0] for n in edge_notches} | {n[2] for n in edge_notches})
        ys = sorted({0.0, ph_f} | {n[1] for n in edge_notches} | {n[3] for n in edge_notches})
        nx_ = len(xs) - 1
        ny_ = len(ys) - 1

        filled = [[False] * ny_ for _ in range(nx_)]
        for i in range(nx_):
            cx_mid = 0.5 * (xs[i] + xs[i + 1])
            for j in range(ny_):
                cy_mid = 0.5 * (ys[j] + ys[j + 1])
                in_notch = False
                for (x0, y0, x1, y1) in edge_notches:
                    if x0 < cx_mid < x1 and y0 < cy_mid < y1:
                        in_notch = True
                        break
                filled[i][j] = not in_notch

        def cell_filled(i, j):
            return 0 <= i < nx_ and 0 <= j < ny_ and filled[i][j]

        # Directed CCW boundary edges keyed by start vertex.
        edges = {}
        for i in range(nx_):
            for j in range(ny_):
                if not filled[i][j]:
                    continue
                x_lo, x_hi = xs[i], xs[i + 1]
                y_lo, y_hi = ys[j], ys[j + 1]
                if not cell_filled(i, j - 1):
                    edges[(x_lo, y_lo)] = (x_hi, y_lo)
                if not cell_filled(i + 1, j):
                    edges[(x_hi, y_lo)] = (x_hi, y_hi)
                if not cell_filled(i, j + 1):
                    edges[(x_hi, y_hi)] = (x_lo, y_hi)
                if not cell_filled(i - 1, j):
                    edges[(x_lo, y_hi)] = (x_lo, y_lo)

        if not edges:
            return []

        # Start at the lowest, then leftmost vertex — guaranteed on outer boundary.
        start = min(edges.keys(), key=lambda p: (p[1], p[0]))
        polygon = [start]
        current = edges[start]
        # Bound the walk so a malformed edge map can't infinite-loop.
        max_steps = len(edges) + 4
        steps = 0
        while current != start and steps < max_steps:
            polygon.append(current)
            current = edges.get(current)
            if current is None:
                return []  # unexpected gap — fall back to plain outline
            steps += 1

        # Drop collinear midpoints so the radius pass below sees only true corners.
        cleaned = []
        n = len(polygon)
        for i in range(n):
            prev = polygon[(i - 1) % n]
            curr = polygon[i]
            nxt = polygon[(i + 1) % n]
            if (prev[0] == curr[0] == nxt[0]) or (prev[1] == curr[1] == nxt[1]):
                continue
            cleaned.append(curr)
        if not cleaned:
            return []

        # Re-apply radii at any original panel corner that survived as a
        # convex 90° vertex.
        QUARTER_BULGE = math.tan(math.pi / 8.0)
        max_r = min(pw_f, ph_f) / 2.0
        corner_radii = {
            (0.0, 0.0):   min(float(radii.get("BL", 0.0)), max_r),
            (pw_f, 0.0):  min(float(radii.get("BR", 0.0)), max_r),
            (pw_f, ph_f): min(float(radii.get("TR", 0.0)), max_r),
            (0.0, ph_f):  min(float(radii.get("TL", 0.0)), max_r),
        }

        result = []
        n = len(cleaned)
        for i in range(n):
            prev = cleaned[(i - 1) % n]
            curr = cleaned[i]
            nxt = cleaned[(i + 1) % n]
            r = corner_radii.get((float(curr[0]), float(curr[1])), 0.0)
            ix, iy = curr[0] - prev[0], curr[1] - prev[1]
            ox, oy = nxt[0] - curr[0], nxt[1] - curr[1]
            cross = ix * oy - iy * ox
            inc_len = math.hypot(ix, iy)
            out_len = math.hypot(ox, oy)
            if r <= 0 or cross <= 0 or inc_len <= r or out_len <= r:
                # No radius, concave/colinear turn, or not enough edge length
                # to accommodate the arc — emit a sharp corner.
                result.append((float(curr[0]), float(curr[1]), 0))
                continue
            ix_n, iy_n = ix / inc_len, iy / inc_len
            ox_n, oy_n = ox / out_len, oy / out_len
            start_arc = (curr[0] - r * ix_n, curr[1] - r * iy_n)
            end_arc   = (curr[0] + r * ox_n, curr[1] + r * oy_n)
            result.append((start_arc[0], start_arc[1], QUARTER_BULGE))
            result.append((end_arc[0],   end_arc[1],   0))

        return result

    @api.model
    def _tp_outline_for_panel(self, *, part, original_pw, original_ph, edge_notches=None):
        """Return the panel outer outline as [(x, y, bulge), ...] in
        panel-local coords. Bulge encodes a circular arc for the
        following segment. For a 90° quarter-circle (the corner radius
        case) bulge = tan(angle/4) = tan(22.5°) ≈ 0.41421356.

        If edge_notches is provided, those rectangles are subtracted from
        the rectangular panel and the outline traces around them, so
        the perimeter cut goes through the notch.

        If the panel has no radii and no notches, returns a plain rectangle.
        """
        # Read corner radii defensively across key naming conventions.
        radii = {"TL": 0.0, "TR": 0.0, "BL": 0.0, "BR": 0.0}
        if part and isinstance(part.radii, dict):
            key_groups = {
                "TL": ("topLeftMm",     "topLeft",     "TL"),
                "TR": ("topRightMm",    "topRight",    "TR"),
                "BL": ("bottomLeftMm",  "bottomLeft",  "BL"),
                "BR": ("bottomRightMm", "bottomRight", "BR"),
            }
            for corner, keys in key_groups.items():
                for k in keys:
                    if k in part.radii and part.radii[k]:
                        try:
                            radii[corner] = float(part.radii[k])
                            break
                        except (TypeError, ValueError):
                            continue

        if edge_notches:
            notched = self._tp_outline_with_notches(
                original_pw=original_pw,
                original_ph=original_ph,
                radii=radii,
                edge_notches=edge_notches,
            )
            if notched:
                return notched
            # Fall through to plain outline on degenerate notch geometry.

        has_any_radius = any(r > 0 for r in radii.values())
        if not has_any_radius:
            # Plain rectangle, bulge=0 on all segments.
            return [
                (0,           0,            0),
                (original_pw, 0,            0),
                (original_pw, original_ph,  0),
                (0,           original_ph,  0),
            ]

        # Build a radiused rectangle. Going CCW from bottom-left corner:
        #   BL -> BR -> TR -> TL -> back to BL.
        # For each corner with radius r, we insert two vertices flanking
        # the corner, with a positive bulge on the FIRST of those two so
        # the arc curves outward into a quarter-circle cut.
        QUARTER_BULGE = math.tan(math.pi / 8.0)  # ≈ 0.41421356, 90° arc

        verts = []
        w = float(original_pw)
        h = float(original_ph)
        # Clamp radii to half the shorter side so we don't get geometry overlap.
        max_r = min(w, h) / 2.0
        r_bl = min(radii["BL"], max_r)
        r_br = min(radii["BR"], max_r)
        r_tr = min(radii["TR"], max_r)
        r_tl = min(radii["TL"], max_r)

        # Bottom edge — from after BL corner to before BR corner
        verts.append((r_bl, 0, 0))                # start of bottom edge
        if r_br > 0:
            verts.append((w - r_br, 0, QUARTER_BULGE))   # end of bottom edge, arc into right side
        else:
            verts.append((w, 0, 0))

        # Right edge
        if r_br > 0:
            verts.append((w, r_br, 0))
        if r_tr > 0:
            verts.append((w, h - r_tr, QUARTER_BULGE))
        else:
            verts.append((w, h, 0))

        # Top edge
        if r_tr > 0:
            verts.append((w - r_tr, h, 0))
        if r_tl > 0:
            verts.append((r_tl, h, QUARTER_BULGE))
        else:
            verts.append((0, h, 0))

        # Left edge
        if r_tl > 0:
            verts.append((0, h - r_tl, 0))
        if r_bl > 0:
            verts.append((0, r_bl, QUARTER_BULGE))   # arc back into bottom edge
        else:
            verts.append((0, 0, 0))

        return verts
