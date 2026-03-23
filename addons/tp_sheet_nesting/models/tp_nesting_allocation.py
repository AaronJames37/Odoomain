from odoo import api, fields, models
from odoo.exceptions import ValidationError


class TpNestingAllocation(models.Model):
    _name = "tp.nesting.allocation"
    _description = "TP Nesting Allocation"
    _order = "id asc"

    run_id = fields.Many2one("tp.nesting.run", required=True, ondelete="cascade")
    mo_id = fields.Many2one(related="run_id.mo_id", store=True, readonly=True)
    job_id = fields.Many2one(related="run_id.job_id", store=True, readonly=True)
    source_type = fields.Selection([("offcut", "Offcut"), ("sheet", "Sheet")], required=True)
    source_offcut_id = fields.Many2one("tp.offcut")
    source_sheet_format_id = fields.Many2one("tp.sheet.format")
    source_lot_id = fields.Many2one("stock.lot", ondelete="set null")
    cut_width_mm = fields.Integer(required=True)
    cut_height_mm = fields.Integer(required=True)
    cut_quantity = fields.Integer(default=1, required=True)
    rotation_applied = fields.Boolean(default=False)
    placed_x_mm = fields.Integer(default=0)
    placed_y_mm = fields.Integer(default=0)
    source_bin_key = fields.Char()
    source_bin_label = fields.Char()
    allocated_area_mm2 = fields.Float()
    status = fields.Selection(
        [("allocated", "Allocated"), ("reserved", "Reserved"), ("consumed", "Consumed")],
        default="allocated",
        required=True,
    )

    @api.constrains("source_type", "source_offcut_id", "source_sheet_format_id", "source_lot_id")
    def _check_source_consistency(self):
        for rec in self:
            if rec.source_type == "offcut" and not rec.source_offcut_id:
                raise ValidationError("Offcut allocations require an offcut source.")
            if rec.source_type == "sheet" and not rec.source_sheet_format_id and not rec.source_lot_id:
                raise ValidationError("Sheet allocations require a sheet format or sheet lot source.")
