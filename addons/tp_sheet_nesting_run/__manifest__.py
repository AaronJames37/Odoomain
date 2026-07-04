{
    "name": "Sheet Nesting: Formal Run",
    "version": "19.0.1.4.0",
    "category": "Manufacturing",
    "summary": (
        "Creates material-batched nesting runs from active website panels and "
        "lets operators create sheet-consuming MOs from each run."
    ),
    "author": "Codex",
    "license": "LGPL-3",
    "depends": [
        "web",
        "bus",
        "tp_sheet_nesting",
        "tp_sheet_nesting_processing_view",
        "tp_sheet_nesting_sandbox",
        "mrp",
        "stock",
    ],
    "data": [
        "security/ir.model.access.csv",
        "views/tp_nesting_run_wizard_views.xml",
        "views/tp_nesting_run_views.xml",
        "views/tp_nesting_job_views.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "tp_sheet_nesting_run/static/src/nesting_app/nesting_app.js",
            "tp_sheet_nesting_run/static/src/nesting_app/nesting_app.xml",
            "tp_sheet_nesting_run/static/src/nesting_app/nesting_app.scss",
        ],
    },
    "installable": True,
    "application": False,
}
