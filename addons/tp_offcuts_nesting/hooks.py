import logging

_logger = logging.getLogger(__name__)


def post_init_hook(env):
    """Log module install completion for operational traceability."""
    dashboard_model = env["tp.offcut.operational.dashboard"].sudo()
    for company in env["res.company"].sudo().search([]):
        dashboard_model.search([("company_id", "=", company.id)], limit=1) or dashboard_model.create(
            {"company_id": company.id}
        )
    _logger.info("tp_offcuts_nesting installed successfully")
