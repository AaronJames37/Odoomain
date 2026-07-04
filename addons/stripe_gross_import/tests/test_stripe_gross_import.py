from unittest.mock import patch

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.tests import tagged


@tagged("post_install", "-at_install")
class TestStripeGrossImport(AccountTestInvoicingCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.aud_currency = cls.env["res.currency"].search([("name", "=", "AUD")], limit=1)
        cls.bank_journal = cls.company_data["default_journal_bank"].copy(
            {
                "name": "Stripe Gross Test",
                "code": "SGTI",
                "currency_id": cls.aud_currency.id,
            }
        )
        cls.connection = cls.env["stripe.gross.connection"].create(
            {
                "name": "Stripe Gross Import Test",
                "company_id": cls.company_data["company"].id,
                "secret_key": "sk_test_123",
                "journal_id": cls.bank_journal.id,
                "import_start_date": "2026-03-01",
                "status_filter": "all",
                "lookback_hours": 72,
                "limit_per_run": 100,
            }
        )

    def setUp(self):
        super().setUp()
        transactions = self.env["stripe.gross.transaction"].search([("connection_id", "=", self.connection.id)])
        if transactions:
            transactions.unlink()

        StatementLine = self.env["account.bank.statement.line"]
        domain = [("journal_id", "=", self.bank_journal.id)]
        if "unique_import_id" in StatementLine._fields:
            domain.append(("unique_import_id", "like", "stripe:%"))
        elif "ref" in StatementLine._fields:
            domain.append(("ref", "like", "txn_%"))
        statement_lines = StatementLine.search(domain)
        if statement_lines:
            statement_lines.unlink()

        self.connection.write(
            {
                "last_stripe_created_timestamp": False,
                "historical_backfill_cursor_timestamp": False,
                "last_successful_import_at": False,
                "last_import_summary": False,
                "import_start_date": "2026-03-01",
                "lookback_hours": 72,
                "limit_per_run": 100,
                "status_filter": "all",
            }
        )

    def _stripe_transaction(
        self,
        transaction_id,
        amount,
        fee,
        net,
        reporting_category,
        stripe_type=None,
        status="available",
        created=1772323200,
        description="Stripe import test",
        source=None,
    ):
        return {
            "id": transaction_id,
            "amount": amount,
            "fee": fee,
            "net": net,
            "currency": "aud",
            "status": status,
            "created": created,
            "description": description,
            "source": source or transaction_id.replace("txn_", "src_"),
            "type": stripe_type or reporting_category,
            "reporting_category": reporting_category,
        }

    def test_classify_refunds_and_payouts_as_importable(self):
        self.assertFalse(
            self.connection._classify_skip_reason(
                self._stripe_transaction("txn_charge", 10000, -175, 9825, "charge")
            )
        )
        self.assertFalse(
            self.connection._classify_skip_reason(
                self._stripe_transaction("txn_refund", -5000, 0, -5000, "refund")
            )
        )
        self.assertFalse(
            self.connection._classify_skip_reason(
                self._stripe_transaction("txn_payout", -20000, 0, -20000, "payout")
            )
        )
        self.assertEqual(
            self.connection._classify_skip_reason(
                self._stripe_transaction("txn_zero", 0, 0, 0, "charge")
            ),
            "zero_amount",
        )

    def test_import_refunds_and_payouts(self):
        charge = self._stripe_transaction(
            "txn_charge_1",
            12000,
            -360,
            11640,
            "charge",
            created=1772409600,
            description="Charge payment",
        )
        refund = self._stripe_transaction(
            "txn_refund_1",
            -2500,
            0,
            -2500,
            "refund",
            created=1772413200,
            description="Customer refund",
        )
        payout = self._stripe_transaction(
            "txn_payout_1",
            -9000,
            0,
            -9000,
            "payout",
            created=1772416800,
            description="Stripe payout",
        )

        with patch.object(
            type(self.connection),
            "_stripe_get",
            side_effect=[
                {
                    "data": [charge, refund, payout],
                    "has_more": False,
                },
                {
                    "data": [charge, refund, payout],
                    "has_more": False,
                },
            ],
        ):
            first_stats = self.connection._import_transactions()
            second_stats = self.connection._import_transactions()

        self.assertEqual(first_stats["imported"], 3)
        self.assertEqual(first_stats["skipped"], 0)
        self.assertEqual(first_stats["errors"], 0)

        transactions = self.env["stripe.gross.transaction"].search(
            [("connection_id", "=", self.connection.id)],
            order="stripe_balance_transaction_id asc",
        )
        self.assertEqual(len(transactions), 3)
        self.assertRecordValues(
            transactions,
            [
                {
                    "stripe_balance_transaction_id": "txn_charge_1",
                    "reporting_category": "charge",
                    "amount": 120.0,
                    "fee_amount": -3.6,
                    "net_amount": 116.4,
                },
                {
                    "stripe_balance_transaction_id": "txn_payout_1",
                    "reporting_category": "payout",
                    "amount": -90.0,
                    "fee_amount": 0.0,
                    "net_amount": -90.0,
                },
                {
                    "stripe_balance_transaction_id": "txn_refund_1",
                    "reporting_category": "refund",
                    "amount": -25.0,
                    "fee_amount": 0.0,
                    "net_amount": -25.0,
                },
            ],
        )
        self.assertEqual(transactions.filtered(lambda t: t.reporting_category == "refund").statement_line_id.amount, -25.0)
        self.assertEqual(transactions.filtered(lambda t: t.reporting_category == "payout").statement_line_id.amount, -90.0)

        self.assertEqual(second_stats["imported"], 0)
        self.assertEqual(second_stats["skipped"], 3)
        self.assertIn("Already imported", self.connection.last_import_summary)

    def test_existing_tracker_without_statement_line_is_reimported(self):
        charge = self._stripe_transaction(
            "txn_charge_reimport",
            12000,
            360,
            11640,
            "charge",
            created=1772409600,
            description="Charge payment",
        )

        with patch.object(
            type(self.connection),
            "_stripe_get",
            side_effect=[
                {
                    "data": [charge],
                    "has_more": False,
                },
                {
                    "data": [charge],
                    "has_more": False,
                },
            ],
        ):
            first_stats = self.connection._import_transactions()
            transaction = self.env["stripe.gross.transaction"].search(
                [("connection_id", "=", self.connection.id)],
                limit=1,
            )
            old_statement_line = transaction.statement_line_id
            old_statement_line.unlink()
            transaction.invalidate_recordset(["statement_line_id"])
            second_stats = self.connection._import_transactions()

        transaction.invalidate_recordset(["statement_line_id"])
        self.assertEqual(first_stats["imported"], 1)
        self.assertEqual(second_stats["imported"], 1)
        self.assertEqual(second_stats["skipped"], 0)
        self.assertTrue(transaction.statement_line_id)
        self.assertNotEqual(transaction.statement_line_id.id, old_statement_line.id)

    def test_historical_backfill_moves_backward_without_touching_forward_cursor(self):
        forward_cursor = 1779026606
        newest_backfill = self._stripe_transaction(
            "txn_backfill_newer",
            -7000,
            0,
            -7000,
            "payout",
            created=1772500000,
            description="Older payout",
        )
        oldest_backfill = self._stripe_transaction(
            "txn_backfill_older",
            -1500,
            0,
            -1500,
            "refund",
            created=1772400000,
            description="Older refund",
        )
        captured_params = []

        def fake_stripe_get(connection, endpoint, params):
            captured_params.append(dict(params))
            return {
                "data": [newest_backfill, oldest_backfill],
                "has_more": True,
            }

        self.connection.write({"last_stripe_created_timestamp": forward_cursor, "limit_per_run": 2})
        with patch.object(type(self.connection), "_stripe_get", fake_stripe_get):
            stats = self.connection._import_transactions(historical_backfill=True)

        self.assertEqual(stats["imported"], 2)
        self.assertEqual(stats["skipped"], 0)
        self.assertEqual(captured_params[0]["created[lte]"], forward_cursor)
        self.assertEqual(captured_params[0]["created[gte]"], self.connection._import_start_timestamp())
        self.assertEqual(self.connection.last_stripe_created_timestamp, forward_cursor)
        self.assertEqual(self.connection.historical_backfill_cursor_timestamp, oldest_backfill["created"] - 1)
        self.assertIn("Next historical backfill continues before", self.connection.last_import_summary)

    def test_historical_backfill_marks_complete_at_import_start(self):
        backfill_transaction = self._stripe_transaction(
            "txn_backfill_done",
            -7000,
            0,
            -7000,
            "payout",
            created=1772400000,
            description="Final older payout",
        )

        with patch.object(
            type(self.connection),
            "_stripe_get",
            return_value={
                "data": [backfill_transaction],
                "has_more": False,
            },
        ):
            stats = self.connection._import_transactions(historical_backfill=True)

        self.assertTrue(stats["backfill_complete"])
        self.assertLess(self.connection.historical_backfill_cursor_timestamp, self.connection._import_start_timestamp())
        self.assertIn("Historical backfill has reached the import start date", self.connection.last_import_summary)
