from odoo import api, fields, models


class TpNestingRun(models.Model):
    _name = "tp.nesting.run"
    _description = "TP Nesting Run"
    _order = "id desc"

    name = fields.Char(required=True, copy=False, default="New")
    mo_id = fields.Many2one("mrp.production", required=True, ondelete="cascade")
    state = fields.Selection(
        [("draft", "Draft"), ("done", "Done"), ("failed", "Failed")],
        default="draft",
        required=True,
    )
    kerf_mm = fields.Integer(default=3, required=True)
    trim_edge_mm = fields.Integer(default=0, required=True)
    rotation_mode = fields.Selection([("free", "Free")], default="free", required=True)
    started_at = fields.Datetime(default=fields.Datetime.now, required=True)
    finished_at = fields.Datetime()
    note = fields.Text()
    allocation_ids = fields.One2many("tp.nesting.allocation", "run_id")
    engine_mode = fields.Selection(
        [("deterministic", "Deterministic"), ("optimal", "Optimal")],
        default="deterministic",
        required=True,
    )
    search_nodes = fields.Integer(default=0)
    search_ms = fields.Integer(default=0)
    waste_area_mm2_total = fields.Float(default=0.0)
    offcut_utilization_pct = fields.Float(default=0.0)
    full_sheet_count = fields.Integer(default=0)
    score = fields.Float(default=0.0)
    selected_order_name = fields.Char(readonly=True)
    scoring_preset = fields.Char(readonly=True)
    score_breakdown_json = fields.Text(readonly=True)
    candidate_plan_count = fields.Integer(default=0, readonly=True)
    rejected_plan_count = fields.Integer(default=0, readonly=True)
    debug_artifact_json = fields.Text(readonly=True)
    nesting_svg = fields.Html(
        readonly=True,
        sanitize=False,
        compute="_compute_nesting_svg",
        store=True,
    )

    @api.depends(
        "allocation_ids",
        "allocation_ids.placed_x_mm",
        "allocation_ids.placed_y_mm",
        "allocation_ids.cut_width_mm",
        "allocation_ids.cut_height_mm",
        "allocation_ids.cut_quantity",
        "allocation_ids.source_so_line_id",
        "allocation_ids.web_cut_part_id",
        "allocation_ids.source_bin_key",
    )
    def _compute_nesting_svg(self):
        Mo = self.env["mrp.production"]
        for run in self:
            if not run.allocation_ids:
                run.nesting_svg = run.nesting_svg or False
                continue
            # Use the run's MO when present, otherwise an empty mrp.production
            # recordset (the SVG builder only needs `self` for env access).
            builder = run.mo_id or Mo
            try:
                run.nesting_svg = builder._tp_build_nesting_svg(run)
            except Exception:
                run.nesting_svg = run.nesting_svg or False
    job_id = fields.Many2one("tp.nesting.job", readonly=True)
    consumed_lot_ids = fields.One2many("tp.nesting.consumed.lot", "run_id", readonly=True)
    produced_panel_ids = fields.One2many("tp.nesting.produced.panel", "run_id", readonly=True)
    produced_offcut_ids = fields.One2many("tp.nesting.produced.offcut", "run_id", readonly=True)
    outputs_materialized = fields.Boolean(default=False, readonly=True)
    outputs_materialized_at = fields.Datetime(readonly=True)
