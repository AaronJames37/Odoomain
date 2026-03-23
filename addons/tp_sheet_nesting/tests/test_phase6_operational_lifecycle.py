from unittest.mock import patch

from odoo.exceptions import ValidationError
from odoo.tests import tagged

from .test_phase4_deterministic_nesting import TestPhase4DeterministicNesting


@tagged("phase6")
class TestPhase6OperationalLifecycle(TestPhase4DeterministicNesting):
    def test_undo_produce_all_requires_done_mo(self):
        mo = self._create_mo(width_mm=500, height_mm=500, quantity=1)

        with self.assertRaisesRegex(ValidationError, "only available when the MO is done"):
            mo.action_tp_undo_produce_all()

    def test_damaged_deleted_offcut_rerun_recomputes(self):
        mo = self._create_mo(width_mm=500, height_mm=500, quantity=1)
        self._create_offcut(name="P6-OFFCUT-SOURCE", width_mm=1000, height_mm=1000)
        self._create_sheet_format(name="P6-SHEET-FALLBACK", width_mm=1500, height_mm=1200, landed_cost=120.0)

        mo.action_run_tp_nesting()
        first_run = mo.tp_last_nesting_run_id
        self.assertTrue(first_run)

        planned_offcuts = first_run.produced_offcut_ids.filtered(lambda r: r.planned_kind == "offcut")
        self.assertTrue(planned_offcuts)

        mo._tp_materialize_last_nesting_outputs()
        produced_offcuts = self.env["tp.offcut"].search([("produced_in_run_id", "=", first_run.id)])
        self.assertTrue(produced_offcuts)
        self.assertEqual(set(produced_offcuts.mapped("produced_in_mo_id").ids), {mo.id})
        damaged = produced_offcuts[0]
        damaged.action_mark_damaged()
        damaged.unlink()

        mo.action_rerun_tp_nesting()
        second_run = mo.tp_last_nesting_run_id

        self.assertNotEqual(first_run.id, second_run.id)
        self.assertEqual(mo.tp_nesting_state, "done")
        self.assertTrue(second_run.allocation_ids)
        self.assertIn("Superseded", first_run.note or "")

    def test_undo_produce_all_rolls_back_materialized_nesting_outputs(self):
        mo = self._create_mo(width_mm=500, height_mm=500, quantity=1)
        source_offcut = self._create_offcut(name="P6-UNDO-SOURCE", width_mm=1000, height_mm=1000)

        source_area_before = float(source_offcut.remaining_area_mm2)
        source_value_before = float(source_offcut.remaining_value)

        mo.action_run_tp_nesting()
        run = mo.tp_last_nesting_run_id
        self.assertTrue(run)
        mo._tp_materialize_last_nesting_outputs()

        self.assertTrue(run.outputs_materialized)
        materialized_rows = run.produced_offcut_ids.filtered(lambda r: r.state == "materialized")
        self.assertTrue(materialized_rows)
        self.assertEqual(source_offcut.consumed_in_run_id, run)
        self.assertEqual(source_offcut.consumed_in_mo_id, mo)
        self.assertTrue(source_offcut.consumed_at)
        self.assertEqual(source_offcut.state, "inactive")
        self.assertFalse(source_offcut.active)

        mo.write({"state": "done"})
        with patch.object(
            type(mo),
            "_tp_create_unbuild_for_done_mo",
            autospec=True,
            return_value=self.env["mrp.unbuild"],
        ):
            mo.action_tp_undo_produce_all()

        run = self.env["tp.nesting.run"].browse(run.id)
        source_offcut = self.env["tp.offcut"].browse(source_offcut.id)

        self.assertFalse(run.outputs_materialized)
        self.assertFalse(run.outputs_materialized_at)
        self.assertEqual(set(run.produced_offcut_ids.mapped("state")), {"planned"})
        self.assertFalse(run.produced_offcut_ids.mapped("offcut_id"))
        self.assertFalse(run.produced_offcut_ids.mapped("waste_id"))
        self.assertEqual(source_offcut.state, "reserved")
        self.assertTrue(source_offcut.active)
        self.assertFalse(source_offcut.consumed_in_run_id)
        self.assertFalse(source_offcut.consumed_in_mo_id)
        self.assertFalse(source_offcut.consumed_at)
        self.assertAlmostEqual(source_offcut.remaining_area_mm2, source_area_before, places=4)
        self.assertAlmostEqual(source_offcut.remaining_value, source_value_before, places=4)
