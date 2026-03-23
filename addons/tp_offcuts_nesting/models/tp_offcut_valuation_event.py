from odoo import fields, models


class TpOffcutValuationEvent(models.Model):
    _name = "tp.offcut.valuation.event"
    _description = "TP Offcut Valuation Event"
    _order = "id desc"

    name = fields.Char(required=True, copy=False, default="New")
    event_type = fields.Selection(
        [
            ("sheet_to_offcut", "Sheet to Offcut"),
            ("offcut_to_remainder", "Offcut to Remainder"),
            ("waste", "Waste"),
        ],
        required=True,
    )
    offcut_id = fields.Many2one("tp.offcut")
    parent_offcut_id = fields.Many2one("tp.offcut")
    parent_lot_id = fields.Many2one("stock.lot", ondelete="set null")
    mo_id = fields.Many2one("mrp.production")
    currency_id = fields.Many2one(
        "res.currency",
        required=True,
        default=lambda self: self.env.company.currency_id.id,
    )

    input_area_mm2 = fields.Float()
    input_value = fields.Monetary(currency_field="currency_id")
    offcut_area_mm2 = fields.Float()
    offcut_value = fields.Monetary(currency_field="currency_id")
    waste_area_mm2 = fields.Float()
    waste_value = fields.Monetary(currency_field="currency_id")
    remainder_area_mm2 = fields.Float()
    remainder_value = fields.Monetary(currency_field="currency_id")
    is_conserved = fields.Boolean(default=True)
    conservation_delta = fields.Monetary(currency_field="currency_id")
    note = fields.Text()
