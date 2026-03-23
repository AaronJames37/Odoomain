from odoo import api, fields, models
from odoo.exceptions import ValidationError


class TpMoCutLine(models.Model):
    _name = "tp.mo.cut.line"
    _description = "TP MO Cut Line"
    _order = "id asc"

    mo_id = fields.Many2one("mrp.production", required=True, ondelete="cascade", index=True)
    source_so_line_id = fields.Many2one("sale.order.line", ondelete="set null")
    width_mm = fields.Integer(required=True)
    height_mm = fields.Integer(required=True)
    quantity = fields.Integer(required=True, default=1)

    @api.constrains("width_mm", "height_mm", "quantity")
    def _check_positive_values(self):
        for rec in self:
            if rec.width_mm <= 0 or rec.height_mm <= 0 or rec.quantity <= 0:
                raise ValidationError("MO cut line width, height, and quantity must be positive.")
