import re

from odoo.addons.tp_sheet_nesting.models.services.tp_2d_nesting_engine import Tp2DNestingEngine
from odoo.tests.common import TransactionCase


class TestOptimizerPhaseO8Explainability(TransactionCase):
    @staticmethod
    def _engine_cuts():
        return [
            {"width_mm": 700, "height_mm": 500},
            {"width_mm": 600, "height_mm": 350},
            {"width_mm": 500, "height_mm": 300},
        ]

    @staticmethod
    def _engine_sources():
        return [
            {
                "kind": "sheet_lot",
                "id": 101,
                "stable_id": "sheet_lot:101",
                "width_mm": 2440,
                "height_mm": 1220,
                "unit_cost": 120.0,
            }
        ], [
            {
                "kind": "sheet_format",
                "id": 201,
                "stable_id": "sheet_format:201",
                "width_mm": 2440,
                "height_mm": 1220,
                "unit_cost": 130.0,
            }
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
                        bool(placement["rotated"]),
                        int(cut["width_mm"]),
                        int(cut["height_mm"]),
                    )
                )
            bins.append((str(source.get("stable_id") or source.get("id")), tuple(placements)))
        return tuple(bins)

    def test_debug_mode_does_not_alter_chosen_result(self):
        cuts = self._engine_cuts()
        sheet_lot_sources, sheet_format_sources = self._engine_sources()
        base = {
            "kerf_mm": 3,
            "timeout_ms": 2000,
            "sheet_size_candidate_limit": 10,
            "beam_width": 6,
            "branch_cap": 8,
            "mode": "optimal",
            "kernel_name": "maxrects",
            "scoring_preset": "yield_first",
        }

        plan_no_debug = Tp2DNestingEngine(**{**base, "debug_enabled": False}).plan(
            cuts=cuts,
            sheet_lot_sources=sheet_lot_sources,
            sheet_format_sources=sheet_format_sources,
        )
        plan_debug = Tp2DNestingEngine(**{**base, "debug_enabled": True}).plan(
            cuts=cuts,
            sheet_lot_sources=sheet_lot_sources,
            sheet_format_sources=sheet_format_sources,
        )

        self.assertTrue(plan_no_debug["ok"])
        self.assertTrue(plan_debug["ok"])
        self.assertEqual(plan_no_debug.get("order_name"), plan_debug.get("order_name"))
        self.assertEqual(self._plan_signature(plan_no_debug), self._plan_signature(plan_debug))
        self.assertTrue(plan_debug["metrics"].get("debug_artifact"))
        self.assertEqual(plan_no_debug["metrics"].get("debug_artifact"), {})

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.warehouse = cls.env.ref("stock.warehouse0")
        cls.partner = cls.env["res.partner"].create({"name": "O8 Explainability Customer"})
        cls.component_product = cls.env["product.product"].create(
            {"name": "O8 Component", "type": "consu", "sale_ok": False, "purchase_ok": True}
        )
        cls.finished_product = cls.env["product.product"].create(
            {
                "name": "O8 Panel",
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
                "bom_line_ids": [(0, 0, {"product_id": cls.component_product.id, "product_qty": 1.0})],
            }
        )

        account_model = cls.env["account.account"]
        account_types = account_model._fields["account_type"].selection
        expense_type = next((key for key, _ in account_types if "expense" in key), account_types[0][0])
        asset_type = next((key for key, _ in account_types if "asset" in key), account_types[0][0])
        waste_account = account_model.create(
            {"name": "O8 Waste Account", "code": "W8ST9", "account_type": expense_type, "company_ids": [(4, cls.company.id)]}
        )
        inventory_account = account_model.create(
            {"name": "O8 Inventory Account", "code": "I8NV9", "account_type": asset_type, "company_ids": [(4, cls.company.id)]}
        )
        cls.finished_product.categ_id.property_stock_valuation_account_id = inventory_account
        journal = cls.env["account.journal"].search(
            [("company_id", "=", cls.company.id), ("type", "=", "general")], limit=1
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

    def test_svg_reflects_allocation_source_bins_and_coordinates(self):
        self.company.write(
            {
                "tp_nesting_engine_mode": "optimal",
                "tp_nesting_debug_enabled": True,
                "tp_nesting_timeout_ms": 2000,
                "tp_nesting_kernel_name": "maxrects",
            }
        )
        mo = self._create_mo(width_mm=700, height_mm=500)
        mo.tp_cut_line_ids = [
            (0, 0, {"width_mm": 700, "height_mm": 500, "quantity": 1}),
            (0, 0, {"width_mm": 600, "height_mm": 350, "quantity": 1}),
        ]
        self.env["tp.sheet.format"].create(
            {
                "name": "O8-2440x1220",
                "product_id": self.finished_product.id,
                "width_mm": 2440,
                "height_mm": 1220,
                "landed_cost": 120.0,
            }
        )

        mo.action_run_tp_nesting()
        run = mo.tp_last_nesting_run_id
        self.assertEqual(run.state, "done")
        self.assertTrue(run.score_breakdown_json)
        self.assertTrue(run.debug_artifact_json)

        sheet_allocs = run.allocation_ids.filtered(lambda a: a.source_type == "sheet")
        self.assertEqual(len(sheet_allocs), 2)
        self.assertTrue(all(a.source_bin_key for a in sheet_allocs))
        self.assertEqual(len(set(sheet_allocs.mapped("source_bin_key"))), 1)

        svg = run.nesting_svg or ""
        self.assertIn("<svg", svg)
        svg_bins = set(re.findall(r'data-source-bin=\"([^\"]+)\"', svg))
        self.assertEqual(svg_bins, set(sheet_allocs.mapped("source_bin_key")))

        for alloc in sheet_allocs:
            self.assertIn(f'data-alloc-id="{alloc.id}"', svg)
            self.assertIn(f'data-x-mm="{int(alloc.placed_x_mm)}"', svg)
            self.assertIn(f'data-y-mm="{int(alloc.placed_y_mm)}"', svg)
            self.assertIn(f'data-w-mm="{int(alloc.cut_width_mm)}"', svg)
            self.assertIn(f'data-h-mm="{int(alloc.cut_height_mm)}"', svg)

