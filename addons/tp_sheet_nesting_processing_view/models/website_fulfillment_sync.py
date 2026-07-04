from odoo import _, api, models


class WebsiteFulfillmentSync(models.AbstractModel):
    _inherit = "website.fulfillment.sync"

    @api.model
    def action_sync_now(self):
        stats = self._sync_fulfillment_statuses()
        message = _(
            "Fetched %(fetched)s orders. Updated %(updated)s, unchanged %(unchanged)s, "
            "skipped %(skipped)s, errors %(errors)s. "
            "Reconciled %(reconciled)s, vanished %(vanished)s. "
            "Nesting jobs: created %(nesting_jobs_created)s, existing %(nesting_jobs_existing)s, "
            "panel rows %(nesting_panel_rows)s, missing manifests %(nesting_orders_missing_panels)s."
        ) % stats
        warn = bool(
            stats["errors"]
            or stats["skipped"]
            or stats["vanished"]
            or stats["nesting_orders_missing_panels"]
        )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Website fulfillment sync complete"),
                "message": message,
                "type": "warning" if warn else "success",
                "sticky": warn,
            },
        }

    @api.model
    def _sync_fulfillment_statuses(self, statuses=None, limit=None):
        stats = super()._sync_fulfillment_statuses(statuses=statuses, limit=limit)
        nesting_stats = self.env["tp.nesting.job"].sudo()._tp_create_from_website_fulfillment(
            statuses=statuses or self._get_default_statuses()
        )
        stats.update(
            {
                "nesting_jobs_created": nesting_stats["jobs_created"],
                "nesting_jobs_existing": nesting_stats["jobs_existing"],
                "nesting_panel_rows": nesting_stats["panel_rows"],
                "nesting_panel_quantity": nesting_stats["panel_quantity"],
                "nesting_orders_missing_panels": nesting_stats["orders_missing_panels"],
            }
        )
        return stats
