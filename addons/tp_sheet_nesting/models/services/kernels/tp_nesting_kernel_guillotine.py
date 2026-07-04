from .tp_nesting_kernel_base import TpNestingKernelBase


class TpNestingKernelGuillotine(TpNestingKernelBase):
    """Panel-saw oriented guillotine packer.

    Produces Cutlist-style strip layouts where same-size pieces stack into clean
    strips, and every cut is edge-to-edge (rip the sheet into strips, then
    crosscut each strip). Two disciplines, chosen by ``rip_first``:

      * rip_first=False (default): vertical strips / columns. The first cut on a
        fresh sheet is a full-height cut (a crosscut on a landscape sheet);
        same-WIDTH pieces stack into columns.
      * rip_first=True: horizontal strips / rows. The first cut is a full-width
        cut (a rip on a landscape sheet); same-HEIGHT pieces sit in rows.

    (maxrects/skyline remain free-placement kernels for CNC.)
    """

    name = "guillotine"

    def __init__(self, *, kerf_mm, rip_first=False):
        super().__init__(kerf_mm=kerf_mm)
        self.rip_first = bool(rip_first)

    def best_fit_in_bin(self, bin_state, cut, *, increment_search):
        free_rects = bin_state["free_rects"]
        if not free_rects:
            return None

        best = None
        best_score = None
        for rect_idx, rect in enumerate(free_rects):
            for fit_w, fit_h, rotated in self._orientation_options(cut):
                increment_search()
                placement = self._make_placement(
                    rect_idx=rect_idx,
                    rect=rect,
                    fit_w=fit_w,
                    fit_h=fit_h,
                    rotated=rotated,
                )
                # The actual piece (fit_w/fit_h) must physically fit the rect.
                # used_w/used_h include kerf and get clamped to the rect, so they
                # must NOT be used for the fit test.
                if rect["w"] < placement["fit_w"] or rect["h"] < placement["fit_h"]:
                    continue

                rot = 1 if rotated else 0
                if self.rip_first:
                    # Horizontal strips (rows): a piece that fills the rect HEIGHT
                    # (within a kerf) continues a row; new rows open in the widest
                    # (full-width) fresh region, topmost first.
                    slack = int(rect["h"]) - int(fit_h)
                    if slack <= self.kerf_mm:
                        score = (0, 0, int(rect["y"]), int(rect["x"]), 0, rot)
                    else:
                        score = (1, -int(rect["w"]), int(rect["y"]), int(rect["x"]), slack, rot)
                else:
                    # Vertical strips (columns): a piece that fills the rect WIDTH
                    # continues a column; new columns open in the tallest
                    # (full-height) fresh region, leftmost first.
                    slack = int(rect["w"]) - int(fit_w)
                    if slack <= self.kerf_mm:
                        score = (0, 0, int(rect["x"]), int(rect["y"]), 0, rot)
                    else:
                        score = (1, -int(rect["h"]), int(rect["x"]), int(rect["y"]), slack, rot)

                if best is None or score < best_score:
                    best = placement
                    best_score = score
        return best

    def _split_rect(self, rect, placement):
        if self.rip_first:
            # Horizontal-cut-first (rip a full-width band the height of the piece):
            #   bottom -> the rest of the sheet, full width (future rows)
            #   right  -> the remainder of THIS row, to the right of the piece
            bottom = {
                "x": rect["x"],
                "y": rect["y"] + placement["used_h"],
                "w": rect["w"],
                "h": rect["h"] - placement["used_h"],
            }
            right = {
                "x": rect["x"] + placement["used_w"],
                "y": rect["y"],
                "w": rect["w"] - placement["used_w"],
                "h": placement["used_h"],
            }
            return [r for r in (bottom, right) if r["w"] > 0 and r["h"] > 0]

        # Vertical-cut-first (rip a full-height strip the width of the piece):
        #   right  -> the rest of the sheet, full height (future columns)
        #   bottom -> the remainder of THIS column, below the piece
        right = {
            "x": rect["x"] + placement["used_w"],
            "y": rect["y"],
            "w": rect["w"] - placement["used_w"],
            "h": rect["h"],
        }
        bottom = {
            "x": rect["x"],
            "y": rect["y"] + placement["used_h"],
            "w": placement["used_w"],
            "h": rect["h"] - placement["used_h"],
        }
        return [r for r in (right, bottom) if r["w"] > 0 and r["h"] > 0]

    def apply_placement(self, bin_state, placement, cut):
        rect = bin_state["free_rects"].pop(placement["rect_idx"])
        new_rects = self._split_rect(rect, placement)
        bin_state["free_rects"].extend(new_rects)
        bin_state["free_rects"] = self._prune_free_rects(bin_state["free_rects"])
        self._append_placement(bin_state, placement, cut)
