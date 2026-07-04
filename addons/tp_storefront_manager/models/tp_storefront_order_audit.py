"""Storefront Order Cross-Reference Audit.

Read-only diff between Odoo sale.order records and the storefront's
`storefront_orders` table, fetched via the website's protected API.

This module NEVER writes to sale.order or to the website. It only loads
read-only transient rows so the operator can spot mismatches by eye.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from urllib import error as url_error
from urllib import parse as url_parse
from urllib import request as url_request

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# System parameter keys reused from the existing website_fulfillment_status_sync
# module so we share the bearer token + base URL with no extra config.
PARAM_BASE_URL = "website_fulfillment_sync.base_url"
PARAM_TOKEN = "website_fulfillment_sync.token"

DEFAULT_BASE_URL = "https://cutmyplastic.com.au"
DEFAULT_LOOKBACK_DAYS = 90

# Severity thresholds — anything with money or existence drift is MAJOR.
# Status/email drift is MINOR. No diffs at all = NONE.
SEVERITY_NONE = "none"
SEVERITY_MINOR = "minor"
SEVERITY_MAJOR = "major"

# Tolerate sub-cent rounding when comparing monetary fields.
MONEY_TOLERANCE = 0.005


class TpStorefrontOrderAudit(models.TransientModel):
    _name = "tp.storefront.order.audit"
    _description = "Storefront Order Cross-Reference Audit Row"
    _order = "severity desc, odoo_so_name desc, storefront_id desc"
    _rec_name = "display_name"

    display_name = fields.Char(compute="_compute_display_name", store=False)

    # --- Odoo side -----------------------------------------------------
    odoo_so_id = fields.Many2one("sale.order", string="Odoo SO", readonly=True)
    odoo_so_name = fields.Char(string="Odoo SO #", readonly=True)
    odoo_partner_email = fields.Char(readonly=True)
    odoo_total = fields.Float(string="Odoo Total", readonly=True, digits=(16, 2))
    odoo_state = fields.Char(readonly=True)
    odoo_fulfillment_status = fields.Char(string="Odoo Fulfillment", readonly=True)
    odoo_date_order = fields.Datetime(string="Odoo Order Date", readonly=True)

    # --- Website side --------------------------------------------------
    storefront_id = fields.Integer(string="Storefront ID", readonly=True)
    storefront_public_token = fields.Char(readonly=True)
    storefront_email = fields.Char(readonly=True)
    storefront_total = fields.Float(string="Website Total", readonly=True, digits=(16, 2))
    storefront_payment_status = fields.Char(readonly=True)
    storefront_fulfillment_status = fields.Char(string="Website Fulfillment", readonly=True)
    storefront_sync_status = fields.Char(string="Website Sync Status", readonly=True)
    storefront_created_at = fields.Datetime(readonly=True)

    # --- Diff outputs --------------------------------------------------
    presence = fields.Selection(
        [
            ("both", "On both"),
            ("odoo_only", "Only in Odoo"),
            ("website_only", "Only on Website"),
        ],
        readonly=True,
    )
    mismatch_flags = fields.Char(
        string="Mismatch Flags",
        readonly=True,
        help="Comma-separated list of differences, e.g. total_diff, email_diff, status_diff.",
    )
    mismatch_summary = fields.Text(
        string="Mismatch Summary",
        readonly=True,
        help="Human-readable explanation of the differences, line by line.",
    )
    severity = fields.Selection(
        [
            (SEVERITY_NONE, "OK"),
            (SEVERITY_MINOR, "Minor"),
            (SEVERITY_MAJOR, "Major"),
        ],
        readonly=True,
        index=True,
    )
    total_diff = fields.Float(
        string="Total Δ (Web − Odoo)",
        readonly=True,
        digits=(16, 2),
    )

    # ------------------------------------------------------------------
    # Action entry point
    # ------------------------------------------------------------------
    @api.model
    def action_refresh_audit(self):
        """Read-only audit: fetch from website, join with sale.order, return
        a window action listing every cross-reference row. NEVER writes to
        sale.order or to the website."""
        # 1. Clear previous transient rows for this user.
        self.search([]).unlink()

        # 2. Fetch from the website API.
        try:
            payload = self._tp_fetch_website_orders()
        except UserError:
            raise
        except Exception as exc:
            _logger.exception("Storefront cross-reference fetch failed")
            raise UserError(
                "Could not fetch storefront orders: %s\n\n"
                "Tip: this endpoint must be deployed on the website. "
                "See the Codex prompt in the module README." % exc
            )

        website_orders = payload.get("orders") or []
        _logger.info(
            "tp.storefront.order.audit: fetched %d orders from website",
            len(website_orders),
        )

        # 3. Build website-side index by Odoo order id (preferred) and name.
        web_by_odoo_id = {}
        web_by_odoo_name = {}
        for w in website_orders:
            ooid = w.get("odooOrderId")
            if ooid:
                try:
                    web_by_odoo_id[int(ooid)] = w
                except (TypeError, ValueError):
                    pass
            oname = (w.get("odooOrderName") or w.get("salesOrderName") or "").strip()
            if oname:
                web_by_odoo_name[oname] = w

        # 4. Fetch Odoo SOs in the lookback window.
        SaleOrder = self.env["sale.order"].sudo()
        since = fields.Datetime.now() - timedelta(days=DEFAULT_LOOKBACK_DAYS)
        odoo_sos = SaleOrder.search([("create_date", ">=", since)])

        seen_web_keys = set()
        audit_rows = []

        # 5. Walk Odoo SOs — for each, find its website counterpart (or note it's missing).
        for so in odoo_sos:
            web = web_by_odoo_id.get(so.id) or web_by_odoo_name.get(so.name)
            if web is not None:
                seen_web_keys.add(("id", web.get("odooOrderId")))
                seen_web_keys.add(("name", (web.get("odooOrderName") or "").strip()))
            audit_rows.append(self._tp_build_audit_vals(so=so, web=web))

        # 6. Now the other direction — website orders Odoo doesn't have.
        for w in website_orders:
            ooid = w.get("odooOrderId")
            oname = (w.get("odooOrderName") or w.get("salesOrderName") or "").strip()
            if ("id", ooid) in seen_web_keys or ("name", oname) in seen_web_keys:
                continue
            # We matched neither by id nor by name. Try a one-off Odoo lookup
            # in case the order is outside our lookback window.
            so = False
            if ooid:
                so = SaleOrder.browse(int(ooid))
                so = so if so.exists() else False
            if not so and oname:
                so = SaleOrder.search([("name", "=", oname)], limit=1)
            if so and so.create_date < since:
                # Found, but out of window — include it anyway so it doesn't
                # look like Odoo lost the order.
                audit_rows.append(self._tp_build_audit_vals(so=so, web=w))
            else:
                # Truly missing on Odoo side.
                audit_rows.append(self._tp_build_audit_vals(so=False, web=w))

        if audit_rows:
            self.create(audit_rows)

        return {
            "type": "ir.actions.act_window",
            "name": "Storefront Cross-Reference Audit",
            "res_model": self._name,
            "view_mode": "list,form",
            "target": "current",
            "context": {"create": False, "delete": False, "edit": False},
        }

    # ------------------------------------------------------------------
    # Per-row diff
    # ------------------------------------------------------------------
    @api.model
    def _tp_build_audit_vals(self, *, so, web):
        """Compute a single audit row's values. Doesn't write anything."""
        flags = []
        summary = []
        severity = SEVERITY_NONE

        if so and web:
            presence = "both"
            odoo_total = float(so.amount_total or 0.0)
            web_total = float(web.get("totalAmount") or 0.0)
            total_diff = web_total - odoo_total

            if abs(total_diff) > MONEY_TOLERANCE:
                flags.append("total_diff")
                summary.append(
                    "Total mismatch: Odoo $%.2f vs Website $%.2f (Δ $%+0.2f)"
                    % (odoo_total, web_total, total_diff)
                )
                severity = SEVERITY_MAJOR

            odoo_email = (so.partner_id.email or "").strip().lower()
            web_email = (web.get("customerEmail") or "").strip().lower()
            if odoo_email and web_email and odoo_email != web_email:
                flags.append("email_diff")
                summary.append(
                    "Email mismatch: Odoo %r vs Website %r" % (odoo_email, web_email)
                )
                if severity == SEVERITY_NONE:
                    severity = SEVERITY_MINOR

            odoo_ff = (so.website_fulfillment_status or "").strip().lower()
            web_ff = (web.get("fulfillmentStatus") or "").strip().lower()
            if odoo_ff and web_ff and odoo_ff != web_ff:
                flags.append("fulfillment_status_diff")
                summary.append(
                    "Fulfillment status: Odoo %r vs Website %r" % (odoo_ff, web_ff)
                )
                if severity == SEVERITY_NONE:
                    severity = SEVERITY_MINOR

            if not severity:
                severity = SEVERITY_NONE

        elif so and not web:
            presence = "odoo_only"
            flags.append("missing_on_website")
            summary.append(
                "Odoo has %s but the website returned no matching order. "
                "Either the website deleted/lost it, or the audit window "
                "didn't cover it." % so.name
            )
            severity = SEVERITY_MAJOR
            odoo_total = float(so.amount_total or 0.0)
            web_total = 0.0
            total_diff = 0.0

        else:  # web and not so
            presence = "website_only"
            flags.append("missing_in_odoo")
            wname = (web.get("odooOrderName") or web.get("salesOrderName") or "?")
            summary.append(
                "Website has an order referencing Odoo as %s but Odoo "
                "has no such order." % wname
            )
            severity = SEVERITY_MAJOR
            odoo_total = 0.0
            web_total = float(web.get("totalAmount") or 0.0)
            total_diff = web_total

        vals = {
            "presence": presence,
            "mismatch_flags": ",".join(flags),
            "mismatch_summary": "\n".join(summary) or "No mismatch detected.",
            "severity": severity,
            "total_diff": total_diff if presence == "both" else 0.0,
        }

        if so:
            vals.update({
                "odoo_so_id": so.id,
                "odoo_so_name": so.name,
                "odoo_partner_email": so.partner_id.email,
                "odoo_total": float(so.amount_total or 0.0),
                "odoo_state": so.state,
                "odoo_fulfillment_status": so.website_fulfillment_status or "",
                "odoo_date_order": so.date_order,
            })

        if web:
            vals.update({
                "storefront_id": web.get("storefrontOrderId") or 0,
                "storefront_public_token": web.get("publicToken") or "",
                "storefront_email": web.get("customerEmail") or "",
                "storefront_total": float(web.get("totalAmount") or 0.0),
                "storefront_payment_status": web.get("paymentStatus") or "",
                "storefront_fulfillment_status": web.get("fulfillmentStatus") or "",
                "storefront_sync_status": web.get("syncStatus") or "",
                "storefront_created_at": self._tp_parse_dt(web.get("createdAt")),
            })

        return vals

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------
    @api.depends("odoo_so_name", "storefront_id", "presence")
    def _compute_display_name(self):
        for rec in self:
            if rec.presence == "both":
                rec.display_name = "%s ↔ storefront:%s" % (
                    rec.odoo_so_name or "?", rec.storefront_id or "?",
                )
            elif rec.presence == "odoo_only":
                rec.display_name = "%s (no website match)" % (rec.odoo_so_name or "?")
            elif rec.presence == "website_only":
                rec.display_name = "storefront:%s (no Odoo match)" % (rec.storefront_id or "?")
            else:
                rec.display_name = "?"

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------
    @api.model
    def _tp_fetch_website_orders(self):
        ICP = self.env["ir.config_parameter"].sudo()
        base_url = (ICP.get_param(PARAM_BASE_URL, DEFAULT_BASE_URL) or DEFAULT_BASE_URL).rstrip("/")
        token = (ICP.get_param(PARAM_TOKEN, "") or "").strip()
        if not token:
            raise UserError(
                "No website API token set. Configure it under "
                "Settings → Sales → Website Fulfillment Sync first."
            )

        since = fields.Datetime.now() - timedelta(days=DEFAULT_LOOKBACK_DAYS)
        since_str = since.date().isoformat()
        url = "%s/api/odoo/orders/cross-reference?%s" % (
            base_url,
            url_parse.urlencode({"since": since_str}),
        )
        req = url_request.Request(url, method="GET")
        req.add_header("Authorization", "Bearer %s" % token)
        req.add_header("Accept", "application/json")

        try:
            with url_request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
        except url_error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            raise UserError(
                "Website returned HTTP %s for %s\n\n%s" % (exc.code, url, body)
            )
        except url_error.URLError as exc:
            raise UserError("Could not reach %s: %s" % (url, exc.reason))

        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise UserError("Website returned non-JSON: %s" % exc)

        if not payload.get("ok", True):
            raise UserError(
                "Website API error: %s" % (payload.get("error") or "unknown")
            )
        return payload

    @api.model
    def _tp_parse_dt(self, value):
        if not value:
            return False
        try:
            cleaned = value.replace("Z", "+00:00") if isinstance(value, str) else value
            dt = datetime.fromisoformat(cleaned) if isinstance(cleaned, str) else cleaned
        except ValueError:
            return False
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return fields.Datetime.to_string(dt)
