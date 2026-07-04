import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


DEFAULT_WEBSITE_NESTING_STATUSES = ("processing",)
WEBSITE_STATUS_PARAM = "website_fulfillment_sync.statuses"


class TpNestingJob(models.Model):
    _inherit = "tp.nesting.job"

    website_fulfillment_status = fields.Selection(
        related="sale_order_id.website_fulfillment_status",
        store=True,
        index=True,
        readonly=True,
        string="Website Fulfillment Status",
    )
    website_fulfillment_status_raw = fields.Char(
        related="sale_order_id.website_fulfillment_status_raw",
        store=True,
        readonly=True,
        string="Website Fulfillment Status (raw)",
    )
    website_panel_ids = fields.Many2many(
        "tp.web.cut.part",
        compute="_compute_website_panel_ids",
        string="Website Panels",
    )
    website_panel_count = fields.Integer(
        compute="_compute_website_panel_summary",
        store=True,
        string="Panel Rows",
    )
    website_panel_quantity = fields.Integer(
        compute="_compute_website_panel_summary",
        store=True,
        string="Panel Qty",
    )
    website_cnc_panel_count = fields.Integer(
        compute="_compute_website_panel_summary",
        store=True,
        string="CNC Panels",
    )
    website_missing_panel_manifest = fields.Boolean(
        compute="_compute_website_panel_summary",
        store=True,
        string="Missing Website Panel Manifest",
    )
    lifecycle_state = fields.Selection(
        [
            ("active", "Active"),
            ("closed", "Closed"),
            ("cancelled", "Cancelled"),
        ],
        compute="_compute_lifecycle_state",
        store=True,
        index=True,
        string="Job State",
    )

    @api.depends(
        "sale_order_id.website_fulfillment_status",
    )
    def _compute_lifecycle_state(self):
        watched = set(self._tp_website_nesting_statuses())
        for job in self:
            status = job.sale_order_id.website_fulfillment_status or False
            if status == "cancelled":
                job.lifecycle_state = "cancelled"
            elif status in watched:
                job.lifecycle_state = "active"
            else:
                job.lifecycle_state = "closed"

    def _tp_website_panel_parts(self):
        self.ensure_one()
        Part = self.env["tp.web.cut.part"]
        if not self.sale_order_id or not self.demand_product_id:
            return Part.browse()
        return Part.search(
            [
                ("active", "=", True),
                ("sale_order_id", "=", self.sale_order_id.id),
                ("product_id", "=", self.demand_product_id.id),
            ],
            order="line_index, panel_index, instance_index, id",
        )

    @api.depends("sale_order_id", "demand_product_id", "sale_order_id.tp_web_cut_part_ids")
    def _compute_website_panel_ids(self):
        for job in self:
            job.website_panel_ids = job._tp_website_panel_parts()

    @api.depends(
        "sale_order_id",
        "sale_order_id.website_fulfillment_status",
        "demand_product_id",
        "sale_order_id.tp_web_cut_part_ids",
        "sale_order_id.tp_web_cut_part_ids.active",
        "sale_order_id.tp_web_cut_part_ids.product_id",
        "sale_order_id.tp_web_cut_part_ids.quantity",
        "sale_order_id.tp_web_cut_part_ids.cnc_required",
    )
    def _compute_website_panel_summary(self):
        statuses = set(self._tp_website_nesting_statuses())
        for job in self:
            parts = job._tp_website_panel_parts()
            job.website_panel_count = len(parts)
            job.website_panel_quantity = sum(parts.mapped("quantity"))
            job.website_cnc_panel_count = len(parts.filtered("cnc_required"))
            job.website_missing_panel_manifest = (
                bool(job.sale_order_id)
                and job.website_fulfillment_status in statuses
                and not parts
            )

    @api.model
    def _tp_website_nesting_statuses(self):
        raw = self.env["ir.config_parameter"].sudo().get_param(WEBSITE_STATUS_PARAM, "") or ""
        statuses = [status.strip().lower() for status in raw.split(",") if status.strip()]
        return statuses or list(DEFAULT_WEBSITE_NESTING_STATUSES)

    @api.model
    def _tp_create_from_website_fulfillment(self, statuses=None):
        if isinstance(statuses, str):
            statuses = statuses.split(",")
        statuses = [status.strip().lower() for status in (statuses or self._tp_website_nesting_statuses()) if status]
        stats = {
            "orders": 0,
            "orders_with_panels": 0,
            "panel_rows": 0,
            "panel_quantity": 0,
            "jobs_created": 0,
            "jobs_existing": 0,
            "orders_missing_panels": 0,
        }
        if not statuses:
            return stats

        SaleOrder = self.env["sale.order"].sudo()
        Part = self.env["tp.web.cut.part"].sudo()
        Job = self.sudo()

        orders = SaleOrder.search([("website_fulfillment_status", "in", statuses)])
        stats["orders"] = len(orders)
        if not orders:
            return stats

        parts = Part.search(
            [
                ("active", "=", True),
                ("sale_order_id", "in", orders.ids),
                ("product_id", "!=", False),
            ],
            order="sale_order_id, product_id, line_index, panel_index, instance_index, id",
        )
        stats["panel_rows"] = len(parts)
        stats["panel_quantity"] = sum(parts.mapped("quantity"))
        orders_with_panels = parts.mapped("sale_order_id")
        stats["orders_with_panels"] = len(orders_with_panels)
        stats["orders_missing_panels"] = len(orders - orders_with_panels)

        grouped = {}
        for part in parts:
            grouped.setdefault((part.sale_order_id.id, part.product_id.id), []).append(part)

        for (sale_order_id, product_id), group_parts in grouped.items():
            job = Job.search(
                [
                    ("sale_order_id", "=", sale_order_id),
                    ("demand_product_id", "=", product_id),
                ],
                limit=1,
                order="id desc",
            )
            if job:
                stats["jobs_existing"] += 1
                continue

            order = group_parts[0].sale_order_id
            product = group_parts[0].product_id
            seq = self.env["ir.sequence"].sudo().next_by_code("tp.nesting.job") or "JOB"
            Job.create(
                {
                    "name": seq,
                    "sale_order_id": order.id,
                    "demand_product_id": product.id,
                    "note": (
                        "Website fulfillment job for SO %s / %s "
                        "(%s panel rows)"
                    )
                    % (order.name, product.display_name, len(group_parts)),
                }
            )
            stats["jobs_created"] += 1

        return stats

    def action_pull_website_fulfillment_jobs(self):
        # Called from a list-view header button — Odoo passes the selected
        # ids as `self`. The method is purpose a global pull (doesn't depend
        # on which rows are selected) so we just discard `self`.

        # Refresh fulfillment statuses from the website first, otherwise we'd
        # create/keep jobs off Odoo's stale stored status — e.g. orders the
        # website has since marked "completed" would still read "processing"
        # and get pulled in. Best-effort: a sync failure must not block the
        # pull (we fall back to stored data), but we surface it in the toast.
        sync_failed = False
        try:
            self.env["website.fulfillment.sync"].sudo()._sync_fulfillment_statuses()
        except Exception:
            sync_failed = True
            _logger.exception("Website status refresh failed during pull-jobs; using stored statuses")

        stats = self.env["tp.nesting.job"].sudo()._tp_create_from_website_fulfillment()
        message = (
            "Created %(jobs_created)s jobs, found %(jobs_existing)s existing jobs, "
            "linked %(panel_rows)s panel rows (%(panel_quantity)s panels) from %(orders_with_panels)s orders. "
            "%(orders_missing_panels)s eligible orders have no website panel manifest yet."
        ) % stats
        if sync_failed:
            message = (
                "Could not refresh statuses from the website — results may be based on "
                "stale data.\n" + message
            )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Website nesting jobs refreshed",
                "message": message,
                "type": "warning" if (stats["orders_missing_panels"] or sync_failed) else "success",
                "sticky": bool(stats["orders_missing_panels"] or sync_failed),
            },
        }

    def action_view_website_panels(self):
        self.ensure_one()
        action = self.env.ref("tp_sheet_nesting.action_tp_web_cut_part").read()[0]
        action.update(
            {
                "name": "Website Panels",
                "domain": [
                    ("active", "=", True),
                    ("sale_order_id", "=", self.sale_order_id.id),
                    ("product_id", "=", self.demand_product_id.id),
                ],
                "context": {
                    "search_default_group_order": 1,
                },
            }
        )
        return action
