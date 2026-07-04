from odoo.tests.common import TransactionCase


class TestWebCutPartManifest(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.warehouse = cls.env.ref("stock.warehouse0")
        cls.partner = cls.env["res.partner"].create({"name": "Website Manifest Customer"})
        cls.component = cls.env["product.product"].create(
            {
                "name": "Manifest Component",
                "type": "consu",
                "sale_ok": False,
                "purchase_ok": True,
            }
        )
        cls.product = cls.env["product.product"].create(
            {
                "name": "Clear Acrylic 3mm Manifest",
                "type": "consu",
                "sale_ok": True,
                "purchase_ok": False,
                "tracking": "lot",
                "tp_material_type": "Acrylic",
                "tp_thickness_mm": 3.0,
                "tp_colour": "Clear",
                "tp_finish": "Gloss",
                "tp_protective_film": "paper",
                "tp_brand_supplier": "TP",
            }
        )
        cls.bom = cls.env["mrp.bom"].create(
            {
                "product_tmpl_id": cls.product.product_tmpl_id.id,
                "product_qty": 1.0,
                "bom_line_ids": [(0, 0, {"product_id": cls.component.id, "product_qty": 1.0})],
            }
        )

    def _create_order_and_mo(self):
        order = self.env["sale.order"].create(
            {
                "partner_id": self.partner.id,
                "order_line": [
                    (
                        0,
                        0,
                        {
                            "product_id": self.product.id,
                            "product_uom_qty": 2,
                            "price_unit": 10.0,
                            "tp_width_mm": 500,
                            "tp_height_mm": 300,
                        },
                    )
                ],
            }
        )
        line = order.order_line
        mo_vals = self.warehouse.manufacture_pull_id._prepare_mo_vals(
            self.product,
            line.product_uom_qty,
            line.product_uom_id,
            self.warehouse.lot_stock_id,
            line.name,
            order.name,
            order.company_id,
            line._prepare_procurement_values(),
            self.bom,
        )
        mo = self.env["mrp.production"].create(mo_vals)
        return order, line, mo

    def test_manifest_parts_drive_cut_lines_and_allocations(self):
        order, line, mo = self._create_order_and_mo()
        part_model = self.env["tp.web.cut.part"]
        part_a = part_model.create(
            {
                "partKey": f"{order.name}-L1-P1-I1",
                "saleOrderLineId": line.id,
                "lineIndex": 1,
                "panelIndex": 1,
                "instanceIndex": 1,
                "sku": "ACRYLIC-3MM-CLEAR",
                "material": "Acrylic",
                "colour": "Clear",
                "thickness": 3,
                "widthMm": 500,
                "heightMm": 300,
                "shape": "rectangle",
                "radii": {},
                "holes": [],
                "cutOuts": [],
                "customShape": {},
                "label": "Panel A",
            }
        )
        part_b = part_model.create(
            {
                "partKey": f"{order.name}-L1-P2-I1",
                "saleOrderLineId": line.id,
                "lineIndex": 1,
                "panelIndex": 2,
                "instanceIndex": 1,
                "sku": "ACRYLIC-3MM-CLEAR",
                "material": "Acrylic",
                "colour": "Clear",
                "thicknessMm": 3,
                "widthMm": 400,
                "heightMm": 250,
                "shape": "radiused_rectangle",
                "radii": {"topLeft": 20, "topRight": 20},
                "holes": [{"x": 50, "y": 80, "diameter": 6}],
                "cutOuts": [{"x": 100, "y": 40, "width": 80, "height": 30}],
                "customShape": {},
                "label": "Panel B",
            }
        )

        order._tp_consolidate_cut_to_size_mos()

        self.assertEqual(len(mo.tp_cut_line_ids), 2)
        self.assertEqual(set(mo.tp_cut_line_ids.mapped("source_web_cut_part_id").ids), {part_a.id, part_b.id})
        self.assertEqual(part_b.cut_outs[0]["width"], 80)
        self.assertEqual(part_b.holes[0]["diameter"], 6)

        self.env["tp.sheet.format"].create(
            {
                "name": "Manifest Sheet",
                "product_id": self.product.id,
                "width_mm": 1220,
                "height_mm": 2440,
                "landed_cost": 100.0,
                "tp_material_type": "Acrylic",
                "tp_thickness_mm": 3.0,
                "tp_colour": "Clear",
                "tp_finish": "Gloss",
                "tp_protective_film": "paper",
                "tp_brand_supplier": "TP",
            }
        )

        mo.action_run_tp_nesting()

        allocations = mo.tp_last_nesting_run_id.allocation_ids
        self.assertEqual(len(allocations), 2)
        self.assertEqual(set(allocations.mapped("web_cut_part_id").ids), {part_a.id, part_b.id})
        self.assertTrue(all(allocation.placed_x_mm >= 0 and allocation.placed_y_mm >= 0 for allocation in allocations))
