from odoo.tests.common import TransactionCase


class TestPhase51MoConsolidation(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.warehouse = cls.env.ref("stock.warehouse0")
        cls.partner = cls.env["res.partner"].create({"name": "Phase 5.1 Customer"})
        cls.component = cls.env["product.product"].create(
            {
                "name": "P5.1 Component",
                "type": "consu",
                "sale_ok": False,
                "purchase_ok": True,
            }
        )
        cls.product_3mm = cls.env["product.product"].create(
            {
                "name": "Clear Acrylic 3mm CTS",
                "type": "consu",
                "sale_ok": True,
                "tracking": "lot",
            }
        )
        cls.product_5mm = cls.env["product.product"].create(
            {
                "name": "Clear Acrylic 5mm CTS",
                "type": "consu",
                "sale_ok": True,
                "tracking": "lot",
            }
        )
        cls.env["mrp.bom"].create(
            {
                "product_tmpl_id": cls.product_3mm.product_tmpl_id.id,
                "product_qty": 1.0,
                "bom_line_ids": [
                    (0, 0, {"product_id": cls.component.id, "product_qty": 1.0}),
                ],
            }
        )
        cls.env["mrp.bom"].create(
            {
                "product_tmpl_id": cls.product_5mm.product_tmpl_id.id,
                "product_qty": 1.0,
                "bom_line_ids": [
                    (0, 0, {"product_id": cls.component.id, "product_qty": 1.0}),
                ],
            }
        )

    def _create_mo_for_line(self, order, line, product):
        bom = self.env["mrp.bom"].search([("product_tmpl_id", "=", product.product_tmpl_id.id)], limit=1)
        mo_vals = self.warehouse.manufacture_pull_id._prepare_mo_vals(
            product,
            line.product_uom_qty,
            line.product_uom_id,
            self.warehouse.lot_stock_id,
            line.name,
            order.name,
            order.company_id,
            line._prepare_procurement_values(),
            bom,
        )
        return self.env["mrp.production"].create(mo_vals)

    def test_same_sku_lines_consolidate_to_one_mo(self):
        order = self.env["sale.order"].create(
            {
                "partner_id": self.partner.id,
                "order_line": [
                    (
                        0,
                        0,
                        {
                            "product_id": self.product_3mm.id,
                            "product_uom_qty": 2,
                            "price_unit": 10.0,
                            "tp_width_mm": 400,
                            "tp_height_mm": 300,
                        },
                    ),
                    (
                        0,
                        0,
                        {
                            "product_id": self.product_3mm.id,
                            "product_uom_qty": 1,
                            "price_unit": 10.0,
                            "tp_width_mm": 1200,
                            "tp_height_mm": 600,
                        },
                    ),
                ],
            }
        )
        line_a, line_b = order.order_line.sorted("id")
        self._create_mo_for_line(order, line_a, self.product_3mm)
        self._create_mo_for_line(order, line_b, self.product_3mm)
        order._tp_consolidate_cut_to_size_mos()

        mos = self.env["mrp.production"].search(
            [
                ("origin", "=", order.name),
                ("product_id", "=", self.product_3mm.id),
                ("state", "!=", "cancel"),
            ]
        )
        self.assertEqual(len(mos), 1)
        self.assertEqual(len(mos.tp_cut_line_ids), 2)
        summary = mos.tp_scope_cut_summary or ""
        self.assertIn("400 x 300 mm x 2", summary)
        self.assertIn("1200 x 600 mm x 1", summary)

    def test_single_existing_mo_still_imports_all_group_lines(self):
        order = self.env["sale.order"].create(
            {
                "partner_id": self.partner.id,
                "order_line": [
                    (
                        0,
                        0,
                        {
                            "product_id": self.product_3mm.id,
                            "product_uom_qty": 2,
                            "price_unit": 10.0,
                            "tp_width_mm": 400,
                            "tp_height_mm": 300,
                        },
                    ),
                    (
                        0,
                        0,
                        {
                            "product_id": self.product_3mm.id,
                            "product_uom_qty": 1,
                            "price_unit": 10.0,
                            "tp_width_mm": 1200,
                            "tp_height_mm": 600,
                        },
                    ),
                ],
            }
        )
        first_line = order.order_line.sorted("id")[0]
        mo = self._create_mo_for_line(order, first_line, self.product_3mm)

        order._tp_consolidate_cut_to_size_mos()

        self.assertEqual(len(mo.tp_cut_line_ids), 2)
        self.assertIn("400 x 300 mm x 2", mo.tp_scope_cut_summary or "")
        self.assertIn("1200 x 600 mm x 1", mo.tp_scope_cut_summary or "")

    def test_mixed_thickness_creates_separate_mos(self):
        order = self.env["sale.order"].create(
            {
                "partner_id": self.partner.id,
                "order_line": [
                    (
                        0,
                        0,
                        {
                            "product_id": self.product_3mm.id,
                            "product_uom_qty": 1,
                            "price_unit": 10.0,
                            "tp_width_mm": 500,
                            "tp_height_mm": 300,
                        },
                    ),
                    (
                        0,
                        0,
                        {
                            "product_id": self.product_5mm.id,
                            "product_uom_qty": 1,
                            "price_unit": 10.0,
                            "tp_width_mm": 500,
                            "tp_height_mm": 300,
                        },
                    ),
                ],
            }
        )
        line_a, line_b = order.order_line.sorted("id")
        self._create_mo_for_line(order, line_a, self.product_3mm)
        self._create_mo_for_line(order, line_b, self.product_5mm)
        order._tp_consolidate_cut_to_size_mos()

        mo_3mm = self.env["mrp.production"].search(
            [
                ("origin", "=", order.name),
                ("product_id", "=", self.product_3mm.id),
                ("state", "!=", "cancel"),
            ]
        )
        mo_5mm = self.env["mrp.production"].search(
            [
                ("origin", "=", order.name),
                ("product_id", "=", self.product_5mm.id),
                ("state", "!=", "cancel"),
            ]
        )
        self.assertEqual(len(mo_3mm), 1)
        self.assertEqual(len(mo_5mm), 1)

