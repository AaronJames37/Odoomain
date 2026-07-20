import base64
import json
import logging
import os
import secrets
import time

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class TpNestingPreviewSession(models.Model):
    _name = "tp.nesting.preview.session"
    _description = "Live Nesting Preview Session"
    _order = "id desc"

    name = fields.Char(default=lambda self: _("Live Nesting Preview"), required=True)
    user_id = fields.Many2one("res.users", default=lambda self: self.env.user, required=True)
    company_id = fields.Many2one("res.company", default=lambda self: self.env.company, required=True)
    access_token = fields.Char(default=lambda self: secrets.token_urlsafe(24), required=True, copy=False)
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("running", "Running"),
            ("done", "Done"),
            ("failed", "Failed"),
            ("cancelled", "Cancelled"),
            ("committed", "Committed"),
        ],
        default="draft",
        required=True,
    )
    nesting_input_mode = fields.Selection(
        [("active_orders", "Queued orders"), ("csv_only", "CSV only")],
        default="active_orders",
        required=True,
    )
    kerf_mm = fields.Integer(default=lambda self: self.env.company.tp_nesting_kerf_mm or 3, required=True)
    progress_pct = fields.Float(default=0.0)
    update_sequence = fields.Integer(default=0)
    cancel_requested = fields.Boolean(default=False)
    started_at = fields.Datetime(readonly=True)
    finished_at = fields.Datetime(readonly=True)
    message = fields.Char()
    last_error = fields.Text()
    current_batch_id = fields.Many2one("tp.nesting.preview.batch", readonly=True)
    batch_ids = fields.One2many("tp.nesting.preview.batch", "session_id")
    result_ids = fields.One2many("tp.nesting.preview.result", "session_id")
    run_ids = fields.Many2many("tp.nesting.run", string="Created Runs", readonly=True)

    @api.model
    def app_bootstrap(self):
        session = self.create(
            {
                "name": _("Live Nesting Preview"),
                "nesting_input_mode": "active_orders",
                "kerf_mm": self.env.company.tp_nesting_kerf_mm or 3,
            }
        )
        session._refresh_active_order_batches()
        return session._app_state()

    def refresh_batches(self):
        self.ensure_one()
        self._ensure_editable()
        if self.nesting_input_mode == "active_orders":
            self._refresh_active_order_batches()
        self._touch(message=_("Batches refreshed."))
        return self._app_state()

    def set_input_mode(self, mode, source_product_id=False):
        self.ensure_one()
        self._ensure_editable()
        if mode not in ("active_orders", "csv_only"):
            raise UserError(_("Unknown nesting input mode."))
        vals = {
            "nesting_input_mode": mode,
            "message": False,
            "last_error": False,
            "progress_pct": 0.0,
        }
        self.write(vals)
        self.batch_ids.unlink()
        if mode == "active_orders":
            self._refresh_active_order_batches()
        elif source_product_id:
            self._create_csv_batch(int(source_product_id))
        self._touch(message=_("Input mode updated."))
        return self._app_state()

    def update_batch(self, batch_id, vals):
        self.ensure_one()
        self._ensure_editable()
        Wizard = self.env["tp.nesting.run.wizard"].sudo()
        batch = self.batch_ids.filtered(lambda item: item.id == int(batch_id))
        if not batch:
            raise UserError(_("Batch not found."))
        allowed = {}
        if "selected" in vals:
            allowed["selected"] = bool(vals["selected"])
        if "source_product_id" in vals:
            product_id = int(vals["source_product_id"] or 0)
            allowed["source_product_id"] = product_id or False
            if (batch.group_key or "").startswith("manual_csv:") and product_id:
                product = self.env["product.product"].sudo().browse(product_id)
                identity = Wizard._tp_source_product_identity(product) if product.exists() else {}
                allowed.update({
                    "group_key": "manual_csv:%s:%s" % (self.id, product_id),
                    "group_label": _("Manual/CSV - %s") % Wizard._tp_csv_source_label(product, identity),
                    "material_type": identity.get("tp_material_type") or "",
                    "thickness_mm": identity.get("tp_thickness_mm") or 0.0,
                    "colour": identity.get("tp_colour") or "",
                    "finish": identity.get("tp_finish") or "",
                    "protective_film": identity.get("tp_protective_film") or "",
                    "brand_supplier": identity.get("tp_brand_supplier") or "",
                })
        if allowed:
            batch.write(allowed)
            self._touch(message=_("Batch updated."))
        return self._app_state()

    def update_settings(self, vals):
        self.ensure_one()
        self._ensure_editable()
        vals = vals or {}
        writes = {}
        if "kerf_mm" in vals:
            writes["kerf_mm"] = max(0, int(vals.get("kerf_mm") or 0))
        if writes:
            self.write(writes)
        if "ignore_sheet_stock" in vals:
            self.env.company.sudo().write({
                "tp_nesting_ignore_sheet_stock": bool(vals.get("ignore_sheet_stock")),
            })
        self._touch(message=_("Settings updated."))
        return self._app_state()

    def import_csv(self, file_data, filename=False, target_batch_id=False, csv_only=False, source_product_id=False):
        self.ensure_one()
        self._ensure_editable()
        Wizard = self.env["tp.nesting.run.wizard"].sudo()
        rows, skipped_disabled = Wizard._tp_parse_manual_csv(file_data)

        if source_product_id:
            existing_source_id = self.batch_ids[:1].source_product_id.id if self.batch_ids[:1].source_product_id else False
            chosen_source_id = int(source_product_id or existing_source_id or 0)
            if not chosen_source_id:
                raise UserError(_("Choose a Manual/CSV material before importing."))
            if csv_only or self.nesting_input_mode == "csv_only":
                self.write({"nesting_input_mode": "csv_only"})
                self.batch_ids.filtered(
                    lambda item: not (item.group_key or "").startswith("manual_csv:")
                ).unlink()
            batch = self._get_or_create_manual_batch(chosen_source_id)
        elif csv_only or self.nesting_input_mode == "csv_only":
            raise UserError(_("Choose a Manual/CSV material before importing."))
        else:
            batch = self.batch_ids.filtered(lambda item: item.id == int(target_batch_id or 0))
            if not batch:
                selected = self.batch_ids.filtered("selected")
                if len(selected) == 1:
                    batch = selected
            if not batch:
                raise UserError(_("Choose the material batch to import into."))

        Part = self.env["tp.web.cut.part"].sudo()
        created = Part.browse()
        stamp = fields.Datetime.now().strftime("%Y%m%d%H%M%S")
        for idx, row in enumerate(rows, start=1):
            label = row.get("label") or "MANUAL-%s-%s-%03d" % (self.id, stamp, idx)
            part = Part.create(self._manual_part_vals(batch, row, label))
            created |= part

        batch.write({"part_ids": [(4, part.id) for part in created]})
        batch._recompute_counts()
        self._touch(
            message=_("Imported %(count)d panel row(s).") % {"count": len(rows)}
            + (_(" Skipped %(count)d disabled row(s).") % {"count": skipped_disabled} if skipped_disabled else "")
        )
        return self._app_state()

    def add_manual_panel(
        self,
        width_mm,
        height_mm,
        quantity=1,
        label=False,
        target_batch_id=False,
        source_product_id=False,
        csv_only=False,
    ):
        self.ensure_one()
        self._ensure_editable()

        def to_int(value, default=0):
            try:
                return int(round(float(value or default)))
            except (TypeError, ValueError):
                return int(default)

        width = to_int(width_mm)
        height = to_int(height_mm)
        qty = max(1, to_int(quantity, 1))
        if width <= 0 or height <= 0:
            raise UserError(_("Enter a valid panel width and height."))

        if source_product_id:
            if csv_only or self.nesting_input_mode == "csv_only":
                self.write({"nesting_input_mode": "csv_only"})
                self.batch_ids.filtered(
                    lambda item: not (item.group_key or "").startswith("manual_csv:")
                ).unlink()
                batch = self._get_or_create_manual_batch(int(source_product_id))
            else:
                batch = self._get_or_create_manual_batch(int(source_product_id))
        elif csv_only or self.nesting_input_mode == "csv_only":
            raise UserError(_("Choose a material before adding manual panels."))
        else:
            batch = self.batch_ids.filtered(lambda item: item.id == int(target_batch_id or 0))
            if not batch:
                selected = self.batch_ids.filtered("selected")
                if len(selected) == 1:
                    batch = selected
            if not batch:
                raise UserError(_("Choose the material batch to add the manual panel to."))

        stamp = fields.Datetime.now().strftime("%Y%m%d%H%M%S")
        row = {"width_mm": width, "height_mm": height, "quantity": qty}
        clean_label = (label or "").strip()
        part_label = clean_label or "MANUAL-%s-%s-%s" % (self.id, batch.id, stamp)
        part = self.env["tp.web.cut.part"].sudo().create(
            self._manual_part_vals(batch, row, part_label, part_label, origin="live_nesting_preview_manual")
        )
        batch.write({"selected": True, "part_ids": [(4, part.id)]})
        batch._recompute_counts()
        self._touch(message=_("Added %(qty)d x %(width)d x %(height)dmm panel(s).") % {
            "qty": qty,
            "width": width,
            "height": height,
        })
        return self._app_state()

    def delete_manual_panel(self, part_id):
        self.ensure_one()
        self._ensure_editable()
        part = self.env["tp.web.cut.part"].sudo().browse(int(part_id or 0))
        if not part.exists():
            raise UserError(_("Manual panel not found."))
        linked_batches = self.batch_ids.filtered(
            lambda batch: part in batch.with_context(active_test=False).part_ids
        )
        manual_batches = linked_batches.filtered(lambda batch: self._is_manual_preview_batch(batch))
        if not manual_batches:
            raise UserError(_("That panel is not part of this manual/CSV nesting session."))

        linked_batches.write({"part_ids": [(3, part.id)]})
        for batch in linked_batches:
            batch._recompute_counts()
            if self._is_manual_preview_batch(batch) and not batch.with_context(active_test=False).part_ids.filtered("active"):
                batch.selected = False

        payload = part.source_payload if isinstance(part.source_payload, dict) else {}
        if int(payload.get("preview_session_id") or 0) == self.id:
            part.unlink()
        self._touch(message=_("Manual panel removed."))
        return self._app_state()

    def toggle_manual_panel(self, part_id, active):
        self.ensure_one()
        self._ensure_editable()
        part = self.env["tp.web.cut.part"].sudo().browse(int(part_id or 0))
        if not part.exists():
            raise UserError(_("Manual panel not found."))
        linked_batches = self.batch_ids.filtered(
            lambda batch: part in batch.with_context(active_test=False).part_ids
        )
        manual_batches = linked_batches.filtered(lambda batch: self._is_manual_preview_batch(batch))
        if not manual_batches:
            raise UserError(_("That panel is not part of this manual/CSV nesting session."))

        part.active = bool(active)
        for batch in linked_batches:
            batch._recompute_counts()
            if self._is_manual_preview_batch(batch):
                batch.selected = bool(batch.with_context(active_test=False).part_ids.filtered("active"))
        self._touch(message=_("Manual panel activated.") if active else _("Manual panel deactivated."))
        return self._app_state()

    def start_preview(self, selected_batch_ids=None, kerf_mm=None):
        self.ensure_one()
        if self.state == "running":
            return self._app_state()
        self._clear_cancel_marker()
        selected_ids = [int(value) for value in (selected_batch_ids or [])]
        if selected_ids:
            for batch in self.batch_ids:
                batch.selected = batch.id in selected_ids
        if kerf_mm is not None:
            self.kerf_mm = max(0, int(kerf_mm or 0))

        self._merge_manual_batches_into_selected_orders()
        selected = self.batch_ids.filtered("selected")
        if not selected:
            raise UserError(_("Select at least one material batch to preview."))
        missing = selected.filtered(lambda batch: not batch.source_product_id)
        if missing:
            raise UserError(
                _("Choose a material for:\n%s")
                % "\n".join("  - " + batch.group_label for batch in missing)
            )

        selected.write(
            {
                "state": "queued",
                "progress_pct": 0.0,
                "message": False,
                "last_error": False,
                "best_result_id": False,
                "part_snapshot_json": False,
            }
        )
        (self.batch_ids - selected).write({"state": "draft", "progress_pct": 0.0})
        self.result_ids.unlink()
        self.write(
            {
                "state": "running",
                "cancel_requested": False,
                "started_at": fields.Datetime.now(),
                "finished_at": False,
                "progress_pct": 0.0,
                "current_batch_id": False,
                "last_error": False,
                "message": _("Preview started."),
            }
        )
        self._publish()
        return self._app_state()

    def run_preview_loop(self):
        self.ensure_one()
        if self.state != "running":
            return self._app_state()

        selected = self.batch_ids.filtered("selected").sorted(lambda b: (b.sequence, b.id))
        total = max(1, len(selected))
        completed = 0
        try:
            for batch in selected:
                if self._cancel_requested():
                    batch.write({"state": "cancelled", "message": _("Cancelled.")})
                    continue
                self.write({"current_batch_id": batch.id})
                batch._run_live_preview()
                completed += 1
                self.progress_pct = completed / total * 100.0
                self._publish()
                self.env.cr.commit()

            if self._cancel_requested():
                self.write(
                    {
                        "state": "cancelled",
                        "finished_at": fields.Datetime.now(),
                        "message": _("Preview cancelled."),
                        "current_batch_id": False,
                    }
                )
                self._clear_cancel_marker()
            else:
                failed = selected.filtered(lambda batch: batch.state == "failed")
                self.write(
                    {
                        "state": "failed" if failed else "done",
                        "finished_at": fields.Datetime.now(),
                        "progress_pct": 100.0,
                        "message": _("Preview failed for one or more batches.") if failed else _("Preview complete."),
                        "current_batch_id": False,
                    }
                )
        except Exception as exc:  # noqa: BLE001 - surface in app state
            _logger.exception("Live nesting preview session %s failed", self.id)
            self.write(
                {
                    "state": "failed",
                    "finished_at": fields.Datetime.now(),
                    "last_error": str(exc),
                    "message": _("Preview failed."),
                    "current_batch_id": False,
                }
            )
        self._publish()
        self.env.cr.commit()
        return self._app_state()

    def run_preview_batch(self, batch_id):
        self.ensure_one()
        batch = self.batch_ids.filtered(lambda item: item.id == int(batch_id or 0))[:1]
        if not batch:
            raise UserError(_("Batch not found."))
        if self.state != "running" or not batch.selected:
            return self._app_state()
        if self._cancel_requested():
            batch.write({"state": "cancelled", "progress_pct": 100.0, "message": _("Cancelled.")})
            self._publish(touch=False)
            self.env.cr.commit()
            return self._app_state()
        try:
            batch._run_live_preview(publish_session=False)
        except Exception as exc:  # noqa: BLE001 - surface per-batch without killing siblings
            _logger.exception("Live nesting preview batch %s failed", batch.id)
            batch.write(
                {
                    "state": "failed",
                    "progress_pct": 100.0,
                    "last_error": str(exc),
                    "message": _("Preview failed."),
                }
            )
            self._publish(touch=False)
            self.env.cr.commit()
        return self._app_state()

    def finish_preview(self):
        self.ensure_one()
        if self.state != "running":
            return self._app_state()
        selected = self.batch_ids.filtered("selected")
        if selected.filtered(lambda batch: batch.state in ("queued", "running")):
            return self._app_state()
        cancelled = bool(self._cancel_requested()) or bool(selected) and all(
            batch.state == "cancelled" for batch in selected
        )
        if cancelled:
            vals = {
                "state": "cancelled",
                "finished_at": fields.Datetime.now(),
                "progress_pct": 100.0,
                "message": _("Preview cancelled."),
                "current_batch_id": False,
            }
            self._clear_cancel_marker()
        else:
            failed = selected.filtered(lambda batch: batch.state == "failed")
            vals = {
                "state": "failed" if failed else "done",
                "finished_at": fields.Datetime.now(),
                "progress_pct": 100.0,
                "message": _("Preview failed for one or more batches.") if failed else _("Preview complete."),
                "current_batch_id": False,
            }
        self.write(vals)
        self._publish()
        self.env.cr.commit()
        return self._app_state()

    def poll(self, last_sequence=0):
        self.ensure_one()
        return self._app_state(last_sequence=int(last_sequence or 0))

    def cancel(self):
        self.ensure_one()
        if self.state == "running":
            self._write_cancel_marker()
            state = self._app_state()
            state.update({"message": _("Cancelling..."), "cancelRequested": True})
            return state
        self._clear_cancel_marker()
        return self._app_state()

    def _cancel_marker_path(self):
        self.ensure_one()
        token = "".join(ch for ch in (self.access_token or "") if ch.isalnum() or ch in ("-", "_"))
        return "/tmp/tp_nesting_preview_cancel_%s_%s" % (self.id, token[:48] or "session")

    def _write_cancel_marker(self):
        self.ensure_one()
        with open(self._cancel_marker_path(), "w", encoding="utf-8") as handle:
            handle.write(str(time.time()))

    def _clear_cancel_marker(self):
        self.ensure_one()
        try:
            os.unlink(self._cancel_marker_path())
        except FileNotFoundError:
            pass

    def _cancel_requested(self):
        self.ensure_one()
        return bool(os.path.exists(self._cancel_marker_path()))

    def commit_best(self):
        self.ensure_one()
        if self.state == "running":
            self._write_cancel_marker()
        selected = self.batch_ids.filtered("selected")
        missing = selected.filtered(lambda batch: not batch.best_result_id)
        if missing:
            raise UserError(
                _("No preview result exists for:\n%s")
                % "\n".join("  - " + batch.group_label for batch in missing)
            )
        if self.state != "running":
            incomplete = selected.filtered(lambda batch: batch.state != "done")
            if incomplete:
                raise UserError(
                    _(
                        "The preview is not complete enough to run yet:\n%s\n\n"
                        "Run Preview again and let it finish successfully before committing."
                    )
                    % "\n".join("  - %s: %s" % (
                        batch.group_label,
                        batch.last_error or batch.message or batch.state,
                    ) for batch in incomplete)
                )

        runs = self.env["tp.nesting.run"].sudo()
        for batch in selected.sorted(lambda b: (b.sequence, b.id)):
            runs |= batch._commit_best_result()
        self.write(
            {
                "state": "committed",
                "run_ids": [(6, 0, runs.ids)],
                "finished_at": fields.Datetime.now(),
                "message": _("Created %(count)d nesting run(s).") % {"count": len(runs)},
                "current_batch_id": False,
            }
        )
        self._publish()
        action = {
            "type": "ir.actions.act_window",
            "name": _("Created Nesting Runs"),
            "res_model": "tp.nesting.run",
            "view_mode": "list,form",
            "views": [[False, "list"], [False, "form"]],
            "domain": [("id", "in", runs.ids)],
            "target": "current",
            "context": {"create": False},
        }
        if len(runs) == 1:
            action.update({
                "view_mode": "form",
                "views": [[False, "form"]],
                "res_id": runs.id,
            })
        return action

    def _ensure_editable(self):
        self.ensure_one()
        if self.state == "running":
            raise UserError(_("Wait for the current preview to finish or cancel it first."))

    @staticmethod
    def _is_manual_preview_batch(batch):
        return (batch.group_key or "").startswith(("manual_csv:", "csv_only:"))

    @staticmethod
    def _tp_token_set(value):
        return {
            token
            for token in "".join(ch.lower() if ch.isalnum() else " " for ch in (value or "")).split()
            if token
        }

    def _manual_batch_matches_order_batch(self, manual_batch, order_batch):
        self.ensure_one()
        Wizard = self.env["tp.nesting.run.wizard"].sudo()
        if manual_batch.source_product_id:
            order_sources = order_batch.source_product_id | order_batch.candidate_source_product_ids
            if manual_batch.source_product_id in order_sources:
                return True

        manual_identity = manual_batch._identity()
        order_identity = order_batch._identity()
        if Wizard._tp_identity_key(manual_identity) == Wizard._tp_identity_key(order_identity):
            return True

        manual_thickness = round(float(manual_identity.get("tp_thickness_mm") or 0.0), 3)
        order_thickness = round(float(order_identity.get("tp_thickness_mm") or 0.0), 3)
        if manual_thickness != order_thickness:
            return False

        for key in ("tp_finish", "tp_protective_film", "tp_brand_supplier"):
            manual_value = (manual_identity.get(key) or "").strip().lower()
            order_value = (order_identity.get(key) or "").strip().lower()
            if manual_value and order_value and manual_value != order_value:
                return False

        manual_tokens = self._tp_token_set(manual_identity.get("tp_material_type"))
        manual_tokens |= self._tp_token_set(manual_identity.get("tp_colour"))
        order_tokens = self._tp_token_set(order_identity.get("tp_material_type"))
        order_tokens |= self._tp_token_set(order_identity.get("tp_colour"))
        return bool(manual_tokens) and manual_tokens.issubset(order_tokens)

    def _manual_overlay_parts(self):
        self.ensure_one()
        manual_batches = self.batch_ids.filtered(lambda batch: self._is_manual_preview_batch(batch))
        return manual_batches.with_context(active_test=False).mapped("part_ids")

    def _clear_manual_order_overlays(self):
        self.ensure_one()
        manual_parts = self._manual_overlay_parts()
        if not manual_parts:
            return
        order_batches = self.batch_ids.filtered(lambda batch: not self._is_manual_preview_batch(batch))
        for batch in order_batches:
            linked = batch.with_context(active_test=False).part_ids & manual_parts
            if not linked:
                continue
            batch.write({"part_ids": [(3, part.id) for part in linked]})
            batch._recompute_counts()

    def _merge_manual_batches_into_selected_orders(self):
        self.ensure_one()
        self._clear_manual_order_overlays()

        selected = self.batch_ids.filtered("selected")
        manual_batches = selected.filtered(lambda batch: self._is_manual_preview_batch(batch))
        order_batches = selected - manual_batches
        if not manual_batches or not order_batches:
            return

        for manual_batch in manual_batches.sorted(lambda item: (item.sequence, item.id)):
            active_parts = manual_batch.with_context(active_test=False).part_ids.filtered("active")
            if not active_parts:
                manual_batch.selected = False
                continue
            target = order_batches.filtered(
                lambda batch: self._manual_batch_matches_order_batch(manual_batch, batch)
            )[:1]
            if not target:
                continue
            target.write({"part_ids": [(4, part.id) for part in active_parts]})
            target._recompute_counts()
            manual_batch.write(
                {
                    "selected": False,
                    "state": "draft",
                    "progress_pct": 0.0,
                    "message": _("Merged into %s for preview.") % target.group_label,
                    "last_error": False,
                    "best_result_id": False,
                    "part_snapshot_json": False,
                }
            )

    def _refresh_active_order_batches(self):
        self.ensure_one()
        Wizard = self.env["tp.nesting.run.wizard"].sudo()
        self._clear_manual_order_overlays()
        manual_batches = self.batch_ids.filtered(lambda batch: (batch.group_key or "").startswith("manual_csv:"))
        (self.batch_ids - manual_batches).unlink()
        commands = Wizard._tp_build_group_line_commands()
        sequence = 0
        for command in commands:
            vals = command[2]
            sequence += 10
            self.env["tp.nesting.preview.batch"].create(
                self._batch_vals_from_wizard_vals(vals, sequence=sequence)
            )
        if manual_batches:
            base_sequence = sequence + 10
            for offset, batch in enumerate(manual_batches.sorted(lambda item: (item.sequence, item.id)), start=0):
                batch.sequence = base_sequence + offset * 10

    def _batch_vals_from_wizard_vals(self, vals, *, sequence):
        def m2m_ids(value):
            ids = []
            for cmd in value or []:
                if cmd[0] == 6:
                    ids.extend(cmd[2])
                elif cmd[0] == 4:
                    ids.append(cmd[1])
            return ids

        return {
            "session_id": self.id,
            "sequence": sequence,
            "selected": False,
            "group_key": vals.get("group_key") or "",
            "group_label": vals.get("group_label") or "",
            "material_type": vals.get("material_type") or "",
            "thickness_mm": vals.get("thickness_mm") or 0.0,
            "colour": vals.get("colour") or "",
            "finish": vals.get("finish") or "",
            "protective_film": vals.get("protective_film") or "",
            "brand_supplier": vals.get("brand_supplier") or "",
            "panel_count": vals.get("panel_count") or 0,
            "panel_quantity": vals.get("panel_quantity") or 0,
            "cnc_panel_count": vals.get("cnc_panel_count") or 0,
            "source_product_id": vals.get("source_product_id") or False,
            "candidate_source_product_ids": [(6, 0, m2m_ids(vals.get("candidate_source_product_ids")))],
            "part_ids": [(6, 0, m2m_ids(vals.get("part_ids")))],
            "warning": vals.get("warning") or False,
        }

    def _create_csv_batch(self, source_product_id):
        self.ensure_one()
        Wizard = self.env["tp.nesting.run.wizard"].sudo()
        source_product = self.env["product.product"].sudo().browse(source_product_id)
        if not source_product.exists():
            raise UserError(_("Choose a valid material."))
        identity = Wizard._tp_source_product_identity(source_product)
        candidates = Wizard._tp_all_source_products_with_active_formats()
        if source_product not in candidates:
            candidates |= source_product
        batch = self.env["tp.nesting.preview.batch"].create(
            {
                "session_id": self.id,
                "sequence": 10,
                "selected": True,
                "group_key": "csv_only:%s" % self.id,
                "group_label": _("CSV Only - %s") % Wizard._tp_csv_source_label(source_product, identity),
                "material_type": identity.get("tp_material_type") or "",
                "thickness_mm": identity.get("tp_thickness_mm") or 0.0,
                "colour": identity.get("tp_colour") or "",
                "finish": identity.get("tp_finish") or "",
                "protective_film": identity.get("tp_protective_film") or "",
                "brand_supplier": identity.get("tp_brand_supplier") or "",
                "source_product_id": source_product.id,
                "candidate_source_product_ids": [(6, 0, candidates.ids)],
            }
        )
        return batch

    def _get_or_create_manual_batch(self, source_product_id):
        self.ensure_one()
        source_product_id = int(source_product_id or 0)
        batch = self.batch_ids.filtered(
            lambda item: (item.group_key or "") == "manual_csv:%s:%s" % (self.id, source_product_id)
        )[:1]
        if batch:
            batch.selected = True
            return batch

        Wizard = self.env["tp.nesting.run.wizard"].sudo()
        source_product = self.env["product.product"].sudo().browse(source_product_id)
        if not source_product.exists():
            raise UserError(_("Choose a valid material."))
        identity = Wizard._tp_source_product_identity(source_product)
        candidates = Wizard._tp_all_source_products_with_active_formats()
        if source_product not in candidates:
            candidates |= source_product
        sequence = (max(self.batch_ids.mapped("sequence") or [0]) or 0) + 10
        return self.env["tp.nesting.preview.batch"].create(
            {
                "session_id": self.id,
                "sequence": sequence,
                "selected": True,
                "group_key": "manual_csv:%s:%s" % (self.id, source_product.id),
                "group_label": _("Manual/CSV - %s") % Wizard._tp_csv_source_label(source_product, identity),
                "material_type": identity.get("tp_material_type") or "",
                "thickness_mm": identity.get("tp_thickness_mm") or 0.0,
                "colour": identity.get("tp_colour") or "",
                "finish": identity.get("tp_finish") or "",
                "protective_film": identity.get("tp_protective_film") or "",
                "brand_supplier": identity.get("tp_brand_supplier") or "",
                "source_product_id": source_product.id,
                "candidate_source_product_ids": [(6, 0, candidates.ids)],
            }
        )

    def _manual_part_vals(self, batch, row, label, part_key=False, origin="live_nesting_preview_csv"):
        part_key = part_key or label
        return {
            "company_id": self.env.company.id,
            "part_key": part_key,
            "label": label,
            "width_mm": row["width_mm"],
            "height_mm": row["height_mm"],
            "quantity": row["quantity"],
            "shape": "rectangle",
            "active": True,
            "material": batch.material_type or False,
            "colour": batch.colour or False,
            "finish": batch.finish or False,
            "protective_film": batch.protective_film or False,
            "brand_supplier": batch.brand_supplier or False,
            "thickness_mm": batch.thickness_mm or 0.0,
            "tp_material_type": batch.material_type or False,
            "tp_thickness_mm": batch.thickness_mm or 0.0,
            "tp_colour": batch.colour or False,
            "tp_finish": batch.finish or False,
            "tp_brand_supplier": batch.brand_supplier or False,
            "source_payload": {
                "origin": origin,
                "preview_session_id": self.id,
                "preview_batch_id": batch.id,
            },
        }

    def _touch(self, message=False):
        vals = {"update_sequence": self.update_sequence + 1}
        if message is not False:
            vals["message"] = message
        self.write(vals)
        self._publish()

    def _publish(self, touch=True):
        self.ensure_one()
        if touch:
            self.update_sequence = self.update_sequence + 1
        payload = {
            "session_id": self.id,
            "sequence": self.update_sequence,
            "state": self.state,
            "progress_pct": self.progress_pct,
            "message": self.message or "",
        }
        self.env["bus.bus"].sudo()._sendone(self._bus_channel(), "tp_nesting_preview_update", payload)

    def _bus_channel(self):
        self.ensure_one()
        return "tp_nesting_preview_%s_%s" % (self.id, self.access_token)

    def _app_state(self, last_sequence=0):
        self.ensure_one()
        Wizard = self.env["tp.nesting.run.wizard"].sudo()
        all_sources = Wizard._tp_all_source_products_with_active_formats()
        batches = [batch._app_state() for batch in self.batch_ids.sorted(lambda b: (b.sequence, b.id))]
        selected_batches = [batch for batch in batches if batch.get("selected")]
        if self.state == "running" and selected_batches:
            progress_pct = sum(float(batch.get("progressPct") or 0.0) for batch in selected_batches) / len(selected_batches)
        else:
            progress_pct = self.progress_pct
        return {
            "id": self.id,
            "name": self.name,
            "state": self.state,
            "inputMode": self.nesting_input_mode,
            "kerfMm": self.kerf_mm,
            "ignoreSheetStock": bool(self.env.company.tp_nesting_ignore_sheet_stock),
            "progressPct": progress_pct,
            "message": self.message or "",
            "lastError": self.last_error or "",
            "updateSequence": self.update_sequence,
            "busChannel": self._bus_channel(),
            "currentBatchId": self.current_batch_id.id or False,
            "allSources": self._material_options(all_sources),
            "batches": batches,
            "runs": [{"id": run.id, "name": run.name} for run in self.run_ids],
            "parallelBatchLimit": 4,
            "changed": self.state == "running" or self.update_sequence > int(last_sequence or 0),
        }

    def _material_options(self, products):
        Wizard = self.env["tp.nesting.run.wizard"].sudo()
        SheetFormat = self.env["tp.sheet.format"].sudo()
        groups = {}
        for product in products:
            identity = Wizard._tp_source_product_identity(product)
            key = Wizard._tp_identity_key(identity)
            entry = groups.setdefault(
                key,
                {
                    "id": product.id,
                    "name": Wizard._tp_csv_source_label(product, identity),
                    "product_ids": [],
                    "sizes": set(),
                },
            )
            entry["id"] = min(entry["id"], product.id)
            entry["product_ids"].append(product.id)
            sheets = SheetFormat.search(SheetFormat._tp_internal_nesting_domain() + [("product_id", "=", product.id)])
            for sheet in sheets._tp_filter_in_stock_for_nesting():
                width = int(sheet.width_mm or 0)
                height = int(sheet.height_mm or 0)
                if width and height:
                    entry["sizes"].add((width, height))

        options = []
        for entry in groups.values():
            sizes = sorted(entry["sizes"])
            size_summary = ", ".join("%sx%s" % size for size in sizes[:3])
            if len(sizes) > 3:
                size_summary += " +%d" % (len(sizes) - 3)
            options.append({
                "id": entry["id"],
                "name": entry["name"],
                "sourceCount": len(set(entry["product_ids"])),
                "sheetSizeCount": len(sizes),
                "sheetSizeSummary": size_summary,
            })
        return sorted(options, key=lambda item: (item["name"] or "", item["id"]))


