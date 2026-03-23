import re
from html import escape

from odoo import fields, models
from odoo.exceptions import ValidationError

from .services.tp_nesting_source_pool import TpNestingSourcePool


class MrpProduction(models.Model):
    _inherit = "mrp.production"

    x_tp_source_so_line_id = fields.Many2one(
        "sale.order.line",
        string="Source SO Line",
        readonly=True,
        index=True,
    )
    tp_last_nesting_run_id = fields.Many2one("tp.nesting.run", readonly=True)
    tp_last_nesting_job_id = fields.Many2one(
        "tp.nesting.job",
        related="tp_last_nesting_run_id.job_id",
        store=True,
        readonly=True,
    )
    tp_last_nesting_svg = fields.Html(
        related="tp_last_nesting_run_id.nesting_svg",
        readonly=True,
        sanitize=False,
    )
    tp_nesting_state = fields.Selection(
        [("draft", "Draft"), ("done", "Done"), ("failed", "Failed")],
        readonly=True,
        default="draft",
    )
    tp_nesting_allocation_ids = fields.One2many(
        "tp.nesting.allocation",
        "mo_id",
        readonly=True,
    )
    tp_last_nesting_allocation_ids = fields.One2many(
        related="tp_last_nesting_run_id.allocation_ids",
        readonly=True,
    )
    tp_last_produced_panel_ids = fields.One2many(
        related="tp_last_nesting_run_id.produced_panel_ids",
        readonly=True,
    )
    tp_last_produced_offcut_ids = fields.One2many(
        related="tp_last_nesting_run_id.produced_offcut_ids",
        readonly=True,
    )
    tp_cut_line_ids = fields.One2many("tp.mo.cut.line", "mo_id", string="Cut Lines")
    tp_scope_cut_count = fields.Integer(
        string="Scope Cut Lines",
        compute="_compute_tp_scope_cut_summary",
        readonly=True,
    )
    tp_scope_cut_summary = fields.Text(
        string="All Sizes In Scope",
        compute="_compute_tp_scope_cut_summary",
        readonly=True,
    )
    tp_source_pool_preview = fields.Html(
        string="Source Pool Preview",
        compute="_compute_tp_source_pool_preview",
        readonly=True,
        sanitize=False,
    )

    def _compute_tp_scope_cut_summary(self):
        for mo in self:
            lines = []
            scope_mos = mo._tp_get_nesting_scope_mos()
            for scoped_mo in scope_mos:
                for cut_line in scoped_mo.tp_cut_line_ids:
                    lines.append(f"{cut_line.width_mm} x {cut_line.height_mm} mm x {cut_line.quantity}")
            mo.tp_scope_cut_count = len(lines)
            mo.tp_scope_cut_summary = "\n".join(lines)

    @staticmethod
    def _tp_format_material_identity(material_identity):
        labels = {
            "tp_material_type": "Material",
            "tp_thickness_mm": "Thickness",
            "tp_colour": "Colour",
            "tp_finish": "Finish",
            "tp_protective_film": "Film",
            "tp_brand_supplier": "Brand",
        }
        parts = []
        for key in [
            "tp_material_type",
            "tp_thickness_mm",
            "tp_colour",
            "tp_finish",
            "tp_protective_film",
            "tp_brand_supplier",
        ]:
            value = material_identity.get(key)
            if value in (False, None, ""):
                continue
            if key == "tp_thickness_mm":
                parts.append(f"{labels[key]}: {float(value):g} mm")
            else:
                parts.append(f"{labels[key]}: {value}")
        return ", ".join(parts) if parts else "No TP material filters set."

    def _compute_tp_source_pool_preview(self):
        for mo in self:
            try:
                scope_mos = mo._tp_get_nesting_scope_mos()
                base_mo = scope_mos[:1] and scope_mos[0] or mo
                demand_product = base_mo.x_tp_source_so_line_id.product_id or base_mo.product_id
                if not demand_product:
                    mo.tp_source_pool_preview = "<p>No demand product found for source pool preview.</p>"
                    continue

                material_identity = base_mo._tp_get_material_identity()
                source_pool = TpNestingSourcePool(
                    mo=mo,
                    product=demand_product,
                    material_identity=material_identity,
                )
                offcut_sources = source_pool.offcut_sources()
                sheet_product_sources = source_pool.sheet_product_sources()
                sheet_lot_sources = source_pool.sheet_lot_sources()
                sheet_format_sources = source_pool.sheet_format_sources()

                grouped_sheet_products = {}
                for source in sheet_product_sources:
                    key = (
                        int(source.get("product_id") or 0),
                        int(source.get("width_mm") or 0),
                        int(source.get("height_mm") or 0),
                    )
                    bucket = grouped_sheet_products.setdefault(
                        key,
                        {
                            "name": source["record"].display_name,
                            "width_mm": int(source.get("width_mm") or 0),
                            "height_mm": int(source.get("height_mm") or 0),
                            "units": 0,
                            "unit_cost": float(source.get("unit_cost") or 0.0),
                        },
                    )
                    bucket["units"] += 1

                sheet_product_rows = sorted(
                    grouped_sheet_products.values(),
                    key=lambda r: (r["width_mm"] * r["height_mm"], r["name"]),
                )

                parts = [
                    '<div class="o_tp_source_pool_preview">',
                    f"<p><strong>Demand Product:</strong> {escape(demand_product.display_name)}</p>",
                    f"<p><strong>Material Filter:</strong> {escape(self._tp_format_material_identity(material_identity))}</p>",
                    (
                        "<p><strong>Pool Summary:</strong> "
                        f"{len(offcut_sources)} offcuts, "
                        f"{len(sheet_product_rows)} sheet SKUs, "
                        f"{len(sheet_lot_sources)} sheet lots, "
                        f"{len(sheet_format_sources)} sheet formats</p>"
                    ),
                ]

                if sheet_product_rows:
                    parts.append(
                        "<table class='o_list_view table table-sm'>"
                        "<thead><tr>"
                        "<th>Sheet SKU</th><th>Size (mm)</th><th>Units Considered</th><th>Unit Cost</th>"
                        "</tr></thead><tbody>"
                    )
                    for row in sheet_product_rows:
                        parts.append(
                            "<tr>"
                            f"<td>{escape(row['name'])}</td>"
                            f"<td>{row['width_mm']} x {row['height_mm']}</td>"
                            f"<td>{row['units']}</td>"
                            f"<td>{row['unit_cost']:.2f}</td>"
                            "</tr>"
                        )
                    parts.append("</tbody></table>")
                else:
                    parts.append("<p>No compatible sheet SKUs found.</p>")

                if offcut_sources:
                    parts.append("<p><strong>Top Offcuts:</strong></p><ul>")
                    for source in offcut_sources[:5]:
                        offcut = source["record"]
                        parts.append(
                            "<li>"
                            f"{escape(offcut.display_name)} - {int(source.get('width_mm') or 0)} x {int(source.get('height_mm') or 0)} mm"
                            "</li>"
                        )
                    if len(offcut_sources) > 5:
                        parts.append(f"<li>...and {len(offcut_sources) - 5} more</li>")
                    parts.append("</ul>")

                parts.append("</div>")
                mo.tp_source_pool_preview = "".join(parts)
            except Exception as exc:  # pragma: no cover - defensive UI safety
                mo.tp_source_pool_preview = (
                    "<p>Could not build source pool preview: "
                    f"{escape(str(exc))}</p>"
                )

    def _tp_get_cut_entries(self):
        self.ensure_one()
        if self.tp_cut_line_ids:
            entries = []
            for cut_line in self.tp_cut_line_ids:
                entries.extend(
                    [{"width_mm": cut_line.width_mm, "height_mm": cut_line.height_mm}] * int(cut_line.quantity)
                )
            if entries:
                return entries
        raise ValidationError("No cut lines found on this MO. Add cut lines before running nesting.")

    def _tp_get_nesting_scope_mos(self):
        self.ensure_one()
        domain = [
            ("company_id", "=", self.company_id.id),
            ("product_id", "=", self.product_id.id),
            ("state", "not in", ["done", "cancel"]),
        ]
        if self.origin:
            domain.append(("origin", "=", self.origin))
        else:
            domain.append(("id", "=", self.id))
        return self.search(domain, order="id asc")

    @staticmethod
    def _tp_fit_source(source_width, source_height, cut_width, cut_height, kerf):
        options = [(cut_width, cut_height, False), (cut_height, cut_width, True)]
        candidates = []
        for fit_w, fit_h, rotated in options:
            if source_width < fit_w + kerf or source_height < fit_h + kerf:
                continue
            rem_w = source_width - fit_w - kerf
            rem_h = source_height
            if rem_w <= 0:
                rem_w = source_width
                rem_h = source_height - fit_h - kerf
            if rem_w < 0 or rem_h < 0:
                rem_w, rem_h = 0, 0
            candidates.append((fit_w, fit_h, rotated, rem_w, rem_h))
        if not candidates:
            return False, False, 0, 0, 0, 0
        # Prefer the orientation that leaves the most usable contiguous remainder.
        best = max(candidates, key=lambda c: (c[3] * c[4], c[3], c[4]))
        return True, best[2], best[0], best[1], best[3], best[4]

    def _tp_release_old_reservations(self):
        self.ensure_one()
        old_reserved = self.env["tp.offcut"].search(
            [("reserved_mo_id", "=", self.id), ("state", "=", "reserved")]
        )
        if old_reserved:
            old_reserved.action_release_reservation()

    def _tp_release_scope_reservations(self, scope_mos):
        reserved = self.env["tp.offcut"].search(
            [("reserved_mo_id", "in", scope_mos.ids), ("state", "=", "reserved")]
        )
        if reserved:
            reserved.action_release_reservation()

    def _tp_get_material_identity(self):
        self.ensure_one()
        source_line = self.x_tp_source_so_line_id
        source_product = self.product_id
        product_tmpl = source_product.product_tmpl_id if source_product else False
        material_fields = [
            "tp_material_type",
            "tp_thickness_mm",
            "tp_colour",
            "tp_finish",
            "tp_protective_film",
            "tp_brand_supplier",
        ]
        identity = {}
        for field_name in material_fields:
            value = False
            if source_line and field_name in source_line._fields:
                value = source_line[field_name]
            if not value and source_product and field_name in source_product._fields:
                value = source_product[field_name]
            if not value and product_tmpl and field_name in product_tmpl._fields:
                value = product_tmpl[field_name]
            identity[field_name] = value
        return identity

    @staticmethod
    def _tp_apply_material_domain(domain, material_identity):
        for field_name, value in material_identity.items():
            if value not in (False, None, ""):
                domain.append((field_name, "=", value))
        return domain

    @staticmethod
    def _tp_has_material_identity(material_identity):
        return any(value not in (False, None, "") for value in material_identity.values())

    @staticmethod
    def _tp_parse_thickness_mm_from_name(name):
        if not name:
            return 0.0
        match = re.search(r"(\d+(?:\.\d+)?)\s*mm", name, flags=re.IGNORECASE)
        if not match:
            return 0.0
        return float(match.group(1))

    def _tp_read_material_value(self, record, field_name):
        value = False
        if field_name in record._fields:
            value = record[field_name]
        if value in (False, None, "") and "product_id" in record._fields and record.product_id:
            product = record.product_id
            if field_name in product._fields:
                value = product[field_name]
            if value in (False, None, "") and product.product_tmpl_id and field_name in product.product_tmpl_id._fields:
                value = product.product_tmpl_id[field_name]
        return value

    def _tp_soft_material_compatible(self, record, material_identity):
        for field_name, expected in material_identity.items():
            if expected in (False, None, ""):
                continue
            actual = self._tp_read_material_value(record, field_name)
            if field_name == "tp_protective_film":
                # Business rule: masking type is allowed as paper/plastic and often
                # unset as "none" on catalog sheet SKUs; treat "none" as unknown.
                if expected in (False, None, "", "none"):
                    continue
                if actual in (False, None, "", "none"):
                    continue
                if expected in ("paper", "plastic") and actual in ("paper", "plastic"):
                    continue
                if actual != expected:
                    return False
                continue
            # Sparse source metadata is allowed; explicit mismatch is not.
            if actual in (False, None, ""):
                continue
            if actual != expected:
                return False
        return True

    def _tp_target_thickness_mm(self, demand_product):
        thickness = float(demand_product.tp_thickness_mm or 0.0)
        if thickness > 0:
            return thickness
        return self._tp_parse_thickness_mm_from_name(demand_product.display_name)

    def _tp_record_matches_thickness(self, record, target_thickness_mm):
        if target_thickness_mm <= 0:
            return True
        thickness = self._tp_read_material_value(record, "tp_thickness_mm")
        if thickness not in (False, None, ""):
            try:
                if abs(float(thickness) - target_thickness_mm) < 0.0001:
                    return True
            except (TypeError, ValueError):
                pass
        name = record.display_name if hasattr(record, "display_name") else ""
        if not name and "product_id" in record._fields and record.product_id:
            name = record.product_id.display_name
        return self._tp_parse_thickness_mm_from_name(name) == target_thickness_mm

    def _tp_get_source_mapping(self, demand_product):
        mappings = self.env["tp.nesting.source.map"].search(
            [("active", "=", True), ("demand_product_id", "=", demand_product.id)],
            order="sequence asc,id asc",
        )
        mapped_lot_ids = set(mappings.mapped("source_lot_id").ids)
        explicit_product_only_ids = set(
            mappings.filtered(lambda m: m.source_product_id and not m.source_lot_id).mapped("source_product_id").ids
        )
        mapped_product_only_ids = self._tp_expand_product_mapping_ids(explicit_product_only_ids)
        mapped_product_ids = set(mapped_product_only_ids)
        mapped_product_ids.update(mappings.mapped("source_lot_id.product_id").ids)
        return mappings, mapped_product_ids, mapped_lot_ids, mapped_product_only_ids

    @staticmethod
    def _tp_non_empty(value):
        return value not in (False, None, "")

    def _tp_material_identity_from_product(self, product):
        self.ensure_one()
        if not product:
            return {}
        product_tmpl = product.product_tmpl_id
        material_fields = [
            "tp_material_type",
            "tp_thickness_mm",
            "tp_colour",
            "tp_finish",
            "tp_protective_film",
            "tp_brand_supplier",
        ]
        identity = {}
        for field_name in material_fields:
            value = False
            if field_name in product._fields:
                value = product[field_name]
            if not value and product_tmpl and field_name in product_tmpl._fields:
                value = product_tmpl[field_name]
            if self._tp_non_empty(value):
                identity[field_name] = value
        return identity

    def _tp_expand_product_mapping_ids(self, explicit_product_ids):
        self.ensure_one()
        Product = self.env["product.product"]
        expanded_ids = set(explicit_product_ids)
        if not explicit_product_ids:
            return expanded_ids

        sheet_domain = [("tp_sheet_width_mm", ">", 0), ("tp_sheet_height_mm", ">", 0)]
        for source_product in Product.browse(list(explicit_product_ids)):
            material_identity = self._tp_material_identity_from_product(source_product)
            if material_identity:
                material_domain = self._tp_apply_material_domain(list(sheet_domain), material_identity)
                candidates = Product.search(material_domain)
                expanded_ids.update(candidates.ids)

            thickness = float(source_product.tp_thickness_mm or 0.0)
            if thickness <= 0:
                thickness = self._tp_parse_thickness_mm_from_name(source_product.display_name)
            if thickness > 0:
                thickness_candidates = Product.search(sheet_domain).filtered(
                    lambda p: abs(float(p.tp_thickness_mm or 0.0) - thickness) < 0.0001
                    or self._tp_parse_thickness_mm_from_name(p.display_name) == thickness
                )
                expanded_ids.update(thickness_candidates.ids)
        return expanded_ids

    def _tp_material_compatible_offcuts(self, product, material_identity):
        # Auto mode: consider all compatible available offcuts by material signature.
        offcuts = self.env["tp.offcut"].search(
            [("state", "=", "available"), ("active", "=", True)],
            order="area_mm2 asc",
        )
        target_thickness = self._tp_target_thickness_mm(product)
        if self._tp_has_material_identity(material_identity):
            offcuts = offcuts.filtered(lambda o: self._tp_soft_material_compatible(o, material_identity))
        if target_thickness > 0:
            offcuts = offcuts.filtered(lambda o: self._tp_record_matches_thickness(o, target_thickness))
        if offcuts:
            return offcuts

        # Fallback: explicit mappings can still seed candidates when material data is sparse.
        mappings, _mapped_product_ids, mapped_lot_ids, mapped_product_only_ids = self._tp_get_source_mapping(product)
        if mappings:
            mapped = self.env["tp.offcut"].search(
                [("state", "=", "available"), ("active", "=", True)],
                order="area_mm2 asc",
            ).filtered(
                lambda o: (o.lot_id and o.lot_id.id in mapped_lot_ids) or o.product_id.id in mapped_product_only_ids
            )
            if target_thickness > 0:
                mapped = mapped.filtered(lambda o: self._tp_record_matches_thickness(o, target_thickness))
            if mapped:
                return mapped
        return self.env["tp.offcut"].browse()

    def _tp_compatible_sheet_formats(self, product, material_identity):
        # Auto mode: include all active sheet formats that match material + thickness.
        sheets = self.env["tp.sheet.format"].search([("active", "=", True)], order="area_mm2 asc")
        target_thickness = self._tp_target_thickness_mm(product)
        if self._tp_has_material_identity(material_identity):
            sheets = sheets.filtered(lambda s: self._tp_soft_material_compatible(s, material_identity))
        if target_thickness > 0:
            sheets = sheets.filtered(lambda s: self._tp_record_matches_thickness(s, target_thickness))
        if sheets:
            return sheets

        # Fallback: explicit mapping rows (if any).
        mappings, mapped_product_ids, _mapped_lot_ids, mapped_product_only_ids = self._tp_get_source_mapping(product)
        if mappings:
            mapped = self.env["tp.sheet.format"].search([("active", "=", True)], order="area_mm2 asc").filtered(
                lambda s: s.product_id.id in mapped_product_ids or s.product_id.id in mapped_product_only_ids
            )
            if target_thickness > 0:
                mapped = mapped.filtered(lambda s: self._tp_record_matches_thickness(s, target_thickness))
            if mapped:
                return mapped
        return self.env["tp.sheet.format"].browse()

    def _tp_compatible_sheet_products(self, product, material_identity):
        mappings, mapped_product_ids, _mapped_lot_ids, mapped_product_only_ids = self._tp_get_source_mapping(product)

        quants = self.env["stock.quant"].search([("location_id.usage", "=", "internal")])
        availability_by_product = {}
        for quant in quants:
            pid = quant.product_id.id
            availability_by_product[pid] = availability_by_product.get(pid, 0.0) + float(
                quant.quantity - quant.reserved_quantity
            )
        # Candidate sheet SKUs come from configured dimensional products, not only on-hand quants.
        products = self.env["product.product"].search([("tp_sheet_width_mm", ">", 0), ("tp_sheet_height_mm", ">", 0)])
        if not products:
            return []
        target_thickness = self._tp_target_thickness_mm(product)
        candidate_products = products
        if self._tp_has_material_identity(material_identity):
            candidate_products = candidate_products.filtered(
                lambda p: self._tp_soft_material_compatible(p, material_identity)
            )
        if target_thickness > 0:
            candidate_products = candidate_products.filtered(
                lambda p: self._tp_record_matches_thickness(p, target_thickness)
            )

        if not candidate_products and mappings:
            # Fallback for sparse metadata: use explicitly mapped products.
            candidate_products = products.filtered(lambda p: p.id in mapped_product_ids or p.id in mapped_product_only_ids)
            if target_thickness > 0:
                candidate_products = candidate_products.filtered(
                    lambda p: self._tp_record_matches_thickness(p, target_thickness)
                )

        max_units = max(1, int(self.company_id.tp_nesting_max_piece_count or 200))
        entries = []
        for candidate in candidate_products.sorted("id"):
            qty_available = int(max(0.0, float(availability_by_product.get(candidate.id, 0.0))))
            if qty_available <= 0:
                continue
            entries.append((candidate, min(qty_available, max_units)))
        if entries:
            return entries

        # Fallback: allow configured matching sheet SKUs even when on-hand quant is zero/missing.
        # This keeps nesting operable in SKU-driven setups where sheet availability is managed outside lots.
        return [(candidate, 1) for candidate in candidate_products.sorted("id")]

    def _tp_compatible_sheet_lots(self, product, material_identity):
        mappings, _mapped_product_ids, mapped_lot_ids, mapped_product_only_ids = self._tp_get_source_mapping(product)
        quant_domain_with_lot = [
            ("lot_id", "!=", False),
            ("location_id.usage", "=", "internal"),
            ("quantity", ">", 0),
        ]
        quants_with_lot = self.env["stock.quant"].search(quant_domain_with_lot)
        available_lots = quants_with_lot.mapped("lot_id").filtered(
            lambda l: not l.tp_is_offcut and l.tp_width_mm > 0 and l.tp_height_mm > 0 and l.product_id
        )
        if not available_lots:
            # Fallback for non-lot-tracked stock: if product has available internal qty,
            # allow its configured lots with dimensions as candidate sheet sources.
            quant_domain_product_level = [("location_id.usage", "=", "internal")]
            quants = self.env["stock.quant"].search(quant_domain_product_level)
            availability_by_product = {}
            for quant in quants:
                pid = quant.product_id.id
                availability_by_product[pid] = availability_by_product.get(pid, 0.0) + float(
                    quant.quantity - quant.reserved_quantity
                )
            available_product_ids = [pid for pid, qty in availability_by_product.items() if qty > 0.0]
            if available_product_ids:
                available_lots = self.env["stock.lot"].search(
                    [
                        ("product_id", "in", available_product_ids),
                        ("tp_is_offcut", "=", False),
                        ("tp_width_mm", ">", 0),
                        ("tp_height_mm", ">", 0),
                    ]
                )
        if not available_lots:
            return self.env["stock.lot"].browse()
        target_thickness = self._tp_target_thickness_mm(product)
        matched = available_lots
        if self._tp_has_material_identity(material_identity):
            matched = matched.filtered(lambda l: self._tp_soft_material_compatible(l, material_identity))
        if target_thickness > 0:
            matched = matched.filtered(lambda l: self._tp_record_matches_thickness(l, target_thickness))
        if matched:
            return matched.sorted(key=lambda l: (l.tp_width_mm * l.tp_height_mm, l.id))

        if mappings:
            mapped = available_lots.filtered(
                lambda l: l.id in mapped_lot_ids or l.product_id.id in mapped_product_only_ids
            )
            if target_thickness > 0:
                mapped = mapped.filtered(lambda l: self._tp_record_matches_thickness(l, target_thickness))
            if mapped:
                return mapped.sorted(key=lambda l: (l.tp_width_mm * l.tp_height_mm, l.id))
        return self.env["stock.lot"].browse()

