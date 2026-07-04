import calendar
import json
import logging
from datetime import datetime, time, timezone
from decimal import Decimal

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError


_logger = logging.getLogger(__name__)
_IMPORTABLE_STRIPE_REPORTING_CATEGORIES = {"charge", "payment", "refund", "payout"}
_IMPORTABLE_STRIPE_TYPES = {"charge", "payment", "refund", "payout"}


class StripeGrossConnection(models.Model):
    _name = "stripe.gross.connection"
    _description = "Stripe Gross Import Connection"
    _check_company_auto = True

    name = fields.Char(required=True, default="Stripe Gross Import")
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
    secret_key = fields.Char(
        string="Stripe Secret Key",
        required=True,
        copy=False,
        groups="account.group_account_manager",
    )
    connected_account_id = fields.Char(
        string="Stripe Connected Account ID",
        copy=False,
        help="Optional. Use this only for Stripe Connect accounts.",
    )
    journal_id = fields.Many2one(
        "account.journal",
        string="Stripe Gross Journal",
        required=True,
        check_company=True,
        domain="[('type', 'in', ('bank', 'cash')), ('company_id', '=', company_id)]",
        help="Gross Stripe transactions will be imported into this mapped bank or cash journal.",
    )
    fee_journal_id = fields.Many2one(
        "account.journal",
        string="Stripe Fee Journal",
        check_company=True,
        domain="[('type', 'in', ('bank', 'cash')), ('company_id', '=', company_id)]",
        help="Stripe fee statement lines are created in this journal. Leave empty to use the same journal as gross transactions.",
    )
    import_start_date = fields.Date(
        required=True,
        default=lambda self: fields.Date.today(),
        help="First sync starts from this UTC date. Later syncs use the saved cursor plus lookback.",
    )
    status_filter = fields.Selection(
        [
            ("all", "Pending and available"),
            ("available", "Available only"),
        ],
        default="all",
        required=True,
        help="Use available only if you want transactions imported after Stripe settlement.",
    )
    lookback_hours = fields.Integer(
        default=72,
        required=True,
        help="Each run overlaps this many hours to avoid missing late-arriving Stripe records. Duplicates are ignored.",
    )
    limit_per_run = fields.Integer(
        default=500,
        required=True,
        help="Maximum Stripe balance transactions to scan per run. Stripe returns up to 100 per page.",
    )
    last_stripe_created_timestamp = fields.Integer(readonly=True, copy=False)
    historical_backfill_cursor_timestamp = fields.Integer(
        string="Historical Backfill Cursor",
        readonly=True,
        copy=False,
        help="Next historical backfill continues before this Stripe created timestamp.",
    )
    last_successful_import_at = fields.Datetime(readonly=True, copy=False)
    last_import_summary = fields.Text(
        readonly=True,
        copy=False,
        help="Summary of the most recent import run, including skip reasons and sample transaction IDs.",
    )
    transaction_count = fields.Integer(compute="_compute_transaction_count")

    @api.depends("company_id")
    def _compute_transaction_count(self):
        grouped = self.env["stripe.gross.transaction"].read_group(
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

    def action_test_connection(self):
        self.ensure_one()
        payload = self._stripe_get("/v1/balance_transactions", {"limit": 1})
        count = len(payload.get("data", []))
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Stripe connection OK"),
                "message": _("Stripe returned %s balance transaction sample(s).") % count,
                "type": "success",
                "sticky": False,
            },
        }

    def action_import_now(self):
        return self.action_fetch_new_transactions()

    def action_fetch_new_transactions(self):
        self.ensure_one()
        stats = self._import_transactions()
        return self._import_notification(_("Stripe fetch complete"), stats)

    def action_backfill_older_transactions(self):
        self.ensure_one()
        stats = self._import_transactions(historical_backfill=True)
        return self._import_notification(_("Stripe historical backfill complete"), stats)

    def action_backfill_fees(self):
        self.ensure_one()
        transactions = self.env["stripe.gross.transaction"].search([
            ("connection_id", "=", self.id),
            ("state", "=", "imported"),
            ("fee_amount", ">", 0),
            ("fee_statement_line_id", "=", False),
        ])
        created = 0
        errors = 0
        for txn in transactions:
            try:
                raw = json.loads(txn.raw_json or "{}")
                fee_line = self._find_or_create_fee_statement_line(raw, txn.fee_amount)
                txn.fee_statement_line_id = fee_line.id
                created += 1
            except Exception:
                errors += 1
                _logger.exception("Fee backfill failed for transaction %s", txn.stripe_balance_transaction_id)
        message = _("Fee lines created: %(created)s. Errors: %(errors)s. Scanned: %(scanned)s.") % {
            "created": created, "errors": errors, "scanned": len(transactions),
        }
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Stripe fee backfill complete"),
                "message": message,
                "type": "warning" if errors else "success",
                "sticky": bool(errors),
            },
        }

    def action_reset_historical_backfill_cursor(self):
        self.ensure_one()
        self.write({"historical_backfill_cursor_timestamp": False})
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Stripe backfill cursor reset"),
                "message": _("The next historical backfill will start from the latest Stripe cursor again."),
                "type": "success",
                "sticky": False,
            },
        }

    def _import_notification(self, title, stats):
        message = _(
            "Scanned %(scanned)s Stripe records. Imported %(imported)s, skipped %(skipped)s, errors %(errors)s."
        ) % stats
        skip_breakdown = self._format_skip_breakdown(stats)
        if skip_breakdown:
            message = "%s %s" % (message, skip_breakdown)
        if stats.get("backfill_complete"):
            message = "%s %s" % (message, _("Historical backfill has reached the import start date."))
        elif stats.get("next_backfill_cursor"):
            message = "%s %s" % (
                message,
                _("Next backfill continues before %s UTC.") % self._datetime_from_stripe_timestamp(
                    stats["next_backfill_cursor"]
                ),
            )
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
            "name": _("Stripe Gross Transactions"),
            "res_model": "stripe.gross.transaction",
            "view_mode": "list,form",
            "domain": [("connection_id", "=", self.id)],
            "context": {"default_connection_id": self.id},
        }

    @api.model
    def _cron_stripe_run_all(self):
        """Single scheduled entry point: pull new Stripe transactions, then run the
        reconciliation passes (charge->invoice, payout->bank). Each phase is isolated
        so a failure in one does not stop the others."""
        for phase in (
            self._cron_import_stripe_transactions,
            self._cron_auto_match_invoices,
            self._cron_match_stripe_payouts,
        ):
            try:
                phase()
            except Exception:
                _logger.exception("Stripe cron phase %s failed", phase.__name__)

    @api.model
    def _cron_import_stripe_transactions(self):
        connections = self.search(
            [
                ("active", "=", True),
                ("auto_import", "=", True),
                ("secret_key", "!=", False),
                ("journal_id", "!=", False),
            ]
        )
        for connection in connections:
            try:
                connection._import_transactions()
            except Exception:
                _logger.exception("Stripe gross import failed for connection %s", connection.display_name)

    @api.model
    def _cron_auto_match_invoices(self):
        connections = self.search([("active", "=", True), ("journal_id", "!=", False)])
        for connection in connections:
            try:
                connection._auto_match_charge_invoices()
            except Exception:
                _logger.exception("Stripe auto-match failed for connection %s", connection.display_name)

    # Bank journal that receives Stripe payouts as "Direct Credit ... STRIPE ..." lines,
    # and the liquidity-transfer account the payout entries debit while in transit.
    _STRIPE_PAYOUT_BANK_JOURNAL_ID = 124   # CommBank Current Account
    _STRIPE_LIQUIDITY_ACCOUNT_ID = 119     # 11170 Liquidity Transfer
    _STRIPE_PAYOUT_JOURNAL_ID = 18         # Stripe Clearing (payout entries)

    @api.model
    def _cron_match_stripe_payouts(self):
        """Reconcile incoming bank "Direct Credit ... STRIPE ..." lines against the
        open Stripe-payout Liquidity Transfer line of the same amount.

        A Stripe payout imports into the Stripe journal and debits Liquidity Transfer
        (money in transit); when it lands in the bank it arrives as a direct credit.
        This clears the in-transit balance by reconciling the two. Only an unambiguous
        exact-amount 1:1 match is reconciled; anything else is left for review.
        """
        bank_lines = self.env["account.bank.statement.line"].search([
            ("journal_id", "=", self._STRIPE_PAYOUT_BANK_JOURNAL_ID),
            ("is_reconciled", "=", False),
            ("payment_ref", "ilike", "Direct Credit%STRIPE%"),
        ])
        matched = 0
        for line in bank_lines:
            liq = self._find_stripe_payout_liquidity_line(line)
            if not liq:
                continue
            try:
                self._match_stripe_payout_line(line, liq)
                matched += 1
            except Exception:
                _logger.exception("Stripe payout match failed for bank line %s", line.id)
        return matched

    @api.model
    def _find_stripe_payout_liquidity_line(self, line):
        liqs = self.env["account.move.line"].search([
            ("account_id", "=", self._STRIPE_LIQUIDITY_ACCOUNT_ID),
            ("reconciled", "=", False),
            ("company_id", "=", line.company_id.id),
            ("move_id.journal_id", "=", self._STRIPE_PAYOUT_JOURNAL_ID),
        ]).filtered(
            lambda l: "STRIPE PAYOUT" in (l.name or "").upper()
            and round(l.balance, 2) == round(line.amount, 2)
        )
        return liqs if len(liqs) == 1 else self.env["account.move.line"]

    @api.model
    def _match_stripe_payout_line(self, line, liq):
        _liquidity, suspense_lines, _other = line._seek_for_lines()
        if len(suspense_lines) != 1:
            raise UserError(
                _("Bank line %s does not have a single suspense leg.") % line.id
            )
        suspense_lines.with_context(skip_invoice_sync=True).write(
            {"account_id": liq.account_id.id}
        )
        (suspense_lines + liq).reconcile()

    def _auto_match_charge_invoices(self):
        """Reconcile unreconciled Stripe *charge* statement lines straight against
        their customer invoice, marking the invoice paid.

        Match key: the Stripe balance-transaction ``description`` carries
        ``Order <uuid>``; that website order token is stored on the invoice ``ref``.
        Only an unambiguous, exact-to-the-cent single-invoice match is reconciled;
        anything else (partial/multi-charge, ambiguous ref, already-partly-paid) is
        left untouched. No account.payment is created: the funds already sit in the
        Stripe clearing journal, so the line's suspense leg is repointed onto the
        invoice receivable and reconciled against it.

        Returns the number of reconciliations performed.
        """
        self.ensure_one()
        order_re = self._order_uuid_regex()
        charges = self.env["stripe.gross.transaction"].search([
            ("connection_id", "=", self.id),
            ("reporting_category", "=", "charge"),
            ("state", "=", "imported"),
        ])
        matched = 0
        stripe_cache = {}
        for txn in charges:
            line = txn.statement_line_id
            if not line or line.is_reconciled:
                continue
            order_uuid = self._auto_match_order_uuid(txn, order_re, stripe_cache)
            if not order_uuid:
                continue
            found = self._auto_match_find_invoice(line, order_uuid)
            if not found:
                continue
            invoice, recv_line = found
            try:
                self._auto_match_reconcile(line, invoice, recv_line)
                matched += 1
            except Exception:
                _logger.exception(
                    "Stripe auto-match failed for line %s -> %s", line.id, invoice.name
                )
        return matched

    @api.model
    def _order_uuid_regex(self):
        import re

        return re.compile(r"Order\s+([0-9a-fA-F-]{36})")

    def _auto_match_order_uuid(self, txn, order_re, stripe_cache):
        """Resolve the website Order UUID for a Stripe charge.

        Charge-type balance transactions carry it in ``description`` ("Order <uuid>").
        Payment-type transactions (source ``py_``/``ch_``) have no description, so we
        fetch the underlying Stripe charge and read ``metadata.order_name`` /
        ``metadata.publicToken``. Results are cached per run.
        """
        match = order_re.search(txn.description or "")
        if match:
            return match.group(1)

        source = txn.stripe_source_id
        if not source or not str(source).startswith(("py_", "ch_")):
            return False
        if source in stripe_cache:
            return stripe_cache[source]

        order_uuid = False
        try:
            charge = self._stripe_get("/v1/charges/%s" % source, {})
            metadata = charge.get("metadata") or {}
            order_uuid = metadata.get("order_name") or metadata.get("publicToken") or False
        except Exception:
            _logger.warning("Could not resolve order metadata for Stripe source %s", source, exc_info=True)
        stripe_cache[source] = order_uuid
        return order_uuid

    def _auto_match_find_invoice(self, line, order_uuid):
        invoices = self.env["account.move"].search([
            ("move_type", "=", "out_invoice"),
            ("state", "=", "posted"),
            ("company_id", "=", line.company_id.id),
            ("ref", "ilike", order_uuid),
        ])
        # An invoice in ``in_payment`` may be blocked by an inert ``in_process``
        # payment (created but never posted: no journal entry, nothing reconciled).
        # Cancel that shell so the invoice returns to a matchable open state; the
        # imported statement line is the real bank movement.
        candidates = invoices.filtered(
            lambda inv: inv.payment_state in ("not_paid", "partial", "in_payment")
        )
        if len(candidates) != 1:
            return False
        invoice = candidates
        if invoice.payment_state == "in_payment" and not self._release_inert_payment(invoice, line):
            return False
        invoice.invalidate_recordset()
        recv_lines = invoice.line_ids.filtered(
            lambda l: l.account_id.account_type == "asset_receivable" and not l.reconciled
        )
        if len(recv_lines) != 1:
            return False
        recv_line = recv_lines
        if round(abs(recv_line.amount_residual), 2) != round(line.amount, 2):
            return False
        return invoice, recv_line

    def _release_inert_payment(self, invoice, line):
        """Cancel the single inert ``in_process`` payment blocking ``invoice``.

        Only acts when it is unambiguous and safe: exactly one payment in the
        Stripe journal, in ``in_process`` state, with NO journal entry, not
        reconciled/matched, and equal to the statement line amount to the cent.
        Returns True if the invoice was released (or needed no release).
        """
        payments = self.env["account.payment"].search([
            ("journal_id", "=", self.journal_id.id),
            ("company_id", "=", invoice.company_id.id),
            ("state", "=", "in_process"),
            ("partner_id", "=", invoice.partner_id.commercial_partner_id.id),
        ]).filtered(
            lambda p: not p.move_id
            and not p.is_reconciled
            and not p.is_matched
            and round(p.amount, 2) == round(line.amount, 2)
        )
        if len(payments) != 1:
            return False
        payments.action_cancel()
        return True

    def _auto_match_reconcile(self, line, invoice, recv_line):
        partner = invoice.partner_id.commercial_partner_id
        if line.partner_id != partner:
            line.partner_id = partner.id
        _liquidity, suspense_lines, _other = line._seek_for_lines()
        if len(suspense_lines) != 1:
            raise UserError(
                _("Statement line %s does not have a single suspense leg.") % line.id
            )
        suspense_lines.with_context(skip_invoice_sync=True).write({
            "account_id": recv_line.account_id.id,
            "partner_id": partner.id,
        })
        (suspense_lines + recv_line).reconcile()

    def _import_transactions(self, historical_backfill=False):
        self.ensure_one()
        self._validate_import_settings()

        stats = self._new_import_stats(historical_backfill=historical_backfill)
        import_run_at = fields.Datetime.now()
        max_to_scan = max(1, min(self.limit_per_run or 500, 10000))
        import_start_timestamp = self._import_start_timestamp()
        backfill_end_timestamp = False
        params = {
            "limit": min(100, max_to_scan),
            "created[gte]": import_start_timestamp if historical_backfill else self._sync_start_timestamp(),
        }
        if historical_backfill:
            backfill_end_timestamp = self._historical_backfill_end_timestamp(import_start_timestamp)
            if not backfill_end_timestamp:
                stats["backfill_complete"] = True
                self.write(
                    {
                        "last_successful_import_at": import_run_at,
                        "last_import_summary": self._format_import_summary(stats, import_run_at),
                    }
                )
                return stats
            params["created[lte]"] = backfill_end_timestamp

        starting_after = False
        has_more = True
        stripe_has_more_after_last_page = False
        latest_created = self.last_stripe_created_timestamp or 0
        oldest_backfill_created = backfill_end_timestamp

        while has_more and stats["scanned"] < max_to_scan:
            page_params = dict(params)
            page_params["limit"] = min(100, max_to_scan - stats["scanned"])
            if starting_after:
                page_params["starting_after"] = starting_after

            payload = self._stripe_get("/v1/balance_transactions", page_params)
            transactions = payload.get("data", [])
            if not transactions:
                break

            for stripe_transaction in transactions:
                stats["scanned"] += 1
                stripe_created = int(stripe_transaction.get("created") or 0)
                latest_created = max(latest_created, stripe_created)
                if historical_backfill and stripe_created:
                    oldest_backfill_created = min(oldest_backfill_created, stripe_created)

                skip_reason = self._classify_skip_reason(stripe_transaction)
                if skip_reason:
                    self._track_skipped_transaction(stats, stripe_transaction, skip_reason)
                    continue

                existing_transaction = self._find_existing_transaction(stripe_transaction)
                if (
                    existing_transaction
                    and existing_transaction.state == "imported"
                    and self._existing_transaction_lines_are_present(existing_transaction)
                ):
                    self._track_skipped_transaction(stats, stripe_transaction, "already_imported")
                    continue

                try:
                    self._create_imported_transaction(stripe_transaction, existing_transaction)
                    stats["imported"] += 1
                except Exception as exc:
                    stats["errors"] += 1
                    self._create_error_transaction(stripe_transaction, exc)
                    _logger.exception(
                        "Could not import Stripe balance transaction %s",
                        stripe_transaction.get("id"),
                    )

            stripe_has_more_after_last_page = bool(payload.get("has_more"))
            has_more = stripe_has_more_after_last_page and stats["scanned"] < max_to_scan
            starting_after = transactions[-1].get("id")

        values = {
            "last_successful_import_at": import_run_at,
            "last_import_summary": self._format_import_summary(stats, import_run_at),
        }
        if historical_backfill:
            if not stats["scanned"] or not stripe_has_more_after_last_page:
                next_backfill_cursor = import_start_timestamp - 1
                stats["backfill_complete"] = True
            else:
                next_backfill_cursor = max(import_start_timestamp - 1, oldest_backfill_created - 1)
                stats["next_backfill_cursor"] = next_backfill_cursor
            values["historical_backfill_cursor_timestamp"] = next_backfill_cursor
            values["last_import_summary"] = self._format_import_summary(stats, import_run_at)
        else:
            values["last_stripe_created_timestamp"] = latest_created

        self.write(values)
        return stats

    def _new_import_stats(self, historical_backfill=False):
        return {
            "scanned": 0,
            "imported": 0,
            "skipped": 0,
            "errors": 0,
            "skipped_breakdown": {},
            "skipped_examples": [],
            "historical_backfill": historical_backfill,
            "backfill_complete": False,
            "next_backfill_cursor": False,
        }

    def _classify_skip_reason(self, stripe_transaction):
        amount = int(stripe_transaction.get("amount") or 0)
        reporting_category = stripe_transaction.get("reporting_category")
        stripe_type = stripe_transaction.get("type")

        if amount == 0:
            return "zero_amount"
        if self.status_filter == "available" and stripe_transaction.get("status") != "available":
            return "not_available"
        if reporting_category:
            if reporting_category in _IMPORTABLE_STRIPE_REPORTING_CATEGORIES:
                return False
            return "reporting_category:%s" % reporting_category
        if stripe_type in _IMPORTABLE_STRIPE_TYPES:
            return False
        return "type:%s" % (stripe_type or "unknown")

    def _track_skipped_transaction(self, stats, stripe_transaction, reason_code):
        stats["skipped"] += 1
        stats["skipped_breakdown"][reason_code] = stats["skipped_breakdown"].get(reason_code, 0) + 1

        examples = stats["skipped_examples"]
        if len(examples) >= 5:
            return

        examples.append(
            {
                "id": stripe_transaction.get("id") or "unknown",
                "reason": self._skip_reason_label(reason_code),
                "amount": self._format_skip_amount(stripe_transaction),
                "status": stripe_transaction.get("status") or "",
                "description": stripe_transaction.get("description") or "",
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

    def _format_import_summary(self, stats, import_run_at):
        lines = [
            _("Run completed at %(timestamp)s UTC.") % {"timestamp": fields.Datetime.to_string(import_run_at)},
            _(
                "Scanned %(scanned)s Stripe records. Imported %(imported)s, skipped %(skipped)s, errors %(errors)s."
            )
            % stats,
        ]
        if stats.get("historical_backfill"):
            if stats.get("backfill_complete"):
                lines.append(_("Historical backfill has reached the import start date."))
            elif stats.get("next_backfill_cursor"):
                lines.append(
                    _("Next historical backfill continues before %(timestamp)s UTC.")
                    % {"timestamp": self._datetime_from_stripe_timestamp(stats["next_backfill_cursor"])}
                )

        breakdown = stats.get("skipped_breakdown") or {}
        if breakdown:
            lines.append("")
            lines.append(_("Skipped breakdown:"))
            for reason_code, count in sorted(breakdown.items(), key=lambda item: (-item[1], item[0])):
                lines.append("- %s x %s" % (count, self._skip_reason_label(reason_code)))

        examples = stats.get("skipped_examples") or []
        if examples:
            lines.append("")
            lines.append(_("Sample skipped transactions:"))
            for example in examples:
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
        if reason_code == "already_imported":
            return _("Already imported")
        if reason_code == "not_available":
            return _("Not yet available in Stripe")
        if reason_code == "zero_amount":
            return _("Zero amount")

        reason_type, separator, reason_value = reason_code.partition(":")
        label_value = (reason_value or "").replace("_", " ")

        if reason_type == "reporting_category":
            return _("Unsupported reporting category: %(value)s") % {"value": label_value}
        if reason_type == "type":
            return _("Unsupported Stripe transaction type: %(value)s") % {"value": label_value}
        return reason_code.replace("_", " ")

    def _format_skip_amount(self, stripe_transaction):
        currency_code = (stripe_transaction.get("currency") or "").upper()
        currency = self._currency_from_stripe_code(currency_code, raise_if_missing=False)
        if currency:
            amount = self._from_stripe_amount(stripe_transaction.get("amount"), currency)
            precision = int(currency.decimal_places or 2)
            return ("%0." + str(precision) + "f %s") % (amount, currency.name)

        raw_amount = stripe_transaction.get("amount")
        if raw_amount is None:
            return currency_code
        return "%s %s" % (raw_amount, currency_code)

    def _validate_import_settings(self):
        self.ensure_one()
        if not self.secret_key:
            raise UserError(_("Add a Stripe secret key before importing."))
        if not self.journal_id:
            raise UserError(_("Choose the Stripe gross journal before importing."))
        journal_currency = self.journal_id.currency_id or self.company_id.currency_id
        if not journal_currency:
            raise UserError(_("The selected company or journal must have a currency."))
        if self.lookback_hours < 0:
            raise UserError(_("Lookback hours cannot be negative."))
        if self.limit_per_run <= 0:
            raise UserError(_("Limit per run must be greater than zero."))

    def _sync_start_timestamp(self):
        self.ensure_one()
        if self.last_stripe_created_timestamp:
            return max(0, self.last_stripe_created_timestamp - (self.lookback_hours * 3600))
        return self._import_start_timestamp()

    def _import_start_timestamp(self):
        self.ensure_one()
        start_date = self.import_start_date or fields.Date.today()
        start_dt = datetime.combine(fields.Date.to_date(start_date), time.min)
        return calendar.timegm(start_dt.utctimetuple())

    def _historical_backfill_end_timestamp(self, import_start_timestamp):
        self.ensure_one()
        if self.historical_backfill_cursor_timestamp:
            if self.historical_backfill_cursor_timestamp < import_start_timestamp:
                return False
            return self.historical_backfill_cursor_timestamp
        if self.last_stripe_created_timestamp:
            return self.last_stripe_created_timestamp
        return calendar.timegm(datetime.now(timezone.utc).utctimetuple())

    def _stripe_get(self, endpoint, params):
        self.ensure_one()
        headers = {"Authorization": "Bearer %s" % self.secret_key.strip()}
        if self.connected_account_id:
            headers["Stripe-Account"] = self.connected_account_id.strip()

        response = requests.get(
            "https://api.stripe.com%s" % endpoint,
            headers=headers,
            params=params,
            timeout=30,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise UserError(_("Stripe returned a non-JSON response.")) from exc

        if response.status_code >= 400:
            error = payload.get("error") or {}
            message = error.get("message") or response.text
            raise UserError(_("Stripe API error: %s") % message)

        return payload

    def _is_importable_gross_transaction(self, stripe_transaction):
        return not self._classify_skip_reason(stripe_transaction)

    def _find_existing_transaction(self, stripe_transaction):
        return self.env["stripe.gross.transaction"].search(
            [
                ("company_id", "=", self.company_id.id),
                ("stripe_balance_transaction_id", "=", stripe_transaction.get("id")),
            ],
            limit=1,
        )

    def _existing_transaction_lines_are_present(self, existing_transaction):
        if not existing_transaction.statement_line_id:
            return False
        if existing_transaction.fee_amount and not existing_transaction.fee_statement_line_id:
            return False
        return True

    def _create_imported_transaction(self, stripe_transaction, existing_transaction=False):
        self.ensure_one()
        currency = self._currency_from_stripe_code(stripe_transaction.get("currency"))
        journal_currency = self.journal_id.currency_id or self.company_id.currency_id
        if currency != journal_currency:
            raise UserError(
                _(
                    "Stripe transaction %(stripe_id)s is in %(stripe_currency)s, but journal %(journal)s is in %(journal_currency)s."
                )
                % {
                    "stripe_id": stripe_transaction.get("id"),
                    "stripe_currency": currency.name,
                    "journal": self.journal_id.display_name,
                    "journal_currency": journal_currency.name,
                }
            )

        amount = self._from_stripe_amount(stripe_transaction.get("amount"), currency)
        fee_amount = self._from_stripe_amount(stripe_transaction.get("fee"), currency)
        net_amount = self._from_stripe_amount(stripe_transaction.get("net"), currency)
        statement_line = self._find_or_create_statement_line(stripe_transaction, amount)
        fee_statement_line = False
        if fee_amount:
            fee_statement_line = self._find_or_create_fee_statement_line(stripe_transaction, fee_amount)
        values = {
            "connection_id": self.id,
            "company_id": self.company_id.id,
            "journal_id": self.journal_id.id,
            "statement_line_id": statement_line.id,
            "fee_statement_line_id": fee_statement_line.id if fee_statement_line else False,
            "stripe_balance_transaction_id": stripe_transaction.get("id"),
            "stripe_source_id": stripe_transaction.get("source"),
            "stripe_type": stripe_transaction.get("type"),
            "reporting_category": stripe_transaction.get("reporting_category"),
            "stripe_status": stripe_transaction.get("status"),
            "stripe_created_at": self._datetime_from_stripe_timestamp(stripe_transaction.get("created")),
            "date": self._date_from_stripe_timestamp(stripe_transaction.get("created")),
            "description": stripe_transaction.get("description"),
            "currency_id": currency.id,
            "amount": amount,
            "fee_amount": fee_amount,
            "net_amount": net_amount,
            "state": "imported",
            "error_message": False,
            "raw_json": json.dumps(stripe_transaction, sort_keys=True),
        }
        if existing_transaction:
            existing_transaction.write(values)
            return existing_transaction
        return self.env["stripe.gross.transaction"].create(values)

    def _find_or_create_statement_line(self, stripe_transaction, amount):
        StatementLine = self.env["account.bank.statement.line"].with_company(self.company_id)
        unique_import_id = "stripe:%s" % stripe_transaction.get("id")
        if "unique_import_id" in StatementLine._fields:
            statement_line = StatementLine.search(
                [
                    ("journal_id", "=", self.journal_id.id),
                    ("unique_import_id", "=", unique_import_id),
                ],
                limit=1,
            )
            if statement_line:
                return statement_line
        elif "ref" in StatementLine._fields:
            statement_line = StatementLine.search(
                [
                    ("journal_id", "=", self.journal_id.id),
                    ("ref", "=", stripe_transaction.get("id")),
                ],
                limit=1,
            )
            if statement_line:
                return statement_line

        values = {
            "company_id": self.company_id.id,
            "journal_id": self.journal_id.id,
            "date": self._date_from_stripe_timestamp(stripe_transaction.get("created")),
            "amount": amount,
        }
        if "payment_ref" in StatementLine._fields:
            values["payment_ref"] = self._payment_reference(stripe_transaction)
        elif "name" in StatementLine._fields:
            values["name"] = self._payment_reference(stripe_transaction)
        if "ref" in StatementLine._fields:
            values["ref"] = stripe_transaction.get("id")
        if "unique_import_id" in StatementLine._fields:
            values["unique_import_id"] = unique_import_id
        return StatementLine.create(values)

    def _find_or_create_fee_statement_line(self, stripe_transaction, fee_amount):
        fee_journal = self.fee_journal_id or self.journal_id
        StatementLine = self.env["account.bank.statement.line"].with_company(self.company_id)
        unique_import_id = "stripe-fee:%s" % stripe_transaction.get("id")
        if "unique_import_id" in StatementLine._fields:
            statement_line = StatementLine.search(
                [
                    ("journal_id", "=", fee_journal.id),
                    ("unique_import_id", "=", unique_import_id),
                ],
                limit=1,
            )
            if statement_line:
                return statement_line
        elif "ref" in StatementLine._fields:
            fee_ref = "%s-fee" % stripe_transaction.get("id")
            statement_line = StatementLine.search(
                [
                    ("journal_id", "=", fee_journal.id),
                    ("ref", "=", fee_ref),
                ],
                limit=1,
            )
            if statement_line:
                return statement_line

        values = {
            "company_id": self.company_id.id,
            "journal_id": fee_journal.id,
            "date": self._date_from_stripe_timestamp(stripe_transaction.get("created")),
            "amount": -fee_amount,
        }
        fee_ref_label = "Stripe fee | %s" % stripe_transaction.get("id")
        if "payment_ref" in StatementLine._fields:
            values["payment_ref"] = fee_ref_label
        elif "name" in StatementLine._fields:
            values["name"] = fee_ref_label
        if "ref" in StatementLine._fields:
            values["ref"] = "%s-fee" % stripe_transaction.get("id")
        if "unique_import_id" in StatementLine._fields:
            values["unique_import_id"] = unique_import_id
        return StatementLine.create(values)

    def _create_error_transaction(self, stripe_transaction, exception):
        if not stripe_transaction.get("id"):
            return False
        existing_transaction = self._find_existing_transaction(stripe_transaction)

        currency = self._currency_from_stripe_code(stripe_transaction.get("currency"), raise_if_missing=False)
        values = {
            "connection_id": self.id,
            "company_id": self.company_id.id,
            "journal_id": self.journal_id.id,
            "stripe_balance_transaction_id": stripe_transaction.get("id"),
            "stripe_source_id": stripe_transaction.get("source"),
            "stripe_type": stripe_transaction.get("type"),
            "reporting_category": stripe_transaction.get("reporting_category"),
            "stripe_status": stripe_transaction.get("status"),
            "stripe_created_at": self._datetime_from_stripe_timestamp(stripe_transaction.get("created")),
            "date": self._date_from_stripe_timestamp(stripe_transaction.get("created")),
            "description": stripe_transaction.get("description"),
            "currency_id": currency.id if currency else self.company_id.currency_id.id,
            "amount": self._from_stripe_amount(stripe_transaction.get("amount"), currency or self.company_id.currency_id),
            "fee_amount": self._from_stripe_amount(stripe_transaction.get("fee"), currency or self.company_id.currency_id),
            "net_amount": self._from_stripe_amount(stripe_transaction.get("net"), currency or self.company_id.currency_id),
            "state": "error",
            "error_message": str(exception),
            "raw_json": json.dumps(stripe_transaction, sort_keys=True),
        }
        if existing_transaction:
            existing_transaction.write(values)
            return existing_transaction
        return self.env["stripe.gross.transaction"].create(values)

    def _currency_from_stripe_code(self, stripe_code, raise_if_missing=True):
        currency = self.env["res.currency"].search([("name", "=", (stripe_code or "").upper())], limit=1)
        if not currency and raise_if_missing:
            raise UserError(_("No Odoo currency found for Stripe currency %s.") % stripe_code)
        return currency

    def _from_stripe_amount(self, amount, currency):
        amount = int(amount or 0)
        factor = Decimal(10) ** int(currency.decimal_places)
        return float(Decimal(amount) / factor)

    def _date_from_stripe_timestamp(self, timestamp):
        return fields.Date.to_date(self._datetime_from_stripe_timestamp(timestamp))

    def _datetime_from_stripe_timestamp(self, timestamp):
        dt_value = datetime.fromtimestamp(int(timestamp or 0), timezone.utc).replace(tzinfo=None)
        return fields.Datetime.to_string(dt_value)

    def _payment_reference(self, stripe_transaction):
        parts = [
            stripe_transaction.get("description"),
            stripe_transaction.get("source"),
            stripe_transaction.get("id"),
        ]
        return " | ".join([part for part in parts if part])[:200]


class StripeGrossTransaction(models.Model):
    _name = "stripe.gross.transaction"
    _description = "Imported Stripe Gross Transaction"
    _order = "stripe_created_at desc, id desc"
    _rec_name = "stripe_balance_transaction_id"

    _stripe_balance_transaction_company_uniq = models.Constraint(
        "unique(stripe_balance_transaction_id, company_id)",
        "This Stripe balance transaction has already been imported for this company.",
    )

    connection_id = fields.Many2one(
        "stripe.gross.connection",
        required=True,
        ondelete="cascade",
    )
    company_id = fields.Many2one("res.company", required=True)
    journal_id = fields.Many2one("account.journal", required=True)
    statement_line_id = fields.Many2one("account.bank.statement.line", readonly=True)
    fee_statement_line_id = fields.Many2one(
        "account.bank.statement.line", readonly=True, string="Fee Statement Line",
    )
    stripe_balance_transaction_id = fields.Char(required=True, index=True)
    stripe_source_id = fields.Char(index=True)
    stripe_type = fields.Char()
    reporting_category = fields.Char()
    stripe_status = fields.Char()
    stripe_created_at = fields.Datetime()
    date = fields.Date()
    description = fields.Char()
    currency_id = fields.Many2one("res.currency", required=True)
    amount = fields.Monetary(currency_field="currency_id")
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
            "name": _("Stripe Statement Line"),
            "res_model": "account.bank.statement.line",
            "res_id": self.statement_line_id.id,
            "view_mode": "form",
        }

    def action_open_fee_statement_line(self):
        self.ensure_one()
        if not self.fee_statement_line_id:
            raise UserError(_("This transaction does not have an imported fee statement line."))
        return {
            "type": "ir.actions.act_window",
            "name": _("Stripe Fee Statement Line"),
            "res_model": "account.bank.statement.line",
            "res_id": self.fee_statement_line_id.id,
            "view_mode": "form",
        }
