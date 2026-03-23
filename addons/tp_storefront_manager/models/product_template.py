import re

from odoo import api, fields, models


class ProductTemplate(models.Model):
    _inherit = "product.template"

    tp_is_top_level_product = fields.Boolean(
        string="Top Level Product",
        help="Enable this for catalog products that represent configurable cut-to-size items.",
    )
    tp_use_price_per_sqm = fields.Boolean(
        string="Use Price per m2",
        help="When enabled on a top level product, pricing is based on entered dimensions and mapped thickness price per m2.",
    )
    tp_price_per_sqm = fields.Float(
        string="Price per m2 (Deprecated)",
        compute="_compute_tp_price_per_sqm",
        digits="Product Price",
        readonly=True,
        help="Deprecated fallback field kept for compatibility.",
    )
    tp_sheet_area_sqm = fields.Float(
        string="Sheet Area (m2)",
        compute="_compute_tp_sheet_area_sqm",
    )
    tp_estimated_full_sheet_price = fields.Float(
        string="Estimated Full Sheet Price",
        digits="Product Price",
        compute="_compute_tp_estimated_full_sheet_price",
        help="Computed as sheet area multiplied by the first mapped thickness price per m2.",
    )
    tp_top_level_thickness_map_ids = fields.One2many(
        "tp.top.level.thickness.map",
        "top_level_template_id",
        string="Thickness Mappings",
    )
    tp_top_level_thickness_count = fields.Integer(
        string="Thickness Options",
        compute="_compute_tp_top_level_thickness_count",
    )
    tp_storefront_short_description = fields.Text(string="Storefront Short Description")
    tp_seo_title = fields.Char(string="SEO Title")
    tp_meta_description = fields.Text(string="Meta Description")
    tp_meta_keywords = fields.Char(string="Meta Keywords")
    tp_seo_slug = fields.Char(string="SEO Slug")
    tp_canonical_url = fields.Char(string="Canonical URL")
    tp_robots_index = fields.Boolean(string="Robots: Index", default=True)
    tp_robots_follow = fields.Boolean(string="Robots: Follow", default=True)
    tp_og_title = fields.Char(string="Open Graph Title")
    tp_og_description = fields.Text(string="Open Graph Description")
    tp_og_image_url = fields.Char(string="Open Graph Image URL")
    tp_twitter_title = fields.Char(string="Twitter Title")
    tp_twitter_description = fields.Text(string="Twitter Description")
    tp_schema_product_enabled = fields.Boolean(string="Enable Product Schema (JSON-LD)", default=True)

    @api.depends("tp_sheet_width_mm", "tp_sheet_height_mm")
    def _compute_tp_sheet_area_sqm(self):
        for template in self:
            width_mm = template.tp_sheet_width_mm or 0
            height_mm = template.tp_sheet_height_mm or 0
            template.tp_sheet_area_sqm = (width_mm * height_mm) / 1_000_000.0 if width_mm and height_mm else 0.0

    @api.depends(
        "tp_sheet_area_sqm",
        "tp_top_level_thickness_map_ids.effective_price_per_sqm",
        "tp_top_level_thickness_map_ids.sequence",
    )
    def _compute_tp_estimated_full_sheet_price(self):
        for template in self:
            default_map = template.tp_top_level_thickness_map_ids.sorted(lambda rec: (rec.sequence, rec.id))[:1]
            default_price = default_map.effective_price_per_sqm if default_map else 0.0
            template.tp_estimated_full_sheet_price = (template.tp_sheet_area_sqm or 0.0) * default_price

    @api.depends(
        "tp_top_level_thickness_map_ids",
        "tp_top_level_thickness_map_ids.effective_price_per_sqm",
        "tp_top_level_thickness_map_ids.sequence",
    )
    def _compute_tp_price_per_sqm(self):
        for template in self:
            default_map = template.tp_top_level_thickness_map_ids.sorted(lambda rec: (rec.sequence, rec.id))[:1]
            template.tp_price_per_sqm = default_map.effective_price_per_sqm if default_map else 0.0

    @api.onchange("tp_is_top_level_product")
    def _onchange_tp_is_top_level_product(self):
        for template in self:
            if not template.tp_is_top_level_product:
                template.tp_use_price_per_sqm = False

    @api.onchange(
        "tp_use_price_per_sqm",
        "tp_sheet_area_sqm",
        "tp_top_level_thickness_map_ids",
        "tp_top_level_thickness_map_ids.effective_price_per_sqm",
    )
    def _onchange_tp_mapping_price(self):
        for template in self:
            if (
                template.tp_is_top_level_product
                and template.tp_use_price_per_sqm
                and template.tp_estimated_full_sheet_price > 0
            ):
                template.list_price = template.tp_estimated_full_sheet_price

    def _compute_tp_top_level_thickness_count(self):
        for template in self:
            template.tp_top_level_thickness_count = len(template.tp_top_level_thickness_map_ids)

    @staticmethod
    def _tp_slugify(value):
        return re.sub(r"^-+|-+$", "", re.sub(r"[^a-z0-9]+", "-", (value or "").lower()))

    @api.onchange("name", "description_sale")
    def _onchange_tp_seo_defaults(self):
        for template in self:
            if not template.tp_seo_title:
                template.tp_seo_title = template.name
            if not template.tp_seo_slug:
                template.tp_seo_slug = self._tp_slugify(template.name)
            if not template.tp_og_title:
                template.tp_og_title = template.tp_seo_title
            if not template.tp_meta_description and template.description_sale:
                template.tp_meta_description = template.description_sale
            if not template.tp_og_description:
                template.tp_og_description = template.tp_meta_description
            if not template.tp_twitter_title:
                template.tp_twitter_title = template.tp_og_title
            if not template.tp_twitter_description:
                template.tp_twitter_description = template.tp_og_description
