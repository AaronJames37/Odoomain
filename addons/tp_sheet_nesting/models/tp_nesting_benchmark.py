import hashlib
import json
import uuid

from odoo import api, fields, models
from odoo.exceptions import ValidationError


class _TpBenchmarkRollback(Exception):
    def __init__(self, payload):
        super().__init__("benchmark rollback")
        self.payload = payload


class TpNestingBenchmark(models.Model):
    _name = "tp.nesting.benchmark"
    _description = "TP Nesting Benchmark Fixture"
    _order = "id desc"

    name = fields.Char(required=True, copy=False, default="New")
    active = fields.Boolean(default=True)
    demand_product_id = fields.Many2one("product.product", required=True)
    bom_id = fields.Many2one(
        "mrp.bom",
        domain="[('product_tmpl_id', '=', demand_product_id.product_tmpl_id)]",
    )
    repeat_count = fields.Integer(default=2, string="Repeats Per Engine")
    note = fields.Text()
    cut_line_ids = fields.One2many("tp.nesting.benchmark.cut", "benchmark_id", string="Cut Lines")
    source_line_ids = fields.One2many("tp.nesting.benchmark.source", "benchmark_id", string="Source Lines")
    result_ids = fields.One2many("tp.nesting.benchmark.result", "benchmark_id", string="Results", readonly=True)
    last_batch_token = fields.Char(readonly=True)
    last_run_at = fields.Datetime(readonly=True)
    deterministic_hash = fields.Char(readonly=True)
    optimal_hash = fields.Char(readonly=True)
    deterministic_stable = fields.Boolean(readonly=True)
    optimal_stable = fields.Boolean(readonly=True)

    def _tp_get_warehouse(self):
        warehouse = self.env.ref("stock.warehouse0", raise_if_not_found=False)
        if warehouse:
            return warehouse
        warehouse = self.env["stock.warehouse"].search([], limit=1)
        if not warehouse:
            raise ValidationError("No warehouse found for benchmark execution.")
        return warehouse

    def _tp_get_bom(self):
        self.ensure_one()
        bom = self.bom_id
        if not bom:
            bom = self.env["mrp.bom"].search(
                [("product_tmpl_id", "=", self.demand_product_id.product_tmpl_id.id)],
                limit=1,
            )
        if not bom:
            raise ValidationError(
                f"No BOM found for benchmark demand product '{self.demand_product_id.display_name}'."
            )
        return bom

    def _tp_build_probe_mo(self):
        self.ensure_one()
        if not self.cut_line_ids:
            raise ValidationError("Benchmark requires at least one cut line.")
        warehouse = self._tp_get_warehouse()
        if not warehouse.manufacture_pull_id:
            raise ValidationError("Warehouse is missing Manufacture pull rule.")
        bom = self._tp_get_bom()
        first_cut = self.cut_line_ids.sorted("sequence")[0]
        partner = self.env["res.partner"].create({"name": f"Benchmark Partner {self.id}"})
        order = self.env["sale.order"].create(
            {
                "partner_id": partner.id,
                "order_line": [
                    (
                        0,
                        0,
                        {
                            "product_id": self.demand_product_id.id,
                            "product_uom_qty": 1.0,
                            "price_unit": 1.0,
                            "tp_width_mm": first_cut.width_mm,
                            "tp_height_mm": first_cut.height_mm,
                        },
                    )
                ],
            }
        )
        sale_line = order.order_line[:1]
        vals = sale_line._prepare_procurement_values()
        mo_vals = warehouse.manufacture_pull_id._prepare_mo_vals(
            self.demand_product_id,
            sale_line.product_uom_qty,
            sale_line.product_uom_id,
            warehouse.lot_stock_id,
            sale_line.name,
            order.name,
            order.company_id,
            vals,
            bom,
        )
        mo = self.env["mrp.production"].create(mo_vals)
        mo.tp_cut_line_ids = [
            (
                0,
                0,
                {
                    "width_mm": line.width_mm,
                    "height_mm": line.height_mm,
                    "quantity": line.quantity,
                },
            )
            for line in self.cut_line_ids.sorted("sequence")
        ]
        return mo

    def _tp_create_source_map(self, source_product, source_lot=False):
        self.ensure_one()
        if source_product == self.demand_product_id:
            return
        map_vals = {
            "name": f"Benchmark {self.name} map {source_product.display_name}",
            "demand_product_id": self.demand_product_id.id,
            "source_product_id": source_product.id,
        }
        if source_lot:
            map_vals["source_lot_id"] = source_lot.id
        self.env["tp.nesting.source.map"].create(map_vals)

    def _tp_materialize_sources(self):
        self.ensure_one()
        stock_location = self.env.ref("stock.stock_location_stock")
        for source in self.source_line_ids.sorted("sequence"):
            source_product = source.product_id or self.demand_product_id
            if source.source_type == "sheet_format":
                self.env["tp.sheet.format"].create(
                    {
                        "name": source.name or f"{self.name}-SHEET-{source.id}",
                        "product_id": source_product.id,
                        "width_mm": source.width_mm,
                        "height_mm": source.height_mm,
                        "landed_cost": source.landed_cost,
                    }
                )
                if source.auto_map:
                    self._tp_create_source_map(source_product)
                continue

            lot = self.env["stock.lot"].create(
                {
                    "name": source.name or f"{self.name}-LOT-{source.id}",
                    "product_id": source_product.id,
                    "company_id": self.env.company.id,
                    "tp_width_mm": source.width_mm,
                    "tp_height_mm": source.height_mm,
                }
            )
            if source.source_type == "sheet_lot":
                self.env["stock.quant"]._update_available_quantity(
                    source_product,
                    stock_location,
                    source.available_qty,
                    lot_id=lot,
                )
                if source.auto_map:
                    self._tp_create_source_map(source_product, source_lot=lot)
                continue

            parent_lot = self.env["stock.lot"].create(
                {
                    "name": f"{lot.name}-PARENT",
                    "product_id": source_product.id,
                    "company_id": self.env.company.id,
                }
            )
            self.env["tp.offcut"].create(
                {
                    "name": source.name or f"{self.name}-OFFCUT-{source.id}",
                    "lot_id": lot.id,
                    "width_mm": source.width_mm,
                    "height_mm": source.height_mm,
                    "source_type": "sheet",
                    "parent_lot_id": parent_lot.id,
                    "remaining_area_mm2": float(source.width_mm * source.height_mm),
                    "remaining_value": source.remaining_value,
                }
            )
            if source.auto_map:
                self._tp_create_source_map(source_product, source_lot=lot)

    def _tp_build_run_hash(self, run):
        entries = []
        for alloc in run.allocation_ids.sorted("id"):
            src_w, src_h = run.mo_id._tp_source_dims_from_allocation(alloc)
            entries.append(
                {
                    "source_type": alloc.source_type or "",
                    "source_w_mm": int(src_w or 0),
                    "source_h_mm": int(src_h or 0),
                    "cut_w_mm": int(alloc.cut_width_mm or 0),
                    "cut_h_mm": int(alloc.cut_height_mm or 0),
                    "cut_qty": int(alloc.cut_quantity or 0),
                    "rotation": bool(alloc.rotation_applied),
                }
            )
        entries.sort(
            key=lambda e: (
                e["source_type"],
                e["source_w_mm"],
                e["source_h_mm"],
                e["cut_w_mm"],
                e["cut_h_mm"],
                e["cut_qty"],
                int(e["rotation"]),
            )
        )
        payload = {
            "engine_mode": run.engine_mode,
            "kerf_mm": int(run.kerf_mm or 3),
            "full_sheet_count": int(run.full_sheet_count or 0),
            "waste_area_mm2_total": float(run.waste_area_mm2_total or 0.0),
            "allocations": entries,
        }
        normalized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def _tp_probe_engine(self, engine_mode):
        self.ensure_one()
        try:
            with self.env.cr.savepoint():
                mo = self._tp_build_probe_mo()
                self._tp_materialize_sources()
                self.env.company.write(
                    {
                        "tp_nesting_engine_mode": engine_mode,
                        "tp_nesting_fallback_enabled": True,
                    }
                )
                mo.action_run_tp_nesting()
                run = mo.tp_last_nesting_run_id
                total_allocated_area = sum(float(a.allocated_area_mm2 or 0.0) for a in run.allocation_ids)
                waste_area = float(run.waste_area_mm2_total or 0.0)
                denom = total_allocated_area + waste_area
                waste_pct = (waste_area / denom * 100.0) if denom > 0 else 0.0
                payload = {
                    "engine_mode": engine_mode,
                    "run_hash": self._tp_build_run_hash(run),
                    "allocation_count": len(run.allocation_ids),
                    "total_allocated_area_mm2": total_allocated_area,
                    "waste_area_mm2_total": waste_area,
                    "waste_pct": waste_pct,
                    "offcut_utilization_pct": float(run.offcut_utilization_pct or 0.0),
                    "full_sheet_count": int(run.full_sheet_count or 0),
                    "search_ms": int(run.search_ms or 0),
                    "search_nodes": int(run.search_nodes or 0),
                    "score": float(run.score or 0.0),
                }
                raise _TpBenchmarkRollback(payload)
        except _TpBenchmarkRollback as probe:
            return probe.payload

    def _tp_engine_result_values(self, *, engine_mode, batch_token, run_sequence, repeat_count):
        self.ensure_one()
        try:
            metrics = self._tp_probe_engine(engine_mode)
            return {
                "name": f"{self.name} [{engine_mode}] #{run_sequence}",
                "benchmark_id": self.id,
                "batch_token": batch_token,
                "engine_mode": engine_mode,
                "run_sequence": run_sequence,
                "repeat_count": repeat_count,
                "success": True,
                "run_hash": metrics["run_hash"],
                "allocation_count": metrics["allocation_count"],
                "total_allocated_area_mm2": metrics["total_allocated_area_mm2"],
                "waste_area_mm2_total": metrics["waste_area_mm2_total"],
                "waste_pct": metrics["waste_pct"],
                "offcut_utilization_pct": metrics["offcut_utilization_pct"],
                "full_sheet_count": metrics["full_sheet_count"],
                "search_ms": metrics["search_ms"],
                "search_nodes": metrics["search_nodes"],
                "score": metrics["score"],
            }
        except Exception as exc:
            return {
                "name": f"{self.name} [{engine_mode}] #{run_sequence}",
                "benchmark_id": self.id,
                "batch_token": batch_token,
                "engine_mode": engine_mode,
                "run_sequence": run_sequence,
                "repeat_count": repeat_count,
                "success": False,
                "error_message": str(exc),
            }

    @staticmethod
    def _tp_compute_stability(results):
        failed = results.filtered(lambda rec: not rec.success)
        if failed:
            return False, False
        hashes = set(results.mapped("run_hash"))
        if len(hashes) != 1:
            return False, False
        return True, next(iter(hashes))

    def action_run_benchmark(self):
        Result = self.env["tp.nesting.benchmark.result"]
        for benchmark in self:
            if not benchmark.cut_line_ids:
                raise ValidationError("Add at least one cut line before running benchmark.")
            batch_token = uuid.uuid4().hex
            repeat_count = max(int(benchmark.repeat_count or 1), 1)
            created = Result.browse()
            for engine_mode in ("deterministic", "optimal"):
                for run_sequence in range(1, repeat_count + 1):
                    vals = benchmark._tp_engine_result_values(
                        engine_mode=engine_mode,
                        batch_token=batch_token,
                        run_sequence=run_sequence,
                        repeat_count=repeat_count,
                    )
                    created |= Result.create(vals)

            det_results = created.filtered(lambda rec: rec.engine_mode == "deterministic")
            opt_results = created.filtered(lambda rec: rec.engine_mode == "optimal")
            det_stable, det_hash = benchmark._tp_compute_stability(det_results)
            opt_stable, opt_hash = benchmark._tp_compute_stability(opt_results)
            benchmark.write(
                {
                    "last_batch_token": batch_token,
                    "last_run_at": fields.Datetime.now(),
                    "deterministic_stable": det_stable,
                    "optimal_stable": opt_stable,
                    "deterministic_hash": det_hash or False,
                    "optimal_hash": opt_hash or False,
                }
            )
        return True


