from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase


class TestPhase3ValuationWaste(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.currency = cls.company.currency_id
        cls.product = cls.env["product.product"].create(
            {
                "name": "Phase 3 Product",
                "type": "consu",
                "tracking": "lot",
            }
        )

        account_model = cls.env["account.account"]
        journal_model = cls.env["account.journal"]
        account_types = account_model._fields["account_type"].selection
        expense_type = next((key for key, _ in account_types if "expense" in key), account_types[0][0])
        asset_type = next((key for key, _ in account_types if "asset" in key), account_types[0][0])

        cls.waste_account = account_model.create(
            {
                "name": "Offcuts Waste Account",
                "code": "WST999",
                "account_type": expense_type,
                "company_ids": [(4, cls.company.id)],
            }
        )
        cls.inventory_account = account_model.create(
            {
                "name": "Offcuts Inventory Account",
                "code": "INV999",
                "account_type": asset_type,
                "company_ids": [(4, cls.company.id)],
            }
        )
        cls.product.categ_id.property_stock_valuation_account_id = cls.inventory_account

        cls.journal = journal_model.search(
            [("company_id", "=", cls.company.id), ("type", "=", "general")],
            limit=1,
        )
        if not cls.journal:
            cls.journal = journal_model.create(
                {
                    "name": "Offcuts General",
                    "type": "general",
                    "code": "OGEN",
                    "company_id": cls.company.id,
                }
            )

        cls.company.tp_waste_account_id = cls.waste_account
        cls.company.tp_waste_journal_id = cls.journal

    @classmethod
    def _new_lot(cls, name):
        return cls.env["stock.lot"].create(
            {
                "name": name,
                "product_id": cls.product.id,
                "company_id": cls.company.id,
            }
        )

    def test_sheet_to_offcut_then_remainder_value_propagation(self):
        parent_lot = self._new_lot("P3-PARENT-LOT-1")
        child_lot = self._new_lot("P3-CHILD-LOT-1")

        offcut = self.env["tp.offcut"].create_offcut_from_sheet(
            lot_id=child_lot.id,
            parent_lot_id=parent_lot.id,
            width_mm=500,
            height_mm=300,
            parent_remaining_area_mm2=300000.0,
            parent_remaining_value=120.0,
            name="P3-OFC-1",
        )
        self.assertAlmostEqual(offcut.remaining_value, 60.0, places=2)
        self.assertAlmostEqual(offcut.remaining_area_mm2, 150000.0, places=2)

        remainder_lot = self._new_lot("P3-REM-LOT-1")
        child = offcut.record_remainder(width_mm=300, height_mm=200, lot_id=remainder_lot.id, name="P3-REM-1")

        self.assertEqual(child.source_type, "offcut")
        self.assertEqual(child.parent_offcut_id, offcut)
        self.assertAlmostEqual(child.remaining_value, 24.0, places=2)
        self.assertAlmostEqual(offcut.remaining_value, 36.0, places=2)

    def test_waste_created_and_accounted(self):
        parent_lot = self._new_lot("P3-PARENT-LOT-2")
        offcut_lot = self._new_lot("P3-OFC-LOT-2")
        offcut = self.env["tp.offcut"].create_offcut_from_sheet(
            lot_id=offcut_lot.id,
            parent_lot_id=parent_lot.id,
            width_mm=400,
            height_mm=250,
            parent_remaining_area_mm2=200000.0,
            parent_remaining_value=100.0,
            name="P3-OFC-2",
        )
        waste = offcut.record_remainder(width_mm=180, height_mm=150)
        self.assertEqual(waste._name, "tp.offcut.waste")
        self.assertEqual(waste.kerf_mm, 3)
        self.assertTrue(waste.account_move_id)
        self.assertEqual(waste.account_move_id.state, "posted")

    def test_conservation_on_events(self):
        parent_lot = self._new_lot("P3-PARENT-LOT-3")
        child_lot = self._new_lot("P3-CHILD-LOT-3")
        offcut = self.env["tp.offcut"].create_offcut_from_sheet(
            lot_id=child_lot.id,
            parent_lot_id=parent_lot.id,
            width_mm=200,
            height_mm=200,
            parent_remaining_area_mm2=40000.0,
            parent_remaining_value=40.0,
            name="P3-OFC-3",
        )
        self.assertTrue(offcut.valuation_reference.is_conserved)
        self.assertLessEqual(abs(offcut.valuation_reference.conservation_delta), 0.01)

    def test_missing_waste_account_configuration_fails(self):
        parent_lot = self._new_lot("P3-PARENT-LOT-4")
        offcut_lot = self._new_lot("P3-OFC-LOT-4")
        offcut = self.env["tp.offcut"].create_offcut_from_sheet(
            lot_id=offcut_lot.id,
            parent_lot_id=parent_lot.id,
            width_mm=300,
            height_mm=300,
            parent_remaining_area_mm2=90000.0,
            parent_remaining_value=90.0,
            name="P3-OFC-4",
        )

        original = self.company.tp_waste_account_id
        self.company.tp_waste_account_id = False
        try:
            with self.assertRaises(ValidationError):
                offcut.record_remainder(width_mm=150, height_mm=150)
        finally:
            self.company.tp_waste_account_id = original
