{
    "name": "Stripe Gross Transaction Import",
    "version": "1.4.0",
    "category": "Accounting",
    "summary": "Import gross Stripe charge balance transactions into an Odoo journal.",
    "author": "Codex",
    "license": "LGPL-3",
    "depends": ["account"],
    "data": [
        "security/ir.model.access.csv",
        "data/ir_cron.xml",
        "views/stripe_gross_views.xml",
    ],
    "installable": True,
    "application": False,
}
