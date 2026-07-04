"""Derive guillotine and panel-saw cut lines for a nested sheet.

Given the placed pieces on a sheet, returns the straight, edge-to-edge
guillotine cuts that produce them. These same lines also bound every leftover
region into a clean rectangle, so feeding them to the DXF makes the offcuts come
out rectangular.

This is a true recursive guillotine derivation: within a rectangular region we
look for one straight cut (vertical or horizontal) that spans the region edge to
edge WITHOUT passing through any piece, split the region in two along it, and
recurse into each half. A cut is only ever emitted across the sub-region it
actually splits — so a cut never runs through a piece, even when a wide piece
spans several narrower pieces beneath it (the classic rotated-panel-on-top-of-a-
column-group case).

Pure geometry, no Odoo deps. Caller's coordinate convention (top-left or
bottom-left) is preserved: cuts come back in the same frame as the input rects.
"""


def _norm_segment(x1, y1, x2, y2):
    x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
    if (x2, y2) < (x1, y1):
        x1, y1, x2, y2 = x2, y2, x1, y1
    return x1, y1, x2, y2


def _transpose_rects(rects):
    return [(int(y), int(x), int(h), int(w)) for (x, y, w, h) in rects]


def _untranspose_segment(seg):
    x1, y1, x2, y2 = seg
    return _norm_segment(y1, x1, y2, x2)


def _bbox(rects):
    return (
        min(x for x, _y, _w, _h in rects),
        min(y for _x, y, _w, _h in rects),
        max(x + w for x, _y, w, _h in rects),
        max(y + h for _x, y, _w, h in rects),
    )


def _best_empty_margin(rx0, ry0, rx1, ry1, members, *, tol=1):
    """Return the largest clean outside margin around members, if one exists."""
    if not members:
        return None

    mnx, mny, mxx, mxy = _bbox(members)
    candidates = []

    def add(name, cut, offcut, keep):
        ox0, oy0, ox1, oy1 = offcut
        w = max(0, ox1 - ox0)
        h = max(0, oy1 - oy0)
        if w <= tol or h <= tol:
            return
        area = w * h
        candidates.append((
            (
                area,
                min(w, h),
                max(w, h),
                1 if (w == rx1 - rx0 or h == ry1 - ry0) else 0,
            ),
            {
                "name": name,
                "cut": cut,
                "offcut": offcut,
                "keep": keep,
            },
        ))

    if mxy < ry1 - tol:
        add(
            "top",
            (rx0, mxy, rx1, mxy),
            (rx0, mxy, rx1, ry1),
            (rx0, ry0, rx1, mxy),
        )
    if mxx < rx1 - tol:
        add(
            "right",
            (mxx, ry0, mxx, ry1),
            (mxx, ry0, rx1, ry1),
            (rx0, ry0, mxx, ry1),
        )
    if mnx > rx0 + tol:
        add(
            "left",
            (mnx, ry0, mnx, ry1),
            (rx0, ry0, mnx, ry1),
            (mnx, ry0, rx1, ry1),
        )
    if mny > ry0 + tol:
        add(
            "bottom",
            (rx0, mny, rx1, mny),
            (rx0, ry0, rx1, mny),
            (rx0, mny, rx1, ry1),
        )

    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _panel_saw_axis_plan(rects, sheet_w, sheet_h, *, tol=1):
    """3-stage panel-saw estimate for one orientation.

    The algorithm models a practical saw sequence: trim outer margins, rip into
    vertical stacks, crosscut each stack into bands, then separate pieces within
    each band. It assumes the layouts produced by the guillotine optimiser:
    clean rows/columns with shared x/y coordinates.
    """
    rects = [(int(x), int(y), int(w), int(h)) for (x, y, w, h) in rects if w and h]
    if not rects:
        return {
            "segments": [],
            "saw_operations": 0,
            "trim_cuts": 0,
            "separation_cuts": 0,
            "fence_settings": 0,
        }

    segments = {}
    fence_values = set()

    def add(x1, y1, x2, y2, kind, fence_value=None):
        if abs(x1 - x2) <= tol and abs(y1 - y2) <= tol:
            return
        seg = _norm_segment(x1, y1, x2, y2)
        current = segments.get(seg)
        if current != "separate":
            segments[seg] = kind
        if fence_value is not None and fence_value > tol:
            fence_values.add(int(round(fence_value)))

    min_x = min(x for x, _y, _w, _h in rects)
    max_x = max(x + w for x, _y, w, _h in rects)
    if min_x > tol:
        add(min_x, 0, min_x, sheet_h, "trim", min_x)
    if max_x < sheet_w - tol:
        add(max_x, 0, max_x, sheet_h, "trim", sheet_w - max_x)

    stacks = {}
    for rect in rects:
        stacks.setdefault(rect[0], []).append(rect)
    stack_xs = sorted(stacks)
    for x in stack_xs[1:]:
        add(x, 0, x, sheet_h, "separate", x)

    for stack_x in stack_xs:
        strip = stacks[stack_x]
        stack_x0 = min(x for x, _y, _w, _h in strip)
        stack_x1 = max(x + w for x, _y, w, _h in strip)
        stack_y0 = min(y for _x, y, _w, _h in strip)
        stack_y1 = max(y + h for _x, y, _w, h in strip)
        if stack_y0 > tol:
            add(stack_x0, stack_y0, stack_x1, stack_y0, "trim", stack_y0)
        if stack_y1 < sheet_h - tol:
            add(stack_x0, stack_y1, stack_x1, stack_y1, "trim", sheet_h - stack_y1)

        bands = {}
        for rect in strip:
            bands.setdefault(rect[1], []).append(rect)
        band_ys = sorted(bands)
        for y in band_ys[1:]:
            add(stack_x0, y, stack_x1, y, "separate", y)

        for band_y in band_ys:
            row = sorted(bands[band_y], key=lambda r: r[0])
            row_x0 = min(x for x, _y, _w, _h in row)
            row_x1 = max(x + w for x, _y, w, _h in row)
            row_y0 = min(y for _x, y, _w, _h in row)
            row_y1 = max(y + h for _x, y, _w, h in row)
            if row_x0 > stack_x0 + tol:
                add(row_x0, row_y0, row_x0, row_y1, "trim", row_x0 - stack_x0)
            if row_x1 < stack_x1 - tol:
                add(row_x1, row_y0, row_x1, row_y1, "trim", stack_x1 - row_x1)
            for rect in row[1:]:
                x = rect[0]
                add(x, row_y0, x, row_y1, "separate", x - row_x0)

            for _x, _y, w, h in row:
                if w > tol:
                    fence_values.add(int(w))
                if h > tol:
                    fence_values.add(int(h))

    ordered = sorted(segments.items(), key=lambda item: (item[0][0], item[0][1], item[0][2], item[0][3]))
    trim_cuts = sum(1 for _seg, kind in ordered if kind == "trim")
    separation_cuts = len(ordered) - trim_cuts
    return {
        "segments": [seg for seg, _kind in ordered],
        "saw_operations": len(ordered),
        "trim_cuts": trim_cuts,
        "separation_cuts": separation_cuts,
        "fence_settings": len(fence_values),
    }


