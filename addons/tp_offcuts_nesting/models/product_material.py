from odoo import api, fields, models


TP_THICKNESS_SELECTION = [
    ("1", "1 mm"),
    ("1.5", "1.5 mm"),
    ("2", "2 mm"),
    ("3", "3 mm"),
    ("4.5", "4.5 mm"),
    ("5", "5 mm"),
    ("6", "6 mm"),
    ("8", "8 mm"),
    ("10", "10 mm"),
    ("12", "12 mm"),
    ("15", "15 mm"),
    ("18", "18 mm"),
    ("20", "20 mm"),
    ("25", "25 mm"),
]


class ProductTemplate(models.Model):
    _inherit = "product.template"

    tp_material_type = fields.Char(string="Material Type")
    tp_thickness_option = fields.Selection(
        selection=TP_THICKNESS_SELECTION,
        string="Thickness",
        help="Select nominal material thickness for matching and MO grouping.",
    )
    tp_thickness_mm = fields.Float(string="Thickness (mm)")
    tp_sheet_width_mm = fields.Integer(
        string="Sheet Width (mm)",
        help="Physical stock sheet width used by nesting when this SKU is a full-sheet source.",
    )
    tp_sheet_height_mm = fields.Integer(
        string="Sheet Height (mm)",
        help="Physical stock sheet height used by nesting when this SKU is a full-sheet source.",
    )
    tp_colour = fields.Char(string="Colour")
    tp_finish = fields.Char(string="Finish")
    tp_protective_film = fields.Selection(
        selection=[("paper", "Paper"), ("plastic", "Plastic"), ("none", "None")],
        string="Protective Film",
        default="none",
    )
    tp_brand_supplier = fields.Char(string="Brand/Supplier")

    @api.model
    def _tp_match_thickness_option(self, thickness_mm):
        if thickness_mm in (False, None):
            return False
        try:
            value = float(thickness_mm)
        except (TypeError, ValueError):
            return False
        for option, _label in TP_THICKNESS_SELECTION:
            if abs(float(option) - value) < 1e-6:
                return option
        return False

    @api.model
    def _tp_sync_thickness_vals(self, vals):
        if "tp_thickness_option" in vals:
            option = vals.get("tp_thickness_option")
            vals["tp_thickness_mm"] = float(option) if option else 0.0
            return vals
        if "tp_thickness_mm" in vals:
            matched = self._tp_match_thickness_option(vals.get("tp_thickness_mm"))
            vals["tp_thickness_option"] = matched or False
        return vals

    @api.model_create_multi
    def create(self, vals_list):
        vals_list = [self._tp_sync_thickness_vals(dict(vals)) for vals in vals_list]
        return super().create(vals_list)

    def write(self, vals):
        return super().write(self._tp_sync_thickness_vals(dict(vals)))

    @api.onchange("tp_thickness_option")
    def _onchange_tp_thickness_option(self):
        for rec in self:
            rec.tp_thickness_mm = float(rec.tp_thickness_option) if rec.tp_thickness_option else 0.0

    @api.onchange("tp_thickness_mm")
    def _onchange_tp_thickness_mm(self):
        for rec in self:
            rec.tp_thickness_option = rec._tp_match_thickness_option(rec.tp_thickness_mm) or False


class ProductProduct(models.Model):
    _inherit = "product.product"

    tp_material_type = fields.Char(
        string="Material Type",
        related="product_tmpl_id.tp_material_type",
        store=True,
        readonly=False,
    )
    tp_thickness_option = fields.Selection(
        related="product_tmpl_id.tp_thickness_option",
        store=True,
        readonly=False,
    )
    tp_thickness_mm = fields.Float(
        string="Thickness (mm)",
        related="product_tmpl_id.tp_thickness_mm",
        store=True,
        readonly=False,
    )
    tp_sheet_width_mm = fields.Integer(related="product_tmpl_id.tp_sheet_width_mm", store=True, readonly=False)
    tp_sheet_height_mm = fields.Integer(related="product_tmpl_id.tp_sheet_height_mm", store=True, readonly=False)
    tp_colour = fields.Char(string="Colour", related="product_tmpl_id.tp_colour", store=True, readonly=False)
    tp_finish = fields.Char(string="Finish", related="product_tmpl_id.tp_finish", store=True, readonly=False)
    tp_protective_film = fields.Selection(
        string="Protective Film",
        related="product_tmpl_id.tp_protective_film",
        store=True,
        readonly=False,
    )
    tp_brand_supplier = fields.Char(
        string="Brand/Supplier",
        related="product_tmpl_id.tp_brand_supplier",
        store=True,
        readonly=False,
    )
