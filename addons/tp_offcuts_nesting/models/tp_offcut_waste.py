from odoo import api, fields, models
from odoo.exceptions import ValidationError


class TpOffcutWaste(models.Model):
    _name = "tp.offcut.waste"
    _description = "TP Offcut Waste"
    _order = "id desc"

    name = fields.Char(required=True, copy=False, default="New")
    mo_id = fields.Many2one("mrp.production")
    parent_source_type = fields.Selection([("sheet", "Sheet"), ("offcut", "Offcut")], required=True)
    parent_lot_id = fields.Many2one("stock.lot", ondelete="set null")
    parent_offcut_id = fields.Many2one("tp.offcut", ondelete="set null")
    product_id = fields.Many2one("product.product", required=True)
    width_mm = fields.Integer(required=True)
    height_mm = fields.Integer(required=True)
    area_mm2 = fields.Float(compute="_compute_area_mm2", store=True)
    kerf_mm = fields.Integer(default=3, required=True)
    currency_id = fields.Many2one(
        "res.currency",
        required=True,
        default=lambda self: self.env.company.currency_id.id,
    )
    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company.id,
        index=True,
    )
    waste_value = fields.Monetary(required=True, currency_field="currency_id")
    valuation_event_id = fields.Many2one("tp.offcut.valuation.event")
    timestamp = fields.Datetime(default=fields.Datetime.now, required=True)
    account_move_id = fields.Many2one("account.move", readonly=True)

    @api.depends("width_mm", "height_mm")
    def _compute_area_mm2(self):
        for record in self:
            record.area_mm2 = float(record.width_mm * record.height_mm)

    @api.constrains("width_mm", "height_mm")
    def _check_waste_dimensions(self):
        for record in self:
            if record.width_mm <= 0 or record.height_mm <= 0:
                raise ValidationError("Waste dimensions must be greater than 0 mm.")
            if record.width_mm >= 200 and record.height_mm >= 200:
                raise ValidationError("Waste must be smaller than 200x200 on at least one side.")

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for record in records:
            record.account_move_id = record._post_waste_accounting_entry()
        return records

    def _post_waste_accounting_entry(self):
        self.ensure_one()
        company = self.env.company
        waste_account = company.tp_waste_account_id
        if not waste_account:
            raise ValidationError("Configure 'Offcuts Waste Account' before creating waste.")
        journal = company.tp_waste_journal_id or self.env["account.journal"].search(
            [("company_id", "=", company.id), ("type", "=", "general")],
            limit=1,
        )
        if not journal:
            raise ValidationError("Configure 'Offcuts Waste Journal' or create a general journal.")

        valuation_account = (
            self.product_id.categ_id.property_stock_valuation_account_id
            or self.product_id.categ_id.property_stock_account_production_cost_id
            or waste_account
        )
        amount = float(self.waste_value)
        if amount <= 0:
            return False

        move = self.env["account.move"].create(
            {
                "move_type": "entry",
                "journal_id": journal.id,
                "date": fields.Date.context_today(self),
                "line_ids": [
                    (
                        0,
                        0,
                        {
                            "name": f"Waste {self.name}",
                            "account_id": waste_account.id,
                            "debit": amount,
                            "credit": 0.0,
                        },
                    ),
                    (
                        0,
                        0,
                        {
                            "name": f"Waste {self.name}",
                            "account_id": valuation_account.id,
                            "debit": 0.0,
                            "credit": amount,
                        },
                    ),
                ],
            }
        )
        move.action_post()
        return move
