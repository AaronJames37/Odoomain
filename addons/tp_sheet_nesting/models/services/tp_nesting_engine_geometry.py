class TpNestingEngineGeometryMixin:

    @staticmethod
    def _source_area(source):
        return float(source["width_mm"] * source["height_mm"])

    @staticmethod
    def _placement_area(placement):
        return float(int(placement["used_w"]) * int(placement["used_h"]))

    def _bin_used_area(self, bin_state):
        return sum(self._placement_area(p) for p in bin_state["placements"])

    def _bin_leftover_area(self, bin_state):
        leftover = self._source_area(bin_state["source"]) - self._bin_used_area(bin_state)
        return max(0.0, leftover)

    @staticmethod
    def _source_stable_id(source):
        return str(source.get("stable_id") or source.get("id") or "")

    @staticmethod
    def _source_sort_key(source):
        kind_priority = {"sheet_product": 0, "sheet_lot": 1, "sheet_format": 2}
        return (
            kind_priority.get(source.get("kind"), 99),
            int(source.get("width_mm") or 0) * int(source.get("height_mm") or 0),
            float(source.get("unit_cost") or 0.0),
            str(source.get("stable_id") or source.get("id") or ""),
        )

    @staticmethod
    def _source_diversity_signature(source):
        return (
            str(source.get("kind") or ""),
            int(source.get("product_id") or 0),
            int(source.get("width_mm") or 0),
            int(source.get("height_mm") or 0),
        )

    def _limit_candidates_diverse(self, candidates):
        if self.sheet_size_candidate_limit <= 0 or len(candidates) <= self.sheet_size_candidate_limit:
            return candidates
        limit = int(self.sheet_size_candidate_limit)
        selected = []
        seen_signatures = set()
        remainder = []
        for source in candidates:
            signature = self._source_diversity_signature(source)
            if signature in seen_signatures:
                remainder.append(source)
                continue
            selected.append(source)
            seen_signatures.add(signature)
            if len(selected) >= limit:
                return selected
        if len(selected) < limit:
            selected.extend(remainder[: limit - len(selected)])
        return selected

    @staticmethod
    def _ordering_signature(ordered_cuts):
        return tuple(
            (
                int(cut.get("_cid", idx)),
                int(bool(cut.get("_rotation_first"))),
            )
            for idx, cut in enumerate(ordered_cuts)
        )

    @staticmethod
    def _clone_cut_sequence(ordered_cuts):
        return [dict(cut) for cut in ordered_cuts]

    def _increment_search_node(self):
        self.search_nodes += 1

    def _best_fit_in_bin(self, bin_state, cut):
        return self.kernel.best_fit_in_bin(bin_state, cut, increment_search=self._increment_search_node)

    def _apply_placement(self, bin_state, placement, cut):
        self.kernel.apply_placement(bin_state, placement, cut)

    @staticmethod
    def _initial_bin(source):
        return {
            "source": source,
            "free_rects": [{"x": 0, "y": 0, "w": int(source["width_mm"]), "h": int(source["height_mm"])}],
            "placements": [],
        }

    @staticmethod
    def _clone_bin(bin_state):
        return {
            "source": bin_state["source"],
            "free_rects": [dict(r) for r in bin_state["free_rects"]],
            "placements": [
                {
                    **dict(p),
                    "cut": dict(p["cut"]),
                }
                for p in bin_state["placements"]
            ],
        }

    def _clone_bins(self, bins):
        return [self._clone_bin(bin_state) for bin_state in bins]

    @staticmethod
    def _can_fit_source_dims(source_width, source_height, cut_width, cut_height, kerf):
        source_width = int(source_width or 0)
        source_height = int(source_height or 0)
        cut_width = int(cut_width or 0)
        cut_height = int(cut_height or 0)
        k = int(kerf or 0)
        return (
            (source_width >= cut_width + k and source_height >= cut_height + k)
            or (source_width >= cut_height + k and source_height >= cut_width + k)
        )

    def _early_infeasible_cut(self, *, cuts, sheet_lot_sources, sheet_format_sources):
        self.early_infeasible_checks += 1
        if len(cuts) > self.max_pieces:
            return {
                "width_mm": int(cuts[0].get("width_mm", 0)),
                "height_mm": int(cuts[0].get("height_mm", 0)),
                "_reason": "max_pieces_exceeded",
            }

        all_sources = list(sheet_lot_sources) + list(sheet_format_sources)
        if not all_sources:
            return {
                "width_mm": int(cuts[0].get("width_mm", 0)),
                "height_mm": int(cuts[0].get("height_mm", 0)),
                "_reason": "no_sheet_sources",
            }

        for cut in cuts:
            can_fit = any(
                self._can_fit_source_dims(
                    source.get("width_mm", 0),
                    source.get("height_mm", 0),
                    cut.get("width_mm", 0),
                    cut.get("height_mm", 0),
                    self.kerf_mm,
                )
                for source in all_sources
            )
            if not can_fit:
                return {
                    "width_mm": int(cut.get("width_mm", 0)),
                    "height_mm": int(cut.get("height_mm", 0)),
                    "_reason": "cut_exceeds_all_sources",
                }
        return None

    @staticmethod
    def _rect_signature(rect):
        return (
            int(rect.get("x", 0)),
            int(rect.get("y", 0)),
            int(rect.get("w", 0)),
            int(rect.get("h", 0)),
        )

    def _bin_signature(self, bin_state):
        source_id = self._source_stable_id(bin_state["source"])
        free_rects = tuple(sorted(self._rect_signature(rect) for rect in bin_state.get("free_rects", []))[:16])
        placements = tuple(
            sorted(
                (
                    int(placement.get("x", 0)),
                    int(placement.get("y", 0)),
                    int(placement.get("fit_w", 0)),
                    int(placement.get("fit_h", 0)),
                    int(placement["cut"].get("_cid", 0)),
                    int(bool(placement.get("rotated", False))),
                )
                for placement in bin_state.get("placements", [])
            )[:32]
        )
        return source_id, free_rects, placements

    def _node_signature(self, node):
        return (
            tuple(sorted(int(lot_id) for lot_id in node.get("unused_lot_ids", set()))),
            tuple(sorted(self._bin_signature(bin_state) for bin_state in node.get("bins", []))),
        )

