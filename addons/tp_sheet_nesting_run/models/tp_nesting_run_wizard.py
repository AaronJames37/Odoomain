import base64
import csv
import io
import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)


ACTIVE_PANEL_STATUSES = ("processing", "cutting")
MATERIAL_FIELDS = (
    "tp_material_type",
    "tp_thickness_mm",
    "tp_colour",
    "tp_finish",
    "tp_protective_film",
    "tp_brand_supplier",
)


class TpNestingRunWizard(models.TransientModel):
    _name = "tp.nesting.run.wizard"
    _description = "New Nesting Runs by Material"

    def _compute_display_name(self):
        # Friendly breadcrumb label instead of the raw transient record id.
        for rec in self:
            rec.display_name = _("New Nesting Runs")

    line_ids = fields.One2many(
        "tp.nesting.run.wizard.line",
        "wizard_id",
        string="Material Batches",
    )
    selected_group_count = fields.Integer(
        compute="_compute_batch_counts",
        string="Selected Batches",
    )
    group_count = fields.Integer(
        compute="_compute_batch_counts",
        string="Material Batches",
    )
    panel_count_preview = fields.Integer(
        string="Panels to Nest",
        compute="_compute_batch_counts",
    )
    cnc_panel_count_preview = fields.Integer(
        string="CNC Panels",
        compute="_compute_batch_counts",
    )
    batch_summary = fields.Text(compute="_compute_batch_summary", readonly=True)
    kerf_mm = fields.Integer(
        string="Kerf (mm)",
        default=3,
        required=True,
    )
    trim_edge_mm = fields.Integer(
        string="Trim Edge (mm, discontinued)",
        default=0,
        required=True,
        help="Deprecated. Trim edge is no longer used by the nesting solver.",
    )
    # Legacy single-source fields are kept for compatibility with old actions.
    source_product_id = fields.Many2one(
        "product.product",
        string="Source Sheet",
        domain="[('id', 'in', candidate_source_product_ids)]",
    )
    candidate_source_product_ids = fields.Many2many(
        "product.product",
        compute="_compute_candidate_source_product_ids",
    )
    nesting_input_mode = fields.Selection(
        [
            ("active_orders", "Queued orders"),
            ("csv_only", "CSV only"),
        ],
        string="Nesting Source",
        default="active_orders",
        required=True,
        help="Use queued order panels, or ignore queued orders and nest only imported CSV panels.",
    )

    state = fields.Selection(
        [("draft", "Draft"), ("previewed", "Previewed"), ("done", "Done"), ("failed", "Failed")],
        default="draft",
        readonly=True,
    )
    preview_summary = fields.Text(readonly=True)
    preview_svg = fields.Html(readonly=True, sanitize=False)
    csv_file = fields.Binary(string="CSV Panel File")
    csv_filename = fields.Char()
    manual_import_line_id = fields.Many2one(
        "tp.nesting.run.wizard.line",
        string="Add CSV Panels To",
        help="Material/thickness batch that should receive the imported CSV panels. "
             "If blank, exactly one selected batch is required.",
    )
    manual_import_csv_only = fields.Boolean(readonly=True)
    manual_line_ids = fields.One2many(
        "tp.nesting.run.wizard.manual.line",
        "wizard_id",
        string="Imported CSV Panels",
    )
    manual_import_count = fields.Integer(
        compute="_compute_manual_import_count",
        string="Imported CSV Quantity",
    )

    @api.model
    def default_get(self, fields_list):
        vals = super().default_get(fields_list)
        if "kerf_mm" in fields_list:
            vals["kerf_mm"] = self.env.company.tp_nesting_kerf_mm or 3
        if "trim_edge_mm" in fields_list:
            vals["trim_edge_mm"] = 0
        if "line_ids" in fields_list:
            vals["line_ids"] = self._tp_build_group_line_commands()
        return vals

    @api.model
    def action_open_new(self):
        """Open a persisted transient wizard so batch totals do not evaporate
        when the webclient refreshes the form after it has been idle."""
        wizard = self.create(
            {
                "nesting_input_mode": "active_orders",
                "kerf_mm": self.env.company.tp_nesting_kerf_mm or 3,
                "trim_edge_mm": 0,
                "line_ids": self._tp_build_group_line_commands(),
            }
        )
        return wizard._reopen_form()

    def action_new_batch_set(self):
        """Button-safe wrapper around action_open_new. Builds a fresh wizard
        with its batches populated in Python (reliable), unlike the web
        client's blank 'New' which can drop the default batch lines."""
        return self.env["tp.nesting.run.wizard"].action_open_new()

    @api.depends("nesting_input_mode")
    @api.depends_context("uid")
    def _compute_candidate_source_product_ids(self):
        for rec in self:
            if rec.nesting_input_mode == "csv_only":
                candidates = rec._tp_all_source_products_with_active_formats()
            else:
                candidates = rec._tp_candidate_source_products_for_parts(rec._tp_active_parts(), {})
            rec.candidate_source_product_ids = [(6, 0, candidates.ids)]

    @api.onchange("nesting_input_mode")
    def _onchange_nesting_input_mode(self):
        for rec in self:
            rec.manual_import_line_id = False
            rec.manual_import_csv_only = False
            rec.csv_file = False
            rec.csv_filename = False
            rec.state = "draft"
            rec.preview_summary = False
            rec.preview_svg = False
            if rec.nesting_input_mode == "csv_only":
                rec.candidate_source_product_ids = [(6, 0, rec._tp_all_source_products_with_active_formats().ids)]
                rec.line_ids = [(5, 0, 0)]
            else:
                rec.source_product_id = False
                rec.candidate_source_product_ids = [(6, 0, rec._tp_candidate_source_products_for_parts(rec._tp_active_parts(), {}).ids)]
                rec.line_ids = [(5, 0, 0)] + rec._tp_build_group_line_commands()

    @api.depends(
        "line_ids",
        "line_ids.selected",
        "line_ids.panel_count",
        "line_ids.panel_quantity",
        "line_ids.cnc_panel_count",
    )
    def _compute_batch_counts(self):
        for rec in self:
            selected = rec.line_ids.filtered("selected")
            rec.group_count = len(rec.line_ids)
            rec.selected_group_count = len(selected)
            rec.panel_count_preview = sum(selected.mapped("panel_quantity"))
            rec.cnc_panel_count_preview = sum(selected.mapped("cnc_panel_count"))

    @api.depends("manual_line_ids", "manual_line_ids.quantity")
    def _compute_manual_import_count(self):
        for rec in self:
            rec.manual_import_count = sum(max(1, int(line.quantity or 1)) for line in rec.manual_line_ids)

    @api.depends(
        "line_ids",
        "line_ids.selected",
        "line_ids.group_label",
        "line_ids.panel_quantity",
        "line_ids.source_product_id",
        "line_ids.warning",
    )
    def _compute_batch_summary(self):
        for rec in self:
            if not rec.line_ids:
                if rec.nesting_input_mode == "csv_only":
                    rec.batch_summary = (
                        "CSV-only mode: choose a source sheet, upload a CSV, "
                        "then import it. No queued order panels will be included."
                    )
                    continue
                rec.batch_summary = "No active processing/cutting panels are queued for nesting."
                continue
            lines = []
            for line in rec.line_ids.sorted(lambda item: (not item.selected, item.group_label or "", item.id)):
                status = "selected" if line.selected else "skipped"
                source = line.source_product_id.display_name if line.source_product_id else "no source sheet"
                warning = f" - {line.warning}" if line.warning else ""
                lines.append(
                    f"{line.group_label}: {line.panel_quantity} panel(s), {source}, {status}{warning}"
                )
            rec.batch_summary = "\n".join(lines)

    def action_refresh(self):
        self.ensure_one()
        self._tp_clear_manual_import_records()
        line_commands = [(5, 0, 0)]
        if self.nesting_input_mode != "csv_only":
            line_commands += self._tp_build_group_line_commands()
        self.write(
            {
                "line_ids": line_commands,
                "manual_import_line_id": False,
                "manual_import_csv_only": False,
                "csv_file": False,
                "csv_filename": False,
                "state": "draft",
                "preview_summary": False,
                "preview_svg": False,
            }
        )
        return self._reopen_form()

    # ------------------------------------------------------------------
    # Manual panel CSV import
    # ------------------------------------------------------------------
    def action_import_manual_csv(self):
        self.ensure_one()
        if not self.csv_file:
            raise UserError(_("Choose a CSV file first."))

        rows, skipped_disabled = self._tp_parse_manual_csv(self.csv_file)
        csv_only = self.nesting_input_mode == "csv_only"
        if csv_only:
            if not self.source_product_id:
                raise UserError(_("Choose a source sheet before importing a CSV-only nest."))
            self._tp_clear_manual_import_records()
            self.line_ids.unlink()
            target = self._tp_create_csv_only_batch_line()
        else:
            target = self.manual_import_line_id
            if target and target.wizard_id != self:
                target = False
            if not target:
                selected = self.line_ids.filtered("selected")
                if len(selected) == 1:
                    target = selected
            if not target:
                raise UserError(_(
                    "Choose the material batch to import into, or select exactly one batch first."
                ))

        Part = self.env["tp.web.cut.part"].sudo()
        created_parts = Part.browse()
        manual_commands = []
        stamp = fields.Datetime.now().strftime("%Y%m%d%H%M%S")

        for idx, row in enumerate(rows, start=1):
            part_key = "MANUAL-%s-%s-%s-%03d" % (self.id, target.id, stamp, idx)
            label = row.get("label") or part_key
            vals = self._tp_manual_part_vals(target, row, part_key, label)
            part = Part.create(vals)
            created_parts |= part
            manual_commands.append((0, 0, {
                "batch_line_id": target.id,
                "part_id": part.id,
                "label": label,
                "width_mm": row["width_mm"],
                "height_mm": row["height_mm"],
                "quantity": row["quantity"],
            }))

        if csv_only:
            target.write({
                "selected": True,
                "part_ids": [(6, 0, created_parts.ids)],
            })
        else:
            target.write({"part_ids": [(4, part_id) for part_id in created_parts.ids]})
        self.write({
            "manual_import_line_id": target.id,
            "manual_line_ids": manual_commands,
            "manual_import_csv_only": csv_only,
            "csv_file": False,
            "csv_filename": False,
            "state": "draft",
            "preview_summary": False,
            "preview_svg": False,
        })
        self._tp_recompute_line_counts(target)
        if skipped_disabled:
            _logger.info("Run wizard manual CSV import: skipped %d disabled row(s).", skipped_disabled)
        return self._reopen_form()

    def action_clear_manual_imports(self):
        self.ensure_one()
        csv_only = self.manual_import_csv_only
        self._tp_clear_manual_import_records()
        vals = {
            "manual_import_line_id": False,
            "manual_import_csv_only": False,
            "csv_file": False,
            "csv_filename": False,
            "state": "draft",
            "preview_summary": False,
            "preview_svg": False,
        }
        if csv_only:
            vals["line_ids"] = [(5, 0, 0)]
        self.write(vals)
        return self._reopen_form()

    def _tp_parse_manual_csv(self, csv_file):
        try:
            raw = base64.b64decode(csv_file)
            text = raw.decode("utf-8-sig", errors="replace")
        except Exception as exc:
            raise UserError(_("Could not read the CSV file: %s") % exc)

        def _to_int(value):
            try:
                return int(round(float(value)))
            except (TypeError, ValueError):
                return None

        def _is_disabled(value):
            return str(value or "").strip().lower() in ("false", "0", "no", "n", "off", "disabled")

        rows = [[(cell or "").strip() for cell in row] for row in csv.reader(io.StringIO(text))]
        rows = [row for row in rows if any(row)]
        if not rows:
            raise UserError(_("The CSV file is empty."))

        alias = {
            "width": "width", "w": "width", "width_mm": "width",
            "height": "height", "h": "height", "height_mm": "height",
            "length": "height", "len": "height",
            "qty": "qty", "quantity": "qty", "count": "qty",
            "label": "label", "name": "label", "reference": "label", "ref": "label",
            "enabled": "enabled", "active": "enabled", "include": "enabled",
        }
        header = rows[0]
        col = {}
        for idx, cell in enumerate(header):
            key = alias.get(cell.strip().lower())
            if key and key not in col:
                col[key] = idx
        has_header = "width" in col and "height" in col
        data_rows = rows[1:] if has_header else rows

        parsed = []
        skipped_disabled = 0
        for cells in data_rows:
            def get(field, pos):
                if has_header and field in col and col[field] < len(cells):
                    return cells[col[field]]
                return cells[pos] if pos < len(cells) else None

            if has_header and "enabled" in col and _is_disabled(get("enabled", -1)):
                skipped_disabled += 1
                continue

            width = _to_int(get("width", 0))
            height = _to_int(get("height", 1))
            if width is None or height is None or width <= 0 or height <= 0:
                continue
            qty = _to_int(get("qty", 2)) or 1
            parsed.append({
                "label": get("label", 3) or False,
                "width_mm": width,
                "height_mm": height,
                "quantity": max(1, qty),
            })

        if not parsed:
            raise UserError(_(
                "No usable panel rows found. Expected columns like LENGTH/WIDTH "
                "(or width/height), QTY, ENABLED (optional), label (optional)."
            ))
        return parsed, skipped_disabled

    def _tp_manual_part_vals(self, target, row, part_key, label):
        self.ensure_one()
        vals = {
            "company_id": self.env.company.id,
            "part_key": part_key,
            "label": label,
            "width_mm": row["width_mm"],
            "height_mm": row["height_mm"],
            "quantity": row["quantity"],
            "shape": "rectangle",
            "active": True,
            "material": target.material_type or False,
            "colour": target.colour or False,
            "finish": target.finish or False,
            "protective_film": target.protective_film or False,
            "brand_supplier": target.brand_supplier or False,
            "thickness_mm": target.thickness_mm or 0.0,
            "tp_material_type": target.material_type or False,
            "tp_thickness_mm": target.thickness_mm or 0.0,
            "tp_colour": target.colour or False,
            "tp_finish": target.finish or False,
            "tp_brand_supplier": target.brand_supplier or False,
            "source_payload": {
                "origin": "nesting_run_wizard_csv",
                "wizard_id": self.id,
                "batch_line_id": target.id,
                "csv_filename": self.csv_filename or "",
            },
        }
        if "tp_protective_film" in self.env["tp.web.cut.part"]._fields:
            film = (target.protective_film or "none").strip().lower()
            vals["tp_protective_film"] = film if film in ("paper", "plastic", "none") else "none"
        return vals

    def _tp_clear_manual_import_records(self):
        self.ensure_one()
        manual_lines = self.manual_line_ids.sudo()
        if not manual_lines:
            return
        manual_lines.unlink()

    def _tp_recompute_line_counts(self, lines):
        for line in lines:
            parts = line.part_ids
            line.write({
                "panel_count": len(parts),
                "panel_quantity": sum(max(1, int(part.quantity or 1)) for part in parts),
                "cnc_panel_count": len(parts.filtered("cnc_required")) if "cnc_required" in parts._fields else 0,
            })

    def action_run(self):
        self.ensure_one()
        selected_lines = self.line_ids.filtered("selected")
        if not selected_lines:
            raise UserError(_("Select at least one material batch to nest."))

        missing_sources = selected_lines.filtered(lambda line: not line.source_product_id)
        if missing_sources:
            labels = "\n".join(f"  - {line.group_label}" for line in missing_sources)
            raise UserError(
                _(
                    "Some material batches have no source sheet product. "
                    "Choose a source sheet before running:\n%s"
                )
                % labels
            )

        run_ids = []
        for line in selected_lines.sorted(lambda item: (item.material_type or "", item.thickness_mm, item.colour or "", item.id)):
            parts = line.part_ids.filtered("active")
            if not parts:
                continue
            run = self._tp_run_global_nest(
                parts,
                source_product=line.source_product_id,
                group_label=line.group_label,
                identity=line._tp_identity(),
                return_action=False,
            )
            run_ids.append(run.id)

        if not run_ids:
            raise UserError(_("No active panels were available in the selected batches."))

        self.write({"state": "done"})
        return {
            "type": "ir.actions.act_window",
            "name": _("Created Nesting Runs"),
            "res_model": "tp.nesting.run",
            "view_mode": "list,form",
            "domain": [("id", "in", run_ids)],
            "target": "current",
            "context": {"create": False},
        }

    def action_preview_all(self):
        """Render an SVG layout for every selected batch, stacked vertically
        with a heading + stats block per batch. No persistence — purely
        ephemeral so the operator can sanity-check before clicking Run."""
        self.ensure_one()
        selected_lines = self.line_ids.filtered("selected")
        if not selected_lines:
            raise UserError(_("Select at least one batch to preview."))

        missing = selected_lines.filtered(lambda line: not line.source_product_id)
        if missing:
            raise UserError(_(
                "Some selected batches have no source sheet. Pick one before "
                "previewing:\n%s"
            ) % "\n".join("  - " + line.group_label for line in missing))

        Sandbox = self.env["tp.nesting.sandbox"].sudo()

        svg_chunks = []
        summary_lines = []
        total_panels = total_sheets = total_saw_cuts = 0
        total_src_area = total_used_area = 0.0
        kerf_mm = self._tp_kerf_mm()
        trim_edge_mm = self._tp_trim_edge_mm()

        for line in selected_lines.sorted(lambda l: (l.material_type or "", l.thickness_mm, l.colour or "", l.id)):
            parts = line.part_ids.filtered("active")
            if not parts:
                continue
            identity = line._tp_identity()
            source_product = line.source_product_id

            Job = self.env["tp.nesting.job"].sudo()
            cuts = Job._tp_run_build_cuts(parts)
            try:
                sources, offcut_sources = self._tp_build_engine_sources(source_product, identity)
            except UserError as exc:
                svg_chunks.append(self._tp_preview_error_block(line.group_label, str(exc)))
                summary_lines.append(
                    "%s: SKIPPED — %s" % (line.group_label, exc.args[0] if exc.args else exc)
                )
                continue

            plan = Job._tp_run_v2_pattern_constructor(
                cuts,
                sources,
                kerf_mm=kerf_mm,
                trim_edge_mm=trim_edge_mm,
                offcut_sources=offcut_sources,
                time_budget_s=self.env.company.tp_nesting_guillotine_seconds or 10,
            )
            if not plan.get("ok"):
                reason = (plan.get("metrics") or {}).get("infeasible_reason") or "unknown"
                svg_chunks.append(self._tp_preview_error_block(
                    line.group_label, "Engine could not pack: %s" % reason,
                ))
                summary_lines.append("%s: ENGINE FAILED — %s" % (line.group_label, reason))
                continue

            bins = plan.get("bins") or []
            sheet_count = len(bins)
            panel_count = sum(len(b.get("placements") or []) for b in bins)
            cnc_ids = parts.filtered("cnc_required").ids if "cnc_required" in parts._fields else []
            cnc_count = sum(
                1
                for b in bins
                for p in (b.get("placements") or [])
                if p.get("cut", {}).get("source_web_cut_part_id") in cnc_ids
            )
            src_area = used_area = 0.0
            for b in bins:
                src = b.get("source") or {}
                src_area += float(src.get("width_mm") or 0) * float(src.get("height_mm") or 0)
                for p in (b.get("placements") or []):
                    used_area += float(p.get("fit_w") or 0) * float(p.get("fit_h") or 0)
            waste = max(0.0, src_area - used_area)
            util_pct = (used_area / src_area * 100.0) if src_area else 0.0

            total_sheets += sheet_count
            total_panels += panel_count
            total_src_area += src_area
            total_used_area += used_area

            cuts_total = Job._tp_count_guillotine_cuts(plan)
            total_saw_cuts += cuts_total

            summary_lines.append(
                "%s: %d sheets, %d panels (%d CNC), %.2f%% util, %.4f m² waste, %d saw cuts"
                % (
                    line.group_label, sheet_count, panel_count, cnc_count,
                    util_pct, waste / 1_000_000.0, cuts_total,
                )
            )

            # Header block per batch
            header_html = (
                '<div style="margin:18px 0 6px 0;padding:8px 12px;'
                'background:#222;color:#fff;font-family:sans-serif;'
                'border-radius:4px;">'
                '<strong>%s</strong> &nbsp; '
                '<span style="opacity:0.85">'
                '%d sheets &middot; %d panels (%d CNC) &middot; %.2f%% util &middot; '
                'waste %.4f m² &middot; %d saw cuts'
                '</span></div>'
            ) % (
                self._tp_escape_html(line.group_label),
                sheet_count, panel_count, cnc_count, util_pct, waste / 1_000_000.0, cuts_total,
            )

            try:
                batch_svg = Sandbox._tp_render_svg(bins=bins, cuts=cuts, parts=parts)
            except Exception:
                _logger.exception("Preview SVG failed for batch %s", line.group_label)
                batch_svg = "<div><em>Could not render SVG.</em></div>"

            svg_chunks.append(header_html + batch_svg)

        if not svg_chunks:
            raise UserError(_("Nothing to preview — selected batches had no active panels."))

        total_waste = max(0.0, total_src_area - total_used_area)
        total_util = (total_used_area / total_src_area * 100.0) if total_src_area else 0.0
        grand_total = (
            "TOTAL ACROSS ALL BATCHES:\n"
            "  Kerf:          %d mm\n"
            "  Sheets used:   %d\n"
            "  Panels placed: %d\n"
            "  Saw cuts:      %d\n"
            "  Sheet area:    %.4f m²\n"
            "  Used area:     %.4f m²\n"
            "  Waste:         %.4f m² (%.2f%%)\n"
            "  Utilisation:   %.2f%%\n"
            "\nPreview only. Click Run Nesting to commit."
        ) % (
            kerf_mm,
            total_sheets, total_panels, total_saw_cuts,
            total_src_area / 1_000_000.0, total_used_area / 1_000_000.0,
            total_waste / 1_000_000.0, 100.0 - total_util, total_util,
        )

        full_summary = grand_total + "\n\nPER BATCH:\n  " + "\n  ".join(summary_lines)
        combined_svg = "\n".join(svg_chunks)

        self.write({
            "state": "previewed",
            "preview_summary": full_summary,
            "preview_svg": combined_svg,
        })
        return self._reopen_form()

    @staticmethod
    def _tp_escape_html(value):
        if value in (None, False):
            return ""
        return (
            str(value)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    @staticmethod
    def _tp_preview_error_block(label, message):
        # Module-level escape inlined to avoid the staticmethod indirection at call time.
        def esc(value):
            return (
                str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            )
        return (
            '<div style="margin:18px 0 6px 0;padding:8px 12px;'
            'background:#aa3333;color:#fff;font-family:sans-serif;'
            'border-radius:4px;">'
            '<strong>%s</strong> — %s</div>'
        ) % (esc(label), esc(message))

    def action_preview(self):
        """Legacy single-batch preview kept for old buttons/actions."""
        self.ensure_one()
        source_product = self.source_product_id
        identity = {}
        parts = self._tp_collect_active_parts()
        if self.line_ids:
            line = self.line_ids.filtered("selected")[:1] or self.line_ids[:1]
            source_product = line.source_product_id
            identity = line._tp_identity()
            parts = line.part_ids

        if not source_product:
            raise UserError(_("Choose a source sheet before previewing."))
        if not parts:
            raise UserError(_("No active panels found for source sheet %s.") % source_product.display_name)

        Job = self.env["tp.nesting.job"].sudo()
        cuts = Job._tp_run_build_cuts(parts)
        sources, offcut_sources = self._tp_build_engine_sources(source_product, identity)
        plan = Job._tp_run_v2_pattern_constructor(
            cuts,
            sources,
            kerf_mm=self._tp_kerf_mm(),
            trim_edge_mm=self._tp_trim_edge_mm(),
            offcut_sources=offcut_sources,
            time_budget_s=self.env.company.tp_nesting_guillotine_seconds or 10,
        )
        if not plan.get("ok"):
            metrics = plan.get("metrics") or {}
            reason = metrics.get("infeasible_reason") or "unknown"
            self.write(
                {
                    "state": "failed",
                    "preview_summary": "Preview failed: %s" % reason,
                    "preview_svg": False,
                }
            )
            return self._reopen_form()

        bins = plan.get("bins") or []
        sheet_count = len(bins)
        panel_count = sum(len(b.get("placements") or []) for b in bins)
        cnc_ids = parts.filtered("cnc_required").ids if "cnc_required" in parts._fields else []
        cnc_count = sum(
            1
            for bin_data in bins
            for placement in (bin_data.get("placements") or [])
            if placement.get("cut", {}).get("source_web_cut_part_id") in cnc_ids
        )

        src_total = used_total = 0.0
        for bin_data in bins:
            source = bin_data.get("source") or {}
            src_total += float(source.get("width_mm") or 0) * float(source.get("height_mm") or 0)
            for placement in bin_data.get("placements") or []:
                used_total += float(placement.get("fit_w") or 0) * float(placement.get("fit_h") or 0)
        waste = max(0.0, src_total - used_total)
        util_pct = (used_total / src_total * 100.0) if src_total else 0.0
        cuts_total = Job._tp_count_guillotine_cuts(plan)

        summary = (
            "Source sheet:    %s\n"
            "Kerf:            %d mm\n"
            "Sheets used:     %d\n"
            "Panels placed:   %d (%d CNC)\n"
            "Saw cuts:        %d\n"
            "Sheet area:      %.4f sqm\n"
            "Used area:       %.4f sqm\n"
            "Waste:           %.4f sqm (%.2f%%)\n"
            "Utilisation:     %.2f%%\n"
            "\nThis is a preview. Click Run Nesting to create nesting runs."
        ) % (
            source_product.display_name,
            self._tp_kerf_mm(),
            sheet_count,
            panel_count,
            cnc_count,
            cuts_total,
            src_total / 1_000_000.0,
            used_total / 1_000_000.0,
            waste / 1_000_000.0,
            100.0 - util_pct,
            util_pct,
        )

        Sandbox = self.env["tp.nesting.sandbox"].sudo()
        try:
            svg = Sandbox._tp_render_svg(bins=bins, cuts=cuts, parts=parts)
        except Exception:
            _logger.exception("Preview SVG build failed")
            svg = ""

        self.write({"state": "previewed", "preview_summary": summary, "preview_svg": svg})
        return self._reopen_form()

    def _reopen_form(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "current",
            "context": dict(self.env.context),
        }

    def _tp_run_global_nest(
        self,
        parts,
        *,
        source_product=False,
        group_label=False,
        identity=False,
        return_action=True,
    ):
        """Persist a run for one material batch. MO creation remains separate."""
        self.ensure_one()
        source_product = source_product or self.source_product_id
        if not source_product:
            raise UserError(_("Choose a source sheet before running nesting."))

        Job = self.env["tp.nesting.job"].sudo()
        Run = self.env["tp.nesting.run"].sudo()
        cuts = Job._tp_run_build_cuts(parts)
        sources, offcut_sources = self._tp_build_engine_sources(source_product, identity or {})

        kerf_mm = self._tp_kerf_mm()
        trim_edge_mm = self._tp_trim_edge_mm()
        # Use the same source pool as the preview so the persisted run matches
        # what the operator previewed. The v2 pattern constructor is the only
        # production solver.
        plan = Job._tp_run_v2_pattern_constructor(
            cuts,
            sources,
            kerf_mm=kerf_mm,
            trim_edge_mm=trim_edge_mm,
            offcut_sources=offcut_sources,
            time_budget_s=self.env.company.tp_nesting_guillotine_seconds or 10,
        )
        if not plan.get("ok"):
            metrics = plan.get("metrics") or {}
            reason = metrics.get("infeasible_reason") or "unknown"
            err_cut = plan.get("error_cut") or {}
            msg = _("Nesting engine could not pack %(group)s: %(reason)s") % {
                "group": group_label or source_product.display_name,
                "reason": reason,
            }
            if err_cut:
                msg += " (failed on %sx%s)" % (err_cut.get("width_mm"), err_cut.get("height_mm"))
            raise UserError(msg)

        bins = plan.get("bins") or []
        if not bins:
            raise UserError(_("Engine returned no bins for %s.") % (group_label or source_product.display_name))

        Job._tp_run_assert_offcuts_available(bins, run=False)

        sheets_needed = sum(1 for bin_data in bins if not (bin_data.get("source") or {}).get("is_offcut"))
        panel_count = sum(len(bin_data.get("placements") or []) for bin_data in bins)
        run_name = "NEST-%s-%s" % (
            self._tp_slug(group_label or source_product.default_code or source_product.display_name),
            fields.Datetime.now().strftime("%Y%m%d%H%M%S"),
        )
        run = Run.create(
            {
                "name": run_name,
                "mo_id": False,
                "kerf_mm": kerf_mm,
                "trim_edge_mm": trim_edge_mm,
                "rotation_mode": "free",
                "engine_mode": "deterministic",
                "state": "done",
                "note": group_label or False,
                "x_tp_source_product_id": source_product.id,
                "x_tp_sheets_needed": sheets_needed,
                "x_tp_panel_count": panel_count,
            }
        )

        Job._tp_run_create_allocations(run, bins, parts)
        Job._tp_run_finalize_metrics(run, plan)

        Sandbox = self.env["tp.nesting.sandbox"].sudo()
        try:
            stub_job_id = self._tp_find_stub_job(parts)
            if stub_job_id:
                stub = Sandbox.create({"job_id": stub_job_id, "sheet_format_id": sources[0]["id"]})
                dxf_bytes, _dxf_name = stub._tp_build_dxf(bins=bins, parts=parts)
                stub.unlink()
            else:
                dxf_bytes = b""
        except Exception:
            _logger.exception("DXF build failed for run %s", run.name)
            dxf_bytes = b""

        if dxf_bytes:
            dxf_name = "%s.zip" % run.name
            run.write(
                {
                    "x_tp_dxf_bytes": base64.b64encode(dxf_bytes),
                    "x_tp_dxf_filename": dxf_name,
                }
            )

        _logger.info(
            "Run %s: placed %d panels onto %d fresh sheets for %s",
            run.name,
            panel_count,
            sheets_needed,
            group_label or source_product.display_name,
        )

        if not return_action:
            return run
        return {
            "type": "ir.actions.act_window",
            "res_model": "tp.nesting.run",
            "res_id": run.id,
            "view_mode": "form",
            "target": "current",
        }

    def _tp_kerf_mm(self):
        self.ensure_one()
        return max(0, int(self.kerf_mm or 0))

    def _tp_trim_edge_mm(self):
        self.ensure_one()
        return 0

    @api.model
    def _tp_active_parts(self):
        Part = self.env["tp.web.cut.part"].sudo()
        domain = [("active", "=", True)]
        if "sale_order_fulfillment_status" in Part._fields:
            domain.append(("sale_order_fulfillment_status", "in", list(ACTIVE_PANEL_STATUSES)))
        return Part.search(domain, order="tp_material_type, tp_thickness_mm, tp_colour, id")

    def _tp_collect_active_parts(self):
        self.ensure_one()
        if not self.source_product_id:
            return self.env["tp.web.cut.part"]
        SourceMap = self.env["tp.nesting.source.map"].sudo()
        Part = self.env["tp.web.cut.part"].sudo()
        maps = SourceMap.search(
            [
                ("source_product_id", "=", self.source_product_id.id),
                ("active", "=", True),
            ]
        )
        demand_ids = maps.mapped("demand_product_id").ids
        if not demand_ids:
            return Part.browse()
        domain = [("product_id", "in", demand_ids), ("active", "=", True)]
        if "sale_order_fulfillment_status" in Part._fields:
            domain.append(("sale_order_fulfillment_status", "in", list(ACTIVE_PANEL_STATUSES)))
        return Part.search(domain)

    @api.model
    def _tp_build_group_line_commands(self):
        Part = self.env["tp.web.cut.part"].sudo()
        groups = {}
        for part in self._tp_active_parts():
            identity = self._tp_part_identity(part)
            key = self._tp_identity_key(identity)
            if key not in groups:
                groups[key] = {"identity": identity, "parts": Part.browse()}
            groups[key]["parts"] |= part

        commands = []
        for key, bucket in sorted(groups.items(), key=lambda item: item[0]):
            identity = bucket["identity"]
            parts = bucket["parts"].sorted(lambda part: (part.sale_order_id.id, part.line_index, part.panel_index, part.id))
            source_products = self._tp_candidate_source_products_for_parts(parts, identity)
            warning = False
            if not source_products:
                warning = "No mapped or matching source sheet product with active sheet formats."
            panel_quantity = sum(max(1, int(part.quantity or 1)) for part in parts)
            cnc_count = len(parts.filtered("cnc_required")) if "cnc_required" in parts._fields else 0
            commands.append(
                (
                    0,
                    0,
                    {
                        "selected": bool(source_products),
                        "group_key": repr(key),
                        "group_label": self._tp_group_label(identity),
                        "material_type": identity.get("tp_material_type") or "",
                        "thickness_mm": identity.get("tp_thickness_mm") or 0.0,
                        "colour": identity.get("tp_colour") or "",
                        "finish": identity.get("tp_finish") or "",
                        "protective_film": identity.get("tp_protective_film") or "",
                        "brand_supplier": identity.get("tp_brand_supplier") or "",
                        "panel_count": len(parts),
                        "panel_quantity": panel_quantity,
                        "cnc_panel_count": cnc_count,
                        "source_product_id": source_products[:1].id if source_products else False,
                        "candidate_source_product_ids": [(6, 0, source_products.ids)],
                        "part_ids": [(6, 0, parts.ids)],
                        "warning": warning,
                    },
                )
            )
        return commands

    def _tp_create_csv_only_batch_line(self):
        self.ensure_one()
        source_product = self.source_product_id
        identity = self._tp_source_product_identity(source_product)
        source_products = self._tp_all_source_products_with_active_formats()
        if source_product not in source_products:
            source_products |= source_product
        label = _("CSV Only - %s") % self._tp_csv_source_label(source_product, identity)
        return self.env["tp.nesting.run.wizard.line"].create({
            "wizard_id": self.id,
            "selected": True,
            "group_key": "csv_only:%s" % self.id,
            "group_label": label,
            "material_type": identity.get("tp_material_type") or "",
            "thickness_mm": identity.get("tp_thickness_mm") or 0.0,
            "colour": identity.get("tp_colour") or "",
            "finish": identity.get("tp_finish") or "",
            "protective_film": identity.get("tp_protective_film") or "",
            "brand_supplier": identity.get("tp_brand_supplier") or "",
            "panel_count": 0,
            "panel_quantity": 0,
            "cnc_panel_count": 0,
            "source_product_id": source_product.id,
            "candidate_source_product_ids": [(6, 0, source_products.ids)],
            "part_ids": [(6, 0, [])],
            "warning": False,
        })

    def _tp_all_source_products_with_active_formats(self):
        SheetFormat = self.env["tp.sheet.format"].sudo()
        products = SheetFormat.search([("active", "=", True)]).mapped("product_id")
        return products.filtered(lambda product: product).sorted(lambda product: (product.display_name or "", product.id))

    def _tp_source_product_identity(self, source_product):
        identity = {}
        for field_name in MATERIAL_FIELDS:
            value = self._tp_read_material_value(source_product, field_name)
            if field_name == "tp_thickness_mm":
                identity[field_name] = self._tp_float_value(value)
            else:
                cleaned = self._tp_clean_string(value)
                if field_name == "tp_protective_film" and cleaned.lower() == "none":
                    cleaned = ""
                identity[field_name] = cleaned
        return identity

    @api.model
    def _tp_part_identity(self, part):
        thickness = self._tp_float_value(self._tp_read_first(part, ("tp_thickness_mm", "thickness_mm")))
        film = self._tp_clean_string(self._tp_read_first(part, ("tp_protective_film", "protective_film")))
        if film == "none":
            film = ""
        return {
            "tp_material_type": self._tp_clean_string(self._tp_read_first(part, ("tp_material_type", "material"))),
            "tp_thickness_mm": thickness,
            "tp_colour": self._tp_clean_string(self._tp_read_first(part, ("tp_colour", "colour", "color"))),
            "tp_finish": self._tp_clean_string(self._tp_read_first(part, ("tp_finish", "finish"))),
            "tp_protective_film": film,
            "tp_brand_supplier": self._tp_clean_string(self._tp_read_first(part, ("tp_brand_supplier", "brand_supplier"))),
        }

    @api.model
    def _tp_candidate_source_products_for_parts(self, parts, identity):
        SourceMap = self.env["tp.nesting.source.map"].sudo()
        SheetFormat = self.env["tp.sheet.format"].sudo()
        Product = self.env["product.product"].sudo()

        demand_ids = parts.mapped("product_id").ids if parts else []
        products = Product.browse()
        if demand_ids:
            maps = SourceMap.search(
                [
                    ("demand_product_id", "in", demand_ids),
                    ("source_product_id", "!=", False),
                    ("active", "=", True),
                ]
            )
            products |= maps.mapped("source_product_id")

        matching_formats = SheetFormat.search([("active", "=", True)]).filtered(
            lambda sheet: self._tp_record_identity_compatible(sheet, identity, strict=True)
        )
        products |= matching_formats.mapped("product_id")
        products = products.filtered(lambda product: self._tp_product_has_active_formats(product))
        compatible = products.filtered(lambda product: self._tp_product_source_compatible(product, identity))
        return (compatible or products).sorted(lambda product: (product.display_name or "", product.id))

    def _tp_product_has_active_formats(self, product):
        return bool(self.env["tp.sheet.format"].sudo().search_count([("product_id", "=", product.id), ("active", "=", True)]))

    def _tp_product_source_compatible(self, product, identity):
        formats = self.env["tp.sheet.format"].sudo().search([("product_id", "=", product.id), ("active", "=", True)])
        if formats.filtered(lambda sheet: self._tp_record_identity_compatible(sheet, identity, strict=False)):
            return True
        return self._tp_record_identity_compatible(product, identity, strict=False)

    def _tp_build_sheet_sources(self, source_product, identity):
        all_formats = self.env["tp.sheet.format"].sudo().search([("active", "=", True)])
        strict_compatible = all_formats.filtered(
            lambda sheet: self._tp_record_identity_compatible(sheet, identity, strict=True)
        )
        compatible = strict_compatible or all_formats.filtered(
            lambda sheet: self._tp_record_identity_compatible(sheet, identity, strict=False)
        )
        if compatible:
            formats = compatible
        else:
            formats = self.env["tp.sheet.format"].sudo().search(
                [("product_id", "=", source_product.id), ("active", "=", True)]
            )
            compatible = formats.filtered(lambda sheet: self._tp_record_identity_compatible(sheet, identity, strict=False))
            if compatible:
                formats = compatible
        if not formats:
            raise UserError(
                _(
                    "No tp.sheet.format record exists for material %s. "
                    "Create one in Sheet Nesting configuration before running."
                )
                % self._tp_csv_source_label(source_product, identity)
            )

        sources = []
        seen = set()
        for sheet in formats.sorted(lambda rec: (
            int(rec.width_mm or 0) * int(rec.height_mm or 0),
            rec.product_id.display_name or "",
            rec.id,
        )):
            width = int(sheet.width_mm or 0)
            height = int(sheet.height_mm or 0)
            key = (sheet.product_id.id, width, height)
            if key in seen:
                continue
            seen.add(key)
            area = float(width * height)
            unit_cost = float(sheet.landed_cost or sheet.product_id.standard_price or 0.0)
            sources.append(
                {
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
                }
            )
        return sources

    def _tp_ensure_inferred_sheet_format(self, product):
        dims = self._tp_inferred_sheet_dimensions(product)
        if not product or not dims:
            return self.env["tp.sheet.format"].sudo().browse()
        width, height = dims
        Format = self.env["tp.sheet.format"].sudo()
        existing = Format.search(
            [
                ("product_id", "=", product.id),
                ("width_mm", "=", width),
                ("height_mm", "=", height),
            ],
            limit=1,
        )
        if existing:
            return existing if existing.active else Format.browse()

        identity = self._tp_source_product_identity(product)
        vals = {
            "name": "%s %sx%s" % (product.display_name, width, height),
            "product_id": product.id,
            "width_mm": width,
            "height_mm": height,
            "landed_cost": product.standard_price or 0.0,
            "tp_material_type": identity.get("tp_material_type") or False,
            "tp_thickness_mm": identity.get("tp_thickness_mm") or 0.0,
            "tp_colour": identity.get("tp_colour") or False,
            "tp_finish": identity.get("tp_finish") or False,
            "tp_brand_supplier": identity.get("tp_brand_supplier") or False,
        }
        film = (identity.get("tp_protective_film") or "none").strip().lower()
        vals["tp_protective_film"] = film if film in ("paper", "plastic", "none") else "none"
        return Format.create(vals)

    def _tp_build_engine_sources(self, source_product, identity):
        """Build the exact source pool used by both Preview and Run.

        The selected product is only a material anchor. The source pool is built
        from the material identity so the optimiser can choose between every
        compatible available sheet size/product. Offcuts are filtered with the
        same material identity, which
        keeps preview and persisted runs byte-for-byte aligned on inputs.
        """
        identity = identity or {}
        Job = self.env["tp.nesting.job"].sudo()

        sources = self._tp_build_sheet_sources(source_product, identity)
        if not sources:
            raise UserError(_("No usable sheet sources found for %s.") % self._tp_csv_source_label(source_product, identity))
        return sources, Job._tp_run_offcut_sources(source_product, identity)

    @api.model
    def _tp_record_identity_compatible(self, record, identity, *, strict):
        identity = identity or {}
        for field_name in MATERIAL_FIELDS:
            expected = identity.get(field_name)
            if field_name == "tp_thickness_mm":
                expected = self._tp_float_value(expected)
                if expected <= 0:
                    continue
                actual = self._tp_float_value(self._tp_read_material_value(record, field_name))
                if actual <= 0:
                    if strict:
                        return False
                    continue
                if abs(actual - expected) >= 0.0001:
                    return False
                continue

            expected = self._tp_clean_string(expected)
            if not expected or expected == "none":
                continue
            actual = self._tp_clean_string(self._tp_read_material_value(record, field_name))
            if not actual or actual == "none":
                if strict:
                    return False
                continue
            if actual.lower() != expected.lower():
                return False
        return True

    @api.model
    def _tp_read_material_value(self, record, field_name):
        value = False
        if field_name in record._fields:
            value = record[field_name]
        if self._tp_non_empty(value):
            return value
        if "product_id" in record._fields and record.product_id:
            product = record.product_id
            if field_name in product._fields:
                value = product[field_name]
            if self._tp_non_empty(value):
                return value
            if product.product_tmpl_id and field_name in product.product_tmpl_id._fields:
                value = product.product_tmpl_id[field_name]
                if self._tp_non_empty(value):
                    return value
        if "product_tmpl_id" in record._fields and record.product_tmpl_id and field_name in record.product_tmpl_id._fields:
            value = record.product_tmpl_id[field_name]
            if self._tp_non_empty(value):
                return value
        inferred = self._tp_inferred_material_identity(record)
        return inferred.get(field_name) or False

    @api.model
    def _tp_inferred_material_identity(self, record):
        """Best-effort material identity from catalogue codes/names.

        Some source sheet and offcut SKUs predate the structured material fields
        but still carry the useful identity in codes like
        ACR-CLR-000-4.5MM-2440X1220. Keep this conservative: explicit Odoo
        fields still win; this only fills blanks for grouping/source matching.
        """
        text_parts = []
        for attr in ("default_code", "name", "display_name"):
            if hasattr(record, attr):
                value = record[attr] if attr in getattr(record, "_fields", {}) else getattr(record, attr, False)
                if value:
                    text_parts.append(str(value))
        if "product_id" in record._fields and record.product_id:
            product = record.product_id
            for attr in ("default_code", "name", "display_name"):
                value = product[attr] if attr in product._fields else getattr(product, attr, False)
                if value:
                    text_parts.append(str(value))
        if "product_tmpl_id" in record._fields and record.product_tmpl_id:
            tmpl = record.product_tmpl_id
            for attr in ("default_code", "name", "display_name"):
                if attr in tmpl._fields:
                    value = tmpl[attr]
                    if value:
                        text_parts.append(str(value))

        text = " ".join(text_parts)
        upper = text.upper()
        identity = {
            "tp_material_type": "",
            "tp_thickness_mm": 0.0,
            "tp_colour": "",
            "tp_finish": "",
            "tp_protective_film": "",
            "tp_brand_supplier": "",
        }

        thickness_match = re.search(r"(\d+(?:\.\d+)?)\s*MM\b", upper)
        if thickness_match:
            identity["tp_thickness_mm"] = self._tp_float_value(thickness_match.group(1))

        material_map = {
            "ACR": "Acrylic",
            "ACRYLIC": "Acrylic",
            "PERSPEX": "Acrylic",
            "ACM": "Aluminium Composite",
            "ACP": "Aluminium Composite",
            "ALUMINIUM COMPOSITE": "Aluminium Composite",
            "ALUMINUM COMPOSITE": "Aluminium Composite",
            "POLYCARBONATE": "Polycarbonate",
            "PC": "Polycarbonate",
            "PVC": "PVC",
        }
        code_match = re.search(r"\b([A-Z]{2,4})-[A-Z]{2,4}-", upper)
        material_code = code_match.group(1) if code_match else ""
        if material_code in material_map:
            identity["tp_material_type"] = material_map[material_code]
        else:
            for token, label in material_map.items():
                if token in upper and token not in ("PC",):
                    identity["tp_material_type"] = label
                    break

        colour_map = {
            "CLR": "Clear",
            "CLEAR": "Clear",
            "BLK": "Black",
            "BLACK": "Black",
            "WHT": "White",
            "WHITE": "White",
            "OPAL": "Opal",
            "BLU": "Blue",
            "BLUE": "Blue",
            "RED": "Red",
            "GRN": "Green",
            "GREEN": "Green",
            "BRZ": "Bronze",
            "BRONZE": "Bronze",
            "AMB": "Amber",
            "AMBER": "Amber",
            "GRY": "Grey",
            "GREY": "Grey",
            "GRAY": "Grey",
            "YEL": "Yellow",
            "YELLOW": "Yellow",
            "ORG": "Orange",
            "ORANGE": "Orange",
        }
        colour_match = re.search(r"\b[A-Z]{2,4}-([A-Z]{2,5})-", upper)
        colour_code = colour_match.group(1) if colour_match else ""
        if colour_code in colour_map:
            identity["tp_colour"] = colour_map[colour_code]
        else:
            for token, label in colour_map.items():
                if token in upper and token not in ("RED", "PC"):
                    identity["tp_colour"] = label
                    break
        return identity

    @api.model
    def _tp_inferred_sheet_dimensions(self, record):
        text_parts = []
        for attr in ("default_code", "name", "display_name"):
            if attr in getattr(record, "_fields", {}):
                value = record[attr]
                if value:
                    text_parts.append(str(value))
            else:
                value = getattr(record, attr, False)
                if value:
                    text_parts.append(str(value))
        text = " ".join(text_parts).upper()
        if "OFFCUT" in text or "CTS" in text:
            return False
        match = re.search(r"\b(\d{3,4})\s*X\s*(\d{3,4})\b", text)
        if not match:
            return False
        width = int(match.group(1))
        height = int(match.group(2))
        if width <= 0 or height <= 0:
            return False
        return width, height

    @staticmethod
    def _tp_read_first(record, field_names):
        for field_name in field_names:
            if field_name in record._fields:
                value = record[field_name]
                if value not in (False, None, ""):
                    return value
        return False

    @staticmethod
    def _tp_non_empty(value):
        return value not in (False, None, "")

    @staticmethod
    def _tp_clean_string(value):
        if value in (False, None):
            return ""
        return str(value).strip()

    @staticmethod
    def _tp_float_value(value):
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _tp_identity_key(identity):
        return (
            (identity.get("tp_material_type") or "").strip().lower(),
            round(float(identity.get("tp_thickness_mm") or 0.0), 3),
            (identity.get("tp_colour") or "").strip().lower(),
            (identity.get("tp_finish") or "").strip().lower(),
            (identity.get("tp_protective_film") or "").strip().lower(),
            (identity.get("tp_brand_supplier") or "").strip().lower(),
        )

    @staticmethod
    def _tp_group_label(identity):
        thickness = float(identity.get("tp_thickness_mm") or 0.0)
        material = identity.get("tp_material_type") or "Unknown material"
        colour = identity.get("tp_colour") or ""
        parts = []
        if thickness:
            parts.append("%gmm" % thickness)
        if colour:
            parts.append(colour)
        parts.append(material)
        for optional_key in ("tp_finish", "tp_protective_film", "tp_brand_supplier"):
            value = identity.get(optional_key)
            if value:
                parts.append(value)
        return " ".join(parts)

    def _tp_csv_source_label(self, source_product, identity):
        product_label = source_product.display_name or _("Source Sheet")
        material = self._tp_clean_string(identity.get("tp_material_type"))
        group_label = self._tp_group_label(identity)
        if material:
            return group_label
        cleaned = group_label.replace("Unknown material", "").strip()
        if not cleaned:
            return product_label
        return "%s - %s" % (cleaned, product_label)

    @staticmethod
    def _tp_slug(value):
        slug = re.sub(r"[^A-Za-z0-9]+", "-", value or "").strip("-").upper()
        return slug[:40] or "GROUP"

    @api.model
    def _tp_find_stub_job(self, parts):
        """Find any nesting job from the parts list for sandbox DXF/SVG helpers."""
        for part in parts:
            so = part.sale_order_id
            if so:
                job = self.env["tp.nesting.job"].sudo().search([("sale_order_id", "=", so.id)], limit=1)
                if job:
                    return job.id
        return False


class TpNestingRunWizardLine(models.TransientModel):
    _name = "tp.nesting.run.wizard.line"
    _description = "Nesting Run Material Batch"
    _order = "material_type, thickness_mm, colour, id"
    _rec_name = "group_label"

    def _compute_display_name(self):
        for line in self:
            label = line.group_label or _("Material Batch")
            if line.panel_quantity:
                label = _("%(label)s (%(count)s panels)") % {
                    "label": label,
                    "count": line.panel_quantity,
                }
            line.display_name = label

    wizard_id = fields.Many2one("tp.nesting.run.wizard", required=True, ondelete="cascade")
    selected = fields.Boolean(default=True)
    group_key = fields.Char(readonly=True)
    group_label = fields.Char(string="Batch", readonly=True)
    material_type = fields.Char(readonly=True)
    thickness_mm = fields.Float(readonly=True)
    colour = fields.Char(readonly=True)
    finish = fields.Char(readonly=True)
    protective_film = fields.Char(readonly=True)
    brand_supplier = fields.Char(readonly=True)
    panel_count = fields.Integer(string="Rows", readonly=True)
    panel_quantity = fields.Integer(string="Panels", readonly=True)
    cnc_panel_count = fields.Integer(string="CNC", readonly=True)
    source_product_id = fields.Many2one(
        "product.product",
        string="Source Sheet",
        domain="[('id', 'in', candidate_source_product_ids)]",
    )
    candidate_source_product_ids = fields.Many2many("product.product", string="Candidate Source Sheets")
    part_ids = fields.Many2many("tp.web.cut.part", string="Panels", readonly=True)
    warning = fields.Char(readonly=True)

    def _tp_identity(self):
        self.ensure_one()
        return {
            "tp_material_type": self.material_type,
            "tp_thickness_mm": self.thickness_mm,
            "tp_colour": self.colour,
            "tp_finish": self.finish,
            "tp_protective_film": self.protective_film,
            "tp_brand_supplier": self.brand_supplier,
        }


class TpNestingRunWizardManualLine(models.TransientModel):
    _name = "tp.nesting.run.wizard.manual.line"
    _description = "Imported CSV Nesting Panel"
    _order = "id"

    wizard_id = fields.Many2one("tp.nesting.run.wizard", required=True, ondelete="cascade")
    batch_line_id = fields.Many2one(
        "tp.nesting.run.wizard.line",
        string="Batch",
        readonly=True,
        ondelete="cascade",
    )
    part_id = fields.Many2one("tp.web.cut.part", string="Panel Record", readonly=True, ondelete="set null")
    label = fields.Char()
    width_mm = fields.Integer(required=True)
    height_mm = fields.Integer(required=True)
    quantity = fields.Integer(required=True, default=1)

    @api.constrains("width_mm", "height_mm", "quantity")
    def _check_positive_values(self):
        for line in self:
            if line.width_mm <= 0 or line.height_mm <= 0:
                raise ValidationError(_("Manual panel width and height must be greater than 0 mm."))
            if line.quantity <= 0:
                raise ValidationError(_("Manual panel quantity must be greater than 0."))

    def write(self, vals):
        watched = {"label", "width_mm", "height_mm", "quantity"}
        res = super().write(vals)
        if watched.intersection(vals):
            self._tp_sync_manual_part_records()
        return res

    def unlink(self):
        affected_wizards = self.mapped("wizard_id")
        affected_lines = self.mapped("batch_line_id")
        manual_parts = self.mapped("part_id").sudo()
        for line in affected_lines:
            linked = manual_parts.filtered(lambda part, line=line: part.id in line.part_ids.ids)
            if linked:
                line.write({"part_ids": [(3, part_id) for part_id in linked.ids]})
        removable = manual_parts.filtered(lambda part: not part.allocation_ids)
        res = super().unlink()
        if removable:
            removable.unlink()
        for wizard in affected_wizards:
            wizard._tp_recompute_line_counts(affected_lines.filtered(lambda line: line.wizard_id == wizard))
            wizard.write({"state": "draft", "preview_summary": False, "preview_svg": False})
        return res

    def _tp_sync_manual_part_records(self):
        affected_lines = self.mapped("batch_line_id")
        for line in self:
            if not line.part_id:
                continue
            line.part_id.sudo().write({
                "label": line.label or line.part_id.part_key,
                "width_mm": line.width_mm,
                "height_mm": line.height_mm,
                "quantity": line.quantity,
            })
        for wizard in self.mapped("wizard_id"):
            wizard._tp_recompute_line_counts(affected_lines.filtered(lambda line: line.wizard_id == wizard))
            wizard.write({"state": "draft", "preview_summary": False, "preview_svg": False})