def panel_saw_cut_metrics(rects, sheet_w, sheet_h, *, tol=1):
    """Return canonical factory-facing cut metrics for a sheet.

    Both column-first and row-first strategies are estimated; the cheaper one is
    returned because a panel-saw operator can choose either orientation.
    """
    sheet_w = int(sheet_w)
    sheet_h = int(sheet_h)
    rects = [(int(x), int(y), int(w), int(h)) for (x, y, w, h) in rects if w and h]
    if not rects:
        return {
            "segments": [],
            "saw_operations": 0,
            "trim_cuts": 0,
            "separation_cuts": 0,
            "fence_settings": 0,
            "orientation": "none",
        }

    column_plan = _panel_saw_axis_plan(rects, sheet_w, sheet_h, tol=tol)
    column_plan["orientation"] = "column_first"

    row_plan = _panel_saw_axis_plan(_transpose_rects(rects), sheet_h, sheet_w, tol=tol)
    row_plan = dict(row_plan)
    row_plan["segments"] = [_untranspose_segment(seg) for seg in row_plan["segments"]]
    row_plan["orientation"] = "row_first"

    def key(plan):
        return (
            int(plan["saw_operations"]),
            int(plan["trim_cuts"]),
            int(plan["fence_settings"]),
            0 if plan["orientation"] == "column_first" else 1,
        )

    return min((column_plan, row_plan), key=key)


def panel_saw_cut_lines(rects, sheet_w, sheet_h, *, tol=1):
    return guillotine_cut_lines(rects, sheet_w, sheet_h, tol=tol)


