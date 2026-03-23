class TpNestingEngineScoringMixin:

    def _prepare_policy_context(self, *, cuts, sheet_lot_sources, sheet_format_sources):
        cut_area = sum(float(int(cut.get("width_mm", 0)) * int(cut.get("height_mm", 0))) for cut in cuts)
        source_areas = [
            float(int(src.get("width_mm", 0)) * int(src.get("height_mm", 0)))
            for src in list(sheet_lot_sources) + list(sheet_format_sources)
            if int(src.get("width_mm", 0)) > 0 and int(src.get("height_mm", 0)) > 0
        ]
        source_costs = [float(src.get("unit_cost") or 0.0) for src in list(sheet_lot_sources) + list(sheet_format_sources)]

        area_candidates = [area for area in source_areas if area > 0.0]
        if area_candidates:
            area_candidates.sort()
            self.policy_area_anchor = area_candidates[len(area_candidates) // 2]
        elif cut_area > 0:
            self.policy_area_anchor = cut_area
        else:
            self.policy_area_anchor = 1.0

        cost_candidates = [cost for cost in source_costs if cost > 0.0]
        if cost_candidates:
            cost_candidates.sort()
            self.policy_cost_anchor = cost_candidates[len(cost_candidates) // 2]
        else:
            self.policy_cost_anchor = 1.0

        self.policy_area_anchor = max(float(self.policy_area_anchor), 1.0)
        self.policy_cost_anchor = max(float(self.policy_cost_anchor), 1.0)

    def _score_bins(self, bins):
        waste_area = sum(self._bin_leftover_area(bin_state) for bin_state in bins)
        # Offcuts are consumed in the pre-pass. Within the sheet planner,
        # sheet-lot usage models reuse preference versus buying new formats.
        reuse_area = sum(
            self._bin_used_area(bin_state)
            for bin_state in bins
            if bin_state["source"].get("kind") in ("sheet_lot", "sheet_product")
        )
        sheet_count = len(bins)
        total_cost = sum(float(bin_state["source"].get("unit_cost") or 0.0) for bin_state in bins)
        waste_norm = waste_area / self.policy_area_anchor
        reuse_norm = reuse_area / self.policy_area_anchor
        cost_norm = total_cost / self.policy_cost_anchor
        policy_score = self.policy.score(
            waste_norm=waste_norm,
            reuse_norm=reuse_norm,
            sheet_count=sheet_count,
            cost_norm=cost_norm,
        )
        source_trace = tuple(self._source_stable_id(bin_state["source"]) for bin_state in bins)
        return policy_score, waste_area, reuse_area, sheet_count, total_cost, source_trace

    def _score_node(self, node):
        policy_score, waste_area, reuse_area, sheet_count, total_cost, source_trace = self._score_bins(node["bins"])
        return (
            policy_score,
            sheet_count,
            waste_area,
            -reuse_area,
            total_cost,
            source_trace,
            node["path_key"],
        )

    def _score_final(self, bins, order_name):
        policy_score, waste_area, reuse_area, sheet_count, total_cost, source_trace = self._score_bins(bins)
        return (
            policy_score,
            sheet_count,
            waste_area,
            -reuse_area,
            total_cost,
            source_trace,
            order_name,
        )

    @staticmethod
    def _score_components(score_tuple):
        return {
            "policy_score": float(score_tuple[0]),
            "sheet_count": int(score_tuple[1]),
            "waste_area_mm2": float(score_tuple[2]),
            "reuse_area_mm2": float(-score_tuple[3]),
            "total_source_cost": float(score_tuple[4]),
            "source_trace": [str(v) for v in (score_tuple[5] or tuple())],
            "order_name": str(score_tuple[6]),
        }
