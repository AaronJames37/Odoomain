import json
import logging
import re
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError


_logger = logging.getLogger(__name__)
_PAYPAL_AUD_CURRENCY = "AUD"
_PAYPAL_MAX_WINDOW = timedelta(days=30, hours=23, minutes=59, seconds=59)
_PAYPAL_TRANSACTION_SEARCH_ENDPOINT = "/v1/reporting/transactions"


class PaypalGrossConnection(models.Model):
    _name = "paypal.gross.connection"
    _description = "PayPal Gross Import Connection"
    _check_company_auto = True

    name = fields.Char(required=True, default="PayPal Gross Import")
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
    paypal_client_id = fields.Char(
        string="PayPal Client ID",
        required=True,
        copy=False,
    )
    paypal_client_secret = fields.Char(
        string="PayPal Client Secret",
        required=True,
        copy=False,
        groups="base.group_system",
    )
    environment = fields.Selection(
        [
            ("production", "Production"),
            ("sandbox", "Sandbox"),
        ],
        required=True,
        default="production",
    )
    journal_id = fields.Many2one(
        "account.journal",
        string="PayPal Gross Journal",
        required=True,
        check_company=True,
        domain="[('type', 'in', ('bank', 'cash')), ('company_id', '=', company_id)]",
        help="PayPal gross transactions will be imported into this mapped bank or cash journal.",
    )
    import_start_date = fields.Date(
        required=True,
        default=lambda self: fields.Date.today(),
        help="The first sync starts from this UTC date. Later syncs reuse the saved cursor plus the lookback window.",
    )
    lookback_hours = fields.Integer(
        default=72,
        required=True,
        help="Each run overlaps this many hours to catch delayed PayPal updates and to refresh changed statuses.",
    )
    limit_per_run = fields.Integer(
        default=500,
        required=True,
        help="Maximum PayPal records to scan per import run.",
    )
    paypal_access_token = fields.Char(copy=False, groups="base.group_system")
    paypal_access_token_expiry = fields.Datetime(copy=False, groups="base.group_system")
    last_paypal_updated_at = fields.Datetime(readonly=True, copy=False)
    last_successful_import_at = fields.Datetime(readonly=True, copy=False)
    last_import_summary = fields.Text(
        readonly=True,
        copy=False,
        help="Summary of the most recent import run, including skip and error reasons with sample transaction IDs.",
    )
    transaction_count = fields.Integer(compute="_compute_transaction_count")

    @api.depends("company_id")
    def _compute_transaction_count(self):
        grouped = self.env["paypal.gross.transaction"].read_group(
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
        if {"paypal_client_id", "paypal_client_secret", "environment"} & set(vals):
            vals = dict(vals)
            vals.update(
                {
                    "paypal_access_token": False,
                    "paypal_access_token_expiry": False,
                }
            )
        return super().write(vals)

    def action_test_connection(self):
        self.ensure_one()
        self._validate_import_settings()
        self._paypal_fetch_access_token(force_refresh=True)
        now_utc = self._get_now_utc()
        transactions = self._paypal_list_transactions_window(now_utc - timedelta(days=1), now_utc, 1)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("PayPal connection OK"),
                "message": _("PayPal returned %s transaction sample(s).") % len(transactions),
                "type": "success",
                "sticky": False,
            },
        }

    def action_import_now(self):
        return self.action_fetch_new_transactions()

    def action_backfill_fees(self):
        """Create the missing fee statement line for already-imported PayPal
        transactions that have a fee but no fee line yet."""
        self.ensure_one()
        transactions = self.env["paypal.gross.transaction"].search([
            ("connection_id", "=", self.id),
            ("state", "=", "imported"),
            ("fee_amount", "!=", 0),
            ("fee_statement_line_id", "=", False),
        ])
        created = 0
        errors = 0
        for txn in transactions:
            try:
                detail = json.loads(txn.raw_json or "{}")
                fee_line, _changed = self._find_or_create_fee_statement_line(
                    txn.paypal_external_key, detail, txn.fee_amount, False)
                txn.fee_statement_line_id = fee_line.id
                created += 1
            except Exception:
                errors += 1
                _logger.exception("PayPal fee backfill failed for %s", txn.paypal_transaction_id)
        return self._notification(
            _("PayPal fee backfill"),
            _("Fee lines created: %(created)s. Errors: %(errors)s.") % {"created": created, "errors": errors},
            "warning" if errors else "success",
            sticky=bool(errors),
        )

    def action_fetch_new_transactions(self):
        self.ensure_one()
        stats = self._import_transactions()
        return self._import_notification(_("PayPal fetch complete"), stats)

    def action_fetch_from_start_date(self):
        self.ensure_one()
        stats = self._import_transactions(force_from_start=True)
        return self._import_notification(_("PayPal fetch from start date complete"), stats)

    def _notification(self, title, message, kind, sticky=False):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": title, "message": message, "type": kind, "sticky": sticky},
        }

    def _import_notification(self, title, stats):
        message = _(
            "Scanned %(scanned)s PayPal records. Imported %(imported)s, updated %(updated)s, skipped %(skipped)s, errors %(errors)s."
        ) % stats
        skip_breakdown = self._format_skip_breakdown(stats)
        if skip_breakdown:
            message = "%s %s" % (message, skip_breakdown)
        error_breakdown = self._format_error_breakdown(stats)
        if error_breakdown:
            message = "%s %s" % (message, error_breakdown)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": title,
                "message": message,
                "type": "warning" if stats["errors"] else "success",
                "sticky": bool(stats["errors"]),
            },
        }

    def action_open_transactions(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("PayPal Gross Transactions"),
            "res_model": "paypal.gross.transaction",
            "view_mode": "list,form",
            "domain": [("connection_id", "=", self.id)],
            "context": {"default_connection_id": self.id},
        }

    @api.model
    def _cron_import_paypal_transactions(self):
        connections = self.search(
            [
                ("active", "=", True),
                ("auto_import", "=", True),
                ("paypal_client_id", "!=", False),
                ("paypal_client_secret", "!=", False),
                ("journal_id", "!=", False),
            ]
        )
        for connection in connections:
            try:
                connection._import_transactions()
            except Exception:
                _logger.exception("PayPal gross import failed for connection %s", connection.display_name)

    def _import_transactions(self, force_from_start=False):
        self.ensure_one()
        self._validate_import_settings()

        stats = self._new_import_stats()
        import_run_at = fields.Datetime.now()
        now_utc = self._get_now_utc()
        sync_start_utc = self._sync_start_utc(force_from_start=force_from_start)
        latest_updated_at = self._odoo_datetime_to_utc(self.last_paypal_updated_at) or sync_start_utc
        max_to_scan = max(1, min(self.limit_per_run or 500, 10000))

        for start_utc, end_utc in self._iter_query_windows(sync_start_utc, now_utc):
            if stats["scanned"] >= max_to_scan:
                break
            remaining = max_to_scan - stats["scanned"]
            for transaction_detail in self._paypal_list_transactions_window(start_utc, end_utc, remaining):
                stats["scanned"] += 1
                detail_updated_at = self._paypal_detail_updated_at(transaction_detail)
                if detail_updated_at and detail_updated_at > latest_updated_at:
                    latest_updated_at = detail_updated_at

                skip_reason = self._classify_skip_reason(transaction_detail)
                if skip_reason:
                    self._track_skipped_transaction(stats, transaction_detail, skip_reason)
                    continue

                existing_transaction = self._find_existing_transaction(transaction_detail)
                try:
                    outcome = self._create_or_update_imported_transaction(
                        transaction_detail,
                        existing_transaction,
                    )
                    stats[outcome] += 1
                    if outcome == "skipped":
                        self._track_skipped_transaction(
                            stats,
                            transaction_detail,
                            "already_up_to_date",
                            increment_counter=False,
                        )
                except Exception as exc:
                    stats["errors"] += 1
                    self._track_error_transaction(stats, transaction_detail, exc)
                    self._create_or_update_error_transaction(transaction_detail, exc, existing_transaction)
                    _logger.exception(
                        "Could not import PayPal transaction %s",
                        self._paypal_transaction_id(transaction_detail) or "unknown",
                    )

        self.write(
            {
                "last_successful_import_at": import_run_at,
                "last_paypal_updated_at": self._utc_datetime_to_odoo_string(latest_updated_at),
                "last_import_summary": self._format_import_summary(stats, import_run_at),
            }
        )
        return stats

    def _new_import_stats(self):
        return {
            "scanned": 0,
            "imported": 0,
            "updated": 0,
            "skipped": 0,
            "errors": 0,
            "skipped_breakdown": {},
            "skipped_examples": [],
            "error_breakdown": {},
            "error_examples": [],
        }

    def _validate_import_settings(self):
        self.ensure_one()
        if not self.paypal_client_id:
            raise UserError(_("Add a PayPal client ID before importing."))
        if not self.paypal_client_secret:
            raise UserError(_("Add a PayPal client secret before importing."))
        if not self.journal_id:
            raise UserError(_("Choose the PayPal gross journal before importing."))
        journal_currency = self.journal_id.currency_id or self.company_id.currency_id
        if not journal_currency:
            raise UserError(_("The selected company or journal must have a currency."))
        if journal_currency.name != _PAYPAL_AUD_CURRENCY:
            raise UserError(
                _("The selected company or journal must be in %s for this PayPal importer.") % _PAYPAL_AUD_CURRENCY
            )
        if self.lookback_hours < 0:
            raise UserError(_("Lookback hours cannot be negative."))
        if self.limit_per_run <= 0:
            raise UserError(_("Limit per run must be greater than zero."))

    def _sync_start_utc(self, force_from_start=False):
        self.ensure_one()
        base_start_utc = self._date_to_utc_datetime(self.import_start_date or fields.Date.today())
        if force_from_start:
            return base_start_utc
        if self.last_paypal_updated_at:
            cursor_start = self._odoo_datetime_to_utc(self.last_paypal_updated_at) - timedelta(hours=self.lookback_hours)
            return max(base_start_utc, cursor_start)
        return base_start_utc

    def _iter_query_windows(self, start_utc, end_utc):
        current = start_utc
        while current <= end_utc:
            window_end = min(current + _PAYPAL_MAX_WINDOW, end_utc)
            yield current, window_end
            if window_end >= end_utc:
                break
            current = window_end + timedelta(seconds=1)

    def _paypal_base_url(self):
        self.ensure_one()
        if self.environment == "sandbox":
            return "https://api-m.sandbox.paypal.com"
        return "https://api-m.paypal.com"

    def _paypal_fetch_access_token(self, force_refresh=False):
        self.ensure_one()
        now_utc = self._get_now_utc()
        expiry_utc = self._odoo_datetime_to_utc(self.paypal_access_token_expiry)
        if (
            not force_refresh
            and self.paypal_access_token
            and expiry_utc
            and now_utc < expiry_utc - timedelta(minutes=5)
        ):
            return self.paypal_access_token

        payload = self._paypal_request_json(
            "POST",
            "/v1/oauth2/token",
            data={"grant_type": "client_credentials"},
            auth_required=False,
            retry_on_401=False,
        )
        access_token = payload.get("access_token")
        if not access_token:
            raise UserError(_("PayPal did not return an access token."))

        expiry_utc = now_utc + timedelta(seconds=int(payload.get("expires_in") or 0))
        self.write(
            {
                "paypal_access_token": access_token,
                "paypal_access_token_expiry": self._utc_datetime_to_odoo_string(expiry_utc),
            }
        )
        return access_token

    def _paypal_request_json(
        self,
        method,
        endpoint,
        params=None,
        data=None,
        auth_required=True,
        retry_on_401=True,
    ):
        self.ensure_one()
        url = "%s%s" % (self._paypal_base_url(), endpoint)
        headers = {
            "Accept": "application/json",
            "PayPal-Enforce-ISO8601-Format": "true",
        }
        request_kwargs = {
            "method": method,
            "url": url,
            "headers": headers,
            "params": params,
            "timeout": 30,
        }

        if auth_required:
            headers["Content-Type"] = "application/json"
            headers["Authorization"] = "Bearer %s" % self._paypal_fetch_access_token()
        else:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            request_kwargs["auth"] = (self.paypal_client_id, self.paypal_client_secret)
            request_kwargs["data"] = data

        response = requests.request(**request_kwargs)
        if response.status_code == 401 and auth_required and retry_on_401:
            self.write({"paypal_access_token": False, "paypal_access_token_expiry": False})
            headers["Authorization"] = "Bearer %s" % self._paypal_fetch_access_token(force_refresh=True)
            response = requests.request(**request_kwargs)

        try:
            payload = response.json()
        except ValueError as exc:
            raise UserError(_("PayPal returned a non-JSON response.")) from exc

        if response.status_code >= 400:
            raise UserError(_("PayPal API error: %s") % self._paypal_error_message(response, payload, endpoint))
        return payload

    def _paypal_error_message(self, response, payload, endpoint=False):
        message = False
        if isinstance(payload, dict):
            details = payload.get("details") or []
            if details and details[0].get("description"):
                message = details[0]["description"]
            elif payload.get("message"):
                message = payload["message"]
            elif payload.get("error_description"):
                message = payload["error_description"]
            elif payload.get("error"):
                message = payload["error"]
        message = message or response.text or _("Unknown PayPal error")
        if self._is_paypal_transaction_search_permission_error(response, payload, endpoint, message):
            return _(
                "The PayPal credentials are valid, but this REST app is not authorized for Transaction Search. "
                "In the PayPal Developer Dashboard, open the %(environment)s REST app, enable Transaction Search, "
                "and save. PayPal says newly added Transaction Search permission can take up to 9 hours to become "
                "available for new access tokens."
            ) % {"environment": dict(self._fields["environment"].selection).get(self.environment, self.environment)}
        return message

    def _is_paypal_transaction_search_permission_error(self, response, payload, endpoint, message):
        if endpoint != _PAYPAL_TRANSACTION_SEARCH_ENDPOINT or response.status_code not in (401, 403):
            return False
        normalized = " ".join(
            [
                str(message or ""),
                str((payload or {}).get("name") if isinstance(payload, dict) else ""),
                str((payload or {}).get("error") if isinstance(payload, dict) else ""),
            ]
        ).lower()
        return (
            "insufficient permission" in normalized
            or "permission_denied" in normalized
            or "not_authorized" in normalized
            or "not authorized" in normalized
        )

    def _paypal_list_transactions_window(self, start_utc, end_utc, remaining_limit):
        self.ensure_one()
        page = 1
        transactions = []
        while len(transactions) < remaining_limit:
            page_size = min(500, remaining_limit - len(transactions))
            payload = self._paypal_request_json(
                "GET",
                _PAYPAL_TRANSACTION_SEARCH_ENDPOINT,
                params={
                    "start_date": self._paypal_format_datetime(start_utc),
                    "end_date": self._paypal_format_datetime(end_utc),
                    "fields": "all",
                    "balance_affecting_records_only": "Y",
                    "transaction_currency": _PAYPAL_AUD_CURRENCY,
                    "page_size": page_size,
                    "page": page,
                },
            )
            page_transactions = payload.get("transaction_details") or []
            if not page_transactions:
                break
            transactions.extend(page_transactions)

            total_pages = int(payload.get("total_pages") or 0)
            if total_pages:
                if page >= total_pages:
                    break
            elif len(page_transactions) < page_size:
                break
            page += 1
        return transactions

    def _classify_skip_reason(self, transaction_detail):
        transaction_info = (transaction_detail or {}).get("transaction_info") or {}
        if not transaction_info:
            return "missing_transaction_info"
        if not self._paypal_external_key(transaction_detail, raise_if_missing=False):
            return "missing_identifiers"
        transaction_amount = transaction_info.get("transaction_amount") or {}
        if transaction_amount.get("value") in (None, ""):
            return "missing_amount"
        currency_code = (transaction_amount.get("currency_code") or "").upper()
        if currency_code != _PAYPAL_AUD_CURRENCY:
            return "currency:%s" % (currency_code or "missing")
        return False

    def _track_skipped_transaction(self, stats, transaction_detail, reason_code, increment_counter=True):
        if increment_counter:
            stats["skipped"] += 1
        stats["skipped_breakdown"][reason_code] = stats["skipped_breakdown"].get(reason_code, 0) + 1
        examples = stats["skipped_examples"]
        if len(examples) >= 5:
            return
        transaction_info = (transaction_detail or {}).get("transaction_info") or {}
        examples.append(
            {
                "id": transaction_info.get("transaction_id") or "unknown",
                "reason": self._skip_reason_label(reason_code),
                "amount": self._format_money(transaction_info.get("transaction_amount")),
                "status": transaction_info.get("transaction_status") or "",
                "description": transaction_info.get("transaction_subject") or "",
            }
        )

    def _track_error_transaction(self, stats, transaction_detail, exception):
        reason_label = self._error_reason_label(exception)
        reason_code = "error:%s" % reason_label
        stats["error_breakdown"][reason_code] = stats["error_breakdown"].get(reason_code, 0) + 1
        examples = stats["error_examples"]
        if len(examples) >= 5:
            return
        transaction_info = (transaction_detail or {}).get("transaction_info") or {}
        examples.append(
            {
                "id": transaction_info.get("transaction_id") or "unknown",
                "reason": reason_label,
                "amount": self._format_money(transaction_info.get("transaction_amount")),
                "status": transaction_info.get("transaction_status") or "",
                "description": transaction_info.get("transaction_subject") or "",
            }
        )

    def _format_skip_breakdown(self, stats):
        breakdown = stats.get("skipped_breakdown") or {}
        if not breakdown:
            return ""
        parts = []
        for reason_code, count in sorted(breakdown.items(), key=lambda item: (-item[1], item[0])):
            parts.append("%s %s" % (count, self._skip_reason_label(reason_code).lower()))
        return _("Skipped breakdown: %s.") % "; ".join(parts)

    def _format_error_breakdown(self, stats):
        breakdown = stats.get("error_breakdown") or {}
        if not breakdown:
            return ""
        parts = []
        for reason_code, count in sorted(breakdown.items(), key=lambda item: (-item[1], item[0])):
            parts.append("%s %s" % (count, self._error_reason_from_code(reason_code).lower()))
        return _("Error breakdown: %s.") % "; ".join(parts)

    def _format_import_summary(self, stats, import_run_at):
        lines = [
            _("Run completed at %(timestamp)s UTC.") % {"timestamp": fields.Datetime.to_string(import_run_at)},
            _(
                "Scanned %(scanned)s PayPal records. Imported %(imported)s, updated %(updated)s, skipped %(skipped)s, errors %(errors)s."
            )
            % stats,
        ]

        skip_breakdown = stats.get("skipped_breakdown") or {}
        if skip_breakdown:
            lines.append("")
            lines.append(_("Skipped breakdown:"))
            for reason_code, count in sorted(skip_breakdown.items(), key=lambda item: (-item[1], item[0])):
                lines.append("- %s x %s" % (count, self._skip_reason_label(reason_code)))

        skip_examples = stats.get("skipped_examples") or []
        if skip_examples:
            lines.append("")
            lines.append(_("Sample skipped transactions:"))
            for example in skip_examples:
                parts = [example["id"], example["reason"]]
                if example["amount"]:
                    parts.append(example["amount"])
                if example["status"]:
                    parts.append("status=%s" % example["status"])
                if example["description"]:
                    parts.append(example["description"])
                lines.append("- %s" % " | ".join(parts))

        error_breakdown = stats.get("error_breakdown") or {}
        if error_breakdown:
            lines.append("")
            lines.append(_("Error breakdown:"))
            for reason_code, count in sorted(error_breakdown.items(), key=lambda item: (-item[1], item[0])):
                lines.append("- %s x %s" % (count, self._error_reason_from_code(reason_code)))

        error_examples = stats.get("error_examples") or []
        if error_examples:
            lines.append("")
            lines.append(_("Sample errored transactions:"))
            for example in error_examples:
                parts = [example["id"], example["reason"]]
                if example["amount"]:
                    parts.append(example["amount"])
                if example["status"]:
                    parts.append("status=%s" % example["status"])
                if example["description"]:
                    parts.append(example["description"])
                lines.append("- %s" % " | ".join(parts))

        return "\n".join(lines)

    def _skip_reason_label(self, reason_code):
        if reason_code == "already_up_to_date":
            return _("Already up to date")
        if reason_code == "missing_transaction_info":
            return _("Missing transaction info")
        if reason_code == "missing_identifiers":
            return _("Missing PayPal identifiers")
        if reason_code == "missing_amount":
            return _("Missing gross amount")
        if reason_code.startswith("currency:"):
            return _("Unexpected currency: %(currency)s") % {"currency": reason_code.split(":", 1)[1]}
        return reason_code.replace("_", " ")

    def _error_reason_label(self, exception):
        message = str(exception).strip() or _("Unknown import error")
        if len(message) > 120:
            message = "%s..." % message[:117]
        return message

    def _error_reason_from_code(self, reason_code):
        return reason_code.split(":", 1)[1] if ":" in reason_code else reason_code

    def _paypal_external_key(self, transaction_detail, raise_if_missing=True):
        transaction_info = (transaction_detail or {}).get("transaction_info") or {}
        transaction_id = transaction_info.get("transaction_id")
        event_code = transaction_info.get("transaction_event_code")
        initiation_date = transaction_info.get("transaction_initiation_date")
        if not transaction_id or not event_code or not initiation_date:
            if raise_if_missing:
                raise UserError(_("PayPal transaction is missing one or more required identifiers."))
            return False
        parts = [
            transaction_id,
            transaction_info.get("paypal_reference_id") or "",
            transaction_info.get("paypal_reference_id_type") or "",
            event_code,
            initiation_date,
        ]
        return "|".join(parts)

    def _find_existing_transaction(self, transaction_detail):
        external_key = self._paypal_external_key(transaction_detail, raise_if_missing=False)
        if not external_key:
            return False
        return self.env["paypal.gross.transaction"].search(
            [
                ("connection_id", "=", self.id),
                ("paypal_external_key", "=", external_key),
            ],
            limit=1,
        )

    def _create_or_update_imported_transaction(self, transaction_detail, existing_transaction=False):
        self.ensure_one()
        transaction_info = (transaction_detail or {}).get("transaction_info") or {}
        external_key = self._paypal_external_key(transaction_detail)
        currency = self._paypal_aud_currency()
        gross_amount = self._money_to_float(transaction_info.get("transaction_amount"), currency)
        fee_amount = self._money_to_float(transaction_info.get("fee_amount"), currency, default="0")
        net_amount = float(Decimal(str(gross_amount)) + Decimal(str(fee_amount)))
        statement_line, statement_line_changed = self._find_or_create_statement_line(
            external_key,
            transaction_detail,
            gross_amount,
            existing_transaction.statement_line_id if existing_transaction else False,
        )
        # PayPal deducts a per-transaction fee. Post it as its own statement line so
        # it hits the accounts (mirrors the Stripe importer). fee_amount is negative.
        fee_statement_line = False
        fee_statement_line_changed = False
        if fee_amount:
            fee_statement_line, fee_statement_line_changed = self._find_or_create_fee_statement_line(
                external_key,
                transaction_detail,
                fee_amount,
                existing_transaction.fee_statement_line_id if existing_transaction else False,
            )
        raw_json = json.dumps(transaction_detail, sort_keys=True)
        values = {
            "connection_id": self.id,
            "company_id": self.company_id.id,
            "journal_id": self.journal_id.id,
            "statement_line_id": statement_line.id,
            "fee_statement_line_id": fee_statement_line.id if fee_statement_line else False,
            "paypal_external_key": external_key,
            "paypal_transaction_id": transaction_info.get("transaction_id"),
            "paypal_reference_id": transaction_info.get("paypal_reference_id"),
            "paypal_reference_id_type": transaction_info.get("paypal_reference_id_type"),
            "paypal_transaction_event_code": transaction_info.get("transaction_event_code"),
            "paypal_transaction_status": transaction_info.get("transaction_status"),
            "paypal_initiation_at": self._utc_datetime_to_odoo_string(
                self._paypal_parse_datetime(transaction_info.get("transaction_initiation_date"))
            ),
            "paypal_updated_at": self._utc_datetime_to_odoo_string(
                self._paypal_detail_updated_at(transaction_detail)
            ),
            "date": self._paypal_detail_date(transaction_detail),
            "description": transaction_info.get("transaction_subject"),
            "currency_id": currency.id,
            "gross_amount": gross_amount,
            "fee_amount": fee_amount,
            "net_amount": net_amount,
            "state": "imported",
            "error_message": False,
            "raw_json": raw_json,
        }
        if existing_transaction:
            if (not self._record_values_differ(existing_transaction, values)
                    and not statement_line_changed and not fee_statement_line_changed):
                return "skipped"
            existing_transaction.write(values)
            return "updated"
        self.env["paypal.gross.transaction"].create(values)
        return "imported"

    def _find_or_create_fee_statement_line(self, external_key, transaction_detail, fee_amount,
                                           existing_statement_line=False):
        """Create/refresh the PayPal fee statement line. ``fee_amount`` is negative
        (a deduction). Booked into the same PayPal journal, labelled 'PayPal fee | ...'
        so the PayPal Fees reconcile model books it to the fee expense account."""
        StatementLine = self.env["account.bank.statement.line"].with_company(self.company_id)
        statement_line = existing_statement_line
        import_id = "%s:fee" % self._statement_import_id(external_key)

        if not statement_line:
            if "unique_import_id" in StatementLine._fields:
                statement_line = StatementLine.search([
                    ("journal_id", "=", self.journal_id.id),
                    ("unique_import_id", "=", import_id),
                ], limit=1)
            elif "ref" in StatementLine._fields:
                statement_line = StatementLine.search([
                    ("journal_id", "=", self.journal_id.id),
                    ("ref", "=", import_id),
                ], limit=1)

        transaction_info = (transaction_detail or {}).get("transaction_info") or {}
        fee_label = "PayPal fee | %s" % (transaction_info.get("transaction_id") or external_key)
        values = {
            "company_id": self.company_id.id,
            "journal_id": self.journal_id.id,
            "date": self._paypal_detail_date(transaction_detail),
            "amount": fee_amount,
        }
        if "payment_ref" in StatementLine._fields:
            values["payment_ref"] = fee_label
        elif "name" in StatementLine._fields:
            values["name"] = fee_label
        if "ref" in StatementLine._fields:
            values["ref"] = import_id
        if "unique_import_id" in StatementLine._fields:
            values["unique_import_id"] = import_id

        if statement_line:
            if self._record_values_differ(statement_line, values):
                statement_line.write(values)
                return statement_line, True
            return statement_line, False
        return StatementLine.create(values), True

    def _create_or_update_error_transaction(self, transaction_detail, exception, existing_transaction=False):
        external_key = self._paypal_external_key(transaction_detail, raise_if_missing=False)
        if not external_key:
            return False
        transaction_info = (transaction_detail or {}).get("transaction_info") or {}
        currency = self._paypal_aud_currency()
        gross_amount = self._money_to_float(transaction_info.get("transaction_amount"), currency, default="0")
        fee_amount = self._money_to_float(transaction_info.get("fee_amount"), currency, default="0")
        net_amount = float(Decimal(str(gross_amount)) + Decimal(str(fee_amount)))
        values = {
            "connection_id": self.id,
            "company_id": self.company_id.id,
            "journal_id": self.journal_id.id,
            "paypal_external_key": external_key,
            "paypal_transaction_id": transaction_info.get("transaction_id"),
            "paypal_reference_id": transaction_info.get("paypal_reference_id"),
            "paypal_reference_id_type": transaction_info.get("paypal_reference_id_type"),
            "paypal_transaction_event_code": transaction_info.get("transaction_event_code"),
            "paypal_transaction_status": transaction_info.get("transaction_status"),
            "paypal_initiation_at": self._utc_datetime_to_odoo_string(
                self._paypal_parse_datetime(transaction_info.get("transaction_initiation_date"))
            ),
            "paypal_updated_at": self._utc_datetime_to_odoo_string(
                self._paypal_detail_updated_at(transaction_detail)
            ),
            "date": self._paypal_detail_date(transaction_detail),
            "description": transaction_info.get("transaction_subject"),
            "currency_id": currency.id,
            "gross_amount": gross_amount,
            "fee_amount": fee_amount,
            "net_amount": net_amount,
            "state": "error",
            "error_message": str(exception),
            "raw_json": json.dumps(transaction_detail, sort_keys=True),
        }
        if existing_transaction:
            existing_transaction.write(values)
            return existing_transaction
        return self.env["paypal.gross.transaction"].create(values)

    def _find_or_create_statement_line(self, external_key, transaction_detail, gross_amount, existing_statement_line=False):
        StatementLine = self.env["account.bank.statement.line"].with_company(self.company_id)
        statement_line = existing_statement_line
        import_id = self._statement_import_id(external_key)

        if not statement_line:
            if "unique_import_id" in StatementLine._fields:
                statement_line = StatementLine.search(
                    [
                        ("journal_id", "=", self.journal_id.id),
                        ("unique_import_id", "=", import_id),
                    ],
                    limit=1,
                )
            elif "ref" in StatementLine._fields:
                statement_line = StatementLine.search(
                    [
                        ("journal_id", "=", self.journal_id.id),
                        ("ref", "=", import_id),
                    ],
                    limit=1,
                )

        transaction_info = (transaction_detail or {}).get("transaction_info") or {}
        payer_info = (transaction_detail or {}).get("payer_info") or {}
        values = {
            "company_id": self.company_id.id,
            "journal_id": self.journal_id.id,
            "date": self._paypal_detail_date(transaction_detail),
            "amount": gross_amount,
        }
        if "payment_ref" in StatementLine._fields:
            values["payment_ref"] = self._payment_reference(transaction_detail)
        elif "name" in StatementLine._fields:
            values["name"] = self._payment_reference(transaction_detail)
        if "ref" in StatementLine._fields:
            values["ref"] = import_id
        if "unique_import_id" in StatementLine._fields:
            values["unique_import_id"] = import_id
        if "partner_name" in StatementLine._fields:
            values["partner_name"] = self._payer_display_name(payer_info)
        if "transaction_type" in StatementLine._fields:
            values["transaction_type"] = transaction_info.get("transaction_event_code")

        if statement_line:
            if self._record_values_differ(statement_line, values):
                statement_line.write(values)
                return statement_line, True
            return statement_line, False
        return StatementLine.create(values), True

    def _statement_import_id(self, external_key):
        return "paypal:%s:%s" % (self.id, external_key)

    def _payer_display_name(self, payer_info):
        payer_name = payer_info.get("payer_name") or {}
        alternate_name = payer_name.get("alternate_full_name")
        if alternate_name:
            return alternate_name[:200]
        given_name = payer_name.get("given_name") or ""
        surname = payer_name.get("surname") or ""
        full_name = " ".join(part for part in [given_name, surname] if part)
        if full_name:
            return full_name[:200]
        return (payer_info.get("email_address") or "")[:200]

    def _payment_reference(self, transaction_detail):
        transaction_info = (transaction_detail or {}).get("transaction_info") or {}
        parts = [
            transaction_info.get("transaction_subject"),
            transaction_info.get("transaction_event_code"),
            transaction_info.get("transaction_id"),
        ]
        return " | ".join(part for part in parts if part)[:200]

    def _record_values_differ(self, record, values):
        for field_name, value in values.items():
            field = record._fields.get(field_name)
            if not field:
                continue
            current_value = record[field_name]
            compare_value = value
            if field.type == "many2one":
                current_value = current_value.id
            elif field.type == "date":
                current_value = fields.Date.to_date(current_value) if current_value else False
                compare_value = fields.Date.to_date(value) if value else False
            elif field.type == "datetime":
                current_value = fields.Datetime.to_datetime(current_value) if current_value else False
                compare_value = fields.Datetime.to_datetime(value) if value else False
            if current_value != compare_value:
                return True
        return False

    def _paypal_detail_updated_at(self, transaction_detail):
        transaction_info = (transaction_detail or {}).get("transaction_info") or {}
        return self._paypal_parse_datetime(
            transaction_info.get("transaction_updated_date")
            or transaction_info.get("transaction_initiation_date")
        )

    def _paypal_detail_date(self, transaction_detail):
        detail_dt = self._paypal_detail_updated_at(transaction_detail)
        return fields.Date.to_date(detail_dt.date() if detail_dt else fields.Date.today())

    def _paypal_transaction_id(self, transaction_detail):
        transaction_info = (transaction_detail or {}).get("transaction_info") or {}
        return transaction_info.get("transaction_id")

    @api.model
    def _paypal_aud_currency(self):
        currency = self.env["res.currency"].search([("name", "=", _PAYPAL_AUD_CURRENCY)], limit=1)
        if not currency:
            raise UserError(_("No Odoo currency found for %s.") % _PAYPAL_AUD_CURRENCY)
        return currency

    def _money_to_float(self, money_data, currency, default=None):
        value = (money_data or {}).get("value", default)
        if value in (None, ""):
            raise UserError(_("PayPal transaction is missing a money amount."))
        try:
            return float(Decimal(str(value)))
        except (InvalidOperation, ValueError) as exc:
            raise UserError(_("PayPal returned an invalid money value: %s") % value) from exc

    def _format_money(self, money_data):
        if not money_data:
            return ""
        value = money_data.get("value")
        currency_code = money_data.get("currency_code")
        if value in (None, ""):
            return currency_code or ""
        if currency_code:
            return "%s %s" % (value, currency_code)
        return str(value)

    @api.model
    def _get_now_utc(self):
        return datetime.now(timezone.utc)

    @api.model
    def _date_to_utc_datetime(self, value):
        return datetime.combine(fields.Date.to_date(value), time.min, tzinfo=timezone.utc)

    @api.model
    def _paypal_format_datetime(self, value):
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @api.model
    def _paypal_parse_datetime(self, value):
        if not value:
            return False
        normalized = str(value).strip()
        if normalized.endswith("Z"):
            normalized = "%s+00:00" % normalized[:-1]
        elif re.search(r"[+-]\d{4}$", normalized):
            normalized = "%s:%s" % (normalized[:-2], normalized[-2:])
        dt_value = datetime.fromisoformat(normalized)
        if dt_value.tzinfo is None:
            dt_value = dt_value.replace(tzinfo=timezone.utc)
        return dt_value.astimezone(timezone.utc)

    @api.model
    def _odoo_datetime_to_utc(self, value):
        if not value:
            return False
        dt_value = fields.Datetime.to_datetime(value)
        if dt_value.tzinfo is None:
            dt_value = dt_value.replace(tzinfo=timezone.utc)
        return dt_value.astimezone(timezone.utc)

    @api.model
    def _utc_datetime_to_odoo_string(self, value):
        if not value:
            return False
        if value.tzinfo:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return fields.Datetime.to_string(value)


