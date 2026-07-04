from odoo import api, fields, models


def _part_has_cut_outs(part):
    """True iff a tp.web.cut.part record has any non-empty cut_outs payload."""
    raw = part.cut_outs
    if not raw:
        return False
    if isinstance(raw, (list, tuple)):
        return any(isinstance(co, dict) for co in raw)
    # Defensive: stored as JSON may surface as str or dict in odd code paths.
    return bool(raw)


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

    @api.model
    def _tp_partition_cutout_parts(self, parts):
        """Split a parts recordset/iterable into (kept, dropped) based on the
        company's tp_nesting_include_cutout_parts flag.

        Returns (kept, dropped) recordsets if `parts` is a recordset; otherwise
        returns plain lists. When the flag is on, every part is kept.
        """
        Part = self.env["tp.web.cut.part"]
        is_recordset = isinstance(parts, type(Part))
        if self.env.company.tp_nesting_include_cutout_parts:
            empty = Part.browse() if is_recordset else []
            return parts, empty
        kept_ids = []
        dropped_ids = []
        kept_list = []
        dropped_list = []
        for part in parts:
            if _part_has_cut_outs(part):
                if is_recordset:
                    dropped_ids.append(part.id)
                else:
                    dropped_list.append(part)
            else:
                if is_recordset:
                    kept_ids.append(part.id)
                else:
                    kept_list.append(part)
        if is_recordset:
            return Part.browse(kept_ids), Part.browse(dropped_ids)
        return kept_list, dropped_list

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
