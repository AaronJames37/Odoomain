import base64
import json
import logging
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError


_logger = logging.getLogger(__name__)

# eBay Finances API only supports gross monetary transactions. We import SALE
# (and related credit) transaction types as positive gross, refunds as negative.
_EBAY_FINANCES_PATH = "/sell/finances/v1/transaction"
_EBAY_OAUTH_PATH = "/identity/v1/oauth2/token"
_EBAY_FINANCES_SCOPE = "https://api.ebay.com/oauth/api_scope/sell.finances"
# Max window eBay allows without an explicit end date is 90 days.
_EBAY_MAX_WINDOW = timedelta(days=89)


class EbayGrossConnection(models.Model):
    _name = "ebay.gross.connection"
    _description = "eBay Gross Import Connection"
    _check_company_auto = True

    name = fields.Char(required=True, default="eBay Gross Import")
    active = fields.Boolean(default=True)
    auto_import = fields.Boolean(
        default=True,
        help="When enabled, the scheduled action imports transactions for this connection.",
    )
    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
    )
    ebay_client_id = fields.Char(
        string="eBay App ID (Client ID)",
        required=True,
        copy=False,
    )
    ebay_client_secret = fields.Char(
        string="eBay Cert ID (Client Secret)",
        required=True,
        copy=False,
        groups="base.group_system",
    )
    # The Finances API uses the authorization-code grant; the seller authorises the
    # app once and we store the long-lived refresh token to mint access tokens.
    ebay_refresh_token = fields.Char(
        string="eBay Refresh Token",
        copy=False,
        groups="base.group_system",
        help="Obtained from the seller consent (authorization code) flow with the "
        "sell.finances scope. Paste it here after authorising the app.",
    )
    environment = fields.Selection(
        [
            ("production", "Production"),
            ("sandbox", "Sandbox"),
        ],
        required=True,
        default="production",
    )
    marketplace_id = fields.Char(
        default="EBAY_AU",
        required=True,
        help="eBay marketplace, e.g. EBAY_AU, EBAY_US.",
    )
    journal_id = fields.Many2one(
        "account.journal",
        string="eBay Gross Journal",
        required=True,
        check_company=True,
        domain="[('type', 'in', ('bank', 'cash')), ('company_id', '=', company_id)]",
        help="eBay gross transactions will be imported into this mapped bank or cash journal.",
    )
    currency_code = fields.Char(
        default="AUD",
        required=True,
        help="Only transactions in this currency are imported (must match the journal currency).",
    )
    import_start_date = fields.Date(
        required=True,
        default=lambda self: fields.Date.today(),
        help="First sync starts from this UTC date. Later syncs use the saved cursor plus lookback.",
    )
    lookback_hours = fields.Integer(
        default=72,
        required=True,
        help="Each run overlaps this many hours to catch delayed eBay updates.",
    )
    limit_per_run = fields.Integer(
        default=500,
        required=True,
        help="Maximum eBay transactions to scan per run.",
    )
    ebay_access_token = fields.Char(copy=False, groups="base.group_system")
    ebay_access_token_expiry = fields.Datetime(copy=False, groups="base.group_system")
    last_ebay_updated_at = fields.Datetime(readonly=True, copy=False)
    last_successful_import_at = fields.Datetime(readonly=True, copy=False)
    last_import_summary = fields.Text(readonly=True, copy=False)
    transaction_count = fields.Integer(compute="_compute_transaction_count")

    @api.depends("company_id")
    def _compute_transaction_count(self):
        grouped = self.env["ebay.gross.transaction"].read_group(
            [("connection_id", "in", self.ids)],
            ["connection_id"],
            ["connection_id"],
        )
        counts = {
            row["connection_id"][0]: row.get("connection_id_count", row.get("__count", 0))
            for row in grouped
            if row.get("connection_id")
        }
        for record in self:
            record.transaction_count = counts.get(record.id, 0)

    def write(self, vals):
        # Any credential change invalidates the cached access token.
        if {"ebay_client_id", "ebay_client_secret", "ebay_refresh_token", "environment"} & set(vals):
            vals = dict(vals)
            vals.update({"ebay_access_token": False, "ebay_access_token_expiry": False})
        return super().write(vals)

    # ------------------------------------------------------------------ actions
    def action_test_connection(self):
        self.ensure_one()
        self._validate_import_settings()
        self._ebay_fetch_access_token(force_refresh=True)
        now_utc = self._get_now_utc()
        transactions = self._ebay_list_transactions_window(now_utc - timedelta(days=1), now_utc, 1)
        return self._notification(
            _("eBay connection OK"),
            _("eBay returned %s transaction sample(s).") % len(transactions),
            "success",
        )

    def action_import_now(self):
        return self.action_fetch_new_transactions()

    def action_fetch_new_transactions(self):
        self.ensure_one()
        stats = self._import_transactions()
        return self._import_notification(_("eBay fetch complete"), stats)

    def action_fetch_from_start_date(self):
        self.ensure_one()
        stats = self._import_transactions(force_from_start=True)
        return self._import_notification(_("eBay fetch from start date complete"), stats)

    def action_open_transactions(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("eBay Gross Transactions"),
            "res_model": "ebay.gross.transaction",
            "view_mode": "list,form",
            "domain": [("connection_id", "=", self.id)],
            "context": {"default_connection_id": self.id},
        }

    def _notification(self, title, message, kind, sticky=False):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": title, "message": message, "type": kind, "sticky": sticky},
        }

    def _import_notification(self, title, stats):
        message = _(
            "Scanned %(scanned)s eBay records. Imported %(imported)s, updated %(updated)s, "
            "skipped %(skipped)s, errors %(errors)s."
        ) % stats
        return self._notification(title, message, "warning" if stats["errors"] else "success",
                                  sticky=bool(stats["errors"]))

    # -------------------------------------------------------------------- cron
    @api.model
    def _cron_import_ebay_transactions(self):
        connections = self.search([
            ("active", "=", True),
            ("auto_import", "=", True),
            ("ebay_client_id", "!=", False),
            ("ebay_client_secret", "!=", False),
            ("ebay_refresh_token", "!=", False),
            ("journal_id", "!=", False),
        ])
        for connection in connections:
            try:
                connection._import_transactions()
            except Exception:
                _logger.exception("eBay gross import failed for connection %s", connection.display_name)

    # --------------------------------------------------------------- importing
    def _import_transactions(self, force_from_start=False):
        self.ensure_one()
        self._validate_import_settings()

        stats = {"scanned": 0, "imported": 0, "updated": 0, "skipped": 0, "errors": 0}
        import_run_at = fields.Datetime.now()
        now_utc = self._get_now_utc()
        sync_start_utc = self._sync_start_utc(force_from_start=force_from_start)
        latest_updated_at = self._odoo_datetime_to_utc(self.last_ebay_updated_at) or sync_start_utc
        max_to_scan = max(1, min(self.limit_per_run or 500, 10000))

        for start_utc, end_utc in self._iter_query_windows(sync_start_utc, now_utc):
            if stats["scanned"] >= max_to_scan:
                break
            remaining = max_to_scan - stats["scanned"]
            for txn in self._ebay_list_transactions_window(start_utc, end_utc, remaining):
                stats["scanned"] += 1
                txn_dt = self._ebay_parse_datetime(txn.get("transactionDate"))
                if txn_dt and txn_dt > latest_updated_at:
                    latest_updated_at = txn_dt

                skip = self._classify_skip_reason(txn)
                if skip:
                    stats["skipped"] += 1
                    continue

                existing = self._find_existing_transaction(txn)
                try:
                    outcome = self._create_or_update_imported_transaction(txn, existing)
                    stats[outcome] += 1
                except Exception as exc:
                    stats["errors"] += 1
                    self._create_or_update_error_transaction(txn, exc, existing)
                    _logger.exception("Could not import eBay transaction %s", txn.get("transactionId"))

        self.write({
            "last_successful_import_at": import_run_at,
            "last_ebay_updated_at": self._utc_datetime_to_odoo_string(latest_updated_at),
            "last_import_summary": _("Run at %(ts)s UTC. Imported %(imported)s, updated %(updated)s, "
                                     "skipped %(skipped)s, errors %(errors)s of %(scanned)s scanned.")
            % dict(stats, ts=fields.Datetime.to_string(import_run_at)),
        })
        return stats

    def _validate_import_settings(self):
        self.ensure_one()
        if not (self.ebay_client_id and self.ebay_client_secret):
            raise UserError(_("Add the eBay App ID and Cert ID before importing."))
        if not self.ebay_refresh_token:
            raise UserError(_("Add the eBay refresh token (from the sell.finances consent flow) before importing."))
        if not self.journal_id:
            raise UserError(_("Choose the eBay gross journal before importing."))
        journal_currency = self.journal_id.currency_id or self.company_id.currency_id
        if not journal_currency:
            raise UserError(_("The selected company or journal must have a currency."))
        if journal_currency.name != (self.currency_code or "").upper():
            raise UserError(_("The journal currency (%(j)s) must match the configured currency (%(c)s).")
                            % {"j": journal_currency.name, "c": self.currency_code})
        if self.lookback_hours < 0:
            raise UserError(_("Lookback hours cannot be negative."))
        if self.limit_per_run <= 0:
            raise UserError(_("Limit per run must be greater than zero."))

    def _sync_start_utc(self, force_from_start=False):
        self.ensure_one()
        base_start = self._date_to_utc_datetime(self.import_start_date or fields.Date.today())
        if force_from_start:
            return base_start
        if self.last_ebay_updated_at:
            cursor = self._odoo_datetime_to_utc(self.last_ebay_updated_at) - timedelta(hours=self.lookback_hours)
            return max(base_start, cursor)
        return base_start

    def _iter_query_windows(self, start_utc, end_utc):
        current = start_utc
        while current <= end_utc:
            window_end = min(current + _EBAY_MAX_WINDOW, end_utc)
            yield current, window_end
            if window_end >= end_utc:
                break
            current = window_end + timedelta(seconds=1)

    # ----------------------------------------------------------------- oauth/api
    def _ebay_base_url(self):
        self.ensure_one()
        return "https://apiz.sandbox.ebay.com" if self.environment == "sandbox" else "https://apiz.ebay.com"

    def _ebay_oauth_url(self):
        self.ensure_one()
        return "https://api.sandbox.ebay.com" if self.environment == "sandbox" else "https://api.ebay.com"

    def _ebay_fetch_access_token(self, force_refresh=False):
        self.ensure_one()
        now_utc = self._get_now_utc()
        expiry = self._odoo_datetime_to_utc(self.ebay_access_token_expiry)
        if (not force_refresh and self.ebay_access_token and expiry
                and now_utc < expiry - timedelta(minutes=5)):
            return self.ebay_access_token

        basic = base64.b64encode(
            ("%s:%s" % (self.ebay_client_id, self.ebay_client_secret)).encode()
        ).decode()
        response = requests.post(
            "%s%s" % (self._ebay_oauth_url(), _EBAY_OAUTH_PATH),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": "Basic %s" % basic,
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.ebay_refresh_token,
                "scope": _EBAY_FINANCES_SCOPE,
            },
            timeout=30,
        )
        payload = self._json_or_raise(response)
        if response.status_code >= 400:
            raise UserError(_("eBay OAuth error: %s") % (payload.get("error_description") or response.text))
        token = payload.get("access_token")
        if not token:
            raise UserError(_("eBay did not return an access token."))
        self.write({
            "ebay_access_token": token,
            "ebay_access_token_expiry": self._utc_datetime_to_odoo_string(
                now_utc + timedelta(seconds=int(payload.get("expires_in") or 0))
            ),
        })
        return token

    def _ebay_get(self, path, params):
        self.ensure_one()
        response = requests.get(
            "%s%s" % (self._ebay_base_url(), path),
            headers={
                "Authorization": "Bearer %s" % self._ebay_fetch_access_token(),
                "X-EBAY-C-MARKETPLACE-ID": self.marketplace_id or "EBAY_AU",
                "Accept": "application/json",
            },
            params=params,
            timeout=30,
        )
        if response.status_code == 401:
            # Token might be stale; force a refresh once.
            response = requests.get(
                "%s%s" % (self._ebay_base_url(), path),
                headers={
                    "Authorization": "Bearer %s" % self._ebay_fetch_access_token(force_refresh=True),
                    "X-EBAY-C-MARKETPLACE-ID": self.marketplace_id or "EBAY_AU",
                    "Accept": "application/json",
                },
                params=params,
                timeout=30,
            )
        payload = self._json_or_raise(response)
        if response.status_code >= 400:
            errors = (payload.get("errors") or [{}])[0]
            raise UserError(_("eBay API error: %s") % (errors.get("message") or response.text))
        return payload

    def _ebay_list_transactions_window(self, start_utc, end_utc, remaining_limit):
        self.ensure_one()
        collected = []
        offset = 0
        date_filter = "transactionDate:[%s..%s]" % (
            self._ebay_format_datetime(start_utc), self._ebay_format_datetime(end_utc))
        while len(collected) < remaining_limit:
            page_limit = min(200, remaining_limit - len(collected))
            payload = self._ebay_get(_EBAY_FINANCES_PATH, {
                "filter": date_filter,
                "limit": page_limit,
                "offset": offset,
            })
            page = payload.get("transactions") or []
            if not page:
                break
            collected.extend(page)
            total = int(payload.get("total") or 0)
            offset += len(page)
            if offset >= total or len(page) < page_limit:
                break
        return collected

    # ------------------------------------------------------------- transform
    def _classify_skip_reason(self, txn):
        if not txn.get("transactionId"):
            return "missing_id"
        amount = (txn.get("amount") or {})
        if amount.get("value") in (None, ""):
            return "missing_amount"
        if (amount.get("currency") or "").upper() != (self.currency_code or "").upper():
            return "currency"
        # Only monetary sale/refund/credit/debit types affect the balance; eBay's
        # Finances API already returns balance-affecting records.
        return False

    def _find_existing_transaction(self, txn):
        return self.env["ebay.gross.transaction"].search([
            ("connection_id", "=", self.id),
            ("ebay_transaction_id", "=", txn.get("transactionId")),
        ], limit=1)

    def _signed_amount(self, txn):
        """eBay returns a positive amount + a bookingEntry of CREDIT/DEBIT.
        CREDIT increases the seller balance (income), DEBIT decreases it."""
        currency = self._journal_currency()
        value = self._money_to_float(txn.get("amount"))
        booking = (txn.get("bookingEntry") or "").upper()
        if booking == "DEBIT":
            value = -value
        return value

    def _create_or_update_imported_transaction(self, txn, existing=False):
        self.ensure_one()
        currency = self._journal_currency()
        gross = self._signed_amount(txn)
        fee = self._money_to_float((txn.get("totalFeeAmount") or txn.get("totalFeeBasisAmount")), default="0")
        statement_line, changed = self._find_or_create_statement_line(txn, gross)
        values = {
            "connection_id": self.id,
            "company_id": self.company_id.id,
            "journal_id": self.journal_id.id,
            "statement_line_id": statement_line.id,
            "ebay_transaction_id": txn.get("transactionId"),
            "ebay_transaction_type": txn.get("transactionType"),
            "ebay_transaction_status": txn.get("transactionStatus"),
            "ebay_booking_entry": txn.get("bookingEntry"),
            "ebay_order_id": txn.get("orderId"),
            "ebay_payout_id": txn.get("payoutId"),
            "date": self._ebay_detail_date(txn),
            "ebay_transaction_at": self._utc_datetime_to_odoo_string(
                self._ebay_parse_datetime(txn.get("transactionDate"))),
            "description": (txn.get("transactionMemo") or txn.get("transactionType") or "")[:200],
            "currency_id": currency.id,
            "gross_amount": gross,
            "fee_amount": fee,
            "state": "imported",
            "error_message": False,
            "raw_json": json.dumps(txn, sort_keys=True),
        }
        if existing:
            if not self._record_values_differ(existing, values) and not changed:
                return "skipped"
            existing.write(values)
            return "updated"
        self.env["ebay.gross.transaction"].create(values)
        return "imported"

    def _create_or_update_error_transaction(self, txn, exc, existing=False):
        if not txn.get("transactionId"):
            return False
        currency = self._journal_currency()
        values = {
            "connection_id": self.id,
            "company_id": self.company_id.id,
            "journal_id": self.journal_id.id,
            "ebay_transaction_id": txn.get("transactionId"),
            "ebay_transaction_type": txn.get("transactionType"),
            "date": self._ebay_detail_date(txn),
            "currency_id": currency.id,
            "gross_amount": self._money_to_float(txn.get("amount"), default="0"),
            "state": "error",
            "error_message": str(exc),
            "raw_json": json.dumps(txn, sort_keys=True),
        }
        if existing:
            existing.write(values)
            return existing
        return self.env["ebay.gross.transaction"].create(values)

    def _find_or_create_statement_line(self, txn, gross_amount):
        StatementLine = self.env["account.bank.statement.line"].with_company(self.company_id)
        import_id = "ebay:%s:%s" % (self.id, txn.get("transactionId"))
        statement_line = False
        if "unique_import_id" in StatementLine._fields:
            statement_line = StatementLine.search([
                ("journal_id", "=", self.journal_id.id),
                ("unique_import_id", "=", import_id),
            ], limit=1)

        values = {
            "company_id": self.company_id.id,
            "journal_id": self.journal_id.id,
            "date": self._ebay_detail_date(txn),
            "amount": gross_amount,
        }
        ref_label = " | ".join(p for p in [
            txn.get("transactionType"), txn.get("orderId"), txn.get("transactionId")] if p)[:200]
        if "payment_ref" in StatementLine._fields:
            values["payment_ref"] = ref_label
        elif "name" in StatementLine._fields:
            values["name"] = ref_label
        if "ref" in StatementLine._fields:
            values["ref"] = txn.get("transactionId")
        if "unique_import_id" in StatementLine._fields:
            values["unique_import_id"] = import_id

        if statement_line:
            if self._record_values_differ(statement_line, values):
                statement_line.write(values)
                return statement_line, True
            return statement_line, False
        return StatementLine.create(values), True

    def _record_values_differ(self, record, values):
        for field_name, value in values.items():
            field = record._fields.get(field_name)
            if not field:
                continue
            current = record[field_name]
            compare = value
            if field.type == "many2one":
                current = current.id
            elif field.type == "date":
                current = fields.Date.to_date(current) if current else False
                compare = fields.Date.to_date(value) if value else False
            elif field.type == "datetime":
                current = fields.Datetime.to_datetime(current) if current else False
                compare = fields.Datetime.to_datetime(value) if value else False
            if current != compare:
                return True
        return False

    # ------------------------------------------------------------- helpers
    def _journal_currency(self):
        return self.journal_id.currency_id or self.company_id.currency_id

    def _money_to_float(self, money, default=None):
        value = (money or {}).get("value", default)
        if value in (None, ""):
            raise UserError(_("eBay transaction is missing a money amount."))
        try:
            return float(Decimal(str(value)))
        except (InvalidOperation, ValueError) as exc:
            raise UserError(_("eBay returned an invalid money value: %s") % value) from exc

    def _ebay_detail_date(self, txn):
        dt = self._ebay_parse_datetime(txn.get("transactionDate"))
        return fields.Date.to_date(dt.date() if dt else fields.Date.today())

    def _json_or_raise(self, response):
        try:
            return response.json()
        except ValueError as exc:
            raise UserError(_("eBay returned a non-JSON response.")) from exc

    @api.model
    def _get_now_utc(self):
        return datetime.now(timezone.utc)

    @api.model
    def _date_to_utc_datetime(self, value):
        return datetime.combine(fields.Date.to_date(value), time.min, tzinfo=timezone.utc)

    @api.model
    def _ebay_format_datetime(self, value):
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    @api.model
    def _ebay_parse_datetime(self, value):
        if not value:
            return False
        normalized = str(value).strip()
        if normalized.endswith("Z"):
            normalized = "%s+00:00" % normalized[:-1]
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    @api.model
    def _odoo_datetime_to_utc(self, value):
        if not value:
            return False
        dt = fields.Datetime.to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    @api.model
    def _utc_datetime_to_odoo_string(self, value):
        if not value:
            return False
        if value.tzinfo:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return fields.Datetime.to_string(value)


