{
    "name": "Sheet Nesting: Sandbox Preview",
    "version": "19.0.1.1.0",
    "category": "Manufacturing",
    "summary": "Run the nesting engine against a Nesting Job's panels without creating an MO. Ephemeral; nothing persists.",
    "author": "Codex",
    "license": "LGPL-3",
    "depends": ["tp_sheet_nesting", "tp_sheet_nesting_processing_view"],
    "data": [
        "security/ir.model.access.csv",
        "views/tp_nesting_sandbox_views.xml",
    ],
    "installable": True,
    "application": False,
}
