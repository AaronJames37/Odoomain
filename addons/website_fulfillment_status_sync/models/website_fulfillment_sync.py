import json
import logging
from datetime import datetime, timezone

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError


_logger = logging.getLogger(__name__)

PARAM_BASE_URL = "website_fulfillment_sync.base_url"
PARAM_TOKEN = "website_fulfillment_sync.token"
PARAM_STATUSES = "website_fulfillment_sync.statuses"
PARAM_LIMIT = "website_fulfillment_sync.limit"
PARAM_RECONCILE_STATUSES = "website_fulfillment_sync.reconcile_statuses"

DEFAULT_BASE_URL = "https://cutmyplastic.com.au"
DEFAULT_STATUSES = "processing"
DEFAULT_RECONCILE_STATUSES = "completed,shipped,on_hold,payment_pending"
DEFAULT_LIMIT = 1000

KNOWN_FULFILLMENT_STATES = {
    "pending",
    "payment_pending",
    "processing",
    "shipping_quote_created",
    "cutting",
    "ready",
    "shipped",
    "shipped_manual",
    "delivered",
    "completed",
    "cancelled",
    "on_hold",
}


class WebsiteFulfillmentSync(models.AbstractModel):
    _name = "website.fulfillment.sync"
    _description = "Website Fulfillment Status Sync"

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------
    @api.model
    def _get_base_url(self):
        param = self.env["ir.config_parameter"].sudo().get_param(PARAM_BASE_URL, DEFAULT_BASE_URL)
        return (param or DEFAULT_BASE_URL).rstrip("/")

    @api.model
    def _get_token(self):
        token = self.env["ir.config_parameter"].sudo().get_param(PARAM_TOKEN, "")
        return (token or "").strip()

    @api.model
    def _get_default_statuses(self):
        raw = self.env["ir.config_parameter"].sudo().get_param(PARAM_STATUSES, DEFAULT_STATUSES) or ""
        statuses = [s.strip().lower() for s in raw.split(",") if s.strip()]
        return statuses or [s.strip() for s in DEFAULT_STATUSES.split(",")]

    @api.model
    def _get_reconcile_statuses(self):
        raw = self.env["ir.config_parameter"].sudo().get_param(
            PARAM_RECONCILE_STATUSES, DEFAULT_RECONCILE_STATUSES,
        ) or ""
        statuses = [s.strip().lower() for s in raw.split(",") if s.strip()]
        return statuses or [s.strip() for s in DEFAULT_RECONCILE_STATUSES.split(",")]

    @api.model
    def _get_limit(self):
        raw = self.env["ir.config_parameter"].sudo().get_param(PARAM_LIMIT, str(DEFAULT_LIMIT))
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return DEFAULT_LIMIT
        return value if value > 0 else DEFAULT_LIMIT

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------
    @api.model
    def action_test_connection(self):
        payload = self._fetch_fulfillment_payload(statuses=self._get_default_statuses(), limit=1)
        count = len(payload.get("orders") or [])
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Website connection OK"),
                "message": _("Website returned %s order sample(s).") % count,
                "type": "success",
                "sticky": False,
            },
        }

    @api.model
    def action_sync_now(self):
        stats = self._sync_fulfillment_statuses()
        message = _(
            "Fetched %(fetched)s orders. Updated %(updated)s, unchanged %(unchanged)s, "
            "skipped %(skipped)s, errors %(errors)s. "
            "Reconciled %(reconciled)s, vanished %(vanished)s."
        ) % stats
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Website fulfillment sync complete"),
                "message": message,
                "type": "warning" if stats["errors"] or stats["skipped"] or stats["vanished"] else "success",
                "sticky": bool(stats["errors"] or stats["skipped"] or stats["vanished"]),
            },
        }

    @api.model
    def _cron_sync_fulfillment_statuses(self):
        try:
            self._sync_fulfillment_statuses()
        except Exception:
            _logger.exception("Website fulfillment status sync failed")

    # ------------------------------------------------------------------
    # Core sync
    # ------------------------------------------------------------------
    @api.model
    def _sync_fulfillment_statuses(self, statuses=None, limit=None):
        statuses = statuses or self._get_default_statuses()
        limit = limit or self._get_limit()

        payload = self._fetch_fulfillment_payload(statuses=statuses, limit=limit)
        orders = payload.get("orders") or []

        stats = {
            "fetched": len(orders),
            "updated": 0,
            "unchanged": 0,
            "skipped": 0,
            "errors": 0,
            "reconciled": 0,
            "vanished": 0,
        }

        Log = self.env["website.fulfillment.sync.log"].sudo()
        synced_at = fields.Datetime.now()

        # ---- pass 1: apply fresh watched-set records --------------------
        still_watched_so_ids = set()
        for record in orders:
            try:
                sale_order = self._find_sale_order(record)
                if not sale_order:
                    stats["skipped"] += 1
                    reason = _("No sale.order matched odoo_order_id=%s name=%s") % (
                        record.get("odooOrderId"),
                        record.get("odooOrderName") or record.get("salesOrderName"),
                    )
                    _logger.info(
                        "Website fulfillment sync: skipping storefront order %s — %s",
                        record.get("storefrontOrderId"),
                        reason,
                    )
                    Log.create({
                        "storefront_order_id": record.get("storefrontOrderId") or 0,
                        "odoo_order_id_hint": record.get("odooOrderId") or 0,
                        "odoo_order_name_hint": record.get("odooOrderName") or record.get("salesOrderName") or "",
                        "fulfillment_status": (record.get("fulfillmentStatus") or "")[:64],
                        "state": "skipped",
                        "message": reason,
                        "raw_json": json.dumps(record, sort_keys=True),
                    })
                    continue

                still_watched_so_ids.add(sale_order.id)
                if self._apply_record_to_sale_order(sale_order, record, synced_at=synced_at):
                    stats["updated"] += 1
                else:
                    stats["unchanged"] += 1
            except Exception as exc:
                stats["errors"] += 1
                _logger.exception(
                    "Website fulfillment sync failed for storefront order %s",
                    record.get("storefrontOrderId"),
                )
                Log.create({
                    "storefront_order_id": record.get("storefrontOrderId") or 0,
                    "odoo_order_id_hint": record.get("odooOrderId") or 0,
                    "odoo_order_name_hint": record.get("odooOrderName") or record.get("salesOrderName") or "",
                    "fulfillment_status": (record.get("fulfillmentStatus") or "")[:64],
                    "state": "error",
                    "message": str(exc),
                    "raw_json": json.dumps(record, sort_keys=True),
                })

        # ---- pass 2: reconcile previously-watched orders that didn't come back
        self._reconcile_missing_orders(
            statuses=statuses,
            still_watched_so_ids=still_watched_so_ids,
            watched_fetch_count=len(orders),
            synced_at=synced_at,
            stats=stats,
            Log=Log,
        )

        self.env["ir.config_parameter"].sudo().set_param(
            "website_fulfillment_sync.last_sync_at",
            fields.Datetime.to_string(synced_at),
        )
        return stats

    @api.model
    def _reconcile_missing_orders(self, statuses, still_watched_so_ids, synced_at, stats, Log, watched_fetch_count=0):
        """For each sale.order Odoo still thinks is in the watched set but the
        fresh response didn't return, fetch the website's 'left-the-watched-set'
        statuses (completed, shipped, cancelled, ...) and apply any updated
        status we find. Orders the website returns nowhere are 'vanished':
        provided the watched fetch came back populated (so we trust the website's
        processing list is complete), they are demoted to 'other' to leave the
        watched set — the website, not Odoo, is the source of truth for what is
        processing. Deleted/test orders the website no longer knows about thus
        stop being pulled back in."""
        SaleOrder = self.env["sale.order"].sudo()
        previously_watched = SaleOrder.search([
            ("website_fulfillment_status", "in", list(statuses)),
        ])
        missing = previously_watched.filtered(lambda o: o.id not in still_watched_so_ids)
        if not missing:
            return

        reconcile_statuses = self._get_reconcile_statuses()
        if not reconcile_statuses:
            return

        # Build the lookup index of orders Odoo cares about (id, name)
        missing_by_id = {so.id: so for so in missing}
        missing_by_name = {so.name: so for so in missing if so.name}
        matched_so_ids = set()

        try:
            payload = self._fetch_fulfillment_payload(
                statuses=reconcile_statuses,
                limit=self._get_limit(),
            )
        except UserError as exc:
            _logger.warning("Website fulfillment reconcile failed: %s", exc)
            Log.create({
                "storefront_order_id": 0,
                "odoo_order_id_hint": 0,
                "odoo_order_name_hint": "",
                "fulfillment_status": "",
                "state": "error",
                "message": "Reconcile fetch failed: %s" % exc,
                "raw_json": "",
            })
            return

        for record in payload.get("orders") or []:
            # Only reconcile orders Odoo is currently tracking — ignore
            # everything else returned by this broader filter.
            so_id = record.get("odooOrderId")
            so_name = (record.get("odooOrderName") or record.get("salesOrderName") or "").strip()
            sale_order = (
                missing_by_id.get(int(so_id)) if so_id else None
            ) or missing_by_name.get(so_name)
            if not sale_order:
                continue
            try:
                matched_so_ids.add(sale_order.id)
                self._apply_record_to_sale_order(sale_order, record, synced_at=synced_at)
                stats["reconciled"] += 1
            except Exception as exc:
                stats["errors"] += 1
                _logger.exception(
                    "Reconcile apply failed for %s", sale_order.name,
                )
                Log.create({
                    "storefront_order_id": record.get("storefrontOrderId") or 0,
                    "odoo_order_id_hint": sale_order.id,
                    "odoo_order_name_hint": sale_order.name or "",
                    "fulfillment_status": (record.get("fulfillmentStatus") or "")[:64],
                    "state": "error",
                    "message": str(exc),
                    "raw_json": json.dumps(record, sort_keys=True),
                })

        # Anything still missing: not in watched set, not in reconcile set, not returned.
        # Either deleted on website or in a status we're not asking about.
        vanished = missing.filtered(lambda o: o.id not in matched_so_ids)
        # Only demote when the watched fetch came back populated — guards against a
        # transient empty/partial API response wrongly clearing the whole set.
        demote = watched_fetch_count > 0
        for so in vanished:
            stats["vanished"] += 1
            previous_status = so.website_fulfillment_status or ""
            _logger.info(
                "Website fulfillment sync: order %s (%s) vanished from website%s",
                so.name, so.id, " — demoting to 'other'" if demote else "",
            )
            if demote:
                so.write({
                    "website_fulfillment_status": "other",
                    "website_fulfillment_synced_at": synced_at,
                })
            Log.create({
                "storefront_order_id": so.website_storefront_order_id or 0,
                "odoo_order_id_hint": so.id,
                "odoo_order_name_hint": so.name or "",
                "fulfillment_status": previous_status,
                "state": "vanished",
                "message": (
                    "Order in Odoo watched set but not returned by website — demoted to 'other'"
                    if demote
                    else "Order in Odoo watched set but not returned by website (watched fetch empty — left untouched)"
                ),
                "raw_json": "",
            })

    # ------------------------------------------------------------------
    # Matching + application
    # ------------------------------------------------------------------
    @api.model
    def _find_sale_order(self, record):
        SaleOrder = self.env["sale.order"].sudo()

        odoo_order_id = record.get("odooOrderId")
        if odoo_order_id:
            try:
                order_id = int(odoo_order_id)
            except (TypeError, ValueError):
                order_id = 0
            if order_id:
                order = SaleOrder.browse(order_id)
                if order.exists():
                    return order

        for key in ("odooOrderName", "salesOrderName"):
            name = (record.get(key) or "").strip()
            if not name:
                continue
            order = SaleOrder.search([("name", "=", name)], limit=1)
            if order:
                return order

        return SaleOrder.browse()

    @api.model
    def _apply_record_to_sale_order(self, sale_order, record, synced_at=None):
        raw_status = (record.get("fulfillmentStatus") or "").strip()
        selection_value = raw_status.lower() if raw_status.lower() in KNOWN_FULFILLMENT_STATES else (
            "other" if raw_status else False
        )

        synced_at = synced_at or fields.Datetime.now()
        values = {
            "website_storefront_order_id": record.get("storefrontOrderId") or False,
            "website_public_token": record.get("publicToken") or False,
            "website_fulfillment_status": selection_value,
            "website_fulfillment_status_raw": raw_status or False,
            "website_payment_status": record.get("paymentStatus") or False,
            "website_sync_status": record.get("syncStatus") or False,
            "website_storefront_created_at": self._parse_datetime(record.get("createdAt")),
            "website_storefront_updated_at": self._parse_datetime(record.get("updatedAt")),
        }

        changed_fields = [
            name for name, new_value in values.items()
            if not self._values_equal(sale_order[name], new_value)
        ]

        write_values = {"website_fulfillment_synced_at": synced_at}
        if changed_fields:
            write_values.update(values)
        sale_order.write(write_values)
        return bool(changed_fields)

    @api.model
    def _values_equal(self, current, incoming):
        if isinstance(current, models.BaseModel):
            return True
        if current in (False, None) and incoming in (False, None, ""):
            return True
        if isinstance(current, datetime) and isinstance(incoming, str):
            return fields.Datetime.to_string(current) == incoming
        return current == incoming

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------
    @api.model
    def _fetch_fulfillment_payload(self, statuses=None, limit=None, ids=None):
        base_url = self._get_base_url()
        token = self._get_token()
        if not token:
            raise UserError(_(
                "Set the website status API token in System Parameters "
                "(%s) before syncing."
            ) % PARAM_TOKEN)

        params = {}
        if ids:
            params["ids"] = ",".join(str(int(i)) for i in ids)
        elif statuses:
            params["statuses"] = ",".join(statuses)
        if limit:
            params["limit"] = int(limit)

        url = "%s/api/odoo/orders/fulfillment-statuses" % base_url
        headers = {
            "Authorization": "Bearer %s" % token,
            "Accept": "application/json",
        }

        try:
            response = requests.get(url, headers=headers, params=params, timeout=30)
        except requests.RequestException as exc:
            raise UserError(_("Could not reach %s: %s") % (url, exc)) from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise UserError(_(
                "Website returned a non-JSON response (status %s): %s"
            ) % (response.status_code, response.text[:300])) from exc

        if response.status_code >= 400 or not payload.get("ok", True):
            message = payload.get("error") or payload.get("message") or response.text
            raise UserError(_("Website fulfillment API error (%s): %s") % (response.status_code, message))

        return payload

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------
    @api.model
    def _parse_datetime(self, value):
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
