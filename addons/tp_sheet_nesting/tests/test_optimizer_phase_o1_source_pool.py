from odoo.addons.tp_sheet_nesting.models.services.tp_nesting_source_pool import (
    TpNestingSourcePool,
)
from odoo.tests.common import TransactionCase


class TestOptimizerPhaseO1SourcePool(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.warehouse = cls.env.ref("stock.warehouse0")
        cls.partner = cls.env["res.partner"].create({"name": "O1 Source Pool Customer"})
        cls.component_product = cls.env["product.product"].create(
            {
                "name": "O1 Component",
                "type": "consu",
                "sale_ok": False,
                "purchase_ok": True,
            }
        )
        cls.demand_product = cls.env["product.product"].create(
            {
                "name": "O1 Demand Panel 3mm",
                "type": "consu",
                "tracking": "lot",
                "sale_ok": True,
                "purchase_ok": False,
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
                "tp_material_type": "Acrylic",
                "tp_thickness_mm": 3.0,
                "tp_colour": "Clear",
                "tp_finish": "Gloss",
                "tp_protective_film": "paper",
                "tp_brand_supplier": "TP",
            }
        )
        cls.bom = cls.env["mrp.bom"].create(
            {
                "product_tmpl_id": cls.demand_product.product_tmpl_id.id,
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

    def _create_mo(self, *, width_mm=600, height_mm=400, quantity=1):
        order = self.env["sale.order"].create(
            {
                "partner_id": self.partner.id,
                "order_line": [
                    (
                        0,
                        0,
                        {
                            "product_id": self.demand_product.id,
                            "product_uom_qty": quantity,
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
            self.demand_product,
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

    def _create_offcut(self, *, name, product, width_mm, height_mm, material_type):
        lot = self.env["stock.lot"].create(
            {
                "name": f"{name}-LOT",
                "product_id": product.id,
                "company_id": self.company.id,
            }
        )
        parent_lot = self.env["stock.lot"].create(
            {
                "name": f"{name}-PARENT",
                "product_id": product.id,
                "company_id": self.company.id,
            }
        )
        return self.env["tp.offcut"].create(
            {
                "name": name,
                "lot_id": lot.id,
                "width_mm": width_mm,
                "height_mm": height_mm,
                "source_type": "sheet",
                "parent_lot_id": parent_lot.id,
                "remaining_value": 100.0,
                "tp_material_type": material_type,
                "tp_thickness_mm": 3.0,
                "tp_colour": "Clear",
                "tp_finish": "Gloss",
                "tp_protective_film": "paper",
                "tp_brand_supplier": "TP",
            }
        )

    def _create_sheet_lot(self, *, product, lot_name, width_mm=2440, height_mm=1220, qty=1.0):
        lot = self.env["stock.lot"].create(
            {
                "name": lot_name,
                "product_id": product.id,
                "company_id": self.company.id,
                "tp_width_mm": width_mm,
                "tp_height_mm": height_mm,
            }
        )
        self.env["stock.quant"]._update_available_quantity(
            product,
            self.env.ref("stock.stock_location_stock"),
            qty,
            lot_id=lot,
        )
        return lot

    def _new_sheet_product(self, *, name, material_type, width_mm=0, height_mm=0):
        return self.env["product.product"].create(
            {
                "name": name,
                "tracking": "lot",
                "is_storable": True,
                "tp_material_type": material_type,
                "tp_thickness_mm": 3.0,
                "tp_colour": "Clear",
                "tp_finish": "Gloss",
                "tp_protective_film": "paper",
                "tp_brand_supplier": "TP",
                "tp_sheet_width_mm": width_mm,
                "tp_sheet_height_mm": height_mm,
            }
        )

    def test_material_mismatch_sources_are_excluded(self):
        mo = self._create_mo()
        matching_offcut = self._create_offcut(
            name="O1-OFFCUT-MATCH",
            product=self.demand_product,
            width_mm=1000,
            height_mm=800,
            material_type="Acrylic",
        )
        mismatched_offcut = self._create_offcut(
            name="O1-OFFCUT-MISMATCH",
            product=self.demand_product,
            width_mm=1000,
            height_mm=800,
            material_type="Polycarb",
        )

        matching_sheet = self.env["tp.sheet.format"].create(
            {
                "name": "O1-SHEET-MATCH",
                "product_id": self.demand_product.id,
                "width_mm": 2440,
                "height_mm": 1220,
                "landed_cost": 100.0,
                "tp_material_type": "Acrylic",
                "tp_thickness_mm": 3.0,
                "tp_colour": "Clear",
                "tp_finish": "Gloss",
                "tp_protective_film": "paper",
                "tp_brand_supplier": "TP",
            }
        )
        mismatched_sheet = self.env["tp.sheet.format"].create(
            {
                "name": "O1-SHEET-MISMATCH",
                "product_id": self.demand_product.id,
                "width_mm": 2440,
                "height_mm": 1220,
                "landed_cost": 100.0,
                "tp_material_type": "Polycarb",
                "tp_thickness_mm": 3.0,
                "tp_colour": "Clear",
                "tp_finish": "Gloss",
                "tp_protective_film": "paper",
                "tp_brand_supplier": "TP",
            }
        )

        match_sheet_product = self._new_sheet_product(name="O1-Lot Match 3mm", material_type="Acrylic")
        mismatch_sheet_product = self._new_sheet_product(name="O1-Lot Mismatch 3mm", material_type="Polycarb")
        match_lot = self._create_sheet_lot(product=match_sheet_product, lot_name="O1-MATCH-2440-1220")
        mismatch_lot = self._create_sheet_lot(product=mismatch_sheet_product, lot_name="O1-MISMATCH-2440-1220")

        pool = TpNestingSourcePool(
            mo=mo,
            product=self.demand_product,
            material_identity=mo._tp_get_material_identity(),
        )
        built = pool.build()

        offcut_ids = {src["id"] for src in built["offcut_sources"]}
        sheet_format_ids = {src["id"] for src in built["sheet_format_sources"]}
        sheet_lot_ids = {src["id"] for src in built["sheet_lot_sources"]}
        self.assertIn(matching_offcut.id, offcut_ids)
        self.assertNotIn(mismatched_offcut.id, offcut_ids)
        self.assertIn(matching_sheet.id, sheet_format_ids)
        self.assertNotIn(mismatched_sheet.id, sheet_format_ids)
        self.assertIn(match_lot.id, sheet_lot_ids)
        self.assertNotIn(mismatch_lot.id, sheet_lot_ids)

    def test_lot_and_product_mapping_restrictions_respected(self):
        mo = self._create_mo()
        sheet_product = self._new_sheet_product(name="O1-Sheet 3mm", material_type="Acrylic")
        lot_a = self._create_sheet_lot(product=sheet_product, lot_name="O1-A-2440-1220")
        lot_b = self._create_sheet_lot(product=sheet_product, lot_name="O1-B-2440-1220")
        self.env["tp.nesting.source.map"].create(
            {
                "name": "O1 lot-restricted map",
                "demand_product_id": self.demand_product.id,
                "source_product_id": sheet_product.id,
                "source_lot_id": lot_b.id,
            }
        )

        pool = TpNestingSourcePool(
            mo=mo,
            product=self.demand_product,
            material_identity=mo._tp_get_material_identity(),
        )
        lot_ids = {src["id"] for src in pool.sheet_lot_sources()}
        self.assertEqual(lot_ids, {lot_b.id})

        # Rebuild scenario for product-level mapping in same transaction.
        self.env["tp.nesting.source.map"].search([("demand_product_id", "=", self.demand_product.id)]).unlink()
        other_product = self._new_sheet_product(name="O1-Other Product 3mm", material_type="Acrylic")
        lot_other = self._create_sheet_lot(product=other_product, lot_name="O1-OTHER-2440-1220")
        self.env["tp.nesting.source.map"].create(
            {
                "name": "O1 product-level map",
                "demand_product_id": self.demand_product.id,
                "source_product_id": sheet_product.id,
            }
        )
        pool.invalidate()
        lot_ids = {src["id"] for src in pool.sheet_lot_sources()}
        self.assertIn(lot_a.id, lot_ids)
        self.assertIn(lot_b.id, lot_ids)
        self.assertNotIn(lot_other.id, lot_ids)

    def test_source_ordering_is_deterministic(self):
        mo = self._create_mo()
        self._create_offcut(
            name="O1-OFFCUT-A",
            product=self.demand_product,
            width_mm=1000,
            height_mm=700,
            material_type="Acrylic",
        )
        self._create_offcut(
            name="O1-OFFCUT-B",
            product=self.demand_product,
            width_mm=900,
            height_mm=700,
            material_type="Acrylic",
        )
        self.env["tp.sheet.format"].create(
            {
                "name": "O1-FORMAT-A",
                "product_id": self.demand_product.id,
                "width_mm": 2440,
                "height_mm": 1220,
                "landed_cost": 90.0,
            }
        )
        self.env["tp.sheet.format"].create(
            {
                "name": "O1-FORMAT-B",
                "product_id": self.demand_product.id,
                "width_mm": 3050,
                "height_mm": 2030,
                "landed_cost": 110.0,
            }
        )
        sheet_product = self._new_sheet_product(name="O1-SHEET-ORDER 3mm", material_type="Acrylic")
        self._create_sheet_lot(product=sheet_product, lot_name="O1-ORDER-A-2440-1220")
        self._create_sheet_lot(product=sheet_product, lot_name="O1-ORDER-B-3050-2030")

        pool = TpNestingSourcePool(
            mo=mo,
            product=self.demand_product,
            material_identity=mo._tp_get_material_identity(),
        )
        first_order = [src["stable_id"] for src in pool.all_sources()]
        second_order = [src["stable_id"] for src in pool.all_sources()]

        self.assertEqual(first_order, second_order)
        self.assertTrue(first_order)
        self.assertTrue(first_order[0].startswith("offcut:"))

    def test_sheet_product_source_fallback_when_no_on_hand_quant(self):
        mo = self._create_mo()
        fallback_sheet = self._new_sheet_product(
            name="O1-SHEET-FALLBACK 3mm",
            material_type="Acrylic",
            width_mm=2440,
            height_mm=1220,
        )

        pool = TpNestingSourcePool(
            mo=mo,
            product=self.demand_product,
            material_identity=mo._tp_get_material_identity(),
        )
        sheet_product_sources = pool.sheet_product_sources()

        self.assertEqual(len(sheet_product_sources), 1)
        self.assertEqual(sheet_product_sources[0]["record"], fallback_sheet)
        self.assertEqual(sheet_product_sources[0]["width_mm"], 2440)
        self.assertEqual(sheet_product_sources[0]["height_mm"], 1220)

    def test_mapping_expands_to_material_matching_sheet_skus(self):
        mo = self._create_mo()

        # Anchor source mapped from demand can be a logical SKU with no dimensions.
        anchor_source = self.env["product.product"].create(
            {
                "name": "O1-Anchor Clear Acrylic 3mm",
                "tracking": "none",
                "type": "consu",
                "tp_material_type": "Acrylic",
                "tp_thickness_mm": 3.0,
                "tp_colour": "Clear",
                "tp_finish": "Gloss",
                "tp_protective_film": "paper",
                "tp_brand_supplier": "TP",
            }
        )

        matching_sheet = self._new_sheet_product(
            name="O1-Matching Sheet SKU",
            material_type="Acrylic",
            width_mm=2440,
            height_mm=1220,
        )
        matching_sheet.write(
            {
                "tp_thickness_mm": 3.0,
                "tp_colour": "Clear",
                "tp_finish": "Gloss",
                "tp_protective_film": "paper",
                "tp_brand_supplier": "TP",
            }
        )
        mismatched_sheet = self._new_sheet_product(
            name="O1-Mismatch Sheet SKU",
            material_type="Polycarb",
            width_mm=2440,
            height_mm=1220,
        )
        mismatched_sheet.write({"tp_thickness_mm": 3.0, "tp_colour": "Clear"})

        self.env["tp.nesting.source.map"].create(
            {
                "name": "O1 map anchor -> material pool",
                "demand_product_id": self.demand_product.id,
                "source_product_id": anchor_source.id,
            }
        )

        pool = TpNestingSourcePool(
            mo=mo,
            product=self.demand_product,
            material_identity=mo._tp_get_material_identity(),
        )
        source_product_ids = {src["product_id"] for src in pool.sheet_product_sources()}

        self.assertIn(matching_sheet.id, source_product_ids)
        self.assertNotIn(mismatched_sheet.id, source_product_ids)

