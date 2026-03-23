from odoo.addons.tp_sheet_nesting.models.services.tp_2d_nesting_engine import Tp2DNestingEngine
from odoo.tests.common import TransactionCase


class TestOptimizerPhaseO4GlobalSearch(TransactionCase):
    PARENT_SHEET_W = 2440
    PARENT_SHEET_H = 1220

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.warehouse = cls.env.ref("stock.warehouse0")
        cls.partner = cls.env["res.partner"].create({"name": "O4 Global Search Customer"})

        cls.component_product = cls.env["product.product"].create(
            {
                "name": "O4 Component",
                "type": "consu",
                "sale_ok": False,
                "purchase_ok": True,
            }
        )
        cls.finished_product = cls.env["product.product"].create(
            {
                "name": "O4 Panel",
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
                "name": "O4 Waste Account",
                "code": "W4ST9",
                "account_type": expense_type,
                "company_ids": [(4, cls.company.id)],
            }
        )
        inventory_account = account_model.create(
            {
                "name": "O4 Inventory Account",
                "code": "I4NV9",
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

    @classmethod
    def _stress_panel_tuples(cls):
        # Stress fixture only: ensure test cuts never exceed parent sheet size.
        return [
            (1200, 600),
            (1100, 500),
            (950, 420),
            (900, 380),
            (850, 360),
            (800, 340),
            (760, 320),
            (700, 300),
            (650, 280),
            (600, 260),
            (560, 240),
            (520, 220),
        ]

    def _stress_cuts(self):
        tuples = self._stress_panel_tuples()
        for width_mm, height_mm in tuples:
            self.assertGreater(width_mm, 0)
            self.assertGreater(height_mm, 0)
            self.assertLessEqual(width_mm, self.PARENT_SHEET_W)
            self.assertLessEqual(height_mm, self.PARENT_SHEET_H)
        return [{"width_mm": width_mm, "height_mm": height_mm} for width_mm, height_mm in tuples]

    @staticmethod
    def _stress_sheet_sources():
        return [
            {
                "kind": "sheet_lot",
                "id": 101,
                "stable_id": "sheet_lot:101",
                "width_mm": 2440,
                "height_mm": 1220,
                "unit_cost": 120.0,
            },
            {
                "kind": "sheet_lot",
                "id": 102,
                "stable_id": "sheet_lot:102",
                "width_mm": 2200,
                "height_mm": 1220,
                "unit_cost": 105.0,
            },
        ], [
            {
                "kind": "sheet_format",
                "id": 201,
                "stable_id": "sheet_format:201",
                "width_mm": 2440,
                "height_mm": 1220,
                "unit_cost": 130.0,
            },
            {
                "kind": "sheet_format",
                "id": 202,
                "stable_id": "sheet_format:202",
                "width_mm": 2000,
                "height_mm": 1000,
                "unit_cost": 90.0,
            },
            {
                "kind": "sheet_format",
                "id": 203,
                "stable_id": "sheet_format:203",
                "width_mm": 1800,
                "height_mm": 1000,
                "unit_cost": 80.0,
            },
        ]

    @staticmethod
    def _plan_signature(plan):
        bins = []
        for bin_state in plan["bins"]:
            source = bin_state["source"]
            placements = []
            for placement in bin_state.get("placements", []):
                cut = placement["cut"]
                placements.append(
                    (
                        int(placement["x"]),
                        int(placement["y"]),
                        int(placement["fit_w"]),
                        int(placement["fit_h"]),
                        int(placement["used_w"]),
                        int(placement["used_h"]),
                        bool(placement["rotated"]),
                        int(cut["width_mm"]),
                        int(cut["height_mm"]),
                    )
                )
            bins.append(
                (
                    str(source.get("stable_id") or source.get("id")),
                    int(source["width_mm"]),
                    int(source["height_mm"]),
                    tuple(placements),
                )
            )
        return tuple(bins)

    @staticmethod
    def _plan_score(plan):
        waste_area = 0.0
        reuse_area = 0.0
        total_cost = 0.0
        source_trace = []
        for bin_state in plan["bins"]:
            source = bin_state["source"]
            src_area = float(source["width_mm"] * source["height_mm"])
            used_area = sum(float(p["used_w"] * p["used_h"]) for p in bin_state.get("placements", []))
            waste_area += max(0.0, src_area - used_area)
            if source.get("kind") == "sheet_lot":
                reuse_area += used_area
            total_cost += float(source.get("unit_cost") or 0.0)
            source_trace.append(str(source.get("stable_id") or source.get("id")))
        return (len(plan["bins"]), waste_area, -reuse_area, total_cost, tuple(source_trace))

    def test_beam_width_changes_search_breadth(self):
        cuts = self._stress_cuts()
        sheet_lot_sources, sheet_format_sources = self._stress_sheet_sources()

        narrow_engine = Tp2DNestingEngine(
            kerf_mm=3,
            timeout_ms=2000,
            sheet_size_candidate_limit=20,
            beam_width=1,
            branch_cap=3,
            mode="optimal",
            kernel_name="maxrects",
        )
        narrow_plan = narrow_engine.plan(
            cuts=cuts,
            sheet_lot_sources=sheet_lot_sources,
            sheet_format_sources=sheet_format_sources,
        )
        self.assertTrue(narrow_plan["ok"])

        wide_engine = Tp2DNestingEngine(
            kerf_mm=3,
            timeout_ms=2000,
            sheet_size_candidate_limit=20,
            beam_width=8,
            branch_cap=8,
            mode="optimal",
            kernel_name="maxrects",
        )
        wide_plan = wide_engine.plan(
            cuts=cuts,
            sheet_lot_sources=sheet_lot_sources,
            sheet_format_sources=sheet_format_sources,
        )
        self.assertTrue(wide_plan["ok"])

        self.assertGreater(wide_plan["metrics"]["beam_expansions"], narrow_plan["metrics"]["beam_expansions"])
        self.assertGreater(wide_plan["metrics"]["search_nodes"], narrow_plan["metrics"]["search_nodes"])

    def test_same_input_and_config_is_deterministic(self):
        cuts = self._stress_cuts()
        sheet_lot_sources, sheet_format_sources = self._stress_sheet_sources()
        config = {
            "kerf_mm": 3,
            "timeout_ms": 2000,
            "sheet_size_candidate_limit": 20,
            "beam_width": 6,
            "branch_cap": 6,
            "mode": "optimal",
            "kernel_name": "maxrects",
        }

        plan_a = Tp2DNestingEngine(**config).plan(
            cuts=cuts,
            sheet_lot_sources=sheet_lot_sources,
            sheet_format_sources=sheet_format_sources,
        )
        plan_b = Tp2DNestingEngine(**config).plan(
            cuts=cuts,
            sheet_lot_sources=sheet_lot_sources,
            sheet_format_sources=sheet_format_sources,
        )

        self.assertTrue(plan_a["ok"])
        self.assertTrue(plan_b["ok"])
        self.assertEqual(plan_a.get("order_name"), plan_b.get("order_name"))
        self.assertEqual(self._plan_signature(plan_a), self._plan_signature(plan_b))

    def test_beam_search_not_worse_than_width_one_on_stress_fixture(self):
        cuts = self._stress_cuts()
        sheet_lot_sources, sheet_format_sources = self._stress_sheet_sources()

        beam_one_plan = Tp2DNestingEngine(
            kerf_mm=3,
            timeout_ms=2000,
            sheet_size_candidate_limit=20,
            beam_width=1,
            branch_cap=3,
            mode="optimal",
            kernel_name="maxrects",
        ).plan(
            cuts=cuts,
            sheet_lot_sources=sheet_lot_sources,
            sheet_format_sources=sheet_format_sources,
        )
        beam_wide_plan = Tp2DNestingEngine(
            kerf_mm=3,
            timeout_ms=2000,
            sheet_size_candidate_limit=20,
            beam_width=8,
            branch_cap=8,
            mode="optimal",
            kernel_name="maxrects",
        ).plan(
            cuts=cuts,
            sheet_lot_sources=sheet_lot_sources,
            sheet_format_sources=sheet_format_sources,
        )
        self.assertTrue(beam_one_plan["ok"])
        self.assertTrue(beam_wide_plan["ok"])
        self.assertLessEqual(self._plan_score(beam_wide_plan), self._plan_score(beam_one_plan))

    def test_candidate_limit_keeps_diverse_sheet_sizes(self):
        cuts = [{"width_mm": 2460, "height_mm": 300}]
        sheet_lot_sources = []
        for idx in range(1, 31):
            sheet_lot_sources.append(
                {
                    "kind": "sheet_product",
                    "id": idx,
                    "stable_id": f"sheet_product:small:{idx}",
                    "width_mm": 1220,
                    "height_mm": 2440,
                    "unit_cost": 100.0,
                }
            )
        sheet_lot_sources.append(
            {
                "kind": "sheet_product",
                "id": 1000,
                "stable_id": "sheet_product:large:1",
                "width_mm": 1880,
                "height_mm": 2490,
                "unit_cost": 150.0,
            }
        )

        plan = Tp2DNestingEngine(
            kerf_mm=3,
            timeout_ms=2000,
            sheet_size_candidate_limit=25,
            beam_width=6,
            branch_cap=6,
            mode="optimal",
            kernel_name="maxrects",
        ).plan(
            cuts=cuts,
            sheet_lot_sources=sheet_lot_sources,
            sheet_format_sources=[],
        )

        self.assertTrue(plan["ok"])
        self.assertEqual(len(plan["bins"]), 1)
        self.assertEqual(plan["bins"][0]["source"]["id"], 1000)

    def _create_mo(self, *, width_mm=600, height_mm=400):
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

    def test_timeout_fallback_returns_deterministic_when_enabled(self):
        self.company.write(
            {
                "tp_nesting_engine_mode": "optimal",
                "tp_nesting_timeout_ms": -1,
                "tp_nesting_beam_width": 8,
                "tp_nesting_branch_cap": 8,
                "tp_nesting_fallback_enabled": True,
            }
        )
        mo = self._create_mo(width_mm=600, height_mm=400)
        self.env["tp.sheet.format"].create(
            {
                "name": "O4-2440x1220",
                "product_id": self.finished_product.id,
                "width_mm": 2440,
                "height_mm": 1220,
                "landed_cost": 120.0,
            }
        )

        mo.action_run_tp_nesting()

        run = mo.tp_last_nesting_run_id
        self.assertEqual(run.state, "done")
        self.assertEqual(run.engine_mode, "deterministic")

