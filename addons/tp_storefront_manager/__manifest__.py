{
    "name": "Storefront Manager",
    "summary": "Manage headless storefront settings and appearance",
    "version": "19.0.1.0.0",
    "author": "TP",
    "license": "LGPL-3",
    "depends": ["base", "product", "tp_offcuts_nesting"],
    "data": [
        "security/ir.model.access.csv",
        "data/tp_storefront_settings_data.xml",
        "views/product_template_views.xml",
        "views/tp_storefront_settings_views.xml"
    ],
    "installable": True,
    "application": True
}
