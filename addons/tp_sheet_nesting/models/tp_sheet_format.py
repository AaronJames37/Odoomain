from odoo import api, fields, models
from odoo.exceptions import ValidationError


class TpSheetFormat(models.Model):
    _name = "tp.sheet.format"
    _description = "TP Sheet Format"
    _order = "id desc"

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    product_id = fields.Many2one("product.product", required=True, ondelete="restrict")
    width_mm = fields.Integer(required=True)
    height_mm = fields.Integer(required=True)
    area_mm2 = fields.Float(compute="_compute_area_mm2", store=True)
    currency_id = fields.Many2one(
        "res.currency",
        required=True,
        default=lambda self: self.env.company.currency_id.id,
    )
    landed_cost = fields.Monetary(currency_field="currency_id", default=0.0)

    tp_material_type = fields.Char(string="Material Type")
    tp_thickness_mm = fields.Float(string="Thickness (mm)")
    tp_colour = fields.Char(string="Colour")
    tp_finish = fields.Char(string="Finish")
    tp_protective_film = fields.Selection(
        selection=[("paper", "Paper"), ("plastic", "Plastic"), ("none", "None")],
        string="Protective Film",
        default="none",
    )
    tp_brand_supplier = fields.Char(string="Brand/Supplier")

    def _compute_area_mm2(self):
        for rec in self:
            rec.area_mm2 = float(rec.width_mm * rec.height_mm)

    @api.constrains("width_mm", "height_mm")
    def _check_dimensions(self):
        for rec in self:
            if rec.width_mm <= 0 or rec.height_mm <= 0:
                raise ValidationError("Sheet format dimensions must be greater than 0 mm.")
