from odoo import fields, models


class SaleOrder(models.Model):
    _inherit = "sale.order"

    website_storefront_order_id = fields.Integer(
        string="Storefront Order ID",
        copy=False,
        index=True,
        help="Identifier of the order on the website storefront database.",
    )
    website_public_token = fields.Char(
        string="Website Public Token",
        copy=False,
        help="Public token used by the storefront to reference this order.",
    )
    website_fulfillment_status = fields.Selection(
        selection=[
            ("pending", "Pending"),
            ("payment_pending", "Payment Pending"),
            ("processing", "Processing"),
            ("shipping_quote_created", "Shipping Quote Created"),
            ("cutting", "Cutting"),
            ("ready", "Ready"),
            ("shipped", "Shipped"),
            ("shipped_manual", "Shipped (Manual)"),
            ("delivered", "Delivered"),
            ("completed", "Completed"),
            ("cancelled", "Cancelled"),
            ("on_hold", "On Hold"),
            ("other", "Other"),
        ],
        string="Website Fulfillment Status",
        copy=False,
        tracking=True,
        help="Snapshot of the fulfillment status reported by the website.",
    )
    website_fulfillment_status_raw = fields.Char(
        string="Website Fulfillment Status (raw)",
        copy=False,
        help="Raw status string as received from the website, including values not in the predefined selection.",
    )
    website_payment_status = fields.Char(
        string="Website Payment Status",
        copy=False,
    )
    website_sync_status = fields.Char(
        string="Website Sync Status",
        copy=False,
    )
    website_fulfillment_synced_at = fields.Datetime(
        string="Website Fulfillment Synced At",
        copy=False,
        readonly=True,
    )
    website_storefront_created_at = fields.Datetime(
        string="Storefront Created At",
        copy=False,
        readonly=True,
    )
    website_storefront_updated_at = fields.Datetime(
        string="Storefront Updated At",
        copy=False,
        readonly=True,
    )
