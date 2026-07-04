from odoo import fields, models


class MrpProduction(models.Model):
    _inherit = "mrp.production"

    x_tp_nesting_run_id = fields.Many2one(
        "tp.nesting.run",
        string="Nesting Run",
        index=True,
        ondelete="set null",
        help="The nesting run that produced this manufacturing order.",
    )
