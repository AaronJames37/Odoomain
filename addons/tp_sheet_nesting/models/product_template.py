from odoo import fields, models


class ProductTemplate(models.Model):
    _inherit = "product.template"

    tp_nesting_source_map_count = fields.Integer(
        string="Nesting Source Mappings",
        compute="_compute_tp_nesting_source_map_count",
    )

    def _compute_tp_nesting_source_map_count(self):
        mapping_model = self.env["tp.nesting.source.map"]
        variant_ids = self.mapped("product_variant_ids").ids
        grouped = mapping_model.read_group(
            [("demand_product_id", "in", variant_ids)],
            ["demand_product_id"],
            ["demand_product_id"],
        )
        by_variant = {
            row["demand_product_id"][0]: row["demand_product_id_count"]
            for row in grouped
            if row.get("demand_product_id")
        }
        for template in self:
            template.tp_nesting_source_map_count = sum(
                by_variant.get(variant.id, 0) for variant in template.product_variant_ids
            )

    def action_open_tp_nesting_source_maps(self):
        self.ensure_one()
        action = self.env.ref("tp_sheet_nesting.action_tp_nesting_source_map").read()[0]
        variants = self.product_variant_ids
        if len(variants) == 1:
            action["domain"] = [("demand_product_id", "=", variants.id)]
            action["context"] = {
                "default_demand_product_id": variants.id,
            }
        else:
            action["domain"] = [("demand_product_id", "in", variants.ids)]
            action["context"] = {}
        return action
