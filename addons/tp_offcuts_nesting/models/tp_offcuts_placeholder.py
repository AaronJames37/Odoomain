from odoo import fields, models


class TpOffcutsPlaceholder(models.Model):
    _name = "tp.offcuts.placeholder"
    _description = "TP Offcuts Placeholder"

    name = fields.Char(required=True, default="Placeholder")
