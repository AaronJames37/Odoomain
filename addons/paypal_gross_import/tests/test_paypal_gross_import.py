from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.tests import tagged


class MockResponse:
    def __init__(self, status_code, payload, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or ""

    def json(self):
        return self._payload


@tagged("post_install", "-at_install")
class TestPaypalGrossImport(AccountTestInvoicingCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.aud_currency = cls.env["res.currency"].search([("name", "=", "AUD")], limit=1)
        cls.bank_journal = cls.company_data["default_journal_bank"].copy(
            {
                "name": "PayPal Gross Test",
                "code": "PPGT",
                "currency_id": cls.aud_currency.id,
            }
        )
        cls.connection = cls.env["paypal.gross.connection"].create(
            {
                "name": "PayPal Gross Import Test",
                "company_id": cls.company_data["company"].id,
                "paypal_client_id": "client-id",
                "paypal_client_secret": "client-secret",
                "environment": "sandbox",
                "journal_id": cls.bank_journal.id,
                "import_start_date": date(2026, 3, 1),
                "lookback_hours": 24,
                "limit_per_run": 100,
            }
        )

    def setUp(self):
        super().setUp()
        transactions = self.env["paypal.gross.transaction"].search([("connection_id", "=", self.connection.id)])
        if transactions:
            transactions.unlink()

        StatementLine = self.env["account.bank.statement.line"]
        domain = [("journal_id", "=", self.bank_journal.id)]
        if "unique_import_id" in StatementLine._fields:
            domain.append(("unique_import_id", "like", "paypal:%"))
        elif "ref" in StatementLine._fields:
            domain.append(("ref", "like", "paypal:%"))
        statement_lines = StatementLine.search(domain)
        if statement_lines:
            statement_lines.unlink()

        self.connection.write(
            {
                "paypal_access_token": False,
                "paypal_access_token_expiry": False,
                "last_paypal_updated_at": False,
                "last_successful_import_at": False,
                "last_import_summary": False,
                "import_start_date": date(2026, 3, 1),
                "lookback_hours": 24,
                "limit_per_run": 100,
            }
        )

    def _transaction_detail(
        self,
        transaction_id,
        amount,
        fee="-0.30",
        event_code="T0006",
        status="S",
        reference_id="REF-1",
        reference_type="TXN",
        initiation="2026-03-01T10:00:00Z",
        updated="2026-03-01T10:05:00Z",
        subject="Gross import test",
        currency="AUD",
    ):
        return {
            "transaction_info": {
                "transaction_id": transaction_id,
                "paypal_reference_id": reference_id,
                "paypal_reference_id_type": reference_type,
                "transaction_event_code": event_code,
                "transaction_initiation_date": initiation,
                "transaction_updated_date": updated,
                "transaction_amount": {
                    "currency_code": currency,
                    "value": str(amount),
                },
                "fee_amount": {
                    "currency_code": currency,
                    "value": str(fee),
                },
                "transaction_status": status,
                "transaction_subject": subject,
            },
            "payer_info": {
                "email_address": "buyer@example.com",
                "payer_name": {
                    "alternate_full_name": "Buyer Example",
                },
            },
        }

    def test_fetch_access_token_and_401_retry(self):
        now_utc = datetime(2026, 3, 2, 0, 0, tzinfo=timezone.utc)
        responses = [
            MockResponse(200, {"access_token": "token-1", "expires_in": 3600}),
            MockResponse(401, {"message": "Expired token"}),
            MockResponse(200, {"access_token": "token-2", "expires_in": 3600}),
            MockResponse(200, {"transaction_details": [], "total_pages": 1}),
        ]

        with patch(
            "odoo.addons.paypal_gross_import.models.paypal_gross_connection.requests.request",
            side_effect=responses,
        ) as request_mock, patch(
            "odoo.addons.paypal_gross_import.models.paypal_gross_connection.PaypalGrossConnection._get_now_utc",
            return_value=now_utc,
        ):
            payload = self.connection._paypal_request_json(
                "GET",
                "/v1/reporting/transactions",
                params={"start_date": "2026-03-01T00:00:00Z", "end_date": "2026-03-02T00:00:00Z"},
            )

        self.assertEqual(payload["transaction_details"], [])
        self.assertEqual(self.connection.paypal_access_token, "token-2")
        self.assertEqual(request_mock.call_count, 4)

    def test_credential_change_clears_cached_access_token(self):
        self.connection.write(
            {
                "paypal_access_token": "old-token",
                "paypal_access_token_expiry": datetime(2026, 3, 2, 1, 0),
            }
        )

        self.connection.write({"paypal_client_id": "new-client-id"})

        self.assertFalse(self.connection.paypal_access_token)
        self.assertFalse(self.connection.paypal_access_token_expiry)

    def test_test_connection_forces_fresh_access_token(self):
        now_utc = datetime(2026, 3, 2, 0, 0, tzinfo=timezone.utc)
        self.connection.write(
            {
                "paypal_access_token": "old-token",
                "paypal_access_token_expiry": datetime(2026, 3, 2, 1, 0),
            }
        )
        responses = [
            MockResponse(200, {"access_token": "fresh-token", "expires_in": 3600}),
            MockResponse(200, {"transaction_details": [], "total_pages": 1}),
        ]

        with patch(
            "odoo.addons.paypal_gross_import.models.paypal_gross_connection.requests.request",
            side_effect=responses,
        ) as request_mock, patch(
            "odoo.addons.paypal_gross_import.models.paypal_gross_connection.PaypalGrossConnection._get_now_utc",
            return_value=now_utc,
        ):
            self.connection.action_test_connection()

        self.assertEqual(request_mock.call_count, 2)
        self.assertEqual(request_mock.call_args_list[1].kwargs["headers"]["Authorization"], "Bearer fresh-token")

    def test_transaction_search_permission_error_is_actionable(self):
        response = MockResponse(
            403,
            {
                "name": "NOT_AUTHORIZED",
                "message": "Authorization failed due to insufficient permissions.",
            },
        )

        message = self.connection._paypal_error_message(
            response,
            response.json(),
            "/v1/reporting/transactions",
        )

        self.assertIn("Transaction Search", message)
        self.assertIn("PayPal Developer Dashboard", message)

    def test_iter_query_windows_splits_large_backfill(self):
        windows = list(
            self.connection._iter_query_windows(
                datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
                datetime(2026, 3, 5, 0, 0, tzinfo=timezone.utc),
            )
        )

        self.assertGreater(len(windows), 1)
        for start_utc, end_utc in windows:
            self.assertLessEqual(end_utc - start_utc, timedelta(days=31))

    def test_list_transactions_window_paginates(self):
        now_utc = datetime(2026, 3, 2, 0, 0, tzinfo=timezone.utc)
        page_one = self._transaction_detail("PAYPAL-TXN-1", "10.00")
        page_two = self._transaction_detail("PAYPAL-TXN-2", "20.00", reference_id="REF-2")
        responses = [
            MockResponse(200, {"access_token": "token-1", "expires_in": 3600}),
            MockResponse(200, {"transaction_details": [page_one], "total_pages": 2}),
            MockResponse(200, {"transaction_details": [page_two], "total_pages": 2}),
        ]

        with patch(
            "odoo.addons.paypal_gross_import.models.paypal_gross_connection.requests.request",
            side_effect=responses,
        ) as request_mock, patch(
            "odoo.addons.paypal_gross_import.models.paypal_gross_connection.PaypalGrossConnection._get_now_utc",
            return_value=now_utc,
        ):
            details = self.connection._paypal_list_transactions_window(
                datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc),
                datetime(2026, 3, 2, 0, 0, tzinfo=timezone.utc),
                2,
            )

        self.assertEqual(len(details), 2)
        first_get_params = request_mock.call_args_list[1].kwargs["params"]
        second_get_params = request_mock.call_args_list[2].kwargs["params"]
        self.assertEqual(first_get_params["page"], 1)
        self.assertEqual(second_get_params["page"], 2)

    def test_import_creates_statement_lines_and_skips_unchanged_records(self):
        now_utc = datetime(2026, 3, 2, 0, 0, tzinfo=timezone.utc)
        positive = self._transaction_detail("PAYPAL-TXN-10", "120.00", fee="-3.60", subject="Sale receipt")
        negative = self._transaction_detail(
            "PAYPAL-TXN-11",
            "-15.00",
            fee="0.00",
            event_code="T1107",
            reference_id="REF-11",
            subject="Refund",
        )
        wrong_currency = self._transaction_detail(
            "PAYPAL-TXN-12",
            "50.00",
            reference_id="REF-12",
            currency="USD",
            subject="Wrong currency",
        )

        with patch(
            "odoo.addons.paypal_gross_import.models.paypal_gross_connection.PaypalGrossConnection._get_now_utc",
            return_value=now_utc,
        ), patch.object(
            type(self.connection),
            "_paypal_list_transactions_window",
            return_value=[positive, negative, wrong_currency],
        ):
            first_stats = self.connection._import_transactions()
            second_stats = self.connection._import_transactions()

        self.assertEqual(first_stats["imported"], 2)
        self.assertEqual(first_stats["updated"], 0)
        self.assertEqual(first_stats["skipped"], 1)
        self.assertEqual(first_stats["errors"], 0)

        transactions = self.env["paypal.gross.transaction"].search(
            [("connection_id", "=", self.connection.id)],
            order="paypal_transaction_id asc",
        )
        self.assertEqual(len(transactions), 2)
        self.assertRecordValues(
            transactions,
            [
                {
                    "paypal_transaction_id": "PAYPAL-TXN-10",
                    "gross_amount": 120.0,
                    "fee_amount": -3.6,
                    "net_amount": 116.4,
                    "paypal_transaction_status": "S",
                },
                {
                    "paypal_transaction_id": "PAYPAL-TXN-11",
                    "gross_amount": -15.0,
                    "fee_amount": 0.0,
                    "net_amount": -15.0,
                    "paypal_transaction_status": "S",
                },
            ],
        )
        self.assertEqual(transactions[0].statement_line_id.amount, 120.0)
        self.assertEqual(transactions[1].statement_line_id.amount, -15.0)
        self.assertIn("Unexpected currency: USD", self.connection.last_import_summary)

        self.assertEqual(second_stats["imported"], 0)
        self.assertEqual(second_stats["updated"], 0)
        self.assertEqual(second_stats["skipped"], 3)
        self.assertEqual(second_stats["errors"], 0)
        self.assertIn("Already up to date", self.connection.last_import_summary)

    def test_fetch_from_start_date_ignores_forward_cursor(self):
        now_utc = datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc)
        captured_windows = []

        def fake_list_transactions(connection, start_utc, end_utc, remaining):
            captured_windows.append((start_utc, end_utc, remaining))
            return []

        self.connection.write({"last_paypal_updated_at": datetime(2026, 4, 25, 12, 0)})
        with patch(
            "odoo.addons.paypal_gross_import.models.paypal_gross_connection.PaypalGrossConnection._get_now_utc",
            return_value=now_utc,
        ), patch.object(type(self.connection), "_paypal_list_transactions_window", fake_list_transactions):
            stats = self.connection._import_transactions(force_from_start=True)

        self.assertEqual(stats["scanned"], 0)
        self.assertEqual(captured_windows[0][0], datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(self.connection.last_paypal_updated_at, datetime(2026, 4, 25, 12, 0))

    def test_existing_tracker_without_statement_line_is_relinked(self):
        now_utc = datetime(2026, 3, 2, 0, 0, tzinfo=timezone.utc)
        detail = self._transaction_detail("PAYPAL-TXN-13", "42.00", fee="-1.20", subject="Reimported sale")

        with patch(
            "odoo.addons.paypal_gross_import.models.paypal_gross_connection.PaypalGrossConnection._get_now_utc",
            return_value=now_utc,
        ), patch.object(type(self.connection), "_paypal_list_transactions_window", return_value=[detail]):
            first_stats = self.connection._import_transactions()
            transaction = self.env["paypal.gross.transaction"].search(
                [("connection_id", "=", self.connection.id)],
                limit=1,
            )
            old_statement_line = transaction.statement_line_id
            old_statement_line.unlink()
            transaction.invalidate_recordset(["statement_line_id"])
            second_stats = self.connection._import_transactions(force_from_start=True)

        transaction.invalidate_recordset(["statement_line_id"])
        self.assertEqual(first_stats["imported"], 1)
        self.assertEqual(second_stats["updated"], 1)
        self.assertEqual(second_stats["skipped"], 0)
        self.assertTrue(transaction.statement_line_id)
        self.assertNotEqual(transaction.statement_line_id.id, old_statement_line.id)

    def test_import_updates_existing_transaction_when_details_change(self):
        initial_now = datetime(2026, 3, 2, 0, 0, tzinfo=timezone.utc)
        updated_now = datetime(2026, 3, 3, 0, 0, tzinfo=timezone.utc)
        initial_detail = self._transaction_detail(
            "PAYPAL-TXN-20",
            "80.00",
            fee="-2.40",
            status="P",
            reference_id="REF-20",
            subject="Pending payment",
            updated="2026-03-02T00:00:00Z",
        )
        changed_detail = self._transaction_detail(
            "PAYPAL-TXN-20",
            "80.00",
            fee="-2.40",
            status="S",
            reference_id="REF-20",
            subject="Settled payment",
            updated="2026-03-03T00:00:00Z",
        )

        with patch(
            "odoo.addons.paypal_gross_import.models.paypal_gross_connection.PaypalGrossConnection._get_now_utc",
            return_value=initial_now,
        ), patch.object(
            type(self.connection),
            "_paypal_list_transactions_window",
            return_value=[initial_detail],
        ):
            first_stats = self.connection._import_transactions()

        with patch(
            "odoo.addons.paypal_gross_import.models.paypal_gross_connection.PaypalGrossConnection._get_now_utc",
            return_value=updated_now,
        ), patch.object(
            type(self.connection),
            "_paypal_list_transactions_window",
            return_value=[changed_detail],
        ):
            second_stats = self.connection._import_transactions()

        transaction = self.env["paypal.gross.transaction"].search(
            [
                ("connection_id", "=", self.connection.id),
                ("paypal_transaction_id", "=", "PAYPAL-TXN-20"),
            ],
            limit=1,
        )
        self.assertEqual(first_stats["imported"], 1)
        self.assertEqual(second_stats["updated"], 1)
        self.assertEqual(transaction.paypal_transaction_status, "S")
        self.assertEqual(transaction.description, "Settled payment")
        self.assertEqual(transaction.statement_line_id.payment_ref, "Settled payment | T0006 | PAYPAL-TXN-20")
