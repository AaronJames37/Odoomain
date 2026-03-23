from .tp_nesting_kernel_base import TpNestingKernelBase


class TpNestingKernelMaxRects(TpNestingKernelBase):
    name = "maxrects"

    @staticmethod
    def _segment_overlap(start_a, end_a, start_b, end_b):
        return max(0, min(end_a, end_b) - max(start_a, start_b))

    def _contact_score(self, bin_state, placement):
        used_x = int(placement["x"])
        used_y = int(placement["y"])
        used_w = int(placement["used_w"])
        used_h = int(placement["used_h"])
        used_right = used_x + used_w
        used_bottom = used_y + used_h
        src_w = int(bin_state["source"]["width_mm"])
        src_h = int(bin_state["source"]["height_mm"])

        score = 0
        if used_x == 0:
            score += used_h
        if used_y == 0:
            score += used_w
        if used_right == src_w:
            score += used_h
        if used_bottom == src_h:
            score += used_w

        for placed in bin_state["placements"]:
            px = int(placed["x"])
            py = int(placed["y"])
            pw = int(placed["used_w"])
            ph = int(placed["used_h"])
            pr = px + pw
            pb = py + ph

            if used_x == pr or used_right == px:
                score += self._segment_overlap(used_y, used_bottom, py, pb)
            if used_y == pb or used_bottom == py:
                score += self._segment_overlap(used_x, used_right, px, pr)
        return score

    def _normalize_free_rects(self, free_rects):
        normalized = []
        seen = set()
        for rect in free_rects:
            x = int(rect["x"])
            y = int(rect["y"])
            w = int(rect["w"])
            h = int(rect["h"])
            # Rectangles smaller than kerf cannot host any valid cut+kerf placement.
            if w <= self.kerf_mm or h <= self.kerf_mm:
                continue
            key = (x, y, w, h)
            if key in seen:
                continue
            seen.add(key)
            normalized.append({"x": x, "y": y, "w": w, "h": h})
        normalized.sort(key=lambda r: (r["y"], r["x"], r["w"], r["h"]))
        return normalized

    def best_fit_in_bin(self, bin_state, cut, *, increment_search):
        best = None
        best_score = None
        for rect_idx, rect in enumerate(bin_state["free_rects"]):
            for fit_w, fit_h, rotated in self._orientation_options(cut):
                increment_search()
                placement = self._make_placement(
                    rect_idx=rect_idx,
                    rect=rect,
                    fit_w=fit_w,
                    fit_h=fit_h,
                    rotated=rotated,
                )
                if rect["w"] < placement["used_w"] or rect["h"] < placement["used_h"]:
                    continue
                leftover_w = rect["w"] - placement["used_w"]
                leftover_h = rect["h"] - placement["used_h"]
                bssf_short = min(leftover_w, leftover_h)
                bssf_long = max(leftover_w, leftover_h)
                area_fit = float(rect["w"] * rect["h"] - placement["used_w"] * placement["used_h"])
                contact = self._contact_score(bin_state, placement)
                # Enhanced MaxRects heuristic:
                # 1) Best Short Side Fit
                # 2) Best Long Side Fit
                # 3) Best Area Fit
                # 4) Contact Point (prefer larger)
                # 5) Bottom-left fallback + stable tie-break
                score = (
                    bssf_short,
                    bssf_long,
                    area_fit,
                    -contact,
                    rect["y"],
                    rect["x"],
                    rect_idx,
                    1 if rotated else 0,
                )
                if best is None or score < best_score:
                    best = placement
                    best_score = score
        return best

    def _split_free_rect(self, free_rect, used_rect):
        if not self._rect_intersects(free_rect, used_rect):
            return [free_rect]

        new_rects = []
        free_right = free_rect["x"] + free_rect["w"]
        free_bottom = free_rect["y"] + free_rect["h"]
        used_right = used_rect["x"] + used_rect["w"]
        used_bottom = used_rect["y"] + used_rect["h"]

        if used_rect["x"] > free_rect["x"]:
            new_rects.append(
                {
                    "x": free_rect["x"],
                    "y": free_rect["y"],
                    "w": used_rect["x"] - free_rect["x"],
                    "h": free_rect["h"],
                }
            )
        if used_right < free_right:
            new_rects.append(
                {
                    "x": used_right,
                    "y": free_rect["y"],
                    "w": free_right - used_right,
                    "h": free_rect["h"],
                }
            )
        if used_rect["y"] > free_rect["y"]:
            new_rects.append(
                {
                    "x": free_rect["x"],
                    "y": free_rect["y"],
                    "w": free_rect["w"],
                    "h": used_rect["y"] - free_rect["y"],
                }
            )
        if used_bottom < free_bottom:
            new_rects.append(
                {
                    "x": free_rect["x"],
                    "y": used_bottom,
                    "w": free_rect["w"],
                    "h": free_bottom - used_bottom,
                }
            )
        return [rect for rect in new_rects if rect["w"] > 0 and rect["h"] > 0]

    def apply_placement(self, bin_state, placement, cut):
        used_rect = {
            "x": placement["x"],
            "y": placement["y"],
            "w": placement["used_w"],
            "h": placement["used_h"],
        }
        new_free_rects = []
        for free_rect in bin_state["free_rects"]:
            new_free_rects.extend(self._split_free_rect(free_rect, used_rect))
        pruned = self._prune_free_rects(new_free_rects)
        bin_state["free_rects"] = self._normalize_free_rects(pruned)
        self._append_placement(bin_state, placement, cut)
