from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase


class TestPhase2Offcuts(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.product = cls.env["product.product"].create(
            {
                "name": "Offcut Test Product",
                "type": "consu",
                "tracking": "lot",
                "tp_thickness_mm": 3.0,
                "tp_colour": "Clear",
                "tp_protective_film": "paper",
            }
        )
        cls.alt_product = cls.env["product.product"].create(
            {
                "name": "Workshop Offcut Product",
                "type": "consu",
                "tracking": "lot",
            }
        )
        cls.stock_location = cls.env.ref("stock.stock_location_stock")
        cls.parent_lot = cls._new_lot("PARENT-LOT")

    @classmethod
    def _new_lot(cls, name):
        return cls.env["stock.lot"].create(
            {
                "name": name,
                "product_id": cls.product.id,
                "company_id": cls.company.id,
            }
        )

    def test_create_offcut_lot_and_compute_area(self):
        offcut = self.env["tp.offcut"].create(
            {
                "name": "OFC-001",
                "lot_id": self._new_lot("LOT-001").id,
                "width_mm": 1200,
                "height_mm": 800,
                "source_type": "sheet",
                "parent_lot_id": self.parent_lot.id,
            }
        )
        self.assertTrue(offcut.id)
        self.assertEqual(offcut.area_mm2, 960000.0)

    def test_reject_less_than_200_dimensions(self):
        with self.assertRaises(ValidationError):
            self.env["tp.offcut"].create(
                {
                    "name": "BAD-LOW-WIDTH",
                    "lot_id": self._new_lot("LOT-002").id,
                    "width_mm": 199,
                    "height_mm": 500,
                    "source_type": "sheet",
                    "parent_lot_id": self.parent_lot.id,
                }
            )

        with self.assertRaises(ValidationError):
            self.env["tp.offcut"].create(
                {
                    "name": "BAD-LOW-HEIGHT",
                    "lot_id": self._new_lot("LOT-003").id,
                    "width_mm": 500,
                    "height_mm": 199,
                    "source_type": "sheet",
                    "parent_lot_id": self.parent_lot.id,
                }
            )

    def test_bin_rule_assignment(self):
        rule = self.env["tp.offcut.bin.rule"].create(
            {
                "name": "Big Panels to Stock",
                "sequence": 10,
                "min_width_mm": 1200,
                "min_height_mm": 1200,
                "bin_location_id": self.stock_location.id,
            }
        )
        offcut = self.env["tp.offcut"].create(
            {
                "name": "OFC-002",
                "lot_id": self._new_lot("LOT-004").id,
                "width_mm": 1500,
                "height_mm": 1300,
                "source_type": "sheet",
                "parent_lot_id": self.parent_lot.id,
            }
        )
        self.assertEqual(offcut.bin_location_id, rule.bin_location_id)

    def test_parent_source_validation(self):
        sheet_without_parent = self.env["tp.offcut"].create(
            {
                "name": "NO-PARENT-LOT",
                "lot_id": self._new_lot("LOT-005").id,
                "width_mm": 300,
                "height_mm": 300,
                "source_type": "sheet",
            }
        )
        self.assertTrue(sheet_without_parent)

        with self.assertRaises(ValidationError):
            self.env["tp.offcut"].create(
                {
                    "name": "NO-PARENT-OFFCUT",
                    "lot_id": self._new_lot("LOT-006").id,
                    "width_mm": 300,
                    "height_mm": 300,
                    "source_type": "offcut",
                }
            )

    def test_state_transitions(self):
        parent_offcut = self.env["tp.offcut"].create(
            {
                "name": "OFC-PARENT",
                "lot_id": self._new_lot("LOT-007").id,
                "width_mm": 400,
                "height_mm": 400,
                "source_type": "sheet",
                "parent_lot_id": self.parent_lot.id,
            }
        )
        offcut = self.env["tp.offcut"].create(
            {
                "name": "OFC-STATE",
                "lot_id": self._new_lot("LOT-008").id,
                "width_mm": 300,
                "height_mm": 300,
                "source_type": "offcut",
                "parent_offcut_id": parent_offcut.id,
            }
        )
        offcut.action_set_in_use()
        self.assertEqual(offcut.state, "in_use")
        offcut.action_set_sold()
        self.assertEqual(offcut.state, "sold")
        offcut.action_archive()
        self.assertEqual(offcut.state, "inactive")
        self.assertFalse(offcut.active)

    def test_lot_dimensions_not_parsed_from_name(self):
        lot = self._new_lot("ACR-CLR-000-03-2440-1220")
        self.assertEqual(lot.tp_height_mm, 0)
        self.assertEqual(lot.tp_width_mm, 0)

    def test_offcut_dimensions_auto_fill_from_lot(self):
        lot = self.env["stock.lot"].create(
            {
                "name": "MANUAL-DIMS-LOT",
                "product_id": self.product.id,
                "company_id": self.company.id,
                "tp_width_mm": 1220,
                "tp_height_mm": 2440,
            }
        )
        offcut = self.env["tp.offcut"].create(
            {
                "name": "OFC-AUTO-DIMS",
                "lot_id": lot.id,
                "source_type": "sheet",
                "parent_lot_id": self.parent_lot.id,
            }
        )
        self.assertEqual(offcut.height_mm, 2440)
        self.assertEqual(offcut.width_mm, 1220)

    def test_lot_custom_fields_manual_values(self):
        lot = self.env["stock.lot"].create(
            {
                "name": "ACR-CLR-000-03-2440-1220",
                "product_id": self.product.id,
                "company_id": self.company.id,
                "tp_width_mm": 1200,
                "tp_height_mm": 800,
                "tp_is_offcut": True,
                "tp_parent_lot_id": self.parent_lot.id,
            }
        )
        self.assertEqual(lot.tp_width_mm, 1200)
        self.assertEqual(lot.tp_height_mm, 800)
        self.assertTrue(lot.tp_is_offcut)
        self.assertEqual(lot.tp_parent_lot_id, self.parent_lot)

    def test_deleting_lot_cascades_linked_offcut(self):
        lot = self._new_lot("LOT-DELETE-001")
        offcut = self.env["tp.offcut"].create(
            {
                "name": "OFC-DELETE",
                "lot_id": lot.id,
                "width_mm": 500,
                "height_mm": 500,
                "source_type": "sheet",
                "parent_lot_id": self.parent_lot.id,
            }
        )
        self.assertTrue(offcut.exists())

        lot.unlink()

        self.assertFalse(self.env["tp.offcut"].browse(offcut.id).exists())

    def test_manual_offcut_auto_creates_dedicated_lot(self):
        offcut = self.env["tp.offcut"].create(
            {
                "name": "OFC-AUTO-LOT",
                "width_mm": 600,
                "height_mm": 400,
                "source_type": "sheet",
                "parent_lot_id": self.parent_lot.id,
            }
        )
        self.assertTrue(offcut.lot_id)
        self.assertEqual(offcut.lot_id.product_id, self.product)
        self.assertTrue(offcut.lot_id.tp_is_offcut)
        self.assertEqual(offcut.lot_id.tp_parent_lot_id, self.parent_lot)
        self.assertNotEqual(offcut.lot_id, self.parent_lot)

    def test_offcut_requires_own_lot_not_parent_lot(self):
        with self.assertRaises(ValidationError):
            self.env["tp.offcut"].create(
                {
                    "name": "OFC-BAD-PARENT-LOT",
                    "lot_id": self.parent_lot.id,
                    "width_mm": 500,
                    "height_mm": 500,
                    "source_type": "sheet",
                    "parent_lot_id": self.parent_lot.id,
                }
            )

    def test_manual_workshop_offcut_without_parent_uses_selected_product(self):
        offcut = self.env["tp.offcut"].create(
            {
                "name": "OFC-WORKSHOP-001",
                "source_type": "sheet",
                "manual_product_id": self.alt_product.id,
                "width_mm": 700,
                "height_mm": 500,
            }
        )
        self.assertTrue(offcut.lot_id)
        self.assertEqual(offcut.product_id, self.alt_product)
        self.assertEqual(offcut.lot_id.product_id, self.alt_product)
        self.assertFalse(offcut.lot_id.tp_parent_lot_id)

    def test_child_offcut_inherits_material_from_parent_offcut(self):
        parent = self.env["tp.offcut"].create(
            {
                "name": "OFC-PARENT-INHERIT",
                "lot_id": self._new_lot("LOT-PARENT-INHERIT").id,
                "width_mm": 900,
                "height_mm": 900,
                "source_type": "sheet",
                "parent_lot_id": self.parent_lot.id,
            }
        )
        parent.write(
            {
                "tp_thickness_mm": 6.0,
                "tp_colour": "Bronze",
                "tp_protective_film": "plastic",
            }
        )
        child_lot = self.env["stock.lot"].create(
            {
                "name": "LOT-CHILD-INHERIT",
                "product_id": self.alt_product.id,
                "company_id": self.company.id,
            }
        )
        child = self.env["tp.offcut"].create(
            {
                "name": "OFC-CHILD-INHERIT",
                "lot_id": child_lot.id,
                "width_mm": 300,
                "height_mm": 300,
                "source_type": "offcut",
                "parent_offcut_id": parent.id,
            }
        )
        self.assertEqual(child.tp_thickness_mm, 6.0)
        self.assertEqual(child.tp_colour, "Bronze")
        self.assertEqual(child.tp_protective_film, "plastic")

    def test_svg_preview_is_generated(self):
        offcut = self.env["tp.offcut"].create(
            {
                "name": "OFC-SVG-001",
                "lot_id": self._new_lot("LOT-SVG-001").id,
                "width_mm": 1200,
                "height_mm": 800,
                "source_type": "sheet",
                "parent_lot_id": self.parent_lot.id,
            }
        )
        self.assertIn("<svg", offcut.tp_preview_svg or "")
        self.assertIn("1200 x 800 mm", offcut.tp_preview_svg or "")
