import json
from html import escape

from odoo import fields, models
from odoo.exceptions import ValidationError

from .services.tp_2d_nesting_engine import Tp2DNestingEngine
from .services.tp_nesting_source_pool import TpNestingSourcePool


class MrpProductionNestingExecution(models.Model):
    _inherit = "mrp.production"

    def _tp_requires_nesting_before_produce(self):
        self.ensure_one()
        if self.x_tp_source_so_line_id:
            return True
        return bool(self.tp_cut_line_ids)

    def _tp_validate_nesting_before_produce(self):
        blocking_mos = self.filtered(
            lambda mo: mo.state not in ("done", "cancel")
            and mo._tp_requires_nesting_before_produce()
            and (not mo.tp_last_nesting_run_id or mo.tp_last_nesting_run_id.state != "done")
        )
        if not blocking_mos:
            return
        mo_labels = ", ".join(blocking_mos.mapped("name"))
        raise ValidationError(
            "Run Nesting and complete it successfully before Produce All. "
            f"Blocked MO(s): {mo_labels}"
        )

    def _tp_build_sheet_requirements_from_run(self, run):
        run.ensure_one()
        unique_source_lots = {}
        for alloc in run.allocation_ids.filtered(
            lambda a: a.source_type in ("sheet", "offcut") and a.source_lot_id and a.source_lot_id.product_id
        ):
            unique_source_lots.setdefault(alloc.source_lot_id.id, alloc.source_lot_id)

        requirements_by_product = {}
        for source_lot in unique_source_lots.values():
            product = source_lot.product_id
            bucket = requirements_by_product.setdefault(
                product.id,
                {
                    "product": product,
                    "qty": 0.0,
                    "lot_ids": [],
                },
            )
            bucket["qty"] += 1.0
            if product.tracking == "lot":
                bucket["lot_ids"].append(source_lot.id)
        return sorted(requirements_by_product.values(), key=lambda r: r["product"].id)

    def _tp_create_raw_move_for_requirement(self, mo, requirement):
        product = requirement["product"]
        return self.env["stock.move"].create(
            {
                "product_id": product.id,
                "product_uom": product.uom_id.id,
                "product_uom_qty": 0.0,
                "location_id": mo.location_src_id.id,
                "location_dest_id": mo.production_location_id.id,
                "company_id": mo.company_id.id,
                "raw_material_production_id": mo.id,
                "picking_type_id": mo.picking_type_id.id,
                "procure_method": "make_to_stock",
            }
        )

    def _tp_apply_requirement_to_raw_move(self, move, requirement):
        product = requirement["product"]
        qty = float(requirement["qty"] or 0.0)
        lot_ids = list(dict.fromkeys(requirement["lot_ids"]))
        if product.tracking == "lot" and int(round(qty)) != len(lot_ids):
            raise ValidationError(
                f"Nesting-selected lots do not match quantity for tracked sheet SKU {product.display_name}."
            )

        move.write(
            {
                "product_id": product.id,
                "product_uom": product.uom_id.id,
                "product_uom_qty": qty,
            }
        )
        # Writing move quantity in assigned state can auto-generate move lines.
        # Rebuild explicitly so consumption lines always match nesting output.
        move.move_line_ids.unlink()
        if qty <= 0:
            return

        if product.tracking == "lot":
            for lot_id in lot_ids:
                self.env["stock.move.line"].create(
                    {
                        "move_id": move.id,
                        "product_id": product.id,
                        "lot_id": lot_id,
                        "quantity": 1.0,
                        "quantity_product_uom": 1.0,
                        "location_id": move.location_id.id,
                        "location_dest_id": move.location_dest_id.id,
                        "company_id": move.company_id.id,
                    }
                )
            return

        self.env["stock.move.line"].create(
            {
                "move_id": move.id,
                "product_id": product.id,
                "quantity": qty,
                "quantity_product_uom": qty,
                "location_id": move.location_id.id,
                "location_dest_id": move.location_dest_id.id,
                "company_id": move.company_id.id,
            }
        )

    def _tp_sync_raw_moves_from_nesting(self):
        for mo in self:
            run = mo.tp_last_nesting_run_id
            if not run or run.state != "done":
                continue
            requirements = mo._tp_build_sheet_requirements_from_run(run)
            raw_moves = mo.move_raw_ids.filtered(lambda m: m.state not in ("done", "cancel")).sorted("id")
            if raw_moves:
                raw_moves._do_unreserve()
                raw_moves.mapped("move_line_ids").unlink()

            assignable_moves = list(raw_moves)
            while len(assignable_moves) < len(requirements):
                new_move = mo._tp_create_raw_move_for_requirement(
                    mo,
                    requirements[len(assignable_moves)],
                )
                assignable_moves.append(new_move)

            for idx, requirement in enumerate(requirements):
                mo._tp_apply_requirement_to_raw_move(assignable_moves[idx], requirement)

            for move in assignable_moves[len(requirements) :]:
                move.move_line_ids.unlink()
                move.write({"product_uom_qty": 0.0})

    def _tp_plan_remainder(
        self,
        *,
        run,
        planned_kind,
        planned_source_type,
        product,
        width_mm,
        height_mm,
        parent_lot=False,
        parent_offcut=False,
        parent_remaining_area_mm2=0.0,
        parent_remaining_value=0.0,
        kerf_mm=3,
    ):
        return self.env["tp.nesting.produced.offcut"].create(
            {
                "run_id": run.id,
                "planned_kind": planned_kind,
                "planned_source_type": planned_source_type,
                "product_id": product.id,
                "parent_lot_id": parent_lot.id if parent_lot else False,
                "parent_offcut_id": parent_offcut.id if parent_offcut else False,
                "planned_width_mm": int(width_mm),
                "planned_height_mm": int(height_mm),
                "kerf_mm": int(kerf_mm or 3),
                "parent_remaining_area_mm2": float(parent_remaining_area_mm2 or 0.0),
                "parent_remaining_value": float(parent_remaining_value or 0.0),
                "currency_id": self.env.company.currency_id.id,
            }
        )

    def _tp_process_sheet_remainder_values(
        self, *, run, product, source_lot, parent_width_mm, parent_height_mm, landed_cost, rem_w, rem_h, name_suffix=""
    ):
        if rem_w <= 0 or rem_h <= 0:
            return
        parent_lot = source_lot
        suffix = f"-{name_suffix}" if name_suffix else ""
        if not parent_lot:
            parent_lot = self.env["stock.lot"].create(
                {
                    "name": f"SHEET-PARENT-{run.id}{suffix}",
                    "product_id": product.id,
                    "company_id": self.env.company.id,
                }
            )
        parent_area = float(parent_width_mm * parent_height_mm)
        parent_value = float(landed_cost or product.standard_price or 0.0)
        planned_kind = "offcut" if rem_w >= 200 and rem_h >= 200 else "waste"
        self._tp_plan_remainder(
            run=run,
            planned_kind=planned_kind,
            planned_source_type="sheet",
            product=product,
            width_mm=int(rem_w),
            height_mm=int(rem_h),
            parent_lot=parent_lot,
            parent_remaining_area_mm2=parent_area,
            parent_remaining_value=parent_value,
            kerf_mm=3,
        )

    def _tp_process_sheet_remainder(self, run, sheet, rem_w, rem_h):
        return self._tp_process_sheet_remainder_values(
            run=run,
            product=sheet.product_id,
            source_lot=False,
            parent_width_mm=sheet.width_mm,
            parent_height_mm=sheet.height_mm,
            landed_cost=sheet.landed_cost,
            rem_w=rem_w,
            rem_h=rem_h,
        )

    def _tp_materialize_sheet_plan(self, *, run, planned):
        offcut_model = self.env["tp.offcut"]
        if not planned.parent_lot_id:
            raise ValidationError("Cannot materialize sheet remainder without a parent lot.")

        if planned.planned_kind == "offcut":
            offcut = offcut_model.create_offcut_from_sheet(
                lot_id=False,
                parent_lot_id=planned.parent_lot_id.id,
                width_mm=int(planned.planned_width_mm),
                height_mm=int(planned.planned_height_mm),
                parent_remaining_area_mm2=float(planned.parent_remaining_area_mm2),
                parent_remaining_value=float(planned.parent_remaining_value),
                mo_id=run.mo_id.id,
                run_id=run.id,
                name=False,
            )
            planned.write(
                {
                    "offcut_id": offcut.id,
                    "state": "materialized",
                    "materialized_at": fields.Datetime.now(),
                }
            )
            return

        parent_area = float(planned.parent_remaining_area_mm2)
        if parent_area <= 0:
            raise ValidationError("Cannot materialize planned sheet waste with zero parent area.")
        parent_value = float(planned.parent_remaining_value)
        waste_area = float(planned.planned_width_mm * planned.planned_height_mm)
        waste_value = offcut_model._compute_area_value(parent_value, parent_area, waste_area)
        is_conserved, delta = offcut_model._assert_value_conservation(
            parent_value, [waste_value, parent_value - waste_value]
        )
        event = self.env["tp.offcut.valuation.event"].create(
            {
                "event_type": "waste",
                "parent_lot_id": planned.parent_lot_id.id,
                "mo_id": run.mo_id.id,
                "input_area_mm2": parent_area,
                "input_value": parent_value,
                "waste_area_mm2": waste_area,
                "waste_value": waste_value,
                "remainder_area_mm2": parent_area - waste_area,
                "remainder_value": parent_value - waste_value,
                "is_conserved": is_conserved,
                "conservation_delta": delta,
                "currency_id": planned.currency_id.id,
            }
        )
        waste = self.env["tp.offcut.waste"].create(
            {
                "name": f"WASTE-SHEET-{run.id}",
                "mo_id": run.mo_id.id,
                "parent_source_type": "sheet",
                "parent_lot_id": planned.parent_lot_id.id,
                "product_id": planned.product_id.id,
                "width_mm": int(planned.planned_width_mm),
                "height_mm": int(planned.planned_height_mm),
                "kerf_mm": int(planned.kerf_mm or 3),
                "waste_value": waste_value,
                "currency_id": planned.currency_id.id,
                "valuation_event_id": event.id,
            }
        )
        planned.write(
            {
                "waste_id": waste.id,
                "state": "materialized",
                "materialized_at": fields.Datetime.now(),
            }
        )

    def _tp_materialize_offcut_plan(self, *, run, planned):
        parent_offcut = planned.parent_offcut_id
        if not parent_offcut:
            raise ValidationError("Cannot materialize offcut remainder without a parent offcut.")
        result = parent_offcut.record_remainder(
            width_mm=int(planned.planned_width_mm),
            height_mm=int(planned.planned_height_mm),
            mo_id=run.mo_id.id,
            run_id=run.id,
            kerf_mm=int(planned.kerf_mm or 3),
            name=False,
        )
        vals = {
            "state": "materialized",
            "materialized_at": fields.Datetime.now(),
        }
        if result._name == "tp.offcut":
            vals["offcut_id"] = result.id
        else:
            vals["waste_id"] = result.id
        planned.write(vals)

    def _tp_materialize_run_outputs(self, run):
        run.ensure_one()
        if run.outputs_materialized:
            return
        if run.state != "done":
            return

        planned_rows = run.produced_offcut_ids.filtered(
            lambda r: r.state == "planned" and not (r.offcut_id or r.waste_id)
        ).sorted("id")
        consumed_offcuts = run.allocation_ids.filtered(
            lambda a: a.source_type == "offcut" and a.source_offcut_id
        ).mapped("source_offcut_id")
        for planned in planned_rows:
            if planned.planned_source_type == "sheet":
                self._tp_materialize_sheet_plan(run=run, planned=planned)
            else:
                self._tp_materialize_offcut_plan(run=run, planned=planned)

        for offcut in consumed_offcuts:
            trace_vals = {}
            if "consumed_in_run_id" in offcut._fields:
                trace_vals["consumed_in_run_id"] = run.id
            if "consumed_at" in offcut._fields:
                trace_vals["consumed_at"] = fields.Datetime.now()
            if trace_vals:
                offcut.write(trace_vals)
            if offcut.state != "inactive":
                offcut.action_archive()

        run.write(
            {
                "outputs_materialized": True,
                "outputs_materialized_at": fields.Datetime.now(),
            }
        )

    def _tp_materialize_last_nesting_outputs(self):
        for mo in self:
            run = mo.tp_last_nesting_run_id
            if not run:
                continue
            mo._tp_materialize_run_outputs(run)

    def _tp_select_unbuild_lot(self):
        self.ensure_one()
        lot = self.lot_producing_ids[:1]
        if lot:
            return lot
        finished_lines = self.move_finished_ids.filtered(lambda m: m.state == "done").mapped("move_line_ids")
        finished_lots = finished_lines.filtered(
            lambda ml: ml.product_id == self.product_id and float(ml.quantity or 0.0) > 0 and ml.lot_id
        ).mapped("lot_id")
        return finished_lots[:1]

    def _tp_create_unbuild_for_done_mo(self):
        self.ensure_one()
        if self.state != "done":
            raise ValidationError("Undo Produce All is only available after the MO is done.")

        qty = float(self.qty_produced or self.product_qty or 0.0)
        if qty <= 0:
            raise ValidationError("Cannot undo production for an MO with zero produced quantity.")

        vals = {
            "mo_id": self.id,
            "company_id": self.company_id.id,
            "product_id": self.product_id.id,
            "product_qty": qty,
            "product_uom_id": self.product_uom_id.id,
            "location_id": self.location_dest_id.id,
            "location_dest_id": self.location_src_id.id,
        }
        if self.product_id.tracking != "none":
            lot = self._tp_select_unbuild_lot()
            if not lot:
                raise ValidationError(
                    "Cannot undo Produce All because no finished lot/serial is available on the MO."
                )
            vals["lot_id"] = lot.id

        unbuild = self.env["mrp.unbuild"].with_context(tp_allow_nesting_unbuild=True).create(vals)
        unbuild.with_context(tp_allow_nesting_unbuild=True).action_unbuild()
        return unbuild

    def _tp_reverse_waste_account_moves(self, waste_records):
        for waste in waste_records.filtered("account_move_id"):
            move = waste.account_move_id
            if move.state != "posted":
                continue
            reversal = move._reverse_moves(
                default_values_list=[{"date": fields.Date.context_today(self)}],
                cancel=False,
            )
            reversal.filtered(lambda m: m.state == "draft").action_post()

    def _tp_validate_produced_offcuts_not_reused(self, offcuts):
        offcuts = offcuts.exists()
        if not offcuts:
            return

        active_children = self.env["tp.offcut"].with_context(active_test=False).search(
            [("parent_offcut_id", "in", offcuts.ids)],
            limit=1,
        )
        if active_children:
            raise ValidationError(
                "Cannot undo Produce All because produced offcuts were already reused by another cut."
            )

        bad_state = offcuts.filtered(lambda o: o.state not in ("available", "inactive"))
        if bad_state:
            labels = ", ".join(bad_state.mapped("name"))
            raise ValidationError(
                "Cannot undo Produce All because produced offcuts are no longer in an undo-safe state. "
                f"Affected: {labels}"
            )

        lot_ids = offcuts.mapped("lot_id").ids
        if not lot_ids:
            return
        has_quant = self.env["stock.quant"].search_count(
            [("lot_id", "in", lot_ids), "|", ("quantity", "!=", 0.0), ("reserved_quantity", "!=", 0.0)]
        )
        if has_quant:
            raise ValidationError(
                "Cannot undo Produce All because produced offcut lots already have stock/reservations."
            )

    def _tp_safe_unlink_valuation_events(self, events):
        events = events.exists()
        if not events:
            return
        offcut_links = self.env["tp.offcut"].with_context(active_test=False).search(
            [("valuation_reference", "in", events.ids)]
        )
        waste_links = self.env["tp.offcut.waste"].with_context(active_test=False).search(
            [("valuation_event_id", "in", events.ids)]
        )
        linked_ids = set(offcut_links.mapped("valuation_reference").ids + waste_links.mapped("valuation_event_id").ids)
        removable = events.filtered(lambda event: event.id not in linked_ids)
        if removable:
            removable.unlink()

    def _tp_rollback_run_materialization(self, run):
        run.ensure_one()
        if not run.outputs_materialized:
            return

        planned_rows = run.produced_offcut_ids.filtered(
            lambda row: row.state == "materialized" and (row.offcut_id or row.waste_id)
        )
        if not planned_rows:
            run.write({"outputs_materialized": False, "outputs_materialized_at": False})
            return

        produced_offcuts = planned_rows.mapped("offcut_id").exists()
        waste_records = planned_rows.mapped("waste_id").exists()
        self._tp_validate_produced_offcuts_not_reused(produced_offcuts)
        self._tp_reverse_waste_account_moves(waste_records)

        parent_restore = {}
        for row in planned_rows.filtered(lambda r: r.planned_source_type == "offcut" and r.parent_offcut_id):
            parent_restore[row.parent_offcut_id.id] = (
                float(row.parent_remaining_area_mm2 or 0.0),
                float(row.parent_remaining_value or 0.0),
            )

        consumed_offcuts = run.allocation_ids.filtered(
            lambda alloc: alloc.source_type == "offcut" and alloc.source_offcut_id
        ).mapped("source_offcut_id")
        for offcut in consumed_offcuts:
            vals = {
                "active": True,
                "state": "reserved",
                "reserved_mo_id": run.mo_id.id,
            }
            if "reservation_run_id" in offcut._fields:
                vals["reservation_run_id"] = run.id
            if "consumed_in_run_id" in offcut._fields:
                vals["consumed_in_run_id"] = False
            if "consumed_at" in offcut._fields:
                vals["consumed_at"] = False
            if offcut.id in parent_restore:
                area, value = parent_restore[offcut.id]
                vals["remaining_area_mm2"] = area
                vals["remaining_value"] = value
            offcut.write(vals)

        valuation_events = (waste_records.mapped("valuation_event_id") | produced_offcuts.mapped("valuation_reference")).exists()
        if waste_records:
            waste_records.unlink()
        if produced_offcuts:
            produced_offcuts.unlink()
        self._tp_safe_unlink_valuation_events(valuation_events)

        planned_rows.write(
            {
                "offcut_id": False,
                "waste_id": False,
                "state": "planned",
                "materialized_at": False,
            }
        )
        run.write({"outputs_materialized": False, "outputs_materialized_at": False})

    def action_tp_undo_produce_all(self):
        for mo in self:
            if mo.state != "done":
                raise ValidationError("Undo Produce All is only available when the MO is done.")
            run = mo.tp_last_nesting_run_id
            if not run or run.state != "done":
                raise ValidationError("Undo Produce All requires a successful nesting run on the MO.")
            if not run.outputs_materialized:
                raise ValidationError("Nothing to undo: nesting outputs were not materialized yet.")
            unbuild = mo._tp_create_unbuild_for_done_mo()
            mo._tp_rollback_run_materialization(run)
            mo.message_post(
                body=(
                    "Produce All was reversed via automatic unbuild "
                    f"{unbuild.display_name} and nesting outputs were rolled back."
                ),
                subtype_xmlid="mail.mt_note",
            )
        return True

    def _tp_mark_prior_runs_superseded(self, scope_mos):
        prior_runs = self.env["tp.nesting.run"].search(
            [("mo_id", "in", scope_mos.ids), ("state", "in", ["draft", "done"])]
        )
        for run in prior_runs:
            note = run.note or ""
            suffix = "Superseded by a newer re-run."
            run.write({"note": f"{note}\n{suffix}".strip()})

    def _tp_resolve_sale_order_for_scope(self, scope_mos):
        sale_orders = scope_mos.mapped("x_tp_source_so_line_id.order_id")
        if len(sale_orders) == 1:
            return sale_orders
        if len(sale_orders) > 1:
            return sale_orders.sorted("id")[:1]
        origin_values = [o for o in scope_mos.mapped("origin") if o]
        if not origin_values:
            return self.env["sale.order"]
        origin = origin_values[0]
        return self.env["sale.order"].search([("name", "=", origin)], limit=1)

    def _tp_create_job_for_run(self, run, scope_mos):
        sale_order = self._tp_resolve_sale_order_for_scope(scope_mos)
        if not sale_order:
            return self.env["tp.nesting.job"]
        demand_product = scope_mos[0].product_id
        job = self.env["tp.nesting.job"].search(
            [
                ("sale_order_id", "=", sale_order.id),
                ("demand_product_id", "=", demand_product.id),
            ],
            limit=1,
            order="id desc",
        )
        if not job:
            seq = self.env["ir.sequence"].next_by_code("tp.nesting.job") or "JOB"
            job = self.env["tp.nesting.job"].create(
                {
                    "name": seq,
                    "sale_order_id": sale_order.id,
                    "demand_product_id": demand_product.id,
                    "note": f"Nesting job for SO {sale_order.name} / {demand_product.display_name}",
                }
            )
        run.job_id = job.id
        job.last_run_id = run.id
        return job

    def _tp_capture_run_outputs(self, run):
        run.ensure_one()
        self.env["tp.nesting.consumed.lot"].search([("run_id", "=", run.id)]).unlink()
        self.env["tp.nesting.produced.panel"].search([("run_id", "=", run.id)]).unlink()

        consumed_by_lot = {}
        for alloc in run.allocation_ids.filtered(lambda a: a.source_lot_id):
            source_lot = alloc.source_lot_id
            if alloc.source_type == "offcut" and alloc.source_offcut_id and alloc.source_offcut_id.lot_id:
                source_lot = alloc.source_offcut_id.lot_id
            key = (source_lot.id, alloc.source_type)
            consumed_by_lot[key] = consumed_by_lot.get(key, 0) + int(alloc.cut_quantity or 0)

            self.env["tp.nesting.produced.panel"].create(
                {
                    "run_id": run.id,
                    "allocation_id": alloc.id,
                    "source_lot_id": source_lot.id,
                    "width_mm": alloc.cut_width_mm,
                    "height_mm": alloc.cut_height_mm,
                    "quantity": int(alloc.cut_quantity or 1),
                }
            )

        for (lot_id, source_type), allocation_count in consumed_by_lot.items():
            self.env["tp.nesting.consumed.lot"].create(
                {
                    "run_id": run.id,
                    "lot_id": lot_id,
                    "source_type": source_type,
                    "allocation_count": allocation_count,
                }
            )

    @staticmethod
    def _tp_source_dims_from_allocation(allocation):
        if allocation.source_type == "offcut" and allocation.source_offcut_id:
            return allocation.source_offcut_id.width_mm, allocation.source_offcut_id.height_mm
        if allocation.source_sheet_format_id:
            return allocation.source_sheet_format_id.width_mm, allocation.source_sheet_format_id.height_mm
        if allocation.source_lot_id:
            return allocation.source_lot_id.tp_width_mm, allocation.source_lot_id.tp_height_mm
        return allocation.cut_width_mm + 3, allocation.cut_height_mm + 3

    def _tp_build_nesting_svg(self, run):
        run.ensure_one()
        allocations = run.allocation_ids.sorted("id")
        if not allocations:
            return "<div>No nesting allocations to display.</div>"

        def _source_group_key(alloc):
            if alloc.source_bin_key:
                return str(alloc.source_bin_key)
            if alloc.source_type == "offcut" and alloc.source_offcut_id:
                return f"offcut:{alloc.source_offcut_id.id}"
            if alloc.source_lot_id:
                return f"lot:{alloc.source_lot_id.id}"
            if alloc.source_sheet_format_id:
                return f"sheetfmt:{alloc.source_sheet_format_id.id}"
            return f"alloc:{alloc.id}"

        def _source_group_meta(alloc):
            src_w, src_h = self._tp_source_dims_from_allocation(alloc)
            src_w = max(int(src_w or 1), 1)
            src_h = max(int(src_h or 1), 1)
            if alloc.source_bin_label:
                label = alloc.source_bin_label
            elif alloc.source_type == "offcut" and alloc.source_offcut_id:
                label = alloc.source_offcut_id.name or "Offcut"
            elif alloc.source_lot_id:
                label = alloc.source_lot_id.name or "Sheet Lot"
            elif alloc.source_sheet_format_id:
                label = alloc.source_sheet_format_id.name or "Sheet Format"
            else:
                label = "Source"
            return src_w, src_h, label

        grouped = []
        groups = {}
        for alloc in allocations:
            key = _source_group_key(alloc)
            if key not in groups:
                src_w, src_h, label = _source_group_meta(alloc)
                groups[key] = {
                    "allocs": [],
                    "key": key,
                    "src_w": src_w,
                    "src_h": src_h,
                    "label": label,
                    "source_type": alloc.source_type or "sheet",
                }
                grouped.append(groups[key])
            groups[key]["allocs"].append(alloc)

        cols = 3
        tile_w = 300
        tile_h = 220
        pad = 16
        rows = (len(grouped) + cols - 1) // cols
        svg_w = (cols * tile_w) + ((cols + 1) * pad)
        svg_h = (rows * tile_h) + ((rows + 1) * pad)
        parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{svg_w}" height="{svg_h}" viewBox="0 0 {svg_w} {svg_h}">',
            '<rect x="0" y="0" width="100%" height="100%" fill="#f8fafc"/>',
        ]

        palette = ["#2563eb", "#16a34a", "#f59e0b", "#dc2626", "#0ea5e9", "#7c3aed", "#ea580c"]
        for idx, group in enumerate(grouped):
            col = idx % cols
            row = idx // cols
            x0 = pad + col * (tile_w + pad)
            y0 = pad + row * (tile_h + pad)
            src_w = group["src_w"]
            src_h = group["src_h"]
            source_label = group["label"]
            cut_count = sum(max(int(a.cut_quantity or 1), 1) for a in group["allocs"])

            inner_w = tile_w - 24
            inner_h = tile_h - 64
            scale = min(float(inner_w) / float(src_w), float(inner_h) / float(src_h))
            src_px_w = max(int(src_w * scale), 1)
            src_px_h = max(int(src_h * scale), 1)
            rect_x = x0 + 12 + int((inner_w - src_px_w) / 2)
            rect_y = y0 + 42 + int((inner_h - src_px_h) / 2)

            parts.extend(
                [
                    f'<rect x="{x0}" y="{y0}" width="{tile_w}" height="{tile_h}" rx="10" fill="#ffffff" stroke="#dbe1ea"/>',
                    f'<text x="{x0 + 10}" y="{y0 + 22}" font-size="12" font-family="Arial, sans-serif" fill="#1f2937">'
                    f'{escape((group["source_type"] or "").upper())} - {escape(source_label)} ({cut_count} cut(s))</text>',
                    f'<text x="{x0 + 10}" y="{y0 + 36}" font-size="10" font-family="Arial, sans-serif" fill="#475569">'
                    f'Bin: {escape(group["key"])}</text>',
                    f'<rect x="{rect_x}" y="{rect_y}" width="{src_px_w}" height="{src_px_h}" fill="#e5e7eb" stroke="#94a3b8"/>',
                    f'<text x="{x0 + 10}" y="{y0 + tile_h - 14}" font-size="11" font-family="Arial, sans-serif" fill="#334155">'
                    f'Source {src_w}x{src_h} mm</text>',
                ]
            )

            color_idx = 0
            for alloc in group["allocs"]:
                qty = max(int(alloc.cut_quantity or 1), 1)
                for _i in range(qty):
                    x_mm = max(0, int(alloc.placed_x_mm or 0))
                    y_mm = max(0, int(alloc.placed_y_mm or 0))
                    w_mm = max(1, int(alloc.cut_width_mm or 1))
                    h_mm = max(1, int(alloc.cut_height_mm or 1))
                    x_mm = min(x_mm, src_w - 1)
                    y_mm = min(y_mm, src_h - 1)
                    w_mm = min(w_mm, src_w - x_mm)
                    h_mm = min(h_mm, src_h - y_mm)
                    cut_px_w = max(int(w_mm * scale), 1)
                    cut_px_h = max(int(h_mm * scale), 1)
                    cut_px_x = rect_x + int(x_mm * scale)
                    cut_px_y = rect_y + int(y_mm * scale)
                    cut_color = palette[color_idx % len(palette)]
                    color_idx += 1
                    parts.append(
                        f'<rect x="{cut_px_x}" y="{cut_px_y}" width="{cut_px_w}" height="{cut_px_h}" '
                        f'fill="{cut_color}" fill-opacity="0.72" stroke="#0f172a" '
                        f'data-alloc-id="{alloc.id}" data-source-bin="{escape(group["key"])}" '
                        f'data-x-mm="{x_mm}" data-y-mm="{y_mm}" data-w-mm="{w_mm}" data-h-mm="{h_mm}"/>'
                    )

        parts.append("</svg>")
        return "".join(parts)

    @staticmethod
    def _tp_candidate_sources(offcuts, sheets, sheet_lots):
        return [
            {
                "type": "offcut",
                "id": offcut.id,
                "record": offcut,
                "width_mm": offcut.width_mm,
                "height_mm": offcut.height_mm,
            }
            for offcut in offcuts
        ] + [
            {
                "type": "sheet",
                "id": sheet.id,
                "record": sheet,
                "width_mm": sheet.width_mm,
                "height_mm": sheet.height_mm,
                "area_mm2": float(sheet.area_mm2),
                "unit_cost": float(sheet.landed_cost or sheet.product_id.standard_price or 0.0),
            }
            for sheet in sheets
        ] + [
            {
                "type": "sheet_lot",
                "id": f"lot-{lot.id}",
                "record": lot,
                "width_mm": lot.tp_width_mm,
                "height_mm": lot.tp_height_mm,
                "area_mm2": float(lot.tp_width_mm * lot.tp_height_mm),
                "unit_cost": float(lot.product_id.standard_price or 0.0),
            }
            for lot in sheet_lots
        ]

    def _tp_count_future_fits(self, width_mm, height_mm, cuts):
        rem_w = int(width_mm or 0)
        rem_h = int(height_mm or 0)
        count = 0
        for future_cut in cuts:
            fits, _rotated, _fit_w, _fit_h, next_w, next_h = self._tp_fit_source(
                rem_w,
                rem_h,
                future_cut["width_mm"],
                future_cut["height_mm"],
                3,
            )
            if not fits:
                continue
            count += 1
            rem_w, rem_h = next_w, next_h
        return count

    def _tp_select_new_sheet_candidate(self, *, cut, remaining_cuts, product, material_identity):
        sheets = self._tp_compatible_sheet_formats(product, material_identity)
        sheet_lots = self._tp_compatible_sheet_lots(product, material_identity)
        sources = self._tp_candidate_sources(self.env["tp.offcut"], sheets, sheet_lots)
        best = None
        best_score = None
        for source in sources:
            if source["type"] not in ("sheet", "sheet_lot"):
                continue
            fits, rotated, fit_w, fit_h, rem_w, rem_h = self._tp_fit_source(
                source["width_mm"], source["height_mm"], cut["width_mm"], cut["height_mm"], 3
            )
            if not fits:
                continue
            future_fit_count = self._tp_count_future_fits(rem_w, rem_h, remaining_cuts)
            remainder_area = float(rem_w * rem_h)
            cost = float(source.get("unit_cost") or 0.0)
            source_priority = 0 if source["type"] == "sheet_lot" else 1
            score = (
                -future_fit_count,
                source_priority,
                remainder_area,
                cost,
                str(source["id"]),
            )
            if best is None or score < best_score:
                best = {
                    "source": source,
                    "rotated": rotated,
                    "fit_w": fit_w,
                    "fit_h": fit_h,
                    "rem_w": rem_w,
                    "rem_h": rem_h,
                }
                best_score = score
        return best

    def _tp_build_cuts_for_scope(self, scope_mos):
        cuts = []
        mo_map = {rec.id: rec for rec in scope_mos}
        for scoped_mo in scope_mos:
            for cut in scoped_mo._tp_get_cut_entries():
                cuts.append(
                    {
                        "width_mm": cut["width_mm"],
                        "height_mm": cut["height_mm"],
                        "source_mo_id": scoped_mo.id,
                    }
                )
        cuts.sort(key=lambda x: x["width_mm"] * x["height_mm"], reverse=True)
        return cuts, mo_map

    def _tp_allocate_from_offcut(self, *, run, cut, candidate):
        offcut = candidate["source"]["record"]
        fit_w = candidate["fit_w"]
        fit_h = candidate["fit_h"]
        rem_w = candidate["rem_w"]
        rem_h = candidate["rem_h"]
        bin_key = f"offcut:{offcut.id}"
        bin_label = offcut.lot_id.name if offcut.lot_id else (offcut.name or bin_key)
        self.env["tp.nesting.allocation"].create(
            {
                "run_id": run.id,
                "source_type": "offcut",
                "source_offcut_id": offcut.id,
                "source_lot_id": offcut.lot_id.id,
                "source_bin_key": bin_key,
                "source_bin_label": bin_label,
                "cut_width_mm": fit_w,
                "cut_height_mm": fit_h,
                "placed_x_mm": 0,
                "placed_y_mm": 0,
                "cut_quantity": 1,
                "rotation_applied": candidate["rotated"],
                "allocated_area_mm2": float(fit_w * fit_h),
                "status": "reserved",
            }
        )
        offcut.action_set_reserved(cut["source_mo_id"], run.id)
        if rem_w > 0 and rem_h > 0:
            planned_kind = "offcut" if rem_w >= 200 and rem_h >= 200 else "waste"
            self._tp_plan_remainder(
                run=run,
                planned_kind=planned_kind,
                planned_source_type="offcut",
                product=offcut.product_id,
                width_mm=int(rem_w),
                height_mm=int(rem_h),
                parent_lot=offcut.parent_lot_id or offcut.lot_id,
                parent_offcut=offcut,
                parent_remaining_area_mm2=float(offcut.remaining_area_mm2 or offcut.area_mm2 or 0.0),
                parent_remaining_value=float(offcut.remaining_value or 0.0),
                kerf_mm=3,
            )

    def _tp_allocate_from_sheet(self, *, run, cut, candidate, source_mo):
        sheet = candidate["source"]["record"]
        fit_w = candidate["fit_w"]
        fit_h = candidate["fit_h"]
        rem_w = candidate["rem_w"]
        rem_h = candidate["rem_h"]
        parent_lot = self.env["stock.lot"].create(
            {
                "name": f"SHEET-SRC-{run.id}",
                "product_id": sheet.product_id.id,
                "company_id": self.env.company.id,
            }
        )
        bin_key = f"sheetfmt:{sheet.id}:run:{run.id}"
        bin_label = parent_lot.name
        self.env["tp.nesting.allocation"].create(
            {
                "run_id": run.id,
                "source_type": "sheet",
                "source_sheet_format_id": sheet.id,
                "source_lot_id": parent_lot.id,
                "source_bin_key": bin_key,
                "source_bin_label": bin_label,
                "cut_width_mm": fit_w,
                "cut_height_mm": fit_h,
                "placed_x_mm": 0,
                "placed_y_mm": 0,
                "cut_quantity": 1,
                "rotation_applied": candidate["rotated"],
                "allocated_area_mm2": float(fit_w * fit_h),
                "status": "allocated",
            }
        )
        source_mo._tp_process_sheet_remainder(run, sheet, rem_w, rem_h)

    def _tp_allocate_from_sheet_slot(
        self,
        *,
        run,
        cut,
        slot,
        fit_w,
        fit_h,
        rotated,
        placed_x_mm=0,
        placed_y_mm=0,
        source_bin_key=None,
        source_bin_label=None,
    ):
        vals = {
            "run_id": run.id,
            "source_type": "sheet",
            "source_lot_id": slot["source_lot_id"],
            "source_bin_key": source_bin_key or slot.get("source_bin_key"),
            "source_bin_label": source_bin_label or slot.get("source_bin_label"),
            "cut_width_mm": fit_w,
            "cut_height_mm": fit_h,
            "placed_x_mm": int(placed_x_mm or 0),
            "placed_y_mm": int(placed_y_mm or 0),
            "cut_quantity": 1,
            "rotation_applied": rotated,
            "allocated_area_mm2": float(fit_w * fit_h),
            "status": "allocated",
        }
        if slot.get("source_sheet_format_id"):
            vals["source_sheet_format_id"] = slot["source_sheet_format_id"]
        self.env["tp.nesting.allocation"].create(vals)

    def _tp_finalize_sheet_slots(self, run, sheet_slots):
        for idx, slot in enumerate(sheet_slots, start=1):
            rem_w = int(slot.get("width_mm", 0))
            rem_h = int(slot.get("height_mm", 0))
            if rem_w <= 0 or rem_h <= 0:
                continue
            self._tp_process_sheet_remainder_values(
                run=run,
                product=slot["product"],
                source_lot=slot["source_lot"],
                parent_width_mm=slot["parent_width_mm"],
                parent_height_mm=slot["parent_height_mm"],
                landed_cost=slot["landed_cost"],
                rem_w=rem_w,
                rem_h=rem_h,
                name_suffix=f"{idx}-{slot['source_lot_id']}",
            )

    def _tp_allocate_from_sheet_lot(self, *, run, cut, candidate):
        lot = candidate["source"]["record"]
        fit_w = candidate["fit_w"]
        fit_h = candidate["fit_h"]
        rem_w = candidate["rem_w"]
        rem_h = candidate["rem_h"]
        bin_key = f"sheetlot:{lot.id}:run:{run.id}"
        bin_label = lot.name or bin_key
        self.env["tp.nesting.allocation"].create(
            {
                "run_id": run.id,
                "source_type": "sheet",
                "source_lot_id": lot.id,
                "source_bin_key": bin_key,
                "source_bin_label": bin_label,
                "cut_width_mm": fit_w,
                "cut_height_mm": fit_h,
                "placed_x_mm": 0,
                "placed_y_mm": 0,
                "cut_quantity": 1,
                "rotation_applied": candidate["rotated"],
                "allocated_area_mm2": float(fit_w * fit_h),
                "status": "allocated",
            }
        )
        self._tp_process_sheet_remainder_values(
            run=run,
            product=lot.product_id,
            source_lot=lot,
            parent_width_mm=lot.tp_width_mm,
            parent_height_mm=lot.tp_height_mm,
            landed_cost=lot.product_id.standard_price or 0.0,
            rem_w=rem_w,
            rem_h=rem_h,
        )

    def _tp_finalize_run_metrics(self, run, optimizer_metrics):
        allocations = run.allocation_ids
        waste_area = sum(
            run.produced_offcut_ids.filtered(lambda r: r.planned_kind == "waste").mapped("area_mm2")
        )
        total_area = sum(float(a.allocated_area_mm2 or 0.0) for a in allocations)
        offcut_area = sum(
            float(a.allocated_area_mm2 or 0.0) for a in allocations if a.source_type == "offcut"
        )
        offcut_pct = (offcut_area / total_area * 100.0) if total_area > 0 else 0.0
        score = float(total_area) - float(offcut_area)
        score_breakdown = optimizer_metrics.get("score_breakdown") or {}
        debug_artifact = optimizer_metrics.get("debug_artifact") or {}
        run.write(
            {
                "search_nodes": optimizer_metrics.get("search_nodes", 0),
                "search_ms": optimizer_metrics.get("search_ms", 0),
                "waste_area_mm2_total": waste_area,
                "offcut_utilization_pct": offcut_pct,
                "full_sheet_count": optimizer_metrics.get("full_sheet_count", 0),
                "score": score,
                "selected_order_name": optimizer_metrics.get("selected_order_name", ""),
                "scoring_preset": optimizer_metrics.get("policy_preset", ""),
                "candidate_plan_count": optimizer_metrics.get("candidate_plan_count", 0),
                "rejected_plan_count": optimizer_metrics.get("rejected_plan_count", 0),
                "score_breakdown_json": json.dumps(score_breakdown, sort_keys=True),
                "debug_artifact_json": json.dumps(debug_artifact, sort_keys=True) if debug_artifact else False,
            }
        )

    def _tp_select_offcut_candidate(self, *, cut, offcut_sources):
        best = None
        best_score = None
        for source in offcut_sources:
            offcut = source["record"]
            fits, rotated, fit_w, fit_h, rem_w, rem_h = self._tp_fit_source(
                offcut.width_mm, offcut.height_mm, cut["width_mm"], cut["height_mm"], 3
            )
            if not fits:
                continue
            score = (
                float(rem_w * rem_h),
                float(offcut.width_mm * offcut.height_mm),
                offcut.id,
                1 if rotated else 0,
            )
            if best is None or score < best_score:
                best = {
                    "source": {"record": offcut},
                    "fit_w": fit_w,
                    "fit_h": fit_h,
                    "rem_w": rem_w,
                    "rem_h": rem_h,
                    "rotated": rotated,
                }
                best_score = score
        return best

    def _tp_allocate_offcuts_first(self, *, mo, run, cuts, source_pool):
        remaining_cuts = []
        for cut in cuts:
            offcut_sources = source_pool.offcut_sources()
            candidate = mo._tp_select_offcut_candidate(cut=cut, offcut_sources=offcut_sources)
            if not candidate:
                remaining_cuts.append(cut)
                continue
            self._tp_allocate_from_offcut(run=run, cut=cut, candidate=candidate)
            # A consumed offcut changes availability; rebuild the pool for next cut.
            source_pool.invalidate()
        return remaining_cuts

    @staticmethod
    def _tp_build_engine_sheet_sources(source_pool):
        return source_pool.sheet_stock_sources(), source_pool.sheet_format_sources()

    @staticmethod
    def _tp_primary_remainder_dims(free_rects):
        if not free_rects:
            return 0, 0
        primary = max(
            free_rects,
            key=lambda rect: (int(rect.get("w", 0)) * int(rect.get("h", 0)), int(rect.get("w", 0)), int(rect.get("h", 0))),
        )
        return int(primary.get("w", 0)), int(primary.get("h", 0))

    def _tp_apply_sheet_plan(self, *, run, plan_bins):
        sheet_slots = []
        for idx, bin_state in enumerate(plan_bins, start=1):
            source = bin_state["source"]
            source_stable_id = str(source.get("stable_id") or source.get("id") or idx)
            bin_key = f"{source.get('kind', 'sheet')}:{source_stable_id}:bin:{idx}"
            slot = {}
            if source["kind"] == "sheet_lot":
                lot = source["record"]
                slot = {
                    "source_lot_id": lot.id,
                    "source_lot": lot,
                    "source_sheet_format_id": False,
                    "source_bin_key": bin_key,
                    "source_bin_label": lot.name or bin_key,
                    "product": lot.product_id,
                    "parent_width_mm": int(lot.tp_width_mm),
                    "parent_height_mm": int(lot.tp_height_mm),
                    "landed_cost": float(lot.product_id.standard_price or 0.0),
                }
            elif source["kind"] == "sheet_product":
                product = source["record"]
                source_lot = self.env["stock.lot"].create(
                    {
                        "name": f"SHEET-SRC-{run.id}-{idx}",
                        "product_id": product.id,
                        "company_id": self.env.company.id,
                        "tp_width_mm": int(source.get("width_mm") or 0),
                        "tp_height_mm": int(source.get("height_mm") or 0),
                    }
                )
                slot = {
                    "source_lot_id": source_lot.id,
                    "source_lot": source_lot,
                    "source_sheet_format_id": False,
                    "source_bin_key": bin_key,
                    "source_bin_label": source_lot.name,
                    "product": product,
                    "parent_width_mm": int(source.get("width_mm") or 0),
                    "parent_height_mm": int(source.get("height_mm") or 0),
                    "landed_cost": float(product.standard_price or 0.0),
                }
            else:
                sheet = source["record"]
                source_lot = self.env["stock.lot"].create(
                    {
                        "name": f"SHEET-SRC-{run.id}-{idx}",
                        "product_id": sheet.product_id.id,
                        "company_id": self.env.company.id,
                    }
                )
                slot = {
                    "source_lot_id": source_lot.id,
                    "source_lot": source_lot,
                    "source_sheet_format_id": sheet.id,
                    "source_bin_key": bin_key,
                    "source_bin_label": source_lot.name,
                    "product": sheet.product_id,
                    "parent_width_mm": int(sheet.width_mm),
                    "parent_height_mm": int(sheet.height_mm),
                    "landed_cost": float(sheet.landed_cost or sheet.product_id.standard_price or 0.0),
                }

            for placement in bin_state.get("placements", []):
                cut = placement["cut"]
                self._tp_allocate_from_sheet_slot(
                    run=run,
                    cut=cut,
                    slot=slot,
                    fit_w=int(placement["fit_w"]),
                    fit_h=int(placement["fit_h"]),
                    rotated=bool(placement["rotated"]),
                    placed_x_mm=int(placement.get("x", 0)),
                    placed_y_mm=int(placement.get("y", 0)),
                    source_bin_key=slot["source_bin_key"],
                    source_bin_label=slot["source_bin_label"],
                )

            rem_w, rem_h = self._tp_primary_remainder_dims(bin_state.get("free_rects", []))
            slot["width_mm"] = rem_w
            slot["height_mm"] = rem_h
            sheet_slots.append(slot)

        self._tp_finalize_sheet_slots(run, sheet_slots)

    def _tp_execute_with_engine(self, *, mo, run, scope_mos, mode):
        company = mo.company_id
        base_mo = scope_mos[0]
        product = base_mo.x_tp_source_so_line_id.product_id or base_mo.product_id
        material_identity = base_mo._tp_get_material_identity()
        cuts, _mo_map = self._tp_build_cuts_for_scope(scope_mos)
        source_pool = TpNestingSourcePool(mo=mo, product=product, material_identity=material_identity)

        remaining_cuts = mo._tp_allocate_offcuts_first(
            mo=mo,
            run=run,
            cuts=cuts,
            source_pool=source_pool,
        )

        metrics = {
            "search_nodes": 0,
            "search_ms": 0,
            "full_sheet_count": 0,
            "memo_hits": 0,
            "memo_prunes": 0,
            "early_infeasible_checks": 0,
            "candidate_plan_count": 0,
            "rejected_plan_count": 0,
            "selected_order_name": "",
            "score_breakdown": {},
            "policy_preset": company.tp_nesting_policy_preset or "yield_first",
            "policy_weights": {},
            "max_pieces": company.tp_nesting_max_piece_count or 200,
            "beam_width_cap": company.tp_nesting_beam_width_cap or 24,
            "timeout_cap_ms": company.tp_nesting_timeout_cap_ms or 15000,
            "effective_timeout_ms": 0,
            "effective_beam_width": company.tp_nesting_beam_width or 6,
            "effective_branch_cap": company.tp_nesting_branch_cap or 12,
            "infeasible_reason": "",
            "debug_artifact": {},
        }
        if remaining_cuts:
            # Offcut pass can mutate availability; ensure fresh source pool snapshot.
            source_pool.invalidate()
            sheet_lot_sources, sheet_format_sources = mo._tp_build_engine_sheet_sources(source_pool)
            max_pieces = max(1, int(company.tp_nesting_max_piece_count or 200))
            beam_width_cap = max(1, int(company.tp_nesting_beam_width_cap or 24))
            timeout_cap_ms = max(0, int(company.tp_nesting_timeout_cap_ms or 15000))
            requested_timeout = (company.tp_nesting_timeout_ms or 2000) if mode == "optimal" else 0
            effective_timeout = int(requested_timeout)
            if effective_timeout > 0 and timeout_cap_ms > 0:
                effective_timeout = min(effective_timeout, timeout_cap_ms)
            effective_beam_width = min(max(1, int(company.tp_nesting_beam_width or 6)), beam_width_cap)
            effective_branch_cap = min(max(1, int(company.tp_nesting_branch_cap or 12)), beam_width_cap)
            metrics.update(
                {
                    "max_pieces": max_pieces,
                    "beam_width_cap": beam_width_cap,
                    "timeout_cap_ms": timeout_cap_ms,
                    "effective_timeout_ms": effective_timeout,
                    "effective_beam_width": effective_beam_width,
                    "effective_branch_cap": effective_branch_cap,
                }
            )
            engine = Tp2DNestingEngine(
                kerf_mm=3,
                timeout_ms=effective_timeout,
                sheet_size_candidate_limit=company.tp_nesting_sheet_size_candidate_limit or 25,
                beam_width=effective_beam_width,
                branch_cap=effective_branch_cap,
                enable_exact_refinement=bool(company.tp_nesting_exact_refinement_enabled),
                exact_refinement_cut_threshold=company.tp_nesting_exact_refinement_cut_threshold or 8,
                exact_refinement_timeout_ms=min(
                    int(company.tp_nesting_exact_refinement_timeout_ms or 250),
                    timeout_cap_ms,
                )
                if timeout_cap_ms > 0
                else int(company.tp_nesting_exact_refinement_timeout_ms or 250),
                mode=mode,
                kernel_name=company.tp_nesting_kernel_name or "maxrects",
                scoring_preset=company.tp_nesting_policy_preset or "yield_first",
                waste_priority=company.tp_nesting_waste_priority or 1.0,
                offcut_reuse_priority=company.tp_nesting_offcut_reuse_priority or 1.0,
                sheet_count_penalty=company.tp_nesting_sheet_count_penalty or 1.0,
                cost_sensitivity=company.tp_nesting_cost_sensitivity or 1.0,
                debug_enabled=bool(company.tp_nesting_debug_enabled),
                max_pieces=max_pieces,
                beam_width_cap=beam_width_cap,
                timeout_cap_ms=timeout_cap_ms,
            )
            plan = engine.plan(
                cuts=remaining_cuts,
                sheet_lot_sources=sheet_lot_sources,
                sheet_format_sources=sheet_format_sources,
            )
            if not plan.get("ok"):
                reason = (plan.get("metrics") or {}).get("infeasible_reason") or ""
                cut = plan.get("error_cut", {}) or {}
                if reason == "max_pieces_exceeded":
                    raise ValidationError(
                        f"Cut list has {len(remaining_cuts)} pieces, exceeding configured max {max_pieces}."
                    )
                if reason == "no_sheet_sources":
                    raise ValidationError("No compatible sheet sources are available for this nesting run.")
                if reason == "cut_exceeds_all_sources":
                    raise ValidationError(
                        f"Cut {cut.get('width_mm', 0)}x{cut.get('height_mm', 0)} exceeds all configured sheet sources."
                    )
                raise ValidationError(
                    f"No compatible source available for cut {cut.get('width_mm', 0)}x{cut.get('height_mm', 0)}."
                )
            self._tp_apply_sheet_plan(run=run, plan_bins=plan.get("bins", []))
            metrics = plan.get("metrics", metrics)

        self._tp_finalize_run_metrics(run, metrics)

    def _tp_execute_deterministic(self, *, mo, run, scope_mos):
        self._tp_execute_with_engine(mo=mo, run=run, scope_mos=scope_mos, mode="deterministic")

    def _tp_execute_optimal(self, *, mo, run, scope_mos):
        self._tp_execute_with_engine(mo=mo, run=run, scope_mos=scope_mos, mode="optimal")

    def button_mark_done(self):
        self._tp_validate_nesting_before_produce()
        self._tp_sync_raw_moves_from_nesting()
        result = super().button_mark_done()
        self.filtered(lambda mo: mo.state == "done")._tp_materialize_last_nesting_outputs()
        return result

    def action_run_tp_nesting(self):
        processed_scope = set()
        for mo in self:
            scope_mos = mo._tp_get_nesting_scope_mos()
            scope_key = tuple(scope_mos.ids)
            if scope_key in processed_scope:
                continue
            processed_scope.add(scope_key)

            run = self.env["tp.nesting.run"].create(
                {
                    "name": f"NEST-{mo.name}",
                    "mo_id": mo.id,
                    "kerf_mm": 3,
                    "rotation_mode": "free",
                    "engine_mode": mo.company_id.tp_nesting_engine_mode or "optimal",
                    "note": f"Scope MOs: {len(scope_mos)}",
                }
            )
            mo._tp_create_job_for_run(run, scope_mos)
            mo._tp_release_scope_reservations(scope_mos)
            try:
                with self.env.cr.savepoint():
                    company = mo.company_id
                    engine_mode = company.tp_nesting_engine_mode or "optimal"
                    try:
                        if engine_mode == "optimal":
                            self._tp_execute_optimal(mo=mo, run=run, scope_mos=scope_mos)
                        else:
                            self._tp_execute_deterministic(mo=mo, run=run, scope_mos=scope_mos)
                    except TimeoutError:
                        if company.tp_nesting_fallback_enabled:
                            run.write({"engine_mode": "deterministic"})
                            self._tp_execute_deterministic(mo=mo, run=run, scope_mos=scope_mos)
                        else:
                            raise

                mo._tp_capture_run_outputs(run)
                run.write(
                    {
                        "state": "done",
                        "finished_at": fields.Datetime.now(),
                        "nesting_svg": mo._tp_build_nesting_svg(run),
                    }
                )
                scope_mos.write({"tp_last_nesting_run_id": run.id, "tp_nesting_state": "done"})
                # Keep MO raw consumption aligned with the selected sheet sources
                # immediately after a successful nesting run.
                mo._tp_sync_raw_moves_from_nesting()
            except Exception as exc:
                run.write(
                    {
                        "state": "failed",
                        "finished_at": fields.Datetime.now(),
                        "note": str(exc),
                    }
                )
                scope_mos.write({"tp_last_nesting_run_id": run.id, "tp_nesting_state": "failed"})
                raise
        return True

    def action_rerun_tp_nesting(self):
        processed_scope = set()
        for mo in self:
            scope_mos = mo._tp_get_nesting_scope_mos()
            scope_key = tuple(scope_mos.ids)
            if scope_key in processed_scope:
                continue
            processed_scope.add(scope_key)
            mo._tp_mark_prior_runs_superseded(scope_mos)
            mo._tp_release_scope_reservations(scope_mos)
        return self.action_run_tp_nesting()
