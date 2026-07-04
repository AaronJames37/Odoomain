class TpNestingKernelBase:
    name = "base"

    def __init__(self, *, kerf_mm):
        self.kerf_mm = max(int(kerf_mm or 0), 0)

    @staticmethod
    def _rect_area(rect):
        return float(rect["w"] * rect["h"])

    @staticmethod
    def _rect_contains(a, b):
        return (
            a["x"] <= b["x"]
            and a["y"] <= b["y"]
            and a["x"] + a["w"] >= b["x"] + b["w"]
            and a["y"] + a["h"] >= b["y"] + b["h"]
        )

    @staticmethod
    def _rect_intersects(a, b):
        return not (
            a["x"] + a["w"] <= b["x"]
            or b["x"] + b["w"] <= a["x"]
            or a["y"] + a["h"] <= b["y"]
            or b["y"] + b["h"] <= a["y"]
        )

    def _prune_free_rects(self, free_rects):
        pruned = []
        for idx, rect in enumerate(free_rects):
            if rect["w"] <= 0 or rect["h"] <= 0:
                continue
            covered = False
            for jdx, other in enumerate(free_rects):
                if idx == jdx:
                    continue
                if self._rect_contains(other, rect):
                    covered = True
                    break
            if not covered:
                pruned.append(rect)
        return pruned

    def _make_placement(self, *, rect_idx, rect, fit_w, fit_h, rotated):
        # Kerf is the saw cut consumed BETWEEN pieces (and between a piece and
        # the offcut beyond it). A piece edge that reaches the free rect's far
        # edge needs no further cut there, so we clamp the reserved kerf to the
        # rect: used = min(fit + kerf, rect_size). This keeps inter-piece kerf
        # intact while never charging kerf against a sheet/offcut boundary, and
        # guarantees used <= rect (so placements stay within the source).
        used_w = int(min(fit_w + self.kerf_mm, int(rect["w"])))
        used_h = int(min(fit_h + self.kerf_mm, int(rect["h"])))
        return {
            "rect_idx": rect_idx,
            "x": int(rect["x"]),
            "y": int(rect["y"]),
            "fit_w": int(fit_w),
            "fit_h": int(fit_h),
            "used_w": used_w,
            "used_h": used_h,
            "rotated": bool(rotated),
            "kernel": self.name,
        }

    @staticmethod
    def _orientation_options(cut):
        cut_w = int(cut["width_mm"])
        cut_h = int(cut["height_mm"])
        options = [(cut_w, cut_h, False), (cut_h, cut_w, True)]
        if bool(cut.get("_rotation_first")):
            options = list(reversed(options))

        deduped = []
        seen = set()
        for fit_w, fit_h, rotated in options:
            key = (fit_w, fit_h)
            if key in seen:
                continue
            seen.add(key)
            deduped.append((fit_w, fit_h, rotated))
        return deduped

    def _append_placement(self, bin_state, placement, cut):
        bin_state["placements"].append(
            {
                "cut": dict(cut),
                "x": int(placement["x"]),
                "y": int(placement["y"]),
                "fit_w": int(placement["fit_w"]),
                "fit_h": int(placement["fit_h"]),
                "used_w": int(placement["used_w"]),
                "used_h": int(placement["used_h"]),
                "rotated": bool(placement["rotated"]),
                "kernel": self.name,
            }
        )

    def best_fit_in_bin(self, bin_state, cut, *, increment_search):
        raise NotImplementedError

    def apply_placement(self, bin_state, placement, cut):
        raise NotImplementedError
