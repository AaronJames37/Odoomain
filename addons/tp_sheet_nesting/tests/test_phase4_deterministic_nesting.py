from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase


class TestPhase4DeterministicNesting(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.currency = cls.company.currency_id
        cls.warehouse = cls.env.ref("stock.warehouse0")
        cls.partner = cls.env["res.partner"].create({"name": "Phase 4 Customer"})

        cls.component_product = cls.env["product.product"].create(
            {
                "name": "Phase 4 Component",
                "type": "consu",
                "sale_ok": False,
                "purchase_ok": True,
            }
        )
        cls.finished_product = cls.env["product.product"].create(
            {
                "name": "Phase 4 Panel",
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
                "name": "Phase4 Waste Account",
                "code": "W4ST9",
                "account_type": expense_type,
                "company_ids": [(4, cls.company.id)],
            }
        )
        cls.inventory_account = account_model.create(
            {
                "name": "Phase4 Inventory Account",
                "code": "I4NV9",
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

    @classmethod
    def _new_lot(cls, name):
        return cls.env["stock.lot"].create(
            {
                "name": name,
                "product_id": cls.finished_product.id,
                "company_id": cls.company.id,
            }
        )

    def setUp(self):
        super().setUp()
        self.env["tp.offcut"].search([("active", "=", True)]).write({"active": False, "state": "inactive"})

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
        vals = sale_line._prepare_procurement_values()
        mo_vals = self.warehouse.manufacture_pull_id._prepare_mo_vals(
            self.finished_product,
            sale_line.product_uom_qty,
            sale_line.product_uom_id,
            self.warehouse.lot_stock_id,
            sale_line.name,
            order.name,
            order.company_id,
            vals,
            self.bom,
        )
        return self.env["mrp.production"].create(mo_vals)

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
                "remaining_value": 100.0,
            }
        )

    def _create_two_mos_same_order(self):
        order = self.env["sale.order"].create(
            {
                "partner_id": self.partner.id,
                "order_line": [
                    (
                        0,
                        0,
                        {
                            "product_id": self.finished_product.id,
                            "product_uom_qty": 1,
                            "price_unit": 100.0,
                            "tp_width_mm": 500,
                            "tp_height_mm": 300,
                        },
                    ),
                    (
                        0,
                        0,
                        {
                            "product_id": self.finished_product.id,
                            "product_uom_qty": 1,
                            "price_unit": 100.0,
                            "tp_width_mm": 300,
                            "tp_height_mm": 200,
                        },
                    ),
                ],
            }
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
        return mo_a, mo_b

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

    def test_offcuts_consumed_and_reserved_first(self):
        mo = self._create_mo(width_mm=500, height_mm=300, quantity=1)
        offcut = self._create_offcut(name="P4-OFFCUT-1", width_mm=900, height_mm=700)
        self._create_sheet_format(name="P4-SHEET-1", width_mm=1200, height_mm=800)

        mo.action_run_tp_nesting()

        allocation = mo.tp_last_nesting_run_id.allocation_ids
        self.assertEqual(len(allocation), 1)
        self.assertEqual(allocation.source_type, "offcut")
        self.assertEqual(allocation.source_offcut_id, offcut)
        self.assertEqual(offcut.state, "reserved")
        self.assertTrue(offcut.active)

    def test_offcut_source_syncs_raw_component_consumption(self):
        mo = self._create_mo(width_mm=500, height_mm=300, quantity=1)
        offcut = self._create_offcut(name="P4-OFFCUT-COMP", width_mm=900, height_mm=700)

        mo.action_run_tp_nesting()

        active_moves = mo.move_raw_ids.filtered(lambda m: m.state not in ("done", "cancel") and m.product_uom_qty > 0)
        self.assertTrue(active_moves, "Expected at least one raw component move after nesting.")
        self.assertEqual(len(active_moves), 1)
        raw_move = active_moves[0]
        self.assertEqual(raw_move.product_id, offcut.product_id)
        self.assertEqual(raw_move.product_uom_qty, 1.0)
        self.assertEqual(len(raw_move.move_line_ids), 1)
        self.assertEqual(raw_move.move_line_ids[0].lot_id, offcut.lot_id)

    def test_sheet_source_creates_remainder_offcut(self):
        mo = self._create_mo(width_mm=600, height_mm=600, quantity=1)
        self._create_sheet_format(name="P4-SHEET-2", width_mm=1000, height_mm=1000, landed_cost=120.0)

        mo.action_run_tp_nesting()

        allocation = mo.tp_last_nesting_run_id.allocation_ids
        self.assertEqual(len(allocation), 1)
        self.assertEqual(allocation.source_type, "sheet")

        planned_remainder = mo.tp_last_nesting_run_id.produced_offcut_ids.filtered(
            lambda r: r.planned_kind == "offcut" and r.planned_source_type == "sheet"
        )
        self.assertTrue(planned_remainder)
        self.assertEqual(planned_remainder[0].state, "planned")
        self.assertGreaterEqual(planned_remainder[0].planned_width_mm, 200)
        self.assertGreaterEqual(planned_remainder[0].planned_height_mm, 200)

    def test_sheet_source_creates_waste_when_remainder_too_small(self):
        mo = self._create_mo(width_mm=500, height_mm=300, quantity=1)
        self._create_sheet_format(name="P4-SHEET-3", width_mm=700, height_mm=500, landed_cost=80.0)

        mo.action_run_tp_nesting()

        planned_waste = mo.tp_last_nesting_run_id.produced_offcut_ids.filtered(
            lambda r: r.planned_kind == "waste" and r.planned_source_type == "sheet"
        )
        self.assertTrue(planned_waste)
        self.assertEqual(planned_waste[0].kerf_mm, 3)

    def test_reserved_offcut_not_reused_by_another_mo(self):
        shared_offcut = self._create_offcut(name="P4-OFFCUT-2", width_mm=1200, height_mm=1200)
        self._create_sheet_format(name="P4-SHEET-4", width_mm=1500, height_mm=1500)

        mo_a = self._create_mo(width_mm=500, height_mm=500, quantity=1)
        mo_a.action_run_tp_nesting()
        self.assertEqual(shared_offcut.state, "reserved")

        mo_b = self._create_mo(width_mm=500, height_mm=500, quantity=1)
        mo_b.action_run_tp_nesting()

        reused = mo_b.tp_last_nesting_run_id.allocation_ids.filtered(
            lambda a: a.source_type == "offcut" and a.source_offcut_id.id == shared_offcut.id
        )
        self.assertFalse(reused)

    def test_single_run_pools_all_sizes_from_same_order(self):
        mo_a, mo_b = self._create_two_mos_same_order()
        self._create_sheet_format(name="P4-SHEET-POOL", width_mm=1500, height_mm=1200)

        self.assertEqual(mo_a.tp_scope_cut_count, 2)
        self.assertIn("500 x 300 mm x 1", mo_a.tp_scope_cut_summary or "")
        self.assertIn("300 x 200 mm x 1", mo_a.tp_scope_cut_summary or "")

        mo_a.action_run_tp_nesting()

        self.assertEqual(mo_a.tp_nesting_state, "done")
        self.assertEqual(mo_b.tp_nesting_state, "done")
        self.assertEqual(mo_a.tp_last_nesting_run_id, mo_b.tp_last_nesting_run_id)
        self.assertEqual(len(mo_a.tp_last_nesting_run_id.allocation_ids), 2)
        self.assertTrue(mo_a.tp_last_nesting_run_id.job_id)
        self.assertIn("<svg", mo_a.tp_last_nesting_run_id.nesting_svg or "")
        self.assertEqual(mo_a.tp_last_nesting_job_id, mo_a.tp_last_nesting_run_id.job_id)
        self.assertEqual(mo_b.tp_last_nesting_job_id, mo_a.tp_last_nesting_run_id.job_id)
        self.assertEqual(
            mo_a.tp_last_nesting_run_id.job_id.sale_order_id,
            mo_a.x_tp_source_so_line_id.order_id,
        )
        job = mo_a.tp_last_nesting_run_id.job_id
        self.assertEqual(set(job.mo_ids.ids), {mo_a.id, mo_b.id})
        self.assertEqual(job.mo_count, 2)
        mo_action = job.action_view_mos()
        self.assertEqual(mo_action.get("res_model"), "mrp.production")
        self.assertEqual(set(mo_action.get("domain", [("id", "in", [])])[0][2]), {mo_a.id, mo_b.id})

    def test_rerun_replaces_reservation_with_new_run(self):
        mo = self._create_mo(width_mm=400, height_mm=400, quantity=1)
        offcut = self._create_offcut(name="P4-OFFCUT-3", width_mm=1000, height_mm=1000)

        mo.action_run_tp_nesting()
        first_run = mo.tp_last_nesting_run_id
        self.assertEqual(offcut.state, "reserved")

        mo.action_rerun_tp_nesting()
        second_run = mo.tp_last_nesting_run_id

        self.assertNotEqual(first_run.id, second_run.id)
        self.assertTrue(second_run.allocation_ids)
        self.assertTrue(second_run.produced_panel_ids)
        self.assertIn("Superseded", first_run.note or "")

    def test_failed_run_does_not_leave_orphans(self):
        mo = self._create_mo(width_mm=5000, height_mm=5000, quantity=1)

        with self.assertRaises(ValidationError):
            mo.action_run_tp_nesting()

        allocations = self.env["tp.nesting.allocation"].search([("mo_id", "=", mo.id)])
        reserved = self.env["tp.offcut"].search([("reserved_mo_id", "=", mo.id), ("state", "=", "reserved")])

        self.assertFalse(allocations)
        self.assertFalse(reserved)

    def test_produce_all_blocked_without_successful_nesting(self):
        mo = self._create_mo(width_mm=500, height_mm=300, quantity=1)
        mo.action_confirm()

        with self.assertRaisesRegex(ValidationError, "Run Nesting"):
            mo.button_mark_done()

    def test_offcut_product_variant_compatible_by_material_identity(self):
        self.finished_product.product_tmpl_id.write(
            {
                "tp_material_type": "Acrylic",
                "tp_thickness_mm": 3.0,
                "tp_colour": "Clear",
                "tp_finish": "Gloss",
                "tp_protective_film": "paper",
                "tp_brand_supplier": "TP",
            }
        )
        offcut_product = self.env["product.product"].create(
            {
                "name": "3mm Clear Acrylic Offcut",
                "type": "consu",
                "tracking": "lot",
                "tp_material_type": "Acrylic",
                "tp_thickness_mm": 3.0,
                "tp_colour": "Clear",
                "tp_finish": "Gloss",
                "tp_protective_film": "paper",
                "tp_brand_supplier": "TP",
            }
        )
        parent_lot = self.env["stock.lot"].create(
            {"name": "P4-MAT-PARENT", "product_id": offcut_product.id, "company_id": self.company.id}
        )
        offcut = self.env["tp.offcut"].create(
            {
                "name": "P4-MAT-OFFCUT",
                "lot_id": self.env["stock.lot"]
                .create({"name": "P4-MAT-LOT", "product_id": offcut_product.id, "company_id": self.company.id})
                .id,
                "width_mm": 1000,
                "height_mm": 800,
                "source_type": "sheet",
                "parent_lot_id": parent_lot.id,
                "remaining_value": 100.0,
            }
        )
        mo = self._create_mo(width_mm=500, height_mm=500, quantity=1)

        mo.action_run_tp_nesting()

        allocation = mo.tp_last_nesting_run_id.allocation_ids
        self.assertEqual(len(allocation), 1)
        self.assertEqual(allocation.source_type, "offcut")
        self.assertEqual(allocation.source_offcut_id, offcut)

    def test_sheet_lot_fallback_used_when_no_sheet_formats(self):
        mo = self._create_mo(width_mm=500, height_mm=500, quantity=1)
        full_sheet_product = self.env["product.product"].create(
            {
                "name": "Clear Acrylic Sheet 3mm",
                "tracking": "lot",
                "is_storable": True,
            }
        )
        self.env["stock.lot"].create(
            {
                "name": "ACR-CLR-000-03-2440-1220",
                "product_id": full_sheet_product.id,
                "company_id": self.company.id,
                "tp_width_mm": 1220,
                "tp_height_mm": 2440,
            }
        )
        lot = self.env["stock.lot"].search([("product_id", "=", full_sheet_product.id)], order="id desc", limit=1)
        self.env["stock.quant"]._update_available_quantity(
            full_sheet_product, self.env.ref("stock.stock_location_stock"), 1.0, lot_id=lot
        )

        mo.action_run_tp_nesting()

        allocation = mo.tp_last_nesting_run_id.allocation_ids
        self.assertEqual(len(allocation), 1)
        self.assertEqual(allocation.source_type, "sheet")
        self.assertTrue(allocation.source_lot_id)

    def test_sheet_lot_mapping_restricts_eligible_sources(self):
        mo = self._create_mo(width_mm=500, height_mm=500, quantity=1)
        full_sheet_product = self.env["product.product"].create(
            {
                "name": "Clear Acrylic Sheet 3mm Mapped",
                "tracking": "lot",
                "is_storable": True,
            }
        )
        lot_a = self.env["stock.lot"].create(
            {
                "name": "ACR-CLR-000-03-2440-1220",
                "product_id": full_sheet_product.id,
                "company_id": self.company.id,
                "tp_width_mm": 1220,
                "tp_height_mm": 2440,
            }
        )
        lot_b = self.env["stock.lot"].create(
            {
                "name": "ACR-CLR-000-03-3050-2030",
                "product_id": full_sheet_product.id,
                "company_id": self.company.id,
                "tp_width_mm": 2030,
                "tp_height_mm": 3050,
            }
        )
        self.env["stock.quant"]._update_available_quantity(
            full_sheet_product, self.env.ref("stock.stock_location_stock"), 1.0, lot_id=lot_a
        )
        self.env["stock.quant"]._update_available_quantity(
            full_sheet_product, self.env.ref("stock.stock_location_stock"), 1.0, lot_id=lot_b
        )
        self.env["tp.nesting.source.map"].create(
            {
                "name": "3mm mapped lot only",
                "demand_product_id": self.finished_product.id,
                "source_product_id": full_sheet_product.id,
                "source_lot_id": lot_b.id,
            }
        )

        mo.action_run_tp_nesting()

        allocation = mo.tp_last_nesting_run_id.allocation_ids
        self.assertEqual(len(allocation), 1)
        self.assertEqual(allocation.source_lot_id, lot_b)

    def test_sheet_product_mapping_allows_multiple_lot_sizes(self):
        mo = self._create_mo(width_mm=500, height_mm=500, quantity=2)
        full_sheet_product = self.env["product.product"].create(
            {
                "name": "Clear Acrylic Sheet 3mm Product-Mapped",
                "tracking": "lot",
                "is_storable": True,
            }
        )
        lot_a = self.env["stock.lot"].create(
            {
                "name": "ACR-CLR-000-03-2440-1220",
                "product_id": full_sheet_product.id,
                "company_id": self.company.id,
                "tp_width_mm": 1220,
                "tp_height_mm": 2440,
            }
        )
        lot_b = self.env["stock.lot"].create(
            {
                "name": "ACR-CLR-000-03-3050-2030",
                "product_id": full_sheet_product.id,
                "company_id": self.company.id,
                "tp_width_mm": 2030,
                "tp_height_mm": 3050,
            }
        )
        self.env["stock.quant"]._update_available_quantity(
            full_sheet_product, self.env.ref("stock.stock_location_stock"), 1.0, lot_id=lot_a
        )
        self.env["stock.quant"]._update_available_quantity(
            full_sheet_product, self.env.ref("stock.stock_location_stock"), 1.0, lot_id=lot_b
        )
        self.env["tp.nesting.source.map"].create(
            {
                "name": "3mm product mapping",
                "demand_product_id": self.finished_product.id,
                "source_product_id": full_sheet_product.id,
            }
        )

        mo.action_run_tp_nesting()

        allocations = mo.tp_last_nesting_run_id.allocation_ids
        sheet_allocations = allocations.filtered(lambda a: a.source_type == "sheet")
        self.assertTrue(sheet_allocations)
        self.assertTrue(all(a.source_lot_id.product_id == full_sheet_product for a in sheet_allocations))
        self.assertFalse(
            sheet_allocations.filtered(
                lambda a: a.source_lot_id and a.source_lot_id.id not in {lot_a.id, lot_b.id}
            )
        )

    def test_sheet_lot_fallback_from_product_level_quants_without_lot_tracking(self):
        mo = self._create_mo(width_mm=600, height_mm=400, quantity=1)
        full_sheet_product = self.env["product.product"].create(
            {
                "name": "Clear Acrylic Sheet 3mm Product-Level Quant",
                "tracking": "none",
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
            full_sheet_product, self.env.ref("stock.stock_location_stock"), 1.0
        )
        self.env["tp.nesting.source.map"].create(
            {
                "name": "3mm product mapping quant fallback",
                "demand_product_id": self.finished_product.id,
                "source_product_id": full_sheet_product.id,
            }
        )

        mo.action_run_tp_nesting()

        sheet_alloc = mo.tp_last_nesting_run_id.allocation_ids.filtered(lambda a: a.source_type == "sheet")
        self.assertTrue(sheet_alloc)
        self.assertEqual(sheet_alloc[0].source_lot_id, lot)

    def test_sheet_product_stock_source_works_without_lot_dimensions(self):
        mo = self._create_mo(width_mm=600, height_mm=400, quantity=1)
        full_sheet_product = self.env["product.product"].create(
            {
                "name": "Clear Acrylic Sheet 3mm SKU-Source",
                "tracking": "none",
                "is_storable": True,
                "tp_sheet_width_mm": 1220,
                "tp_sheet_height_mm": 2440,
            }
        )
        self.env["stock.quant"]._update_available_quantity(
            full_sheet_product, self.env.ref("stock.stock_location_stock"), 1.0
        )
        self.env["tp.nesting.source.map"].create(
            {
                "name": "3mm product-only mapping",
                "demand_product_id": self.finished_product.id,
                "source_product_id": full_sheet_product.id,
            }
        )

        mo.action_run_tp_nesting()

        sheet_alloc = mo.tp_last_nesting_run_id.allocation_ids.filtered(lambda a: a.source_type == "sheet")
        self.assertTrue(sheet_alloc)
        self.assertTrue(sheet_alloc[0].source_lot_id)
        self.assertEqual(sheet_alloc[0].source_lot_id.product_id, full_sheet_product)

    def test_two_panels_fit_single_2440x1220_sheet_lot(self):
        order = self.env["sale.order"].create(
            {
                "partner_id": self.partner.id,
                "order_line": [
                    (
                        0,
                        0,
                        {
                            "product_id": self.finished_product.id,
                            "product_uom_qty": 1,
                            "price_unit": 100.0,
                            "tp_width_mm": 700,
                            "tp_height_mm": 500,
                        },
                    ),
                    (
                        0,
                        0,
                        {
                            "product_id": self.finished_product.id,
                            "product_uom_qty": 1,
                            "price_unit": 100.0,
                            "tp_width_mm": 600,
                            "tp_height_mm": 350,
                        },
                    ),
                ],
            }
        )
        lines = order.order_line.sorted("id")
        mo_a = self.env["mrp.production"].create(
            self.warehouse.manufacture_pull_id._prepare_mo_vals(
                self.finished_product,
                lines[0].product_uom_qty,
                lines[0].product_uom_id,
                self.warehouse.lot_stock_id,
                lines[0].name,
                order.name,
                order.company_id,
                lines[0]._prepare_procurement_values(),
                self.bom,
            )
        )
        self.env["mrp.production"].create(
            self.warehouse.manufacture_pull_id._prepare_mo_vals(
                self.finished_product,
                lines[1].product_uom_qty,
                lines[1].product_uom_id,
                self.warehouse.lot_stock_id,
                lines[1].name,
                order.name,
                order.company_id,
                lines[1]._prepare_procurement_values(),
                self.bom,
            )
        )

        full_sheet_product = self.env["product.product"].create(
            {
                "name": "Clear Acrylic Sheet 3mm",
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
                "name": "panel to 2440x1220 lot product",
                "demand_product_id": self.finished_product.id,
                "source_product_id": full_sheet_product.id,
            }
        )

        self.company.tp_nesting_engine_mode = "optimal"
        mo_a.action_run_tp_nesting()

        run = mo_a.tp_last_nesting_run_id
        sheet_allocs = run.allocation_ids.filtered(lambda a: a.source_type == "sheet")
        self.assertEqual(len(sheet_allocs), 2)
        self.assertEqual(len(set(sheet_allocs.mapped("source_lot_id").ids)), 1)

