from itertools import combinations

from odoo.addons.tp_sheet_nesting.models.services.tp_2d_nesting_engine import Tp2DNestingEngine
from odoo.tests.common import TransactionCase


class TestOptimizerPhaseO3MaxRects(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.warehouse = cls.env.ref("stock.warehouse0")
        cls.partner = cls.env["res.partner"].create({"name": "O3 MaxRects Customer"})
        cls.component_product = cls.env["product.product"].create(
            {
                "name": "O3 Component",
                "type": "consu",
                "sale_ok": False,
                "purchase_ok": True,
            }
        )
        cls.finished_product = cls.env["product.product"].create(
            {
                "name": "O3 Panel",
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
                "name": "O3 Waste Account",
                "code": "W3ST9",
                "account_type": expense_type,
                "company_ids": [(4, cls.company.id)],
            }
        )
        inventory_account = account_model.create(
            {
                "name": "O3 Inventory Account",
                "code": "I3NV9",
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

    def setUp(self):
        super().setUp()
        self.env["tp.offcut"].search([("active", "=", True)]).write({"active": False, "state": "inactive"})

    def _create_mo(self, *, width_mm=700, height_mm=500):
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

    @staticmethod
    def _assert_plan_no_overlap_and_in_bounds(plan):
        for bin_state in plan["bins"]:
            src_w = int(bin_state["source"]["width_mm"])
            src_h = int(bin_state["source"]["height_mm"])
            rects = []
            for placement in bin_state["placements"]:
                x1 = int(placement["x"])
                y1 = int(placement["y"])
                x2 = x1 + int(placement["used_w"])
                y2 = y1 + int(placement["used_h"])
                assert x2 <= src_w
                assert y2 <= src_h
                rects.append((x1, y1, x2, y2))
            for (ax1, ay1, ax2, ay2), (bx1, by1, bx2, by2) in combinations(rects, 2):
                no_overlap = ax2 <= bx1 or bx2 <= ax1 or ay2 <= by1 or by2 <= ay1
                assert no_overlap

    def _run_maxrects_scenario(self, cuts):
        self.company.write(
            {
                "tp_nesting_engine_mode": "deterministic",
                "tp_nesting_kernel_name": "maxrects",
            }
        )
        mo = self._create_mo(width_mm=cuts[0][0], height_mm=cuts[0][1])
        mo.tp_cut_line_ids = [(0, 0, {"width_mm": w, "height_mm": h, "quantity": 1}) for (w, h) in cuts]
        self.env["tp.sheet.format"].create(
            {
                "name": "O3-2440x1220",
                "product_id": self.finished_product.id,
                "width_mm": 2440,
                "height_mm": 1220,
                "landed_cost": 100.0,
            }
        )

        mo.action_run_tp_nesting()
        run = mo.tp_last_nesting_run_id
        sheet_allocs = run.allocation_ids.filtered(lambda a: a.source_type == "sheet")
        self.assertEqual(len(sheet_allocs), len(cuts))
        self.assertEqual(len(set(sheet_allocs.mapped("source_lot_id").ids)), 1)

    def _plan_maxrects(self, cuts):
        engine = Tp2DNestingEngine(
            kerf_mm=3,
            timeout_ms=2000,
            sheet_size_candidate_limit=25,
            mode="deterministic",
            kernel_name="maxrects",
        )
        plan = engine.plan(
            cuts=[{"width_mm": w, "height_mm": h} for (w, h) in cuts],
            sheet_lot_sources=[],
            sheet_format_sources=[
                {
                    "kind": "sheet_format",
                    "id": "o3-sheet-2440x1220",
                    "width_mm": 2440,
                    "height_mm": 1220,
                    "unit_cost": 100.0,
                }
            ],
        )
        self.assertTrue(plan["ok"])
        self.assertEqual(len(plan["bins"]), 1)
        self.assertEqual(len(plan["bins"][0]["placements"]), len(cuts))
        self._assert_plan_no_overlap_and_in_bounds(plan)

    def test_regression_two_panels_fit_one_sheet_700x500_600x350(self):
        self._plan_maxrects([(700, 500), (600, 350)])
        self._run_maxrects_scenario([(700, 500), (600, 350)])

    def test_regression_two_panels_fit_one_sheet_600x600_500x500(self):
        self._plan_maxrects([(600, 600), (500, 500)])
        self._run_maxrects_scenario([(600, 600), (500, 500)])

