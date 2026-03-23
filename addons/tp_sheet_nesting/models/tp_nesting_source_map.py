from odoo import api, fields, models
from odoo.exceptions import ValidationError


class TpNestingSourceMap(models.Model):
    _name = "tp.nesting.source.map"
    _description = "TP Nesting Source Map"
    _order = "sequence asc, id asc"

    name = fields.Char(required=True, copy=False, default="New")
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)
    demand_product_id = fields.Many2one("product.product", required=True, ondelete="cascade")
    source_product_id = fields.Many2one("product.product", ondelete="set null")
    source_lot_id = fields.Many2one("stock.lot", ondelete="set null")
    note = fields.Char()

    @api.onchange("source_lot_id")
    def _onchange_source_lot_id(self):
        for rec in self:
            if rec.source_lot_id and not rec.source_product_id:
                rec.source_product_id = rec.source_lot_id.product_id

    @api.constrains("source_product_id", "source_lot_id")
    def _check_source_definition(self):
        for rec in self:
            if not rec.source_product_id and not rec.source_lot_id:
                raise ValidationError("Set Source Product or Source Lot.")
            if rec.source_lot_id and rec.source_product_id and rec.source_lot_id.product_id != rec.source_product_id:
                raise ValidationError("Source Lot product must match Source Product.")

    @api.constrains("demand_product_id", "source_product_id", "source_lot_id")
    def _check_unique_mapping(self):
        for rec in self:
            dup = self.search(
                [
                    ("id", "!=", rec.id),
                    ("demand_product_id", "=", rec.demand_product_id.id),
                    ("source_product_id", "=", rec.source_product_id.id),
                    ("source_lot_id", "=", rec.source_lot_id.id),
                ],
                limit=1,
            )
            if dup:
                raise ValidationError("This source mapping already exists for the demand product.")
