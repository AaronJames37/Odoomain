from odoo.tests.common import TransactionCase


class TestOptimizerPhaseO0Benchmark(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.warehouse = cls.env.ref("stock.warehouse0")
        cls.component_product = cls.env["product.product"].create(
            {
                "name": "O0 Component",
                "type": "consu",
                "sale_ok": False,
                "purchase_ok": True,
            }
        )
        cls.demand_product = cls.env["product.product"].create(
            {
                "name": "O0 Demand Panel",
                "type": "consu",
                "tracking": "lot",
                "sale_ok": True,
                "purchase_ok": False,
                "route_ids": [
                    (
                        6,
                        0,
                        [
                            cls.warehouse.mto_pull_id.route_id.id,
                            cls.warehouse.manufacture_pull_id.route_id.id,
                        ],
                    )
                ],
            }
        )
        cls.bom = cls.env["mrp.bom"].create(
            {
                "product_tmpl_id": cls.demand_product.product_tmpl_id.id,
                "product_qty": 1.0,
                "bom_line_ids": [
                    (
                        0,
                        0,
                        {
                            "product_id": cls.component_product.id,
                            "product_qty": 1.0,
                        },
                    )
                ],
            }
        )

    def _new_benchmark(self, repeat_count=2):
        return self.env["tp.nesting.benchmark"].create(
            {
                "name": "O0 Fixture",
                "demand_product_id": self.demand_product.id,
                "bom_id": self.bom.id,
                "repeat_count": repeat_count,
                "cut_line_ids": [
                    (0, 0, {"sequence": 10, "width_mm": 700, "height_mm": 500, "quantity": 1}),
                    (0, 0, {"sequence": 20, "width_mm": 600, "height_mm": 350, "quantity": 1}),
                ],
                "source_line_ids": [
                    (
                        0,
                        0,
                        {
                            "sequence": 10,
                            "source_type": "sheet_format",
                            "name": "O0 2440x1220",
                            "product_id": self.demand_product.id,
                            "width_mm": 2440,
                            "height_mm": 1220,
                            "landed_cost": 100.0,
                            "auto_map": False,
                        },
                    )
                ],
            }
        )

    def test_runner_is_probe_only_no_persistent_nesting_side_effects(self):
        benchmark = self._new_benchmark(repeat_count=1)
        model_names = [
            "mrp.production",
            "tp.nesting.run",
            "tp.nesting.allocation",
            "tp.sheet.format",
            "tp.offcut",
            "stock.lot",
            "tp.nesting.source.map",
        ]
        counts_before = {name: self.env[name].search_count([]) for name in model_names}

        benchmark.action_run_benchmark()

        counts_after = {name: self.env[name].search_count([]) for name in model_names}
        self.assertEqual(counts_before, counts_after)
        self.assertEqual(len(benchmark.result_ids), 2)
        self.assertTrue(all(benchmark.result_ids.mapped("success")))

    def test_kpi_fields_are_recorded_for_both_engines(self):
        benchmark = self._new_benchmark(repeat_count=1)

        benchmark.action_run_benchmark()

        det = benchmark.result_ids.filtered(lambda r: r.engine_mode == "deterministic")
        opt = benchmark.result_ids.filtered(lambda r: r.engine_mode == "optimal")
        self.assertEqual(len(det), 1)
        self.assertEqual(len(opt), 1)
        self.assertTrue(det.success)
        self.assertTrue(opt.success)
        self.assertTrue(det.run_hash)
        self.assertTrue(opt.run_hash)
        self.assertGreater(det.allocation_count, 0)
        self.assertGreater(opt.allocation_count, 0)
        self.assertGreaterEqual(det.waste_area_mm2_total, 0.0)
        self.assertGreaterEqual(opt.waste_area_mm2_total, 0.0)
        self.assertGreaterEqual(det.search_ms, 0)
        self.assertGreaterEqual(opt.search_ms, 0)
        self.assertGreaterEqual(det.search_nodes, 0)
        self.assertGreaterEqual(opt.search_nodes, 0)

    def test_repeatability_hash_stable_across_repeats_and_batches(self):
        benchmark = self._new_benchmark(repeat_count=2)

        benchmark.action_run_benchmark()

        self.assertTrue(benchmark.deterministic_stable)
        self.assertTrue(benchmark.optimal_stable)
        first_det_hash = benchmark.deterministic_hash
        first_opt_hash = benchmark.optimal_hash
        self.assertTrue(first_det_hash)
        self.assertTrue(first_opt_hash)

        benchmark.action_run_benchmark()

        self.assertEqual(benchmark.deterministic_hash, first_det_hash)
        self.assertEqual(benchmark.optimal_hash, first_opt_hash)

