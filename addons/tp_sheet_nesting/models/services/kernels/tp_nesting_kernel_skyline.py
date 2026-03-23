from .tp_nesting_kernel_base import TpNestingKernelBase


class TpNestingKernelSkyline(TpNestingKernelBase):
    name = "skyline"

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
                # Basic skyline bottom-left preference.
                score = (
                    rect["y"],
                    rect["x"],
                    rem_h,
                    rem_w,
                    1 if rotated else 0,
                )
                if best is None or score < best_score:
                    best = placement
                    best_score = score
        return best

    def apply_placement(self, bin_state, placement, cut):
        rect = bin_state["free_rects"].pop(placement["rect_idx"])
        right_rect = {
            "x": rect["x"] + placement["used_w"],
            "y": rect["y"],
            "w": rect["w"] - placement["used_w"],
            "h": placement["used_h"],
        }
        top_rect = {
            "x": rect["x"],
            "y": rect["y"] + placement["used_h"],
            "w": rect["w"],
            "h": rect["h"] - placement["used_h"],
        }
        for candidate in (right_rect, top_rect):
            if candidate["w"] > 0 and candidate["h"] > 0:
                bin_state["free_rects"].append(candidate)
        bin_state["free_rects"] = self._prune_free_rects(bin_state["free_rects"])
        self._append_placement(bin_state, placement, cut)
