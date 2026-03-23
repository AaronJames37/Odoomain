import time


class TpNestingOptimizer:
    """Phase 5 optimizer service.

    Keeps behavior deterministic and bounded while improving source selection:
    - score by waste first
    - then offcut preference
    - then sheet count
    - then effective material cost
    """

    def __init__(self, *, kerf_mm=3, timeout_ms=2000, sheet_size_candidate_limit=25):
        self.kerf_mm = kerf_mm
        self.timeout_ms = timeout_ms
        self.sheet_size_candidate_limit = max(1, int(sheet_size_candidate_limit or 1))
        self.started_at = time.monotonic()
        self.search_nodes = 0
        self.sheet_ids_used = set()

    def _check_timeout(self):
        elapsed_ms = int((time.monotonic() - self.started_at) * 1000)
        if elapsed_ms > self.timeout_ms:
            raise TimeoutError("Optimal nesting search exceeded timeout.")
        return elapsed_ms

    @staticmethod
    def _effective_cost_per_area(source):
        if source["type"] == "offcut":
            area = float(source["record"].remaining_area_mm2 or source["record"].area_mm2 or 0.0)
            value = float(source["record"].remaining_value or 0.0)
        elif source["type"] == "sheet_slot":
            area = float(source.get("area_mm2") or 0.0)
            value = float(source.get("unit_cost") or 0.0)
        else:
            area = float(source.get("area_mm2") or getattr(source["record"], "area_mm2", 0.0) or 0.0)
            value = float(
                source.get("unit_cost")
                or getattr(source["record"], "landed_cost", 0.0)
                or source["record"].product_id.standard_price
                or 0.0
            )
        if area <= 0:
            return float("inf")
        return value / area

    def _score_candidate(self, candidate):
        source = candidate["source"]
        remainder_area = float(candidate["rem_w"] * candidate["rem_h"])
        source_penalty = 0 if source["type"] == "offcut" else (1 if source["type"] == "sheet_slot" else 2)
        is_sheet_source = source["type"] in ("sheet", "sheet_lot")
        projected_sheet_count = len(self.sheet_ids_used | {source["id"]}) if is_sheet_source else len(self.sheet_ids_used)
        cost = self._effective_cost_per_area(source) * float(candidate["fit_w"] * candidate["fit_h"])
        return (
            remainder_area,
            source_penalty,
            projected_sheet_count,
            cost,
            source["id"],
            1 if candidate["rotated"] else 0,
        )

    def select_candidate(self, *, cut, sources, fit_fn):
        self._check_timeout()
        offcut_sources = [s for s in sources if s["type"] == "offcut"]
        slot_sources = [s for s in sources if s["type"] == "sheet_slot"]
        sheet_sources = [s for s in sources if s["type"] in ("sheet", "sheet_lot")][: self.sheet_size_candidate_limit]
        candidate_sources = offcut_sources + slot_sources + sheet_sources
        best = None
        best_score = None
        for source in candidate_sources:
            self.search_nodes += 1
            fits, rotated, fit_w, fit_h, rem_w, rem_h = fit_fn(
                source["width_mm"], source["height_mm"], cut["width_mm"], cut["height_mm"], self.kerf_mm
            )
            if not fits:
                continue
            candidate = {
                "source": source,
                "rotated": rotated,
                "fit_w": fit_w,
                "fit_h": fit_h,
                "rem_w": rem_w,
                "rem_h": rem_h,
            }
            score = self._score_candidate(candidate)
            if best is None or score < best_score:
                best = candidate
                best_score = score
        if best and best["source"]["type"] in ("sheet", "sheet_lot"):
            self.sheet_ids_used.add(best["source"]["id"])
        return best

    def metrics(self):
        elapsed_ms = int((time.monotonic() - self.started_at) * 1000)
        return {
            "search_nodes": self.search_nodes,
            "search_ms": elapsed_ms,
            "full_sheet_count": len(self.sheet_ids_used),
        }
