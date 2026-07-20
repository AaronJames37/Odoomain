from odoo import fields, models


class ResCompany(models.Model):
    _inherit = "res.company"
    tp_nesting_kerf_mm = fields.Integer(
        default=3,
        string="Nesting Kerf (mm)",
    )
    tp_nesting_ignore_sheet_stock = fields.Boolean(
        default=False,
        string="Ignore Full Sheet Stock Levels",
        help=(
            "When disabled, workshop nesting only considers full sheet SKUs "
            "that currently have stock in the main warehouse. Offcuts are still "
            "considered separately. Enable this to allow out-of-stock full "
            "sheets into the nesting source pool."
        ),
    )
    tp_nesting_trim_edge_mm = fields.Integer(
        default=0,
        string="Trim Edge (mm, discontinued)",
        help="Deprecated. Trim edge is no longer used by the sheet nesting solver.",
    )
    tp_nesting_guillotine_seconds = fields.Integer(
        default=10,
        string="Guillotine Search Time (s)",
        help="How long the CutList-grade guillotine (panel-saw) optimiser may "
             "search for the best layout. Longer = better utilisation on big jobs.",
    )
    tp_nesting_offcut_min_usage_pct = fields.Integer(
        default=80,
        string="Offcut Min Usage (%)",
        help="Use an offcut before opening a fresh sheet only if the job fills it "
             "to at least this percentage. Stops a barely-used offcut being "
             "consumed when a fresh sheet would be tidier. 0 = always use offcuts.",
    )
    tp_nesting_engine_mode = fields.Selection(
        [("optimal", "Optimal"), ("deterministic", "Deterministic")],
        default="optimal",
        string="Nesting Engine Mode",
    )
    tp_nesting_sheet_size_candidate_limit = fields.Integer(
        default=25,
        string="Sheet Size Candidate Limit",
    )
    tp_nesting_beam_width = fields.Integer(
        default=6,
        string="Nesting Beam Width",
    )
    tp_nesting_branch_cap = fields.Integer(
        default=12,
        string="Nesting Branch Cap",
    )
    tp_nesting_max_piece_count = fields.Integer(
        default=200,
        string="Nesting Max Piece Count",
    )
    tp_nesting_beam_width_cap = fields.Integer(
        default=24,
        string="Nesting Beam Width Cap",
    )
    tp_nesting_timeout_cap_ms = fields.Integer(
        default=15000,
        string="Nesting Timeout Cap (ms)",
    )
    tp_nesting_policy_preset = fields.Selection(
        [
            ("yield_first", "Yield First"),
            ("cost_first", "Cost First"),
            ("offcut_first", "Offcut First"),
        ],
        default="yield_first",
        string="Nesting Policy Preset",
    )
    tp_nesting_waste_priority = fields.Float(
        default=1.0,
        string="Waste Priority",
    )
    tp_nesting_offcut_reuse_priority = fields.Float(
        default=1.0,
        string="Offcut Reuse Priority",
    )
    tp_nesting_sheet_count_penalty = fields.Float(
        default=1.0,
        string="Sheet Count Penalty",
    )
    tp_nesting_cost_sensitivity = fields.Float(
        default=1.0,
        string="Cost Sensitivity",
    )
    tp_nesting_debug_enabled = fields.Boolean(
        default=False,
        string="Enable Nesting Debug Artifacts",
    )
    tp_nesting_exact_refinement_enabled = fields.Boolean(
        default=True,
        string="Enable Exact Refinement (Small Jobs)",
    )
    tp_nesting_exact_refinement_cut_threshold = fields.Integer(
        default=8,
        string="Exact Refinement Cut Threshold",
    )
    tp_nesting_exact_refinement_timeout_ms = fields.Integer(
        default=250,
        string="Exact Refinement Timeout (ms)",
    )
    tp_nesting_kernel_name = fields.Selection(
        [
            ("maxrects", "MaxRects"),
            ("guillotine", "Guillotine"),
            ("skyline", "Skyline"),
        ],
        default="maxrects",
        string="Nesting Placement Kernel",
    )
    tp_nesting_timeout_ms = fields.Integer(default=2000, string="Nesting Timeout (ms)")
    tp_nesting_fallback_enabled = fields.Boolean(default=True, string="Fallback To Deterministic")
    tp_nesting_include_cutout_parts = fields.Boolean(
        default=True,
        string="Include Cut-Out Parts In Nesting",
        help=(
            "When enabled (default), panels with inner cut-outs are included "
            "in nesting runs. Disable to silently skip any panel whose "
            "cut_outs payload is non-empty — useful while the CAM-side "
            "support for cut-outs is still being validated."
        ),
    )
