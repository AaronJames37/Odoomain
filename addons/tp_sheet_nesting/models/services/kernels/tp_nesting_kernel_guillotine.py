from .tp_nesting_kernel_base import TpNestingKernelBase


class TpNestingKernelGuillotine(TpNestingKernelBase):
    name = "guillotine"

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
                rem_w = rect["w"] - placement["used_w"]
                rem_h = rect["h"] - placement["used_h"]
                leftover_area = float(rem_w * rect["h"] + placement["used_w"] * rem_h)
                short_side = min(rem_w, rem_h)
                long_side = max(rem_w, rem_h)
                score = (leftover_area, short_side, long_side, rect["y"], rect["x"], 1 if rotated else 0)
                if best is None or score < best_score:
                    best = placement
                    best_score = score
        return best

    def _split_rect(self, rect, placement):
        right_a = {
            "x": rect["x"] + placement["used_w"],
            "y": rect["y"],
            "w": rect["w"] - placement["used_w"],
            "h": rect["h"],
        }
        bottom_a = {
            "x": rect["x"],
            "y": rect["y"] + placement["used_h"],
            "w": placement["used_w"],
            "h": rect["h"] - placement["used_h"],
        }
        a_rects = [r for r in (right_a, bottom_a) if r["w"] > 0 and r["h"] > 0]

        right_b = {
            "x": rect["x"] + placement["used_w"],
            "y": rect["y"],
            "w": rect["w"] - placement["used_w"],
            "h": placement["used_h"],
        }
        bottom_b = {
            "x": rect["x"],
            "y": rect["y"] + placement["used_h"],
            "w": rect["w"],
            "h": rect["h"] - placement["used_h"],
        }
        b_rects = [r for r in (right_b, bottom_b) if r["w"] > 0 and r["h"] > 0]

        a_score = max((self._rect_area(r) for r in a_rects), default=0.0)
        b_score = max((self._rect_area(r) for r in b_rects), default=0.0)
        return a_rects if a_score >= b_score else b_rects

    def apply_placement(self, bin_state, placement, cut):
        rect = bin_state["free_rects"].pop(placement["rect_idx"])
        new_rects = self._split_rect(rect, placement)
        bin_state["free_rects"].extend(new_rects)
        bin_state["free_rects"] = self._prune_free_rects(bin_state["free_rects"])
        self._append_placement(bin_state, placement, cut)
