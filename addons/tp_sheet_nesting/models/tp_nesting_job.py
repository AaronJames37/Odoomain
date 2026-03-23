from odoo import api, fields, models


class TpNestingJob(models.Model):
    _name = "tp.nesting.job"
    _description = "TP Nesting Job"
    _order = "id desc"

    name = fields.Char(required=True, copy=False, default="New", readonly=True)
    sale_order_id = fields.Many2one("sale.order", required=True, ondelete="restrict", readonly=True)
    demand_product_id = fields.Many2one("product.product", required=True, ondelete="restrict", readonly=True)
    run_ids = fields.One2many("tp.nesting.run", "job_id", readonly=True)
    last_run_id = fields.Many2one("tp.nesting.run", readonly=True)
    mo_ids = fields.Many2many(
        "mrp.production",
        compute="_compute_mo_links",
        string="Manufacturing Orders",
        readonly=True,
    )
    mo_count = fields.Integer(
        string="MO Count",
        compute="_compute_mo_links",
        readonly=True,
    )
    allocation_ids = fields.One2many("tp.nesting.allocation", "job_id", readonly=True)
    note = fields.Char()

    @api.depends("run_ids.mo_id")
    def _compute_mo_links(self):
        for record in self:
            mos = record.run_ids.mapped("mo_id")
            record.mo_ids = mos
            record.mo_count = len(mos)

    def action_view_sale_order(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "res_model": "sale.order",
            "view_mode": "form",
            "res_id": self.sale_order_id.id,
            "target": "current",
        }

    def action_view_mos(self):
        self.ensure_one()
        mos = self.mo_ids
        if len(mos) == 1:
            return {
                "type": "ir.actions.act_window",
                "name": "Manufacturing Order",
                "res_model": "mrp.production",
                "view_mode": "form",
                "res_id": mos.id,
                "target": "current",
            }
        return {
            "type": "ir.actions.act_window",
            "name": "Manufacturing Orders",
            "res_model": "mrp.production",
            "view_mode": "list,form",
            "domain": [("id", "in", mos.ids)],
            "target": "current",
        }