def guillotine_cut_lines(rects, sheet_w, sheet_h, *, tol=1):
    """rects: iterable of (x, y, w, h) in mm. Returns a de-duplicated list of
    cut segments (x1, y1, x2, y2), each a single guillotine cut clipped to the
    region it splits (so no segment ever crosses a piece)."""
    sheet_w = int(sheet_w)
    sheet_h = int(sheet_h)
    rects = [(int(x), int(y), int(w), int(h)) for (x, y, w, h) in rects if w and h]
    if not rects:
        return []

    segs = set()

    def overlaps_1d(a0, a1, b0, b1):
        # open-interval overlap: touching edges don't count
        return a0 < b1 - tol and b0 < a1 - tol

    def recurse(rx0, ry0, rx1, ry1, members):
        # members: pieces (x, y, w, h) fully inside this region.
        if not members:
            return

        margin = _best_empty_margin(rx0, ry0, rx1, ry1, members, tol=tol)
        if margin:
            segs.add(margin["cut"])
            recurse(*margin["keep"], members)
            return

        # Candidate vertical cut positions: every piece's right/left edge that
        # lies strictly inside the region.
        v_cands = set()
        for (x, y, w, h) in members:
            if rx0 + tol < x < rx1 - tol:
                v_cands.add(x)
            if rx0 + tol < x + w < rx1 - tol:
                v_cands.add(x + w)
        h_cands = set()
        for (x, y, w, h) in members:
            if ry0 + tol < y < ry1 - tol:
                h_cands.add(y)
            if ry0 + tol < y + h < ry1 - tol:
                h_cands.add(y + h)

        # A vertical cut at cx is valid only if no member straddles it.
        def valid_v(cx):
            for (x, y, w, h) in members:
                if x + tol < cx < x + w - tol:
                    return False
            return True

        def valid_h(cy):
            for (x, y, w, h) in members:
                if y + tol < cy < y + h - tol:
                    return False
            return True

        def bbox(members_subset):
            return (
                min(m[0] for m in members_subset),
                min(m[1] for m in members_subset),
                max(m[0] + m[2] for m in members_subset),
                max(m[1] + m[3] for m in members_subset),
            )

        def edge_support_v(cx):
            count = 0
            span = 0
            for (x, y, w, h) in members:
                if abs(x - cx) <= tol or abs(x + w - cx) <= tol:
                    count += 1
                    span += max(0, min(y + h, ry1) - max(y, ry0))
            return count, span

        def edge_support_h(cy):
            count = 0
            span = 0
            for (x, y, w, h) in members:
                if abs(y - cy) <= tol or abs(y + h - cy) <= tol:
                    count += 1
                    span += max(0, min(x + w, rx1) - max(x, rx0))
            return count, span

        def child_tightness(child_specs):
            score = 0
            for (cx0, cy0, cx1, cy1, child_members) in child_specs:
                if not child_members:
                    continue
                mx0, my0, mx1, my1 = bbox(child_members)
                score -= (mx0 - cx0) + (my0 - cy0) + (cx1 - mx1) + (cy1 - my1)
            return score

        candidates = []
        for cx in sorted(v_cands):
            if not valid_v(cx):
                continue
            left = [m for m in members if m[0] + m[2] <= cx + tol]
            right = [m for m in members if m[0] >= cx - tol]
            if not left or not right:
                continue
            support_count, support_span = edge_support_v(cx)
            candidates.append((
                (
                    support_count,
                    support_span,
                    child_tightness((
                        (rx0, ry0, cx, ry1, left),
                        (cx, ry0, rx1, ry1, right),
                    )),
                    -abs(len(left) - len(right)),
                    -(ry1 - ry0),
                    0,
                    -cx,
                ),
                "v",
                cx,
                left,
                right,
            ))

        for cy in sorted(h_cands):
            if not valid_h(cy):
                continue
            bottom = [m for m in members if m[1] + m[3] <= cy + tol]
            top = [m for m in members if m[1] >= cy - tol]
            if not bottom or not top:
                continue
            support_count, support_span = edge_support_h(cy)
            candidates.append((
                (
                    support_count,
                    support_span,
                    child_tightness((
                        (rx0, ry0, rx1, cy, bottom),
                        (rx0, cy, rx1, ry1, top),
                    )),
                    -abs(len(bottom) - len(top)),
                    -(rx1 - rx0),
                    1,
                    -cy,
                ),
                "h",
                cy,
                bottom,
                top,
            ))

        if candidates:
            _score, axis, pos, first, second = max(candidates, key=lambda item: item[0])
            if axis == "v":
                segs.add((pos, ry0, pos, ry1))
                recurse(rx0, ry0, pos, ry1, first)
                recurse(pos, ry0, rx1, ry1, second)
            else:
                segs.add((rx0, pos, rx1, pos))
                recurse(rx0, ry0, rx1, pos, first)
                recurse(rx0, pos, rx1, ry1, second)
            return

        # No interior cut splits the members (single piece, or a non-guillotine
        # arrangement). Trim the region down to the piece(s) so leftover material
        # is still bounded into a rectangle: cut along the union bounding edges of
        # the members where they fall short of the region.
        mx0 = min(m[0] for m in members)
        my0 = min(m[1] for m in members)
        mx1 = max(m[0] + m[2] for m in members)
        my1 = max(m[1] + m[3] for m in members)
        if mx1 < rx1 - tol:
            segs.add((mx1, ry0, mx1, ry1))
        if my1 < ry1 - tol:
            segs.add((rx0, my1, mx1, my1))
        if mx0 > rx0 + tol:
            segs.add((mx0, ry0, mx0, ry1))
        if my0 > ry0 + tol:
            segs.add((rx0, my0, mx1, my0))

    recurse(0, 0, sheet_w, sheet_h, rects)
    return sorted(_norm_segment(*seg) for seg in segs)


