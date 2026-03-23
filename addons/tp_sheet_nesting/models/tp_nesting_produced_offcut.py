from odoo import api, fields, models
from odoo.exceptions import ValidationError


class TpNestingProducedOffcut(models.Model):
    _name = "tp.nesting.produced.offcut"
    _description = "TP Nesting Produced Offcut"
    _order = "id asc"

    run_id = fields.Many2one("tp.nesting.run", required=True, ondelete="cascade")
    job_id = fields.Many2one(related="run_id.job_id", store=True, readonly=True)
    offcut_id = fields.Many2one("tp.offcut", ondelete="set null", readonly=True)
    waste_id = fields.Many2one("tp.offcut.waste", ondelete="set null", readonly=True)
    state = fields.Selection(
        [("planned", "Planned"), ("materialized", "Materialized")],
        default="planned",
        required=True,
        readonly=True,
    )
    planned_kind = fields.Selection(
        [("offcut", "Offcut"), ("waste", "Waste")],
        default="offcut",
        required=True,
        readonly=True,
    )
    planned_source_type = fields.Selection(
        [("sheet", "Sheet"), ("offcut", "Offcut")],
        required=False,
        readonly=True,
    )
    parent_lot_id = fields.Many2one("stock.lot", ondelete="set null", readonly=True)
    parent_offcut_id = fields.Many2one("tp.offcut", ondelete="set null", readonly=True)
    product_id = fields.Many2one("product.product", required=False, readonly=True)
    planned_width_mm = fields.Integer(default=0, readonly=True)
    planned_height_mm = fields.Integer(default=0, readonly=True)
    area_mm2 = fields.Float(compute="_compute_area_mm2", store=True, readonly=True)
    kerf_mm = fields.Integer(default=3, required=False, readonly=True)
    parent_remaining_area_mm2 = fields.Float(default=0.0, readonly=True)
    currency_id = fields.Many2one(
        "res.currency",
        required=False,
        default=lambda self: self.env.company.currency_id.id,
        readonly=True,
    )
    parent_remaining_value = fields.Monetary(currency_field="currency_id", default=0.0, readonly=True)
    materialized_at = fields.Datetime(readonly=True)

    lot_id = fields.Many2one(
        "stock.lot",
        compute="_compute_display_values",
        store=True,
        readonly=True,
    )
    width_mm = fields.Integer(compute="_compute_display_values", store=True, readonly=True)
    height_mm = fields.Integer(compute="_compute_display_values", store=True, readonly=True)
    source_type = fields.Selection(
        [("sheet", "Sheet"), ("offcut", "Offcut")],
        compute="_compute_display_values",
        store=True,
        readonly=True,
    )

    @api.depends("offcut_id", "waste_id", "planned_width_mm", "planned_height_mm", "planned_source_type")
    def _compute_display_values(self):
        for record in self:
            if record.offcut_id:
                record.lot_id = record.offcut_id.lot_id.id
                record.width_mm = record.offcut_id.width_mm
                record.height_mm = record.offcut_id.height_mm
                record.source_type = record.offcut_id.source_type
                continue
            if record.waste_id:
                record.lot_id = record.waste_id.parent_lot_id.id
                record.width_mm = record.waste_id.width_mm
                record.height_mm = record.waste_id.height_mm
                record.source_type = record.waste_id.parent_source_type
                continue
            record.lot_id = False
            record.width_mm = record.planned_width_mm
            record.height_mm = record.planned_height_mm
            record.source_type = record.planned_source_type

    @api.depends("width_mm", "height_mm")
    def _compute_area_mm2(self):
        for record in self:
            record.area_mm2 = float((record.width_mm or 0) * (record.height_mm or 0))

    @api.constrains("planned_width_mm", "planned_height_mm")
    def _check_planned_dimensions(self):
        for record in self:
            if record.offcut_id or record.waste_id:
                continue
            if record.planned_width_mm <= 0 or record.planned_height_mm <= 0:
                raise ValidationError("Produced offcut plan dimensions must be greater than 0 mm.")

    @api.constrains("planned_kind", "planned_width_mm", "planned_height_mm")
    def _check_planned_kind_dimensions(self):
        for record in self:
            if record.offcut_id or record.waste_id:
                continue
            if record.planned_kind == "offcut" and (
                record.planned_width_mm < 200 or record.planned_height_mm < 200
            ):
                raise ValidationError("Planned offcuts must be at least 200x200 mm.")
            if record.planned_kind == "waste" and (
                record.planned_width_mm >= 200 and record.planned_height_mm >= 200
            ):
                raise ValidationError("Planned waste must be below 200 mm on at least one side.")

    @api.constrains("planned_source_type", "parent_lot_id", "parent_offcut_id")
    def _check_planned_parent_source(self):
        for record in self:
            if record.offcut_id or record.waste_id:
                continue
            if record.planned_source_type == "offcut" and not record.parent_offcut_id:
                raise ValidationError("Offcut-planned remainders require a parent offcut.")
            if record.planned_source_type == "sheet" and not record.parent_lot_id:
                raise ValidationError("Sheet-planned remainders require a parent lot.")
