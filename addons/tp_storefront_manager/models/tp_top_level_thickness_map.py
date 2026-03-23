from odoo import api, fields, models


class TpTopLevelThicknessMap(models.Model):
    _name = "tp.top.level.thickness.map"
    _description = "Top Level Thickness Mapping"
    _order = "sequence, id"

    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    top_level_template_id = fields.Many2one(
        "product.template",
        required=True,
        ondelete="cascade",
        index=True,
    )
    source_product_id = fields.Many2one(
        "product.template",
        required=True,
        domain=[("sale_ok", "=", True)],
        help="Source full-sheet SKU used to derive thickness and price per m2.",
    )
    thickness_label = fields.Char(
        compute="_compute_source_metrics",
        store=True,
    )
    source_sheet_area_sqm = fields.Float(
        string="Source Sheet Area (m2)",
        compute="_compute_source_metrics",
        store=True,
    )
    source_sheet_sales_price = fields.Float(
        string="Source Sheet Sales Price",
        digits="Product Price",
        compute="_compute_source_metrics",
        store=True,
    )
    effective_price_per_sqm = fields.Float(
        string="Effective Price per m2",
        digits="Product Price",
        compute="_compute_source_metrics",
        store=True,
    )
    note = fields.Char()

    @api.depends(
        "source_product_id",
        "source_product_id.tp_thickness_option",
        "source_product_id.tp_thickness_mm",
        "source_product_id.tp_sheet_width_mm",
        "source_product_id.tp_sheet_height_mm",
        "source_product_id.list_price",
    )
    def _compute_source_metrics(self):
        for rec in self:
            product = rec.source_product_id
            if not product:
                rec.thickness_label = False
                rec.source_sheet_area_sqm = 0.0
                rec.source_sheet_sales_price = 0.0
                rec.effective_price_per_sqm = 0.0
                continue

            width_mm = float(product.tp_sheet_width_mm or 0)
            height_mm = float(product.tp_sheet_height_mm or 0)
            area_sqm = (width_mm * height_mm) / 1_000_000.0 if width_mm and height_mm else 0.0
            sheet_price = float(product.list_price or 0.0)
            thickness_option = product.tp_thickness_option
            thickness_mm = float(product.tp_thickness_mm or 0.0)

            if thickness_option:
                rec.thickness_label = "%s mm" % thickness_option
            elif thickness_mm:
                rec.thickness_label = "%s mm" % ("{:g}".format(thickness_mm))
            else:
                rec.thickness_label = False

            rec.source_sheet_area_sqm = area_sqm
            rec.source_sheet_sales_price = sheet_price
            rec.effective_price_per_sqm = (sheet_price / area_sqm) if area_sqm > 0 else 0.0