def offcut_rects(rects, sheet_w, sheet_h, *, tol=1, min_side=1):
    """Return the leftover (empty) rectangles a guillotine cutting of this layout
    produces — i.e. the physical offcuts. Same recursion as guillotine_cut_lines,
    but emits the empty sub-regions instead of the cut segments. Each offcut is a
    clean rectangle (x, y, w, h). `min_side` filters out slivers below that size.
    """
    sheet_w = int(sheet_w)
    sheet_h = int(sheet_h)
    rects = [(int(x), int(y), int(w), int(h)) for (x, y, w, h) in rects if w and h]
    out = []

    def emit(x0, y0, x1, y1):
        w, h = x1 - x0, y1 - y0
        if w > min_side and h > min_side:
            out.append((int(x0), int(y0), int(w), int(h)))

    def recurse(rx0, ry0, rx1, ry1, members):
        if rx1 - rx0 <= tol or ry1 - ry0 <= tol:
            return
        if not members:
            emit(rx0, ry0, rx1, ry1)
            return
        # FIRST peel off any empty margin around the pieces' bounding box as a
        # single big offcut (one rip), THEN recurse into the tight bbox. This
        # consolidates the leftover into the largest possible rectangles instead
        # of slicing the margin into many strips (e.g. the right-hand offcut of a
        # column of stacked bands comes out as ONE rectangle, not one per band).
        margin = _best_empty_margin(rx0, ry0, rx1, ry1, members, tol=tol)
        if margin:
            emit(*margin["offcut"])
            recurse(*margin["keep"], members)
            return
        # Try a vertical guillotine cut that crosses no piece.
        vx = set()
        for (x, y, w, h) in members:
            if rx0 + tol < x < rx1 - tol:
                vx.add(x)
            if rx0 + tol < x + w < rx1 - tol:
                vx.add(x + w)
        for cx in sorted(vx):
            if all(not (m[0] + tol < cx < m[0] + m[2] - tol) for m in members):
                left = [m for m in members if m[0] + m[2] <= cx + tol]
                right = [m for m in members if m[0] >= cx - tol]
                if left and right:
                    recurse(rx0, ry0, cx, ry1, left)
                    recurse(cx, ry0, rx1, ry1, right)
                    return
        # Then a horizontal one.
        hy = set()
        for (x, y, w, h) in members:
            if ry0 + tol < y < ry1 - tol:
                hy.add(y)
            if ry0 + tol < y + h < ry1 - tol:
                hy.add(y + h)
        for cy in sorted(hy):
            if all(not (m[1] + tol < cy < m[1] + m[3] - tol) for m in members):
                bottom = [m for m in members if m[1] + m[3] <= cy + tol]
                top = [m for m in members if m[1] >= cy - tol]
                if bottom and top:
                    recurse(rx0, ry0, rx1, cy, bottom)
                    recurse(rx0, cy, rx1, ry1, top)
                    return
        # No interior cut: the region is bounded by the piece bbox; the surrounding
        # margins are offcuts.
        mx1 = max(m[0] + m[2] for m in members)
        my1 = max(m[1] + m[3] for m in members)
        if mx1 < rx1 - tol:
            recurse(mx1, ry0, rx1, ry1, [])
        if my1 < ry1 - tol:
            recurse(rx0, my1, mx1, ry1, [])

    recurse(0, 0, sheet_w, sheet_h, rects)
    return out
