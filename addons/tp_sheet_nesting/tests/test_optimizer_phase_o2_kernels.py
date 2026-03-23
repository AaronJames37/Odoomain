from itertools import combinations

from odoo.addons.tp_sheet_nesting.models.services.tp_2d_nesting_engine import Tp2DNestingEngine
from odoo.tests.common import TransactionCase


class TestOptimizerPhaseO2Kernels(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.warehouse = cls.env.ref("stock.warehouse0")
        cls.partner = cls.env["res.partner"].create({"name": "O2 Kernel Customer"})
        cls.component_product = cls.env["product.product"].create(
            {
                "name": "O2 Component",
                "type": "consu",
                "sale_ok": False,
                "purchase_ok": True,
            }
        )
        cls.finished_product = cls.env["product.product"].create(
            {
                "name": "O2 Panel",
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
        waste_account = account_model.create(
            {
                "name": "O2 Waste Account",
                "code": "W2ST9",
                "account_type": expense_type,
                "company_ids": [(4, cls.company.id)],
            }
        )
        inventory_account = account_model.create(
            {
                "name": "O2 Inventory Account",
                "code": "I2NV9",
                "account_type": asset_type,
                "company_ids": [(4, cls.company.id)],
            }
        )
        cls.finished_product.categ_id.property_stock_valuation_account_id = inventory_account
        journal = cls.env["account.journal"].search(
            [("company_id", "=", cls.company.id), ("type", "=", "general")],
            limit=1,
        )
        cls.company.tp_waste_account_id = waste_account
        cls.company.tp_waste_journal_id = journal

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
        line = order.order_line[:1]
        mo_vals = self.warehouse.manufacture_pull_id._prepare_mo_vals(
            self.finished_product,
            line.product_uom_qty,
            line.product_uom_id,
            self.warehouse.lot_stock_id,
            line.name,
            order.name,
            order.company_id,
            line._prepare_procurement_values(),
            self.bom,
        )
        return self.env["mrp.production"].create(mo_vals)

    def _plan_for_kernel(self, kernel_name, *, cuts, source_width=2440, source_height=1220):
        engine = Tp2DNestingEngine(
            kerf_mm=3,
            timeout_ms=2000,
            sheet_size_candidate_limit=25,
            mode="deterministic",
            kernel_name=kernel_name,
        )
        plan = engine.plan(
            cuts=cuts,
            sheet_lot_sources=[],
            sheet_format_sources=[
                {
                    "kind": "sheet_format",
                    "id": f"{kernel_name}-sheet",
                    "width_mm": source_width,
                    "height_mm": source_height,
                    "unit_cost": 100.0,
                }
            ],
        )
        self.assertTrue(plan["ok"])
        self.assertTrue(plan["bins"])
        return plan

    def _assert_plan_geometry_valid(self, plan):
        for bin_state in plan["bins"]:
            src_w = int(bin_state["source"]["width_mm"])
            src_h = int(bin_state["source"]["height_mm"])
            placements = bin_state["placements"]
            rects = []
            for placement in placements:
                cut = placement["cut"]
                if placement["rotated"]:
                    self.assertEqual(int(placement["fit_w"]), int(cut["height_mm"]))
                    self.assertEqual(int(placement["fit_h"]), int(cut["width_mm"]))
                else:
                    self.assertEqual(int(placement["fit_w"]), int(cut["width_mm"]))
                    self.assertEqual(int(placement["fit_h"]), int(cut["height_mm"]))
                self.assertGreater(int(placement["used_w"]), int(placement["fit_w"]))
                self.assertGreater(int(placement["used_h"]), int(placement["fit_h"]))
                x1 = int(placement["x"])
                y1 = int(placement["y"])
                x2 = x1 + int(placement["used_w"])
                y2 = y1 + int(placement["used_h"])
                self.assertLessEqual(x2, src_w)
                self.assertLessEqual(y2, src_h)
                rects.append((x1, y1, x2, y2))
            for (ax1, ay1, ax2, ay2), (bx1, by1, bx2, by2) in combinations(rects, 2):
                no_overlap = ax2 <= bx1 or bx2 <= ax1 or ay2 <= by1 or by2 <= ay1
                self.assertTrue(no_overlap)

    def test_each_kernel_places_valid_geometry_no_overlap(self):
        cuts = [
            {"width_mm": 700, "height_mm": 500},
            {"width_mm": 600, "height_mm": 350},
            {"width_mm": 500, "height_mm": 300},
        ]
        for kernel_name in ("guillotine", "maxrects", "skyline"):
            plan = self._plan_for_kernel(kernel_name, cuts=cuts)
            self._assert_plan_geometry_valid(plan)

    def test_each_kernel_rotation_consistency(self):
        cuts = [{"width_mm": 650, "height_mm": 850}]
        for kernel_name in ("guillotine", "maxrects", "skyline"):
            plan = self._plan_for_kernel(kernel_name, cuts=cuts, source_width=900, source_height=700)
            placement = plan["bins"][0]["placements"][0]
            self.assertTrue(placement["rotated"])

    def test_company_kernel_setting_switches_mo_execution(self):
        for idx, kernel_name in enumerate(("guillotine", "maxrects", "skyline"), start=1):
            self.company.write(
                {
                    "tp_nesting_engine_mode": "deterministic",
                    "tp_nesting_kernel_name": kernel_name,
                }
            )
            mo = self._create_mo(width_mm=500, height_mm=300, quantity=1)
            self.env["tp.sheet.format"].create(
                {
                    "name": f"O2-SHEET-{kernel_name}-{idx}",
                    "product_id": self.finished_product.id,
                    "width_mm": 1000,
                    "height_mm": 800,
                    "landed_cost": 120.0,
                }
            )

            mo.action_run_tp_nesting()

            run = mo.tp_last_nesting_run_id
            self.assertEqual(run.state, "done")
            self.assertTrue(run.allocation_ids)

