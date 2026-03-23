from odoo import fields, models


class ResCompany(models.Model):
    _inherit = "res.company"

    tp_waste_account_id = fields.Many2one(
        "account.account",
        string="Offcuts Waste Account",
    )
    tp_waste_journal_id = fields.Many2one(
        "account.journal",
        string="Offcuts Waste Journal",
        domain="[('type', '=', 'general')]",
    )
    tp_offcut_sold_bin_location_id = fields.Many2one(
        "stock.location",
        string="Offcuts Sold BIN",
        domain="[('usage', '=', 'internal')]",
    )
    tp_offcut_sold_cleanup_days = fields.Integer(
        string="Sold Offcut Cleanup Days",
        default=30,
    )
