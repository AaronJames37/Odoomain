from odoo import models


class StockRule(models.Model):
    _inherit = "stock.rule"

    def _prepare_mo_vals(
        self,
        product_id,
        product_qty,
        product_uom,
        location_dest_id,
        name,
        origin,
        company_id,
        values,
        bom,
    ):
        mo_vals = super()._prepare_mo_vals(
            product_id,
            product_qty,
            product_uom,
            location_dest_id,
            name,
            origin,
            company_id,
            values,
            bom,
        )
        if values.get("sale_line_id"):
            cut_line_commands = []
            width_mm = int(values.get("tp_width_mm") or 0)
            height_mm = int(values.get("tp_height_mm") or 0)
            quantity = int(round(values.get("tp_quantity") or 0.0))
            if width_mm > 0 and height_mm > 0 and quantity > 0:
                cut_line_commands.append(
                    (
                        0,
                        0,
                        {
                            "source_so_line_id": values["sale_line_id"],
                            "width_mm": width_mm,
                            "height_mm": height_mm,
                            "quantity": quantity,
                        },
                    )
                )
            mo_vals.update(
                {
                    "x_tp_source_so_line_id": values["sale_line_id"],
                    "tp_cut_line_ids": cut_line_commands,
                }
            )
        return mo_vals