class PaypalGrossTransaction(models.Model):
    _name = "paypal.gross.transaction"
    _description = "Imported PayPal Gross Transaction"
    _order = "paypal_updated_at desc, id desc"
    _rec_name = "paypal_transaction_id"

    _paypal_external_key_connection_uniq = models.Constraint(
        "unique(connection_id, paypal_external_key)",
        "This PayPal transaction has already been imported for this connection.",
    )

    connection_id = fields.Many2one(
        "paypal.gross.connection",
        required=True,
        ondelete="cascade",
    )
    company_id = fields.Many2one("res.company", required=True)
    journal_id = fields.Many2one("account.journal", required=True)
    statement_line_id = fields.Many2one("account.bank.statement.line", readonly=True)
    fee_statement_line_id = fields.Many2one(
        "account.bank.statement.line", readonly=True, string="Fee Statement Line")
    paypal_external_key = fields.Char(required=True, index=True)
    paypal_transaction_id = fields.Char(required=True, index=True)
    paypal_reference_id = fields.Char(index=True)
    paypal_reference_id_type = fields.Char()
    paypal_transaction_event_code = fields.Char(index=True)
    paypal_transaction_status = fields.Char()
    paypal_initiation_at = fields.Datetime()
    paypal_updated_at = fields.Datetime()
    date = fields.Date()
    description = fields.Char()
    currency_id = fields.Many2one("res.currency", required=True)
    gross_amount = fields.Monetary(currency_field="currency_id")
    fee_amount = fields.Monetary(currency_field="currency_id")
    net_amount = fields.Monetary(currency_field="currency_id")
    state = fields.Selection(
        [
            ("imported", "Imported"),
            ("error", "Error"),
        ],
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
            "name": _("PayPal Statement Line"),
            "res_model": "account.bank.statement.line",
            "res_id": self.statement_line_id.id,
            "view_mode": "form",
        }
