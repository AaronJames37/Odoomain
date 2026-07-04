from odoo import fields, models


class WebsiteFulfillmentSyncLog(models.Model):
    _name = "website.fulfillment.sync.log"
    _description = "Website Fulfillment Sync Log"
    _order = "create_date desc, id desc"
    _rec_name = "odoo_order_name_hint"

    storefront_order_id = fields.Integer(string="Storefront Order ID", index=True)
    odoo_order_id_hint = fields.Integer(string="Odoo Order ID (hint)")
    odoo_order_name_hint = fields.Char(string="Odoo Order Name (hint)")
    fulfillment_status = fields.Char(string="Reported Fulfillment Status")
    state = fields.Selection(
        [
            ("skipped", "Skipped"),
            ("error", "Error"),
            ("vanished", "Vanished"),
        ],
        required=True,
        default="skipped",
    )
    message = fields.Text(string="Message")
    raw_json = fields.Text(string="Raw Payload", readonly=True)
