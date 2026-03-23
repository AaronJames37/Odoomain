from odoo import api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools.float_utils import float_round


class TpTopLevelThicknessMap(models.Model):
    _name = "tp.top.level.thickness.map"
    _description = "Top Level Product Thickness Mapping"
    _order = "sequence asc, thickness_mm asc, id asc"
    _rec_name = "thickness_label"

    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)
    top_level_template_id = fields.Many2one(
        "product.template",
        required=True,
        ondelete="cascade",
        domain=[("tp_is_top_level_product", "=", True)],
    )
    source_product_id = fields.Many2one(
        "product.product",
        required=True,
        domain=[("tp_sheet_width_mm", ">", 0), ("tp_sheet_height_mm", ">", 0)],
        help="Backend full-sheet/source product used behind the scenes.",
    )
    thickness_label = fields.Char(
        string="Thickness",
        compute="_compute_thickness_fields",
        store=True,
    )
    thickness_mm = fields.Float(
        string="Thickness (mm)",
        compute="_compute_thickness_fields",
        store=True,
        digits=(16, 3),
    )
    source_sheet_area_sqm = fields.Float(
        string="Source Sheet Area (m2)",
        compute="_compute_source_sheet_area_sqm",
        digits=(16, 6),
    )
    source_sheet_sales_price = fields.Float(
        string="Source Sheet Sales Price",
        compute="_compute_source_sheet_sales_price",
        readonly=True,
        digits="Product Price",
    )
    effective_price_per_sqm = fields.Float(
        string="Price per m2",
        compute="_compute_effective_price_per_sqm",
        digits="Product Price",
    )
    price_per_sqm = fields.Float(
        string="Price per m2 (Deprecated)",
        compute="_compute_effective_price_per_sqm",
        readonly=True,
        digits="Product Price",
        help="Deprecated compatibility field mirrored from effective price per m2.",
    )
    note = fields.Char()
    company_id = fields.Many2one(
        "res.company",
        related="top_level_template_id.company_id",
        store=True,
        readonly=True,
    )

    _sql_constraints = [
        (
            "tp_top_level_thickness_source_unique",
            "unique(top_level_template_id, source_product_id)",
            "This source product is already mapped for the top level product.",
        ),
    ]

    @api.depends("source_product_id.tp_thickness_option", "source_product_id.tp_thickness_mm")
    def _compute_thickness_fields(self):
        for rec in self:
            option = rec.source_product_id.tp_thickness_option if rec.source_product_id else False
            mm = rec.source_product_id.tp_thickness_mm if rec.source_product_id else 0.0
            if (not mm) and option:
                try:
                    mm = float(option)
                except (TypeError, ValueError):
                    mm = 0.0
            rec.thickness_mm = mm or 0.0
            if option:
                rec.thickness_label = f"{option}mm"
            elif mm:
                rec.thickness_label = f"{mm:g}mm"
            else:
                rec.thickness_label = "N/A"

    @api.depends(
        "source_product_id.tp_sheet_width_mm",
        "source_product_id.tp_sheet_height_mm",
        "source_product_id.product_tmpl_id.tp_sheet_width_mm",
        "source_product_id.product_tmpl_id.tp_sheet_height_mm",
    )
    def _compute_source_sheet_area_sqm(self):
        for rec in self:
            width = (
                rec.source_product_id.tp_sheet_width_mm
                or rec.source_product_id.product_tmpl_id.tp_sheet_width_mm
                or 0
            )
            height = (
                rec.source_product_id.tp_sheet_height_mm
                or rec.source_product_id.product_tmpl_id.tp_sheet_height_mm
                or 0
            )
            rec.source_sheet_area_sqm = (width * height) / 1_000_000.0 if width and height else 0.0

    @api.depends("source_product_id.lst_price", "source_product_id.product_tmpl_id.list_price")
    def _compute_source_sheet_sales_price(self):
        for rec in self:
            template_price = rec.source_product_id.product_tmpl_id.list_price
            variant_price = rec.source_product_id.lst_price
            rec.source_sheet_sales_price = template_price if template_price not in (False, None) else (variant_price or 0.0)

    @api.depends("source_sheet_sales_price", "source_sheet_area_sqm")
    def _compute_effective_price_per_sqm(self):
        for rec in self:
            computed = (
                rec.source_sheet_sales_price / rec.source_sheet_area_sqm
                if rec.source_sheet_area_sqm
                else 0.0
            )
            rounded = float_round(computed, precision_digits=2)
            rec.effective_price_per_sqm = rounded
            rec.price_per_sqm = rounded

    @api.constrains("top_level_template_id")
    def _check_top_level_template(self):
        for rec in self:
            if not rec.top_level_template_id.tp_is_top_level_product:
                raise ValidationError("Thickness mappings are only allowed on top level products.")
