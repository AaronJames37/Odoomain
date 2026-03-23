from odoo import fields, models


class TpOffcutNestingLink(models.Model):
    _inherit = "tp.offcut"

    reservation_run_id = fields.Many2one("tp.nesting.run", string="Reserved In Run", readonly=True)
    produced_in_run_id = fields.Many2one("tp.nesting.run", string="Produced In Run", readonly=True, index=True)
    produced_in_mo_id = fields.Many2one(
        "mrp.production",
        string="Produced By MO",
        related="produced_in_run_id.mo_id",
        store=True,
        readonly=True,
        index=True,
    )
    consumed_in_run_id = fields.Many2one("tp.nesting.run", string="Consumed In Run", readonly=True, index=True)
    consumed_in_mo_id = fields.Many2one(
        "mrp.production",
        string="Consumed By MO",
        related="consumed_in_run_id.mo_id",
        store=True,
        readonly=True,
        index=True,
    )
    consumed_at = fields.Datetime(string="Consumed At", readonly=True, index=True)
