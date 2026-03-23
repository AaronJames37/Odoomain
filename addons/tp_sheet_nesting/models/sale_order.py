from odoo import models


class SaleOrder(models.Model):
    _inherit = "sale.order"

    @staticmethod
    def _tp_group_key_from_product(product):
        product_tmpl = product.product_tmpl_id if product else False
        field_names = [
            "tp_thickness_mm",
            "tp_material_type",
            "tp_colour",
            "tp_finish",
            "tp_protective_film",
            "tp_brand_supplier",
        ]
        attrs = []
        for field_name in field_names:
            value = False
            if product and field_name in product._fields:
                value = product[field_name]
            if not value and product_tmpl and field_name in product_tmpl._fields:
                value = product_tmpl[field_name]
            attrs.append(value or False)
        return (product.id, *attrs)

    def _tp_group_key_from_mo(self, mo):
        return self._tp_group_key_from_product(mo.product_id)

    def _tp_group_key_from_so_line(self, line):
        return self._tp_group_key_from_product(line.product_id)

    def _tp_rebuild_keeper_cut_lines(self, keeper_mo, source_lines):
        keeper_mo.tp_cut_line_ids.unlink()
        create_vals = []
        for line in source_lines:
            qty = int(round(line.product_uom_qty or 0.0))
            if qty <= 0 or line.tp_width_mm <= 0 or line.tp_height_mm <= 0:
                continue
            create_vals.append(
                {
                    "mo_id": keeper_mo.id,
                    "source_so_line_id": line.id,
                    "width_mm": line.tp_width_mm,
                    "height_mm": line.tp_height_mm,
                    "quantity": qty,
                }
            )
        if create_vals:
            self.env["tp.mo.cut.line"].create(create_vals)

    def _tp_consolidate_cut_to_size_mos(self):
        mrp_production = self.env["mrp.production"]
        for order in self:
            mos = mrp_production.search(
                [
                    ("origin", "=", order.name),
                    ("state", "not in", ["done", "cancel"]),
                    ("x_tp_source_so_line_id", "!=", False),
                ],
                order="id asc",
            )
            mo_groups = {}
            for mo in mos:
                key = self._tp_group_key_from_mo(mo)
                mo_groups.setdefault(key, self.env["mrp.production"])
                mo_groups[key] |= mo

            so_line_groups = {}
            for line in order.order_line.filtered(
                lambda l: l.product_id and l.tp_width_mm > 0 and l.tp_height_mm > 0 and l.product_uom_qty > 0
            ):
                key = self._tp_group_key_from_so_line(line)
                so_line_groups.setdefault(key, self.env["sale.order.line"])
                so_line_groups[key] |= line

            for key, source_lines in so_line_groups.items():
                grouped_mos = mo_groups.get(key)
                if not grouped_mos:
                    continue
                keeper = grouped_mos[0]
                self._tp_rebuild_keeper_cut_lines(keeper, source_lines.sorted("id"))
                for redundant in grouped_mos[1:]:
                    if redundant.state not in ("done", "cancel"):
                        redundant.action_cancel()
                    if redundant.state == "cancel":
                        redundant.unlink()

    def action_confirm(self):
        res = super().action_confirm()
        self._tp_consolidate_cut_to_size_mos()
        return res
