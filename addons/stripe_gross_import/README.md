# Stripe Gross Transaction Import

Ultra-light Odoo addon for importing gross Stripe charge/payment balance transactions into a mapped Odoo bank/cash journal as bank statement lines.

## Why API polling

This module uses Stripe's Balance Transactions API instead of webhooks. Polling is simpler for this workflow because Odoo does not need a public webhook endpoint, missed runs are recoverable, and imported records are idempotent by Stripe balance transaction ID.

## Accounting shape

The module imports gross charge/payment balance transactions, refunds, and payouts.

Example Stripe charge:

- Gross charge: 100.00
- Stripe fee: 3.00
- Net payout impact: 97.00

This module imports the 100.00 gross amount into your selected Stripe journal. Refunds and payouts are imported as negative lines so historical Stripe activity can be brought into the same journal.

## Setup

1. Copy `stripe_gross_import` into your Odoo addons path.
2. Restart Odoo and update the apps list.
3. Install **Stripe Gross Transaction Import**.
4. Open **Stripe Gross Import > Connections**.
5. Create a connection with:
   - Stripe secret key
   - Stripe gross bank/cash journal mapping
   - Import start date
   - Optional Stripe connected account ID for Connect setups
6. Click **Test Connection**.
7. Click **Fetch New Transactions** to manually pull recent Stripe records that are not already imported.
8. Click **Backfill Older Transactions** to walk backward through older Stripe records from the current Stripe cursor toward the import start date.
9. Or let the scheduled action run every 15 minutes.

## Stripe key permissions

Use a restricted live key where possible. This module only needs read access to Balance Transactions for the basic import.

If you later want invoice/order matching from Stripe metadata, also allow read access to Charges and Payment Intents.

## Notes

- Transactions are deduplicated by Stripe balance transaction ID per Odoo company.
- The cron overlaps previous syncs by the configured lookback period, then skips duplicates.
- Manual fetch uses the same forward cursor as the cron, so it only scans the new Stripe window after the first import.
- Historical backfill uses a separate cursor and can be run repeatedly to move backward through older records without disrupting the cron cursor. If it stops before the date you need, run it again or raise the limit per run temporarily.
- Currency must match the selected Odoo journal currency.
- The journal mapping is configurable on the connection and allows any bank or cash journal in the selected company.
- Disputes, transfers, and standalone Stripe fee rows are intentionally skipped.