class EbayGrossTransaction(models.Model):
    _name = "ebay.gross.transaction"
    _description = "Imported eBay Gross Transaction"
    _order = "ebay_transaction_at desc, id desc"
    _rec_name = "ebay_transaction_id"

    _sql_constraints = [
        (
            "ebay_transaction_connection_uniq",
            "unique(connection_id, ebay_transaction_id)",
            "This eBay transaction has already been imported for this connection.",
        )
    ]

    connection_id = fields.Many2one("ebay.gross.connection", required=True, ondelete="cascade")
    company_id = fields.Many2one("res.company", required=True)
    journal_id = fields.Many2one("account.journal", required=True)
    statement_line_id = fields.Many2one("account.bank.statement.line", readonly=True)
    ebay_transaction_id = fields.Char(required=True, index=True)
    ebay_transaction_type = fields.Char()
    ebay_transaction_status = fields.Char()
    ebay_booking_entry = fields.Char()
    ebay_order_id = fields.Char(index=True)
    ebay_payout_id = fields.Char(index=True)
    ebay_transaction_at = fields.Datetime()
    date = fields.Date()
    description = fields.Char()
    currency_id = fields.Many2one("res.currency", required=True)
    gross_amount = fields.Monetary(currency_field="currency_id")
    fee_amount = fields.Monetary(currency_field="currency_id")
    state = fields.Selection(
        [("imported", "Imported"), ("error", "Error")],
        required=True,
        default="imported",
    )
    error_message = fields.Text()
    raw_json = fields.Text(readonly=True)

    def action_open_statement_line(self):
        self.ensure_one()
        if not self.statement_line_id:
            raise UserError(_("This transaction does not have an imported statement line."))
        return {
            "type": "ir.actions.act_window",
            "name": _("eBay Statement Line"),
            "res_model": "account.bank.statement.line",
            "res_id": self.statement_line_id.id,
            "view_mode": "form",
        }
