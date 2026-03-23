from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"
    tp_nesting_engine_mode = fields.Selection(
        related="company_id.tp_nesting_engine_mode",
        readonly=False,
    )
    tp_nesting_sheet_size_candidate_limit = fields.Integer(
        related="company_id.tp_nesting_sheet_size_candidate_limit",
        readonly=False,
    )
    tp_nesting_beam_width = fields.Integer(
        related="company_id.tp_nesting_beam_width",
        readonly=False,
    )
    tp_nesting_branch_cap = fields.Integer(
        related="company_id.tp_nesting_branch_cap",
        readonly=False,
    )
    tp_nesting_max_piece_count = fields.Integer(
        related="company_id.tp_nesting_max_piece_count",
        readonly=False,
    )
    tp_nesting_beam_width_cap = fields.Integer(
        related="company_id.tp_nesting_beam_width_cap",
        readonly=False,
    )
    tp_nesting_timeout_cap_ms = fields.Integer(
        related="company_id.tp_nesting_timeout_cap_ms",
        readonly=False,
    )
    tp_nesting_policy_preset = fields.Selection(
        related="company_id.tp_nesting_policy_preset",
        readonly=False,
    )
    tp_nesting_waste_priority = fields.Float(
        related="company_id.tp_nesting_waste_priority",
        readonly=False,
    )
    tp_nesting_offcut_reuse_priority = fields.Float(
        related="company_id.tp_nesting_offcut_reuse_priority",
        readonly=False,
    )
    tp_nesting_sheet_count_penalty = fields.Float(
        related="company_id.tp_nesting_sheet_count_penalty",
        readonly=False,
    )
    tp_nesting_cost_sensitivity = fields.Float(
        related="company_id.tp_nesting_cost_sensitivity",
        readonly=False,
    )
    tp_nesting_debug_enabled = fields.Boolean(
        related="company_id.tp_nesting_debug_enabled",
        readonly=False,
    )
    tp_nesting_exact_refinement_enabled = fields.Boolean(
        related="company_id.tp_nesting_exact_refinement_enabled",
        readonly=False,
    )
    tp_nesting_exact_refinement_cut_threshold = fields.Integer(
        related="company_id.tp_nesting_exact_refinement_cut_threshold",
        readonly=False,
    )
    tp_nesting_exact_refinement_timeout_ms = fields.Integer(
        related="company_id.tp_nesting_exact_refinement_timeout_ms",
        readonly=False,
    )
    tp_nesting_kernel_name = fields.Selection(
        related="company_id.tp_nesting_kernel_name",
        readonly=False,
    )
    tp_nesting_timeout_ms = fields.Integer(
        related="company_id.tp_nesting_timeout_ms",
        readonly=False,
    )
    tp_nesting_fallback_enabled = fields.Boolean(
        related="company_id.tp_nesting_fallback_enabled",
        readonly=False,
    )
