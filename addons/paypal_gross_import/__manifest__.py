{
    "name": "PayPal Gross Transaction Import",
    "version": "19.0.1.1.0",
    "category": "Accounting",
    "summary": "Import gross PayPal balance-affecting transactions into an Odoo journal.",
    "author": "Codex",
    "license": "LGPL-3",
    "depends": ["account"],
    "data": [
        "security/ir.model.access.csv",
        "data/ir_cron.xml",
        "views/paypal_gross_views.xml",
    ],
    "installable": True,
    "application": False,
}
