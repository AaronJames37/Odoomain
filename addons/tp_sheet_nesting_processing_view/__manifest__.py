{
    "name": "Sheet Nesting: Processing Panels View",
    "version": "19.0.1.1.0",
    "category": "Manufacturing",
    "summary": "Surfaces all website cut parts whose sale order is currently in website_fulfillment_status='processing' as a single tidy list.",
    "author": "Codex",
    "license": "LGPL-3",
    "depends": ["tp_sheet_nesting", "website_fulfillment_status_sync"],
    "data": [
        "views/tp_nesting_job_website_views.xml",
        "views/tp_web_cut_part_processing_views.xml",
    ],
    "installable": True,
    "application": False,
}
