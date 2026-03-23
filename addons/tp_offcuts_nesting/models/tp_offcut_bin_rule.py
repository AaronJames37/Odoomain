from odoo import fields, models


class TpOffcutBinRule(models.Model):
    _name = "tp.offcut.bin.rule"
    _description = "TP Offcut BIN Rule"
    _order = "sequence, id"

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)
    min_width_mm = fields.Integer(default=0, required=True)
    min_height_mm = fields.Integer(default=0, required=True)
    bin_location_id = fields.Many2one(
        "stock.location",
        required=True,
        domain="[('usage', '=', 'internal')]",
    )
