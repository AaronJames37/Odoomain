"""Stateless RPC wrapper around the nesting engine.

Exposes tp.sheet.format.quote_nesting(payload_json) so the website can ask
Odoo "how many full sheets does this basket of panels need?" without
persisting any demand. Mirrors how tp.nesting.sandbox.action_run invokes the
engine, but takes its inputs from a JSON payload instead of tp.web.cut.part
rows.
"""
import json
import logging

from odoo import api, models

_logger = logging.getLogger(__name__)

# Match what the sandbox uses; the engine has internal guardrails too.
_QUOTE_KERF_DEFAULT_MM = 3
_QUOTE_TRIM_EDGE_DEFAULT_MM = 0
_QUOTE_FAST_FALLBACK_SECONDS = 1.5


class TpSheetFormat(models.Model):
    _inherit = "tp.sheet.format"

    # ------------------------------------------------------------------
    # Public RPC entry point
    # ------------------------------------------------------------------
    @api.model
    def quote_nesting(self, payload_json):
        """Run the nesting engine in memory for a website price quote.

        Input: a JSON string holding a list of "thickness groups" with the
        shape documented in the module README. Persists nothing.

        Returns a JSON string holding a list of per-group results, in input
        order, each with at minimum:
          - thickness, fullSheetSku (echoed back)
          - sheetsNeeded (int, or null if the group could not be nested)
          - panelsPlaced (int)
          - previewSvg  (str or null)

        On a per-group failure the result still carries an `error` field;
        whole-payload failures (bad JSON, etc.) raise so the RPC layer
        surfaces them and the website falls back to per-m² pricing.
        """
        if isinstance(payload_json, (bytes, bytearray)):
            payload_json = payload_json.decode("utf-8")
        try:
            payload = json.loads(payload_json) if isinstance(payload_json, str) else payload_json
        except (TypeError, ValueError) as exc:
            raise ValueError("quote_nesting payload is not valid JSON: %s" % exc)
        if not isinstance(payload, list):
            raise ValueError("quote_nesting payload must be a JSON array of thickness groups.")

        results = []
        for group in payload:
            if not isinstance(group, dict):
                results.append({
                    "thickness": None,
                    "fullSheetSku": None,
                    "sheetsNeeded": None,
                    "panelsPlaced": 0,
                    "previewSvg": None,
                    "error": "Group entry is not a JSON object.",
                })
                continue
            try:
                results.append(self._tp_quote_one_group(group))
            except Exception as exc:  # noqa: BLE001 — degrade gracefully per group
                _logger.warning(
                    "quote_nesting: group %r failed: %s",
                    group.get("fullSheetSku") or group.get("thickness"),
                    exc,
                )
                results.append({
                    "thickness": group.get("thickness"),
                    "fullSheetSku": group.get("fullSheetSku"),
                    "sheetsNeeded": None,
                    "panelsPlaced": 0,
                    "previewSvg": None,
                    "error": str(exc),
                })

        return json.dumps(results)

    # ------------------------------------------------------------------
    # Per-group computation
    # ------------------------------------------------------------------
    @api.model
    def _tp_quote_one_group(self, group):
        thickness = group.get("thickness")
        sku = group.get("fullSheetSku")
        sheet = self._tp_quote_resolve_sheet(group)
        # Try the requested stock size first, then same-material/thickness siblings
        # if that first source cannot produce a complete fast quote.
        sheets = self._tp_quote_sibling_sheets(sheet)
        sheets = self.browse([sheet.id] + [s.id for s in sheets if s.id != sheet.id])
        cuts = self._tp_quote_build_cuts(group.get("panels") or [])
        if not cuts:
            return {
                "thickness": thickness,
                "fullSheetSku": sku,
                "sheetsNeeded": 0,
                "panelsPlaced": 0,
                "previewSvg": None,
            }

        Sandbox = self.env["tp.nesting.sandbox"]
        sources = [Sandbox._tp_sheet_format_source(s) for s in sheets]

        best_plan = self._tp_quote_run_best_of(cuts, sources)
        if not best_plan or not best_plan.get("ok"):
            metrics = (best_plan or {}).get("metrics") or {}
            reason = metrics.get("infeasible_reason") or "unknown"
            err_cut = (best_plan or {}).get("error_cut") or {}
            extra = ""
            if err_cut:
                extra = " (failed on %sx%s)" % (err_cut.get("width_mm"), err_cut.get("height_mm"))
            return {
                "thickness": thickness,
                "fullSheetSku": sku,
                "sheetsNeeded": None,
                "panelsPlaced": 0,
                "previewSvg": None,
                "error": "Engine could not pack: %s%s" % (reason, extra),
            }

        bins = best_plan.get("bins") or []
        placed = sum(len(b.get("placements") or []) for b in bins)
        try:
            preview_svg = Sandbox._tp_render_svg(bins=bins, cuts=cuts, parts=[])
        except Exception as exc:  # noqa: BLE001 — preview is optional
            _logger.warning("quote_nesting: SVG render failed for %s: %s", sku, exc)
            preview_svg = None

        # Per-stock-sheet breakdown so the website can price each size used.
        sheets_by_size = {}
        for b in bins:
            src = b.get("source") or {}
            rec = src.get("record")
            key = (rec.product_id.default_code if (rec and rec.product_id and rec.product_id.default_code)
                   else "%sx%s" % (int(src.get("width_mm") or 0), int(src.get("height_mm") or 0)))
            entry = sheets_by_size.setdefault(key, {
                "sku": rec.product_id.default_code if (rec and rec.product_id) else None,
                "widthMm": int(src.get("width_mm") or 0),
                "heightMm": int(src.get("height_mm") or 0),
                "count": 0,
            })
            entry["count"] += 1

        return {
            "thickness": thickness,
            "fullSheetSku": sku,
            "sheetsNeeded": int(len(bins)),
            "sheetsBySize": list(sheets_by_size.values()),
            "panelsPlaced": int(placed),
            "previewSvg": preview_svg,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @api.model
    def _tp_quote_resolve_sheet(self, group):
        """Resolve which tp.sheet.format record to nest against.
        Prefers the explicit fullSheetSku; falls back to width/height match.
        """
        sku = (group.get("fullSheetSku") or "").strip()
        if sku:
            sheet = self.sudo().search(
                [("active", "=", True), ("product_id.default_code", "=", sku)],
                limit=1,
            )
            if sheet:
                return sheet

        width = int(group.get("fullSheetWidthMm") or 0)
        height = int(group.get("fullSheetHeightMm") or 0)
        if width > 0 and height > 0:
            sheet = self.sudo().search(
                [("active", "=", True), ("width_mm", "=", width), ("height_mm", "=", height)],
                limit=1,
            )
            if sheet:
                return sheet

        raise ValueError(
            "Could not resolve a tp.sheet.format for sku=%r width=%s height=%s"
            % (sku, width, height)
        )

    @api.model
    def _tp_quote_sibling_sheets(self, sheet):
        """All active stock sheet formats of the same material + thickness as
        `sheet` (so the quote can nest across multiple sizes). Matches on the SKU
        prefix (e.g. 'ACR-CLR-000-3MM-' -> all sizes of that material/thickness),
        falling back to thickness/material fields, then to the sheet itself."""
        code = (sheet.product_id.default_code or "").strip() if sheet.product_id else ""
        siblings = self.browse()
        if code and "-" in code:
            prefix = code.rsplit("-", 1)[0]  # drop the size suffix
            siblings = self.sudo().search([
                ("active", "=", True),
                ("product_id.default_code", "=like", prefix + "-%"),
            ])
        if len(siblings) <= 1 and (sheet.tp_thickness_mm or sheet.tp_material_type):
            domain = [("active", "=", True)]
            if sheet.tp_thickness_mm:
                domain.append(("tp_thickness_mm", "=", sheet.tp_thickness_mm))
            if sheet.tp_material_type:
                domain.append(("tp_material_type", "=", sheet.tp_material_type))
            if sheet.tp_colour:
                domain.append(("tp_colour", "=", sheet.tp_colour))
            siblings = self.sudo().search(domain)
        # Always include the resolved sheet; de-dupe by (w,h) keeping smallest cost.
        sheets = (siblings | sheet)
        by_size = {}
        for s in sheets:
            key = (int(s.width_mm or 0), int(s.height_mm or 0))
            cur = by_size.get(key)
            if cur is None or float(s.landed_cost or 0.0) < float(cur.landed_cost or 0.0):
                by_size[key] = s
        return self.browse([s.id for s in by_size.values()])

    @api.model
    def _tp_quote_build_cuts(self, panels):
        """Expand the input panels into one engine cut per quantity-instance.
        Same shape as tp.nesting.sandbox._tp_build_cuts, minus the
        sale-order-line / web-cut-part linkage."""
        cuts = []
        for panel in panels:
            if not isinstance(panel, dict):
                continue
            try:
                w = int(panel.get("widthMm") or 0)
                h = int(panel.get("heightMm") or 0)
                qty = int(panel.get("quantity") or 0)
            except (TypeError, ValueError):
                continue
            if w <= 0 or h <= 0 or qty <= 0:
                continue
            for inst in range(qty):
                cuts.append({
                    "width_mm": w,
                    "height_mm": h,
                    "source_mo_id": 0,
                    "source_so_line_id": 0,
                    "source_web_cut_part_id": 0,
                    "_quote_key": panel.get("key"),
                    "_quote_instance_index": inst,
                })
        # Bigger pieces first, the same ordering the sandbox uses.
        cuts.sort(key=lambda c: c["width_mm"] * c["height_mm"], reverse=True)
        return cuts

    @api.model
    def _tp_quote_run_best_of(self, cuts, sources):
        """Nest quote panels through the same v2 planner in quote-fast mode."""
        Job = self.env.get("tp.nesting.job")
        if Job is None:
            return {
                "ok": False,
                "metrics": {"infeasible_reason": "tp.nesting.job unavailable"},
            }
        try:
            timeout_ms = int(self.env.company.tp_nesting_timeout_ms or 0)
            budget = _QUOTE_FAST_FALLBACK_SECONDS
            if timeout_ms > 0:
                budget = max(0.6, min(2.0, timeout_ms / 1000.0))
            return Job.sudo()._tp_run_v2_pattern_constructor(
                cuts,
                sources,
                kerf_mm=_QUOTE_KERF_DEFAULT_MM,
                trim_edge_mm=_QUOTE_TRIM_EDGE_DEFAULT_MM,
                time_budget_s=budget,
                seed_count=2,
                beam_width=4,
                first_feasible=True,
                return_first_viable=True,
            )
        except Exception as exc:  # noqa: BLE001 - quote RPC returns plan-shaped failure
            _logger.exception("quote_nesting: v2 pattern constructor failed")
            return {
                "ok": False,
                "metrics": {"infeasible_reason": str(exc)},
            }