class TpNestingBenchmarkCut(models.Model):
    _name = "tp.nesting.benchmark.cut"
    _description = "TP Nesting Benchmark Cut Line"
    _order = "sequence asc,id asc"

    sequence = fields.Integer(default=10)
    benchmark_id = fields.Many2one("tp.nesting.benchmark", required=True, ondelete="cascade")
    width_mm = fields.Integer(required=True)
    height_mm = fields.Integer(required=True)
    quantity = fields.Integer(required=True, default=1)

    @api.constrains("width_mm", "height_mm", "quantity")
    def _check_dimensions(self):
        for rec in self:
            if rec.width_mm <= 0 or rec.height_mm <= 0 or rec.quantity <= 0:
                raise ValidationError("Cut line width, height, and quantity must be greater than zero.")


class TpNestingBenchmarkSource(models.Model):
    _name = "tp.nesting.benchmark.source"
    _description = "TP Nesting Benchmark Source Line"
    _order = "sequence asc,id asc"

    sequence = fields.Integer(default=10)
    benchmark_id = fields.Many2one("tp.nesting.benchmark", required=True, ondelete="cascade")
    source_type = fields.Selection(
        [("sheet_format", "Sheet Format"), ("sheet_lot", "Sheet Lot"), ("offcut", "Offcut")],
        required=True,
        default="sheet_format",
    )
    name = fields.Char()
    product_id = fields.Many2one("product.product")
    width_mm = fields.Integer(required=True)
    height_mm = fields.Integer(required=True)
    landed_cost = fields.Float(default=0.0)
    remaining_value = fields.Float(default=0.0)
    available_qty = fields.Float(default=1.0)
    auto_map = fields.Boolean(default=True)

    @api.constrains("width_mm", "height_mm", "available_qty", "source_type")
    def _check_dimensions(self):
        for rec in self:
            if rec.width_mm <= 0 or rec.height_mm <= 0:
                raise ValidationError("Source width and height must be greater than zero.")
            if rec.source_type == "sheet_lot" and rec.available_qty <= 0:
                raise ValidationError("Sheet lot available quantity must be greater than zero.")


class TpNestingBenchmarkResult(models.Model):
    _name = "tp.nesting.benchmark.result"
    _description = "TP Nesting Benchmark Result"
    _order = "id desc"

    name = fields.Char(required=True, copy=False, default="New")
    benchmark_id = fields.Many2one("tp.nesting.benchmark", required=True, ondelete="cascade")
    batch_token = fields.Char(required=True, index=True)
    engine_mode = fields.Selection(
        [("deterministic", "Deterministic"), ("optimal", "Optimal")],
        required=True,
    )
    run_sequence = fields.Integer(default=1, required=True)
    repeat_count = fields.Integer(default=1, required=True)
    success = fields.Boolean(default=True, required=True)
    error_message = fields.Text()
    run_hash = fields.Char(index=True)
    allocation_count = fields.Integer(default=0)
    total_allocated_area_mm2 = fields.Float(default=0.0)
    waste_area_mm2_total = fields.Float(default=0.0)
    waste_pct = fields.Float(default=0.0)
    offcut_utilization_pct = fields.Float(default=0.0)
    full_sheet_count = fields.Integer(default=0)
    search_ms = fields.Integer(default=0)
    search_nodes = fields.Integer(default=0)
    score = fields.Float(default=0.0)
