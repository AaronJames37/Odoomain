from odoo import fields, models


class TpNestingConsumedLot(models.Model):
    _name = "tp.nesting.consumed.lot"
    _description = "TP Nesting Consumed Material Lot"
    _order = "id asc"

    run_id = fields.Many2one("tp.nesting.run", required=True, ondelete="cascade")
    job_id = fields.Many2one(related="run_id.job_id", store=True, readonly=True)
    lot_id = fields.Many2one("stock.lot", required=True, ondelete="restrict")
    source_type = fields.Selection([("offcut", "Offcut"), ("sheet", "Sheet")], required=True)
    allocation_count = fields.Integer(default=1, required=True)
