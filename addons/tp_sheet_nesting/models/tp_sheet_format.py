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
    tp_availability = fields.Selection(
        selection=[
            ("stock", "Internal Stock"),
            ("supplier_quote", "Supplier / Quote Only"),
        ],
        string="Availability",
        required=True,
        default="stock",
        help=(
            "Internal Stock formats are available to workshop nesting. "
            "Supplier / Quote Only formats are available to website quoting "
            "but are excluded from general-purpose workshop nesting."
        ),
    )
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

    @api.model
    def _tp_internal_nesting_domain(self):
        return [("active", "=", True), ("tp_availability", "=", "stock")]

    @api.model
    def _tp_quote_nesting_domain(self):
        return [("active", "=", True), ("tp_availability", "in", ["stock", "supplier_quote"])]

    def _tp_filter_internal_nesting(self):
        return self.filtered(lambda sheet: sheet.active and sheet.tp_availability == "stock")

    @api.model
    def _tp_nesting_stock_qty_by_product(self, products):
        products = products.sudo()
        if not products:
            return {}
        warehouse = self.env["stock.warehouse"].sudo().search(
            [("company_id", "=", self.env.company.id)],
            limit=1,
        )
        if not warehouse:
            return {}
        stock_location = warehouse.lot_stock_id
        return {
            product.id: float(product.with_context(location=stock_location.id).qty_available or 0.0)
            for product in products
        }

    def _tp_filter_in_stock_for_nesting(self):
        """Filter full-sheet formats by product stock for workshop nesting.

        Offcuts are not represented by tp.sheet.format and are intentionally not
        affected by this filter.
        """
        sheets = self._tp_filter_internal_nesting()
        if self.env.company.tp_nesting_ignore_sheet_stock:
            return sheets
        products = sheets.mapped("product_id")
        if not products:
            return self.browse()
        qty_by_product = self._tp_nesting_stock_qty_by_product(products)
        if not qty_by_product:
            return self.browse()
        return sheets.filtered(lambda sheet: qty_by_product.get(sheet.product_id.id, 0.0) >= 1.0)

    @api.constrains("width_mm", "height_mm")
    def _check_dimensions(self):
        for rec in self:
            if rec.width_mm <= 0 or rec.height_mm <= 0:
                raise ValidationError("Sheet format dimensions must be greater than 0 mm.")
