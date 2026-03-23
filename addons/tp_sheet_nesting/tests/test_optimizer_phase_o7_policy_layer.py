from odoo.addons.tp_sheet_nesting.models.services.tp_2d_nesting_engine import Tp2DNestingEngine
from odoo.tests import tagged
from odoo.tests.common import TransactionCase


@tagged("optimizer_o7")
class TestOptimizerPhaseO7PolicyLayer(TransactionCase):
    @staticmethod
    def _cuts():
        return [{"width_mm": 800, "height_mm": 500}]

    @staticmethod
    def _sheet_sources():
        # Two valid single-sheet options for the same cut:
        # - sheet_lot: higher cost and slightly more waste
        # - sheet_format: cheaper with slightly less waste
        return [
            {
                "kind": "sheet_lot",
                "id": 101,
                "stable_id": "sheet_lot:101",
                "width_mm": 1200,
                "height_mm": 1000,
                "unit_cost": 180.0,
            }
        ], [
            {
                "kind": "sheet_format",
                "id": 201,
                "stable_id": "sheet_format:201",
                "width_mm": 1100,
                "height_mm": 1000,
                "unit_cost": 80.0,
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
                        int(placement["used_w"]),
                        int(placement["used_h"]),
                        bool(placement["rotated"]),
                        int(cut["width_mm"]),
                        int(cut["height_mm"]),
                    )
                )
            bins.append((str(source.get("stable_id") or source.get("id")), tuple(placements)))
        return tuple(bins)

    @staticmethod
    def _first_source_id(plan):
        if not plan["bins"]:
            return ""
        source = plan["bins"][0]["source"]
        return str(source.get("stable_id") or source.get("id"))

    def _plan_with_preset(self, preset, **kwargs):
        sheet_lot_sources, sheet_format_sources = self._sheet_sources()
        return Tp2DNestingEngine(
            kerf_mm=3,
            timeout_ms=1000,
            sheet_size_candidate_limit=10,
            beam_width=4,
            branch_cap=6,
            mode="optimal",
            kernel_name="maxrects",
            scoring_preset=preset,
            **kwargs,
        ).plan(
            cuts=self._cuts(),
            sheet_lot_sources=sheet_lot_sources,
            sheet_format_sources=sheet_format_sources,
        )

    def test_presets_produce_expected_source_preference(self):
        plan_yield = self._plan_with_preset("yield_first")
        plan_cost = self._plan_with_preset("cost_first")
        plan_offcut = self._plan_with_preset("offcut_first")

        self.assertTrue(plan_yield["ok"])
        self.assertTrue(plan_cost["ok"])
        self.assertTrue(plan_offcut["ok"])

        self.assertEqual(self._first_source_id(plan_yield), "sheet_format:201")
        self.assertEqual(self._first_source_id(plan_cost), "sheet_format:201")
        self.assertEqual(self._first_source_id(plan_offcut), "sheet_lot:101")

    def test_weight_multipliers_allow_no_code_policy_tuning(self):
        baseline = self._plan_with_preset("offcut_first")
        tuned = self._plan_with_preset(
            "offcut_first",
            offcut_reuse_priority=0.0,
            waste_priority=1.0,
            sheet_count_penalty=1.0,
            cost_sensitivity=1.0,
        )

        self.assertTrue(baseline["ok"])
        self.assertTrue(tuned["ok"])
        self.assertEqual(self._first_source_id(baseline), "sheet_lot:101")
        self.assertEqual(self._first_source_id(tuned), "sheet_format:201")

    def test_deterministic_output_is_maintained_per_preset(self):
        for preset in ("yield_first", "cost_first", "offcut_first"):
            plan_a = self._plan_with_preset(preset)
            plan_b = self._plan_with_preset(preset)
            self.assertTrue(plan_a["ok"])
            self.assertTrue(plan_b["ok"])
            self.assertEqual(plan_a["metrics"].get("policy_preset"), preset)
            self.assertEqual(plan_b["metrics"].get("policy_preset"), preset)
            self.assertEqual(self._plan_signature(plan_a), self._plan_signature(plan_b))

