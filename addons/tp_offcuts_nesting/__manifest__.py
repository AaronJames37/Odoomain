{
    "name": "Offcut Manager",
    "summary": "Offcut inventory, valuation, and waste tracking",
    "version": "19.0.6.0.0",
    "author": "TP",
    "license": "LGPL-3",
    "depends": ["base", "mrp", "stock", "account"],
    "data": [
        "data/ir_cron_data.xml",
        "data/tp_offcut_dashboard_data.xml",
        "security/ir.model.access.csv",
        "views/product_template_views.xml",
        "views/stock_lot_views.xml",
        "views/res_config_settings_views.xml",
        "views/tp_offcut_operational_dashboard_views.xml",
        "views/tp_offcut_bin_rule_views.xml",
        "views/tp_offcut_views.xml",
        "views/tp_offcut_valuation_event_views.xml",
        "views/tp_offcut_waste_views.xml",
        "views/tp_offcuts_placeholder_views.xml",
        "views/tp_offcuts_settings_actions.xml",
        "views/tp_offcuts_menus.xml",
        "views/tp_offcuts_menu_icons.xml"
    ],
    "assets": {
        "web.assets_backend": [
            "tp_offcuts_nesting/static/src/scss/tp_offcut_dashboard.scss",
        ],
    },
    "installable": True,
    "application": False,
    "post_init_hook": "post_init_hook"
}
