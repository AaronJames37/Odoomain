import re
import uuid
import logging
from datetime import timedelta
from html import escape

from odoo import api, fields, models
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class TpOffcut(models.Model):
    _name = "tp.offcut"
    _description = "TP Offcut"
    _order = "id desc"
    _rec_name = "name"

    name = fields.Char(required=True, copy=False, default="New")
    active = fields.Boolean(default=True)
    lot_id = fields.Many2one("stock.lot", ondelete="cascade", index=True)
    company_id = fields.Many2one(
        "res.company",
        related="lot_id.company_id",
        store=True,
        readonly=True,
        index=True,
    )
    product_id = fields.Many2one(
        "product.product",
        related="lot_id.product_id",
        store=True,
        readonly=True,
    )
    manual_product_id = fields.Many2one("product.product")
    width_mm = fields.Integer(required=True)
    height_mm = fields.Integer(required=True)
    area_mm2 = fields.Float(compute="_compute_area_mm2", store=True)
    tp_preview_svg = fields.Html(
        string="SVG Preview",
        compute="_compute_tp_preview_svg",
        sanitize=False,
        readonly=True,
    )
    state = fields.Selection(
        [
            ("available", "Available"),
            ("reserved", "Reserved"),
            ("in_use", "In Use"),
            ("sold", "Sold"),
            ("inactive", "Inactive"),
        ],
        default="available",
        required=True,
    )
    source_type = fields.Selection(
        [("sheet", "Sheet"), ("offcut", "Offcut")],
        required=True,
        default="sheet",
    )
    parent_lot_id = fields.Many2one("stock.lot", ondelete="set null")
    parent_offcut_id = fields.Many2one("tp.offcut", ondelete="set null")
    bin_location_id = fields.Many2one(
        "stock.location",
        domain="[('usage', '=', 'internal')]",
    )

    # Material identity attributes for future matching/selection.
    tp_material_type = fields.Char(string="Material Type")
    tp_thickness_mm = fields.Float(string="Thickness (mm)")
    tp_colour = fields.Char(string="Colour")
    tp_finish = fields.Char(string="Finish")
    tp_protective_film = fields.Selection(
        selection=[("paper", "Paper"), ("plastic", "Plastic"), ("none", "None")],
        string="Protective Film",
        default="none",
    )
    tp_brand_supplier = fields.Char(string="Brand/Supplier")
    reserved_mo_id = fields.Many2one("mrp.production", readonly=True)
    sold_at = fields.Datetime(readonly=True, index=True)
    damaged_at = fields.Datetime(readonly=True, index=True)
    currency_id = fields.Many2one(
        "res.currency",
        required=True,
        default=lambda self: self.env.company.currency_id.id,
    )
    remaining_area_mm2 = fields.Float(default=0.0)
    remaining_value = fields.Monetary(currency_field="currency_id", default=0.0)
    parent_remaining_area_mm2 = fields.Float(default=0.0)
    parent_remaining_value_snapshot = fields.Monetary(currency_field="currency_id", default=0.0)
    valuation_reference = fields.Many2one("tp.offcut.valuation.event", readonly=True)

    @staticmethod
    def _tp_material_field_names():
        return [
            "tp_material_type",
            "tp_thickness_mm",
            "tp_colour",
            "tp_finish",
            "tp_protective_film",
            "tp_brand_supplier",
        ]

    def _tp_apply_material_defaults_from_product(self, vals, product):
        for field_name in self._tp_material_field_names():
            if vals.get(field_name) not in (False, None, ""):
                continue
            value = False
            if field_name in product._fields:
                value = product[field_name]
            if not value and field_name in product.product_tmpl_id._fields:
                value = product.product_tmpl_id[field_name]
            if value not in (False, None, ""):
                vals[field_name] = value

    def _tp_apply_material_defaults_from_record(self, vals, record):
        for field_name in self._tp_material_field_names():
            if vals.get(field_name) not in (False, None, ""):
                continue
            value = False
            if field_name in record._fields:
                value = record[field_name]
            if value not in (False, None, ""):
                vals[field_name] = value

    def _tp_apply_material_defaults_from_parent_sources(self, vals, *, parent_lot=False, parent_offcut=False):
        # Parent source metadata is authoritative for remainders and should win over
        # sparse/placeholder offcut SKU metadata.
        if parent_offcut and parent_offcut.exists():
            self._tp_apply_material_defaults_from_record(vals, parent_offcut)
            if parent_offcut.product_id:
                self._tp_apply_material_defaults_from_product(vals, parent_offcut.product_id)

        if parent_lot and parent_lot.exists():
            parent_offcut_from_lot = self.search([("lot_id", "=", parent_lot.id)], limit=1)
            if parent_offcut_from_lot:
                self._tp_apply_material_defaults_from_record(vals, parent_offcut_from_lot)
            if parent_lot.product_id:
                self._tp_apply_material_defaults_from_product(vals, parent_lot.product_id)

    @api.model
    def _tp_code_from_value(self, value, code_map, fallback="UNK"):
        text = (value or "").strip().lower()
        if not text:
            return fallback
        for key, code in code_map.items():
            if key in text:
                return code
        token = "".join(ch for ch in text.upper() if ch.isalnum())
        if len(token) >= 3:
            return token[:3]
        if token:
            return token.ljust(3, "X")
        return fallback

    @api.model
    def _tp_extract_thickness_mm(self, product):
        if product.tp_thickness_mm:
            return float(product.tp_thickness_mm)
        for candidate in [product.default_code, product.display_name]:
            match = re.search(r"(\d+(?:\.\d+)?)\s*mm", candidate or "", flags=re.IGNORECASE)
            if match:
                return float(match.group(1))
        return 0.0

    @api.model
    def _tp_format_thickness_code(self, thickness_mm):
        if thickness_mm <= 0:
            return "00"
        rounded = int(round(float(thickness_mm)))
        return f"{max(0, min(rounded, 99)):02d}"

    @api.model
    def _tp_next_offcut_serial(self, *, product, colour_code, material_code, thickness_code):
        pattern = f"{colour_code}-{material_code}-%-{thickness_code}-%"
        lots = self.env["stock.lot"].search(
            [("product_id", "=", product.id), ("name", "like", pattern)],
            order="id desc",
            limit=200,
        )
        max_serial = -1
        for lot in lots:
            parts = (lot.name or "").split("-")
            if len(parts) < 6:
                continue
            if parts[0] != colour_code or parts[1] != material_code or parts[3] != thickness_code:
                continue
            if parts[2].isdigit():
                max_serial = max(max_serial, int(parts[2]))
        return f"{max_serial + 1:03d}"

    @api.model
    def _tp_generate_offcut_structured_name(self, *, product, width_mm, height_mm):
        colour_map = {
            "clear": "CLR",
            "black": "BLK",
            "white": "WHT",
            "red": "RED",
            "blue": "BLU",
            "green": "GRN",
            "grey": "GRY",
            "gray": "GRY",
            "bronze": "BRZ",
            "opal": "OPL",
            "frost": "FRS",
            "smoke": "SMK",
        }
        material_map = {
            "acrylic": "ACR",
            "polycarbonate": "POL",
            "poly carb": "POL",
            "petg": "PTG",
            "pvc": "PVC",
            "abs": "ABS",
            "hdpe": "HDP",
            "mdpe": "MDP",
        }
        colour_source = product.tp_colour or product.display_name
        material_source = product.tp_material_type or product.display_name
        colour_code = self._tp_code_from_value(colour_source, colour_map, fallback="CLR")
        material_code = self._tp_code_from_value(material_source, material_map, fallback="MAT")
        thickness_code = self._tp_format_thickness_code(self._tp_extract_thickness_mm(product))
        serial_code = self._tp_next_offcut_serial(
            product=product,
            colour_code=colour_code,
            material_code=material_code,
            thickness_code=thickness_code,
        )
        length_code = int(height_mm or 0)
        width_code = int(width_mm or 0)
        return f"{colour_code}-{material_code}-{serial_code}-{thickness_code}-{length_code}-{width_code}"

    @api.depends("width_mm", "height_mm")
    def _compute_area_mm2(self):
        for record in self:
            record.area_mm2 = float(record.width_mm * record.height_mm)

    @api.depends("name", "width_mm", "height_mm", "remaining_area_mm2", "state", "source_type")
    def _compute_tp_preview_svg(self):
        state_color = {
            "available": "#16a34a",
            "reserved": "#f59e0b",
            "in_use": "#2563eb",
            "sold": "#6b7280",
            "inactive": "#ef4444",
        }
        source_fill = {
            "offcut": "#bbf7d0",
            "sheet": "#bfdbfe",
        }
        source_stroke = {
            "offcut": "#15803d",
            "sheet": "#1d4ed8",
        }
        state_labels = dict(self._fields["state"].selection)
        source_labels = dict(self._fields["source_type"].selection)

        canvas_w = 360
        canvas_h = 230
        max_draw_w = 300.0
        max_draw_h = 140.0

        for record in self:
            width = max(int(record.width_mm or 0), 0)
            height = max(int(record.height_mm or 0), 0)
            if width <= 0 or height <= 0:
                record.tp_preview_svg = "<p class='text-muted'>No dimensions to preview.</p>"
                continue

            scale = min(max_draw_w / float(width), max_draw_h / float(height))
            draw_w = max(8, int(round(width * scale)))
            draw_h = max(8, int(round(height * scale)))
            rect_x = (canvas_w - draw_w) // 2
            rect_y = 42 + (int(max_draw_h) - draw_h) // 2

            full_area = float(width * height)
            remaining_area = float(record.remaining_area_mm2 or 0.0)
            remaining_pct = 0.0
            if full_area > 0:
                remaining_pct = max(0.0, min(100.0, (remaining_area / full_area) * 100.0))

            state_value = record.state or ""
            source_value = record.source_type or ""
            state_text = state_labels.get(state_value, state_value or "-")
            source_text = source_labels.get(source_value, source_value or "-")

            fill = source_fill.get(source_value, "#e5e7eb")
            stroke = source_stroke.get(source_value, "#374151")
            badge = state_color.get(state_value, "#6b7280")
            title = escape(record.display_name or record.name or "Offcut")
            dims = f"{width} x {height} mm"
            remaining_text = f"Remaining: {remaining_area:,.0f} mm² ({remaining_pct:.1f}%)"

            record.tp_preview_svg = f"""
<div class="o_tp_offcut_preview">
  <svg viewBox="0 0 {canvas_w} {canvas_h}" width="100%" height="230" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Offcut Preview">
    <rect x="0" y="0" width="{canvas_w}" height="{canvas_h}" fill="#f8fafc"/>
    <text x="16" y="20" font-size="13" font-weight="600" fill="#111827">{title}</text>
    <text x="16" y="36" font-size="11" fill="#374151">{escape(source_text)} • {escape(state_text)}</text>
    <rect x="{rect_x}" y="{rect_y}" width="{draw_w}" height="{draw_h}" rx="4" ry="4" fill="{fill}" stroke="{stroke}" stroke-width="2"/>
    <rect x="{rect_x}" y="{rect_y - 16}" width="74" height="14" rx="7" ry="7" fill="{badge}" opacity="0.95"/>
    <text x="{rect_x + 37}" y="{rect_y - 6}" text-anchor="middle" font-size="9" fill="#ffffff">{escape(state_text)}</text>
    <text x="{canvas_w // 2}" y="{rect_y + draw_h + 18}" text-anchor="middle" font-size="12" fill="#111827">{escape(dims)}</text>
    <text x="{canvas_w // 2}" y="{rect_y + draw_h + 34}" text-anchor="middle" font-size="11" fill="#4b5563">{escape(remaining_text)}</text>
  </svg>
</div>
"""

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            parent_lot = self.env["stock.lot"].browse(vals.get("parent_lot_id")) if vals.get("parent_lot_id") else False
            parent_offcut = (
                self.env["tp.offcut"].browse(vals.get("parent_offcut_id"))
                if vals.get("parent_offcut_id")
                else False
            )
            if not vals.get("lot_id"):
                product_id = False
                if parent_lot and parent_lot.exists():
                    product_id = parent_lot.product_id.id
                elif parent_offcut and parent_offcut.exists():
                    product_id = parent_offcut.product_id.id
                elif vals.get("manual_product_id"):
                    product_id = vals.get("manual_product_id")
                elif vals.get("product_id"):
                    product_id = vals.get("product_id")
                elif self.env.context.get("default_product_id"):
                    product_id = self.env.context.get("default_product_id")
                if not product_id:
                    raise ValidationError(
                        "Cannot auto-create offcut lot. Choose Product when creating a manual offcut with no parent."
                    )
                product = self.env["product.product"].browse(product_id)
                self._tp_apply_material_defaults_from_parent_sources(
                    vals,
                    parent_lot=parent_lot,
                    parent_offcut=parent_offcut,
                )
                self._tp_apply_material_defaults_from_product(vals, product)
                width_mm = int(vals.get("width_mm") or 0)
                height_mm = int(vals.get("height_mm") or 0)
                lot_name = vals.get("name") or "OFFCUT"
                if width_mm > 0 and height_mm > 0:
                    lot_name = self._tp_generate_offcut_structured_name(
                        product=product,
                        width_mm=width_mm,
                        height_mm=height_mm,
                    )
                parent_reference_lot_id = False
                if parent_lot and parent_lot.exists():
                    parent_reference_lot_id = parent_lot.id
                elif parent_offcut and parent_offcut.exists() and parent_offcut.lot_id:
                    parent_reference_lot_id = parent_offcut.lot_id.id
                vals["lot_id"] = self.env["stock.lot"].create(
                    {
                        "name": f"{lot_name}-LOT-{uuid.uuid4().hex[:8].upper()}",
                        "product_id": product_id,
                        "company_id": self.env.company.id,
                        "tp_is_offcut": True,
                        "tp_parent_lot_id": parent_reference_lot_id,
                        "tp_width_mm": width_mm,
                        "tp_height_mm": height_mm,
                    }
                ).id
                if vals.get("name") in (False, None, "", "New", "OFFCUT"):
                    vals["name"] = lot_name
                vals.setdefault("manual_product_id", product_id)
            if vals.get("lot_id"):
                lot = self.env["stock.lot"].browse(vals["lot_id"])
                if lot.exists():
                    vals.setdefault("manual_product_id", lot.product_id.id)
                    self._tp_apply_material_defaults_from_parent_sources(
                        vals,
                        parent_lot=parent_lot,
                        parent_offcut=parent_offcut,
                    )
                    if lot.product_id:
                        self._tp_apply_material_defaults_from_product(vals, lot.product_id)
                    if not vals.get("width_mm") or not vals.get("height_mm"):
                        vals["height_mm"] = vals.get("height_mm") or lot.tp_height_mm
                        vals["width_mm"] = vals.get("width_mm") or lot.tp_width_mm
                    if vals.get("name") in (False, None, "", "New"):
                        vals["name"] = lot.name
                    if (
                        lot.tp_is_offcut
                        and vals.get("width_mm")
                        and vals.get("height_mm")
                        and (not lot.tp_width_mm or not lot.tp_height_mm)
                    ):
                        lot.write(
                            {
                                "tp_width_mm": int(vals["width_mm"]),
                                "tp_height_mm": int(vals["height_mm"]),
                            }
                        )
            if not vals.get("remaining_area_mm2"):
                width = vals.get("width_mm") or 0
                height = vals.get("height_mm") or 0
                vals["remaining_area_mm2"] = float(width * height)
        records = super().create(vals_list)
        for record in records.filtered(lambda r: not r.bin_location_id):
            record._assign_bin_location_from_rules()
        return records

    def write(self, vals):
        res = super().write(vals)
        if any(field in vals for field in ("width_mm", "height_mm")):
            for record in self.filtered(lambda r: not r.bin_location_id):
                record._assign_bin_location_from_rules()
        return res

    @api.onchange("lot_id")
    def _onchange_lot_id_set_dimensions(self):
        for rec in self:
            if rec.lot_id:
                rec.manual_product_id = rec.lot_id.product_id
                if not rec.height_mm and rec.lot_id.tp_height_mm:
                    rec.height_mm = rec.lot_id.tp_height_mm
                if not rec.width_mm and rec.lot_id.tp_width_mm:
                    rec.width_mm = rec.lot_id.tp_width_mm

    @api.constrains("width_mm", "height_mm")
    def _check_dimensions(self):
        for record in self:
            if record.width_mm <= 0 or record.height_mm <= 0:
                raise ValidationError("Offcut width and height must be greater than 0 mm.")
            if record.width_mm < 200 or record.height_mm < 200:
                raise ValidationError("Offcut must be at least 200x200 mm.")
            if record.remaining_area_mm2 < 0:
                raise ValidationError("Remaining area cannot be negative.")
            if record.remaining_value < 0:
                raise ValidationError("Remaining value cannot be negative.")

    @api.constrains("source_type", "parent_lot_id", "parent_offcut_id")
    def _check_parent_source(self):
        for record in self:
            if record.source_type == "offcut" and not record.parent_offcut_id:
                raise ValidationError("Offcut source offcuts require a parent offcut.")

    @api.constrains("lot_id", "source_type", "parent_lot_id", "parent_offcut_id")
    def _check_own_lot_identity(self):
        for record in self:
            if not record.lot_id:
                raise ValidationError("Offcut requires its own lot.")
            if record.source_type == "sheet" and record.parent_lot_id and record.lot_id == record.parent_lot_id:
                raise ValidationError("Offcut lot must be different from parent sheet lot.")
            if (
                record.source_type == "offcut"
                and record.parent_offcut_id
                and record.parent_offcut_id.lot_id
                and record.lot_id == record.parent_offcut_id.lot_id
            ):
                raise ValidationError("Offcut lot must be different from parent offcut lot.")

    @api.constrains("lot_id")
    def _check_unique_lot(self):
        for record in self:
            duplicate = self.search([("lot_id", "=", record.lot_id.id), ("id", "!=", record.id)], limit=1)
            if duplicate:
                raise ValidationError("An offcut already exists for this lot.")

    def _assign_bin_location_from_rules(self):
        rules = self.env["tp.offcut.bin.rule"].search([("active", "=", True)], order="sequence asc")
        for record in self:
            for rule in rules:
                if record.width_mm >= rule.min_width_mm and record.height_mm >= rule.min_height_mm:
                    record.bin_location_id = rule.bin_location_id
                    break

    @api.model
    def _compute_area_value(self, parent_value, parent_area, child_area):
        if parent_area <= 0:
            raise ValidationError("Parent remaining area must be greater than zero.")
        if child_area < 0:
            raise ValidationError("Child area cannot be negative.")
        value = float(parent_value) * (float(child_area) / float(parent_area))
        return self.env.company.currency_id.round(value)

    @api.model
    def _assert_value_conservation(self, input_value, components, tolerance=0.01):
        total = sum(float(v or 0.0) for v in components)
        delta = self.env.company.currency_id.round(float(input_value or 0.0) - total)
        return abs(delta) <= tolerance, delta

    @api.model
    def _create_valuation_event(self, values):
        event = self.env["tp.offcut.valuation.event"].create(values)
        if not event.is_conserved:
            raise ValidationError(
                f"Value conservation failed. Delta={event.conservation_delta} exceeds tolerance."
            )
        return event

    @api.model
    def create_offcut_from_sheet(
        self,
        *,
        lot_id=False,
        parent_lot_id,
        width_mm,
        height_mm,
        parent_remaining_area_mm2,
        parent_remaining_value,
        mo_id=False,
        run_id=False,
        name=False,
    ):
        parent_lot = self.env["stock.lot"].browse(parent_lot_id)
        if not parent_lot.exists():
            raise ValidationError("Parent lot is required to create a sheet remainder offcut.")
        if not lot_id:
            lot_name = self._tp_generate_offcut_structured_name(
                product=parent_lot.product_id,
                width_mm=width_mm,
                height_mm=height_mm,
            )
            lot_id = self.env["stock.lot"].create(
                {
                    "name": lot_name,
                    "product_id": parent_lot.product_id.id,
                    "company_id": self.env.company.id,
                    "tp_is_offcut": True,
                    "tp_parent_lot_id": parent_lot.id,
                    "tp_width_mm": int(width_mm),
                    "tp_height_mm": int(height_mm),
                }
            ).id
        lot = self.env["stock.lot"].browse(lot_id)
        if name in (False, None, "", "New", "OFFCUT"):
            name = lot.name
        child_area = float(width_mm * height_mm)
        if child_area > parent_remaining_area_mm2:
            raise ValidationError("Offcut area cannot exceed parent remaining area.")
        child_value = self._compute_area_value(parent_remaining_value, parent_remaining_area_mm2, child_area)
        remainder_area = float(parent_remaining_area_mm2 - child_area)
        remainder_value = self.env.company.currency_id.round(float(parent_remaining_value) - child_value)
        is_conserved, delta = self._assert_value_conservation(
            parent_remaining_value, [child_value, remainder_value]
        )
        event = self._create_valuation_event(
            {
                "event_type": "sheet_to_offcut",
                "parent_lot_id": parent_lot_id,
                "mo_id": mo_id,
                "input_area_mm2": parent_remaining_area_mm2,
                "input_value": parent_remaining_value,
                "offcut_area_mm2": child_area,
                "offcut_value": child_value,
                "remainder_area_mm2": remainder_area,
                "remainder_value": remainder_value,
                "is_conserved": is_conserved,
                "conservation_delta": delta,
                "currency_id": self.env.company.currency_id.id,
            }
        )
        create_vals = {
            "name": name,
            "lot_id": lot_id,
            "width_mm": width_mm,
            "height_mm": height_mm,
            "source_type": "sheet",
            "parent_lot_id": parent_lot_id,
            "remaining_area_mm2": child_area,
            "remaining_value": child_value,
            "parent_remaining_area_mm2": parent_remaining_area_mm2,
            "parent_remaining_value_snapshot": parent_remaining_value,
            "valuation_reference": event.id,
        }
        if "produced_in_run_id" in self._fields:
            create_vals["produced_in_run_id"] = run_id
        offcut = self.create(create_vals)
        event.offcut_id = offcut.id
        return offcut

    def record_remainder(self, *, width_mm, height_mm, mo_id=False, run_id=False, kerf_mm=3, lot_id=False, name="New"):
        self.ensure_one()
        if self.remaining_area_mm2 <= 0:
            raise ValidationError("Parent offcut has no remaining area.")
        child_area = float(width_mm * height_mm)
        if child_area > self.remaining_area_mm2:
            raise ValidationError("Remainder area cannot exceed parent remaining area.")
        child_value = self._compute_area_value(self.remaining_value, self.remaining_area_mm2, child_area)
        remainder_area = float(self.remaining_area_mm2 - child_area)
        remainder_value = self.currency_id.round(float(self.remaining_value) - child_value)

        if width_mm < 200 or height_mm < 200:
            waste = self.env["tp.offcut.waste"].create(
                {
                    "name": f"WASTE-{self.name}",
                    "mo_id": mo_id,
                    "parent_source_type": "offcut",
                    "parent_offcut_id": self.id,
                    "parent_lot_id": self.parent_lot_id.id,
                    "product_id": self.product_id.id,
                    "width_mm": width_mm,
                    "height_mm": height_mm,
                    "kerf_mm": kerf_mm,
                    "waste_value": child_value,
                    "currency_id": self.currency_id.id,
                }
            )
            is_conserved, delta = self._assert_value_conservation(
                self.remaining_value, [child_value, remainder_value]
            )
            event = self._create_valuation_event(
                {
                    "event_type": "waste",
                    "parent_offcut_id": self.id,
                    "parent_lot_id": self.parent_lot_id.id or self.lot_id.id,
                    "mo_id": mo_id,
                    "input_area_mm2": self.remaining_area_mm2,
                    "input_value": self.remaining_value,
                    "waste_area_mm2": child_area,
                    "waste_value": child_value,
                    "remainder_area_mm2": remainder_area,
                    "remainder_value": remainder_value,
                    "is_conserved": is_conserved,
                    "conservation_delta": delta,
                    "currency_id": self.currency_id.id,
                }
            )
            waste.valuation_event_id = event.id
            self.write(
                {
                    "remaining_area_mm2": remainder_area,
                    "remaining_value": remainder_value,
                    "valuation_reference": event.id,
                }
            )
            return waste

        if not lot_id:
            lot_name = self._tp_generate_offcut_structured_name(
                product=self.product_id,
                width_mm=width_mm,
                height_mm=height_mm,
            )
            lot_id = self.env["stock.lot"].create(
                {
                    "name": lot_name,
                    "product_id": self.product_id.id,
                    "company_id": self.env.company.id,
                    "tp_is_offcut": True,
                    "tp_parent_lot_id": self.lot_id.id,
                    "tp_width_mm": int(width_mm),
                    "tp_height_mm": int(height_mm),
                }
            ).id
        lot = self.env["stock.lot"].browse(lot_id)
        if name in (False, None, "", "New", "OFFCUT"):
            name = lot.name
        create_vals = {
            "name": name,
            "lot_id": lot_id,
            "width_mm": width_mm,
            "height_mm": height_mm,
            "source_type": "offcut",
            "parent_offcut_id": self.id,
            "remaining_area_mm2": child_area,
            "remaining_value": child_value,
            "parent_remaining_area_mm2": self.remaining_area_mm2,
            "parent_remaining_value_snapshot": self.remaining_value,
        }
        if "produced_in_run_id" in self._fields:
            create_vals["produced_in_run_id"] = run_id
        child = self.create(create_vals)
        is_conserved, delta = self._assert_value_conservation(self.remaining_value, [child_value, remainder_value])
        event = self._create_valuation_event(
            {
                "event_type": "offcut_to_remainder",
                "offcut_id": child.id,
                "parent_offcut_id": self.id,
                "parent_lot_id": self.parent_lot_id.id or self.lot_id.id,
                "mo_id": mo_id,
                "input_area_mm2": self.remaining_area_mm2,
                "input_value": self.remaining_value,
                "offcut_area_mm2": child_area,
                "offcut_value": child_value,
                "remainder_area_mm2": remainder_area,
                "remainder_value": remainder_value,
                "is_conserved": is_conserved,
                "conservation_delta": delta,
                "currency_id": self.currency_id.id,
            }
        )
        child.valuation_reference = event.id
        self.write(
            {
                "remaining_area_mm2": remainder_area,
                "remaining_value": remainder_value,
                "valuation_reference": event.id,
            }
        )
        return child

    def action_set_available(self):
        vals = {"state": "available"}
        if "reservation_run_id" in self._fields:
            vals["reservation_run_id"] = False
        self.write(vals)

    def action_set_in_use(self):
        self.write({"state": "in_use"})

    def action_set_sold(self):
        for record in self:
            company = record.company_id or self.env.company
            vals = {
                "state": "sold",
                "sold_at": fields.Datetime.now(),
                "reserved_mo_id": False,
            }
            if company.tp_offcut_sold_bin_location_id:
                vals["bin_location_id"] = company.tp_offcut_sold_bin_location_id.id
            if "reservation_run_id" in self._fields:
                vals["reservation_run_id"] = False
            record.write(vals)

    def action_mark_damaged(self):
        vals = {
            "state": "inactive",
            "active": False,
            "damaged_at": fields.Datetime.now(),
            "reserved_mo_id": False,
        }
        if "reservation_run_id" in self._fields:
            vals["reservation_run_id"] = False
        self.write(vals)

    def action_archive(self):
        vals = {
            "state": "inactive",
            "active": False,
            "reserved_mo_id": False,
        }
        if "reservation_run_id" in self._fields:
            vals["reservation_run_id"] = False
        self.write(vals)

    def action_set_reserved(self, mo_id, run_id=False):
        vals = {
            "state": "reserved",
            "reserved_mo_id": mo_id,
        }
        if "reservation_run_id" in self._fields:
            vals["reservation_run_id"] = run_id
        self.write(vals)

    def action_release_reservation(self):
        vals = {
            "state": "available",
            "reserved_mo_id": False,
        }
        if "reservation_run_id" in self._fields:
            vals["reservation_run_id"] = False
        self.write(vals)

    @api.model
    def _tp_cleanup_sold_domain(self, company, cutoff):
        domain = [
            ("company_id", "=", company.id),
            ("state", "=", "sold"),
            ("sold_at", "!=", False),
            ("sold_at", "<=", cutoff),
        ]
        if company.tp_offcut_sold_bin_location_id:
            domain.append(("bin_location_id", "=", company.tp_offcut_sold_bin_location_id.id))
        return domain

    @api.model
    def _tp_try_delete_orphan_lot(self, lot):
        if not lot or not lot.exists():
            return False
        # Keep lots that still have inventory or are still linked.
        has_quant = self.env["stock.quant"].with_context(active_test=False).search_count(
            [("lot_id", "=", lot.id), "|", ("quantity", "!=", 0.0), ("reserved_quantity", "!=", 0.0)]
        )
        if has_quant:
            return False
        has_offcut = self.with_context(active_test=False).search_count([("lot_id", "=", lot.id)])
        if has_offcut:
            return False
        try:
            lot.unlink()
            return True
        except Exception as exc:  # pragma: no cover - defensive cleanup logging
            _logger.warning("Could not delete sold offcut lot %s during cleanup: %s", lot.id, exc)
            return False

    @api.model
    def cron_cleanup_sold_offcuts(self):
        companies = self.env["res.company"].sudo().search([])
        now = fields.Datetime.now()
        removed_offcuts = 0
        removed_lots = 0
        for company in companies:
            cleanup_days = max(int(company.tp_offcut_sold_cleanup_days or 30), 1)
            cutoff = now - timedelta(days=cleanup_days)
            domain = self._tp_cleanup_sold_domain(company, cutoff)
            sold_offcuts = self.with_context(active_test=False).sudo().search(domain)
            for offcut in sold_offcuts:
                lot = offcut.lot_id
                offcut.unlink()
                removed_offcuts += 1
                if self._tp_try_delete_orphan_lot(lot.sudo()):
                    removed_lots += 1
        _logger.info(
            "tp.offcut sold cleanup complete: removed_offcuts=%s removed_lots=%s",
            removed_offcuts,
            removed_lots,
        )
        return {"removed_offcuts": removed_offcuts, "removed_lots": removed_lots}
