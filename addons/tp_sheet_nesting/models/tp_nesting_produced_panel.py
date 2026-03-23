from odoo import fields, models


class TpNestingProducedPanel(models.Model):
    _name = "tp.nesting.produced.panel"
    _description = "TP Nesting Produced Panel"
    _order = "id asc"

    run_id = fields.Many2one("tp.nesting.run", required=True, ondelete="cascade")
    job_id = fields.Many2one(related="run_id.job_id", store=True, readonly=True)
    allocation_id = fields.Many2one("tp.nesting.allocation", ondelete="set null")
    source_lot_id = fields.Many2one("stock.lot", ondelete="set null")
    width_mm = fields.Integer(required=True)
    height_mm = fields.Integer(required=True)
    quantity = fields.Integer(default=1, required=True)
