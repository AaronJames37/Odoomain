from odoo.addons.tp_sheet_nesting.models.services.tp_2d_nesting_engine import Tp2DNestingEngine
from odoo.tests.common import TransactionCase


class TestOptimizerPhaseO6ExactRefinement(TransactionCase):
    PARENT_SHEET_W = 2440
    PARENT_SHEET_H = 1220

    @staticmethod
    def _sheet_sources():
        sheet_lots = [
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
        ]
        sheet_formats = [
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
        return sheet_lots, sheet_formats

    def _small_job_cuts(self):
        cuts = [
            {"width_mm": 900, "height_mm": 420},
            {"width_mm": 850, "height_mm": 360},
            {"width_mm": 800, "height_mm": 340},
            {"width_mm": 700, "height_mm": 500},
            {"width_mm": 650, "height_mm": 280},
            {"width_mm": 600, "height_mm": 350},
            {"width_mm": 500, "height_mm": 500},
        ]
        for cut in cuts:
            self.assertLessEqual(int(cut["width_mm"]), self.PARENT_SHEET_W)
            self.assertLessEqual(int(cut["height_mm"]), self.PARENT_SHEET_H)
        return cuts

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

    def test_exact_refinement_can_be_enabled_and_disabled_safely(self):
        cuts = self._small_job_cuts()
        sheet_lot_sources, sheet_format_sources = self._sheet_sources()
        base_config = {
            "kerf_mm": 3,
            "timeout_ms": 2500,
            "sheet_size_candidate_limit": 20,
            "beam_width": 6,
            "branch_cap": 8,
            "mode": "optimal",
            "kernel_name": "maxrects",
            "enable_local_improvement": True,
            "local_improvement_max_steps": 4,
            "exact_refinement_cut_threshold": 8,
            "exact_refinement_timeout_ms": 500,
        }

        plan_disabled = Tp2DNestingEngine(
            **{**base_config, "enable_exact_refinement": False}
        ).plan(
            cuts=cuts,
            sheet_lot_sources=sheet_lot_sources,
            sheet_format_sources=sheet_format_sources,
        )
        plan_enabled = Tp2DNestingEngine(
            **{**base_config, "enable_exact_refinement": True}
        ).plan(
            cuts=cuts,
            sheet_lot_sources=sheet_lot_sources,
            sheet_format_sources=sheet_format_sources,
        )

        self.assertTrue(plan_disabled["ok"])
        self.assertTrue(plan_enabled["ok"])
        self.assertFalse(bool(plan_disabled["metrics"].get("exact_refinement_used")))
        self.assertTrue(bool(plan_enabled["metrics"].get("exact_refinement_used")))
        self.assertLessEqual(self._plan_score(plan_enabled), self._plan_score(plan_disabled))

    def test_exact_refinement_timeout_falls_back_cleanly(self):
        cuts = self._small_job_cuts()
        sheet_lot_sources, sheet_format_sources = self._sheet_sources()
        plan = Tp2DNestingEngine(
            kerf_mm=3,
            timeout_ms=2500,
            sheet_size_candidate_limit=20,
            beam_width=6,
            branch_cap=8,
            mode="optimal",
            kernel_name="maxrects",
            enable_exact_refinement=True,
            exact_refinement_cut_threshold=8,
            exact_refinement_timeout_ms=-1,
            enable_local_improvement=True,
            local_improvement_max_steps=3,
        ).plan(
            cuts=cuts,
            sheet_lot_sources=sheet_lot_sources,
            sheet_format_sources=sheet_format_sources,
        )

        self.assertTrue(plan["ok"])
        self.assertTrue(plan["bins"])
        self.assertTrue(bool(plan["metrics"].get("exact_refinement_used")))
        self.assertTrue(bool(plan["metrics"].get("exact_refinement_timeout")))

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.warehouse = cls.env.ref("stock.warehouse0")
        cls.partner = cls.env["res.partner"].create({"name": "O6 Exact Customer"})

        cls.component_product = cls.env["product.product"].create(
            {
                "name": "O6 Component",
                "type": "consu",
                "sale_ok": False,
                "purchase_ok": True,
            }
        )
        cls.finished_product = cls.env["product.product"].create(
            {
                "name": "O6 Panel",
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
                "name": "O6 Waste Account",
                "code": "W6ST9",
                "account_type": expense_type,
                "company_ids": [(4, cls.company.id)],
            }
        )
        inventory_account = account_model.create(
            {
                "name": "O6 Inventory Account",
                "code": "I6NV9",
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

    def test_exact_refinement_failure_has_no_side_effect_regression(self):
        self.company.write(
            {
                "tp_nesting_engine_mode": "optimal",
                "tp_nesting_timeout_ms": 2500,
                "tp_nesting_sheet_size_candidate_limit": 20,
                "tp_nesting_beam_width": 6,
                "tp_nesting_branch_cap": 8,
                "tp_nesting_exact_refinement_enabled": True,
                "tp_nesting_exact_refinement_cut_threshold": 8,
                "tp_nesting_exact_refinement_timeout_ms": -1,
                "tp_nesting_fallback_enabled": True,
            }
        )

        mo = self._create_mo(width_mm=700, height_mm=500)
        mo.tp_cut_line_ids = [
            (0, 0, {"width_mm": 700, "height_mm": 500, "quantity": 1}),
            (0, 0, {"width_mm": 600, "height_mm": 350, "quantity": 1}),
        ]
        self.env["tp.sheet.format"].create(
            {
                "name": "O6-2440x1220",
                "product_id": self.finished_product.id,
                "width_mm": 2440,
                "height_mm": 1220,
                "landed_cost": 120.0,
            }
        )

        mo.action_run_tp_nesting()

        run = mo.tp_last_nesting_run_id
        self.assertEqual(run.state, "done")
        self.assertTrue(run.allocation_ids)
        self.assertEqual(run.engine_mode, "optimal")
        reserved = self.env["tp.offcut"].search([("reserved_mo_id", "=", mo.id), ("state", "=", "reserved")])
        self.assertFalse(reserved)