class TpNestingPreviewBatch(models.Model):
    _name = "tp.nesting.preview.batch"
    _description = "Live Nesting Preview Batch"
    _order = "sequence, id"

    session_id = fields.Many2one("tp.nesting.preview.session", required=True, ondelete="cascade")
    sequence = fields.Integer(default=10)
    selected = fields.Boolean(default=False)
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("queued", "Queued"),
            ("running", "Running"),
            ("done", "Done"),
            ("failed", "Failed"),
            ("cancelled", "Cancelled"),
        ],
        default="draft",
        required=True,
    )
    group_key = fields.Char()
    group_label = fields.Char(required=True)
    material_type = fields.Char()
    thickness_mm = fields.Float()
    colour = fields.Char()
    finish = fields.Char()
    protective_film = fields.Char()
    brand_supplier = fields.Char()
    panel_count = fields.Integer(default=0)
    panel_quantity = fields.Integer(default=0)
    cnc_panel_count = fields.Integer(default=0)
    source_product_id = fields.Many2one("product.product")
    candidate_source_product_ids = fields.Many2many(
        "product.product",
        "tp_nesting_preview_batch_product_rel",
        "batch_id",
        "product_id",
    )
    part_ids = fields.Many2many(
        "tp.web.cut.part",
        "tp_nesting_preview_batch_part_rel",
        "batch_id",
        "part_id",
    )
    warning = fields.Char()
    progress_pct = fields.Float(default=0.0)
    message = fields.Char()
    last_error = fields.Text()
    part_snapshot_json = fields.Json()
    best_result_id = fields.Many2one("tp.nesting.preview.result", readonly=True)
    result_ids = fields.One2many("tp.nesting.preview.result", "batch_id")

    def _recompute_counts(self):
        for batch in self:
            parts = batch.with_context(active_test=False).part_ids.filtered("active")
            batch.write(
                {
                    "panel_count": len(parts),
                    "panel_quantity": sum(max(1, int(part.quantity or 1)) for part in parts),
                    "cnc_panel_count": len(parts.filtered("cnc_required")) if "cnc_required" in parts._fields else 0,
                }
            )

    def _run_live_preview(self, publish_session=True):
        self.ensure_one()
        session = self.session_id
        Job = self.env["tp.nesting.job"].sudo()
        Sandbox = self.env["tp.nesting.sandbox"].sudo()
        Wizard = self.env["tp.nesting.run.wizard"].sudo()
        parts = self.part_ids.filtered("active")
        if not parts:
            self.write({"state": "failed", "last_error": _("No active panels in this batch.")})
            session._publish(touch=publish_session)
            return

        self.write(
            {
                "state": "running",
                "progress_pct": 0.0,
                "message": _("Finding first layout..."),
                "last_error": False,
                "part_snapshot_json": self._parts_snapshot(parts),
            }
        )
        session._publish(touch=publish_session)
        self.env.cr.commit()

        cuts = Job._tp_run_build_cuts(parts)
        sources, offcut_sources = Wizard._tp_build_engine_sources(self.source_product_id, self._identity())
        budget = self.env.company.tp_nesting_guillotine_seconds or 10
        last_publish = {"at": 0.0}

        def cancel_requested():
            return session._cancel_requested()

        def publish_progress(plan, event=None):
            if not plan or not plan.get("ok"):
                return
            now = time.monotonic()
            if event not in ("best", "first", "final") and now - last_publish["at"] < 0.75:
                return
            last_publish["at"] = now
            self._store_result(plan, parts, cuts, Sandbox, event=event or "best")
            self.progress_pct = min(95.0, max(self.progress_pct or 0.0, 10.0))
            self.message = _("Improved layout found.") if self.best_result_id else _("First layout found.")
            session._publish(touch=publish_session)
            self.env.cr.commit()

        plan = Job._tp_run_v2_pattern_constructor(
            cuts,
            sources,
            kerf_mm=session.kerf_mm,
            trim_edge_mm=0,
            time_budget_s=budget,
            offcut_sources=offcut_sources,
            progress_callback=publish_progress,
            cancel_callback=cancel_requested,
        )
        if cancel_requested():
            self.write({"state": "cancelled", "progress_pct": 100.0, "message": _("Cancelled.")})
            return
        if not plan or not plan.get("ok"):
            if self.best_result_id:
                self.write(
                    {
                        "state": "failed",
                        "progress_pct": 100.0,
                        "message": _("Optimisation stopped before finishing."),
                        "last_error": _(
                            "Partial layout only. The solver found a first layout, "
                            "but did not finish optimisation, so this result cannot be run."
                        ),
                    }
                )
                session._publish(touch=publish_session)
                return
            reason = ((plan or {}).get("metrics") or {}).get("infeasible_reason") or "unknown"
            self.write(
                {
                    "state": "failed",
                    "progress_pct": 100.0,
                    "last_error": reason,
                    "message": _("Engine could not pack: %s") % reason,
                }
            )
            session._publish(touch=publish_session)
            return

        self._store_result(plan, parts, cuts, Sandbox, event="final")
        self.write({"state": "done", "progress_pct": 100.0, "message": _("Preview complete.")})
        session._publish(touch=publish_session)

    def _store_result(self, plan, parts, cuts, Sandbox, event="best"):
        self.ensure_one()
        Job = self.env["tp.nesting.job"].sudo()
        bins = plan.get("bins") or []
        if not bins or not Job._tp_plan_is_guillotine(plan):
            return False
        try:
            svg = Sandbox._tp_render_svg(bins=bins, cuts=cuts, parts=parts)
        except Exception:
            _logger.exception("Live preview SVG render failed for batch %s", self.id)
            svg = "<div><em>Could not render SVG.</em></div>"
        metrics = dict(plan.get("metrics") or {})
        sheet_count = len(bins)
        panel_count = sum(len(b.get("placements") or []) for b in bins)
        src_area = used_area = 0.0
        fresh_area = 0.0
        fresh_sheet_count = 0
        for bin_data in bins:
            source = bin_data.get("source") or {}
            bin_area = float(source.get("width_mm") or 0) * float(source.get("height_mm") or 0)
            src_area += bin_area
            if not source.get("is_offcut"):
                fresh_area += bin_area
                fresh_sheet_count += 1
            for placement in bin_data.get("placements") or []:
                used_area += float(placement.get("fit_w") or 0) * float(placement.get("fit_h") or 0)
        waste = max(0.0, src_area - used_area)
        util_pct = used_area / src_area * 100.0 if src_area else 0.0
        saw_cuts = Job._tp_count_guillotine_cuts(plan)
        offcut_stats = self._preview_plan_offcut_stats(bins)
        metrics.update(
            {
                "preview_offcut_count": offcut_stats["count"],
                "preview_best_offcut_mm": offcut_stats["best_mm"],
                "preview_best_offcut_area_mm2": offcut_stats["best_area"],
                "preview_generated_offcuts": offcut_stats["generated"],
                "preview_fresh_sheet_count": fresh_sheet_count,
                "preview_fresh_area_mm2": int(round(fresh_area)),
                "preview_waste_area_mm2": int(round(waste)),
                "preview_utilization_pct": round(util_pct, 4),
            }
        )
        rank = [
            round(waste),
            -round(util_pct, 4),
            round(fresh_area),
            fresh_sheet_count,
            offcut_stats["count"],
            -offcut_stats["best_area"],
            saw_cuts,
            sheet_count,
        ]
        if self.best_result_id and self.best_result_id.score_rank_json:
            try:
                old_rank = json.loads(self.best_result_id.score_rank_json)
            except Exception:
                old_rank = None
            if old_rank is not None and len(old_rank) != len(rank):
                old_rank = None
            if old_rank is not None and rank >= old_rank:
                return False

        result = self.env["tp.nesting.preview.result"].create(
            {
                "session_id": self.session_id.id,
                "batch_id": self.id,
                "sequence": (self.session_id.update_sequence or 0) + 1,
                "event": event or "best",
                "plan_json": self._plan_to_json(plan),
                "svg": svg,
                "metrics_json": metrics,
                "score_rank_json": json.dumps(rank),
                "sheet_count": sheet_count,
                "panel_count": panel_count,
                "saw_cut_count": saw_cuts,
                "utilization_pct": util_pct,
                "waste_area_mm2": waste,
            }
        )
        self.best_result_id = result.id
        return result

    def _preview_plan_offcut_stats(self, bins):
        from odoo.addons.tp_sheet_nesting.models.services.tp_guillotine_cuts import (
            offcut_rects,
        )

        best_area = 0
        best_mm = ""
        count = 0
        generated = []
        for sheet_index, bin_data in enumerate(bins, start=1):
            source = bin_data.get("source") or {}
            sheet_w = int(source.get("width_mm") or 0)
            sheet_h = int(source.get("height_mm") or 0)
            if sheet_w <= 0 or sheet_h <= 0:
                continue
            source_is_offcut = bool(source.get("is_offcut"))
            record = source.get("record")
            if source_is_offcut:
                source_label = _("Offcut #%s") % (source.get("offcut_ref") or getattr(record, "offcut_ref", "") or source.get("id"))
            else:
                source_label = (
                    getattr(record, "display_name", "")
                    or source.get("stable_id")
                    or _("Sheet")
                )
            rects = [
                (
                    int(placement.get("x") or 0),
                    int(placement.get("y") or 0),
                    int(placement.get("fit_w") or 0),
                    int(placement.get("fit_h") or 0),
                )
                for placement in (bin_data.get("placements") or [])
            ]
            for _x, _y, width, height in offcut_rects(rects, sheet_w, sheet_h, min_side=80):
                if width <= 0 or height <= 0:
                    continue
                area = int(width) * int(height)
                if not source_is_offcut:
                    count += 1
                if not source_is_offcut and area > best_area:
                    best_area = area
                    best_mm = "%dx%d" % (int(width), int(height))
                if int(width) >= 200 and int(height) >= 200:
                    generated.append(
                        {
                            "key": "%s:%s:%s:%s:%s:%s" % (
                                "offcut" if source_is_offcut else "sheet",
                                source.get("id") or 0,
                                len(generated),
                                int(_x),
                                int(_y),
                                int(width) * int(height),
                            ),
                            "sheetIndex": sheet_index,
                            "sourceType": "offcut" if source_is_offcut else "sheet",
                            "sourceLabel": source_label,
                            "widthMm": int(width),
                            "heightMm": int(height),
                            "areaM2": round(area / 1_000_000.0, 4),
                            "x": int(_x),
                            "y": int(_y),
                            "kindLabel": _("Generated"),
                        }
                    )
        generated.sort(key=lambda item: (-int(item["widthMm"]) * int(item["heightMm"]), item["sourceLabel"], item["x"], item["y"]))
        for idx, item in enumerate(generated, start=1):
            item["displayRef"] = _("Offcut %d") % idx
        return {"count": count, "best_area": best_area, "best_mm": best_mm, "generated": generated}

    def _commit_best_result(self):
        self.ensure_one()
        result = self.best_result_id
        if not result:
            raise UserError(_("No preview result exists for %s.") % self.group_label)
        parts = self.part_ids.filtered("active")
        self._assert_snapshot_current(parts)
        Job = self.env["tp.nesting.job"].sudo()
        Run = self.env["tp.nesting.run"].sudo()
        Sandbox = self.env["tp.nesting.sandbox"].sudo()
        Wizard = self.env["tp.nesting.run.wizard"].sudo()
        plan = self._plan_from_json(result.plan_json or {})
        bins = plan.get("bins") or []
        if not bins:
            raise UserError(_("Stored preview result for %s has no sheet bins.") % self.group_label)
        if not Job._tp_plan_is_guillotine(plan):
            raise UserError(_("Stored preview result for %s is no longer guillotine-valid.") % self.group_label)
        Job._tp_run_assert_offcuts_available(bins, run=False)

        kerf_mm = int(self.session_id.kerf_mm or 0)
        sheets_needed = sum(1 for bin_data in bins if not (bin_data.get("source") or {}).get("is_offcut"))
        panel_count = sum(len(bin_data.get("placements") or []) for bin_data in bins)
        run_name = "NEST-%s-%s" % (
            Wizard._tp_slug(self.group_label or self.source_product_id.display_name),
            fields.Datetime.now().strftime("%Y%m%d%H%M%S"),
        )
        run = Run.create(
            {
                "name": run_name,
                "mo_id": False,
                "kerf_mm": kerf_mm,
                "trim_edge_mm": 0,
                "rotation_mode": "free",
                "engine_mode": "deterministic",
                "state": "done",
                "note": self.group_label or False,
                "x_tp_source_product_id": self.source_product_id.id,
                "x_tp_sheets_needed": sheets_needed,
                "x_tp_panel_count": panel_count,
            }
        )
        Job._tp_run_create_allocations(run, bins, parts)
        Job._tp_run_materialize_generated_offcuts(run, plan)
        Job._tp_run_finalize_metrics(run, plan)

        try:
            stub_job_id = Wizard._tp_find_stub_job(parts)
            if stub_job_id:
                first_source_id = False
                for bin_data in bins:
                    source = bin_data.get("source") or {}
                    if not source.get("is_offcut") and source.get("id"):
                        first_source_id = source.get("id")
                        break
                if first_source_id:
                    stub = Sandbox.create({"job_id": stub_job_id, "sheet_format_id": first_source_id})
                    dxf_bytes, _dxf_name = stub._tp_build_dxf(bins=bins, parts=parts)
                    stub.unlink()
                else:
                    dxf_bytes = b""
            else:
                dxf_bytes = b""
        except Exception:
            _logger.exception("DXF build failed for run %s", run.name)
            dxf_bytes = b""
        if dxf_bytes:
            run.write(
                {
                    "x_tp_dxf_bytes": base64.b64encode(dxf_bytes),
                    "x_tp_dxf_filename": "%s.zip" % run.name,
                }
            )
        return run

    def _identity(self):
        self.ensure_one()
        return {
            "tp_material_type": self.material_type or "",
            "tp_thickness_mm": self.thickness_mm or 0.0,
            "tp_colour": self.colour or "",
            "tp_finish": self.finish or "",
            "tp_protective_film": self.protective_film or "",
            "tp_brand_supplier": self.brand_supplier or "",
        }

    def _parts_snapshot(self, parts):
        return [
            {
                "id": part.id,
                "width_mm": int(part.width_mm or 0),
                "height_mm": int(part.height_mm or 0),
                "quantity": int(part.quantity or 0),
                "active": bool(part.active),
                "status": getattr(part, "sale_order_fulfillment_status", False) or "",
            }
            for part in parts.sorted("id")
        ]

    def _assert_snapshot_current(self, parts):
        self.ensure_one()
        expected = self.part_snapshot_json or []
        current = self._parts_snapshot(parts)
        if expected != current:
            raise UserError(_("%s changed after preview. Refresh and preview again.") % self.group_label)

    def _plan_to_json(self, plan):
        bins = []
        for bin_data in (plan.get("bins") or []):
            source = dict(bin_data.get("source") or {})
            clean_source = {}
            for key in (
                "kind",
                "stable_id",
                "id",
                "product_id",
                "lot_id",
                "width_mm",
                "height_mm",
                "area_mm2",
                "unit_cost",
                "effective_cost_per_area",
                "is_offcut",
                "offcut_ref",
            ):
                if key in source:
                    clean_source[key] = source.get(key)
            placements = []
            for placement in bin_data.get("placements") or []:
                clean = {}
                for key in (
                    "x",
                    "y",
                    "fit_w",
                    "fit_h",
                    "used_w",
                    "used_h",
                    "rotated",
                    "kernel",
                    "pattern_strategy",
                ):
                    if key in placement:
                        clean[key] = placement.get(key)
                clean["cut"] = dict(placement.get("cut") or {})
                placements.append(clean)
            bins.append(
                {
                    "source": clean_source,
                    "placements": placements,
                    "free_rects": list(bin_data.get("free_rects") or []),
                    "pattern_strategy": bin_data.get("pattern_strategy") or "",
                }
            )
        return {"ok": bool(plan.get("ok")), "bins": bins, "metrics": dict(plan.get("metrics") or {})}

    def _plan_from_json(self, payload):
        Format = self.env["tp.sheet.format"].sudo()
        Offcut = self.env["tp.offcut"].sudo()
        plan = {"ok": bool(payload.get("ok", True)), "bins": [], "metrics": dict(payload.get("metrics") or {})}
        for bin_data in payload.get("bins") or []:
            source = dict(bin_data.get("source") or {})
            if source.get("is_offcut"):
                rec = Offcut.browse(source.get("id"))
            else:
                rec = Format.browse(source.get("id"))
            if rec and rec.exists():
                source["record"] = rec
            plan["bins"].append(
                {
                    "source": source,
                    "placements": list(bin_data.get("placements") or []),
                    "free_rects": list(bin_data.get("free_rects") or []),
                    "pattern_strategy": bin_data.get("pattern_strategy") or "",
                }
            )
        return plan

    def _app_state(self):
        self.ensure_one()
        result = self.best_result_id
        source_pool = self._source_pool_details()
        part_rows = []
        if (self.group_key or "").startswith(("manual_csv:", "csv_only:")):
            for part in self.with_context(active_test=False).part_ids.sorted(lambda item: (item.id,)):
                part_rows.append(
                    {
                        "id": part.id,
                        "batchId": self.id,
                        "label": part.label or "",
                        "lengthMm": int(part.height_mm or 0),
                        "widthMm": int(part.width_mm or 0),
                        "quantity": int(part.quantity or 1),
                        "active": bool(part.active),
                    }
                )
        return {
            "id": self.id,
            "sequence": self.sequence,
            "groupKey": self.group_key or "",
            "selected": bool(self.selected),
            "state": self.state,
            "label": self.group_label,
            "panelCount": self.panel_count,
            "panelQuantity": self.panel_quantity,
            "cncPanelCount": self.cnc_panel_count,
            "sourceProductId": self.source_product_id.id or False,
            "sourceProductName": self._material_label(),
            "sourcePoolSummary": source_pool["summary"],
            "offcuts": source_pool["offcuts"],
            "candidateSources": self.session_id._material_options(self.candidate_source_product_ids),
            "warning": self.warning or "",
            "progressPct": self.progress_pct,
            "message": self.message or "",
            "lastError": self.last_error or "",
            "partRows": part_rows,
            "bestResult": result._app_state() if result else False,
        }

    def _material_label(self):
        self.ensure_one()
        Wizard = self.env["tp.nesting.run.wizard"].sudo()
        return Wizard._tp_group_label(self._identity())

    def _source_pool_summary(self):
        return self._source_pool_details()["summary"]

    def _source_pool_details(self):
        self.ensure_one()
        if not self.source_product_id:
            return {"summary": "", "offcuts": []}
        Wizard = self.env["tp.nesting.run.wizard"].sudo()
        try:
            sources, offcut_sources = Wizard._tp_build_engine_sources(self.source_product_id, self._identity())
        except Exception:
            return {"summary": "", "offcuts": []}
        sizes = sorted({
            (int(source.get("width_mm") or 0), int(source.get("height_mm") or 0))
            for source in sources
            if int(source.get("width_mm") or 0) and int(source.get("height_mm") or 0)
        })
        size_text = ", ".join("%sx%s" % size for size in sizes[:3])
        if len(sizes) > 3:
            size_text += " +%d" % (len(sizes) - 3)
        parts = []
        if size_text:
            parts.append(_("%(count)d sheet size(s): %(sizes)s") % {"count": len(sizes), "sizes": size_text})
        if offcut_sources:
            parts.append(_("%d offcut(s)") % len(offcut_sources))
        state_labels = dict(self.env["tp.offcut"]._fields["state"].selection)
        offcuts = []
        for source in offcut_sources:
            offcut = source.get("record")
            width = int(source.get("width_mm") or (offcut.width_mm if offcut else 0) or 0)
            height = int(source.get("height_mm") or (offcut.height_mm if offcut else 0) or 0)
            area = width * height
            ref_value = source.get("offcut_ref") or (offcut.offcut_ref if offcut else 0)
            state = (offcut.state if offcut else "") or ""
            offcuts.append(
                {
                    "id": int(source.get("id") or (offcut.id if offcut else 0) or 0),
                    "ref": "#%s" % ref_value if ref_value else _("Offcut"),
                    "name": offcut.display_name if offcut else source.get("stable_id") or "",
                    "widthMm": width,
                    "heightMm": height,
                    "areaM2": area / 1_000_000.0,
                    "state": state,
                    "stateLabel": state_labels.get(state, state or _("Available")),
                }
            )
        offcuts.sort(key=lambda item: (-float(item["areaM2"] or 0.0), item["widthMm"], item["heightMm"], item["id"]))
        return {"summary": " | ".join(parts), "offcuts": offcuts}


