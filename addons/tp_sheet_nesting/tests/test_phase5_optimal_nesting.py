from odoo.tests.common import TransactionCase


class TestPhase5OptimalNesting(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.warehouse = cls.env.ref("stock.warehouse0")
        cls.partner = cls.env["res.partner"].create({"name": "Phase 5 Customer"})

        cls.component_product = cls.env["product.product"].create(
            {
                "name": "Phase 5 Component",
                "type": "consu",
                "sale_ok": False,
                "purchase_ok": True,
            }
        )
        cls.finished_product = cls.env["product.product"].create(
            {
                "name": "Phase 5 Panel",
                "type": "consu",
                "tracking": "lot",
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

        account_model = cls.env["account.account"]
        account_types = account_model._fields["account_type"].selection
        expense_type = next((key for key, _ in account_types if "expense" in key), account_types[0][0])
        asset_type = next((key for key, _ in account_types if "asset" in key), account_types[0][0])
        cls.waste_account = account_model.create(
            {
                "name": "Phase5 Waste Account",
                "code": "W5ST9",
                "account_type": expense_type,
                "company_ids": [(4, cls.company.id)],
            }
        )
        cls.inventory_account = account_model.create(
            {
                "name": "Phase5 Inventory Account",
                "code": "I5NV9",
                "account_type": asset_type,
                "company_ids": [(4, cls.company.id)],
            }
        )
        cls.finished_product.categ_id.property_stock_valuation_account_id = cls.inventory_account
        cls.journal = cls.env["account.journal"].search(
            [("company_id", "=", cls.company.id), ("type", "=", "general")],
            limit=1,
        )
        cls.company.tp_waste_account_id = cls.waste_account
        cls.company.tp_waste_journal_id = cls.journal

    def setUp(self):
        super().setUp()
        existing = self.env["tp.offcut"].search([("active", "=", True)])
        existing.write({"active": False, "state": "inactive"})

    @classmethod
    def _new_lot(cls, name):
        return cls.env["stock.lot"].create(
            {
                "name": name,
                "product_id": cls.finished_product.id,
                "company_id": cls.company.id,
            }
        )

    def _create_mo(self, *, width_mm, height_mm, quantity):
        order = self.env["sale.order"].create(
            {
                "partner_id": self.partner.id,
                "order_line": [
                    (
                        0,
                        0,
                        {
                            "product_id": self.finished_product.id,
                            "product_uom_qty": quantity,
                            "price_unit": 100.0,
                            "tp_width_mm": width_mm,
                            "tp_height_mm": height_mm,
                        },
                    )
                ],
            }
        )
        sale_line = order.order_line
        mo_vals = self.warehouse.manufacture_pull_id._prepare_mo_vals(
            self.finished_product,
            sale_line.product_uom_qty,
            sale_line.product_uom_id,
            self.warehouse.lot_stock_id,
            sale_line.name,
            order.name,
            order.company_id,
            sale_line._prepare_procurement_values(),
            self.bom,
        )
        return self.env["mrp.production"].create(mo_vals)

    def _create_sheet_format(self, *, name, width_mm, height_mm, landed_cost=100.0):
        return self.env["tp.sheet.format"].create(
            {
                "name": name,
                "product_id": self.finished_product.id,
                "width_mm": width_mm,
                "height_mm": height_mm,
                "landed_cost": landed_cost,
            }
        )

    def _create_offcut(self, *, name, width_mm, height_mm):
        parent_lot = self._new_lot(f"{name}-PARENT")
        return self.env["tp.offcut"].create(
            {
                "name": name,
                "lot_id": self._new_lot(f"{name}-LOT").id,
                "width_mm": width_mm,
                "height_mm": height_mm,
                "source_type": "sheet",
                "parent_lot_id": parent_lot.id,
                "remaining_value": 90.0,
            }
        )

    def test_optimal_mode_records_engine_and_telemetry(self):
        self.company.write(
            {
                "tp_nesting_engine_mode": "optimal",
                "tp_nesting_timeout_ms": 2000,
                "tp_nesting_sheet_size_candidate_limit": 5,
            }
        )
        mo = self._create_mo(width_mm=500, height_mm=300, quantity=2)
        self._create_sheet_format(name="P5-SHEET-1", width_mm=1200, height_mm=800, landed_cost=120.0)

        mo.action_run_tp_nesting()

        run = mo.tp_last_nesting_run_id
        self.assertEqual(run.engine_mode, "optimal")
        self.assertEqual(run.state, "done")
        self.assertGreater(run.search_nodes, 0)
        self.assertGreaterEqual(run.search_ms, 0)
        # Two cuts should stay on one opened sheet slot when they fit.
        self.assertEqual(len(run.allocation_ids.filtered(lambda a: a.source_type == "sheet")), 2)
        self.assertEqual(len(set(run.allocation_ids.mapped("source_lot_id").ids)), 1)
        lot_name = run.allocation_ids[0].source_lot_id.name
        self.assertEqual((run.nesting_svg or "").count(lot_name), 1)

    def test_deterministic_mode_from_settings(self):
        self.company.tp_nesting_engine_mode = "deterministic"
        mo = self._create_mo(width_mm=500, height_mm=300, quantity=1)
        self._create_sheet_format(name="P5-SHEET-2", width_mm=1000, height_mm=800)

        mo.action_run_tp_nesting()

        run = mo.tp_last_nesting_run_id
        self.assertEqual(run.engine_mode, "deterministic")
        self.assertTrue(run.allocation_ids)

    def test_timeout_fallback_to_deterministic(self):
        self.company.write(
            {
                "tp_nesting_engine_mode": "optimal",
                "tp_nesting_timeout_ms": -1,
                "tp_nesting_fallback_enabled": True,
            }
        )
        mo = self._create_mo(width_mm=400, height_mm=300, quantity=1)
        self._create_sheet_format(name="P5-SHEET-3", width_mm=1000, height_mm=800)

        mo.action_run_tp_nesting()

        run = mo.tp_last_nesting_run_id
        self.assertEqual(run.state, "done")
        self.assertEqual(run.engine_mode, "deterministic")

    def test_sheet_candidate_limit_is_applied(self):
        self.company.write(
            {
                "tp_nesting_engine_mode": "optimal",
                "tp_nesting_timeout_ms": 2000,
                "tp_nesting_sheet_size_candidate_limit": 1,
            }
        )
        mo = self._create_mo(width_mm=600, height_mm=400, quantity=2)
        self._create_sheet_format(name="P5-SHEET-4A", width_mm=1200, height_mm=800, landed_cost=90.0)
        self._create_sheet_format(name="P5-SHEET-4B", width_mm=1220, height_mm=810, landed_cost=80.0)

        mo.action_run_tp_nesting()

        run = mo.tp_last_nesting_run_id
        used_sheets = run.allocation_ids.filtered(lambda a: a.source_type == "sheet").mapped("source_sheet_format_id")
        self.assertLessEqual(len(set(used_sheets.ids)), 1)

    def test_prefers_single_sheet_when_future_fit_possible(self):
        self.company.write(
            {
                "tp_nesting_engine_mode": "optimal",
                "tp_nesting_timeout_ms": 2000,
                "tp_nesting_sheet_size_candidate_limit": 10,
            }
        )
        mo = self._create_mo(width_mm=700, height_mm=500, quantity=1)
        mo.tp_cut_line_ids = [
            (0, 0, {"width_mm": 700, "height_mm": 500, "quantity": 1}),
            (0, 0, {"width_mm": 600, "height_mm": 350, "quantity": 1}),
        ]
        self._create_sheet_format(name="P5-SHEET-SMALL", width_mm=800, height_mm=800, landed_cost=70.0)
        self._create_sheet_format(name="P5-SHEET-BIG", width_mm=2440, height_mm=1220, landed_cost=90.0)

        mo.action_run_tp_nesting()

        run = mo.tp_last_nesting_run_id
        sheet_allocs = run.allocation_ids.filtered(lambda a: a.source_type == "sheet")
        self.assertEqual(len(sheet_allocs), 2)
        self.assertEqual(len(set(sheet_allocs.mapped("source_lot_id").ids)), 1)

    def test_optimal_waste_not_worse_than_deterministic(self):
        mo_det = self._create_mo(width_mm=500, height_mm=300, quantity=1)
        mo_opt = self._create_mo(width_mm=500, height_mm=300, quantity=1)
        self._create_sheet_format(name="P5-SHEET-5", width_mm=700, height_mm=500, landed_cost=80.0)

        self.company.tp_nesting_engine_mode = "deterministic"
        mo_det.action_run_tp_nesting()
        det_waste = mo_det.tp_last_nesting_run_id.waste_area_mm2_total

        self.company.tp_nesting_engine_mode = "optimal"
        mo_opt.action_run_tp_nesting()
        opt_waste = mo_opt.tp_last_nesting_run_id.waste_area_mm2_total

        self.assertLessEqual(opt_waste, det_waste)

    def test_two_medium_panels_pool_to_single_sheet_lot(self):
        self.company.write(
            {
                "tp_nesting_engine_mode": "optimal",
                "tp_nesting_timeout_ms": 2000,
            }
        )
        mo = self._create_mo(width_mm=600, height_mm=600, quantity=1)
        mo.tp_cut_line_ids = [
            (0, 0, {"width_mm": 600, "height_mm": 600, "quantity": 1}),
            (0, 0, {"width_mm": 500, "height_mm": 500, "quantity": 1}),
        ]

        full_sheet_product = self.env["product.product"].create(
            {
                "name": "Clear Acrylic Sheet 3mm P5",
                "tracking": "lot",
                "is_storable": True,
            }
        )
        lot = self.env["stock.lot"].create(
            {
                "name": "ACR-CLR-000-03-2440-1220",
                "product_id": full_sheet_product.id,
                "company_id": self.company.id,
                "tp_width_mm": 1220,
                "tp_height_mm": 2440,
            }
        )
        self.env["stock.quant"]._update_available_quantity(
            full_sheet_product, self.env.ref("stock.stock_location_stock"), 1.0, lot_id=lot
        )
        self.env["tp.nesting.source.map"].create(
            {
                "name": "P5 medium panels to 2440x1220 lot",
                "demand_product_id": self.finished_product.id,
                "source_product_id": full_sheet_product.id,
            }
        )

        mo.action_run_tp_nesting()

        run = mo.tp_last_nesting_run_id
        sheet_allocs = run.allocation_ids.filtered(lambda a: a.source_type == "sheet")
        self.assertEqual(len(sheet_allocs), 2)
        self.assertEqual(len(set(sheet_allocs.mapped("source_lot_id").ids)), 1)

