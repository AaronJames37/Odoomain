from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    tp_waste_account_id = fields.Many2one(
        "account.account",
        related="company_id.tp_waste_account_id",
        readonly=False,
    )
    tp_waste_journal_id = fields.Many2one(
        "account.journal",
        related="company_id.tp_waste_journal_id",
        readonly=False,
    )
    tp_offcut_sold_bin_location_id = fields.Many2one(
        "stock.location",
        related="company_id.tp_offcut_sold_bin_location_id",
        readonly=False,
    )
    tp_offcut_sold_cleanup_days = fields.Integer(
        related="company_id.tp_offcut_sold_cleanup_days",
        readonly=False,
    )
