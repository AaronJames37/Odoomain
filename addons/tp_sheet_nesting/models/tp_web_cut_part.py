from odoo import api, fields, models
from odoo.exceptions import ValidationError


class TpWebCutPart(models.Model):
    _name = "tp.web.cut.part"
    _description = "Website Production Cut Part"
    _order = "sale_order_id, sale_order_line_id, line_index, panel_index, instance_index, id"
    _rec_name = "part_key"

    _CAMELCASE_ALIASES = {
        "partKey": "part_key",
        "orderId": "website_order_id",
        "orderToken": "website_order_token",
        "odooOrderId": "sale_order_id",
        "odooOrderName": "odoo_order_name",
        "saleOrderLineId": "sale_order_line_id",
        "lineIndex": "line_index",
        "panelIndex": "panel_index",
        "instanceIndex": "instance_index",
        "widthMm": "width_mm",
        "heightMm": "height_mm",
        "thickness": "thickness_mm",
        "thicknessMm": "thickness_mm",
        "cutOuts": "cut_outs",
        "customShape": "custom_shape",
        "sourcePayload": "source_payload",
        "geometryPayload": "geometry_payload",
        "brandSupplier": "brand_supplier",
        "protectiveFilm": "protective_film",
        "tpMaterialType": "tp_material_type",
        "tpThicknessMm": "tp_thickness_mm",
        "tpColour": "tp_colour",
        "tpFinish": "tp_finish",
        "tpProtectiveFilm": "tp_protective_film",
        "tpBrandSupplier": "tp_brand_supplier",
    }

    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
        index=True,
        ondelete="cascade",
    )

    part_key = fields.Char(required=True, index=True, copy=False)
    website_order_id = fields.Char(index=True)
    website_order_token = fields.Char(index=True)
    sale_order_id = fields.Many2one("sale.order", index=True, ondelete="cascade")
    odoo_order_name = fields.Char(index=True)
    sale_order_line_id = fields.Many2one("sale.order.line", index=True, ondelete="cascade")
    product_id = fields.Many2one(related="sale_order_line_id.product_id", store=True, readonly=True)

    line_index = fields.Integer(default=0, index=True)
    panel_index = fields.Integer(default=0, index=True)
    instance_index = fields.Integer(default=0, index=True)

    sku = fields.Char(index=True)
    material = fields.Char(index=True)
    colour = fields.Char(index=True)
    finish = fields.Char(index=True)
    protective_film = fields.Char(index=True)
    brand_supplier = fields.Char(index=True)
    thickness_mm = fields.Float(index=True)

    # Odoo-compatible material identity fields, for callers that can send
    # production-normalized material metadata directly.
    tp_material_type = fields.Char(string="Odoo Material Type", index=True)
    tp_thickness_mm = fields.Float(string="Odoo Thickness (mm)", index=True)
    tp_colour = fields.Char(string="Odoo Colour", index=True)
    tp_finish = fields.Char(string="Odoo Finish", index=True)
    tp_protective_film = fields.Selection(
        selection=[("paper", "Paper"), ("plastic", "Plastic"), ("none", "None")],
        string="Odoo Protective Film",
        default="none",
        index=True,
    )
    tp_brand_supplier = fields.Char(string="Odoo Brand/Supplier", index=True)

    width_mm = fields.Integer(required=True, index=True)
    height_mm = fields.Integer(required=True, index=True)
    quantity = fields.Integer(default=1, required=True)
    shape = fields.Char(required=True, default="rectangle", index=True)
    label = fields.Char()

    radii = fields.Json(default=dict)
    holes = fields.Json(default=list)
    cut_outs = fields.Json(default=list)
    custom_shape = fields.Json(default=dict)
    geometry_payload = fields.Json(default=dict)
    source_payload = fields.Json(default=dict)

    mo_cut_line_ids = fields.One2many("tp.mo.cut.line", "source_web_cut_part_id", readonly=True)
    allocation_ids = fields.One2many("tp.nesting.allocation", "web_cut_part_id", readonly=True)

    _sql_constraints = [
        (
            "part_key_company_unique",
            "unique(company_id, part_key)",
            "Website production part keys must be unique per company.",
        ),
    ]

    @api.model
    def _normalize_website_vals(self, vals):
        normalized = dict(vals)
        for source_key, target_key in self._CAMELCASE_ALIASES.items():
            if source_key in normalized and target_key not in normalized:
                normalized[target_key] = normalized.pop(source_key)
            elif source_key in normalized:
                normalized.pop(source_key)

        if "color" in normalized and "colour" not in normalized:
            normalized["colour"] = normalized.pop("color")
        if "cutouts" in normalized and "cut_outs" not in normalized:
            normalized["cut_outs"] = normalized.pop("cutouts")
        if "custom_shape" not in normalized and "custom_shape_json" in normalized:
            normalized["custom_shape"] = normalized.pop("custom_shape_json")

        if normalized.get("sale_order_line_id") and not normalized.get("sale_order_id"):
            line = self.env["sale.order.line"].browse(normalized["sale_order_line_id"])
            if line.exists():
                normalized["sale_order_id"] = line.order_id.id
                normalized.setdefault("odoo_order_name", line.order_id.name)
                normalized.setdefault("company_id", line.company_id.id)

        if normalized.get("sale_order_id") and not normalized.get("odoo_order_name"):
            order = self.env["sale.order"].browse(normalized["sale_order_id"])
            if order.exists():
                normalized["odoo_order_name"] = order.name
                normalized.setdefault("company_id", order.company_id.id)

        if normalized.get("material") and not normalized.get("tp_material_type"):
            normalized["tp_material_type"] = normalized["material"]
        if normalized.get("colour") and not normalized.get("tp_colour"):
            normalized["tp_colour"] = normalized["colour"]
        if normalized.get("finish") and not normalized.get("tp_finish"):
            normalized["tp_finish"] = normalized["finish"]
        if normalized.get("brand_supplier") and not normalized.get("tp_brand_supplier"):
            normalized["tp_brand_supplier"] = normalized["brand_supplier"]
        if normalized.get("thickness_mm") and not normalized.get("tp_thickness_mm"):
            normalized["tp_thickness_mm"] = normalized["thickness_mm"]

        return normalized

    @api.model_create_multi
    def create(self, vals_list):
        return super().create([self._normalize_website_vals(vals) for vals in vals_list])

    def write(self, vals):
        normalized = self._normalize_website_vals(vals)
        dimension_keys = {"width_mm", "height_mm", "quantity"}
        if dimension_keys.intersection(normalized):
            self._tp_check_dimension_edit_allowed()
        res = super().write(normalized)
        if dimension_keys.intersection(normalized):
            self._tp_sync_linked_mo_cut_lines()
        return res

    def _tp_check_dimension_edit_allowed(self):
        for part in self:
            if part.allocation_ids:
                run_names = ", ".join(part.allocation_ids.mapped("run_id.name"))
                raise ValidationError(
                    "This panel has already been allocated to a nesting run%s. "
                    "Cancel or remove that nesting run before changing the panel size."
                    % (": %s" % run_names if run_names else "")
                )

            blocked_mos = part.mo_cut_line_ids.mapped("mo_id").filtered(
                lambda mo: mo.state in ("progress", "to_close", "done")
            )
            if blocked_mos:
                raise ValidationError(
                    "This panel is already being cut or has been completed in MO %s. "
                    "Change the panel size before production starts."
                    % ", ".join(blocked_mos.mapped("name"))
                )

    def _tp_sync_linked_mo_cut_lines(self):
        for part in self:
            cut_lines = part.mo_cut_line_ids.filtered(
                lambda line: line.mo_id.state not in ("progress", "to_close", "done", "cancel")
            )
            if cut_lines:
                cut_lines.write({
                    "width_mm": part.width_mm,
                    "height_mm": part.height_mm,
                    "quantity": part.quantity,
                })

    @api.constrains("width_mm", "height_mm", "quantity")
    def _check_positive_dimensions(self):
        for part in self:
            if part.width_mm <= 0 or part.height_mm <= 0:
                raise ValidationError("Website cut part width and height must be greater than 0 mm.")
            if part.quantity <= 0:
                raise ValidationError("Website cut part quantity must be greater than 0.")

    def _tp_cut_line_vals(self, mo):
        self.ensure_one()
        return {
            "mo_id": mo.id,
            "source_so_line_id": self.sale_order_line_id.id,
            "source_web_cut_part_id": self.id,
            "width_mm": self.width_mm,
            "height_mm": self.height_mm,
            "quantity": self.quantity,
        }
