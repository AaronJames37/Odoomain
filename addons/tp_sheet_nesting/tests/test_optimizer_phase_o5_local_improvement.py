import time

from odoo.addons.tp_sheet_nesting.models.services.tp_2d_nesting_engine import Tp2DNestingEngine
from odoo.tests.common import TransactionCase


class TestOptimizerPhaseO5LocalImprovement(TransactionCase):
    PARENT_SHEET_W = 2440
    PARENT_SHEET_H = 1220

    @classmethod
    def _stress_panel_tuples(cls):
        # Stress fixture only: each panel is within 2440x1220.
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
            (500, 500),
            (600, 600),
            (700, 500),
            (600, 350),
            (400, 300),
            (350, 250),
            (300, 220),
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
    def _sources():
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
        formats = [
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
        return sheet_lots, formats

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

    def test_local_neighborhood_has_swap_rotate_and_reinsert(self):
        cuts = [{"width_mm": 800, "height_mm": 340}, {"width_mm": 600, "height_mm": 350}, {"width_mm": 500, "height_mm": 500}]
        engine = Tp2DNestingEngine(
            kerf_mm=3,
            timeout_ms=2000,
            mode="optimal",
            kernel_name="maxrects",
            enable_local_improvement=True,
        )
        normalized = engine._normalize_cuts(cuts)
        neighbors = engine._build_local_neighbors(normalized, step_idx=0)
        op_names = {name for name, _cuts in neighbors}
        self.assertIn("swap", op_names)
        self.assertIn("reinsert", op_names)
        self.assertIn("rotate_subset", op_names)

    def test_local_improvement_never_worsens_selected_plan(self):
        cuts = self._stress_cuts()
        sheet_lot_sources, sheet_format_sources = self._sources()
        base_config = {
            "kerf_mm": 3,
            "timeout_ms": 3000,
            "sheet_size_candidate_limit": 20,
            "beam_width": 6,
            "branch_cap": 8,
            "mode": "optimal",
            "kernel_name": "maxrects",
            "local_improvement_max_steps": 6,
            "late_acceptance_window": 4,
            "local_neighbor_cap": 18,
        }

        plan_without = Tp2DNestingEngine(**{**base_config, "enable_local_improvement": False}).plan(
            cuts=cuts,
            sheet_lot_sources=sheet_lot_sources,
            sheet_format_sources=sheet_format_sources,
        )
        plan_with = Tp2DNestingEngine(**{**base_config, "enable_local_improvement": True}).plan(
            cuts=cuts,
            sheet_lot_sources=sheet_lot_sources,
            sheet_format_sources=sheet_format_sources,
        )

        self.assertTrue(plan_without["ok"])
        self.assertTrue(plan_with["ok"])
        self.assertLessEqual(self._plan_score(plan_with), self._plan_score(plan_without))
        self.assertGreaterEqual(plan_with["metrics"]["local_improvement_steps"], 0)
        self.assertGreaterEqual(plan_with["metrics"]["local_improvement_moves"], 0)

    def test_timeout_envelope_respected_with_local_improvement(self):
        cuts = self._stress_cuts() * 2
        sheet_lot_sources, sheet_format_sources = self._sources()
        engine = Tp2DNestingEngine(
            kerf_mm=3,
            timeout_ms=1,
            sheet_size_candidate_limit=20,
            beam_width=8,
            branch_cap=10,
            mode="optimal",
            kernel_name="maxrects",
            enable_local_improvement=True,
            local_improvement_max_steps=8,
            late_acceptance_window=4,
            local_neighbor_cap=24,
        )

        started_at = time.monotonic()
        with self.assertRaises(TimeoutError):
            engine.plan(
                cuts=cuts,
                sheet_lot_sources=sheet_lot_sources,
                sheet_format_sources=sheet_format_sources,
            )
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        self.assertLess(elapsed_ms, 1500)