class TpNestingPreviewResult(models.Model):
    _name = "tp.nesting.preview.result"
    _description = "Live Nesting Preview Result"
    _order = "id desc"

    session_id = fields.Many2one("tp.nesting.preview.session", required=True, ondelete="cascade")
    batch_id = fields.Many2one("tp.nesting.preview.batch", required=True, ondelete="cascade")
    sequence = fields.Integer(default=0)
    event = fields.Char()
    plan_json = fields.Json()
    svg = fields.Html(sanitize=False)
    metrics_json = fields.Json()
    score_rank_json = fields.Char()
    sheet_count = fields.Integer()
    panel_count = fields.Integer()
    saw_cut_count = fields.Integer()
    utilization_pct = fields.Float()
    waste_area_mm2 = fields.Float()

    def _app_state(self):
        self.ensure_one()
        return {
            "id": self.id,
            "event": self.event or "",
            "sequence": self.sequence,
            "svg": self.svg or "",
            "sheetCount": self.sheet_count,
            "panelCount": self.panel_count,
            "sawCuts": self.saw_cut_count,
            "utilizationPct": self.utilization_pct,
            "wasteM2": (self.waste_area_mm2 or 0.0) / 1_000_000.0,
            "generatedOffcuts": (self.metrics_json or {}).get("preview_generated_offcuts") or [],
            "metrics": self.metrics_json or {},
        }
