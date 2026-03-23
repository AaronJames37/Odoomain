import json
from datetime import datetime, timezone
from urllib import error as url_error
from urllib import request as url_request

from odoo import api, fields, models
from odoo.exceptions import ValidationError


class TpStorefrontSettings(models.Model):
    _name = "tp.storefront.settings"
    _description = "Storefront Settings"
    _rec_name = "site_name"

    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
        index=True,
        ondelete="cascade",
    )

    site_name = fields.Char(required=True, default="Cut My Plastic")
    site_tagline = fields.Char(default="Cut-to-size plastics")
    support_email = fields.Char()
    support_phone = fields.Char()

    logo_url = fields.Char(help="Public logo URL used by the storefront")
    favicon_url = fields.Char(help="Public favicon URL used by the storefront")

    primary_color = fields.Char(default="#FF6E00")
    secondary_color = fields.Char(default="#1F2937")
    accent_color = fields.Char(default="#0EA5E9")
    background_color = fields.Char(default="#F8FAFC")
    text_color = fields.Char(default="#111827")

    content_max_width_px = fields.Integer(default=1260)
    body_font_family = fields.Char(default="Montserrat")
    sync_endpoint_url = fields.Char(default="http://host.docker.internal:3000/api/catalog/sync")
    sync_token = fields.Char(help="Optional token sent as x-catalog-sync-token header.")
    last_catalog_sync_at = fields.Datetime(readonly=True)
    last_catalog_sync_status = fields.Char(readonly=True)

    promo_bar_enabled = fields.Boolean(default=True)
    promo_bar_text = fields.Char(default="FREE Metro Delivery over $250!")

    usp_bar_enabled = fields.Boolean(default=True)
    usp_1_heading = fields.Char(default="No minimum order!")
    usp_1_subheading = fields.Char(default="Order exactly what you need")
    usp_2_heading = fields.Char(default="Full sheet delivery Australia wide")
    usp_2_subheading = fields.Char(default="2440x1220 sheets at no extra cost!")
    usp_3_heading = fields.Char(default="Australian Owned & Operated")
    usp_3_subheading = fields.Char(default="Based in Sydney")
    usp_4_heading = fields.Char(default="Cut to size available")
    usp_4_subheading = fields.Char(default="Made to your measurements")

    panel_qty_discount_enabled = fields.Boolean(
        string="Enable Panel Quantity Discounts", default=False
    )
    panel_qty_discount_1_min_qty = fields.Integer(string="Tier 1 Min Panels", default=2)
    panel_qty_discount_1_percent = fields.Float(
        string="Tier 1 Discount %", default=3.0, digits=(16, 2)
    )
    panel_qty_discount_2_min_qty = fields.Integer(string="Tier 2 Min Panels", default=5)
    panel_qty_discount_2_percent = fields.Float(
        string="Tier 2 Discount %", default=5.0, digits=(16, 2)
    )
    panel_qty_discount_3_min_qty = fields.Integer(string="Tier 3 Min Panels", default=10)
    panel_qty_discount_3_percent = fields.Float(
        string="Tier 3 Discount %", default=8.0, digits=(16, 2)
    )
    panel_qty_discount_4_min_qty = fields.Integer(string="Tier 4 Min Panels", default=20)
    panel_qty_discount_4_percent = fields.Float(
        string="Tier 4 Discount %", default=12.0, digits=(16, 2)
    )

    _sql_constraints = [
        (
            "tp_storefront_settings_company_unique",
            "unique(company_id)",
            "Only one storefront settings record is allowed per company.",
        ),
    ]

    @api.constrains("content_max_width_px")
    def _check_content_max_width_px(self):
        for record in self:
            if record.content_max_width_px < 320:
                raise ValidationError("Content max width must be at least 320px.")

    @api.constrains(
        "panel_qty_discount_1_min_qty",
        "panel_qty_discount_2_min_qty",
        "panel_qty_discount_3_min_qty",
        "panel_qty_discount_4_min_qty",
        "panel_qty_discount_1_percent",
        "panel_qty_discount_2_percent",
        "panel_qty_discount_3_percent",
        "panel_qty_discount_4_percent",
    )
    def _check_panel_quantity_discounts(self):
        for record in self:
            min_fields = [
                record.panel_qty_discount_1_min_qty,
                record.panel_qty_discount_2_min_qty,
                record.panel_qty_discount_3_min_qty,
                record.panel_qty_discount_4_min_qty,
            ]
            percent_fields = [
                record.panel_qty_discount_1_percent,
                record.panel_qty_discount_2_percent,
                record.panel_qty_discount_3_percent,
                record.panel_qty_discount_4_percent,
            ]
            for min_qty in min_fields:
                if min_qty and min_qty < 2:
                    raise ValidationError("Panel discount minimum quantity must be at least 2.")
            for percent in percent_fields:
                if percent < 0 or percent > 100:
                    raise ValidationError("Panel discount percent must be between 0 and 100.")

    def _collect_panel_quantity_discount_tiers(self):
        self.ensure_one()
        tiers = [
            {
                "minPanels": int(self.panel_qty_discount_1_min_qty or 0),
                "discountPercent": float(self.panel_qty_discount_1_percent or 0.0),
            },
            {
                "minPanels": int(self.panel_qty_discount_2_min_qty or 0),
                "discountPercent": float(self.panel_qty_discount_2_percent or 0.0),
            },
            {
                "minPanels": int(self.panel_qty_discount_3_min_qty or 0),
                "discountPercent": float(self.panel_qty_discount_3_percent or 0.0),
            },
            {
                "minPanels": int(self.panel_qty_discount_4_min_qty or 0),
                "discountPercent": float(self.panel_qty_discount_4_percent or 0.0),
            },
        ]
        sanitized = [
            tier
            for tier in tiers
            if tier["minPanels"] >= 2 and tier["discountPercent"] > 0
        ]
        sanitized.sort(key=lambda tier: tier["minPanels"])
        deduped = {}
        for tier in sanitized:
            deduped[tier["minPanels"]] = tier
        return list(deduped.values())

    @api.model
    def action_open_current_company_settings(self):
        record = self.search([("company_id", "=", self.env.company.id)], limit=1)
        if not record:
            record = self.create({"company_id": self.env.company.id})

        return {
            "type": "ir.actions.act_window",
            "name": "Storefront Settings",
            "res_model": "tp.storefront.settings",
            "res_id": record.id,
            "view_mode": "form",
            "target": "current",
        }

    def action_sync_storefront_catalog(self):
        self.ensure_one()
        if not self.sync_endpoint_url:
            raise ValidationError("Set a Sync Endpoint URL before running catalog sync.")

        headers = {"Content-Type": "application/json"}
        if self.sync_token:
            headers["x-catalog-sync-token"] = self.sync_token

        req = url_request.Request(
            self.sync_endpoint_url,
            data=b"{}",
            headers=headers,
            method="POST",
        )

        try:
            with url_request.urlopen(req, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8") or "{}")
        except url_error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            self.last_catalog_sync_status = f"HTTP {exc.code}: {body[:200] or exc.reason}"
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Storefront Sync Failed",
                    "message": self.last_catalog_sync_status,
                    "type": "danger",
                    "sticky": False,
                },
            }
        except Exception as exc:
            self.last_catalog_sync_status = str(exc)
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": "Storefront Sync Failed",
                    "message": self.last_catalog_sync_status,
                    "type": "danger",
                    "sticky": False,
                },
            }

        sync_iso = payload.get("lastSync")
        sync_dt = fields.Datetime.now()
        if sync_iso:
            try:
                parsed = datetime.fromisoformat(sync_iso.replace("Z", "+00:00"))
                if parsed.tzinfo:
                    sync_dt = parsed.astimezone(timezone.utc).replace(tzinfo=None)
                else:
                    sync_dt = parsed
            except Exception:
                sync_dt = fields.Datetime.now()

        if payload.get("synced") is True:
            self.last_catalog_sync_at = sync_dt
            self.last_catalog_sync_status = "Success"
            message = f"Catalog synced at {sync_iso or sync_dt}."
            notification_type = "success"
        else:
            self.last_catalog_sync_status = payload.get("error") or "Unknown sync response."
            message = self.last_catalog_sync_status
            notification_type = "warning"

        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Storefront Sync",
                "message": message,
                "type": notification_type,
                "sticky": False,
            },
        }

    @api.model
    def get_storefront_public_settings(self):
        record = self.search([("company_id", "=", self.env.company.id)], limit=1)
        if not record:
            record = self.create({"company_id": self.env.company.id})

        return {
            "siteName": record.site_name,
            "siteTagline": record.site_tagline,
            "supportEmail": record.support_email,
            "supportPhone": record.support_phone,
            "logoUrl": record.logo_url,
            "faviconUrl": record.favicon_url,
            "theme": {
                "primary": record.primary_color,
                "secondary": record.secondary_color,
                "accent": record.accent_color,
                "background": record.background_color,
                "text": record.text_color,
                "contentMaxWidthPx": record.content_max_width_px,
                "bodyFontFamily": record.body_font_family,
            },
            "promo": {
                "enabled": record.promo_bar_enabled,
                "text": record.promo_bar_text,
            },
            "usp": {
                "enabled": record.usp_bar_enabled,
                "items": [
                    {
                        "heading": record.usp_1_heading,
                        "subheading": record.usp_1_subheading,
                    },
                    {
                        "heading": record.usp_2_heading,
                        "subheading": record.usp_2_subheading,
                    },
                    {
                        "heading": record.usp_3_heading,
                        "subheading": record.usp_3_subheading,
                    },
                    {
                        "heading": record.usp_4_heading,
                        "subheading": record.usp_4_subheading,
                    },
                ],
            },
            "panelQuantityDiscounts": {
                "enabled": record.panel_qty_discount_enabled,
                "tiers": record._collect_panel_quantity_discount_tiers(),
            },
        }
