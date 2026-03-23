from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase


class TestPhase1SaleToMO(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.warehouse = cls.env.ref("stock.warehouse0")
        cls.partner = cls.env["res.partner"].create({"name": "Phase 1 Customer"})

        cls.component_product = cls.env["product.product"].create(
            {
                "name": "TP Component",
                "type": "consu",
                "sale_ok": False,
                "purchase_ok": True,
            }
        )
        cls.finished_product = cls.env["product.product"].create(
            {
                "name": "TP Panel",
                "type": "consu",
                "sale_ok": True,
                "purchase_ok": False,
                "list_price": 100.0,
                "route_ids": [
                    (
                        6,
                        0,
                        [
                            cls.warehouse.mto_pull_id.route_id.id,
                            cls.warehouse.manufacture_pull_id.route_id.id,
                        ],
                    )
                ],
            }
        )

        cls.bom = cls.env["mrp.bom"].create(
            {
                "product_tmpl_id": cls.finished_product.product_tmpl_id.id,
                "product_qty": 1.0,
                "bom_line_ids": [
                    (
                        0,
                        0,
                        {
                            "product_id": cls.component_product.id,
                            "product_qty": 1.0,
                        },
                    )
                ],
            }
        )

    def _create_order(self, line_vals):
        return self.env["sale.order"].create(
            {
                "partner_id": self.partner.id,
                "order_line": line_vals,
            }
        )

    def test_so_line_dimensions_persist_for_multiple_sizes(self):
        order = self._create_order(
            [
                (
                    0,
                    0,
                    {
                        "product_id": self.finished_product.id,
                        "product_uom_qty": 2,
                        "price_unit": 100,
                        "tp_width_mm": 1200,
                        "tp_height_mm": 800,
                    },
                ),
                (
                    0,
                    0,
                    {
                        "product_id": self.finished_product.id,
                        "product_uom_qty": 1,
                        "price_unit": 100,
                        "tp_width_mm": 600,
                        "tp_height_mm": 400,
                    },
                ),
            ]
        )

        lines = order.order_line.sorted("id")
        self.assertEqual(lines[0].tp_width_mm, 1200)
        self.assertEqual(lines[0].tp_height_mm, 800)
        self.assertEqual(lines[1].tp_width_mm, 600)
        self.assertEqual(lines[1].tp_height_mm, 400)

    def test_reject_non_positive_dimensions(self):
        with self.assertRaises(ValidationError):
            self._create_order(
                [
                    (
                        0,
                        0,
                        {
                            "product_id": self.finished_product.id,
                            "product_uom_qty": 1,
                            "price_unit": 100,
                            "tp_width_mm": 0,
                            "tp_height_mm": 500,
                        },
                    )
                ]
            )

        with self.assertRaises(ValidationError):
            self._create_order(
                [
                    (
                        0,
                        0,
                        {
                            "product_id": self.finished_product.id,
                            "product_uom_qty": 1,
                            "price_unit": 100,
                            "tp_width_mm": 500,
                            "tp_height_mm": -1,
                        },
                    )
                ]
            )

    def test_dimensions_reach_mo_from_so_line(self):
        order = self._create_order(
            [
                (
                    0,
                    0,
                    {
                        "product_id": self.finished_product.id,
                        "product_uom_qty": 3,
                        "price_unit": 100,
                        "tp_width_mm": 1500,
                        "tp_height_mm": 900,
                    },
                )
            ]
        )
        sale_line = order.order_line

        procurement_values = sale_line._prepare_procurement_values()
        mo_vals = self.warehouse.manufacture_pull_id._prepare_mo_vals(
            self.finished_product,
            sale_line.product_uom_qty,
            sale_line.product_uom_id,
            self.warehouse.lot_stock_id,
            sale_line.name,
            order.name,
            order.company_id,
            procurement_values,
            self.bom,
        )
        mo = self.env["mrp.production"].create(mo_vals)

        self.assertEqual(mo.x_tp_source_so_line_id, sale_line)
        self.assertEqual(len(mo.tp_cut_line_ids), 1)
        cut_line = mo.tp_cut_line_ids[0]
        self.assertEqual(cut_line.width_mm, sale_line.tp_width_mm)
        self.assertEqual(cut_line.height_mm, sale_line.tp_height_mm)
        self.assertEqual(cut_line.quantity, int(round(sale_line.product_uom_qty)))

    def test_each_panel_spec_is_its_own_so_line(self):
        order = self._create_order(
            [
                (
                    0,
                    0,
                    {
                        "product_id": self.finished_product.id,
                        "product_uom_qty": 2,
                        "price_unit": 100,
                        "tp_width_mm": 1200,
                        "tp_height_mm": 800,
                    },
                ),
                (
                    0,
                    0,
                    {
                        "product_id": self.finished_product.id,
                        "product_uom_qty": 1,
                        "price_unit": 100,
                        "tp_width_mm": 600,
                        "tp_height_mm": 400,
                    },
                ),
            ]
        )

        line_a, line_b = order.order_line.sorted("id")

        mo_vals_a = self.warehouse.manufacture_pull_id._prepare_mo_vals(
            self.finished_product,
            line_a.product_uom_qty,
            line_a.product_uom_id,
            self.warehouse.lot_stock_id,
            line_a.name,
            order.name,
            order.company_id,
            line_a._prepare_procurement_values(),
            self.bom,
        )
        mo_vals_b = self.warehouse.manufacture_pull_id._prepare_mo_vals(
            self.finished_product,
            line_b.product_uom_qty,
            line_b.product_uom_id,
            self.warehouse.lot_stock_id,
            line_b.name,
            order.name,
            order.company_id,
            line_b._prepare_procurement_values(),
            self.bom,
        )
        mo_a = self.env["mrp.production"].create(mo_vals_a)
        mo_b = self.env["mrp.production"].create(mo_vals_b)

        self.assertEqual(len(mo_a.tp_cut_line_ids), 1)
        self.assertEqual(mo_a.tp_cut_line_ids[0].width_mm, 1200)
        self.assertEqual(mo_a.tp_cut_line_ids[0].height_mm, 800)
        self.assertEqual(len(mo_b.tp_cut_line_ids), 1)
        self.assertEqual(mo_b.tp_cut_line_ids[0].width_mm, 600)
        self.assertEqual(mo_b.tp_cut_line_ids[0].height_mm, 400)

