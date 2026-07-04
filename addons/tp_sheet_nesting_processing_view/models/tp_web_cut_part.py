from odoo import api, fields, models


RECTANGULAR_SHAPES = {"", "rect", "rectangle", "square"}


class TpWebCutPart(models.Model):
    _inherit = "tp.web.cut.part"

    sale_order_fulfillment_status = fields.Selection(
        related="sale_order_id.website_fulfillment_status",
        store=True,
        index=True,
        readonly=True,
        string="Website Fulfillment Status",
    )
    sale_order_fulfillment_status_raw = fields.Char(
        related="sale_order_id.website_fulfillment_status_raw",
        store=True,
        readonly=True,
        string="Website Fulfillment Status (raw)",
    )
    cnc_required = fields.Boolean(
        compute="_compute_cnc_summary",
        store=True,
        index=True,
        string="CNC Required",
    )
    cnc_operation_summary = fields.Char(
        compute="_compute_cnc_summary",
        store=True,
        string="CNC / Machining",
    )

    @staticmethod
    def _tp_json_has_payload(value):
        if value in (False, None, "", 0, 0.0):
            return False
        if isinstance(value, dict):
            return any(TpWebCutPart._tp_json_has_payload(item) for item in value.values())
        if isinstance(value, (list, tuple)):
            return any(TpWebCutPart._tp_json_has_payload(item) for item in value)
        return True

    @staticmethod
    def _tp_json_item_count(value):
        if isinstance(value, (list, tuple)):
            return len([item for item in value if TpWebCutPart._tp_json_has_payload(item)])
        if isinstance(value, dict) and TpWebCutPart._tp_json_has_payload(value):
            return 1
        return 0

    @api.depends("shape", "radii", "holes", "cut_outs", "custom_shape", "geometry_payload")
    def _compute_cnc_summary(self):
        for part in self:
            operations = []
            shape = (part.shape or "").strip().lower()
            if shape not in RECTANGULAR_SHAPES:
                operations.append(part.shape or "custom shape")
            if self._tp_json_has_payload(part.radii):
                operations.append("rounded corners")

            hole_count = self._tp_json_item_count(part.holes)
            if hole_count:
                operations.append("%s hole%s" % (hole_count, "" if hole_count == 1 else "s"))

            cut_out_count = self._tp_json_item_count(part.cut_outs)
            if cut_out_count:
                operations.append("%s cut out%s" % (cut_out_count, "" if cut_out_count == 1 else "s"))

            if self._tp_json_has_payload(part.custom_shape):
                operations.append("custom shape data")
            if self._tp_json_has_payload(part.geometry_payload):
                operations.append("geometry payload")

            part.cnc_required = bool(operations)
            part.cnc_operation_summary = ", ".join(operations) if operations else "Straight cut"
