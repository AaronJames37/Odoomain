import logging

_logger = logging.getLogger(__name__)


def post_init_hook(env):
    """Disable legacy nesting UI records previously shipped in tp_offcuts_nesting."""
    legacy_xmlids = [
        "tp_offcuts_nesting.menu_tp_nesting_source_map",
        "tp_offcuts_nesting.menu_tp_nesting_runs",
        "tp_offcuts_nesting.menu_tp_nesting_jobs",
        "tp_offcuts_nesting.menu_tp_nesting_allocations",
        "tp_offcuts_nesting.menu_tp_nesting_benchmarks",
        "tp_offcuts_nesting.action_tp_nesting_source_map",
        "tp_offcuts_nesting.action_tp_nesting_run",
        "tp_offcuts_nesting.action_tp_nesting_job",
        "tp_offcuts_nesting.action_tp_nesting_allocation",
        "tp_offcuts_nesting.action_tp_nesting_benchmark",
        "tp_offcuts_nesting.view_tp_nesting_source_map_tree",
        "tp_offcuts_nesting.view_tp_nesting_source_map_form",
        "tp_offcuts_nesting.view_tp_nesting_source_map_search",
        "tp_offcuts_nesting.view_tp_nesting_run_tree",
        "tp_offcuts_nesting.view_tp_nesting_run_form",
        "tp_offcuts_nesting.view_tp_nesting_run_search",
        "tp_offcuts_nesting.view_tp_nesting_job_tree",
        "tp_offcuts_nesting.view_tp_nesting_job_form",
        "tp_offcuts_nesting.view_tp_nesting_job_search",
        "tp_offcuts_nesting.view_tp_nesting_allocation_tree",
        "tp_offcuts_nesting.view_tp_nesting_allocation_form",
        "tp_offcuts_nesting.view_tp_nesting_allocation_search",
        "tp_offcuts_nesting.view_tp_nesting_benchmark_tree",
        "tp_offcuts_nesting.view_tp_nesting_benchmark_form",
        "tp_offcuts_nesting.view_order_line_tree_tp_dimensions",
        "tp_offcuts_nesting.sale_order_line_form_readonly_tp_dimensions",
        "tp_offcuts_nesting.view_sales_order_line_filter_tp_dimensions",
        "tp_offcuts_nesting.view_order_form_tp_dimensions",
        "tp_offcuts_nesting.mrp_production_form_view_tp_cutlist",
    ]

    disabled = 0
    for xmlid in legacy_xmlids:
        record = env.ref(xmlid, raise_if_not_found=False)
        if not record:
            continue
        if "active" in record._fields:
            record.sudo().write({"active": False})
            disabled += 1
    _logger.info("tp_sheet_nesting post_init: disabled %s legacy tp_offcuts_nesting UI records", disabled)