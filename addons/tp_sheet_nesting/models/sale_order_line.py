from odoo import api, fields, models
from odoo.exceptions import ValidationError


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    tp_width_mm = fields.Integer(
        string="Width (mm)",
        required=True,
        default=1,
        index=True,
    )
    tp_height_mm = fields.Integer(
        string="Height (mm)",
        required=True,
        default=1,
        index=True,
    )
    tp_area_sqm = fields.Float(
        string="Area (m2)",
        compute="_compute_tp_area_sqm",
        store=True,
    )
    tp_is_top_level_product = fields.Boolean(
        compute="_compute_tp_top_level_flags",
        readonly=True,
    )
    tp_use_price_per_sqm = fields.Boolean(
        compute="_compute_tp_top_level_flags",
        readonly=True,
    )
    tp_thickness_map_id = fields.Many2one(
        "tp.top.level.thickness.map",
        string="Thickness Option",
        domain="[('top_level_template_id', '=', product_template_id)]",
    )

    @api.constrains("tp_width_mm", "tp_height_mm")
    def _check_tp_dimensions_positive(self):
        for line in self:
            if line.tp_width_mm <= 0:
                raise ValidationError("Width must be greater than 0 mm.")
            if line.tp_height_mm <= 0:
                raise ValidationError("Height must be greater than 0 mm.")

    @api.depends("tp_width_mm", "tp_height_mm")
    def _compute_tp_area_sqm(self):
        for line in self:
            line.tp_area_sqm = (line.tp_width_mm * line.tp_height_mm) / 1_000_000.0

    @api.depends("product_template_id")
    def _compute_tp_top_level_flags(self):
        for line in self:
            template = line.product_template_id
            line.tp_is_top_level_product = bool(getattr(template, "tp_is_top_level_product", False))
            line.tp_use_price_per_sqm = bool(getattr(template, "tp_use_price_per_sqm", False))

    def _tp_compute_sqm_price_unit(self):
        self.ensure_one()
        if not (self.tp_is_top_level_product and self.tp_use_price_per_sqm):
            return None
        mapped_price = self.tp_thickness_map_id.effective_price_per_sqm if self.tp_thickness_map_id else 0.0
        if self.tp_area_sqm <= 0 or mapped_price <= 0:
            return 0.0
        return self.tp_area_sqm * mapped_price

    def _tp_apply_sqm_pricing(self):
        for line in self:
            if (
                line.tp_is_top_level_product
                and line.product_template_id.tp_top_level_thickness_map_ids
                and not line.tp_thickness_map_id
            ):
                line.tp_thickness_map_id = line.product_template_id.tp_top_level_thickness_map_ids[0]
            new_price = line._tp_compute_sqm_price_unit()
            if new_price is not None:
                line.with_context(tp_skip_sqm_pricing=True).price_unit = new_price

    @api.onchange("product_id", "tp_width_mm", "tp_height_mm", "product_uom_qty", "tp_thickness_map_id")
    def _onchange_tp_apply_sqm_pricing(self):
        self._tp_apply_sqm_pricing()

    def _prepare_procurement_values(self):
        values = super()._prepare_procurement_values()
        self.ensure_one()
        values.update(
            {
                "tp_width_mm": self.tp_width_mm,
                "tp_height_mm": self.tp_height_mm,
                "tp_quantity": self.product_uom_qty,
                "tp_thickness_map_id": self.tp_thickness_map_id.id,
            }
        )
        return values

    @api.model_create_multi
    def create(self, vals_list):
        lines = super().create(vals_list)
        if not self.env.context.get("tp_skip_sqm_pricing"):
            lines._tp_apply_sqm_pricing()
        return lines

    def write(self, vals):
        res = super().write(vals)
        if self.env.context.get("tp_skip_sqm_pricing"):
            return res

        watched_keys = {"product_id", "tp_width_mm", "tp_height_mm", "product_uom_qty", "tp_thickness_map_id"}
        if watched_keys.intersection(vals.keys()):
            self._tp_apply_sqm_pricing()
        return res
