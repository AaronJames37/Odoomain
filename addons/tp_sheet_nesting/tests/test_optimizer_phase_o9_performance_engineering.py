from odoo.addons.tp_sheet_nesting.models.services.tp_2d_nesting_engine import Tp2DNestingEngine
from odoo.tests.common import TransactionCase


class TestOptimizerPhaseO9PerformanceEngineering(TransactionCase):
    @staticmethod
    def _sheet_sources():
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
            },
            {
                "kind": "sheet_format",
                "id": 202,
                "stable_id": "sheet_format:202",
                "width_mm": 2200,
                "height_mm": 1220,
                "unit_cost": 118.0,
            },
        ]

    @classmethod
    def _stress_cuts(cls):
        tuples = []
        base = [
            (900, 350),
            (850, 320),
            (800, 300),
            (760, 280),
            (700, 260),
            (650, 240),
            (600, 220),
            (560, 200),
        ]
        for _i in range(5):
            tuples.extend(base)
        for width_mm, height_mm in tuples:
            if int(width_mm) > 2440 or int(height_mm) > 1220:
                raise AssertionError("Stress fixture cut exceeds parent sheet dimensions.")
        return [{"width_mm": width_mm, "height_mm": height_mm} for width_mm, height_mm in tuples]

    def test_guardrail_caps_are_applied_to_effective_runtime_settings(self):
        cuts = [{"width_mm": 700, "height_mm": 500}, {"width_mm": 600, "height_mm": 350}]
        sheet_lot_sources, sheet_format_sources = self._sheet_sources()
        plan = Tp2DNestingEngine(
            kerf_mm=3,
            timeout_ms=50000,
            timeout_cap_ms=1200,
            sheet_size_candidate_limit=10,
            beam_width=50,
            branch_cap=50,
            beam_width_cap=4,
            max_pieces=100,
            mode="optimal",
            kernel_name="maxrects",
        ).plan(
            cuts=cuts,
            sheet_lot_sources=sheet_lot_sources,
            sheet_format_sources=sheet_format_sources,
        )

        self.assertTrue(plan["ok"])
        self.assertEqual(plan["metrics"]["effective_beam_width"], 4)
        self.assertEqual(plan["metrics"]["effective_branch_cap"], 4)
        self.assertEqual(plan["metrics"]["timeout_cap_ms"], 1200)
        self.assertEqual(plan["metrics"]["effective_timeout_ms"], 1200)

    def test_max_piece_guardrail_fails_fast(self):
        cuts = [
            {"width_mm": 400, "height_mm": 300},
            {"width_mm": 420, "height_mm": 320},
            {"width_mm": 440, "height_mm": 340},
            {"width_mm": 460, "height_mm": 360},
            {"width_mm": 480, "height_mm": 380},
            {"width_mm": 500, "height_mm": 400},
        ]
        sheet_lot_sources, sheet_format_sources = self._sheet_sources()
        plan = Tp2DNestingEngine(
            kerf_mm=3,
            timeout_ms=2000,
            sheet_size_candidate_limit=10,
            beam_width=6,
            branch_cap=8,
            max_pieces=5,
            mode="optimal",
            kernel_name="maxrects",
        ).plan(
            cuts=cuts,
            sheet_lot_sources=sheet_lot_sources,
            sheet_format_sources=sheet_format_sources,
        )

        self.assertFalse(plan["ok"])
        self.assertEqual(plan["metrics"]["infeasible_reason"], "max_pieces_exceeded")
        self.assertEqual(plan["metrics"]["search_nodes"], 0)

    def test_early_infeasibility_detects_oversized_cut(self):
        cuts = [{"width_mm": 3000, "height_mm": 1800}]
        sheet_lot_sources, sheet_format_sources = self._sheet_sources()
        plan = Tp2DNestingEngine(
            kerf_mm=3,
            timeout_ms=2000,
            sheet_size_candidate_limit=10,
            beam_width=6,
            branch_cap=8,
            max_pieces=100,
            mode="optimal",
            kernel_name="maxrects",
        ).plan(
            cuts=cuts,
            sheet_lot_sources=sheet_lot_sources,
            sheet_format_sources=sheet_format_sources,
        )

        self.assertFalse(plan["ok"])
        self.assertEqual(plan["metrics"]["infeasible_reason"], "cut_exceeds_all_sources")
        self.assertEqual(plan["metrics"]["search_nodes"], 0)
        self.assertGreaterEqual(plan["metrics"]["early_infeasible_checks"], 1)

    def test_stress_fixture_meets_sla_and_bounded_growth(self):
        cuts = self._stress_cuts()
        sheet_lot_sources, sheet_format_sources = self._sheet_sources()
        plan = Tp2DNestingEngine(
            kerf_mm=3,
            timeout_ms=4000,
            timeout_cap_ms=4000,
            sheet_size_candidate_limit=20,
            beam_width=4,
            branch_cap=4,
            beam_width_cap=10,
            max_pieces=200,
            mode="optimal",
            kernel_name="maxrects",
            enable_local_improvement=False,
            enable_exact_refinement=False,
        ).plan(
            cuts=cuts,
            sheet_lot_sources=sheet_lot_sources,
            sheet_format_sources=sheet_format_sources,
        )

        self.assertTrue(plan["ok"])
        self.assertLessEqual(plan["metrics"]["search_ms"], 4200)
        self.assertLess(plan["metrics"]["search_nodes"], 200000)
        self.assertGreaterEqual(plan["metrics"]["memo_hits"], 0)
        self.assertGreaterEqual(plan["metrics"]["memo_prunes"], 0)

