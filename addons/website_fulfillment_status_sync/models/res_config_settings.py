from odoo import api, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    website_fulfillment_base_url = fields.Char(
        string="Website Base URL",
        config_parameter="website_fulfillment_sync.base_url",
        default="https://cutmyplastic.com.au",
        help="Base URL of the storefront, e.g. https://cutmyplastic.com.au.",
    )
    website_fulfillment_token = fields.Char(
        string="Website Status API Token",
        config_parameter="website_fulfillment_sync.token",
        help="Bearer token (ODOO_STATUS_API_TOKEN) used to authenticate against the website fulfillment endpoint.",
    )
    website_fulfillment_statuses = fields.Char(
        string="Fulfillment Statuses",
        config_parameter="website_fulfillment_sync.statuses",
        default="processing",
        help="Comma-separated list of fulfillment statuses to pull from the website.",
    )
    website_fulfillment_limit = fields.Integer(
        string="Per-run Limit",
        config_parameter="website_fulfillment_sync.limit",
        default=1000,
    )
    website_fulfillment_reconcile_statuses = fields.Char(
        string="Reconcile Statuses",
        config_parameter="website_fulfillment_sync.reconcile_statuses",
        default="completed,shipped,on_hold,payment_pending",
        help=(
            "Comma-separated statuses to refetch on every sync so Odoo can detect orders "
            "that have left the watched set (e.g. moved from 'processing' to 'completed' "
            "on the website). Should cover every storefront status NOT in 'Fulfillment Statuses'."
        ),
    )

    def action_website_fulfillment_test_connection(self):
        self.ensure_one()
        return self.env["website.fulfillment.sync"].action_test_connection()

    def action_website_fulfillment_sync_now(self):
        self.ensure_one()
        return self.env["website.fulfillment.sync"].action_sync_now()
