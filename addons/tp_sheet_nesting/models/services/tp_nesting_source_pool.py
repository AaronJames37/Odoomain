class TpNestingSourcePool:
    """Centralized normalized source pool for nesting.

    Provides deterministic, compatibility-filtered sources shared by all engine modes.
    """

    MATERIAL_FIELDS = [
        "tp_material_type",
        "tp_thickness_mm",
        "tp_colour",
        "tp_finish",
        "tp_protective_film",
        "tp_brand_supplier",
    ]
    KIND_PRIORITY = {"offcut": 0, "sheet_product": 1, "sheet_lot": 2, "sheet_format": 3}

    def __init__(self, *, mo, product, material_identity):
        self.mo = mo
        self.env = mo.env
        self.product = product
        self.material_identity = material_identity or {}
        self._cache = None

    def invalidate(self):
        self._cache = None

    def _source_sort_key(self, source):
        return (
            self.KIND_PRIORITY.get(source["kind"], 99),
            int(source.get("product_id") or 0),
            int(source.get("width_mm") or 0),
            int(source.get("height_mm") or 0),
            source.get("stable_id") or "",
        )

    def _material_identity_items(self):
        return [
            (field_name, self.material_identity.get(field_name))
            for field_name in self.MATERIAL_FIELDS
            if self.material_identity.get(field_name) not in (False, None, "")
        ]

    @staticmethod
    def _non_empty(value):
        return value not in (False, None, "")

    def _record_material_value(self, record, field_name):
        value = False
        if field_name in record._fields:
            value = record[field_name]
        if self._non_empty(value):
            return value

        if "product_id" in record._fields and record.product_id and field_name in record.product_id._fields:
            value = record.product_id[field_name]
        if self._non_empty(value):
            return value

        if "product_tmpl_id" in record._fields and record.product_tmpl_id and field_name in record.product_tmpl_id._fields:
            value = record.product_tmpl_id[field_name]
        if self._non_empty(value):
            return value
        return False

    def is_material_compatible(self, record):
        identity_items = self._material_identity_items()
        if not identity_items:
            return True
        for field_name, expected in identity_items:
            actual = self._record_material_value(record, field_name)
            if not self._non_empty(actual) or actual != expected:
                return False
        return True

    def _offcut_sources(self):
        offcuts = self.mo._tp_material_compatible_offcuts(self.product, self.material_identity)
        sources = []
        for offcut in offcuts:
            area_mm2 = float(offcut.remaining_area_mm2 or offcut.area_mm2 or 0.0)
            sources.append(
                {
                    "kind": "offcut",
                    "stable_id": f"offcut:{offcut.id}",
                    "id": offcut.id,
                    "record": offcut,
                    "product_id": offcut.product_id.id if offcut.product_id else 0,
                    "lot_id": offcut.lot_id.id if offcut.lot_id else 0,
                    "width_mm": int(offcut.width_mm or 0),
                    "height_mm": int(offcut.height_mm or 0),
                    "area_mm2": float(offcut.width_mm * offcut.height_mm),
                    "unit_cost": float(offcut.remaining_value or 0.0),
                    "effective_cost_per_area": float((offcut.remaining_value or 0.0) / area_mm2) if area_mm2 > 0 else 0.0,
                }
            )
        return sorted(sources, key=self._source_sort_key)

    def _sheet_lot_sources(self):
        lots = self.mo._tp_compatible_sheet_lots(self.product, self.material_identity)
        sources = []
        for lot in lots:
            area_mm2 = float((lot.tp_width_mm or 0) * (lot.tp_height_mm or 0))
            unit_cost = float(lot.product_id.standard_price or 0.0)
            sources.append(
                {
                    "kind": "sheet_lot",
                    "stable_id": f"sheet_lot:{lot.id}",
                    "id": lot.id,
                    "record": lot,
                    "product_id": lot.product_id.id if lot.product_id else 0,
                    "lot_id": lot.id,
                    "width_mm": int(lot.tp_width_mm or 0),
                    "height_mm": int(lot.tp_height_mm or 0),
                    "area_mm2": area_mm2,
                    "unit_cost": unit_cost,
                    "effective_cost_per_area": float(unit_cost / area_mm2) if area_mm2 > 0 else 0.0,
                }
            )
        return sorted(sources, key=self._source_sort_key)

    def _sheet_product_sources(self):
        sources = []
        product_entries = self.mo._tp_compatible_sheet_products(self.product, self.material_identity)
        for product, unit_count in product_entries:
            width_mm = int(product.tp_sheet_width_mm or 0)
            height_mm = int(product.tp_sheet_height_mm or 0)
            area_mm2 = float(width_mm * height_mm)
            unit_cost = float(product.standard_price or 0.0)
            for unit_idx in range(1, int(unit_count) + 1):
                synthetic_id = -int(product.id * 100000 + unit_idx)
                sources.append(
                    {
                        "kind": "sheet_product",
                        "stable_id": f"sheet_product:{product.id}:unit:{unit_idx}",
                        "id": synthetic_id,
                        "record": product,
                        "product_id": product.id,
                        "lot_id": 0,
                        "width_mm": width_mm,
                        "height_mm": height_mm,
                        "area_mm2": area_mm2,
                        "unit_cost": unit_cost,
                        "effective_cost_per_area": float(unit_cost / area_mm2) if area_mm2 > 0 else 0.0,
                    }
                )
        return sorted(sources, key=self._source_sort_key)

    def _sheet_format_sources(self):
        sheets = self.mo._tp_compatible_sheet_formats(self.product, self.material_identity)
        sources = []
        for sheet in sheets:
            area_mm2 = float(sheet.area_mm2 or 0.0)
            unit_cost = float(sheet.landed_cost or sheet.product_id.standard_price or 0.0)
            sources.append(
                {
                    "kind": "sheet_format",
                    "stable_id": f"sheet_format:{sheet.id}",
                    "id": sheet.id,
                    "record": sheet,
                    "product_id": sheet.product_id.id if sheet.product_id else 0,
                    "lot_id": 0,
                    "width_mm": int(sheet.width_mm or 0),
                    "height_mm": int(sheet.height_mm or 0),
                    "area_mm2": float(sheet.area_mm2 or 0.0),
                    "unit_cost": unit_cost,
                    "effective_cost_per_area": float(unit_cost / area_mm2) if area_mm2 > 0 else 0.0,
                }
            )
        return sorted(sources, key=self._source_sort_key)

    def build(self):
        if self._cache is not None:
            return self._cache
        offcut_sources = self._offcut_sources()
        sheet_lot_sources = self._sheet_lot_sources()
        sheet_product_sources = self._sheet_product_sources()
        sheet_stock_sources = sorted(sheet_lot_sources + sheet_product_sources, key=self._source_sort_key)
        sheet_format_sources = self._sheet_format_sources()
        all_sources = sorted(
            offcut_sources + sheet_stock_sources + sheet_format_sources,
            key=self._source_sort_key,
        )
        self._cache = {
            "offcut_sources": offcut_sources,
            "sheet_lot_sources": sheet_lot_sources,
            "sheet_product_sources": sheet_product_sources,
            "sheet_stock_sources": sheet_stock_sources,
            "sheet_format_sources": sheet_format_sources,
            "all_sources": all_sources,
        }
        return self._cache

    def offcut_sources(self):
        return list(self.build()["offcut_sources"])

    def sheet_lot_sources(self):
        return list(self.build()["sheet_lot_sources"])

    def sheet_product_sources(self):
        return list(self.build()["sheet_product_sources"])

    def sheet_stock_sources(self):
        return list(self.build()["sheet_stock_sources"])

    def sheet_format_sources(self):
        return list(self.build()["sheet_format_sources"])

    def all_sources(self):
        return list(self.build()["all_sources"])
