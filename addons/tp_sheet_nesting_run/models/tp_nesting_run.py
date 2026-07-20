from collections import Counter

from odoo import api, fields, models
from odoo.exceptions import UserError


class TpNestingRun(models.Model):
    _inherit = "tp.nesting.run"

    # Loosen the existing required mo_id — we now support runs with N MOs.
    # The legacy field still gets set to the first MO for back-compat with any
    # consumer reading run.mo_id directly.
    mo_id = fields.Many2one(required=False)

    x_tp_mo_ids = fields.One2many(
        "mrp.production",
        "x_tp_nesting_run_id",
        string="Manufacturing Orders",
        readonly=True,
    )
    x_tp_mo_count = fields.Integer(
        compute="_compute_x_tp_mo_count",
        string="MO Count",
    )
    # Holds the per-sheet DXF bundle so it survives MO completion.
    x_tp_dxf_bytes = fields.Binary(
        string="DXF Bundle (.zip)",
        readonly=True,
        attachment=True,
    )
    x_tp_dxf_filename = fields.Char(readonly=True)

    # When a run was produced from the wizard, these record what the engine
    # decided. They're used later by the 'Create MO' action so we know exactly
    # what to consume.
    x_tp_source_product_id = fields.Many2one(
        "product.product",
        string="Source Sheet Product",
        readonly=True,
        help="Full-sheet product to consume when the MO is created.",
    )
    x_tp_sheets_needed = fields.Integer(
        string="Sheets Needed",
        readonly=True,
        help="How many sheets of the source product to consume.",
    )
    x_tp_panel_count = fields.Integer(
        string="Panels Placed",
        readonly=True,
    )
    x_tp_saw_operation_count = fields.Integer(
        string="Saw Cuts",
        readonly=True,
        help="Estimated panel-saw operations for the chosen nesting layout.",
    )
    x_tp_trim_cut_count = fields.Integer(
        string="Trim Cuts",
        readonly=True,
        help="Outer/leftover trim cuts included in the saw cut estimate.",
    )
    x_tp_fence_setting_count = fields.Integer(
        string="Fence Settings",
        readonly=True,
        help="Approximate count of distinct saw stop/fence dimensions.",
    )
    x_tp_can_create_mo = fields.Boolean(
        string="Can Create MO",
        compute="_compute_x_tp_can_create_mo",
        help="True when the run has no MO yet and we have what we need to make one.",
    )

    @api.depends(
        "x_tp_mo_ids",
        "x_tp_source_product_id",
        "x_tp_sheets_needed",
        "allocation_ids",
        "allocation_ids.source_type",
        "allocation_ids.source_sheet_format_id",
        "allocation_ids.source_lot_id",
        "allocation_ids.source_bin_key",
    )
    def _compute_x_tp_can_create_mo(self):
        for rec in self:
            rec.x_tp_can_create_mo = (
                not rec.x_tp_mo_ids
                and bool(rec._tp_x_fresh_sheet_qty_by_product())
            )

    @api.depends("x_tp_mo_ids")
    def _compute_x_tp_mo_count(self):
        for rec in self:
            rec.x_tp_mo_count = len(rec.x_tp_mo_ids)

    def action_view_x_tp_mos(self):
        self.ensure_one()
        mos = self.x_tp_mo_ids
        action = {
            "type": "ir.actions.act_window",
            "name": "Manufacturing Orders",
            "res_model": "mrp.production",
            "view_mode": "list,form",
            "domain": [("id", "in", mos.ids)],
        }
        if len(mos) == 1:
            action.update({"view_mode": "form", "res_id": mos.id})
        return action

    def _tp_x_fresh_sheet_qty_by_product(self):
        """Return product_id -> fresh sheet count for this run.

        Modern runs can use mixed sheet sizes/products and offcuts, so the
        authoritative source is the allocation table. The legacy fields are
        retained as a fallback for old runs created before allocations carried
        enough source detail.
        """
        self.ensure_one()
        product_to_qty = Counter()
        seen_bins = set()
        for alloc in self.allocation_ids.filtered(lambda a: a.source_type == "sheet"):
            bin_key = alloc.source_bin_key or "alloc:%s" % alloc.id
            if bin_key in seen_bins:
                continue
            seen_bins.add(bin_key)
            product = (
                alloc.source_sheet_format_id.product_id
                or alloc.source_lot_id.product_id
            )
            if product:
                product_to_qty[product.id] += 1

        if not product_to_qty and self.x_tp_source_product_id and self.x_tp_sheets_needed > 0:
            product_to_qty[self.x_tp_source_product_id.id] = int(self.x_tp_sheets_needed)
        return product_to_qty

    def _tp_x_reserved_offcuts(self):
        self.ensure_one()
        return self.allocation_ids.filtered(
            lambda a: a.source_type == "offcut" and a.source_offcut_id
        ).mapped("source_offcut_id")

    def action_create_mo(self):
        """Create the Manufacturing Order for this run, consuming the
        sheets the engine decided on. Posts stock + COGS via standard Odoo
        MRP machinery."""
        self.ensure_one()
        if self.x_tp_mo_ids:
            raise UserError("This run already has %d MO(s)." % len(self.x_tp_mo_ids))

        product_to_qty = self._tp_x_fresh_sheet_qty_by_product()
        if not product_to_qty:
            raise UserError(
                "No fresh sheet consumption is recorded on this run. "
                "It may be an offcut-only run, or the run was created without "
                "source allocations."
            )

        Product = self.env["product.product"].sudo()
        Mrp = self.env["mrp.production"].sudo()
        Warehouse = self.env["stock.warehouse"].sudo()
        Location = self.env["stock.location"].sudo()

        warehouse = Warehouse.search([("company_id", "=", self.env.company.id)], limit=1)
        if not warehouse:
            raise UserError("No warehouse configured for company %s." % self.env.company.display_name)
        stock_location = warehouse.lot_stock_id
        production_location = Location.search([
            ("usage", "=", "production"),
            ("company_id", "in", [self.env.company.id, False]),
        ], limit=1)
        if not production_location:
            raise UserError("No 'Production' stock location found.")

        # Re-check stock NOW (don't trust what was available at wizard time).
        if not self.env.company.tp_nesting_ignore_sheet_stock:
            shortages = []
            for product_id, qty in product_to_qty.items():
                sheet = Product.browse(product_id).with_context(location=stock_location.id)
                available = sheet.qty_available
                if available < qty:
                    shortages.append(
                        "  - %s: need %d sheets, have %.1f"
                        % (sheet.display_name, qty, available)
                    )
            if shortages:
                raise UserError(
                    "Insufficient stock for this nesting run:\n%s\n\n"
                    "Top up stock and try again."
                    % "\n".join(shortages)
                )

        cut_op = Product.search([("default_code", "=", "CUT-OPERATION")], limit=1)
        if not cut_op:
            raise UserError(
                "No CUT-OPERATION service product exists. Create one "
                "(type=service, default_code=CUT-OPERATION) before creating MOs."
            )

        mos_created = self.env["mrp.production"]
        for product_id, qty in product_to_qty.items():
            sheet_product = Product.browse(product_id)
            mo = Mrp.create({
                "product_id": cut_op.id,
                "product_qty": qty,
                "product_uom_id": cut_op.uom_id.id,
                "origin": "%s / %s" % (
                    self.name,
                    sheet_product.default_code or sheet_product.display_name,
                ),
                "company_id": self.env.company.id,
                "x_tp_nesting_run_id": self.id,
                "move_raw_ids": [(0, 0, {
                    "product_id": sheet_product.id,
                    "product_uom_qty": qty,
                    "product_uom": sheet_product.uom_id.id,
                    "location_id": stock_location.id,
                    "location_dest_id": production_location.id,
                    "company_id": self.env.company.id,
                })],
            })
            try:
                mo.action_confirm()
            except Exception:
                import logging
                logging.getLogger(__name__).exception("MO %s action_confirm failed", mo.name)
            mos_created |= mo

        # Keep the legacy single-mo_id pointing at the (now) primary MO.
        self.mo_id = mos_created[0].id

        # If this run reserved offcuts before the MO existed, attach the
        # reservation to the primary MO as well as the run for traceability.
        for offcut in self._tp_x_reserved_offcuts():
            if offcut.state == "reserved":
                offcut.action_set_reserved(mos_created[0].id, self.id)

        if len(mos_created) > 1:
            return {
                "type": "ir.actions.act_window",
                "name": "Manufacturing Orders",
                "res_model": "mrp.production",
                "view_mode": "list,form",
                "domain": [("id", "in", mos_created.ids)],
                "target": "current",
            }

        return {
            "type": "ir.actions.act_window",
            "res_model": "mrp.production",
            "res_id": mos_created.id,
            "view_mode": "form",
            "target": "current",
        }

    def action_download_x_tp_dxf(self):
        self.ensure_one()
        if not self.x_tp_dxf_bytes:
            from odoo.exceptions import UserError
            raise UserError("This run has no DXF bundle attached.")
        return {
            "type": "ir.actions.act_url",
            "url": (
                "/web/content/?model=%s&id=%s&field=x_tp_dxf_bytes"
                "&filename_field=x_tp_dxf_filename&download=true"
            ) % (self._name, self.id),
            "target": "self",
        }
