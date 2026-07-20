"""V2 CutList-style guillotine pattern constructor.

This is a clean-room, pure-Python (no Odoo deps) guillotine layout *constructor*.
It builds saw-friendly sheet-pattern candidates directly, then uses deterministic
beam search to choose the best complete layout. The production goal is the same
shape of layout CutListOptimiser tends to produce:

  1. CLASSIFY the job into anchors (big/awkward one-offs), fillers (repeated small
     parts) and side-strips (long narrow parts).
  2. Place anchors first, biggest first, bottom-left into the current sheet.
  3. After each anchor, the leftover region is split into clean guillotine
     sub-rectangles (right strip + top strip). Feed FILLER parts into those
     strips as same-width stacks — this is the "repeated parts as packing sand"
     behaviour that absorbs dead space instead of ghettoising the small parts.
  4. Side-strips pair with anchors of complementary height.
  5. Open a new sheet only when nothing more fits the current one.

Every region is split by straight guillotine cuts, so every layout it produces is
panel-saw cuttable by construction. Output is a list of sheets, each a list of
placements {item, x, y, w, h, rotated} in the sheet's bottom-left frame.

The only public production search is `search_v2_orientations`.  `search_v3_*`
is a shadow/profiling path only; Odoo production callers must not route to it
until benchmarks prove parity.
"""
from collections import Counter
from functools import lru_cache
from itertools import combinations, product
import time


def _deadline_hit(deadline, reserve=0.0):
    """True when a bounded search should stop before starting more work."""
    return bool(deadline and time.monotonic() + float(reserve or 0.0) >= deadline)


def _elapsed_ms(started_at):
    return max(0, int((time.monotonic() - started_at) * 1000))


def _record_strategy_metric(stats, name, runtime_ms, candidate_count):
    if stats is None:
        return
    runtime = dict(stats.get("strategy_runtime_ms") or {})
    counts = dict(stats.get("strategy_candidate_counts") or {})
    runtime[name] = int(runtime.get(name) or 0) + int(runtime_ms or 0)
    counts[name] = int(counts.get(name) or 0) + int(candidate_count or 0)
    stats["strategy_runtime_ms"] = runtime
    stats["strategy_candidate_counts"] = counts


def _cancel_requested(callback):
    if not callback:
        return False
    try:
        return bool(callback())
    except Exception:
        return True


def _area(p):
    return p["w"] * p["h"]


def classify(pieces, *, sheet_w, sheet_h):
    """Split pieces into (anchors, fillers, side_strips).

    pieces: list of dicts {id, w, h}. Returns three lists. Heuristic:
      - fillers   = piece TYPES with a high repeat count (>= _FILLER_MIN_QTY)
      - side_strips = long & narrow (aspect >= _STRIP_ASPECT) one-offs/low-count
      - anchors   = everything else (the big awkward panels)
    """
    _FILLER_MIN_QTY = 4
    _STRIP_ASPECT = 3.0

    # count by (w,h) unordered so rotations of the same part group together
    key = lambda p: tuple(sorted((p["w"], p["h"])))
    counts = Counter(key(p) for p in pieces)

    anchors, fillers, side_strips = [], [], []
    for p in pieces:
        k = key(p)
        long_side, short_side = max(p["w"], p["h"]), min(p["w"], p["h"])
        if counts[k] >= _FILLER_MIN_QTY:
            fillers.append(p)
        elif long_side >= _STRIP_ASPECT * short_side:
            side_strips.append(p)
        else:
            anchors.append(p)
    return anchors, fillers, side_strips


class _Sheet:
    """One sheet being built. Tracks free rectangles (guillotine sub-regions)."""

    def __init__(self, w, h, kerf):
        self.w, self.h, self.kerf = w, h, kerf
        self.placements = []          # {item, x, y, w, h, rotated}
        self.free = [(0, 0, w, h)]    # free rectangles (x, y, w, h)

    def _orientations(self, p):
        yield p["w"], p["h"], False
        if p["w"] != p["h"]:
            yield p["h"], p["w"], True

    def _place_in(self, rect, pw, ph, p, rotated):
        rx, ry, rw, rh = rect
        self.placements.append({"item": p["id"], "x": rx, "y": ry,
                                "w": pw, "h": ph, "rotated": rotated})
        self.free.remove(rect)
        # Guillotine split — choose the direction that keeps the LARGER leftover
        # rectangle WHOLE, so dead space stays consolidated into big reusable
        # blocks instead of being shredded into strips. Two options:
        #   A) horizontal cut at piece top: right(piece-height) + top(full-width)
        #   B) vertical cut at piece right: right(full-height) + top(piece-width)
        # Pick whichever yields the bigger single child rectangle.
        right_a = (rx + pw + self.kerf, ry, rw - pw - self.kerf, ph)
        top_a = (rx, ry + ph + self.kerf, rw, rh - ph - self.kerf)
        right_b = (rx + pw + self.kerf, ry, rw - pw - self.kerf, rh)
        top_b = (rx, ry + ph + self.kerf, pw, rh - ph - self.kerf)
        big_a = max(right_a[2] * right_a[3], top_a[2] * top_a[3])
        big_b = max(right_b[2] * right_b[3], top_b[2] * top_b[3])
        children = (right_b, top_b) if big_b >= big_a else (right_a, top_a)
        for r in children:
            if r[2] > 0 and r[3] > 0:
                self.free.append(r)
        # keep free rects sorted bottom-left so fillers go into low corners first
        self.free.sort(key=lambda r: (r[1], r[0]))

    def try_place(self, p, *, prefer_smallest=True):
        """Place piece p into the best free rect (smallest that fits = tightest).
        Returns True if placed."""
        best = None  # (waste, rect, pw, ph, rotated)
        for rect in self.free:
            rw, rh = rect[2], rect[3]
            for pw, ph, rot in self._orientations(p):
                if pw <= rw and ph <= rh:
                    waste = rw * rh - pw * ph
                    if best is None or waste < best[0]:
                        best = (waste, rect, pw, ph, rot)
        if best is None:
            return False
        _w, rect, pw, ph, rot = best
        self._place_in(rect, pw, ph, p, rot)
        return True

    def fill_strip(self, p, max_count):
        """Greedily stack as many copies of piece p (same dims) into ONE free
        rect as fit, in a same-width column — the CutList filler strip. Returns
        the count placed. Caller passes identical-dim copies via max_count."""
        placed = 0
        # pick the free rect that fits the most copies of p (best filler home)
        while placed < max_count:
            if not self.try_place(p):
                break
            placed += 1
        return placed

    def utilisation(self):
        used = sum(_area(pl) for pl in self.placements)
        return used / float(self.w * self.h) if self.w and self.h else 0.0


def construct(pieces, sheet_w, sheet_h, *, kerf=3, seed=0, deadline=None):
    """Build sheets from `pieces` (list of {id, w, h}). Anchor-first, then feed
    filler parts into the dead space, then side-strips, open new sheets as
    needed. Returns list of _Sheet (each with .placements).

    `seed` deterministically varies the anchor ORDER (different orders create
    differently-shaped dead space, so the search can find the arrangement where
    everything fits in fewer sheets). seed=0 is the plain biggest-first pass."""
    import random
    rng = random.Random(seed)
    anchors, fillers, side_strips = classify(pieces, sheet_w=sheet_w, sheet_h=sheet_h)

    # Biggest awkward panels first; side-strips after anchors; fillers last (they
    # absorb whatever dead space remains).
    anchors.sort(key=_area, reverse=True)
    side_strips.sort(key=_area, reverse=True)
    if seed:
        # Perturb the anchor order: keep a big-first BIAS but jitter each piece's
        # effective area by up to +-35% so different seeds explore meaningfully
        # different placement orders (and therefore different dead-space shapes).
        anchors.sort(key=lambda p: -_area(p) * (1.0 + rng.uniform(-0.35, 0.35)))
        rng.shuffle(side_strips)

    # group fillers by unordered dims so we can stack identical ones
    filler_groups = {}
    for p in fillers:
        k = tuple(sorted((p["w"], p["h"])))
        filler_groups.setdefault(k, []).append(p)

    sheets = []

    def new_sheet():
        s = _Sheet(sheet_w, sheet_h, kerf)
        sheets.append(s)
        return s

    cur = new_sheet()

    # 1) Place ALL anchors + side-strips FIRST, across sheets. Crucially, before
    #    opening a NEW sheet we try every existing sheet — anchors co-locate
    #    instead of stranding a near-empty sheet (Phase 1 bug: sheet at 22%).
    unplaced = []
    pending_anchors = list(anchors) + list(side_strips)
    for p_idx, p in enumerate(pending_anchors):
        if _deadline_hit(deadline):
            unplaced.extend(pending_anchors[p_idx:])
            return sheets, unplaced
        if any(s.try_place(p) for s in sheets):
            continue
        cur = new_sheet()
        if not cur.try_place(p):
            unplaced.append(p)        # doesn't fit even an empty sheet — record it

    # 2) Backfill fillers into the dead space the anchors left — FILL EACH SHEET
    #    AS FULL AS POSSIBLE before moving to the next. This consolidates the
    #    leftover: early sheets get packed solid with filler absorbing their dead
    #    space, and whatever's left pools onto the LAST sheet as ONE big clean
    #    offcut (the CutList 659x2440 reusable sheet) instead of being smeared as
    #    little gaps across every sheet.
    remaining = [p for group in filler_groups.values() for p in group]
    for s in sheets:
        if not remaining:
            break
        # dump as many fillers as physically fit into THIS sheet's dead space
        progress = True
        while remaining and progress:
            if _deadline_hit(deadline):
                unplaced.extend(remaining)
                return sheets, unplaced
            progress = False
            for idx, p in enumerate(remaining):
                if s.try_place(p):
                    remaining.pop(idx)
                    progress = True
                    break

    # 3) Anything still unplaced (fillers that didn't fit any existing dead
    #    space): open fresh sheets and pack them as strips.
    while remaining:
        if _deadline_hit(deadline):
            unplaced.extend(remaining)
            break
        cur = new_sheet()
        placed_any = False
        kept = []
        for p in remaining:
            if cur.try_place(p):
                placed_any = True
            else:
                kept.append(p)
        remaining = kept
        if not placed_any:
            unplaced.extend(remaining)  # genuinely can't place — record, don't drop
            break

    return sheets, unplaced


# --------------------------------------------------------------------------
# Shelf/column pattern constructor
# --------------------------------------------------------------------------
def _piece_type_key(p):
    return tuple(sorted((int(p["w"]), int(p["h"]))))


def _repeat_keys(pieces, min_qty=6):
    counts = Counter(_piece_type_key(p) for p in pieces)
    return {key for key, qty in counts.items() if qty >= min_qty}


def _verticalized_item(p, filler_keys, sheet_w, sheet_h):
    """Orient a piece for CutList-style portrait strip work.

    Repeated fillers are made short/tall-stack friendly (290x250 for a
    250x290 part). Long narrow side strips stay narrow. Wide-but-short parts
    become shelves. Everything else uses its narrower side as the column width.
    """
    w, h = int(p["w"]), int(p["h"])
    short, long = sorted((w, h))
    key = _piece_type_key(p)
    aspect = float(long) / float(short or 1)

    if key in filler_keys:
        ow, oh = long, short
        role = "filler"
    elif aspect >= 3.0 and short <= max(260, int(sheet_w * 0.30)):
        ow, oh = short, long
        role = "side"
    elif short <= int(sheet_w * 0.42) and long >= int(sheet_w * 0.78) and long <= sheet_w:
        ow, oh = long, short
        role = "wide"
    else:
        ow, oh = short, long
        role = "anchor"

    if (ow > sheet_w or oh > sheet_h) and h <= sheet_w and w <= sheet_h:
        ow, oh = h, w
    if ow > sheet_w or oh > sheet_h:
        ow, oh = w, h

    return {
        "id": p["id"],
        "orig_w": w,
        "orig_h": h,
        "w": int(ow),
        "h": int(oh),
        "rotated": int(ow) != w or int(oh) != h,
        "role": role,
        "key": key,
    }


def _stack_extent(stack, kerf):
    if not stack:
        return 0
    return sum(i["h"] for i in stack) + kerf * (len(stack) - 1)


def _stack_area(stack):
    return sum(i["w"] * i["h"] for i in stack)


def _best_stack(seed, candidates, max_h, kerf):
    """Best vertical stack starting with seed, keeping the seed's width."""
    fitting = [
        i for i in candidates
        if i is not seed and i["w"] <= seed["w"]
    ]
    # Exhaustive is cheap for the benchmark-sized anchor set; cap keeps it sane.
    if len(fitting) > 14:
        fitting = sorted(fitting, key=lambda i: (i["w"] * i["h"], i["h"]), reverse=True)[:14]

    best = [seed]
    best_key = (seed["w"] * seed["h"], seed["h"], -1)
    for n in range(1, len(fitting) + 1):
        for subset in combinations(fitting, n):
            stack = [seed] + list(subset)
            height = _stack_extent(stack, kerf)
            if height > max_h:
                continue
            key = (_stack_area(stack), height, -len(stack))
            if key > best_key:
                best_key = key
                best = stack
    # Put wider/taller items lower; this mirrors the CutList-looking columns.
    return [seed] + sorted([i for i in best if i is not seed], key=lambda i: (i["w"], i["h"]), reverse=True)


def _best_side_stack(side_items, width_limit, height_limit, kerf, deadline=None):
    fitting = [i for i in side_items if i["w"] <= width_limit]
    if len(fitting) > 14:
        fitting = sorted(
            fitting,
            key=lambda i: (i["w"] * i["h"], i["h"], -i["w"]),
            reverse=True,
        )[:14]
    best = []
    best_key = (0, 0)
    for n in range(1, len(fitting) + 1):
        if _deadline_hit(deadline, 0.02):
            break
        for subset in combinations(fitting, n):
            if _deadline_hit(deadline, 0.02):
                break
            stack = sorted(subset, key=lambda i: i["h"], reverse=True)
            height = _stack_extent(stack, kerf)
            if height > height_limit:
                continue
            key = (_stack_area(stack), height)
            if key > best_key:
                best_key = key
                best = list(stack)
    return best


def _filler_stack(fillers, width_limit, height_limit, kerf):
    if not fillers:
        return []
    groups = {}
    for item in fillers:
        groups.setdefault((item["w"], item["h"]), []).append(item)
    best = []
    best_key = (0, 0, 0)
    for (w, h), group in groups.items():
        if w > width_limit:
            continue
        max_count = min(len(group), (height_limit + kerf) // (h + kerf))
        if max_count <= 0:
            continue
        stack = group[:max_count]
        key = (_stack_area(stack), max_count, -w)
        if key > best_key:
            best_key = key
            best = stack
    return best


def _add_stack(sheet, stack, x, y, kerf):
    top = y
    for item in stack:
        sheet.placements.append({
            "item": item["id"],
            "x": int(x),
            "y": int(top),
            "w": int(item["w"]),
            "h": int(item["h"]),
            "rotated": bool(item["rotated"]),
        })
        top += item["h"] + kerf
    return top - kerf if stack else y


def _set_column_free(sheet, columns, used_w, kerf):
    free = []
    for x, w, used_h in columns:
        top_y = used_h + kerf
        if top_y < sheet.h:
            free.append((int(x), int(top_y), int(w), int(sheet.h - top_y)))
    right_x = used_w + kerf
    if right_x < sheet.w:
        free.append((int(right_x), 0, int(sheet.w - right_x), int(sheet.h)))
    sheet.free = [r for r in free if r[2] > 0 and r[3] > 0]


def _remove_items(pool, chosen):
    chosen_ids = {id(i) for i in chosen}
    return [i for i in pool if id(i) not in chosen_ids]


def _make_two_column_sheet(anchors, companion_items, sheet_w, sheet_h, kerf, *, companion, deadline=None):
    if not anchors or not companion_items:
        return None, [], []
    best = None
    for seed in anchors:
        left = _best_stack(seed, anchors, sheet_h, kerf)
        left_w = max(i["w"] for i in left)
        right_w = sheet_w - left_w - kerf
        if right_w <= 0:
            continue
        if companion == "side":
            right = _best_side_stack(companion_items, right_w, sheet_h, kerf, deadline=deadline)
        else:
            right = _filler_stack(companion_items, right_w, sheet_h, kerf)
        if not right:
            continue
        placed_area = _stack_area(left) + _stack_area(right)
        height_balance = -abs(_stack_extent(left, kerf) - _stack_extent(right, kerf))
        key = (placed_area, height_balance, len(left) + len(right))
        if best is None or key > best[0]:
            best = (key, left, right)
    if best is None:
        return None, [], []

    _key, left, right = best
    sheet = _Sheet(sheet_w, sheet_h, kerf)
    sheet.placements = []
    left_w = max(i["w"] for i in left)
    right_w = max(i["w"] for i in right)
    left_h = _add_stack(sheet, left, 0, 0, kerf)
    right_x = left_w + kerf
    right_h = _add_stack(sheet, right, right_x, 0, kerf)
    _set_column_free(sheet, [(0, left_w, left_h), (right_x, right_w, right_h)], right_x + right_w, kerf)
    sheet.strategy = "two_column_%s" % companion
    return sheet, left, right


def _make_wide_shelf_sheet(items, sheet_w, sheet_h, kerf):
    wide = [i for i in items if i["role"] == "wide"]
    rest = [i for i in items if i["role"] != "wide"]
    if not wide and not rest:
        return None, [], []

    sheet = _Sheet(sheet_w, sheet_h, kerf)
    sheet.placements = []
    used = []
    y = 0
    for item in sorted(wide, key=lambda i: (i["w"], i["h"]), reverse=True):
        if item["w"] > sheet_w or y + item["h"] > sheet_h:
            continue
        sheet.placements.append({
            "item": item["id"],
            "x": 0,
            "y": int(y),
            "w": int(item["w"]),
            "h": int(item["h"]),
            "rotated": bool(item["rotated"]),
        })
        used.append(item)
        y += item["h"] + kerf

    top_y = y
    top_h = sheet_h - top_y
    x = 0
    columns = []
    remaining = _remove_items(rest, [])

    # Stack non-fillers first, then repeated fillers in tidy columns.
    while top_h > 0:
        anchors = [i for i in remaining if i["role"] not in ("filler", "wide") and i["w"] <= sheet_w - x]
        if not anchors:
            break
        seed = max(anchors, key=lambda i: (i["w"] * i["h"], i["h"]))
        stack = _best_stack(seed, anchors, top_h, kerf)
        col_w = max(i["w"] for i in stack)
        if x + col_w > sheet_w:
            break
        used_h = _add_stack(sheet, stack, x, top_y, kerf)
        columns.append((x, col_w, used_h))
        used.extend(stack)
        remaining = _remove_items(remaining, stack)
        x += col_w + kerf

    while top_h > 0:
        fillers = [i for i in remaining if i["role"] == "filler" and i["w"] <= sheet_w - x]
        stack = _filler_stack(fillers, sheet_w - x, top_h, kerf)
        if not stack:
            break
        col_w = max(i["w"] for i in stack)
        used_h = _add_stack(sheet, stack, x, top_y, kerf)
        columns.append((x, col_w, used_h))
        used.extend(stack)
        remaining = _remove_items(remaining, stack)
        x += col_w + kerf

    free = []
    bottom_used_w = max((i["w"] for i in used if i["role"] == "wide"), default=0)
    top_used_w = max((col_x + col_w for col_x, col_w, _used_h in columns), default=0)
    full_height_right_x = max(bottom_used_w, top_used_w) + kerf
    if full_height_right_x < sheet_w:
        free.append((int(full_height_right_x), 0, int(sheet_w - full_height_right_x), int(sheet_h)))
    for col_x, col_w, used_h in columns:
        fy = used_h + kerf
        if fy < sheet_h:
            free.append((int(col_x), int(fy), int(col_w), int(sheet_h - fy)))
    right_x = x
    if right_x < min(full_height_right_x, sheet_w):
        free.append((int(right_x), int(top_y), int(min(full_height_right_x, sheet_w) - right_x), int(top_h)))
    if not used and not sheet.placements:
        return None, [], items
    sheet.free = [r for r in free if r[2] > 0 and r[3] > 0]
    sheet.strategy = "wide_shelf"
    return sheet, used, remaining


def construct_shelf(pieces, sheet_w, sheet_h, *, kerf=3, seed=0, deadline=None):
    """Construct explicit CutList-like shelf/column patterns.

    This is intentionally conservative and sits beside the original constructor.
    It gives mixed jobs a way to create the observed CutList shapes: tall anchor
    columns with side strips or filler columns, followed by wide bottom shelves
    with smaller columns above them.
    """
    import random
    rng = random.Random(seed)
    filler_keys = _repeat_keys(pieces)
    remaining = [_verticalized_item(p, filler_keys, sheet_w, sheet_h) for p in pieces]
    sheets = []

    def take_sheet(sheet, chosen):
        nonlocal remaining
        if sheet and chosen:
            sheets.append(sheet)
            remaining = _remove_items(remaining, chosen)

    # Pair side strips with the best tall anchor column before fillers consume that
    # narrow companion space.
    side = [i for i in remaining if i["role"] == "side"]
    anchors = [i for i in remaining if i["role"] == "anchor"]
    if side and anchors and not _deadline_hit(deadline):
        sheet, left, right = _make_two_column_sheet(anchors, side, sheet_w, sheet_h, kerf, companion="side", deadline=deadline)
        take_sheet(sheet, left + right)

    # Then use repeated panels as the filler column beside another anchor stack.
    fillers = [i for i in remaining if i["role"] == "filler"]
    anchors = [i for i in remaining if i["role"] == "anchor"]
    if fillers and anchors and not _deadline_hit(deadline):
        # Seeded variation: occasionally let a different anchor stack try first by
        # shuffling equal-scoring candidates through the input order.
        rng.shuffle(anchors)
        sheet, left, right = _make_two_column_sheet(anchors, fillers, sheet_w, sheet_h, kerf, companion="filler", deadline=deadline)
        take_sheet(sheet, left + right)

    # Wide shelf sheet: 1060-wide bands at the bottom, smaller columns/fillers above.
    if not _deadline_hit(deadline):
        sheet, used, leftover = _make_wide_shelf_sheet(remaining, sheet_w, sheet_h, kerf)
        take_sheet(sheet, used)

    # Pack anything still left with the old free-rect constructor, but keep the
    # explicit oriented dimensions so this path remains deterministic.
    unplaced = []
    while remaining:
        if _deadline_hit(deadline):
            unplaced.extend(remaining)
            break
        sheet = _Sheet(sheet_w, sheet_h, kerf)
        sheet.placements = []
        y = x = col_w = col_h = 0
        placed = []
        for item in sorted(remaining, key=lambda i: (i["role"] != "filler", i["w"] * i["h"]), reverse=True):
            if x + item["w"] > sheet_w:
                x += col_w + kerf
                y = 0
                col_w = col_h = 0
            if x + item["w"] <= sheet_w and y + item["h"] <= sheet_h:
                sheet.placements.append({
                    "item": item["id"],
                    "x": int(x),
                    "y": int(y),
                    "w": int(item["w"]),
                    "h": int(item["h"]),
                    "rotated": bool(item["rotated"]),
                })
                placed.append(item)
                col_w = max(col_w, item["w"])
                col_h = max(col_h, y + item["h"])
                y += item["h"] + kerf
        if not placed:
            unplaced.extend(remaining)
            break
        sheet.free = []
        used_w = max(p["x"] + p["w"] for p in sheet.placements)
        used_h = max(p["y"] + p["h"] for p in sheet.placements)
        if used_w + kerf < sheet_w:
            sheet.free.append((used_w + kerf, 0, sheet_w - used_w - kerf, sheet_h))
        if used_h + kerf < sheet_h:
            sheet.free.append((0, used_h + kerf, used_w, sheet_h - used_h - kerf))
        sheet.strategy = "shelf_tail_pack"
        sheets.append(sheet)
        remaining = _remove_items(remaining, placed)

    return sheets, unplaced


# --------------------------------------------------------------------------
# Dominant repeated-part band constructor
# --------------------------------------------------------------------------
def _piece_orientations(p, max_w, max_h):
    w0, h0 = int(p["w"]), int(p["h"])
    seen = set()
    for w, h in ((w0, h0), (h0, w0)):
        if (w, h) in seen:
            continue
        seen.add((w, h))
        if w <= max_w and h <= max_h:
            yield {
                "id": p["id"],
                "orig": p,
                "w": int(w),
                "h": int(h),
                "rotated": int(w) != w0 or int(h) != h0,
            }


@lru_cache(maxsize=512)
def _band_rows_for_dims(sample_w, sample_h, count, sheet_w, sheet_h, kerf):
    if count <= 0:
        return [], 0
    sample = {"id": "_sample", "w": sample_w, "h": sample_h}
    options = list(_piece_orientations(sample, sheet_w, sheet_h))
    best = None

    def recurse(remaining, rows):
        nonlocal best
        if remaining == 0:
            used_h = sum(r["h"] for r in rows) + kerf * max(0, len(rows) - 1)
            if used_h > sheet_h:
                return
            max_row_w = max((r["count"] * r["w"] + kerf * max(0, r["count"] - 1) for r in rows), default=0)
            key = (used_h, len(rows), -max_row_w)
            if best is None or key < best[0]:
                best = (key, [dict(r) for r in rows], used_h)
            return
        if len(rows) >= 4:
            return
        for opt in options:
            cap = (sheet_w + kerf) // (opt["w"] + kerf)
            for qty in range(min(cap, remaining), 0, -1):
                rows.append({
                    "count": int(qty),
                    "w": int(opt["w"]),
                    "h": int(opt["h"]),
                    "rotated": bool(opt["rotated"]),
                })
                recurse(remaining - qty, rows)
                rows.pop()

    recurse(count, [])
    if best is None:
        return None, 0
    _key, rows, used_h = best
    return tuple(tuple(sorted(r.items())) for r in rows), used_h


def _band_rows_for_count(sample, count, sheet_w, sheet_h, kerf):
    rows, used_h = _band_rows_for_dims(
        int(sample["w"]), int(sample["h"]), int(count), int(sheet_w), int(sheet_h), int(kerf),
    )
    if rows is None:
        return None, 0
    return [dict(row) for row in rows], used_h


def _make_repeated_band_sheet(fillers, count, sheet_w, sheet_h, kerf, *, y0=0):
    if count <= 0:
        return None, [], 0
    chosen = list(fillers[:count])
    rows, used_h = _band_rows_for_count(chosen[0], count, sheet_w, sheet_h - y0, kerf)
    if not rows:
        return None, [], 0

    sheet = _Sheet(sheet_w, sheet_h, kerf)
    sheet.placements = []
    free = []
    if y0 > 0:
        bottom_h = int(y0 - kerf) if y0 > kerf else int(y0)
        if bottom_h > 0:
            free.append((0, 0, int(sheet_w), int(bottom_h)))
    idx = 0
    y = int(y0)
    for row in rows:
        x = 0
        row_w = row["count"] * row["w"] + kerf * max(0, row["count"] - 1)
        for _i in range(row["count"]):
            piece = chosen[idx]
            sheet.placements.append({
                "item": piece["id"],
                "x": int(x),
                "y": int(y),
                "w": int(row["w"]),
                "h": int(row["h"]),
                "rotated": bool(row["rotated"]),
            })
            x += row["w"] + kerf
            idx += 1
        if row_w + kerf < sheet_w:
            free.append((int(row_w + kerf), int(y), int(sheet_w - row_w - kerf), int(row["h"])))
        y += row["h"] + kerf

    top_y = int(y0 + used_h + kerf)
    if top_y < sheet_h:
        free.append((0, top_y, int(sheet_w), int(sheet_h - top_y)))
    sheet.free = [r for r in free if r[2] > 0 and r[3] > 0]
    sheet.strategy = "repeat_band"
    return sheet, chosen, used_h


def _pack_rows_in_rect(items, x0, y0, width, height, kerf):
    """Pack a small set of anchors into full-width shelf rows inside a rectangle."""
    if not items:
        return [], [], 0
    orders = [
        sorted(items, key=lambda p: int(p["w"]) * int(p["h"]), reverse=True),
        sorted(items, key=lambda p: max(int(p["w"]), int(p["h"])), reverse=True),
        sorted(items, key=lambda p: min(int(p["w"]), int(p["h"])), reverse=True),
        sorted(items, key=lambda p: int(p["h"]), reverse=True),
        sorted(items, key=lambda p: int(p["w"]), reverse=True),
    ]
    best = None
    for order in orders:
        rows = []
        ok = True
        for piece in order:
            choices = []
            current_h = sum(r["h"] for r in rows) + kerf * max(0, len(rows) - 1)
            for opt in _piece_orientations(piece, width, height):
                for row_idx, row in enumerate(rows):
                    next_x = row["used_w"] + kerf + opt["w"]
                    if next_x > width:
                        continue
                    next_h = max(row["h"], opt["h"])
                    total_h = current_h - row["h"] + next_h
                    if total_h <= height:
                        choices.append((next_h - row["h"], next_x, row_idx, opt))
                total_h = current_h + (kerf if rows else 0) + opt["h"]
                if total_h <= height:
                    choices.append((opt["h"], opt["w"], len(rows), opt))
            if not choices:
                ok = False
                break
            choices.sort(key=lambda c: (c[0], c[1]))
            _grow, _span, row_idx, opt = choices[0]
            if row_idx == len(rows):
                rows.append({"h": opt["h"], "used_w": 0, "items": []})
            row = rows[row_idx]
            item_x = row["used_w"] + (kerf if row["items"] else 0)
            row["items"].append((opt, item_x))
            row["used_w"] = item_x + opt["w"]
            row["h"] = max(row["h"], opt["h"])
        if not ok:
            continue
        used_h = sum(r["h"] for r in rows) + kerf * max(0, len(rows) - 1)
        row_waste = sum((width - r["used_w"]) * r["h"] for r in rows)
        key = (used_h, row_waste, len(rows))
        if best is None or key < best[0]:
            best = (key, rows, used_h)
    if best is None:
        return None

    _key, rows, used_h = best
    placements = []
    free = []
    y = int(y0)
    for row in rows:
        for opt, item_x in row["items"]:
            placements.append({
                "item": opt["id"],
                "x": int(x0 + item_x),
                "y": int(y),
                "w": int(opt["w"]),
                "h": int(opt["h"]),
                "rotated": bool(opt["rotated"]),
            })
        if row["used_w"] + kerf < width:
            free.append((int(x0 + row["used_w"] + kerf), int(y), int(width - row["used_w"] - kerf), int(row["h"])))
        y += row["h"] + kerf
    if used_h + kerf < height:
        free.append((int(x0), int(y0 + used_h + kerf), int(width), int(height - used_h - kerf)))
    return placements, free, used_h


def _row_candidates(items, width, height, kerf, *, min_count=1, max_count=5, max_results=60, deadline=None):
    """All useful one-row guillotine candidates for a small anchor set."""
    if not items:
        return []
    candidates = []
    keep_limit = max(max_results * 4, max_results + 20)

    def candidate_key(c):
        return (-c["area"], -c["row_w"], c["row_h"], -len(c["items"]))

    n = len(items)
    max_count = min(max_count, n)
    for size in range(min_count, max_count + 1):
        if _deadline_hit(deadline):
            break
        for idxs in combinations(range(n), size):
            if _deadline_hit(deadline):
                break
            subset = [items[i] for i in idxs]
            orient_lists = [list(_piece_orientations(p, width, height)) for p in subset]
            if any(not opts for opts in orient_lists):
                continue
            for orient_combo in product(*orient_lists):
                if _deadline_hit(deadline):
                    break
                row_w = sum(o["w"] for o in orient_combo) + kerf * (len(orient_combo) - 1)
                row_h = max(o["h"] for o in orient_combo)
                if row_w > width or row_h > height:
                    continue
                area = sum(o["w"] * o["h"] for o in orient_combo)
                candidates.append({
                    "area": int(area),
                    "row_w": int(row_w),
                    "row_h": int(row_h),
                    "items": [o["orig"] for o in orient_combo],
                    "oriented": list(orient_combo),
                })
                if len(candidates) > keep_limit:
                    candidates.sort(key=candidate_key)
                    del candidates[max_results:]
    candidates.sort(key=candidate_key)
    return candidates[:max_results]


def _place_row_candidate(candidate, x0, y0, kerf):
    placements = []
    x = int(x0)
    for opt in candidate["oriented"]:
        placements.append({
            "item": opt["id"],
            "x": int(x),
            "y": int(y0),
            "w": int(opt["w"]),
            "h": int(opt["h"]),
            "rotated": bool(opt["rotated"]),
        })
        x += opt["w"] + kerf
    return placements


def _column_candidates(items, width, height, kerf, *, min_count=1, max_count=5, max_results=80, deadline=None):
    """Useful one-column candidates for small anchor-shelf jobs."""
    if not items:
        return []
    candidates = []
    keep_limit = max(max_results * 4, max_results + 20)

    def candidate_key(c):
        return (-c["area"], c["col_w"], c["col_h"], -len(c["items"]))

    n = len(items)
    max_count = min(max_count, n)
    for size in range(min_count, max_count + 1):
        if _deadline_hit(deadline):
            break
        for idxs in combinations(range(n), size):
            if _deadline_hit(deadline):
                break
            subset = [items[i] for i in idxs]
            orient_lists = [list(_piece_orientations(p, width, height)) for p in subset]
            if any(not opts for opts in orient_lists):
                continue
            for orient_combo in product(*orient_lists):
                if _deadline_hit(deadline):
                    break
                col_w = max(o["w"] for o in orient_combo)
                col_h = sum(o["h"] for o in orient_combo) + kerf * (len(orient_combo) - 1)
                if col_w > width or col_h > height:
                    continue
                area = sum(o["w"] * o["h"] for o in orient_combo)
                candidates.append({
                    "area": int(area),
                    "col_w": int(col_w),
                    "col_h": int(col_h),
                    "items": [o["orig"] for o in orient_combo],
                    "oriented": list(orient_combo),
                })
                if len(candidates) > keep_limit:
                    candidates.sort(key=candidate_key)
                    del candidates[max_results:]
    candidates.sort(key=candidate_key)
    return candidates[:max_results]


def _place_column_candidate(candidate, x0, y0, kerf):
    placements = []
    y = int(y0)
    for opt in candidate["oriented"]:
        placements.append({
            "item": opt["id"],
            "x": int(x0),
            "y": int(y),
            "w": int(opt["w"]),
            "h": int(opt["h"]),
            "rotated": bool(opt["rotated"]),
        })
        y += opt["h"] + kerf
    return placements


def _remove_originals(items, chosen):
    chosen_ids = {id(p) for p in chosen}
    return [p for p in items if id(p) not in chosen_ids]


def construct_anchor_shelf(pieces, sheet_w, sheet_h, *, kerf=3, seed=0, deadline=None):
    """Build CutList's small mixed-job pattern.

    For jobs with one near-full-height anchor, CutList tends to pin that anchor
    to the left, place a dense top row to its right, then use a narrow lower-left
    column plus a short top-aligned shelf. The reward is one fat lower-right
    reusable rectangle instead of a skinny full-height strip.
    """
    if len(pieces) < 3 or len(pieces) > 18:
        return [], list(pieces)
    if sum(_area(p) for p in pieces) > sheet_w * sheet_h:
        return [], list(pieces)

    best = None
    for anchor in pieces:
        if _deadline_hit(deadline, 0.02):
            break
        for anchor_o in _piece_orientations(anchor, sheet_w, sheet_h):
            if _deadline_hit(deadline, 0.02):
                break
            if anchor_o["h"] < sheet_h * 0.70 or anchor_o["w"] > sheet_w * 0.55:
                continue
            right_x0 = anchor_o["w"] + kerf
            right_w = sheet_w - right_x0
            if right_w <= 0:
                continue
            remaining = _remove_originals(pieces, [anchor])
            top_rows = _row_candidates(
                remaining,
                right_w,
                int(sheet_h * 0.45),
                kerf,
                min_count=1,
                max_count=min(6, len(remaining)),
                max_results=1000,
                deadline=deadline,
            )
            for top in top_rows:
                if _deadline_hit(deadline, 0.02):
                    break
                top_y = sheet_h - top["row_h"]
                lower_h = top_y - kerf
                if lower_h <= 0:
                    continue
                after_top = _remove_originals(remaining, top["items"])
                left_cols = _column_candidates(
                    after_top,
                    right_w,
                    lower_h,
                    kerf,
                    min_count=1,
                    max_count=min(4, len(after_top)),
                    max_results=80,
                    deadline=deadline,
                )
                for left in left_cols:
                    if _deadline_hit(deadline, 0.02):
                        break
                    lower_right_x = right_x0 + left["col_w"] + kerf
                    lower_right_w = sheet_w - lower_right_x
                    if lower_right_w <= 0:
                        continue
                    after_left = _remove_originals(after_top, left["items"])
                    if not after_left:
                        continue
                    right_rows = _row_candidates(
                        after_left,
                        lower_right_w,
                        lower_h,
                        kerf,
                        min_count=len(after_left),
                        max_count=len(after_left),
                        max_results=200,
                        deadline=deadline,
                    )
                    for lower_row in right_rows:
                        if _deadline_hit(deadline, 0.02):
                            break
                        sheet = _Sheet(sheet_w, sheet_h, kerf)
                        sheet.placements = [{
                            "item": anchor_o["id"],
                            "x": 0,
                            "y": 0,
                            "w": int(anchor_o["w"]),
                            "h": int(anchor_o["h"]),
                            "rotated": bool(anchor_o["rotated"]),
                        }]
                        sheet.placements.extend(_place_row_candidate(top, right_x0, top_y, kerf))

                        left_y = lower_h - left["col_h"]
                        lower_row_y = lower_h - lower_row["row_h"]
                        if left_y < 0 or lower_row_y < 0:
                            continue
                        sheet.placements.extend(_place_column_candidate(left, right_x0, left_y, kerf))
                        sheet.placements.extend(_place_row_candidate(lower_row, lower_right_x, lower_row_y, kerf))

                        free = []
                        if anchor_o["h"] + kerf < sheet_h:
                            free.append((0, int(anchor_o["h"] + kerf), int(anchor_o["w"]), int(sheet_h - anchor_o["h"] - kerf)))
                        if top["row_w"] + kerf < right_w:
                            free.append((int(right_x0 + top["row_w"] + kerf), int(top_y), int(right_w - top["row_w"] - kerf), int(top["row_h"])))
                        if left_y - kerf > 0:
                            free.append((int(right_x0), 0, int(left["col_w"]), int(left_y - kerf)))
                        if lower_row_y - kerf > 0:
                            free.append((int(lower_right_x), 0, int(lower_right_w), int(lower_row_y - kerf)))
                        if lower_row["row_w"] + kerf < lower_right_w:
                            free.append((
                                int(lower_right_x + lower_row["row_w"] + kerf),
                                int(lower_row_y),
                                int(lower_right_w - lower_row["row_w"] - kerf),
                                int(lower_row["row_h"]),
                            ))
                        sheet.free = [r for r in free if r[2] > 0 and r[3] > 0]
                        sheet.strategy = "anchor_shelf"

                        sc = score([sheet], [])
                        largest = max((r[2] * r[3] for r in _offcut_rects(sheet)), default=0)
                        tie = (
                            -largest,
                            _scatter_penalty([sheet]),
                            saw_metrics(sheet),
                        )
                        if best is None or (sc, tie) < (best[0], best[1]):
                            best = (sc, tie, sheet)

    if best is None:
        return [], list(pieces)
    return [best[2]], []


def _make_anchor_layered_sheet(anchors, sheet_w, sheet_h, kerf, deadline=None):
    """Fit the remaining awkward panels as a bottom row plus upper split.

    This mirrors the 3mm CutList sheet: a dense medium-panel bottom row, then a
    tall/awkward panel on the left and the remaining panels row-packed in the
    right cap.
    """
    if not anchors:
        return None, []

    best = None
    bottom_rows = _row_candidates(
        anchors,
        sheet_w,
        int(sheet_h * 0.48),
        kerf,
        min_count=min(3, len(anchors)),
        max_count=min(5, len(anchors)),
        max_results=80,
        deadline=deadline,
    )
    for bottom in bottom_rows:
        if _deadline_hit(deadline):
            break
        cap_y = bottom["row_h"] + kerf
        cap_h = sheet_h - cap_y
        if cap_h <= 0:
            continue
        rest = _remove_originals(anchors, bottom["items"])
        left_options = [None]
        for item in rest:
            for opt in _piece_orientations(item, sheet_w, cap_h):
                if opt["h"] >= cap_h * 0.58 and opt["w"] <= sheet_w * 0.48:
                    left_options.append(opt)

        for left in left_options:
            if _deadline_hit(deadline):
                break
            remaining = list(rest)
            placements = _place_row_candidate(bottom, 0, 0, kerf)
            free = []
            if bottom["row_w"] + kerf < sheet_w:
                free.append((int(bottom["row_w"] + kerf), 0, int(sheet_w - bottom["row_w"] - kerf), int(bottom["row_h"])))

            right_x = 0
            right_w = sheet_w
            if left is not None:
                placements.append({
                    "item": left["id"],
                    "x": 0,
                    "y": int(cap_y),
                    "w": int(left["w"]),
                    "h": int(left["h"]),
                    "rotated": bool(left["rotated"]),
                })
                remaining = _remove_originals(remaining, [left["orig"]])
                if left["h"] + kerf < cap_h:
                    free.append((0, int(cap_y + left["h"] + kerf), int(left["w"]), int(cap_h - left["h"] - kerf)))
                right_x = left["w"] + kerf
                right_w = sheet_w - right_x
            if right_w <= 0:
                continue

            packed = _pack_rows_in_rect(remaining, right_x, cap_y, right_w, cap_h, kerf) if remaining else ([], [], 0)
            if not packed:
                continue
            more_pl, more_free, _used_h = packed
            placements.extend(more_pl)
            free.extend(more_free)
            used_h = max((p["y"] + p["h"] for p in placements), default=0)
            key = (
                abs(sheet_w - bottom["row_w"]),
                used_h,
                len(free),
                -bottom["area"],
            )
            if best is None or key < best[0]:
                best = (key, placements, free)

    if best is None:
        plain = _pack_rows_in_rect(anchors, 0, 0, sheet_w, sheet_h, kerf)
        if not plain:
            return None, anchors
        placements, free, _used_h = plain
    else:
        _key, placements, free = best

    sheet = _Sheet(sheet_w, sheet_h, kerf)
    sheet.placements = placements
    sheet.free = [r for r in free if r[2] > 0 and r[3] > 0]
    sheet.strategy = "anchor_layered"
    return sheet, []


def _make_anchor_mosaic_sheet(anchors, sheet_w, sheet_h, kerf, deadline=None):
    """Fit awkward one-off panels using a bottom row + upper split pattern."""
    if not anchors:
        return None, []

    plain = _pack_rows_in_rect(anchors, 0, 0, sheet_w, sheet_h, kerf)
    best = None
    if plain:
        placements, free, used_h = plain
        best = ((used_h, len(free), 0), placements, free)

    n = len(anchors)
    max_bottom = min(5, n)
    for size in range(2, max_bottom + 1):
        if _deadline_hit(deadline):
            break
        for idxs in combinations(range(n), size):
            if _deadline_hit(deadline):
                break
            bottom_items = [anchors[i] for i in idxs]
            rest_items = [anchors[i] for i in range(n) if i not in idxs]
            orient_lists = [list(_piece_orientations(p, sheet_w, sheet_h)) for p in bottom_items]
            if any(not opts for opts in orient_lists):
                continue
            for orient_combo in product(*orient_lists):
                if _deadline_hit(deadline):
                    break
                row_w = sum(o["w"] for o in orient_combo) + kerf * (len(orient_combo) - 1)
                row_h = max(o["h"] for o in orient_combo)
                if row_w > sheet_w or row_h > int(sheet_h * 0.55):
                    continue
                cap_y = row_h + kerf
                cap_h = sheet_h - cap_y
                if cap_h <= 0:
                    continue

                bottom = []
                x = 0
                for opt in orient_combo:
                    bottom.append({
                        "item": opt["id"],
                        "x": int(x),
                        "y": 0,
                        "w": int(opt["w"]),
                        "h": int(opt["h"]),
                        "rotated": bool(opt["rotated"]),
                    })
                    x += opt["w"] + kerf
                base_free = []
                if row_w + kerf < sheet_w:
                    base_free.append((int(row_w + kerf), 0, int(sheet_w - row_w - kerf), int(row_h)))

                # Try a tall left block in the upper cap, then shelf-pack the right.
                left_options = [None]
                for item in rest_items:
                    for opt in _piece_orientations(item, sheet_w, cap_h):
                        if opt["h"] >= cap_h * 0.70:
                            left_options.append(opt)
                for left in left_options:
                    if _deadline_hit(deadline):
                        break
                    remaining = list(rest_items)
                    placements = list(bottom)
                    free = list(base_free)
                    right_x = 0
                    right_w = sheet_w
                    if left is not None:
                        placements.append({
                            "item": left["id"],
                            "x": 0,
                            "y": int(cap_y),
                            "w": int(left["w"]),
                            "h": int(left["h"]),
                            "rotated": bool(left["rotated"]),
                        })
                        remaining = [p for p in remaining if p is not left["orig"]]
                        if left["h"] + kerf < cap_h:
                            free.append((0, int(cap_y + left["h"] + kerf), int(left["w"]), int(cap_h - left["h"] - kerf)))
                        right_x = left["w"] + kerf
                        right_w = sheet_w - right_x
                    packed = _pack_rows_in_rect(remaining, right_x, cap_y, right_w, cap_h, kerf) if remaining else ([], [], 0)
                    if not packed:
                        continue
                    more_pl, more_free, used_h = packed
                    placements.extend(more_pl)
                    free.extend(more_free)
                    upper_used = max((p["y"] + p["h"] for p in placements if p["y"] >= cap_y), default=cap_y) - cap_y
                    key = (max(row_w, right_x + max((p["x"] + p["w"] - right_x for p in more_pl), default=0)),
                           row_h + kerf + upper_used,
                           len(free))
                    if best is None or key < best[0]:
                        best = (key, placements, free)

    if best is None:
        return None, anchors
    _key, placements, free = best
    sheet = _Sheet(sheet_w, sheet_h, kerf)
    sheet.placements = placements
    sheet.free = [r for r in free if r[2] > 0 and r[3] > 0]
    sheet.strategy = "anchor_mosaic"
    return sheet, []


def _make_mixed_repeat_band_sheet(fillers, count, anchors, sheet_w, sheet_h, kerf, *, anchor_row=None):
    sheet, chosen, used_h = _make_repeated_band_sheet(fillers, count, sheet_w, sheet_h, kerf)
    if sheet is None:
        return None, [], []
    if not anchors:
        sheet.strategy = "mixed_repeat_band"
        return sheet, chosen, []
    cap_y = used_h + kerf
    cap_h = sheet_h - cap_y
    if anchor_row is not None:
        placements = _place_row_candidate(anchor_row, 0, cap_y, kerf)
        free = []
        if anchor_row["row_w"] + kerf < sheet_w:
            free.append((int(anchor_row["row_w"] + kerf), int(cap_y), int(sheet_w - anchor_row["row_w"] - kerf), int(anchor_row["row_h"])))
        top_y = cap_y + anchor_row["row_h"] + kerf
        if top_y < sheet_h:
            free.append((0, int(top_y), int(sheet_w), int(sheet_h - top_y)))
    else:
        packed = _pack_rows_in_rect(anchors, 0, cap_y, sheet_w, cap_h, kerf)
        if not packed:
            return None, [], []
        placements, free, _anchor_h = packed
    sheet.placements.extend(placements)
    # Keep the band row side offcuts, replace the top full-width offcut with the
    # anchor cap's actual free rectangles.
    band_free = [r for r in sheet.free if r[1] < cap_y]
    sheet.free = [r for r in band_free + free if r[2] > 0 and r[3] > 0]
    sheet.strategy = "mixed_repeat_band"
    return sheet, chosen, list(anchors)


def construct_repeat_band(pieces, sheet_w, sheet_h, *, kerf=3, seed=0, deadline=None):
    """Build CutList's dominant repeated-part band pattern.

    This targets jobs like the 3mm benchmark where a high-count repeated part
    forms one or more full-width bands, deliberately leaving a standard-width
    reusable offcut such as 2440x659.
    """
    # This constructor currently emits at most three sheets
    # (anchor + mixed-repeat + repeat-only). Larger jobs are handled by the
    # shelf/column constructor; don't burn time enumerating impossible splits.
    if sum(_area(p) for p in pieces) > sheet_w * sheet_h * 3:
        return [], list(pieces)

    groups = {}
    for p in pieces:
        groups.setdefault(_piece_type_key(p), []).append(p)
    repeat_groups = sorted(
        (items for items in groups.values() if len(items) >= 12),
        key=lambda items: len(items),
        reverse=True,
    )
    best = None
    for fillers in repeat_groups[:2]:
        if _deadline_hit(deadline, 0.02):
            break
        filler_ids = {id(p) for p in fillers}
        anchors = [p for p in pieces if id(p) not in filler_ids]
        qty = len(fillers)
        base = qty // 2
        counts = sorted(
            set(range(max(1, base - 6), min(qty - 1, base + 6) + 1)),
            key=lambda c: (abs(c - base), c),
        )
        for solo_count in counts:
            if _deadline_hit(deadline, 0.02):
                break
            mixed_count = qty - solo_count
            solo_sheet, solo_used, _solo_h = _make_repeated_band_sheet(fillers, solo_count, sheet_w, sheet_h, kerf)
            if solo_sheet is None or mixed_count <= 0:
                continue
            remaining_fillers = _remove_originals(fillers, solo_used)
            rows, mixed_used_h = _band_rows_for_count(remaining_fillers[0], mixed_count, sheet_w, sheet_h, kerf)
            if not rows:
                continue
            cap_h = sheet_h - mixed_used_h - kerf
            if cap_h <= 0:
                continue

            mixed_candidates = [{"items": []}] + _row_candidates(
                anchors,
                sheet_w,
                cap_h,
                kerf,
                min_count=1,
                max_count=min(5, len(anchors)),
                max_results=40,
                deadline=deadline,
            )
            for candidate in mixed_candidates:
                if _deadline_hit(deadline, 0.02):
                    break
                mixed_anchors = list(candidate["items"])
                rest_anchors = _remove_originals(anchors, mixed_anchors)
                if sum(_area(p) for p in rest_anchors) > sheet_w * sheet_h:
                    continue
                mixed_sheet, mixed_fillers, mixed_used = _make_mixed_repeat_band_sheet(
                    remaining_fillers,
                    mixed_count,
                    mixed_anchors,
                    sheet_w,
                    sheet_h,
                    kerf,
                    anchor_row=candidate if candidate.get("oriented") else None,
                )
                if mixed_sheet is None:
                    continue
                anchor_sheet, unplaced = _make_anchor_layered_sheet(rest_anchors, sheet_w, sheet_h, kerf, deadline=deadline)
                if unplaced:
                    continue
                sheets = [s for s in (anchor_sheet, mixed_sheet, solo_sheet) if s is not None]
                sc = score(sheets, [])
                # Prefer the repeat-band strategy when the score ties: it is the
                # more CutList-like, full-width-offcut pattern.
                tie = (
                    -max((r[2] * r[3] for s in sheets for r in _offcut_rects(s)), default=0),
                    len(mixed_anchors),
                )
                if best is None or (sc, tie) < (best[0], best[1]):
                    best = (sc, tie, sheets)
    if best is None:
        return [], list(pieces)
    return best[2], []


# --------------------------------------------------------------------------
# Large mixed job repeat-tail constructor
# --------------------------------------------------------------------------
def _take_from_pool(pool, predicate, count):
    chosen = []
    for item in list(pool):
        if predicate(item):
            pool.remove(item)
            chosen.append(item)
            if len(chosen) == count:
                return chosen
    return []


def _placement(item, x, y, w, h):
    return {
        "item": item["id"],
        "x": int(x),
        "y": int(y),
        "w": int(w),
        "h": int(h),
        "rotated": int(w) != int(item["w"]) or int(h) != int(item["h"]),
    }


def _sheet_from_pattern(sheet_w, sheet_h, kerf, placements, free, strategy):
    sheet = _Sheet(sheet_w, sheet_h, kerf)
    sheet.placements = list(placements)
    sheet.free = [
        (int(x), int(y), int(w), int(h))
        for x, y, w, h in free
        if int(w) > 0 and int(h) > 0
    ]
    sheet.strategy = strategy
    return sheet


def _guillotine_sort_key(piece, mode):
    w, h = int(piece["w"]), int(piece["h"])
    if mode == "cutlist_column":
        short, long = min(w, h), max(w, h)
        aspect = long / float(short or 1)
        # In a shallow cap CutList-like layouts prefer a long strip that spans the
        # waste cleanly before small block fillers. This keeps the normal big-panel
        # order, but lifts long/narrow strips above blocky filler parts.
        strip_bonus = 140 if aspect >= 4.0 and short <= 180 else 0
        return (short + strip_bonus, long if strip_bonus else max(w, h), w * h, -int(piece["id"]))
    if mode == "perimeter":
        return (w + h, w * h, max(w, h), min(w, h), -int(piece["id"]))
    if mode == "short_side":
        return (min(w, h), max(w, h), w * h, -int(piece["id"]))
    if mode == "long_side":
        return (max(w, h), min(w, h), w * h, -int(piece["id"]))
    if mode == "ratio":
        return (max(w, h) / float(min(w, h) or 1), w * h, -int(piece["id"]))
    return (w * h, max(w, h), min(w, h), -int(piece["id"]))


def _guillotine_pack_dims(w, h, sheet_w, sheet_h, kerf):
    """Reserve kerf as spacing after the piece while keeping sheet edges usable."""
    pw = int(w) + (int(kerf) if int(w) < int(sheet_w) else 0)
    ph = int(h) + (int(kerf) if int(h) < int(sheet_h) else 0)
    return pw, ph


def _guillotine_fitness(rect, pw, ph, mode):
    _x, _y, rw, rh = rect
    if pw > rw or ph > rh:
        return None
    if mode == "bssf":
        return min(rw - pw, rh - ph)
    if mode == "blsf":
        return max(rw - pw, rh - ph)
    return rw * rh - pw * ph


def _guillotine_split_horizontal(rect, pw, ph):
    x, y, rw, rh = rect
    children = []
    if ph < rh:
        children.append((int(x), int(y + ph), int(rw), int(rh - ph)))
    if pw < rw:
        children.append((int(x + pw), int(y), int(rw - pw), int(ph)))
    return children


def _guillotine_split_vertical(rect, pw, ph):
    x, y, rw, rh = rect
    children = []
    if ph < rh:
        children.append((int(x), int(y + ph), int(pw), int(rh - ph)))
    if pw < rw:
        children.append((int(x + pw), int(y), int(rw - pw), int(rh)))
    return children


def _guillotine_split(rect, pw, ph, mode):
    _x, _y, rw, rh = rect
    if mode == "sas":
        horizontal = rw < rh
    elif mode == "las":
        horizontal = rw >= rh
    elif mode == "slas":
        horizontal = (rw - pw) < (rh - ph)
    elif mode == "llas":
        horizontal = (rw - pw) >= (rh - ph)
    elif mode == "maxas":
        horizontal = pw * (rh - ph) <= ph * (rw - pw)
    elif mode == "minas":
        horizontal = pw * (rh - ph) >= ph * (rw - pw)
    else:
        horizontal = True
    children = (
        _guillotine_split_horizontal(rect, pw, ph)
        if horizontal
        else _guillotine_split_vertical(rect, pw, ph)
    )
    return [r for r in children if int(r[2]) > 0 and int(r[3]) > 0]


def _guillotine_join(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    if ax == bx and aw == bw:
        if ay + ah == by:
            return (ax, ay, aw, ah + bh)
        if by + bh == ay:
            return (bx, by, bw, bh + ah)
    if ay == by and ah == bh:
        if ax + aw == bx:
            return (ax, ay, aw + bw, ah)
        if bx + bw == ax:
            return (bx, by, bw + aw, bh)
    return None


def _guillotine_prune_sections(sections):
    sections = [tuple(map(int, r)) for r in sections if int(r[2]) > 0 and int(r[3]) > 0]
    changed = True
    while changed:
        changed = False
        merged = []
        used = set()
        for i, a in enumerate(sections):
            if i in used:
                continue
            current = a
            for j in range(i + 1, len(sections)):
                if j in used:
                    continue
                joined = _guillotine_join(current, sections[j])
                if joined is not None:
                    current = joined
                    used.add(j)
                    changed = True
            used.add(i)
            merged.append(current)
        sections = merged

    kept = []
    for i, rect in enumerate(sections):
        x, y, w, h = rect
        contained = False
        for j, other in enumerate(sections):
            if i == j:
                continue
            ox, oy, ow, oh = other
            if x >= ox and y >= oy and x + w <= ox + ow and y + h <= oy + oh:
                contained = True
                break
        if not contained:
            kept.append(rect)
    kept.sort(key=lambda r: (r[1], r[0], r[2] * r[3]))
    return kept


def _guillotine_best_fit(sheet, piece, sheet_w, sheet_h, kerf, fitness_mode):
    best = None
    for rect_idx, rect in enumerate(sheet["free"]):
        _rx, _ry, rw, rh = rect
        for opt in _piece_orientations(piece, sheet_w, sheet_h):
            # Kerf is a gap between sibling pieces, not beyond the final edge of
            # the current guillotine section. Reserve it only when there is enough
            # room to leave a real child section after the cut.
            pw = int(opt["w"]) + (int(kerf) if int(opt["w"]) + int(kerf) <= int(rw) else 0)
            ph = int(opt["h"]) + (int(kerf) if int(opt["h"]) + int(kerf) <= int(rh) else 0)
            fit = _guillotine_fitness(rect, pw, ph, fitness_mode)
            if fit is None:
                continue
            x, y, _rw, _rh = rect
            key = (
                fit,
                min(_rw - pw, _rh - ph),
                y,
                x,
                int(opt["h"]),
                int(opt["w"]),
                rect_idx,
            )
            if best is None or key < best[0]:
                best = (key, rect_idx, rect, opt, pw, ph)
    return best


def _guillotine_place(sheet, piece, fit, split_mode, sheet_w, sheet_h, kerf):
    _key, rect_idx, rect, opt, pw, ph = fit
    x, y, _rw, _rh = rect
    sheet["free"].pop(rect_idx)
    sheet["free"].extend(_guillotine_split(rect, pw, ph, split_mode))
    sheet["free"] = _guillotine_prune_sections(sheet["free"])
    sheet["placements"].append({
        "item": int(piece["id"]),
        "x": int(x),
        "y": int(y),
        "w": int(opt["w"]),
        "h": int(opt["h"]),
        "rotated": bool(opt["rotated"]),
    })


def _guillotine_pack_variant(pieces, sheet_w, sheet_h, kerf, *, fitness_mode, split_mode, sort_mode, deadline=None):
    ordered = sorted(
        pieces,
        key=lambda piece: _guillotine_sort_key(piece, sort_mode),
        reverse=True,
    )
    packed = []
    for piece_idx, piece in enumerate(ordered):
        if _deadline_hit(deadline, 0.02):
            return [], list(pieces)
        best = None
        for sheet_idx, sheet in enumerate(packed):
            fit = _guillotine_best_fit(sheet, piece, sheet_w, sheet_h, kerf, fitness_mode)
            if fit is None:
                continue
            key = (fit[0][0], len(sheet["placements"]), sheet_idx, fit[0])
            if best is None or key < best[0]:
                best = (key, sheet_idx, fit)
        if best is None:
            sheet = {"placements": [], "free": [(0, 0, int(sheet_w), int(sheet_h))]}
            fit = _guillotine_best_fit(sheet, piece, sheet_w, sheet_h, kerf, fitness_mode)
            if fit is None:
                return [], ordered[piece_idx:]
            packed.append(sheet)
            sheet_idx = len(packed) - 1
        else:
            _key, sheet_idx, fit = best
        _guillotine_place(packed[sheet_idx], piece, fit, split_mode, sheet_w, sheet_h, kerf)

    sheets = []
    for idx, raw in enumerate(packed, 1):
        sheet = _sheet_from_pattern(
            sheet_w,
            sheet_h,
            kerf,
            raw["placements"],
            raw["free"],
            "guillotine_%s_%s_%s_%02d" % (fitness_mode, split_mode, sort_mode, idx),
        )
        if not _sheet_geometry_ok(sheet):
            return [], list(pieces)
        sheets.append(sheet)
    used_ids = set(_ids_in_sheets(sheets) or ())
    if len(used_ids) != len(pieces):
        return [], [p for p in pieces if int(p["id"]) not in used_ids]
    return sheets, []


_GUILLOTINE_VARIANTS = (
    ("bssf", "slas", "long_side"),
    ("bssf", "llas", "long_side"),
    ("bssf", "maxas", "long_side"),
    ("bssf", "llas", "cutlist_column"),
    ("bssf", "maxas", "cutlist_column"),
    ("bssf", "las", "cutlist_column"),
    ("bssf", "las", "short_side"),
    ("bssf", "llas", "short_side"),
    ("bssf", "maxas", "short_side"),
    ("bssf", "maxas", "perimeter"),
    ("bssf", "las", "perimeter"),
    ("bssf", "llas", "perimeter"),
    ("baf", "minas", "area"),
    ("baf", "minas", "long_side"),
    ("baf", "sas", "short_side"),
    ("baf", "sas", "area"),
    ("baf", "las", "area"),
    ("baf", "maxas", "area"),
    ("baf", "las", "short_side"),
    ("baf", "maxas", "short_side"),
    ("bssf", "minas", "area"),
    ("blsf", "las", "short_side"),
)


def construct_guillotine_baf(pieces, sheet_w, sheet_h, *, kerf=3, seed=0, deadline=None):
    """Offline guillotine Best-Area-Fit constructor.

    This is the general maths behind the CutList-like stress results: sort the
    inventory, place each part into the open sheet/free-section with the least
    leftover area, then split the remaining section by a guillotine rule. Kerf is
    reserved as spacing in the packed rectangle, not by replaying coordinates.
    """
    best = None
    for fitness_mode, split_mode, sort_mode in _GUILLOTINE_VARIANTS:
        if _deadline_hit(deadline, 0.02):
            break
        sheets, unplaced = _guillotine_pack_variant(
            pieces,
            sheet_w,
            sheet_h,
            kerf,
            fitness_mode=fitness_mode,
            split_mode=split_mode,
            sort_mode=sort_mode,
            deadline=deadline,
        )
        if unplaced or not sheets:
            continue
        sc = score(sheets, [])
        cuts = sum(_guillotine_tree_cut_count(sheet) for sheet in sheets)
        full = _best_offcut_info(sheets, full_dim_only=True)
        tie = (
            sc,
            cuts,
            -int(full.get("value") or 0),
            "%s_%s_%s" % (fitness_mode, split_mode, sort_mode),
        )
        if best is None or tie < best[0]:
            best = (tie, sheets)
    if best is None:
        return [], list(pieces)
    return best[1], []


def pack_single_sheet(pieces, sheet_w, sheet_h, *, kerf=3, deadline=None):
    """Pack one finite physical source, returning a single guillotine sheet.

    Offcuts are use-once stock, so the multi-sheet constructor is the wrong
    primitive for them. This helper uses the same guillotine variants as the v2
    constructor, but deliberately emits at most one sheet and leaves the caller
    to decide whether the utilised area is worth consuming that offcut.
    """
    global _LAST_V2_METRICS
    started_at = time.monotonic()
    pieces = [dict(p) for p in pieces or []]
    piece_by_id = {int(p["id"]): p for p in pieces}
    variants = []
    seen_variants = set()
    for variant in tuple(_GUILLOTINE_VARIANTS) + tuple(_GUILLOTINE_MOSAIC_VARIANTS):
        if variant in seen_variants:
            continue
        seen_variants.add(variant)
        variants.append(variant)

    best = None
    total_candidates = 0
    orientations = [(int(sheet_w), int(sheet_h))]
    if int(sheet_w) != int(sheet_h):
        orientations.append((int(sheet_h), int(sheet_w)))

    for orientation_idx, (width, height) in enumerate(orientations):
        if _deadline_hit(deadline, 0.02):
            break
        for fitness_mode, split_mode, sort_mode in variants:
            if _deadline_hit(deadline, 0.02):
                break
            sheet = _guillotine_single_sheet_variant(
                pieces,
                width,
                height,
                int(kerf or 0),
                fitness_mode=fitness_mode,
                split_mode=split_mode,
                sort_mode=sort_mode,
                deadline=deadline,
            )
            total_candidates += 1
            if not sheet:
                continue
            placed_ids = _ids_in_sheets([sheet]) or ()
            if not placed_ids:
                continue
            placed_area = sum(
                int(piece_by_id[item_id]["w"]) * int(piece_by_id[item_id]["h"])
                for item_id in placed_ids
                if item_id in piece_by_id
            )
            if placed_area <= 0:
                continue
            cut_count = _guillotine_tree_cut_count(sheet)
            full_offcut = int(_best_offcut_info([sheet], full_dim_only=True).get("value") or 0)
            best_offcut = int(_best_offcut_info([sheet]).get("value") or 0)
            rank = (
                -int(placed_area),
                -len(placed_ids),
                int(cut_count),
                -int(full_offcut),
                -int(best_offcut),
                orientation_idx,
                fitness_mode,
                split_mode,
                sort_mode,
            )
            if best is None or rank < best[0]:
                best = (rank, sheet, set(placed_ids), placed_area, cut_count)

    elapsed = max(1, int((time.monotonic() - started_at) * 1000))
    if best is None:
        _LAST_V2_METRICS = {
            "pattern_engine_version": "v2_beam",
            "best_pattern_strategy": "",
            "beam_states_evaluated": 0,
            "pattern_candidates_evaluated": total_candidates,
            "search_elapsed_ms": elapsed,
            "time_budget_hit": bool(_deadline_hit(deadline)),
        }
        return [], 0, score([], pieces), list(pieces)

    _rank, sheet, placed_ids, _placed_area, _cut_count = best
    unplaced = [piece for piece in pieces if int(piece["id"]) not in placed_ids]
    sc = score([sheet], [])
    _LAST_V2_METRICS = {
        "pattern_engine_version": "v2_beam",
        "best_pattern_strategy": getattr(sheet, "strategy", "") or "single_sheet_guillotine",
        "beam_states_evaluated": 1,
        "pattern_candidates_evaluated": total_candidates,
        "search_elapsed_ms": elapsed,
        "time_budget_hit": bool(_deadline_hit(deadline)),
    }
    _LAST_V2_METRICS.update(_layout_metric_fields([sheet]))
    return [sheet], 0, sc, unplaced


_GUILLOTINE_MOSAIC_VARIANTS = (
    ("bssf", "llas", "cutlist_column"),
    ("bssf", "maxas", "cutlist_column"),
    ("bssf", "las", "cutlist_column"),
    ("bssf", "las", "short_side"),
    ("bssf", "llas", "short_side"),
    ("bssf", "maxas", "short_side"),
    ("bssf", "maxas", "perimeter"),
    ("bssf", "slas", "long_side"),
)


def _guillotine_single_sheet_variant(
        pieces, sheet_w, sheet_h, kerf, *,
        fitness_mode, split_mode, sort_mode, caps_by_key=None, boost_keys=None, deadline=None):
    """Build one recursive guillotine sheet from a variant.

    Unlike `_guillotine_pack_variant`, this deliberately does not open a second
    sheet. It is the beam-search primitive: each variant proposes one reusable
    sheet pattern, then the beam decides what the next sheet should be.
    """
    caps_by_key = dict(caps_by_key or {})
    boost_keys = set(boost_keys or ())
    placed_counts = Counter()
    ordered = sorted(
        pieces,
        key=lambda piece: ((_piece_type_key(piece) in boost_keys),) + _guillotine_sort_key(piece, sort_mode),
        reverse=True,
    )
    raw = {"placements": [], "free": [(0, 0, int(sheet_w), int(sheet_h))]}
    for piece in ordered:
        if _deadline_hit(deadline, 0.01):
            break
        piece_key = _piece_type_key(piece)
        cap = caps_by_key.get(piece_key)
        if cap is not None and placed_counts[piece_key] >= int(cap):
            continue
        fit = _guillotine_best_fit(raw, piece, sheet_w, sheet_h, kerf, fitness_mode)
        if fit is None:
            continue
        _guillotine_place(raw, piece, fit, split_mode, sheet_w, sheet_h, kerf)
        placed_counts[piece_key] += 1

    strategy = "guillotine_one_%s_%s_%s" % (fitness_mode, split_mode, sort_mode)
    if boost_keys:
        strategy += "_boosted"
    if caps_by_key:
        strategy += "_capped"
    sheet = _sheet_from_pattern(sheet_w, sheet_h, kerf, raw["placements"], raw["free"], strategy)
    if not sheet.placements or not _sheet_geometry_ok(sheet):
        return None
    return sheet


def _guillotine_cap_options(sheet, pieces, sheet_w, sheet_h, *, max_options=4):
    """Controlled leave-one variants for repeated anchor stacks.

    CutList-like mosaics often avoid maxing out a repeated tall-anchor column so
    the next large panel can enter the same guillotine tree. Generate those caps
    from geometry only: repeated, large-ish piece types already present on the
    proposed sheet.
    """
    available = Counter(_piece_type_key(piece) for piece in pieces or [])
    placed = Counter()
    area_by_key = {}
    for pl in sheet.placements or []:
        key = tuple(sorted((int(pl["w"]), int(pl["h"]))))
        placed[key] += 1
        area_by_key[key] = int(pl["w"]) * int(pl["h"])

    sheet_area = max(1, int(sheet_w) * int(sheet_h))
    options = []
    for key, count in placed.items():
        if count < 2:
            continue
        area = area_by_key.get(key, 0)
        if area < sheet_area * 0.045:
            continue
        # Try capped mosaics even when the uncapped one would consume every
        # remaining copy of this large family. CutList-style layouts often keep
        # the last large repeat as a "glue" panel for a later mixed sheet instead
        # of maxing out the current sheet.
        for cap in range(count - 1, max(1, count - 3), -1):
            if cap <= 0:
                continue
            options.append((-area, -count, key, cap))

    options.sort()
    return [({key: cap}, "%sx%s=%s" % (key[0], key[1], cap)) for _area_key, _count, key, cap in options[:max_options]]


def _guillotine_boost_options(pieces, sheet_w, sheet_h, *, max_options=10):
    """Repeated-family starting points for column mosaic sheets."""
    groups = {}
    for piece in pieces or []:
        groups.setdefault(_piece_type_key(piece), []).append(piece)
    sheet_area = max(1, int(sheet_w) * int(sheet_h))
    repeat_groups = []
    strip_groups = []
    for key, items in groups.items():
        short, long = int(key[0]), int(key[1])
        area = short * long
        if len(items) < 2:
            continue
        if len(items) < 3 and area < sheet_area * 0.11:
            continue
        if area < sheet_area * 0.02:
            continue
        aspect = long / float(short or 1)
        entry = (key, len(items), area, aspect)
        repeat_groups.append(entry)
        if aspect >= 4.0 and short <= max(180, int(min(sheet_w, sheet_h) * 0.16)):
            strip_groups.append(entry)

    repeat_groups.sort(key=lambda item: (-item[1], -item[2], item[0]))
    strip_groups.sort(key=lambda item: (-item[2], -item[1], item[0]))
    companion_groups = []
    seen = set()
    for entry in strip_groups[:3] + repeat_groups[:4]:
        if entry[0] in seen:
            continue
        seen.add(entry[0])
        companion_groups.append(entry)

    options = []
    seen_options = set()
    for primary in repeat_groups[:5]:
        combos = [(primary[0],)]
        for companion in companion_groups:
            if companion[0] != primary[0]:
                combos.append((primary[0], companion[0]))
        for combo in combos:
            signature = tuple(sorted(combo))
            if signature in seen_options:
                continue
            seen_options.add(signature)
            label = "_".join("%sx%s" % (key[0], key[1]) for key in combo)
            options.append((combo, label))
            if len(options) >= max_options:
                return options
    return options


def _row_filler_orientation(sample, sheet_w, sheet_h, kerf):
    options = []
    for opt in _piece_orientations(sample, sheet_w, sheet_h):
        cap = (sheet_w + kerf) // (opt["w"] + kerf)
        used_w = cap * opt["w"] + kerf * max(0, cap - 1)
        if cap <= 0 or used_w > sheet_w:
            continue
        options.append((cap, -opt["h"], -used_w, opt))
    if not options:
        return None
    options.sort(reverse=True)
    return options[0][3]


def construct_large_repeat_tail(pieces, sheet_w, sheet_h, *, kerf=3, seed=0, deadline=None):
    """Build CutList-style large repeat jobs with a one-row tail sheet.

    When a job has a dominant repeated part plus a handful of awkward anchors,
    CutList often spends two dense repeat sheets, pushes some repeats into the
    anchor dead space, and leaves the final repeat sheet as one clean full-width
    offcut strip. The generic constructor can hit the sheet count but miss that
    reusable tail offcut, so this candidate constructs that family directly.
    """
    if sheet_w < sheet_h or len(pieces) < 25:
        return [], list(pieces)

    groups = {}
    for piece in pieces:
        groups.setdefault(_piece_type_key(piece), []).append(piece)
    repeat_groups = sorted(
        (items for items in groups.values() if len(items) >= 30),
        key=lambda items: len(items),
        reverse=True,
    )
    if not repeat_groups:
        return [], list(pieces)

    best = None
    for fillers in repeat_groups[:1]:
        if _deadline_hit(deadline, 0.02):
            break
        row_opt = _row_filler_orientation(fillers[0], sheet_w, sheet_h, kerf)
        if not row_opt:
            continue
        row_w = int(row_opt["w"])
        row_h = int(row_opt["h"])
        row_cap = int((sheet_w + kerf) // (row_w + kerf))
        full_repeat_count = row_cap * 2
        if row_cap < 4 or full_repeat_count <= row_cap:
            continue
        if len(fillers) < full_repeat_count * 2 + row_cap + 8:
            continue

        filler_ids = {id(p) for p in fillers}
        anchors = [p for p in pieces if id(p) not in filler_ids]

        strip_candidates = []
        for key, items in groups.items():
            if key == _piece_type_key(fillers[0]) or len(items) < 4:
                continue
            short, long = key
            if short <= max(row_w, row_h) + 80 and long >= sheet_w * 0.25:
                strip_candidates.append((long, short, key, list(items)))
        strip_candidates.sort(reverse=True)
        if len(strip_candidates) < 2:
            continue
        long_key = strip_candidates[0][2]
        mid_key = next(
            (candidate[2] for candidate in strip_candidates[1:]
             if abs(candidate[1] - strip_candidates[0][1]) <= 80),
            None,
        )
        if not mid_key:
            continue
        long_w, long_h = int(max(long_key)), int(min(long_key))
        mid_w, mid_h = int(max(mid_key)), int(min(mid_key))
        mid_col_w, mid_col_h = int(min(mid_key)), int(max(mid_key))

        remaining_keys = [
            (key, list(items))
            for key, items in groups.items()
            if key not in (_piece_type_key(fillers[0]), long_key, mid_key)
        ]
        large_keys = sorted(
            ((key, items) for key, items in remaining_keys if len(items) >= 2),
            key=lambda pair: pair[0][0] * pair[0][1],
            reverse=True,
        )
        if not large_keys:
            continue
        large_key = large_keys[0][0]
        large_w, large_h = int(max(large_key)), int(min(large_key))

        singles = [key for key, items in remaining_keys if key != large_key for _item in items]
        side_keys = sorted(
            (key for key in singles if max(key) / float(min(key) or 1) >= 4.0),
            key=lambda key: key[0] * key[1],
            reverse=True,
        )
        medium_keys = sorted(
            (key for key in singles if key not in side_keys),
            key=lambda key: key[0] * key[1],
            reverse=True,
        )
        if not side_keys or len(medium_keys) < 2:
            continue
        side_key = side_keys[0]
        medium_key = medium_keys[0]
        small_key = medium_keys[-1]
        medium_w, medium_h = int(min(medium_key)), int(max(medium_key))
        side_w, side_h = int(max(side_key)), int(min(side_key))
        small_w, small_h = int(max(small_key)), int(min(small_key))

        pool = list(pieces)
        filler_pool = _take_from_pool(pool, lambda p: id(p) in filler_ids, len(fillers))
        anchor_pool = [p for p in pool if id(p) not in filler_ids]

        medium = _take_from_pool(anchor_pool, lambda p, key=medium_key: _piece_type_key(p) == key, 1)
        large_a = _take_from_pool(anchor_pool, lambda p, key=large_key: _piece_type_key(p) == key, 1)
        mid_a = _take_from_pool(anchor_pool, lambda p, key=mid_key: _piece_type_key(p) == key, 1)
        side = _take_from_pool(anchor_pool, lambda p, key=side_key: _piece_type_key(p) == key, 1)
        if not (medium and large_a and mid_a and side):
            continue

        # Mosaic sheet: medium panel on the left, large anchor above the long
        # strip, and one mid strip in the right cap.
        right_x = medium_w + kerf
        mid_x = right_x + large_w + kerf
        if (
            right_x + max(large_w, side_w) > sheet_w
            or mid_x + mid_col_w > sheet_w
            or medium_h > sheet_h
            or large_h > sheet_h
            or mid_col_h > sheet_h
            or side_h + kerf > sheet_h
        ):
            continue
        sheet1 = _sheet_from_pattern(
            sheet_w,
            sheet_h,
            kerf,
            [
                _placement(medium[0], 0, sheet_h - medium_h, medium_w, medium_h),
                _placement(side[0], right_x, 0, side_w, side_h),
                _placement(large_a[0], right_x, sheet_h - large_h, large_w, large_h),
                _placement(mid_a[0], mid_x, sheet_h - mid_col_h, mid_col_w, mid_col_h),
            ],
            [
                (0, 0, medium_w, sheet_h - medium_h - kerf),
                (right_x + side_w + kerf, 0, sheet_w - right_x - side_w - kerf, side_h),
                (mid_x, side_h + kerf, mid_col_w, sheet_h - mid_col_h - side_h - 2 * kerf),
                (
                    mid_x + mid_col_w + kerf,
                    side_h + kerf,
                    sheet_w - mid_x - mid_col_w - kerf,
                    sheet_h - side_h - kerf,
                ),
            ],
            "large_repeat_anchor_mosaic",
        )

        repeat_sheets = []
        ok = True
        for count in (full_repeat_count, full_repeat_count):
            sheet, used, _used_h = _make_repeated_band_sheet(filler_pool, count, sheet_w, sheet_h, kerf)
            if sheet is None:
                ok = False
                break
            repeat_sheets.append(sheet)
            filler_pool = _remove_originals(filler_pool, used)
        if not ok:
            continue

        long_three = _take_from_pool(anchor_pool, lambda p, key=long_key: _piece_type_key(p) == key, 3)
        small = _take_from_pool(anchor_pool, lambda p, key=small_key: _piece_type_key(p) == key, 1)
        strip_fillers = filler_pool[:2]
        filler_pool = filler_pool[2:]
        if len(long_three) != 3 or not small or len(strip_fillers) != 2:
            continue
        stack_right_x = long_w + kerf
        filler_y = sheet_h - small_h - kerf - row_h
        if stack_right_x + max(small_w, 2 * row_w + kerf) > sheet_w or filler_y < 0:
            continue
        sheet4 = _sheet_from_pattern(
            sheet_w,
            sheet_h,
            kerf,
            [
                _placement(long_three[0], 0, 0, long_w, long_h),
                _placement(long_three[1], 0, long_h + kerf, long_w, long_h),
                _placement(long_three[2], 0, 2 * (long_h + kerf), long_w, long_h),
                _placement(small[0], stack_right_x, sheet_h - small_h, small_w, small_h),
                _placement(strip_fillers[0], stack_right_x, filler_y, row_w, row_h),
                _placement(strip_fillers[1], stack_right_x + row_w + kerf, filler_y, row_w, row_h),
            ],
            [
                (0, 3 * long_h + 3 * kerf, long_w, sheet_h - 3 * long_h - 3 * kerf),
                (stack_right_x + small_w + kerf, sheet_h - small_h, sheet_w - stack_right_x - small_w - kerf, small_h),
                (stack_right_x + 2 * row_w + 2 * kerf, filler_y, sheet_w - stack_right_x - 2 * row_w - 2 * kerf, row_h),
                (stack_right_x, 0, sheet_w - stack_right_x, max(0, filler_y - kerf)),
            ],
            "large_repeat_strip_stack",
        )

        large_b = _take_from_pool(anchor_pool, lambda p, key=large_key: _piece_type_key(p) == key, 1)
        mid_three = _take_from_pool(anchor_pool, lambda p, key=mid_key: _piece_type_key(p) == key, 3)
        if not large_b or len(mid_three) != 3:
            continue
        column_x = large_w + kerf
        if column_x + mid_w > sheet_w or 3 * mid_h + 2 * kerf > sheet_h:
            continue
        column_sheet = _sheet_from_pattern(
            sheet_w,
            sheet_h,
            kerf,
            [
                _placement(large_b[0], 0, sheet_h - large_h, large_w, large_h),
                _placement(mid_three[0], column_x, 0, mid_w, mid_h),
                _placement(mid_three[1], column_x, mid_h + kerf, mid_w, mid_h),
                _placement(mid_three[2], column_x, 2 * (mid_h + kerf), mid_w, mid_h),
            ],
            [
                (0, 0, large_w, sheet_h - large_h - kerf),
                (column_x, 3 * mid_h + 3 * kerf, mid_w, sheet_h - 3 * mid_h - 3 * kerf),
                (column_x + mid_w + kerf, 0, sheet_w - column_x - mid_w - kerf, sheet_h),
            ],
            "large_repeat_anchor_column",
        )

        long_one = _take_from_pool(anchor_pool, lambda p, key=long_key: _piece_type_key(p) == key, 1)
        mixed_fillers = filler_pool[: row_cap + 1]
        filler_pool = filler_pool[row_cap + 1:]
        if not long_one or len(mixed_fillers) != row_cap + 1:
            continue
        bottom_offcut_h = sheet_h - long_h - kerf - row_h - kerf
        row_y = bottom_offcut_h + kerf
        cap_y = row_y + row_h + kerf
        if bottom_offcut_h < 0 or cap_y + long_h > sheet_h:
            continue
        mixed_placements = [
            _placement(long_one[0], 0, cap_y, long_w, long_h),
            _placement(mixed_fillers[0], long_w + kerf, sheet_h - row_w, row_h, row_w),
        ]
        x = 0
        for filler in mixed_fillers[1:]:
            mixed_placements.append(_placement(filler, x, row_y, row_w, row_h))
            x += row_w + kerf
        sheet6 = _sheet_from_pattern(
            sheet_w,
            sheet_h,
            kerf,
            mixed_placements,
            [
                (0, 0, sheet_w, bottom_offcut_h),
                (row_cap * row_w + row_cap * kerf, row_y, sheet_w - row_cap * row_w - row_cap * kerf, row_h),
                (long_w + kerf + row_h + kerf, sheet_h - row_w, sheet_w - long_w - row_h - 2 * kerf, row_w),
            ],
            "large_repeat_mixed_tail_band",
        )

        tail_sheet, tail_used, _tail_h = _make_repeated_band_sheet(
            filler_pool, row_cap, sheet_w, sheet_h, kerf,
        )
        if tail_sheet is None:
            continue
        tail_sheet.strategy = "large_repeat_tail_band"
        filler_pool = _remove_originals(filler_pool, tail_used)
        if anchor_pool or filler_pool:
            continue

        sheets = [sheet1] + repeat_sheets + [sheet4, column_sheet, sheet6, tail_sheet]
        sc = score(sheets, [])
        if best is None or sc < best[0]:
            best = (sc, sheets)

    if best is None:
        return [], list(pieces)
    return best[1], []


# --------------------------------------------------------------------------
# Mixed repeat edge-frame tail constructor
# --------------------------------------------------------------------------
def _edge_group_orientation(sample, max_w, max_h, *, mode):
    options = list(_piece_orientations(sample, max_w, max_h))
    if not options:
        return None
    if mode == "vertical_strip":
        options.sort(key=lambda opt: (opt["w"], -opt["h"], -opt["w"] * opt["h"]))
    elif mode == "top_compact":
        options.sort(key=lambda opt: (opt["w"], opt["h"], -opt["w"] * opt["h"]))
    elif mode == "low_row":
        options.sort(key=lambda opt: (opt["h"], -opt["w"], -opt["w"] * opt["h"]))
    else:
        options.sort(key=lambda opt: (-opt["w"] * opt["h"], opt["h"], opt["w"]))
    return options[0]


def _edge_take_group(pool_by_key, key, count):
    items = pool_by_key.get(key) or []
    chosen = items[:count]
    pool_by_key[key] = items[count:]
    return chosen


def _edge_row_capacity(opt, width, kerf):
    return int((int(width) + int(kerf)) // (int(opt["w"]) + int(kerf)))


def construct_edge_frame_tail(pieces, sheet_w, sheet_h, *, kerf=3, seed=0, deadline=None):
    """Build a CutList-like mixed-repeat tail sheet around one big offcut.

    This targets jobs where no single repeat dominates, but several repeat
    families plus long strips can make a clean perimeter frame. Instead of
    letting free-rect scatter those parts across anchor sheets, this reserves a
    large central rectangle and fills the sheet edges with saw-friendly rows and
    columns. It is deliberately inventory-driven, not benchmark-specific.

    Note: this is currently a reference candidate, not part of the production
    beam. On the 100-panel stress benchmark it recreates CutList's final sheet,
    but the remaining panels still need a companion first-six-sheet constructor
    before this can be enabled without increasing sheet count.
    """
    if len(pieces) < 18 or sheet_w < sheet_h:
        return [], list(pieces)

    groups = {}
    for piece in pieces:
        groups.setdefault(_piece_type_key(piece), []).append(piece)

    strip_groups = []
    filler_groups = []
    for key, items in groups.items():
        if len(items) < 3:
            continue
        short, long = int(key[0]), int(key[1])
        aspect = float(long) / float(short or 1)
        if aspect >= 4.0 and short <= max(180, int(sheet_h * 0.16)):
            strip_groups.append((key, list(items), short, long, aspect))
        elif len(items) >= 4 and long <= int(sheet_w * 0.35) and short <= int(sheet_h * 0.25):
            filler_groups.append((key, list(items), short, long, len(items)))

    if not strip_groups or len(filler_groups) < 2:
        return [], list(pieces)

    # Left edge columns should be tall but not full-height; full-height strips
    # tend to consume the exact pieces CutList prefers on dense anchor sheets.
    left_candidates = [
        item for item in strip_groups
        if int(sheet_h * 0.48) <= item[3] <= int(sheet_h * 0.88)
    ] or strip_groups
    left_candidates.sort(key=lambda item: (abs(item[3] - int(sheet_h * 0.78)), item[2], -len(item[1])))

    rail_candidates = [
        item for item in strip_groups
        if item not in left_candidates[:1] and item[3] <= int(sheet_w * 0.55)
    ] or [item for item in strip_groups if item not in left_candidates[:1]]
    rail_candidates.sort(key=lambda item: (abs(item[3] - int(sheet_w * 0.32)), item[2], -len(item[1])))

    filler_groups.sort(key=lambda item: (-len(item[1]), item[2], -item[3]))
    best = None
    for left_group in left_candidates[:3]:
        if _deadline_hit(deadline, 0.02):
            break
        left_key, left_items, _left_short, _left_long, _aspect = left_group
        left_opt = _edge_group_orientation(left_items[0], sheet_w, sheet_h, mode="vertical_strip")
        if not left_opt:
            continue
        max_left_cols = min(
            len(left_items),
            max(1, int((sheet_w * 0.28 + kerf) // (int(left_opt["w"]) + kerf))),
            4,
        )
        min_left_cols = 3 if max_left_cols >= 3 else 2
        for left_count in range(max_left_cols, min_left_cols - 1, -1):
            left_w = left_count * int(left_opt["w"]) + kerf * max(0, left_count - 1)
            if left_w <= 0 or left_w >= sheet_w * 0.38:
                continue
            frame_x = left_w + kerf
            frame_w = sheet_w - frame_x
            if frame_w < sheet_w * 0.45:
                continue

            for rail_group in rail_candidates[:3]:
                if _deadline_hit(deadline, 0.02):
                    break
                rail_key, rail_items, _rail_short, _rail_long, _rail_aspect = rail_group
                if rail_key == left_key:
                    continue
                rail_opt = _edge_group_orientation(rail_items[0], sheet_w, sheet_h, mode="low_row")
                if not rail_opt:
                    continue
                rail_cap = _edge_row_capacity(rail_opt, sheet_w, kerf)
                rail_count = min(len(rail_items), rail_cap, 4)
                if rail_count < 2:
                    continue

                remaining_fillers = [fg for fg in filler_groups if fg[0] not in (left_key, rail_key)]
                if len(remaining_fillers) < 2:
                    continue
                bottom_choices = []
                for group in remaining_fillers:
                    key, items, _short, _long, _qty = group
                    opt = _edge_group_orientation(items[0], sheet_w, sheet_h, mode="low_row")
                    if (
                        not opt
                        or int(opt["h"]) > int(sheet_h * 0.18)
                        or int(opt["h"]) < max(120, int(sheet_h * 0.11))
                    ):
                        continue
                    cap = _edge_row_capacity(opt, sheet_w, kerf)
                    count = min(len(items), cap)
                    if count < 4:
                        continue
                    fill_ratio = (count * opt["w"] + kerf * max(0, count - 1)) / float(sheet_w)
                    while fill_ratio > 0.84 and count > 4:
                        count -= 1
                        fill_ratio = (count * opt["w"] + kerf * max(0, count - 1)) / float(sheet_w)
                    bottom_choices.append((abs(0.78 - fill_ratio), -count, int(opt["h"]), key, items, opt, count))
                bottom_choices.sort()

                for _ratio, _neg_count, _h, bottom_key, bottom_items, bottom_opt, bottom_count in bottom_choices[:3]:
                    top_groups = [fg for fg in remaining_fillers if fg[0] != bottom_key]
                    top_rows = []
                    top_used_h = 0
                    top_used_ids = set()
                    available_top_w = frame_w
                    for group in sorted(top_groups, key=lambda fg: (-fg[2] * fg[3], -len(fg[1])))[:5]:
                        key, items, _short, _long, _qty = group
                        opt = _edge_group_orientation(
                            items[0],
                            available_top_w,
                            int(sheet_h * 0.18),
                            mode="top_compact",
                        )
                        if not opt or int(opt["h"]) > int(sheet_h * 0.18):
                            continue
                        used_w = sum(row[4] for row in top_rows) + kerf * max(0, len(top_rows))
                        remaining_w = available_top_w - used_w
                        if remaining_w <= 0:
                            continue
                        cap = _edge_row_capacity(opt, remaining_w, kerf)
                        if not top_rows and len(top_groups) > 1:
                            cap = min(cap, max(3, cap // 2))
                        count = min(len(items), cap, 8)
                        if count < 3:
                            continue
                        row_w = count * int(opt["w"]) + kerf * max(0, count - 1)
                        top_rows.append((key, items, opt, count, row_w))
                        top_used_h = max(top_used_h, int(opt["h"]))
                        top_used_ids.add(key)
                        # One mixed top row is enough; add a second family only
                        # when it still keeps the row compact.
                        used_w = sum(row[4] for row in top_rows) + kerf * max(0, len(top_rows) - 1)
                        if used_w >= available_top_w * 0.78 or len(top_rows) >= 2:
                            break
                    if not top_rows:
                        continue

                    bottom_h = int(bottom_opt["h"]) + kerf + int(rail_opt["h"])
                    top_h = int(top_used_h)
                    central_y = bottom_h + kerf
                    central_h = sheet_h - bottom_h - top_h - 2 * kerf
                    central_w = frame_w
                    if central_h < sheet_h * 0.35 or central_w < sheet_w * 0.45:
                        continue

                    pool_by_key = {key: list(items) for key, items in groups.items()}
                    placements = []
                    free = []

                    left_chosen = _edge_take_group(pool_by_key, left_key, left_count)
                    x = 0
                    for piece in left_chosen:
                        placements.append(_placement(piece, x, sheet_h - int(left_opt["h"]), int(left_opt["w"]), int(left_opt["h"])))
                        if sheet_h - int(left_opt["h"]) - kerf > 0:
                            free.append((x, 0, int(left_opt["w"]), sheet_h - int(left_opt["h"]) - kerf))
                        x += int(left_opt["w"]) + kerf

                    bottom_chosen = _edge_take_group(pool_by_key, bottom_key, bottom_count)
                    x = 0
                    for piece in bottom_chosen:
                        placements.append(_placement(piece, x, 0, int(bottom_opt["w"]), int(bottom_opt["h"])))
                        x += int(bottom_opt["w"]) + kerf
                    if x - kerf + kerf < sheet_w:
                        free.append((x, 0, sheet_w - x, int(bottom_opt["h"])))

                    rail_chosen = _edge_take_group(pool_by_key, rail_key, rail_count)
                    x = 0
                    rail_y = int(bottom_opt["h"]) + kerf
                    for piece in rail_chosen:
                        placements.append(_placement(piece, x, rail_y, int(rail_opt["w"]), int(rail_opt["h"])))
                        x += int(rail_opt["w"]) + kerf
                    if x - kerf + kerf < sheet_w:
                        free.append((x, rail_y, sheet_w - x, int(rail_opt["h"])))

                    top_y = sheet_h - top_h
                    x = frame_x
                    for key, _items, opt, count, _row_w in top_rows:
                        chosen = _edge_take_group(pool_by_key, key, count)
                        for piece in chosen:
                            placements.append(_placement(piece, x, top_y, int(opt["w"]), int(opt["h"])))
                            x += int(opt["w"]) + kerf
                    if x < sheet_w:
                        free.append((x, top_y, sheet_w - x, top_h))

                    # The prize: one big reusable offcut protected in the centre.
                    free.append((frame_x, central_y, central_w, central_h))

                    sheet = _sheet_from_pattern(
                        sheet_w,
                        sheet_h,
                        kerf,
                        placements,
                        free,
                        "edge_frame_tail",
                    )
                    if not _sheet_geometry_ok(sheet):
                        continue
                    used_area = sum(pl["w"] * pl["h"] for pl in placements)
                    central_value = _offcut_value(central_w, central_h, sheet_w, sheet_h)
                    sc_key = (
                        -central_value,
                        -used_area,
                        _guillotine_tree_cut_count(sheet),
                        len(placements),
                    )
                    if best is None or sc_key < best[0]:
                        best = (sc_key, sheet)

    if best is None:
        return [], list(pieces)
    used_ids = set(_ids_in_sheets([best[1]]) or ())
    return [best[1]], [p for p in pieces if int(p["id"]) not in used_ids]

def _varied_shelf_orientations(piece, sheet_w, sheet_h):
    w0, h0 = int(piece["w"]), int(piece["h"])
    seen = set()
    for w, h, rotated in ((w0, h0, False), (h0, w0, True)):
        if (w, h) in seen:
            continue
        seen.add((w, h))
        if w <= sheet_w and h <= sheet_h:
            yield {
                "id": int(piece["id"]),
                "w": int(w),
                "h": int(h),
                "rotated": bool(rotated),
                "orig": piece,
            }


def construct_varied_dense_shelves(pieces, sheet_w, sheet_h, *, kerf=3, seed=0, deadline=None):
    """Pack high-variety inventories into dense guillotine shelf rows.

    This targets the opposite of the repeat-block cases: many unique sizes with
    only light repetition. CutList-like behaviour here is a best-fit shelf pack:
    large panels establish row heights, then smaller panels are absorbed into the
    row ends and height slack before a new sheet is opened.
    """
    if len(pieces) < 40:
        return [], list(pieces)
    unique_keys = {_piece_type_key(p) for p in pieces}
    if len(unique_keys) < max(24, int(len(pieces) * 0.22)):
        return [], list(pieces)

    ordered = sorted(
        pieces,
        key=lambda p: (
            int(p["w"]) * int(p["h"]),
            max(int(p["w"]), int(p["h"])),
            min(int(p["w"]), int(p["h"])),
            -int(p["id"]),
        ),
        reverse=True,
    )

    # Each sheet is a list of row dicts. Entry x/y coordinates are resolved after
    # packing so rows remain simple guillotine shelves.
    packed = []
    unplaced = []

    def used_height(rows):
        return sum(row["h"] for row in rows) + int(kerf) * max(0, len(rows) - 1)

    for piece_idx, piece in enumerate(ordered):
        if _deadline_hit(deadline, 0.02):
            unplaced.extend(ordered[piece_idx:])
            return [], list(pieces)
        opts = list(_varied_shelf_orientations(piece, sheet_w, sheet_h))
        if not opts:
            unplaced.append(piece)
            continue

        best = None
        for sheet_idx, rows in enumerate(packed):
            sheet_used_h = used_height(rows)
            for opt in opts:
                for row_idx, row in enumerate(rows):
                    next_w = row["used_w"] + (kerf if row["entries"] else 0) + opt["w"]
                    if opt["h"] > row["h"] or next_w > sheet_w:
                        continue
                    row_end_waste = (sheet_w - next_w) * row["h"]
                    height_slack = row["h"] - opt["h"]
                    key = (
                        row_end_waste,
                        height_slack * max(1, opt["w"]),
                        sheet_idx,
                        row_idx,
                        opt["h"],
                    )
                    if best is None or key < best[0]:
                        best = (key, sheet_idx, row_idx, opt)

                extra_h = (kerf if rows else 0) + opt["h"]
                if sheet_used_h + extra_h <= sheet_h:
                    row_waste = (sheet_w - opt["w"]) * opt["h"]
                    key = (
                        row_waste,
                        sheet_h - sheet_used_h - extra_h,
                        sheet_idx,
                        len(rows),
                        opt["h"],
                    )
                    if best is None or key < best[0]:
                        best = (key, sheet_idx, len(rows), opt)

        if best is None:
            opt = min(opts, key=lambda item: (item["h"], -item["w"]))
            packed.append([{
                "h": int(opt["h"]),
                "used_w": int(opt["w"]),
                "entries": [opt],
            }])
            continue

        _key, sheet_idx, row_idx, opt = best
        if row_idx == len(packed[sheet_idx]):
            packed[sheet_idx].append({
                "h": int(opt["h"]),
                "used_w": int(opt["w"]),
                "entries": [opt],
            })
        else:
            row = packed[sheet_idx][row_idx]
            row["used_w"] += (kerf if row["entries"] else 0) + int(opt["w"])
            row["entries"].append(opt)

    if unplaced:
        return [], list(pieces)

    sheets = []
    for sheet_idx, rows in enumerate(packed, 1):
        placements = []
        free = []
        y = 0
        for row in rows:
            x = 0
            for opt in row["entries"]:
                placements.append({
                    "item": int(opt["id"]),
                    "x": int(x),
                    "y": int(y),
                    "w": int(opt["w"]),
                    "h": int(opt["h"]),
                    "rotated": bool(opt["rotated"]),
                })
                x += int(opt["w"]) + int(kerf)
            row_used_w = int(row["used_w"])
            if row_used_w + kerf < sheet_w:
                free.append((int(row_used_w + kerf), int(y), int(sheet_w - row_used_w - kerf), int(row["h"])))
            y += int(row["h"]) + int(kerf)
        top_y = used_height(rows) + int(kerf)
        if top_y < sheet_h:
            free.append((0, int(top_y), int(sheet_w), int(sheet_h - top_y)))
        sheet = _sheet_from_pattern(
            sheet_w,
            sheet_h,
            kerf,
            placements,
            free,
            "varied_dense_shelves_%02d" % sheet_idx,
        )
        if not _sheet_geometry_ok(sheet):
            return [], list(pieces)
        sheets.append(sheet)

    used_ids = set(_ids_in_sheets(sheets) or ())
    if len(used_ids) != len(pieces):
        return [], list(pieces)
    return sheets, []


# --------------------------------------------------------------------------
# Tall strip column constructor
# --------------------------------------------------------------------------
def _tall_column_item(p, sheet_w, sheet_h):
    """Orient a part as a tall saw strip when that is clearly natural.

    This targets jobs made from sheet-height strips: e.g. 300x1220 and 600x1220
    parts on a 2440x1220 sheet, plus occasional long/narrow recut strips that
    rotate into 235-wide columns. It deliberately rejects broad mixed anchors so
    it does not fight the anchor/filler constructors.
    """
    w0, h0 = int(p["w"]), int(p["h"])
    max_column_w = max(620, int(sheet_w * 0.28))
    options = []
    for w, h, rotated in ((w0, h0, False), (h0, w0, True)):
        if w > max_column_w or h > sheet_h:
            continue
        full_height = abs(h - sheet_h) <= 5
        long_narrow = h >= sheet_h * 0.62 and (float(h) / float(w or 1)) >= 2.5
        if not (full_height or long_narrow):
            continue
        options.append((0 if full_height else 1, w, -h, {
            "id": p["id"],
            "w": int(w),
            "h": int(h),
            "rotated": bool(rotated),
            "full_height": bool(full_height),
        }))
    if not options:
        return None
    options.sort()
    return options[0][3]


def _tall_pattern_width(pattern, types, kerf):
    qty = sum(pattern)
    if qty <= 0:
        return 0
    return sum(types[idx]["w"] * count for idx, count in enumerate(pattern)) + kerf * (qty - 1)


def _make_tall_strip_sheet(pattern, types, sheet_w, sheet_h, kerf, pools=None):
    sheet = _Sheet(sheet_w, sheet_h, kerf)
    sheet.placements = []
    columns = []
    for idx, count in enumerate(pattern):
        if count <= 0:
            continue
        typ = types[idx]
        for _i in range(count):
            if pools is not None:
                item = pools[idx].pop(0)
                item_id = item["id"]
            else:
                item_id = "%s:%s" % (idx, _i)
            columns.append({
                "item": item_id,
                "w": typ["w"],
                "h": typ["h"],
                "rotated": typ["rotated"],
                "full_height": typ["full_height"],
            })

    # Full-height strips first, grouped by width; short recut strips at the end,
    # top-aligned like CutList so the small offcuts sit below them.
    columns.sort(key=lambda c: (not c["full_height"], -c["w"], -c["h"], str(c["item"])))
    x = 0
    free = []
    for col in columns:
        y = 0 if col["full_height"] else int(sheet_h - col["h"])
        sheet.placements.append({
            "item": col["item"],
            "x": int(x),
            "y": int(y),
            "w": int(col["w"]),
            "h": int(col["h"]),
            "rotated": bool(col["rotated"]),
        })
        if y > kerf:
            free.append((int(x), 0, int(col["w"]), int(y - kerf)))
        x += col["w"] + kerf

    used_w = x - kerf if columns else 0
    right_x = used_w + kerf
    if right_x < sheet_w:
        free.append((int(right_x), 0, int(sheet_w - right_x), int(sheet_h)))
    sheet.free = [r for r in free if r[2] > 0 and r[3] > 0]
    sheet.strategy = "tall_strip_columns"
    return sheet


def construct_tall_strip_columns(pieces, sheet_w, sheet_h, *, kerf=3, seed=0, deadline=None):
    """Bin-pack tall strip columns across sheets.

    CutList handles pure 1220-long strip jobs by treating each part as a column
    and packing column widths to the sheet width. This constructor creates that
    exact family of layouts, including rotated narrow recut strips.
    """
    if len(pieces) < 4 or len(pieces) > 80:
        return [], list(pieces)

    oriented = []
    for p in pieces:
        item = _tall_column_item(p, sheet_w, sheet_h)
        if item is None:
            return [], list(pieces)
        oriented.append(item)
    full_height_count = sum(1 for item in oriented if item["full_height"])
    if full_height_count < max(2, int(len(oriented) * 0.60)):
        return [], list(pieces)

    groups = {}
    for item in oriented:
        key = (item["w"], item["h"], item["rotated"], item["full_height"])
        groups.setdefault(key, []).append(item)
    if len(groups) > 6:
        return [], list(pieces)

    types = []
    for (w, h, rotated, full_height), items in groups.items():
        types.append({
            "w": int(w),
            "h": int(h),
            "rotated": bool(rotated),
            "full_height": bool(full_height),
            "count": len(items),
            "items": list(items),
        })
    types.sort(key=lambda t: (not t["full_height"], -t["w"], -t["h"]))
    initial = tuple(t["count"] for t in types)

    patterns = []

    def build_patterns(idx, counts):
        if _deadline_hit(deadline, 0.02):
            return
        if idx == len(types):
            pattern = tuple(counts)
            if sum(pattern) <= 0:
                return
            if _tall_pattern_width(pattern, types, kerf) <= sheet_w:
                patterns.append(pattern)
            return
        for count in range(types[idx]["count"] + 1):
            counts.append(count)
            build_patterns(idx + 1, counts)
            counts.pop()

    build_patterns(0, [])
    patterns.sort(key=lambda p: (-_tall_pattern_width(p, types, kerf), -sum(p), p))

    from functools import lru_cache as _local_lru_cache

    @_local_lru_cache(maxsize=None)
    def solve(state):
        if sum(state) == 0:
            return (), score([], [])
        first = next((idx for idx, count in enumerate(state) if count), None)
        best = None
        for pattern in patterns:
            if _deadline_hit(deadline, 0.02):
                break
            if first is not None and pattern[first] <= 0:
                continue
            if any(pattern[idx] > state[idx] for idx in range(len(types))):
                continue
            next_state = tuple(state[idx] - pattern[idx] for idx in range(len(types)))
            sub_patterns, _sub_score = solve(next_state)
            if sub_patterns is None:
                continue
            candidate = (pattern,) + sub_patterns
            sheets = [_make_tall_strip_sheet(pat, types, sheet_w, sheet_h, kerf) for pat in candidate]
            sc = score(sheets, [])
            if best is None or (sc, len(candidate), candidate) < (best[1], len(best[0]), best[0]):
                best = (candidate, sc)
        if best is None:
            return None, (999, 0, 0)
        return best

    best_patterns, _best_score = solve(initial)
    if not best_patterns:
        return [], list(pieces)

    pools = [list(t["items"]) for t in types]
    sheets = [_make_tall_strip_sheet(pattern, types, sheet_w, sheet_h, kerf, pools=pools) for pattern in best_patterns]
    return sheets, []


# --------------------------------------------------------------------------
# Objective + seeded search
# --------------------------------------------------------------------------
def _offcut_rects(sheet):
    """Leftover free rectangles on a built sheet that are big enough to matter."""
    return [r for r in sheet.free if r[2] >= 80 and r[3] >= 80]


# Tier 3 — acrylic handling. An offcut narrower than this on its short side is a
# snapping/whip hazard on the saw and rarely reusable; treat it as junk.
_UNSAFE_SLIVER_MM = 60


def _scatter_penalty(sheets):
    """Tier 3 — same-part GROUPING. For each piece size, measure how spread out
    its instances are: bounding box of all its copies minus their combined area.
    Tight = pieces grouped (operator grabs a stack, easy to sort/label);
    scattered = pieces strewn across sheets. Lower is better. Scaled down so it's
    a gentle tie-break, never overriding offcut value or saw effort."""
    from collections import defaultdict
    waste = 0
    for s in sheets:
        by_size = defaultdict(list)
        for pl in s.placements:
            by_size[tuple(sorted((pl["w"], pl["h"])))].append(pl)
        for pls in by_size.values():
            if len(pls) < 2:
                continue
            x0 = min(pl["x"] for pl in pls)
            y0 = min(pl["y"] for pl in pls)
            x1 = max(pl["x"] + pl["w"] for pl in pls)
            y1 = max(pl["y"] + pl["h"] for pl in pls)
            used = sum(pl["w"] * pl["h"] for pl in pls)
            waste += max(0, (x1 - x0) * (y1 - y0) - used)
    return waste // 20000          # gentle: a few units, not hundreds


def _pattern_repeat_penalty(sheets, max_occurrences=2):
    """CutList-style protection against overusing one identical sheet mosaic."""
    signatures = Counter()
    for sheet in sheets or []:
        dims = _sheet_mosaic_signature(sheet)
        if dims:
            signatures[dims] += 1
    penalty = 0
    for count in signatures.values():
        if count > max_occurrences:
            penalty += (count - max_occurrences) * 500
    return penalty


def _sheet_mosaic_signature(sheet):
    """Dimension-count signature for one reusable sheet pattern."""
    dims = Counter()
    for pl in getattr(sheet, "placements", None) or []:
        dims[tuple(sorted((int(pl["w"]), int(pl["h"]))))] += 1
    return tuple(sorted(dims.items()))


def _simple_saw_metrics(sheet):
    """Fast saw-effort estimate used inside search scoring.

    It is intentionally cheap because the beam calls it thousands of times. The
    public `saw_metrics` below is more accurate for reporting/rendered layouts.
    """
    from collections import defaultdict
    strips = defaultdict(list)
    for pl in sheet.placements or []:
        strips[int(pl["x"])].append(pl)

    rip_widths = set()
    crosscut_lengths = set()
    cut_count = 0
    xs = sorted(strips)
    cut_count += max(0, len(xs) - 1)
    if sheet.placements and max(int(pl["x"]) + int(pl["w"]) for pl in sheet.placements) < int(sheet.w) - 1:
        cut_count += 1
    for x in xs:
        strip = strips[x]
        rip_widths.add(max(int(pl["w"]) for pl in strip))
        for pl in strip:
            crosscut_lengths.add(int(pl["h"]))
        cut_count += len(strip)
        if max(int(pl["y"]) + int(pl["h"]) for pl in strip) < int(sheet.h) - 1:
            cut_count += 1
    return len(rip_widths) + len(crosscut_lengths), cut_count


def _axis_saw_metrics(sheet):
    """Fast panel-saw axis estimate used for fence settings and fallback cuts.

    The first v2 metric grouped placements only by exact x coordinate. That made
    CutList-like lane sheets look far worse than they really are, because a row
    of adjacent pieces was counted as one crosscut per panel instead of as shared
    saw segments. This estimator evaluates both a column-first and row-first saw
    plan, then keeps the cheaper one.
    """
    rects = [
        (int(pl["x"]), int(pl["y"]), int(pl["w"]), int(pl["h"]))
        for pl in (sheet.placements or [])
        if int(pl["w"]) > 0 and int(pl["h"]) > 0
    ]
    if not rects:
        return 0, 0

    def norm_segment(x1, y1, x2, y2):
        x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
        if (x2, y2) < (x1, y1):
            x1, y1, x2, y2 = x2, y2, x1, y1
        return x1, y1, x2, y2

    def axis_plan(axis_rects, axis_w, axis_h, tol=1):
        segments = {}
        fence_values = set()

        def add(x1, y1, x2, y2, kind, fence_value=None):
            if abs(x1 - x2) <= tol and abs(y1 - y2) <= tol:
                return
            seg = norm_segment(x1, y1, x2, y2)
            current = segments.get(seg)
            if current != "separate":
                segments[seg] = kind
            if fence_value is not None and fence_value > tol:
                fence_values.add(int(round(fence_value)))

        min_x = min(x for x, _y, _w, _h in axis_rects)
        max_x = max(x + w for x, _y, w, _h in axis_rects)
        if min_x > tol:
            add(min_x, 0, min_x, axis_h, "trim", min_x)
        if max_x < axis_w - tol:
            add(max_x, 0, max_x, axis_h, "trim", axis_w - max_x)

        stacks = {}
        for rect in axis_rects:
            stacks.setdefault(rect[0], []).append(rect)
        for x in sorted(stacks)[1:]:
            add(x, 0, x, axis_h, "separate", x)

        for _stack_x, strip in sorted(stacks.items()):
            stack_x0 = min(x for x, _y, _w, _h in strip)
            stack_x1 = max(x + w for x, _y, w, _h in strip)
            stack_y0 = min(y for _x, y, _w, _h in strip)
            stack_y1 = max(y + h for _x, y, _w, h in strip)
            if stack_y0 > tol:
                add(stack_x0, stack_y0, stack_x1, stack_y0, "trim", stack_y0)
            if stack_y1 < axis_h - tol:
                add(stack_x0, stack_y1, stack_x1, stack_y1, "trim", axis_h - stack_y1)

            bands = {}
            for rect in strip:
                bands.setdefault(rect[1], []).append(rect)
            for y in sorted(bands)[1:]:
                add(stack_x0, y, stack_x1, y, "separate", y)

            for _band_y, row in sorted(bands.items()):
                row = sorted(row, key=lambda r: r[0])
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
                    fence_values.add(int(w))
                    fence_values.add(int(h))

        trim_cuts = sum(1 for kind in segments.values() if kind == "trim")
        return len(fence_values), len(segments), trim_cuts

    def simple_fence_estimate():
        from collections import defaultdict
        strips = defaultdict(list)
        for pl in sheet.placements or []:
            strips[int(pl["x"])].append(pl)
        rip_widths = set()
        crosscut_lengths = set()
        for strip in strips.values():
            rip_widths.add(max(int(pl["w"]) for pl in strip))
            for pl in strip:
                crosscut_lengths.add(int(pl["h"]))
        return len(rip_widths) + len(crosscut_lengths)
    column = axis_plan(rects, int(sheet.w), int(sheet.h))
    transposed = [(y, x, h, w) for x, y, w, h in rects]
    row = axis_plan(transposed, int(sheet.h), int(sheet.w))
    _axis_fence, cut_count, _trim = min(
        (column, row),
        key=lambda item: (int(item[1]), int(item[2]), int(item[0])),
    )
    return int(simple_fence_estimate()), int(cut_count)


def _guillotine_tree_cut_count(sheet):
    """Estimate CutList-style guillotine split count for a generated sheet.

    CutList's reported sheet cuts correspond to internal nodes in the guillotine
    split tree. For our constructed sheets the remaining free rectangles are the
    waste leaves we still know about, so final panels + free leaves - 1 is a
    better solver objective than coordinate-line counting when choosing between
    two saw-valid mosaics. Fall back to the line estimator if a sheet has no free
    leaves, which can happen for completely filled or imported layouts.
    """
    panels = len(getattr(sheet, "placements", None) or [])
    if panels <= 0:
        return 0
    free_leaves = len(getattr(sheet, "free", None) or [])
    if free_leaves <= 0:
        return int(_axis_saw_metrics(sheet)[1])
    return max(0, int(panels) + int(free_leaves) - 1)


def saw_metrics(sheet):
    """Tier 2 — CutList-style saw effort for one sheet.

    CutList's reported cuts track the guillotine split tree more closely than
    our older coordinate-line estimate. Use that tree count for ranking and
    reporting, while keeping the axis estimator's fence-setting count.
    """
    fence, _axis_cuts = _axis_saw_metrics(sheet)
    return int(fence), int(_guillotine_tree_cut_count(sheet))


def _offcut_value(w, h, sheet_w, sheet_h, tol=5):
    """Reusability value of one offcut. The shape rules, in priority order:

    1. FULL-DIMENSION offcut (spans the whole sheet width OR height) — this is a
       PRIZE regardless of aspect ratio: it comes off in one straight rip and is
       a full-size standard strip you nest the next job on (CutList's 2440x659).
       It gets the +30% bonus and is NEVER treated as a sliver, even at 3.7:1.
    2. Otherwise, large long-rectangle offcuts are still valuable when the short
       side is practical to handle. CutList often protects a big central offcut
       like 2100x750; that should beat a scattered collection of small gaps.
    3. Otherwise, near-square-ish offcuts are good reusable blocks — full area.
    4. Otherwise it's a thin interior strip — near-useless, heavily discounted.
    """
    area = w * h
    full_dim = (abs(w - sheet_w) <= tol) or (abs(h - sheet_h) <= tol)
    if full_dim:
        sheet_long = max(sheet_w, sheet_h)
        full_long_dim = abs(w - sheet_long) <= tol or abs(h - sheet_long) <= tol
        if full_long_dim:
            bonus = 1.30 if min(w, h) >= 150 else 1.10
        else:
            # A strip spanning only the short edge is usable, but not as valuable
            # as a long-edge 2440-style offcut that becomes a standard stock strip.
            bonus = 0.70
        return area * bonus                    # full-width/height one-rip strip: PRIZE
    ar = max(w, h) / float(min(w, h))
    if min(w, h) >= 450 and max(w, h) >= max(sheet_w, sheet_h) * 0.65:
        return area * 1.05                     # big central reusable rectangle
    if ar > 3.2:
        return area * 0.1                      # interior strip: mostly junk
    return area                                # square-ish reusable block


def score(sheets, unplaced=()):
    """Tier 1 objective — LOWER is better.

        (n_sheets, -best_offcut_value_bucket, tree_cut_count, niceness)

    1. n_sheets            — fewest fresh sheets (the money lever).
    2. best_offcut_value   — MAXIMISE the single most valuable reusable offcut.
       Value = area, +30% if it's a full-dimension one-rip offcut (CutList's
       2440x659 bottom strip), -90% if it's a thin sliver. Once sheets are tied
       this is the only thing that matters: consolidate the fixed leftover into
       one big, cleanly-reusable sheet.
    3. tree_cut_count      — CutList-like guillotine split count. This is a
       first-class shop-floor cost, not a weak cosmetic tie-break.
    4. niceness            — blended material-handling tie-break: fence changes,
       offcut fragmentation, unsafe slivers and same-part scatter.
    """
    profile_started_at = time.monotonic() if _SHADOW_PROFILE_ENABLED else None
    if not sheets:
        return _profile_score_result((999, 0, 0, 0), profile_started_at)
    # HARD: a layout that didn't place every piece must never win. The caller
    # passes unplaced via score(sheets, unplaced); any unplaced piece dominates
    # the score so an incomplete layout always loses to a complete one.
    if unplaced:
        return _profile_score_result((999 + len(unplaced), 0, 0, 0), profile_started_at)
    n_sheets = len(sheets)
    sw, sh = sheets[0].w, sheets[0].h
    total_panels = sum(len(getattr(s, "placements", None) or []) for s in sheets)
    small_single_sheet_job = n_sheets == 1 and total_panels <= 12

    strip_area = 0
    offcut_count = 0
    best_value = 0
    best_full_dim_value = 0
    fence_changes = 0
    cut_count = 0
    sliver_count = 0       # Tier 3: offcuts too narrow to handle safely
    for s in sheets:
        for (x, y, w, h) in _offcut_rects(s):
            offcut_count += 1
            ar = max(w, h) / float(min(w, h))
            if ar > 2.5:
                strip_area += w * h
            # An offcut whose SHORT side is below a safe handling width is a
            # snapping/whip hazard in acrylic and rarely reusable — penalise.
            if min(w, h) < _UNSAFE_SLIVER_MM:
                sliver_count += 1
            value = _offcut_value(w, h, sw, sh)
            if small_single_sheet_job:
                sheet_short = min(sw, sh)
                full_dim = abs(w - sw) <= 5 or abs(h - sh) <= 5
                full_short_edge = (
                    (abs(w - sheet_short) <= 5 and h >= sheet_short * 0.35)
                    or (abs(h - sheet_short) <= 5 and w >= sheet_short * 0.35)
                )
                if full_dim and full_short_edge and min(w, h) >= sheet_short * 0.35:
                    value = max(value, (w * h) * 1.35)
            best_value = max(best_value, value)
            if abs(w - sw) <= 5 or abs(h - sh) <= 5:
                best_full_dim_value = max(best_full_dim_value, value)
        fc, cc = saw_metrics(s)
        fence_changes += fc
        cut_count += cc
    scatter = _scatter_penalty(sheets)         # Tier 3: same-part grouping
    repeats = _pattern_repeat_penalty(sheets)  # Tier 3: avoid cloning one mosaic too many
    # Only full-width/full-height strips get a hard tier. Ordinary interior scraps
    # are useful, but should not beat a much cleaner saw plan by themselves.
    best_bucket = int(best_full_dim_value) // 10000
    any_offcut_bucket = int(best_value) // 10000
    niceness = ((strip_area // 1000)           # waste trapped in thin strips
                + 20 * offcut_count            # fragmentation
                + 30 * fence_changes           # Tier 2: saw setups
                + 40 * sliver_count            # Tier 3: unsafe slivers
                + scatter                      # Tier 3: same-part scatter
                + repeats
                - 8 * any_offcut_bucket)       # soft reward for ordinary offcuts
    return _profile_score_result((n_sheets, -best_bucket, int(cut_count), niceness), profile_started_at)


def _best_offcut_info(sheets, *, full_dim_only=False):
    best = {
        "value": 0.0,
        "w": 0,
        "h": 0,
        "sheet": 0,
        "full_dim": False,
    }
    for sheet_idx, sheet in enumerate(sheets or [], 1):
        for _x, _y, w, h in _offcut_rects(sheet):
            full_dim = abs(w - sheet.w) <= 5 or abs(h - sheet.h) <= 5
            if full_dim_only and not full_dim:
                continue
            value = _offcut_value(w, h, sheet.w, sheet.h)
            if value > best["value"]:
                best = {
                    "value": float(value),
                    "w": int(w),
                    "h": int(h),
                    "sheet": int(sheet_idx),
                    "full_dim": bool(full_dim),
                }
    return best


def _format_offcut(info):
    if not info or not int(info.get("w") or 0) or not int(info.get("h") or 0):
        return ""
    return "%dx%d" % (int(info["w"]), int(info["h"]))


def _layout_metric_fields(sheets):
    best = _best_offcut_info(sheets)
    best_full = _best_offcut_info(sheets, full_dim_only=True)
    return {
        "best_offcut_mm": _format_offcut(best),
        "best_full_dim_offcut_mm": _format_offcut(best_full),
        "best_offcut_value": int(best.get("value") or 0),
        "best_full_dim_offcut_value": int(best_full.get("value") or 0),
    }


def _free_band_promise(sheets):
    best = 0.0
    for sheet in sheets or []:
        for _x, _y, w, h in _offcut_rects(sheet):
            full_dim = abs(w - sheet.w) <= 5 or abs(h - sheet.h) <= 5
            value = _offcut_value(w, h, sheet.w, sheet.h)
            if full_dim:
                value *= 1.10
            best = max(best, value)
    return int(best) // 10000


def _repeat_tail_promise(remaining, sheet_w, sheet_h, kerf):
    """Estimate whether remaining repeated panels can form a clean final tail.

    This is not the final score; it only protects promising partial states from
    being pruned before the tail-sheet generator gets a chance to finish them.
    """
    groups = {}
    for piece in remaining or []:
        groups.setdefault(_piece_type_key(piece), []).append(piece)
    best = 0.0
    for fillers in groups.values():
        if len(fillers) < 3:
            continue
        row_opt = _row_filler_orientation(fillers[0], sheet_w, sheet_h, kerf)
        if not row_opt:
            continue
        row_cap = int((sheet_w + kerf) // (int(row_opt["w"]) + kerf))
        if row_cap <= 0:
            continue
        for count in (min(len(fillers), row_cap), min(len(fillers), row_cap * 2)):
            rows, used_h = _band_rows_for_count(fillers[0], count, sheet_w, sheet_h, kerf)
            if not rows:
                continue
            offcut_h = sheet_h - used_h - kerf
            if offcut_h >= 80:
                best = max(best, _offcut_value(sheet_w, offcut_h, sheet_w, sheet_h))
    return int(best) // 10000


_LAST_V2_METRICS = {}
_LAST_V3_METRICS = {}
_SHADOW_PROFILE_ENABLED = False
_SHADOW_PROFILE_METRICS = {}


def last_search_v2_metrics():
    """Metrics from the most recent v2 search in this Python worker."""
    return dict(_LAST_V2_METRICS)


def last_search_v3_metrics():
    """Metrics from the most recent v3 shadow search in this Python worker."""
    return dict(_LAST_V3_METRICS)


def reset_shadow_profile(enabled=True):
    """Enable/reset lightweight profiling used by the benchmark runner."""
    global _SHADOW_PROFILE_ENABLED, _SHADOW_PROFILE_METRICS
    _SHADOW_PROFILE_ENABLED = bool(enabled)
    _SHADOW_PROFILE_METRICS = {
        "clone_calls": 0,
        "score_calls": 0,
        "score_ms": 0,
    }


def shadow_profile_metrics():
    return dict(_SHADOW_PROFILE_METRICS)


def _profile_inc(name, amount=1):
    if _SHADOW_PROFILE_ENABLED:
        _SHADOW_PROFILE_METRICS[name] = int(_SHADOW_PROFILE_METRICS.get(name) or 0) + int(amount or 0)


def _profile_score_result(result, started_at):
    if _SHADOW_PROFILE_ENABLED:
        _profile_inc("score_calls")
        _profile_inc("score_ms", _elapsed_ms(started_at))
    return result


def _clone_sheet(sheet):
    _profile_inc("clone_calls")
    clone = _Sheet(int(sheet.w), int(sheet.h), int(sheet.kerf))
    clone.placements = [dict(pl) for pl in (sheet.placements or [])]
    clone.free = [tuple(r) for r in (sheet.free or [])]
    clone.strategy = getattr(sheet, "strategy", "")
    return clone


def _mirror_sheet(sheet, *, horizontal=False, vertical=False, suffix=""):
    clone = _Sheet(int(sheet.w), int(sheet.h), int(sheet.kerf))
    clone.placements = []
    for pl in sheet.placements or []:
        x = int(pl["x"])
        y = int(pl["y"])
        w = int(pl["w"])
        h = int(pl["h"])
        if horizontal:
            x = int(sheet.w) - x - w
        if vertical:
            y = int(sheet.h) - y - h
        new_pl = dict(pl)
        new_pl.update({"x": int(x), "y": int(y)})
        clone.placements.append(new_pl)
    clone.placements.sort(key=lambda pl: (int(pl["y"]), int(pl["x"]), int(pl["item"])))
    clone.free = []
    for x, y, w, h in sheet.free or []:
        nx = int(sheet.w) - int(x) - int(w) if horizontal else int(x)
        ny = int(sheet.h) - int(y) - int(h) if vertical else int(y)
        clone.free.append((int(nx), int(ny), int(w), int(h)))
    clone.free.sort(key=lambda r: (r[1], r[0]))
    base = getattr(sheet, "strategy", "") or "pattern"
    clone.strategy = "%s%s" % (base, suffix)
    return clone


def _rotate_sheet_clockwise(sheet, sheet_w, sheet_h, pieces_by_id=None, suffix="_rotated"):
    """Rotate a guillotine sheet pattern into the production sheet frame.

    CutList-like column mosaics often appear with the stock shown as 1220x2440,
    while Odoo's canonical sheet frame is 2440x1220. A quarter-turn preserves
    every guillotine relationship, but lets the same pattern family search both
    long-axis and short-axis stacks without duplicating the constructor logic.
    """
    old_w = int(sheet.w)
    clone = _Sheet(int(sheet_w), int(sheet_h), int(sheet.kerf))
    clone.placements = []
    for pl in sheet.placements or []:
        x = int(pl["x"])
        y = int(pl["y"])
        w = int(pl["w"])
        h = int(pl["h"])
        new_pl = dict(pl)
        new_w = int(h)
        new_h = int(w)
        new_pl.update({
            "x": int(y),
            "y": int(old_w - x - w),
            "w": new_w,
            "h": new_h,
        })
        if pieces_by_id and int(pl["item"]) in pieces_by_id:
            item = pieces_by_id[int(pl["item"])]
            new_pl["rotated"] = int(new_w) != int(item["w"]) or int(new_h) != int(item["h"])
        else:
            new_pl["rotated"] = not bool(pl.get("rotated"))
        clone.placements.append(new_pl)
    clone.placements.sort(key=lambda pl: (int(pl["y"]), int(pl["x"]), int(pl["item"])))
    clone.free = []
    for x, y, w, h in sheet.free or []:
        clone.free.append((
            int(y),
            int(old_w - int(x) - int(w)),
            int(h),
            int(w),
        ))
    clone.free = [
        (int(x), int(y), int(w), int(h))
        for x, y, w, h in clone.free
        if int(w) > 0 and int(h) > 0
    ]
    clone.free.sort(key=lambda r: (r[1], r[0]))
    base = getattr(sheet, "strategy", "") or "pattern"
    clone.strategy = "%s%s" % (base, suffix)
    return clone


def _candidate_variants(cand):
    variants = [cand]
    sheets = cand.get("sheets") or []
    if len(sheets) != 1:
        return variants
    sheet = sheets[0]
    strategy = cand.get("strategy") or getattr(sheet, "strategy", "") or "pattern"
    if not sheet.placements:
        return variants
    # Mirroring keeps every guillotine relationship intact while letting the beam
    # try anchor-left/right and top/bottom strip versions of the same pattern.
    for axis, mirrored in (
        ("_right", _mirror_sheet(sheet, horizontal=True, suffix="_right")),
        ("_top", _mirror_sheet(sheet, vertical=True, suffix="_top")),
    ):
        variant = _candidate_from_sheets([mirrored], "%s%s" % (strategy, axis))
        if variant:
            variants.append(variant)
    return variants


def _ids_in_sheets(sheets):
    ids = []
    seen = set()
    for sheet in sheets or []:
        for pl in sheet.placements or []:
            item_id = int(pl["item"])
            if item_id in seen:
                return None
            seen.add(item_id)
            ids.append(item_id)
    return tuple(sorted(ids))


def _sheet_geometry_ok(sheet):
    placements = list(sheet.placements or [])
    for idx, a in enumerate(placements):
        ax0 = int(a["x"])
        ay0 = int(a["y"])
        ax1 = ax0 + int(a["w"])
        ay1 = ay0 + int(a["h"])
        if ax0 < 0 or ay0 < 0 or ax1 > int(sheet.w) or ay1 > int(sheet.h):
            return False
        for b in placements[idx + 1:]:
            bx0 = int(b["x"])
            by0 = int(b["y"])
            bx1 = bx0 + int(b["w"])
            by1 = by0 + int(b["h"])
            if ax0 < bx1 and bx0 < ax1 and ay0 < by1 and by0 < ay1:
                return False
    return True


def _candidate_from_sheets(sheets, strategy):
    sheets = [s for s in (sheets or []) if s and (s.placements or [])]
    if not sheets:
        return None
    if any(not _sheet_geometry_ok(s) for s in sheets):
        return None
    used = _ids_in_sheets(sheets)
    if not used:
        return None
    cloned = [_clone_sheet(s) for s in sheets]
    for sheet in cloned:
        if not getattr(sheet, "strategy", ""):
            sheet.strategy = strategy
    return {
        "sheets": cloned,
        "used": used,
        "strategy": strategy or "+".join(getattr(s, "strategy", "") for s in cloned),
    }


def _remaining_items(pieces_by_id, remaining_ids):
    return [pieces_by_id[item_id] for item_id in remaining_ids]


def _layout_strategy(sheets):
    names = [getattr(sheet, "strategy", "") or "free_rect" for sheet in sheets or []]
    return "+".join(names[-4:]) if names else ""


def _area_lower_bound(pieces, sheet_w, sheet_h):
    sheet_area = float(sheet_w * sheet_h)
    if sheet_area <= 0:
        return 999
    area = sum(float(_area(p)) for p in pieces)
    return int((area + sheet_area - 1) // sheet_area)


def _append_candidate_variants(candidates, cand, *, allow_mirror=True):
    if not cand:
        return 0
    added = 0
    variants = _candidate_variants(cand) if allow_mirror else [cand]
    for variant in variants:
        candidates.append(variant)
        added += 1
    return added


def _candidate_prune_key(cand, sheet_w, sheet_h):
    sheets = cand.get("sheets") or []
    used = tuple(cand.get("used") or ())
    used_area = 0
    cut_count = 0
    for sheet in sheets:
        cut_count += _guillotine_tree_cut_count(sheet)
        for pl in sheet.placements or []:
            used_area += int(pl["w"]) * int(pl["h"])
    full = int(_best_offcut_info(sheets, full_dim_only=True).get("value") or 0) // 10000
    best = int(_best_offcut_info(sheets).get("value") or 0) // 10000
    return (
        len(sheets),
        -int(used_area),
        -len(used),
        -int(full),
        int(cut_count),
        -int(best),
        cand.get("strategy") or "",
        tuple(used),
    )


def _prune_candidate_pool(candidates, sheet_w, sheet_h, limit):
    """Keep a bounded, strategy-diverse candidate pool.

    Several v2 generators can legitimately produce thousands of valid one-sheet
    variants. The beam only needs the best spread of those; retaining all of
    them makes the next partial-state layer balloon and can push an Odoo worker
    over its memory cap before the time budget expires.
    """
    limit = max(1, int(limit or 0))
    if len(candidates or []) <= limit:
        return candidates

    ranked = sorted(
        enumerate(candidates),
        key=lambda pair: (_candidate_prune_key(pair[1], sheet_w, sheet_h), pair[0]),
    )
    selected = []
    selected_indexes = set()

    def add(index, cand):
        if index in selected_indexes:
            return
        selected_indexes.add(index)
        selected.append(cand)

    primary = max(1, int(limit * 0.70))
    for index, cand in ranked[:primary]:
        add(index, cand)

    by_strategy = {}
    for index, cand in ranked:
        strategy = (cand.get("strategy") or "").split("_right", 1)[0].split("_top", 1)[0]
        by_strategy.setdefault(strategy, []).append((index, cand))
    for bucket in by_strategy.values():
        for index, cand in bucket[:3]:
            add(index, cand)
            if len(selected) >= limit:
                break
        if len(selected) >= limit:
            break

    if len(selected) < limit:
        for index, cand in ranked:
            add(index, cand)
            if len(selected) >= limit:
                break
    return selected[:limit]


def _v2_lane_recipe_candidates(remaining, sheet_w, sheet_h, kerf, *, stats=None, deadline=None, allow_expensive=False):
    """Generate count-based guillotine lane recipes.

    A lane recipe is a rip strip cut from the sheet, crosscut into repeated rows,
    with the leftover cap filled by a small guillotine row mosaic. This searches
    piece-type counts first and assigns real ids only when emitting a candidate.
    """
    started_at = time.monotonic()
    if len(remaining or []) < 4:
        return []

    groups = {}
    for piece in remaining:
        groups.setdefault(_piece_type_key(piece), []).append(piece)
    available_by_key = Counter({key: len(items) for key, items in groups.items()})
    sheet_area = max(1, int(sheet_w) * int(sheet_h))

    oriented_groups = []
    seen_orientations = set()
    for key, items in groups.items():
        sample = items[0]
        for opt in _piece_orientations(sample, sheet_w, sheet_h):
            sig = (key, int(opt["w"]), int(opt["h"]))
            if sig in seen_orientations:
                continue
            seen_orientations.add(sig)
            oriented_groups.append({
                "key": key,
                "items": list(items),
                "w": int(opt["w"]),
                "h": int(opt["h"]),
                "rotated": bool(opt["rotated"]),
                "area": int(key[0]) * int(key[1]),
                "qty": len(items),
            })
    if not oriented_groups:
        return []

    oriented_groups.sort(key=lambda group: (
        -min(int(group["qty"]), 12) * int(group["area"]),
        int(group["w"]),
        -int(group["h"]),
        group["key"],
    ))
    row_group_limit = 44 if allow_expensive else 28
    lane_primary_limit = 34 if allow_expensive else 20

    def group_remaining(group, used_counts):
        return int(available_by_key[group["key"]]) - int(used_counts.get(group["key"], 0))

    def count_choices(max_count):
        max_count = int(max_count)
        if max_count <= 0:
            return []
        choices = {max_count, 1}
        for value in (2, 3, 4, 5, 6, max_count - 1, max_count - 2):
            if 1 <= value <= max_count:
                choices.add(value)
        return sorted(choices, reverse=True)

    def row_options(width_limit, height_limit, used_counts, *, limit=36):
        width_limit = int(width_limit)
        height_limit = int(height_limit)
        if width_limit <= 0 or height_limit <= 0:
            return []
        fitting = [
            group for group in oriented_groups
            if int(group["w"]) <= width_limit
            and int(group["h"]) <= height_limit
            and group_remaining(group, used_counts) > 0
        ]
        fitting.sort(key=lambda group: (
            -int(group["area"]) * min(group_remaining(group, used_counts), 8),
            int(group["w"]),
            int(group["h"]),
            group["key"],
        ))
        options = []

        def add(entries):
            counts = Counter()
            panel_count = 0
            for group, count in entries:
                count = int(count)
                if count <= 0:
                    return
                counts[group["key"]] += count
                panel_count += count
            if any(int(used_counts.get(key, 0)) + count > int(available_by_key[key]) for key, count in counts.items()):
                return
            row_w = (
                sum(int(group["w"]) * int(count) for group, count in entries)
                + int(kerf) * max(0, panel_count - 1)
            )
            row_h = max(int(group["h"]) for group, _count in entries)
            if row_w > width_limit or row_h > height_limit:
                return
            row_area = sum(int(group["w"]) * int(group["h"]) * int(count) for group, count in entries)
            width_waste = width_limit - row_w
            exact_width = int(width_waste <= max(3, int(width_limit) * 0.03))
            rank = (
                -exact_width,
                width_waste // 25,
                -int(row_area),
                int(row_h),
                -int(panel_count),
                tuple((group["key"], int(group["w"]), int(group["h"]), int(count)) for group, count in entries),
            )
            options.append((rank, [(group, int(count)) for group, count in entries], int(row_w), int(row_h), int(row_area), counts))

        for group in fitting[:row_group_limit]:
            max_across = min(
                group_remaining(group, used_counts),
                int((width_limit + int(kerf)) // (int(group["w"]) + int(kerf))),
            )
            for count in count_choices(max_across):
                add([(group, count)])

        pair_pool = fitting[:(36 if allow_expensive else 22)]
        for left, right in combinations(pair_pool, 2):
            if _deadline_hit(deadline, 0.003):
                break
            add([(left, 1), (right, 1)])
            if group_remaining(left, used_counts) >= 2:
                add([(left, 2), (right, 1)])
            if group_remaining(right, used_counts) >= 2:
                add([(left, 1), (right, 2)])

        options.sort(key=lambda item: item[0])
        kept = []
        seen = set()

        def option_sig(option):
            _rank, entries, row_w, row_h, _area, _counts = option
            return (
                int(row_w),
                int(row_h),
                tuple((group["key"], int(group["w"]), int(group["h"]), int(count)) for group, count in entries),
            )

        def keep(source, max_items):
            for option in source:
                sig = option_sig(option)
                if sig in seen:
                    continue
                seen.add(sig)
                kept.append(option)
                if len(kept) >= max_items:
                    return

        keep(options, max(1, int(limit) // 2))
        family_buckets = {}
        for option in options:
            _rank, _entries, _row_w, _row_h, _area, counts = option
            if not counts:
                continue
            family = max(counts, key=lambda key: int(key[0]) * int(key[1]) * int(counts[key]))
            family_buckets.setdefault(family, []).append(option)
        for family in sorted(family_buckets):
            keep(sorted(family_buckets[family], key=lambda item: item[0])[:3], int(limit))
        keep(sorted(options, key=lambda item: (-int(item[4]), item[0])), int(limit))
        return kept[:limit]

    def cap_mosaics(width_limit, height_limit, used_counts, *, limit=48):
        width_limit = int(width_limit)
        height_limit = int(height_limit)
        if width_limit <= 0 or height_limit <= 0:
            return []
        seed = {
            "w": 0,
            "h": 0,
            "area": 0,
            "counts": Counter(),
            "placements": [],
            "rows": 0,
        }
        states = [seed]
        results = []
        seen = set()
        depth_limit = 5 if allow_expensive else 3
        state_keep = 42 if allow_expensive else 18

        def state_sig(state):
            return (
                int(state["w"]),
                int(state["h"]),
                tuple(sorted(state["counts"].items())),
                tuple(
                    (
                        pl["group"]["key"],
                        int(pl["group"]["w"]),
                        int(pl["group"]["h"]),
                        int(pl["x"]),
                        int(pl["y"]),
                    )
                    for pl in state["placements"]
                ),
            )

        def state_rank(state):
            return (
                -int(state["area"]),
                int(width_limit) - int(state["w"]),
                int(height_limit) - int(state["h"]),
                -int(state["rows"]),
                tuple(sorted(state["counts"].items())),
            )

        for _depth in range(depth_limit):
            if _deadline_hit(deadline, 0.004):
                break
            next_states = []
            for state in states:
                if _deadline_hit(deadline, 0.004):
                    break
                gap = int(kerf) if int(state["rows"]) else 0
                remaining_h = int(height_limit) - int(state["h"]) - gap
                if remaining_h <= 0:
                    continue
                row_used = Counter(used_counts)
                row_used.update(state["counts"])
                for _rank, entries, row_w, row_h, row_area, entry_counts in row_options(
                    width_limit,
                    remaining_h,
                    row_used,
                    limit=(42 if allow_expensive else 20),
                ):
                    y = int(state["h"]) + gap
                    x = 0
                    placements = [dict(pl) for pl in state["placements"]]
                    for group, count in entries:
                        for _idx in range(int(count)):
                            placements.append({
                                "group": group,
                                "x": int(x),
                                "y": int(y),
                            })
                            x += int(group["w"]) + int(kerf)
                    counts = Counter(state["counts"])
                    counts.update(entry_counts)
                    child = {
                        "w": max(int(state["w"]), int(row_w)),
                        "h": int(y) + int(row_h),
                        "area": int(state["area"]) + int(row_area),
                        "counts": counts,
                        "placements": placements,
                        "rows": int(state["rows"]) + 1,
                    }
                    sig = state_sig(child)
                    if sig in seen:
                        continue
                    seen.add(sig)
                    results.append(child)
                    next_states.append(child)
            if not next_states:
                break
            next_states.sort(key=state_rank)
            states = next_states[:state_keep]

        results.sort(key=state_rank)
        kept = []
        seen_kept = set()

        def keep(source, max_items):
            for state in source:
                sig = state_sig(state)
                if sig in seen_kept:
                    continue
                seen_kept.add(sig)
                kept.append(state)
                if len(kept) >= max_items:
                    return

        keep(results, max(1, int(limit) // 2))
        width_buckets = {}
        family_buckets = {}
        for state in results:
            width_buckets.setdefault(int(state["w"]), []).append(state)
            if state["counts"]:
                family = max(state["counts"], key=lambda key: int(key[0]) * int(key[1]) * int(state["counts"][key]))
                family_buckets.setdefault(family, []).append(state)
        for width in sorted(width_buckets):
            keep(sorted(width_buckets[width], key=state_rank)[:3], int(limit))
        for family in sorted(family_buckets):
            keep(sorted(family_buckets[family], key=state_rank)[:4], int(limit))
        return kept[:limit]

    def simple_cap_mosaics(width_limit, height_limit, used_counts, *, limit=18):
        """Cheap cap fills that must be emitted before the richer cap beam runs."""
        caps = []
        seen = set()
        for _rank, entries, row_w, row_h, row_area, entry_counts in row_options(
            width_limit,
            height_limit,
            used_counts,
            limit=limit,
        ):
            placements = []
            x = 0
            for group, count in entries:
                for _idx in range(int(count)):
                    placements.append({
                        "group": group,
                        "x": int(x),
                        "y": 0,
                    })
                    x += int(group["w"]) + int(kerf)
            sig = (
                int(row_w),
                int(row_h),
                tuple(sorted(entry_counts.items())),
                tuple(
                    (
                        pl["group"]["key"],
                        int(pl["group"]["w"]),
                        int(pl["group"]["h"]),
                        int(pl["x"]),
                    )
                    for pl in placements
                ),
            )
            if sig in seen:
                continue
            seen.add(sig)
            caps.append({
                "w": int(row_w),
                "h": int(row_h),
                "area": int(row_area),
                "counts": Counter(entry_counts),
                "placements": placements,
                "rows": 1,
            })
        return caps[:limit]

    lane_options = []
    seen_lanes = set()

    def add_lane(base_group, base_rows, cap):
        placements = []
        counts = Counter()
        y = 0
        for _idx in range(int(base_rows)):
            placements.append({
                "group": base_group,
                "x": 0,
                "y": int(y),
            })
            counts[base_group["key"]] += 1
            y += int(base_group["h"]) + int(kerf)
        lane_h = y - int(kerf) if base_rows else 0
        lane_w = int(base_group["w"]) if base_rows else 0
        area = int(base_rows) * int(base_group["w"]) * int(base_group["h"])
        if cap:
            cap_y = lane_h + (int(kerf) if lane_h else 0)
            if cap_y + int(cap["h"]) > int(sheet_h):
                return
            for pl in cap["placements"]:
                rel = dict(pl)
                rel["x"] = int(rel.get("x") or 0)
                rel["y"] = int(cap_y) + int(rel.get("y") or 0)
                placements.append(rel)
            counts.update(cap["counts"])
            lane_w = max(lane_w, int(cap["w"]))
            lane_h = max(lane_h, int(cap_y) + int(cap["h"]))
            area += int(cap["area"])
        if not placements or lane_w <= 0 or lane_h <= 0:
            return
        if lane_w > int(sheet_w) or lane_h > int(sheet_h):
            return
        if any(count > int(available_by_key[key]) for key, count in counts.items()):
            return
        sig = (
            int(lane_w),
            int(lane_h),
            tuple(sorted(counts.items())),
            tuple(
                (
                    pl["group"]["key"],
                    int(pl["group"]["w"]),
                    int(pl["group"]["h"]),
                    int(pl["x"]),
                    int(pl["y"]),
                )
                for pl in sorted(placements, key=lambda item: (int(item["y"]), int(item["x"]), item["group"]["key"]))
            ),
        )
        if sig in seen_lanes:
            return
        seen_lanes.add(sig)
        lane_options.append({
            "w": int(lane_w),
            "h": int(lane_h),
            "area": int(area),
            "counts": counts,
            "placements": placements,
            "rows": int(base_rows) + int(cap["rows"] if cap else 0),
            "primary": base_group["key"],
        })

    primary_groups = [
        group for group in oriented_groups
        if int(group["qty"]) >= 2 or int(group["area"]) >= int(sheet_area) * 0.035
    ]
    primary_groups.sort(key=lambda group: (
        -int(group["area"]) * min(int(group["qty"]), 8),
        int(group["w"]),
        -int(group["h"]),
        group["key"],
    ))
    for group_idx, group in enumerate(primary_groups[:lane_primary_limit]):
        if _deadline_hit(deadline, 0.01):
            break
        max_rows = min(
            int(group["qty"]),
            int((int(sheet_h) + int(kerf)) // (int(group["h"]) + int(kerf))),
        )
        if max_rows <= 0:
            continue
        row_counts = set(count_choices(max_rows))
        for near_full in range(max(1, max_rows - 4), max_rows + 1):
            row_counts.add(near_full)
        for rows in sorted(row_counts, reverse=True):
            used_counts = Counter({group["key"]: int(rows)})
            base_h = int(rows) * int(group["h"]) + int(kerf) * max(0, int(rows) - 1)
            if base_h > int(sheet_h):
                continue
            add_lane(group, rows, None)
            cap_h = int(sheet_h) - int(base_h) - int(kerf)
            if cap_h <= 0:
                continue
            for cap in simple_cap_mosaics(
                int(group["w"]),
                cap_h,
                used_counts,
                limit=(80 if allow_expensive else 24),
            ):
                add_lane(group, rows, cap)
            if (
                not allow_expensive
                or group_idx >= 10
                or rows not in sorted(row_counts, reverse=True)[:4]
                or _deadline_hit(deadline, 8.0)
            ):
                continue
            for cap in cap_mosaics(
                int(group["w"]),
                cap_h,
                used_counts,
                limit=30,
            ):
                add_lane(group, rows, cap)

    if not lane_options:
        _record_strategy_metric(stats, "v2_lane_recipe_candidates", _elapsed_ms(started_at), 0)
        return []

    def lane_sig(lane):
        return (
            int(lane["w"]),
            tuple(sorted(lane["counts"].items())),
            tuple(
                (
                    pl["group"]["key"],
                    int(pl["group"]["w"]),
                    int(pl["group"]["h"]),
                    int(pl["x"]),
                    int(pl["y"]),
                )
                for pl in sorted(lane["placements"], key=lambda item: (int(item["y"]), int(item["x"]), item["group"]["key"]))
            ),
        )

    def lane_rank(lane):
        exact_rows = sum(
            1 for pl in lane["placements"]
            if int(pl["group"]["w"]) == int(lane["w"])
        )
        return (
            -int(lane["area"]),
            int(sheet_h) - int(lane["h"]),
            int(lane["w"]),
            -exact_rows,
            tuple(sorted(lane["counts"].items())),
        )

    lane_options.sort(key=lane_rank)
    kept_lanes = []
    seen_kept_lanes = set()

    def keep_lanes(source, max_items):
        for lane in source:
            sig = lane_sig(lane)
            if sig in seen_kept_lanes:
                continue
            seen_kept_lanes.add(sig)
            kept_lanes.append(lane)
            if len(kept_lanes) >= max_items:
                return

    lane_keep = 700 if allow_expensive else 260
    keep_lanes(lane_options, max(1, lane_keep // 2))
    width_buckets = {}
    family_buckets = {}
    family_width_buckets = {}
    for lane in lane_options:
        width_buckets.setdefault(int(lane["w"]), []).append(lane)
        family_buckets.setdefault(lane["primary"], []).append(lane)
        family_width_buckets.setdefault((lane["primary"], int(lane["w"])), []).append(lane)
    for width in sorted(width_buckets):
        keep_lanes(sorted(width_buckets[width], key=lane_rank)[:12], lane_keep)
    for family in sorted(family_buckets):
        keep_lanes(sorted(family_buckets[family], key=lane_rank)[:14], lane_keep)
    for family_width in sorted(family_width_buckets):
        keep_lanes(sorted(family_width_buckets[family_width], key=lane_rank)[:5], lane_keep)
    lane_options = kept_lanes[:lane_keep]

    pools = {key: sorted(items, key=lambda piece: int(piece["id"])) for key, items in groups.items()}
    candidates = []
    seen_sheets = set()

    def build_sheet_from_lanes(lanes):
        used_counts = Counter()
        placements = []
        free = []
        x = 0
        for lane in lanes:
            if x and x + int(lane["w"]) > int(sheet_w):
                return None
            for key, count in lane["counts"].items():
                if used_counts[key] + int(count) > int(available_by_key[key]):
                    return None
            local_counts = Counter()
            for pl in sorted(lane["placements"], key=lambda item: (int(item["y"]), int(item["x"]), item["group"]["key"])):
                group = pl["group"]
                key = group["key"]
                item_index = used_counts[key] + local_counts[key]
                if item_index >= len(pools[key]):
                    return None
                piece = pools[key][item_index]
                local_counts[key] += 1
                placements.append({
                    "item": int(piece["id"]),
                    "x": int(x) + int(pl.get("x") or 0),
                    "y": int(pl.get("y") or 0),
                    "w": int(group["w"]),
                    "h": int(group["h"]),
                    "rotated": bool(group["rotated"]),
                })
            used_counts.update(lane["counts"])
            top_y = int(lane["h"]) + int(kerf)
            if top_y < int(sheet_h):
                free.append((int(x), int(top_y), int(lane["w"]), int(sheet_h) - int(top_y)))
            x += int(lane["w"]) + int(kerf)
        used_w = x - int(kerf) if lanes else 0
        right_x = used_w + int(kerf)
        if right_x < int(sheet_w):
            free.append((int(right_x), 0, int(sheet_w) - int(right_x), int(sheet_h)))
        sheet = _sheet_from_pattern(
            sheet_w,
            sheet_h,
            kerf,
            placements,
            free,
            "lane_recipe_mosaic",
        )
        if not _sheet_geometry_ok(sheet):
            return None
        return sheet

    def state_rank(state):
        return (
            -int(state["area"]),
            int(sheet_w) - int(state["w"]),
            -len(state["lanes"]),
            tuple(sorted(state["counts"].items())),
        )

    def state_sig(state):
        return (
            int(state["w"]),
            tuple(sorted(state["counts"].items())),
            tuple(lane_sig(lane) for lane in state["lanes"]),
        )

    states = [{
        "lanes": [],
        "counts": Counter(),
        "w": 0,
        "area": 0,
    }]
    sheet_states = []
    seen_sheet_states = set()
    max_sheet_states = 2400 if allow_expensive else 700

    def append_sheet_state(lanes):
        if len(sheet_states) >= max_sheet_states:
            return
        if not lanes:
            return
        counts = Counter()
        width = 0
        area = 0
        for lane in lanes:
            width = int(lane["w"]) if not width else int(width) + int(kerf) + int(lane["w"])
            if width > int(sheet_w):
                return
            counts.update(lane["counts"])
            area += int(lane["area"])
        if any(count > int(available_by_key[key]) for key, count in counts.items()):
            return
        if len(lanes) < 2 and int(area) < int(sheet_area) * 0.70:
            return
        state = {
            "lanes": list(lanes),
            "counts": counts,
            "w": int(width),
            "area": int(area),
        }
        sig = state_sig(state)
        if sig in seen_sheet_states:
            return
        seen_sheet_states.add(sig)
        sheet_states.append(state)

    # Balanced CutList-style sheets are often just two strong lanes from different
    # families. Emit those directly before the wider beam has a chance to over-prune
    # lower-area-but-future-friendly lane combinations.
    direct_pool = lane_options[:(220 if allow_expensive else 90)]
    direct_by_family = {}
    for lane in lane_options:
        direct_by_family.setdefault(lane["primary"], []).append(lane)
    for family in sorted(direct_by_family):
        for lane in sorted(direct_by_family[family], key=lane_rank)[:(18 if allow_expensive else 8)]:
            if lane not in direct_pool:
                direct_pool.append(lane)
    direct_pool = direct_pool[:(320 if allow_expensive else 130)]
    narrow_direct = sorted(
        [lane for lane in direct_pool if int(lane["w"]) <= int(sheet_w) * 0.30],
        key=lambda lane: (int(lane["w"]), -int(lane["area"]), tuple(sorted(lane["counts"].items()))),
    )
    direct_family_keys = sorted(direct_by_family)
    family_pair_limit = 8 if allow_expensive else 5

    def direct_family_bucket(family):
        bucket = sorted(direct_by_family[family], key=lane_rank)
        kept = []
        seen = set()

        def keep(source, max_items):
            for lane in source:
                sig = lane_sig(lane)
                if sig in seen:
                    continue
                seen.add(sig)
                kept.append(lane)
                if len(kept) >= max_items:
                    return

        keep(bucket, max(1, family_pair_limit // 2))
        by_width = {}
        for lane in bucket:
            by_width.setdefault(int(lane["w"]), []).append(lane)
        for width in sorted(by_width):
            keep(sorted(by_width[width], key=lane_rank)[:3], family_pair_limit * 2)
        return kept[:family_pair_limit * 2]

    for family_idx, family_a in enumerate(direct_family_keys):
        if _deadline_hit(deadline, 3.0) or len(sheet_states) >= max_sheet_states:
            break
        bucket_a = direct_family_bucket(family_a)
        for family_b in direct_family_keys[family_idx:]:
            if _deadline_hit(deadline, 3.0) or len(sheet_states) >= max_sheet_states:
                break
            bucket_b = direct_family_bucket(family_b)
            for first in bucket_a:
                if _deadline_hit(deadline, 3.0) or len(sheet_states) >= max_sheet_states:
                    break
                for second in bucket_b:
                    if _deadline_hit(deadline, 3.0) or len(sheet_states) >= max_sheet_states:
                        break
                    pair_w = int(first["w"]) + int(kerf) + int(second["w"])
                    if pair_w > int(sheet_w):
                        continue
                    append_sheet_state([first, second])
                    remaining_w = int(sheet_w) - pair_w - int(kerf)
                    if remaining_w <= 0:
                        continue
                    for third in narrow_direct[:24]:
                        if int(third["w"]) > remaining_w:
                            continue
                        append_sheet_state([first, second, third])
                        if len(sheet_states) >= max_sheet_states:
                            break
                    if len(sheet_states) >= max_sheet_states:
                        break
                if len(sheet_states) >= max_sheet_states:
                    continue

    min_lane_w = min(int(lane["w"]) for lane in lane_options)
    max_lanes = min(10, max(1, int((int(sheet_w) + int(kerf)) // (min_lane_w + int(kerf)))))
    beam_keep = 120 if allow_expensive else 48
    for _depth in range(max_lanes):
        if _deadline_hit(deadline, 3.0) or len(sheet_states) >= max_sheet_states:
            break
        next_states = []
        seen_next = set()
        for state in states:
            if _deadline_hit(deadline, 3.0) or len(sheet_states) >= max_sheet_states:
                break
            for lane in lane_options:
                if _deadline_hit(deadline, 3.0) or len(sheet_states) >= max_sheet_states:
                    break
                new_w = int(lane["w"]) if not state["lanes"] else int(state["w"]) + int(kerf) + int(lane["w"])
                if new_w > int(sheet_w):
                    continue
                counts = Counter(state["counts"])
                counts.update(lane["counts"])
                if any(count > int(available_by_key[key]) for key, count in counts.items()):
                    continue
                child = {
                    "lanes": list(state["lanes"]) + [lane],
                    "counts": counts,
                    "w": int(new_w),
                    "area": int(state["area"]) + int(lane["area"]),
                }
                sig = state_sig(child)
                if sig in seen_next:
                    continue
                seen_next.add(sig)
                next_states.append(child)
                append_sheet_state(child["lanes"])
        if not next_states:
            break
        next_states.sort(key=state_rank)
        kept_states = []
        seen_state = set()

        def keep_states(source, max_items):
            for state in source:
                sig = state_sig(state)
                if sig in seen_state:
                    continue
                seen_state.add(sig)
                kept_states.append(state)
                if len(kept_states) >= max_items:
                    return

        keep_states(next_states, max(1, beam_keep // 2))
        state_width_buckets = {}
        state_family_buckets = {}
        for state in next_states:
            state_width_buckets.setdefault(int(state["w"]), []).append(state)
            if state["counts"]:
                family = max(state["counts"], key=lambda key: int(key[0]) * int(key[1]) * int(state["counts"][key]))
                state_family_buckets.setdefault(family, []).append(state)
        for width in sorted(state_width_buckets):
            keep_states(sorted(state_width_buckets[width], key=state_rank)[:3], beam_keep)
        for family in sorted(state_family_buckets):
            keep_states(sorted(state_family_buckets[family], key=state_rank)[:4], beam_keep)
        states = kept_states[:beam_keep]

    sheet_states.sort(key=lambda state: (
        -int(state["area"]),
        int(sheet_w) - int(state["w"]),
        -len(state["lanes"]),
        tuple(sorted(state["counts"].items())),
    ))
    for state in sheet_states[:(1800 if allow_expensive else 520)]:
        if _deadline_hit(deadline, 0.01):
            break
        sheet = build_sheet_from_lanes(state["lanes"])
        if not sheet:
            continue
        signature = _sheet_mosaic_signature(sheet)
        if signature in seen_sheets:
            continue
        seen_sheets.add(signature)
        cand = _candidate_from_sheets([sheet], "v2_lane_recipe_mosaic")
        _append_candidate_variants(candidates, cand, allow_mirror=False)

    _record_strategy_metric(stats, "v2_lane_recipe_candidates", _elapsed_ms(started_at), len(candidates))
    return candidates


def _v2_transposed_lane_recipe_candidates(remaining, sheet_w, sheet_h, kerf, *, stats=None, deadline=None, allow_expensive=False):
    """Search lane recipes in the quarter-turned frame."""
    started_at = time.monotonic()
    if int(sheet_w) == int(sheet_h):
        return []
    pieces_by_id = {int(piece["id"]): piece for piece in remaining}
    candidates = []
    seen = set()

    for cand in _v2_lane_recipe_candidates(
        remaining,
        int(sheet_h),
        int(sheet_w),
        kerf,
        stats=None,
        deadline=deadline,
        allow_expensive=allow_expensive,
    ):
        sheets = cand.get("sheets") or []
        if len(sheets) != 1:
            continue
        rotated = _rotate_sheet_clockwise(
            sheets[0],
            int(sheet_w),
            int(sheet_h),
            pieces_by_id=pieces_by_id,
            suffix="_long_axis",
        )
        if not _sheet_geometry_ok(rotated):
            continue
        signature = _sheet_mosaic_signature(rotated)
        if signature in seen:
            continue
        seen.add(signature)
        new_cand = _candidate_from_sheets([rotated], "v2_lane_recipe_mosaic_long_axis")
        _append_candidate_variants(candidates, new_cand, allow_mirror=False)

    _record_strategy_metric(stats, "v2_lane_recipe_mosaic_long_axis_candidates", _elapsed_ms(started_at), len(candidates))
    return candidates


def _v2_simple_lane_pair_candidates(remaining, sheet_w, sheet_h, kerf, *, stats=None, deadline=None, allow_expensive=False):
    """Fast lane-pair generator for repeated-stack plus cap-row mosaics."""
    started_at = time.monotonic()
    if len(remaining or []) < 4:
        return []
    groups = {}
    for piece in remaining:
        groups.setdefault(_piece_type_key(piece), []).append(piece)
    available = Counter({key: len(items) for key, items in groups.items()})
    oriented = []
    seen_oriented = set()
    for key, items in groups.items():
        for opt in _piece_orientations(items[0], sheet_w, sheet_h):
            sig = (key, int(opt["w"]), int(opt["h"]))
            if sig in seen_oriented:
                continue
            seen_oriented.add(sig)
            oriented.append({
                "key": key,
                "w": int(opt["w"]),
                "h": int(opt["h"]),
                "rotated": bool(opt["rotated"]),
                "qty": len(items),
                "area": int(key[0]) * int(key[1]),
            })
    oriented.sort(key=lambda group: (-min(int(group["qty"]), 12) * int(group["area"]), int(group["w"]), group["key"]))
    if not oriented:
        return []

    def count_choices(max_count):
        max_count = int(max_count)
        if max_count <= 0:
            return []
        choices = {max_count, 1}
        for value in (2, 3, 4, 5, 6, max_count - 1, max_count - 2):
            if 1 <= value <= max_count:
                choices.add(value)
        return sorted(choices, reverse=True)

    def cap_row_options(width_limit, height_limit, used_counts, limit=80):
        options = []
        for group in oriented:
            remaining_count = int(available[group["key"]]) - int(used_counts.get(group["key"], 0))
            if remaining_count <= 0 or int(group["w"]) > int(width_limit) or int(group["h"]) > int(height_limit):
                continue
            max_across = min(
                remaining_count,
                int((int(width_limit) + int(kerf)) // (int(group["w"]) + int(kerf))),
            )
            for count in count_choices(max_across):
                row_w = int(count) * int(group["w"]) + int(kerf) * max(0, int(count) - 1)
                row_h = int(group["h"])
                if row_w > int(width_limit) or row_h > int(height_limit):
                    continue
                row_area = int(count) * int(group["w"]) * int(group["h"])
                width_waste = int(width_limit) - int(row_w)
                rank = (
                    width_waste // 25,
                    -int(row_area),
                    int(row_h),
                    -int(count),
                    group["key"],
                    int(group["w"]),
                    int(group["h"]),
                )
                placements = []
                x = 0
                for _idx in range(int(count)):
                    placements.append({"group": group, "x": int(x), "y": 0})
                    x += int(group["w"]) + int(kerf)
                options.append((rank, {
                    "w": int(row_w),
                    "h": int(row_h),
                    "area": int(row_area),
                    "counts": Counter({group["key"]: int(count)}),
                    "placements": placements,
                    "family": group["key"],
                }))
        options.sort(key=lambda item: item[0])
        kept = []
        seen = set()

        def cap_sig(cap):
            return (
                int(cap["w"]),
                int(cap["h"]),
                tuple(sorted(cap["counts"].items())),
                cap["family"],
            )

        def keep(source, max_items):
            for _rank, cap in source:
                sig = cap_sig(cap)
                if sig in seen:
                    continue
                seen.add(sig)
                kept.append(cap)
                if len(kept) >= max_items:
                    return

        keep(options, max(1, int(limit) // 2))
        by_family = {}
        for item in options:
            by_family.setdefault(item[1]["family"], []).append(item)
        for family in sorted(by_family):
            keep(sorted(by_family[family], key=lambda item: item[0])[:4], int(limit))
        return kept[:limit]

    lanes = []
    seen_lanes = set()

    def add_lane(group, rows, cap=None):
        placements = []
        counts = Counter({group["key"]: int(rows)})
        y = 0
        for _idx in range(int(rows)):
            placements.append({"group": group, "x": 0, "y": int(y)})
            y += int(group["h"]) + int(kerf)
        lane_h = int(y) - int(kerf)
        lane_w = int(group["w"])
        area = int(rows) * int(group["w"]) * int(group["h"])
        if cap:
            cap_y = int(lane_h) + int(kerf)
            if cap_y + int(cap["h"]) > int(sheet_h):
                return
            for pl in cap["placements"]:
                rel = dict(pl)
                rel["y"] = int(cap_y) + int(rel.get("y") or 0)
                placements.append(rel)
            counts.update(cap["counts"])
            lane_w = max(lane_w, int(cap["w"]))
            lane_h = max(lane_h, int(cap_y) + int(cap["h"]))
            area += int(cap["area"])
        if lane_w > int(sheet_w) or lane_h > int(sheet_h):
            return
        if any(count > int(available[key]) for key, count in counts.items()):
            return
        sig = (
            int(lane_w),
            int(lane_h),
            tuple(sorted(counts.items())),
            tuple((pl["group"]["key"], int(pl["group"]["w"]), int(pl["group"]["h"]), int(pl["x"]), int(pl["y"])) for pl in placements),
        )
        if sig in seen_lanes:
            return
        seen_lanes.add(sig)
        lanes.append({
            "w": int(lane_w),
            "h": int(lane_h),
            "area": int(area),
            "counts": counts,
            "placements": placements,
            "primary": group["key"],
        })

    primary_limit = 42 if allow_expensive else 24
    for group in oriented[:primary_limit]:
        if _deadline_hit(deadline, 1.0):
            break
        max_rows = min(
            int(group["qty"]),
            int((int(sheet_h) + int(kerf)) // (int(group["h"]) + int(kerf))),
        )
        if max_rows <= 0:
            continue
        row_counts = set(count_choices(max_rows))
        for near_full in range(max(1, max_rows - 4), max_rows + 1):
            row_counts.add(near_full)
        for rows in sorted(row_counts, reverse=True):
            base_h = int(rows) * int(group["h"]) + int(kerf) * max(0, int(rows) - 1)
            if base_h > int(sheet_h):
                continue
            add_lane(group, rows, None)
            cap_h = int(sheet_h) - int(base_h) - int(kerf)
            if cap_h <= 0:
                continue
            used = Counter({group["key"]: int(rows)})
            for cap in cap_row_options(int(group["w"]), cap_h, used, limit=(100 if allow_expensive else 36)):
                add_lane(group, rows, cap)

    if not lanes:
        _record_strategy_metric(stats, "v2_simple_lane_pair_candidates", _elapsed_ms(started_at), 0)
        return []

    def lane_sig(lane):
        return (
            int(lane["w"]),
            int(lane["h"]),
            tuple(sorted(lane["counts"].items())),
            lane["primary"],
        )

    def lane_rank(lane):
        return (
            -int(lane["area"]),
            int(sheet_h) - int(lane["h"]),
            int(lane["w"]),
            tuple(sorted(lane["counts"].items())),
        )

    lanes.sort(key=lane_rank)
    kept = []
    seen = set()

    def keep_lanes(source, max_items):
        for lane in source:
            sig = lane_sig(lane)
            if sig in seen:
                continue
            seen.add(sig)
            kept.append(lane)
            if len(kept) >= max_items:
                return

    lane_keep = 900 if allow_expensive else 320
    keep_lanes(lanes, max(1, lane_keep // 3))
    by_width = {}
    by_family = {}
    by_family_width = {}
    for lane in lanes:
        by_width.setdefault(int(lane["w"]), []).append(lane)
        by_family.setdefault(lane["primary"], []).append(lane)
        by_family_width.setdefault((lane["primary"], int(lane["w"])), []).append(lane)
    for width in sorted(by_width):
        keep_lanes(sorted(by_width[width], key=lane_rank)[:14], lane_keep)
    for family in sorted(by_family):
        keep_lanes(sorted(by_family[family], key=lane_rank)[:18], lane_keep)
    for key in sorted(by_family_width):
        keep_lanes(sorted(by_family_width[key], key=lane_rank)[:6], lane_keep)
    lanes = kept[:lane_keep]

    pools = {key: sorted(items, key=lambda piece: int(piece["id"])) for key, items in groups.items()}
    candidates = []
    seen_sheets = set()

    def build_sheet(lane_list):
        used_counts = Counter()
        placements = []
        free = []
        x = 0
        for lane in lane_list:
            lane_w = int(lane["w"])
            if x and x + lane_w > int(sheet_w):
                return None
            for key, count in lane["counts"].items():
                if used_counts[key] + int(count) > int(available[key]):
                    return None
            local_counts = Counter()
            for pl in sorted(lane["placements"], key=lambda item: (int(item["y"]), int(item["x"]), item["group"]["key"])):
                group = pl["group"]
                key = group["key"]
                item_index = used_counts[key] + local_counts[key]
                if item_index >= len(pools[key]):
                    return None
                piece = pools[key][item_index]
                local_counts[key] += 1
                placements.append({
                    "item": int(piece["id"]),
                    "x": int(x) + int(pl["x"]),
                    "y": int(pl["y"]),
                    "w": int(group["w"]),
                    "h": int(group["h"]),
                    "rotated": bool(group["rotated"]),
                })
            used_counts.update(lane["counts"])
            top_y = int(lane["h"]) + int(kerf)
            if top_y < int(sheet_h):
                free.append((int(x), int(top_y), lane_w, int(sheet_h) - int(top_y)))
            x += lane_w + int(kerf)
        used_w = x - int(kerf) if lane_list else 0
        right_x = used_w + int(kerf)
        if right_x < int(sheet_w):
            free.append((int(right_x), 0, int(sheet_w) - int(right_x), int(sheet_h)))
        sheet = _sheet_from_pattern(sheet_w, sheet_h, kerf, placements, free, "simple_lane_pair_mosaic")
        if not _sheet_geometry_ok(sheet):
            return None
        return sheet

    family_keys = sorted(by_family)
    family_bucket_cache = {}

    def family_bucket(family):
        if family in family_bucket_cache:
            return family_bucket_cache[family]
        bucket = sorted([lane for lane in lanes if lane["primary"] == family], key=lane_rank)
        out = []
        seen_bucket = set()

        def keep(source, max_items):
            for lane in source:
                sig = lane_sig(lane)
                if sig in seen_bucket:
                    continue
                seen_bucket.add(sig)
                out.append(lane)
                if len(out) >= max_items:
                    return

        keep(bucket, 6 if allow_expensive else 4)
        width_buckets = {}
        for lane in bucket:
            width_buckets.setdefault(int(lane["w"]), []).append(lane)
        for width in sorted(width_buckets):
            keep(sorted(width_buckets[width], key=lane_rank)[:4], 18 if allow_expensive else 10)
        family_bucket_cache[family] = out[:(18 if allow_expensive else 10)]
        return family_bucket_cache[family]

    narrow_lanes = sorted(
        [lane for lane in lanes if int(lane["w"]) <= int(sheet_w) * 0.30],
        key=lambda lane: (int(lane["w"]), -int(lane["area"]), tuple(sorted(lane["counts"].items()))),
    )[:32]

    def add_candidate(lane_list):
        if len(candidates) >= (5000 if allow_expensive else 1200):
            return
        counts = Counter()
        width = 0
        for lane in lane_list:
            width = int(lane["w"]) if not width else int(width) + int(kerf) + int(lane["w"])
            counts.update(lane["counts"])
        if width > int(sheet_w):
            return
        if any(count > int(available[key]) for key, count in counts.items()):
            return
        sheet = build_sheet(lane_list)
        if not sheet:
            return
        signature = _sheet_mosaic_signature(sheet)
        if signature in seen_sheets:
            return
        seen_sheets.add(signature)
        cand = _candidate_from_sheets([sheet], "v2_simple_lane_pair_mosaic")
        _append_candidate_variants(candidates, cand, allow_mirror=False)

    for idx, family_a in enumerate(family_keys):
        if _deadline_hit(deadline, 0.4) or len(candidates) >= (5000 if allow_expensive else 1200):
            break
        for family_b in family_keys[idx:]:
            if _deadline_hit(deadline, 0.4) or len(candidates) >= (5000 if allow_expensive else 1200):
                break
            pair_added = 0
            pair_cap = 28 if allow_expensive else 12
            for first in family_bucket(family_a):
                if _deadline_hit(deadline, 0.4):
                    break
                for second in family_bucket(family_b):
                    if _deadline_hit(deadline, 0.4) or pair_added >= pair_cap:
                        break
                    pair_w = int(first["w"]) + int(kerf) + int(second["w"])
                    if pair_w > int(sheet_w):
                        continue
                    before = len(candidates)
                    add_candidate([first, second])
                    if len(candidates) > before:
                        pair_added += 1
                    remaining_w = int(sheet_w) - pair_w - int(kerf)
                    if remaining_w <= 0:
                        continue
                    for third in narrow_lanes:
                        if int(third["w"]) <= remaining_w:
                            before = len(candidates)
                            add_candidate([first, second, third])
                            if len(candidates) > before:
                                pair_added += 1
                            break
                    if pair_added >= pair_cap:
                        break
                if pair_added >= pair_cap:
                    break

    _record_strategy_metric(stats, "v2_simple_lane_pair_candidates", _elapsed_ms(started_at), len(candidates))
    return candidates


def _v2_transposed_simple_lane_pair_candidates(remaining, sheet_w, sheet_h, kerf, *, stats=None, deadline=None, allow_expensive=False):
    started_at = time.monotonic()
    if int(sheet_w) == int(sheet_h):
        return []
    pieces_by_id = {int(piece["id"]): piece for piece in remaining}
    candidates = []
    seen = set()
    for cand in _v2_simple_lane_pair_candidates(
        remaining,
        int(sheet_h),
        int(sheet_w),
        kerf,
        stats=None,
        deadline=deadline,
        allow_expensive=allow_expensive,
    ):
        sheets = cand.get("sheets") or []
        if len(sheets) != 1:
            continue
        rotated = _rotate_sheet_clockwise(
            sheets[0],
            int(sheet_w),
            int(sheet_h),
            pieces_by_id=pieces_by_id,
            suffix="_long_axis",
        )
        if not _sheet_geometry_ok(rotated):
            continue
        signature = _sheet_mosaic_signature(rotated)
        if signature in seen:
            continue
        seen.add(signature)
        new_cand = _candidate_from_sheets([rotated], "v2_simple_lane_pair_mosaic_long_axis")
        _append_candidate_variants(candidates, new_cand, allow_mirror=False)
    _record_strategy_metric(stats, "v2_simple_lane_pair_mosaic_long_axis_candidates", _elapsed_ms(started_at), len(candidates))
    return candidates


def _v2_repeat_sheet_candidates(remaining, sheet_w, sheet_h, kerf, *, stats=None, deadline=None, allow_top_aligned=False):
    started_at = time.monotonic()
    groups = {}
    for piece in remaining:
        groups.setdefault(_piece_type_key(piece), []).append(piece)
    candidates = []
    for fillers in sorted(groups.values(), key=len, reverse=True)[:3]:
        if _deadline_hit(deadline, 0.02):
            break
        if len(fillers) < 3:
            continue
        row_opt = _row_filler_orientation(fillers[0], sheet_w, sheet_h, kerf)
        if not row_opt:
            continue
        row_cap = int((sheet_w + kerf) // (int(row_opt["w"]) + kerf))
        if row_cap <= 0:
            continue
        counts = {
            min(len(fillers), row_cap),
            min(len(fillers), row_cap * 2),
            min(len(fillers), row_cap * 3),
        }
        remainder = len(fillers) % max(1, row_cap)
        if remainder:
            counts.add(remainder)
        counts.add(min(len(fillers), max(1, row_cap - 1)))
        counts.add(min(len(fillers), max(1, row_cap + 1)))
        if len(fillers) > row_cap:
            # Preserve a clean final tail by testing counts that intentionally
            # leave one full row/strip of repeats for a later sheet.
            counts.add(min(len(fillers), max(1, len(fillers) - row_cap)))
        if len(fillers) > row_cap * 2:
            counts.add(min(len(fillers), max(1, len(fillers) - row_cap * 2)))
        for count in sorted({c for c in counts if 0 < c <= len(fillers)}, reverse=True):
            if _deadline_hit(deadline, 0.02):
                break
            sheet, _used, _used_h = _make_repeated_band_sheet(fillers, count, sheet_w, sheet_h, kerf)
            cand = _candidate_from_sheets([sheet], "v2_repeat_band")
            _append_candidate_variants(candidates, cand, allow_mirror=False)
            if sheet is not None and allow_top_aligned:
                top_y = max(0, int(sheet_h - _used_h))
                if top_y > 0:
                    top_sheet, _top_used, _top_h = _make_repeated_band_sheet(
                        fillers, count, sheet_w, sheet_h, kerf, y0=top_y,
                    )
                    top_cand = _candidate_from_sheets([top_sheet], "v2_repeat_band_top_aligned")
                    _append_candidate_variants(candidates, top_cand, allow_mirror=False)
    _record_strategy_metric(stats, "v2_repeat_band_candidates", _elapsed_ms(started_at), len(candidates))
    return candidates


def _v2_guillotine_mosaic_candidates(remaining, sheet_w, sheet_h, kerf, *, stats=None, deadline=None, allow_expensive=False):
    started_at = time.monotonic()
    candidates = []
    variants = _GUILLOTINE_MOSAIC_VARIANTS if allow_expensive else _GUILLOTINE_MOSAIC_VARIANTS[:5]
    for fitness_mode, split_mode, sort_mode in variants:
        if _deadline_hit(deadline, 0.02):
            break
        variant_started = time.monotonic()
        before = len(candidates)
        sheet = _guillotine_single_sheet_variant(
            remaining,
            sheet_w,
            sheet_h,
            kerf,
            fitness_mode=fitness_mode,
            split_mode=split_mode,
            sort_mode=sort_mode,
            deadline=deadline,
        )
        cand = _candidate_from_sheets([sheet], "v2_guillotine_mosaic")
        _append_candidate_variants(candidates, cand, allow_mirror=False)
        if sheet is not None:
            for caps, cap_label in _guillotine_cap_options(sheet, remaining, sheet_w, sheet_h):
                if _deadline_hit(deadline, 0.02):
                    break
                capped = _guillotine_single_sheet_variant(
                    remaining,
                    sheet_w,
                    sheet_h,
                    kerf,
                    fitness_mode=fitness_mode,
                    split_mode=split_mode,
                    sort_mode=sort_mode,
                    caps_by_key=caps,
                    deadline=deadline,
                )
                if capped is not None:
                    capped.strategy = "%s_%s" % (getattr(capped, "strategy", "guillotine_one"), cap_label)
                capped_cand = _candidate_from_sheets([capped], "v2_guillotine_mosaic_capped")
                _append_candidate_variants(candidates, capped_cand, allow_mirror=False)
        if sort_mode == "cutlist_column":
            for boost_keys, boost_label in _guillotine_boost_options(remaining, sheet_w, sheet_h, max_options=8 if allow_expensive else 4):
                if _deadline_hit(deadline, 0.02):
                    break
                boosted = _guillotine_single_sheet_variant(
                    remaining,
                    sheet_w,
                    sheet_h,
                    kerf,
                    fitness_mode=fitness_mode,
                    split_mode=split_mode,
                    sort_mode=sort_mode,
                    boost_keys=boost_keys,
                    deadline=deadline,
                )
                if boosted is not None:
                    boosted.strategy = "%s_%s" % (getattr(boosted, "strategy", "guillotine_one"), boost_label)
                boosted_cand = _candidate_from_sheets([boosted], "v2_guillotine_mosaic_boosted")
                _append_candidate_variants(candidates, boosted_cand, allow_mirror=False)
        _record_strategy_metric(
            stats,
            "v2_guillotine_mosaic_%s_%s_%s" % (fitness_mode, split_mode, sort_mode),
            _elapsed_ms(variant_started),
            len(candidates) - before,
        )
    _record_strategy_metric(stats, "v2_guillotine_mosaic_candidates", _elapsed_ms(started_at), len(candidates))
    return candidates


def _pack_subset_best_sheet(subset, sheet_w, sheet_h, kerf, *, deadline=None, variants=None):
    if not subset:
        return None
    wanted = {int(p["id"]) for p in subset}
    best = None
    for fitness_mode, split_mode, sort_mode in (variants or _GUILLOTINE_MOSAIC_VARIANTS):
        if _deadline_hit(deadline, 0.005):
            break
        sheet = _guillotine_single_sheet_variant(
            subset,
            sheet_w,
            sheet_h,
            kerf,
            fitness_mode=fitness_mode,
            split_mode=split_mode,
            sort_mode=sort_mode,
            deadline=deadline,
        )
        if sheet is None:
            continue
        used = set(_ids_in_sheets([sheet]) or ())
        if used != wanted:
            continue
        area_used = sum(int(pl["w"]) * int(pl["h"]) for pl in sheet.placements or [])
        simple_cuts = _guillotine_tree_cut_count(sheet)
        best_any = _best_offcut_info([sheet])
        key = (
            -area_used,
            simple_cuts,
            -int(best_any.get("value") or 0),
            "%s_%s_%s" % (fitness_mode, split_mode, sort_mode),
        )
        if best is None or key < best[0]:
            sheet.strategy = "group_mosaic_%s_%s_%s" % (fitness_mode, split_mode, sort_mode)
            best = (key, sheet)
    return best[1] if best else None


def _v2_group_mosaic_candidates(remaining, sheet_w, sheet_h, kerf, *, stats=None, deadline=None, allow_expensive=False):
    """Generate dense one-sheet mosaics by choosing size-family counts first.

    Greedy guillotine packing is sensitive to the input order. CutList-like
    varied jobs appear to build a dense sheet from repeated families, then solve
    that sheet as a guillotine tree. This generator explores that middle layer:
    select a bounded subset of repeated/large families, validate the subset with
    the guillotine kernel, and return only fully placed sheets.
    """
    started_at = time.monotonic()
    if len(remaining) < 8:
        return []

    groups = {}
    for piece in remaining:
        groups.setdefault(_piece_type_key(piece), []).append(piece)
    sheet_area = max(1, int(sheet_w) * int(sheet_h))
    ranked_groups = []
    for key, items in groups.items():
        area = int(key[0]) * int(key[1])
        qty = len(items)
        if qty < 2 and area < sheet_area * 0.07:
            continue
        ranked_groups.append((
            -min(qty, 4) * area,
            -area,
            key,
            list(items),
        ))
    ranked_groups.sort()
    if not ranked_groups:
        return []

    seed_groups = ranked_groups[:(10 if allow_expensive else 5)]
    pool_groups = ranked_groups[:(18 if allow_expensive else 10)]
    variants = (
        ("bssf", "llas", "cutlist_column"),
        ("bssf", "maxas", "cutlist_column"),
        ("bssf", "las", "cutlist_column"),
        ("bssf", "las", "short_side"),
        ("bssf", "maxas", "perimeter"),
    )
    candidates = []
    seen_subsets = set()

    def count_options(items, key):
        qty = len(items)
        opts = {1, min(qty, 2)}
        if qty >= 3:
            opts.add(3)
        if qty >= 4 and (key[0] * key[1]) < sheet_area * 0.09:
            opts.add(min(qty, 4))
        if qty >= 6 and (key[0] * key[1]) < sheet_area * 0.055:
            opts.add(min(qty, 8))
        return sorted({opt for opt in opts if 0 < opt <= qty}, reverse=True)

    def subset_key(items):
        return tuple(sorted(int(p["id"]) for p in items))

    def subset_area(items):
        return sum(int(_area(p)) for p in items)

    for _seed_rank, _seed_area, seed_key, seed_items in seed_groups:
        if _deadline_hit(deadline, 0.02):
            break
        for seed_count in count_options(seed_items, seed_key):
            if _deadline_hit(deadline, 0.02):
                break
            subset = list(seed_items[:seed_count])
            used_counts = Counter({seed_key: seed_count})
            sheet = _pack_subset_best_sheet(subset, sheet_w, sheet_h, kerf, deadline=deadline, variants=variants)
            if sheet is None:
                continue

            # Greedily add the family-count that gives the densest still-valid
            # guillotine sheet. Different seeds produce different local optima,
            # which become competing patterns for the global beam.
            while not _deadline_hit(deadline, 0.02):
                current_area = subset_area(subset)
                if current_area >= sheet_area * 0.975:
                    break
                best_option = None
                for _rank, _area_rank, key, items in pool_groups:
                    if _deadline_hit(deadline, 0.01):
                        break
                    already = int(used_counts.get(key) or 0)
                    available = items[already:]
                    if not available:
                        continue
                    for add_count in count_options(available, key):
                        trial = subset + available[:add_count]
                        skey = subset_key(trial)
                        if skey in seen_subsets:
                            continue
                        trial_area = subset_area(trial)
                        if trial_area > sheet_area * 0.995:
                            continue
                        trial_sheet = _pack_subset_best_sheet(
                            trial,
                            sheet_w,
                            sheet_h,
                            kerf,
                            deadline=deadline,
                            variants=variants,
                        )
                        if trial_sheet is None:
                            continue
                        simple_cuts = _guillotine_tree_cut_count(trial_sheet)
                        distinct = len({tuple(sorted((int(pl["w"]), int(pl["h"])))) for pl in trial_sheet.placements})
                        rank = (
                            -trial_area,
                            simple_cuts * 1000000 // max(1, trial_area),
                            -distinct,
                            -add_count,
                            key,
                        )
                        if best_option is None or rank < best_option[0]:
                            best_option = (rank, key, add_count, trial, trial_sheet)
                if best_option is None:
                    break
                _rank, key, add_count, subset, sheet = best_option
                used_counts[key] += add_count

            skey = subset_key(subset)
            if skey in seen_subsets:
                continue
            seen_subsets.add(skey)
            sheet.strategy = "group_mosaic"
            cand = _candidate_from_sheets([sheet], "v2_group_mosaic")
            _append_candidate_variants(candidates, cand, allow_mirror=False)

    _record_strategy_metric(stats, "v2_group_mosaic_candidates", _elapsed_ms(started_at), len(candidates))
    return candidates


def _v2_column_stack_candidates(remaining, sheet_w, sheet_h, kerf, *, stats=None, deadline=None, allow_expensive=False):
    """Generate CutList-style full-height column stack mosaics.

    Many industrial guillotine layouts first rip the sheet into vertical columns,
    then crosscut each column into repeated rows. A row may be one large part or
    several identical small parts across that column. This candidate family makes
    that structure explicit instead of hoping a free-rect order discovers it.
    """
    started_at = time.monotonic()
    if len(remaining) < 6:
        return []

    groups = {}
    for piece in remaining:
        groups.setdefault(_piece_type_key(piece), []).append(piece)
    sheet_area = max(1, int(sheet_w) * int(sheet_h))

    oriented_groups = []
    cap_filler_groups = []
    for key, items in groups.items():
        area = int(key[0]) * int(key[1])
        sample = items[0]
        for opt in _piece_orientations(sample, sheet_w, sheet_h):
            oriented = {
                "key": key,
                "items": list(items),
                "w": int(opt["w"]),
                "h": int(opt["h"]),
                "rotated": bool(opt["rotated"]),
                "area": area,
                "qty": len(items),
            }
            if len(items) < 2 and area < sheet_area * 0.04:
                cap_filler_groups.append(oriented)
            else:
                oriented_groups.append(oriented)
    if not oriented_groups:
        return []

    oriented_groups.sort(key=lambda g: (-min(g["qty"], 8) * g["area"], g["w"], -g["h"], g["key"]))
    primaries = oriented_groups[:(18 if allow_expensive else 10)]
    primary_ids = {id(group) for group in primaries}
    skinny_column_seeds = [
        group for group in oriented_groups
        if id(group) not in primary_ids
        and int(group["qty"]) >= 2
        and int(group["w"]) <= max(180, int(sheet_w) * 0.16)
        and int(group["h"]) >= int(sheet_h) * 0.20
    ]
    primaries = primaries + skinny_column_seeds[:(18 if allow_expensive else 8)]
    # Keep small cap-fill rows in play. Their area rank is low, but they are the
    # difference between a nearly-CutList column and a fully topped-off one.
    filler_seen = set()
    fillers = []
    for group in oriented_groups[:(64 if allow_expensive else 32)] + cap_filler_groups[:(48 if allow_expensive else 20)]:
        sig = (group["key"], int(group["w"]), int(group["h"]))
        if sig in filler_seen:
            continue
        filler_seen.add(sig)
        fillers.append(group)

    def layer_capacity(group, col_w, remaining_count):
        cap = int((int(col_w) + int(kerf)) // (int(group["w"]) + int(kerf)))
        cap = min(max(0, cap), int(remaining_count))
        if cap <= 0:
            return []
        counts = {1, cap}
        if cap >= 2:
            counts.add(2)
        if cap >= 3:
            counts.add(3)
        if cap >= 5:
            counts.add(min(cap, 6))
        return sorted(counts, reverse=True)

    def make_stack(seed_group, seed_rows, *, fill=True):
        col_w = int(seed_group["w"])
        used_by_key = Counter()
        layers = []
        y_used = 0

        def add_layer(group, count):
            nonlocal y_used
            if used_by_key[group["key"]] + count > len(group["items"]):
                return False
            row_w = count * int(group["w"]) + int(kerf) * max(0, count - 1)
            if row_w > col_w:
                return False
            next_h = y_used + (int(kerf) if layers else 0) + int(group["h"])
            if next_h > int(sheet_h):
                return False
            used_by_key[group["key"]] += count
            layers.append({
                "group": group,
                "count": int(count),
                "entries": [{"group": group, "count": int(count)}],
                "row_w": int(row_w),
                "h": int(group["h"]),
            })
            y_used = next_h
            return True

        def add_mixed_layer(entries):
            nonlocal y_used
            entry_counts = Counter()
            for group, count in entries:
                entry_counts[group["key"]] += int(count)
            if any(used_by_key[key] + count > len(groups[key]) for key, count in entry_counts.items()):
                return False
            row_w = (
                sum(int(group["w"]) * int(count) for group, count in entries)
                + int(kerf) * max(0, sum(int(count) for _group, count in entries) - 1)
            )
            row_h = max(int(group["h"]) for group, _count in entries)
            if row_w > col_w:
                return False
            next_h = y_used + (int(kerf) if layers else 0) + row_h
            if next_h > int(sheet_h):
                return False
            for key, count in entry_counts.items():
                used_by_key[key] += count
            layers.append({
                "entries": [
                    {"group": group, "count": int(count)}
                    for group, count in entries
                ],
                "row_w": int(row_w),
                "h": int(row_h),
            })
            y_used = next_h
            return True

        for _idx in range(seed_rows):
            if not add_layer(seed_group, 1):
                return None

        while fill and not _deadline_hit(deadline, 0.005):
            best = None
            for group in fillers:
                if group["w"] > col_w:
                    continue
                remaining_count = len(group["items"]) - int(used_by_key[group["key"]])
                for count in layer_capacity(group, col_w, remaining_count):
                    row_w = count * int(group["w"]) + int(kerf) * max(0, count - 1)
                    next_h = y_used + (int(kerf) if layers else 0) + int(group["h"])
                    if next_h > int(sheet_h):
                        continue
                    row_area = count * int(group["w"]) * int(group["h"])
                    width_waste = col_w - row_w
                    width_rank = 0 if width_waste == 0 else 1 + width_waste // 50
                    rank = (
                        width_rank,
                        -row_area,
                        width_waste,
                        int(group["h"]),
                        -count,
                        group["key"],
                    )
                    if best is None or rank < best[0]:
                        best = (rank, "single", group, count)
            remaining_h = int(sheet_h) - y_used - (int(kerf) if layers else 0)
            if remaining_h > 0:
                mix_groups = [
                    group for group in fillers
                    if int(group["w"]) <= col_w
                    and int(group["h"]) <= remaining_h
                    and len(group["items"]) - int(used_by_key[group["key"]]) > 0
                ]
                mix_groups.sort(key=lambda group: (-int(group["area"]), int(group["w"]), int(group["h"]), group["key"]))
                for left, right in combinations(mix_groups[:24], 2):
                    if left["key"] == right["key"] and left["w"] == right["w"] and left["h"] == right["h"]:
                        continue
                    row_w = int(left["w"]) + int(kerf) + int(right["w"])
                    if row_w > col_w:
                        continue
                    entry_counts = Counter((left["key"], right["key"]))
                    if any(used_by_key[key] + count > len(groups[key]) for key, count in entry_counts.items()):
                        continue
                    row_h = max(int(left["h"]), int(right["h"]))
                    row_area = int(left["w"]) * int(left["h"]) + int(right["w"]) * int(right["h"])
                    width_waste = col_w - row_w
                    width_rank = 0 if width_waste == 0 else 1 + width_waste // 50
                    rank = (
                        width_rank,
                        -row_area,
                        width_waste,
                        row_h,
                        -2,
                        (left["key"], right["key"]),
                    )
                    if best is None or rank < best[0]:
                        best = (rank, "mixed", [(left, 1), (right, 1)])
            if best is None:
                break
            _rank, kind, *payload = best
            before = y_used
            if kind == "mixed":
                if not add_mixed_layer(payload[0]):
                    break
            else:
                group, count = payload
                if not add_layer(group, count):
                    break
            if y_used == before:
                break

        if not layers:
            return None
        area = sum(
            int(entry["count"]) * int(entry["group"]["w"]) * int(entry["group"]["h"])
            for layer in layers
            for entry in (layer.get("entries") or [{"group": layer["group"], "count": layer["count"]}])
        )
        return {
            "w": int(col_w),
            "h": int(y_used),
            "area": int(area),
            "layers": layers,
            "signature": tuple(sorted(used_by_key.items())),
        }

    def _clone_layer(layer):
        cloned = dict(layer)
        if layer.get("entries"):
            cloned["entries"] = [dict(entry) for entry in layer.get("entries") or []]
        if layer.get("placements"):
            cloned["placements"] = [dict(entry) for entry in layer.get("placements") or []]
        return cloned

    def _layer_entries(layer):
        if layer.get("placements"):
            for entry in layer.get("placements") or []:
                yield {
                    "group": entry["group"],
                    "count": int(entry.get("count") or 1),
                }
            return
        for entry in (layer.get("entries") or [{"group": layer["group"], "count": layer["count"]}]):
            yield entry

    def _layer_counts(layer):
        counts = Counter()
        for entry in _layer_entries(layer):
            counts[entry["group"]["key"]] += int(entry["count"])
        return counts

    def _layers_counts(layers):
        counts = Counter()
        for layer in layers or []:
            counts.update(_layer_counts(layer))
        return counts

    def _layer_signature(layer):
        if layer.get("placements"):
            return tuple(
                (
                    "p",
                    entry["group"]["key"],
                    int(entry.get("count") or 1),
                    int(entry["group"]["w"]),
                    int(entry["group"]["h"]),
                    int(entry.get("x") or 0),
                    int(entry.get("y") or 0),
                )
                for entry in sorted(
                    layer.get("placements") or [],
                    key=lambda e: (
                        int(e.get("y") or 0),
                        int(e.get("x") or 0),
                        e["group"]["key"],
                        int(e["group"]["w"]),
                        int(e["group"]["h"]),
                    ),
                )
            )
        return tuple(
            (
                "r",
                entry["group"]["key"],
                int(entry["count"]),
                int(entry["group"]["w"]),
                int(entry["group"]["h"]),
            )
            for entry in _layer_entries(layer)
        )

    def _layers_signature(layers):
        return tuple(_layer_signature(layer) for layer in layers or [])

    def _stack_area(layers):
        return sum(
            int(entry["count"]) * int(entry["group"]["w"]) * int(entry["group"]["h"])
            for layer in layers
            for entry in _layer_entries(layer)
        )

    def _row_options_for_stack(stack, limit=18):
        col_w = int(stack["w"])
        y_used = int(stack["h"])
        used_by_key = Counter(dict(stack["signature"]))
        remaining_h = int(sheet_h) - y_used - (int(kerf) if stack["layers"] else 0)
        if remaining_h <= 0:
            return []
        options = []
        for group in fillers:
            if int(group["w"]) > col_w or int(group["h"]) > remaining_h:
                continue
            remaining_count = len(group["items"]) - int(used_by_key[group["key"]])
            for count in layer_capacity(group, col_w, remaining_count):
                row_w = count * int(group["w"]) + int(kerf) * max(0, count - 1)
                if row_w > col_w:
                    continue
                row_area = count * int(group["w"]) * int(group["h"])
                width_waste = col_w - row_w
                width_rank = 0 if width_waste == 0 else 1 + width_waste // 50
                rank = (
                    width_rank,
                    -row_area,
                    width_waste,
                    int(group["h"]),
                    -count,
                    group["key"],
                )
                options.append((rank, [{
                    "group": group,
                    "count": int(count),
                }], int(row_w), int(group["h"])))

        mix_groups = [
            group for group in fillers
            if int(group["w"]) <= col_w
            and int(group["h"]) <= remaining_h
            and len(group["items"]) - int(used_by_key[group["key"]]) > 0
        ]
        mix_groups.sort(key=lambda group: (-int(group["area"]), int(group["w"]), int(group["h"]), group["key"]))
        for left, right in combinations(mix_groups[:24], 2):
            if left["key"] == right["key"] and left["w"] == right["w"] and left["h"] == right["h"]:
                continue
            row_w = int(left["w"]) + int(kerf) + int(right["w"])
            if row_w > col_w:
                continue
            entry_counts = Counter((left["key"], right["key"]))
            if any(used_by_key[key] + count > len(groups[key]) for key, count in entry_counts.items()):
                continue
            row_h = max(int(left["h"]), int(right["h"]))
            row_area = int(left["w"]) * int(left["h"]) + int(right["w"]) * int(right["h"])
            width_waste = col_w - row_w
            width_rank = 0 if width_waste == 0 else 1 + width_waste // 50
            rank = (
                width_rank,
                -row_area,
                width_waste,
                row_h,
                -2,
                (left["key"], right["key"]),
            )
            options.append((rank, [
                {"group": left, "count": 1},
                {"group": right, "count": 1},
            ], int(row_w), int(row_h)))
        for double in mix_groups[:24]:
            if len(double["items"]) - int(used_by_key[double["key"]]) < 2:
                continue
            for single in mix_groups[:24]:
                if (
                    single["key"] == double["key"]
                    and single["w"] == double["w"]
                    and single["h"] == double["h"]
                ):
                    continue
                row_w = int(double["w"]) * 2 + int(single["w"]) + int(kerf) * 2
                if row_w > col_w:
                    continue
                entry_counts = Counter()
                entry_counts[double["key"]] += 2
                entry_counts[single["key"]] += 1
                if any(used_by_key[key] + count > len(groups[key]) for key, count in entry_counts.items()):
                    continue
                row_h = max(int(double["h"]), int(single["h"]))
                row_area = int(double["w"]) * int(double["h"]) * 2 + int(single["w"]) * int(single["h"])
                width_waste = col_w - row_w
                width_rank = 0 if width_waste == 0 else 1 + width_waste // 50
                rank = (
                    width_rank,
                    -row_area,
                    width_waste,
                    row_h,
                    -3,
                    (double["key"], single["key"]),
                )
                options.append((rank, [
                    {"group": double, "count": 2},
                    {"group": single, "count": 1},
                ], int(row_w), int(row_h)))

        options.sort(key=lambda item: item[0])
        return options[:limit]

    def _append_row_to_stack(stack, entries, row_w, row_h):
        used_by_key = Counter(dict(stack["signature"]))
        for entry in entries:
            used_by_key[entry["group"]["key"]] += int(entry["count"])
        if any(count > len(groups[key]) for key, count in used_by_key.items()):
            return None
        y_used = int(stack["h"]) + (int(kerf) if stack["layers"] else 0) + int(row_h)
        if y_used > int(sheet_h):
            return None
        layers = [_clone_layer(layer) for layer in stack["layers"]]
        layers.append({
            "entries": [dict(entry) for entry in entries],
            "row_w": int(row_w),
            "h": int(row_h),
        })
        return {
            "w": int(stack["w"]),
            "h": int(y_used),
            "area": int(_stack_area(layers)),
            "layers": layers,
            "signature": tuple(sorted(used_by_key.items())),
        }

    def _mini_row_options(width_limit, height_limit, used_by_key, limit=24):
        width_limit = int(width_limit)
        height_limit = int(height_limit)
        if width_limit <= 0 or height_limit <= 0:
            return []
        options = []
        mini_groups = [
            group for group in fillers
            if int(group["w"]) <= width_limit
            and int(group["h"]) <= height_limit
            and len(group["items"]) - int(used_by_key[group["key"]]) > 0
        ]
        mini_groups.sort(key=lambda group: (-int(group["area"]), int(group["w"]), int(group["h"]), group["key"]))

        def add_option(entries, family_key):
            entry_counts = Counter()
            panel_count = 0
            for group, count in entries:
                count = int(count)
                entry_counts[group["key"]] += count
                panel_count += count
            if any(int(used_by_key[key]) + count > len(groups[key]) for key, count in entry_counts.items()):
                return
            row_w = (
                sum(int(group["w"]) * int(count) for group, count in entries)
                + int(kerf) * max(0, panel_count - 1)
            )
            if row_w > width_limit:
                return
            row_h = max(int(group["h"]) for group, _count in entries)
            if row_h > height_limit:
                return
            row_area = sum(int(group["w"]) * int(group["h"]) * int(count) for group, count in entries)
            width_waste = width_limit - row_w
            width_rank = 0 if width_waste == 0 else 1 + width_waste // 50
            rank = (
                width_rank,
                -int(row_area),
                width_waste,
                row_h,
                -panel_count,
                family_key,
            )
            options.append((rank, [(group, int(count)) for group, count in entries], int(row_w), int(row_h), int(row_area), entry_counts))

        row_group_limit = 48 if allow_expensive else 30
        for group in mini_groups[:row_group_limit]:
            remaining_count = len(group["items"]) - int(used_by_key[group["key"]])
            for count in layer_capacity(group, width_limit, remaining_count):
                add_option([(group, count)], (group["key"],))

        for left, right in combinations(mini_groups[:row_group_limit], 2):
            if left["key"] == right["key"] and left["w"] == right["w"] and left["h"] == right["h"]:
                continue
            add_option([(left, 1), (right, 1)], (left["key"], right["key"]))

        for double in mini_groups[:row_group_limit]:
            if len(double["items"]) - int(used_by_key[double["key"]]) < 2:
                continue
            for single in mini_groups[:row_group_limit]:
                if (
                    single["key"] == double["key"]
                    and single["w"] == double["w"]
                    and single["h"] == double["h"]
                ):
                    continue
                add_option([(double, 2), (single, 1)], (double["key"], single["key"]))

        options.sort(key=lambda item: item[0])
        kept = []
        seen_options = set()

        def row_sig(option):
            _rank, entries, row_w, row_h, _row_area, _entry_counts = option
            return (
                int(row_w),
                int(row_h),
                tuple((group["key"], int(group["w"]), int(group["h"]), int(count)) for group, count in entries),
            )

        def keep(source, max_items):
            for option in source:
                sig = row_sig(option)
                if sig in seen_options:
                    continue
                seen_options.add(sig)
                kept.append(option)
                if len(kept) >= max_items:
                    return

        keep(options, max(1, int(limit) // 2))

        family_buckets = {}
        for option in options:
            _rank, _entries, _row_w, _row_h, _row_area, entry_counts = option
            if not entry_counts:
                continue
            family_key = max(entry_counts, key=lambda key: int(key[0]) * int(key[1]) * int(entry_counts[key]))
            family_buckets.setdefault(family_key, []).append(option)
        for family_key in sorted(family_buckets):
            keep(sorted(family_buckets[family_key], key=lambda item: item[0])[:3], int(limit))

        keep(
            sorted(options, key=lambda item: (-int(item[4]), item[0])),
            int(limit),
        )
        return kept[:limit]

    def _mini_stack_candidates(width_limit, height_limit, used_by_key, limit=44):
        seed = {
            "w": 0,
            "h": 0,
            "area": 0,
            "counts": Counter(),
            "placements": [],
            "rows": 0,
        }
        states = [seed]
        results = []
        seen = set()

        def _mini_state_key(state):
            return (
                -(int(state["area"])),
                int(width_limit) - int(state["w"]),
                int(height_limit) - int(state["h"]),
                -int(state["rows"]),
                tuple(sorted(state["counts"].items())),
            )

        def _mini_state_sig(state):
            return (
                int(state["w"]),
                int(state["h"]),
                tuple(sorted(state["counts"].items())),
                tuple(
                    (
                        pl["group"]["key"],
                        int(pl["group"]["w"]),
                        int(pl["group"]["h"]),
                        int(pl["x"]),
                        int(pl["y"]),
                    )
                    for pl in state["placements"]
                ),
            )

        def _select_mini_states(candidates, keep_limit):
            candidates = sorted(candidates, key=_mini_state_key)
            kept = []
            seen_state = set()

            def keep(source, max_items):
                for state in source:
                    sig = _mini_state_sig(state)
                    if sig in seen_state:
                        continue
                    seen_state.add(sig)
                    kept.append(state)
                    if len(kept) >= max_items:
                        return

            keep(candidates, max(1, int(keep_limit) // 2))

            family_buckets = {}
            width_buckets = {}
            for state in candidates:
                if state["counts"]:
                    family = max(
                        state["counts"],
                        key=lambda key: int(key[0]) * int(key[1]) * int(state["counts"][key]),
                    )
                    family_buckets.setdefault(family, []).append(state)
                width_buckets.setdefault(int(state["w"]), []).append(state)
            for width in sorted(width_buckets):
                keep(sorted(width_buckets[width], key=_mini_state_key)[:2], int(keep_limit))
            for family in sorted(family_buckets):
                bucket = sorted(family_buckets[family], key=_mini_state_key)
                keep(bucket[:3], int(keep_limit))
                secondary_buckets = {}
                for state in bucket:
                    counts = Counter(state["counts"])
                    counts.pop(family, None)
                    if not counts:
                        continue
                    secondary = max(counts, key=lambda key: int(key[0]) * int(key[1]) * int(counts[key]))
                    secondary_buckets.setdefault(secondary, []).append(state)
                for secondary in sorted(secondary_buckets):
                    keep(sorted(secondary_buckets[secondary], key=_mini_state_key)[:2], int(keep_limit))
            return kept[:keep_limit]

        def _state_from_row_entries(entries):
            counts = Counter()
            panel_count = 0
            for group, count in entries:
                count = int(count)
                counts[group["key"]] += count
                panel_count += count
            if any(int(used_by_key[key]) + count > len(groups[key]) for key, count in counts.items()):
                return None
            row_w = (
                sum(int(group["w"]) * int(count) for group, count in entries)
                + int(kerf) * max(0, panel_count - 1)
            )
            row_h = max(int(group["h"]) for group, _count in entries)
            if row_w > int(width_limit) or row_h > int(height_limit):
                return None
            placements = []
            x = 0
            for group, count in entries:
                for _idx in range(int(count)):
                    placements.append({
                        "group": group,
                        "count": 1,
                        "x": int(x),
                        "y": 0,
                    })
                    x += int(group["w"]) + int(kerf)
            return {
                "w": int(row_w),
                "h": int(row_h),
                "area": sum(int(group["w"]) * int(group["h"]) * int(count) for group, count in entries),
                "counts": counts,
                "placements": placements,
                "rows": 1,
            }

        anchored_states = []
        anchor_groups = [
            group for group in fillers
            if int(group["w"]) <= int(width_limit)
            and int(group["h"]) <= int(height_limit)
            and len(group["items"]) - int(used_by_key[group["key"]]) > 0
        ]
        anchor_groups.sort(key=lambda group: (-int(group["area"]), int(group["w"]), int(group["h"]), group["key"]))
        for anchor in anchor_groups[:(18 if allow_expensive else 8)]:
            state = _state_from_row_entries([(anchor, 1)])
            if state:
                anchored_states.append(state)
            for companion in anchor_groups[:(48 if allow_expensive else 24)]:
                if (
                    companion["key"] == anchor["key"]
                    and companion["w"] == anchor["w"]
                    and companion["h"] == anchor["h"]
                    and len(companion["items"]) - int(used_by_key[companion["key"]]) < 2
                ):
                    continue
                state = _state_from_row_entries([(anchor, 1), (companion, 1)])
                if state:
                    anchored_states.append(state)
        anchored_states = _select_mini_states(anchored_states, (30 if allow_expensive else 12))
        states = [seed] + anchored_states
        results.extend(anchored_states)
        seen.update(_mini_state_sig(state) for state in anchored_states)

        for _depth in range(3):
            if _deadline_hit(deadline, 0.005):
                break
            next_states = []
            for state in states:
                if _deadline_hit(deadline, 0.005):
                    break
                gap = int(kerf) if state["rows"] else 0
                remaining_h = int(height_limit) - int(state["h"]) - gap
                if remaining_h <= 0:
                    continue
                row_used = Counter(used_by_key)
                row_used.update(state["counts"])
                for _rank, entries, row_w, row_h, row_area, entry_counts in _mini_row_options(
                    width_limit,
                    remaining_h,
                    row_used,
                    limit=(34 if allow_expensive else 18),
                ):
                    y = int(state["h"]) + gap
                    x = 0
                    placements = [dict(pl) for pl in state["placements"]]
                    for group, count in entries:
                        for _idx in range(int(count)):
                            placements.append({
                                "group": group,
                                "count": 1,
                                "x": int(x),
                                "y": int(y),
                            })
                            x += int(group["w"]) + int(kerf)
                    counts = Counter(state["counts"])
                    counts.update(entry_counts)
                    child = {
                        "w": max(int(state["w"]), int(row_w)),
                        "h": y + int(row_h),
                        "area": int(state["area"]) + int(row_area),
                        "counts": counts,
                        "placements": placements,
                        "rows": int(state["rows"]) + 1,
                    }
                    sig = (
                        int(child["w"]),
                        int(child["h"]),
                        tuple(sorted(counts.items())),
                        tuple(
                            (
                                pl["group"]["key"],
                                int(pl["group"]["w"]),
                                int(pl["group"]["h"]),
                                int(pl["x"]),
                                int(pl["y"]),
                            )
                            for pl in placements
                        ),
                    )
                    if sig in seen:
                        continue
                    seen.add(sig)
                    results.append(child)
                    next_states.append(child)
            if not next_states:
                break
            states = _select_mini_states(next_states, (26 if allow_expensive else 12))
        def result_key(state):
            return _mini_state_key(state)

        def add_kept(target, source, max_items):
            seen_local = {
                _mini_state_sig(state)
                for state in target
            }
            for state in source:
                key = _mini_state_sig(state)
                if key in seen_local:
                    continue
                seen_local.add(key)
                target.append(state)
                if len(target) >= max_items:
                    return

        results.sort(key=result_key)
        kept = []
        add_kept(kept, results, max(1, int(limit) // 2))
        add_kept(
            kept,
            sorted(results, key=lambda state: (int(state["w"]), -int(state["area"]), int(height_limit) - int(state["h"]))),
            max(1, int(limit) * 3 // 4),
        )
        add_kept(
            kept,
            sorted(results, key=lambda state: (int(height_limit) - int(state["h"]), int(state["w"]), -int(state["area"]))),
            max(1, int(limit)),
        )

        family_buckets = {}
        width_buckets = {}
        for state in results:
            if state["counts"]:
                family = max(
                    state["counts"],
                    key=lambda key: int(key[0]) * int(key[1]) * int(state["counts"][key]),
                )
                family_buckets.setdefault(family, []).append(state)
            width_buckets.setdefault(int(state["w"]), []).append(state)
        for width in sorted(width_buckets):
            bucket = sorted(width_buckets[width], key=result_key)
            add_kept(kept, bucket[:3], max(1, int(limit)))
        for family in sorted(family_buckets):
            bucket = sorted(family_buckets[family], key=result_key)
            add_kept(kept, bucket[:4], max(1, int(limit)))
            secondary_buckets = {}
            for state in bucket:
                counts = Counter(state["counts"])
                counts.pop(family, None)
                if not counts:
                    continue
                secondary = max(counts, key=lambda key: int(key[0]) * int(key[1]) * int(counts[key]))
                secondary_buckets.setdefault(secondary, []).append(state)
            for secondary in sorted(secondary_buckets):
                secondary_bucket = sorted(secondary_buckets[secondary], key=result_key)
                add_kept(kept, secondary_bucket[:3], max(1, int(limit)))
        return kept[:limit]

    def _append_cap_to_stack(stack, option):
        used_by_key = Counter(dict(stack["signature"]))
        used_by_key.update(option["counts"])
        if any(count > len(groups[key]) for key, count in used_by_key.items()):
            return None
        y_used = int(stack["h"]) + (int(kerf) if stack["layers"] else 0) + int(option["h"])
        if y_used > int(sheet_h):
            return None
        layers = [_clone_layer(layer) for layer in stack["layers"]]
        layers.append({
            "placements": [dict(pl) for pl in option["placements"]],
            "row_w": int(option["w"]),
            "h": int(option["h"]),
            "cap_mosaic": True,
        })
        return {
            "w": int(stack["w"]),
            "h": int(y_used),
            "area": int(_stack_area(layers)),
            "layers": layers,
            "signature": tuple(sorted(used_by_key.items())),
        }

    def _cap_mosaic_options(stack, limit=16):
        if _deadline_hit(deadline, 0.005):
            return []
        used_by_key = Counter(dict(stack["signature"]))
        cap_w = int(stack["w"])
        cap_h = int(sheet_h) - int(stack["h"]) - (int(kerf) if stack["layers"] else 0)
        if cap_w <= 0 or cap_h <= 0:
            return []
        mini = _mini_stack_candidates(
            cap_w,
            cap_h,
            used_by_key,
            limit=(120 if allow_expensive else 44),
        )
        if not mini:
            return []

        options = []
        seen = set()

        def add_option(parts):
            counts = Counter()
            placements = []
            x0 = 0
            used_w = 0
            used_h = 0
            area = 0
            for idx, part in enumerate(parts):
                if idx:
                    x0 += int(kerf)
                for pl in part["placements"]:
                    rel = dict(pl)
                    rel["x"] = int(x0) + int(rel.get("x") or 0)
                    rel["y"] = int(rel.get("y") or 0)
                    placements.append(rel)
                counts.update(part["counts"])
                used_w = max(used_w, int(x0) + int(part["w"]))
                used_h = max(used_h, int(part["h"]))
                area += int(part["area"])
                x0 += int(part["w"])
            if not placements or used_w > cap_w or used_h > cap_h:
                return
            if any(int(used_by_key[key]) + count > len(groups[key]) for key, count in counts.items()):
                return
            sig = (
                int(used_w),
                int(used_h),
                tuple(sorted(counts.items())),
                tuple(
                    (
                        pl["group"]["key"],
                        int(pl["group"]["w"]),
                        int(pl["group"]["h"]),
                        int(pl["x"]),
                        int(pl["y"]),
                    )
                    for pl in sorted(placements, key=lambda item: (int(item["y"]), int(item["x"]), item["group"]["key"]))
                ),
            )
            if sig in seen:
                return
            seen.add(sig)
            width_waste = cap_w - used_w
            height_waste = cap_h - used_h
            rank = (
                -int(area),
                width_waste,
                height_waste,
                -len(placements),
                tuple(sorted(counts.items())),
            )
            options.append((rank, {
                "w": int(used_w),
                "h": int(used_h),
                "area": int(area),
                "counts": counts,
                "placements": placements,
            }))

        for part in mini[:(70 if allow_expensive else 24)]:
            add_option([part])

        pair_pool = mini[:(90 if allow_expensive else 28)]
        for left in pair_pool:
            if _deadline_hit(deadline, 0.005):
                break
            for right in pair_pool:
                if int(left["w"]) + int(kerf) + int(right["w"]) > cap_w:
                    continue
                add_option([left, right])

        # A clean CutList-style cap is often not a row; it is a small guillotine
        # mosaic inside the top of a lane. Bounded three-stack caps capture cases
        # such as 270 + 270 + 130 inside a 740 mm strip without turning the
        # generator into an unbounded exact-cover search.
        if allow_expensive and not _deadline_hit(deadline, 0.02):
            triple_pool = mini[:54]
            narrow_pool = sorted(
                triple_pool,
                key=lambda part: (
                    int(part["w"]),
                    -int(part["area"]),
                    tuple(sorted(part["counts"].items())),
                ),
            )[:24]
            for left in triple_pool:
                if _deadline_hit(deadline, 0.005):
                    break
                for middle in triple_pool:
                    used_w = int(left["w"]) + int(kerf) + int(middle["w"])
                    remaining_w = int(cap_w) - used_w - int(kerf)
                    if remaining_w <= 0:
                        continue
                    for right in narrow_pool:
                        if int(right["w"]) > remaining_w:
                            continue
                        add_option([left, middle, right])
                        break

        options.sort(key=lambda item: item[0])
        kept = []
        seen_options = set()

        def option_sig(option):
            return (
                int(option["w"]),
                int(option["h"]),
                tuple(sorted(option["counts"].items())),
                tuple(
                    (
                        pl["group"]["key"],
                        int(pl["group"]["w"]),
                        int(pl["group"]["h"]),
                        int(pl["x"]),
                        int(pl["y"]),
                    )
                    for pl in sorted(option["placements"], key=lambda item: (int(item["y"]), int(item["x"]), item["group"]["key"]))
                ),
            )

        def keep(source, max_items):
            for _rank, option in source:
                sig = option_sig(option)
                if sig in seen_options:
                    continue
                seen_options.add(sig)
                kept.append(option)
                if len(kept) >= max_items:
                    return

        keep(options, max(1, int(limit) // 2))

        dominant_buckets = {}
        for rank, option in options:
            if not option["counts"]:
                continue
            family_key = max(
                option["counts"],
                key=lambda key: int(key[0]) * int(key[1]) * int(option["counts"][key]),
            )
            dominant_buckets.setdefault(family_key, []).append((rank, option))
        for family_key in sorted(dominant_buckets):
            keep(sorted(dominant_buckets[family_key], key=lambda item: item[0])[:3], int(limit))

        keep(
            sorted(
                options,
                key=lambda item: (
                    -max((int(key[0]) * int(key[1]) for key in item[1]["counts"]), default=0),
                    len(item[1]["counts"]),
                    item[0],
                ),
            ),
            int(limit),
        )
        return kept[:limit]

    def make_stack_variants(seed_group, seed_rows):
        base = make_stack(seed_group, seed_rows, fill=False)
        if not base:
            return []
        variants = [base]
        states = [base]
        for _depth in range(3):
            next_states = []
            for state in states:
                for _rank, entries, row_w, row_h in _row_options_for_stack(state):
                    child = _append_row_to_stack(state, entries, row_w, row_h)
                    if child:
                        variants.append(child)
                        next_states.append(child)
            if not next_states:
                break
            next_states.sort(key=lambda stack: (
                -int(stack["area"]),
                int(sheet_h) - int(stack["h"]),
                int(stack["w"]),
                tuple(stack["signature"]),
            ))
            states = next_states[:10]
        cap_sources = sorted(variants, key=lambda stack: (
            int(sheet_h) - int(stack["h"]),
            -int(stack["area"]),
            tuple(stack["signature"]),
        ))[:(22 if allow_expensive else 10)]
        for state in cap_sources:
            if _deadline_hit(deadline, 0.005):
                break
            for option in _cap_mosaic_options(state, limit=(80 if allow_expensive else 20)):
                child = _append_cap_to_stack(state, option)
                if child:
                    variants.append(child)
        return variants

    stack_options = []
    seen_stacks = set()
    for group in primaries:
        if _deadline_hit(deadline, 0.02):
            break
        max_rows = min(
            int(group["qty"]),
            int((int(sheet_h) + int(kerf)) // (int(group["h"]) + int(kerf))),
        )
        if max_rows <= 0:
            continue
        row_counts = {max_rows, max(1, max_rows - 1), max(1, max_rows - 2)}
        if max_rows >= 3:
            row_counts.add(3)
        for rows in sorted(row_counts, reverse=True):
            generated = [make_stack(group, rows, fill=True)]
            generated.extend(make_stack_variants(group, rows))
            for stack in generated:
                if not stack:
                    continue
                sig = (
                    stack["w"],
                    stack["signature"],
                    _layers_signature(stack["layers"]),
                )
                if sig in seen_stacks:
                    continue
                seen_stacks.add(sig)
                stack_options.append(stack)

    if stats is not None:
        stats["column_stack_generated"] = len(stack_options)
    stack_options.sort(key=lambda s: (-s["area"], s["w"], -sum(count for _key, count in s["signature"])))
    keep_limit = 96 if allow_expensive else 48
    kept_options = list(stack_options[:keep_limit])
    kept_ids = {id(stack) for stack in kept_options}
    def exact_row_count(stack):
        return sum(
            1 for layer in stack["layers"]
            if int(layer.get("row_w") or 0) == int(stack["w"])
        )

    protected_buckets = {}
    protected_family_buckets = {}
    for stack in stack_options[keep_limit:]:
        protect_stack = (
            (
                int(stack["w"]) <= max(180, int(sheet_w) * 0.16)
                and int(stack["h"]) >= int(sheet_h) * 0.50
            )
            or int(stack["area"]) >= int(sheet_area) * 0.25
            or (
                int(stack["w"]) <= int(sheet_w) * 0.60
                and int(stack["area"]) >= int(sheet_area) * 0.18
            )
        )
        if protect_stack and id(stack) not in kept_ids:
            protected_buckets.setdefault(int(stack["w"]), []).append(stack)
            counts = Counter(dict(stack["signature"]))
            if counts:
                family_key = max(counts, key=lambda key: int(key[0]) * int(key[1]))
                protected_family_buckets.setdefault((int(stack["w"]), family_key), []).append(stack)
    for width in sorted(protected_buckets):
        bucket = protected_buckets[width]
        bucket.sort(key=lambda stack: (
            -exact_row_count(stack),
            -int(stack["area"]),
            int(sheet_h) - int(stack["h"]),
            tuple(stack["signature"]),
        ))
        for stack in bucket[:(40 if allow_expensive else 14)]:
            if id(stack) in kept_ids:
                continue
            kept_options.append(stack)
            kept_ids.add(id(stack))
    for family in sorted(protected_family_buckets):
        bucket = protected_family_buckets[family]
        bucket.sort(key=lambda stack: (
            -exact_row_count(stack),
            -int(stack["area"]),
            int(sheet_h) - int(stack["h"]),
            tuple(stack["signature"]),
        ))
        for stack in bucket[:(40 if allow_expensive else 10)]:
            if id(stack) in kept_ids:
                continue
            kept_options.append(stack)
            kept_ids.add(id(stack))
    stack_options = kept_options

    def _stack_pool(*groups_to_merge, limit=None):
        pool = []
        seen = set()
        for group in groups_to_merge:
            for stack in group:
                key = (
                    int(stack["w"]),
                    tuple(stack["signature"]),
                    _layers_signature(stack["layers"]),
                )
                if key in seen:
                    continue
                seen.add(key)
                pool.append(stack)
                if limit and len(pool) >= limit:
                    return pool
        return pool

    area_pool = sorted(stack_options, key=lambda stack: (-int(stack["area"]), int(stack["w"])))
    narrow_first_pool = sorted(stack_options, key=lambda stack: (int(stack["w"]), -int(stack["area"])))
    half_width_pool = sorted(
        stack_options,
        key=lambda stack: (abs(int(stack["w"]) - int(sheet_w) // 2), -int(stack["area"])),
    )
    exact_row_pool = sorted(
        stack_options,
        key=lambda stack: (-exact_row_count(stack), -int(stack["area"]), int(stack["w"])),
    )
    exact_by_width = []
    width_groups = {}
    family_groups = {}
    for stack in stack_options:
        width_groups.setdefault(int(stack["w"]), []).append(stack)
        counts = Counter(dict(stack["signature"]))
        if counts:
            family_key = max(counts, key=lambda key: int(key[0]) * int(key[1]))
            family_groups.setdefault((int(stack["w"]), family_key), []).append(stack)
    for width in sorted(width_groups):
        bucket = width_groups[width]
        bucket.sort(key=lambda stack: (-exact_row_count(stack), -int(stack["area"]), int(sheet_h) - int(stack["h"])))
        exact_by_width.extend(bucket[:(20 if allow_expensive else 8)])
    exact_by_family = []
    for family in sorted(family_groups):
        bucket = family_groups[family]
        bucket.sort(key=lambda stack: (-exact_row_count(stack), -int(stack["area"]), int(sheet_h) - int(stack["h"])))
        exact_by_family.extend(bucket[:(12 if allow_expensive else 5)])
    exact_by_family.sort(key=lambda stack: (
        0 if int(stack["w"]) <= int(sheet_w) * 0.75 else 1,
        abs(int(stack["w"]) - int(sheet_w) // 2),
        -int(stack["area"]),
        -exact_row_count(stack),
    ))
    anchor_pair_pool = [
        stack for stack in stack_options
        if int(sheet_w) * 0.20 <= int(stack["w"]) <= int(sheet_w) * 0.80
        and any(
            count >= 2 and int(key[0]) * int(key[1]) >= int(sheet_area) * 0.12
            for key, count in Counter(dict(stack["signature"])).items()
        )
    ]
    anchor_pair_pool.sort(key=lambda stack: (
        -exact_row_count(stack),
        -max(
            int(key[0]) * int(key[1]) * int(count)
            for key, count in Counter(dict(stack["signature"])).items()
        ),
        -int(stack["area"]),
    ))
    skinny_pair_pool = [
        stack for stack in stack_options
        if int(stack["w"]) <= max(180, int(sheet_w) * 0.16)
        and int(stack["h"]) >= int(sheet_h) * 0.35
    ]
    skinny_pair_pool.sort(key=lambda stack: (
        int(stack["w"]),
        sum(count for _key, count in stack["signature"]),
        -int(stack["h"]),
        -int(stack["area"]),
    ))
    side_repeat_pool = [
        stack for stack in stack_options
        if int(sheet_w) * 0.15 <= int(stack["w"]) <= int(sheet_w) * 0.35
        and any(
            count >= 3 and int(key[0]) * int(key[1]) >= int(sheet_area) * 0.04
            for key, count in Counter(dict(stack["signature"])).items()
        )
    ]
    side_repeat_pool.sort(key=lambda stack: (
        -exact_row_count(stack),
        -max(
            int(key[0]) * int(key[1]) * int(count)
            for key, count in Counter(dict(stack["signature"])).items()
        ),
        int(stack["w"]),
    ))
    wide_companion_pool = [
        stack for stack in stack_options
        if int(sheet_w) * 0.55 <= int(stack["w"]) <= int(sheet_w) * 0.88
        and any(
            count >= 2 and int(key[0]) * int(key[1]) >= int(sheet_area) * 0.10
            for key, count in Counter(dict(stack["signature"])).items()
        )
    ]
    wide_companion_pool.sort(key=lambda stack: (
        -exact_row_count(stack),
        int(sheet_w) - int(stack["w"]),
        -max(
            int(key[0]) * int(key[1]) * int(count)
            for key, count in Counter(dict(stack["signature"])).items()
        ),
        -int(stack["area"]),
    ))
    wide_companion_family_pool = []
    wide_family_buckets = {}
    for stack in wide_companion_pool:
        counts = Counter(dict(stack["signature"]))
        if not counts:
            continue
        family_key = max(counts, key=lambda key: int(key[0]) * int(key[1]) * int(counts[key]))
        wide_family_buckets.setdefault(family_key, []).append(stack)
    wide_seen = set()
    def add_wide_stack(stack):
        key = (
            int(stack["w"]),
            tuple(stack["signature"]),
            _layers_signature(stack["layers"]),
        )
        if key in wide_seen:
            return
        wide_seen.add(key)
        wide_companion_family_pool.append(stack)

    for family_key in sorted(wide_family_buckets):
        bucket = wide_family_buckets[family_key]
        bucket.sort(key=lambda stack: (
            -exact_row_count(stack),
            int(sheet_w) - int(stack["w"]),
            -int(stack["area"]),
            tuple(stack["signature"]),
        ))
        for stack in bucket[:(24 if allow_expensive else 6)]:
            add_wide_stack(stack)
        secondary_buckets = {}
        for stack in bucket:
            counts = Counter(dict(stack["signature"]))
            counts.pop(family_key, None)
            if not counts:
                continue
            secondary_key = max(counts, key=lambda key: int(key[0]) * int(key[1]) * int(counts[key]))
            secondary_buckets.setdefault(secondary_key, []).append(stack)
        for secondary_key in sorted(secondary_buckets):
            secondary_bucket = secondary_buckets[secondary_key]
            secondary_bucket.sort(key=lambda stack: (
                -exact_row_count(stack),
                int(sheet_w) - int(stack["w"]),
                -int(stack["area"]),
                tuple(stack["signature"]),
            ))
            for stack in secondary_bucket[:(18 if allow_expensive else 4)]:
                add_wide_stack(stack)
    wide_companion_family_pool.sort(key=lambda stack: (
        -exact_row_count(stack),
        int(sheet_w) - int(stack["w"]),
        -int(stack["area"]),
        tuple(stack["signature"]),
    ))
    pair_pool = _stack_pool(
        wide_companion_family_pool[:(260 if allow_expensive else 72)],
        anchor_pair_pool[:(80 if allow_expensive else 32)],
        side_repeat_pool[:(80 if allow_expensive else 32)],
        skinny_pair_pool[:(80 if allow_expensive else 32)],
        exact_by_family,
        exact_by_width,
        area_pool[:(100 if allow_expensive else 48)],
        exact_row_pool[:(100 if allow_expensive else 48)],
        narrow_first_pool[:(80 if allow_expensive else 36)],
        half_width_pool[:(80 if allow_expensive else 36)],
        limit=(360 if allow_expensive else 128),
    )
    anchor_pool_for_pairs = _stack_pool(
        wide_companion_family_pool[:(280 if allow_expensive else 80)],
        anchor_pair_pool[:(90 if allow_expensive else 36)],
        side_repeat_pool[:(90 if allow_expensive else 36)],
        exact_by_family[:(90 if allow_expensive else 36)],
        exact_by_width[:(90 if allow_expensive else 36)],
        limit=(320 if allow_expensive else 120),
    )
    if stats is not None:
        stats["column_stack_options"] = len(stack_options)
        stats["column_pair_pool"] = len(pair_pool)
        stats["column_pair_widths"] = sorted({int(stack["w"]) for stack in pair_pool})[:40]
    candidates = []
    seen_sheets = set()
    available_by_key = Counter(_piece_type_key(piece) for piece in remaining)
    remaining_sheet_lb = _area_lower_bound(remaining, sheet_w, sheet_h)
    if allow_expensive:
        candidate_limit = 750 if remaining_sheet_lb <= 14 else 520
    else:
        candidate_limit = 420 if remaining_sheet_lb <= 14 else 260

    def candidate_limit_hit():
        return len(candidates) >= int(candidate_limit)

    def build_sheet(columns):
        pools = {key: list(items) for key, items in groups.items()}
        used_counts = Counter()
        x = 0
        placements = []
        free = []
        for col in columns:
            y = 0
            for layer in col["layers"]:
                if layer.get("placements"):
                    for entry in sorted(
                        layer.get("placements") or [],
                        key=lambda item: (int(item.get("y") or 0), int(item.get("x") or 0), item["group"]["key"]),
                    ):
                        group = entry["group"]
                        key = group["key"]
                        count = int(entry.get("count") or 1)
                        if used_counts[key] + count > available_by_key[key]:
                            return None
                        chosen = pools[key][used_counts[key]:used_counts[key] + count]
                        used_counts[key] += count
                        lx = int(x) + int(entry.get("x") or 0)
                        ly = int(y) + int(entry.get("y") or 0)
                        for piece in chosen:
                            placements.append({
                                "item": int(piece["id"]),
                                "x": int(lx),
                                "y": int(ly),
                                "w": int(group["w"]),
                                "h": int(group["h"]),
                                "rotated": bool(group["rotated"]),
                            })
                            lx += int(group["w"]) + int(kerf)
                    y += int(layer["h"]) + int(kerf)
                    continue

                lx = x
                entries = layer.get("entries") or [{"group": layer["group"], "count": layer["count"]}]
                for entry in entries:
                    group = entry["group"]
                    key = group["key"]
                    count = int(entry["count"])
                    if used_counts[key] + count > available_by_key[key]:
                        return None
                    chosen = pools[key][used_counts[key]:used_counts[key] + count]
                    used_counts[key] += count
                    for piece in chosen:
                        placements.append({
                            "item": int(piece["id"]),
                            "x": int(lx),
                            "y": int(y),
                            "w": int(group["w"]),
                            "h": int(group["h"]),
                            "rotated": bool(group["rotated"]),
                        })
                        lx += int(group["w"]) + int(kerf)
                y += int(layer["h"]) + int(kerf)
            top_y = int(col["h"]) + int(kerf)
            if top_y < int(sheet_h):
                free.append((int(x), int(top_y), int(col["w"]), int(sheet_h - top_y)))
            x += int(col["w"]) + int(kerf)
        used_w = x - int(kerf) if columns else 0
        right_x = used_w + int(kerf)
        if right_x < int(sheet_w):
            free.append((int(right_x), 0, int(sheet_w - right_x), int(sheet_h)))
        sheet = _sheet_from_pattern(
            sheet_w,
            sheet_h,
            kerf,
            placements,
            free,
            "column_stack_mosaic",
        )
        if not _sheet_geometry_ok(sheet):
            return None
        return sheet

    def columns_width(columns):
        if not columns:
            return 0
        return sum(int(col["w"]) for col in columns) + int(kerf) * (len(columns) - 1)

    def columns_counts(columns):
        counts = Counter()
        for col in columns:
            counts.update(Counter(dict(col["signature"])))
        return counts

    def add_columns_candidate(columns):
        if candidate_limit_hit():
            return
        if len(columns) < 2 or columns_width(columns) > int(sheet_w):
            return
        counts = columns_counts(columns)
        if any(count > available_by_key[key] for key, count in counts.items()):
            return
        sheet = build_sheet(columns)
        if sheet is None:
            return
        signature = _sheet_mosaic_signature(sheet)
        if signature in seen_sheets:
            return
        seen_sheets.add(signature)
        cand = _candidate_from_sheets([sheet], "v2_column_stack_mosaic")
        _append_candidate_variants(candidates, cand, allow_mirror=False)

    early_narrow_stacks = [
        stack for stack in skinny_pair_pool[:(80 if allow_expensive else 32)]
        if int(stack["w"]) <= max(180, int(sheet_w) * 0.16)
    ]
    side_companion_pool = _stack_pool(
        side_repeat_pool[:(120 if allow_expensive else 48)],
        skinny_pair_pool[:(80 if allow_expensive else 32)],
        exact_by_width[:(100 if allow_expensive else 40)],
        limit=(220 if allow_expensive else 80),
    )
    side_companion_pool = [
        stack for stack in side_companion_pool
        if int(stack["w"]) <= int(sheet_w) * 0.38
    ]
    side_companion_pool.sort(key=lambda stack: (
        -exact_row_count(stack),
        -sum(count for _key, count in stack["signature"]),
        int(stack["w"]),
        -int(stack["area"]),
        tuple(stack["signature"]),
    ))

    def direct_stack_family(source_groups, *, wide=False, side=False, limit=160):
        direct = []
        for group in source_groups:
            if _deadline_hit(deadline, 0.005):
                break
            if wide:
                if not (
                    int(sheet_w) * 0.55 <= int(group["w"]) <= int(sheet_w) * 0.88
                    and int(group["qty"]) >= 2
                    and int(group["area"]) >= int(sheet_area) * 0.10
                ):
                    continue
            if side:
                if not (
                    int(group["w"]) <= int(sheet_w) * 0.38
                    and int(group["qty"]) >= 3
                ):
                    continue
            max_rows = min(
                int(group["qty"]),
                int((int(sheet_h) + int(kerf)) // (int(group["h"]) + int(kerf))),
            )
            if max_rows <= 0:
                continue
            row_counts = {max_rows, max(1, max_rows - 1)}
            if max_rows >= 3:
                row_counts.add(3)
            if side and max_rows >= 4:
                row_counts.add(4)
            for rows in sorted(row_counts, reverse=True):
                base = make_stack(group, rows, fill=False)
                if not base:
                    continue
                direct.append(base)
                for option in _cap_mosaic_options(base, limit=(70 if allow_expensive else 18)):
                    child = _append_cap_to_stack(base, option)
                    if child:
                        direct.append(child)
        direct = _stack_pool(direct, limit=None)
        direct.sort(key=lambda stack: (
            -exact_row_count(stack),
            int(sheet_w) - int(stack["w"]) if wide else int(stack["w"]),
            -int(stack["area"]),
            tuple(stack["signature"]),
            _layers_signature(stack["layers"]),
        ))
        if side:
            kept = []
            seen_direct = set()

            def stack_sig(stack):
                return (
                    int(stack["w"]),
                    tuple(stack["signature"]),
                    _layers_signature(stack["layers"]),
                )

            def keep(source, max_items):
                for stack in source:
                    sig = stack_sig(stack)
                    if sig in seen_direct:
                        continue
                    seen_direct.add(sig)
                    kept.append(stack)
                    if len(kept) >= max_items:
                        return

            keep(direct, max(1, int(limit) // 4))
            width_buckets = {}
            family_buckets = {}
            for stack in direct:
                width_buckets.setdefault(int(stack["w"]), []).append(stack)
                counts = Counter(dict(stack["signature"]))
                if counts:
                    family = max(counts, key=lambda key: int(key[0]) * int(key[1]) * int(counts[key]))
                    family_buckets.setdefault(family, []).append(stack)
            for width in sorted(width_buckets):
                bucket = sorted(width_buckets[width], key=lambda stack: (-exact_row_count(stack), -int(stack["area"]), tuple(stack["signature"])))
                keep(bucket[:60], int(limit))
            for family in sorted(family_buckets):
                bucket = sorted(family_buckets[family], key=lambda stack: (-exact_row_count(stack), -int(stack["area"]), int(stack["w"]), tuple(stack["signature"])))
                keep(bucket[:70], int(limit))
                secondary_buckets = {}
                for stack in bucket:
                    counts = Counter(dict(stack["signature"]))
                    counts.pop(family, None)
                    if not counts:
                        continue
                    secondary = max(counts, key=lambda key: int(key[0]) * int(key[1]) * int(counts[key]))
                    secondary_buckets.setdefault(secondary, []).append(stack)
                for secondary in sorted(secondary_buckets):
                    keep(sorted(secondary_buckets[secondary], key=lambda stack: (-exact_row_count(stack), -int(stack["area"]), int(stack["w"])))[:25], int(limit))
            return kept[:limit]
        return direct[:limit]

    direct_wide_stacks = direct_stack_family(
        oriented_groups,
        wide=True,
        limit=(180 if allow_expensive else 60),
    )
    direct_side_stacks = direct_stack_family(
        oriented_groups,
        side=True,
        limit=(900 if allow_expensive else 180),
    )
    for wide in direct_wide_stacks:
        if _deadline_hit(deadline, 0.02) or candidate_limit_hit():
            break
        remaining_lane_w = int(sheet_w) - int(wide["w"]) - int(kerf)
        compatible_sides = [
            side for side in direct_side_stacks
            if int(side["w"]) <= remaining_lane_w
        ]
        compatible_sides.sort(key=lambda side: (
            remaining_lane_w - int(side["w"]),
            -exact_row_count(side),
            -int(side["area"]),
            tuple(side["signature"]),
        ))
        for side in compatible_sides[:(70 if allow_expensive else 28)]:
            if _deadline_hit(deadline, 0.005) or candidate_limit_hit():
                break
            counts = columns_counts([wide, side])
            if any(count > available_by_key[key] for key, count in counts.items()):
                continue
            add_columns_candidate([wide, side])
            add_columns_candidate([side, wide])

    for wide in wide_companion_family_pool[:(280 if allow_expensive else 80)]:
        if _deadline_hit(deadline, 0.02) or candidate_limit_hit():
            break
        for side in side_companion_pool[:(140 if allow_expensive else 48)]:
            if _deadline_hit(deadline, 0.01) or candidate_limit_hit():
                break
            if columns_width([wide, side]) > int(sheet_w):
                continue
            counts = columns_counts([wide, side])
            if any(count > available_by_key[key] for key, count in counts.items()):
                continue
            add_columns_candidate([wide, side])
            add_columns_candidate([side, wide])

    for first in anchor_pool_for_pairs[:(90 if allow_expensive else 32)]:
        if _deadline_hit(deadline, 0.02) or candidate_limit_hit():
            break
        first_counts = Counter(dict(first["signature"]))
        for second in anchor_pool_for_pairs[:(140 if allow_expensive else 56)]:
            if _deadline_hit(deadline, 0.01) or candidate_limit_hit():
                break
            pair = [first, second]
            if columns_width(pair) > int(sheet_w):
                continue
            counts = columns_counts(pair)
            if any(count > available_by_key[key] for key, count in counts.items()):
                continue
            add_columns_candidate(pair)
            remaining_w = int(sheet_w) - columns_width(pair) - int(kerf)
            if remaining_w <= 0:
                continue
            compatible_narrows = [
                skinny for skinny in early_narrow_stacks
                if int(skinny["w"]) <= remaining_w
            ]
            compatible_narrows.sort(key=lambda skinny: (
                remaining_w - int(skinny["w"]),
                sum(count for _key, count in skinny["signature"]),
                -int(skinny["h"]),
                -int(skinny["area"]),
            ))
            for skinny in compatible_narrows[:16]:
                trio = [first, second, skinny]
                counts = columns_counts(trio)
                if any(count > available_by_key[key] for key, count in counts.items()):
                    continue
                add_columns_candidate(trio)

    for stack in anchor_pool_for_pairs[:(90 if allow_expensive else 32)]:
        if _deadline_hit(deadline, 0.02) or candidate_limit_hit():
            break
        stack_counts = Counter(dict(stack["signature"]))
        if not stack_counts:
            continue
        max_by_stock = min(
            int(available_by_key[key] // max(1, count))
            for key, count in stack_counts.items()
        )
        max_by_width = int((int(sheet_w) + int(kerf)) // (int(stack["w"]) + int(kerf)))
        for copies in range(min(max_by_stock, max_by_width, 4), 1, -1):
            columns = [stack] * copies
            if columns_width(columns) > int(sheet_w):
                continue
            add_columns_candidate(columns)
            remaining_w = int(sheet_w) - columns_width(columns) - int(kerf)
            if remaining_w <= 0:
                continue
            compatible_narrows = [
                skinny for skinny in early_narrow_stacks
                if int(skinny["w"]) <= remaining_w
            ]
            compatible_narrows.sort(key=lambda skinny: (
                remaining_w - int(skinny["w"]),
                sum(count for _key, count in skinny["signature"]),
                -int(skinny["h"]),
                -int(skinny["area"]),
            ))
            for skinny in compatible_narrows[:16]:
                counts = columns_counts(columns + [skinny])
                if any(count > available_by_key[key] for key, count in counts.items()):
                    continue
                add_columns_candidate(columns + [skinny])

    for first in anchor_pool_for_pairs[:(70 if allow_expensive else 24)]:
        if _deadline_hit(deadline, 0.02) or candidate_limit_hit():
            break
        columns = [first]
        used_counts = Counter(dict(first["signature"]))
        used_w = int(first["w"])
        while not _deadline_hit(deadline, 0.005) and not candidate_limit_hit():
            best = None
            for stack in anchor_pool_for_pairs:
                stack_counts = Counter(dict(stack["signature"]))
                if any(used_counts[key] + count > available_by_key[key] for key, count in stack_counts.items()):
                    continue
                next_w = used_w + int(kerf) + int(stack["w"])
                if next_w > int(sheet_w):
                    continue
                rank = (
                    -(sum(col["area"] for col in columns) + stack["area"]),
                    int(sheet_w) - next_w,
                    -sum(stack_counts.values()),
                    stack["w"],
                )
                if best is None or rank < best[0]:
                    best = (rank, stack, stack_counts, next_w)
            if best is None:
                break
            _rank, stack, stack_counts, used_w = best
            columns.append(stack)
            used_counts.update(stack_counts)
            if used_w >= int(sheet_w) * 0.96:
                break
        if len(columns) < 2:
            continue
        add_columns_candidate(columns)

    narrow_stacks = [
        stack for stack in skinny_pair_pool[:(80 if allow_expensive else 32)]
        if int(stack["w"]) <= max(180, int(sheet_w) * 0.16)
    ]

    for stack in anchor_pool_for_pairs:
        if _deadline_hit(deadline, 0.02) or candidate_limit_hit():
            break
        stack_counts = Counter(dict(stack["signature"]))
        if not stack_counts:
            continue
        max_by_stock = min(
            int(available_by_key[key] // max(1, count))
            for key, count in stack_counts.items()
        )
        max_by_width = int((int(sheet_w) + int(kerf)) // (int(stack["w"]) + int(kerf)))
        max_copies = min(max_by_stock, max_by_width, 4)
        if max_copies < 2:
            continue
        for copies in range(max_copies, 1, -1):
            columns = [stack] * copies
            if columns_width(columns) > int(sheet_w):
                continue
            add_columns_candidate(columns)
            remaining_w = int(sheet_w) - columns_width(columns) - int(kerf)
            if remaining_w <= 0:
                continue
            compatible_narrows = [
                skinny for skinny in narrow_stacks
                if int(skinny["w"]) <= remaining_w
            ]
            compatible_narrows.sort(key=lambda skinny: (
                remaining_w - int(skinny["w"]),
                sum(count for _key, count in skinny["signature"]),
                -int(skinny["h"]),
                -int(skinny["area"]),
            ))
            for skinny in compatible_narrows[:24]:
                counts = columns_counts(columns + [skinny])
                if any(count > available_by_key[key] for key, count in counts.items()):
                    continue
                add_columns_candidate(columns + [skinny])

    for first in anchor_pool_for_pairs[:(120 if allow_expensive else 36)]:
        if _deadline_hit(deadline, 0.02) or candidate_limit_hit():
            break
        for second in anchor_pool_for_pairs[:(180 if allow_expensive else 72)]:
            if _deadline_hit(deadline, 0.01) or candidate_limit_hit():
                break
            pair = [first, second]
            if columns_width(pair) > int(sheet_w):
                continue
            counts = columns_counts(pair)
            if any(count > available_by_key[key] for key, count in counts.items()):
                continue
            add_columns_candidate(pair)
            remaining_w = int(sheet_w) - columns_width(pair) - int(kerf)
            if remaining_w <= 0:
                continue
            compatible_narrows = [
                stack for stack in narrow_stacks
                if int(stack["w"]) <= remaining_w
            ]
            compatible_narrows.sort(key=lambda stack: (
                remaining_w - int(stack["w"]),
                sum(count for _key, count in stack["signature"]),
                -int(stack["h"]),
                -int(stack["area"]),
            ))
            for third in compatible_narrows[:32]:
                trio = [first, second, third]
                counts = columns_counts(trio)
                if any(count > available_by_key[key] for key, count in counts.items()):
                    continue
                add_columns_candidate(trio)

    _record_strategy_metric(stats, "v2_column_stack_mosaic_candidates", _elapsed_ms(started_at), len(candidates))
    return candidates


def _v2_transposed_column_stack_candidates(remaining, sheet_w, sheet_h, kerf, *, stats=None, deadline=None, allow_expensive=False):
    """Search the column-stack family in the quarter-turned sheet frame."""
    started_at = time.monotonic()
    if int(sheet_w) == int(sheet_h):
        return []
    pieces_by_id = {int(piece["id"]): piece for piece in remaining}
    candidates = []
    seen = set()

    def consume(raw):
        for cand in raw:
            sheets = cand.get("sheets") or []
            if len(sheets) != 1:
                continue
            rotated = _rotate_sheet_clockwise(
                sheets[0],
                int(sheet_w),
                int(sheet_h),
                pieces_by_id=pieces_by_id,
                suffix="_long_axis",
            )
            if not _sheet_geometry_ok(rotated):
                continue
            signature = _sheet_mosaic_signature(rotated)
            if signature in seen:
                continue
            seen.add(signature)
            new_cand = _candidate_from_sheets([rotated], "v2_column_stack_mosaic_long_axis")
            _append_candidate_variants(candidates, new_cand, allow_mirror=False)

    consume(_v2_column_stack_candidates(
        remaining,
        int(sheet_h),
        int(sheet_w),
        kerf,
        stats=None,
        deadline=deadline,
        allow_expensive=False,
    ))
    if allow_expensive and not _deadline_hit(deadline, 0.02):
        consume(_v2_column_stack_candidates(
            remaining,
            int(sheet_h),
            int(sheet_w),
            kerf,
            stats=None,
            deadline=deadline,
            allow_expensive=True,
        ))
    _record_strategy_metric(stats, "v2_column_stack_mosaic_long_axis_candidates", _elapsed_ms(started_at), len(candidates))
    return candidates


def _v2_lane_run_candidates(remaining, sheet_w, sheet_h, kerf, *, stats=None, deadline=None, allow_expensive=False):
    """Build sheets from independent guillotine lanes.

    This is the reusable version of the pattern CutList often finds on mixed
    repeat jobs: rip the sheet into a few vertical lanes, fill each lane with a
    dominant repeated run, then use the cap of each lane for smaller rows.
    """
    started_at = time.monotonic()
    if len(remaining) < 4:
        return []

    groups = {}
    for piece in remaining:
        groups.setdefault(_piece_type_key(piece), []).append(piece)
    for items in groups.values():
        items.sort(key=lambda p: int(p["id"]))

    available_by_key = Counter(_piece_type_key(piece) for piece in remaining)
    sheet_area = max(1, int(sheet_w) * int(sheet_h))
    oriented = []
    seen_oriented = set()
    for key, items in sorted(groups.items()):
        sample = items[0]
        for opt in _piece_orientations(sample, sheet_w, sheet_h):
            sig = (key, int(opt["w"]), int(opt["h"]))
            if sig in seen_oriented:
                continue
            seen_oriented.add(sig)
            oriented.append({
                "key": key,
                "items": items,
                "w": int(opt["w"]),
                "h": int(opt["h"]),
                "rotated": bool(opt["rotated"]),
                "qty": len(items),
                "area": int(key[0]) * int(key[1]),
            })
    if not oriented:
        return []

    oriented.sort(key=lambda g: (-min(int(g["qty"]), 24) * int(g["area"]), int(g["w"]), int(g["h"]), g["key"]))
    by_width = {}
    for group in oriented:
        by_width.setdefault(int(group["w"]), []).append(group)

    def row_entries_signature(entries):
        return tuple(
            (
                entry["group"]["key"],
                int(entry["group"]["w"]),
                int(entry["group"]["h"]),
                int(entry["count"]),
            )
            for entry in entries
        )

    def layer_signature(layer):
        return (
            int(layer["row_w"]),
            int(layer["h"]),
            row_entries_signature(layer["entries"]),
        )

    def state_signature(state):
        return (
            int(state["w"]),
            int(state["h"]),
            tuple(sorted(state["counts"].items())),
            tuple(layer_signature(layer) for layer in state["layers"]),
        )

    def state_rank(state):
        exact_rows = sum(1 for layer in state["layers"] if int(layer["row_w"]) >= int(state["w"]) - 5)
        dominant = max(
            (
                int(key[0]) * int(key[1]) * int(count)
                for key, count in state["counts"].items()
            ),
            default=0,
        )
        wide_lane_strip_bias = 0
        if int(state["w"]) > max(220, int(sheet_w) * 0.18):
            strip_count = 0
            strip_area = 0
            for key, count in state["counts"].items():
                short = min(int(key[0]), int(key[1]))
                long = max(int(key[0]), int(key[1]))
                if short <= 180 and long >= int(sheet_h) * 0.28:
                    strip_count += int(count)
                    strip_area += int(key[0]) * int(key[1]) * int(count)
            if strip_count:
                # A sparse wide lane made from long strips burns useful filler.
                # A dense repeated strip stack is the opposite: it is exactly the
                # CutList-style "crosscut first" lane that reduces saw work.
                if strip_count >= 4 and strip_area >= int(state["area"]) * 0.55:
                    wide_lane_strip_bias = -min(24, strip_count)
                else:
                    wide_lane_strip_bias = strip_count
        return (
            int(wide_lane_strip_bias),
            -int(state["area"]),
            int(sheet_h) - int(state["h"]),
            -exact_rows,
            -dominant,
            int(state["w"]),
            tuple(sorted(state["counts"].items())),
        )

    def select_states(states, limit):
        states = sorted(states, key=state_rank)
        kept = []
        seen = set()

        def keep(source, max_items):
            for state in source:
                sig = state_signature(state)
                if sig in seen:
                    continue
                seen.add(sig)
                kept.append(state)
                if len(kept) >= max_items:
                    return

        keep(states, max(1, int(limit) // 2))
        width_buckets = {}
        family_buckets = {}
        for state in states:
            width_buckets.setdefault(int(state["w"]), []).append(state)
            dominant_family = None
            if state["counts"]:
                dominant_family = max(
                    state["counts"],
                    key=lambda key: int(key[0]) * int(key[1]) * int(state["counts"][key]),
                )
            for family in state["counts"]:
                family_buckets.setdefault((int(state["w"]), "has", family), []).append(state)
                family_buckets.setdefault((
                    int(state["w"]),
                    "count",
                    family,
                    int(state["counts"][family]),
                ), []).append(state)
                if dominant_family and family != dominant_family:
                    family_buckets.setdefault((
                        int(state["w"]),
                        "dom_secondary",
                        dominant_family,
                        int(state["counts"][dominant_family]),
                        family,
                    ), []).append(state)
        for width in sorted(width_buckets):
            keep(sorted(width_buckets[width], key=state_rank)[:6], int(limit))
        for family in sorted(family_buckets):
            keep(sorted(family_buckets[family], key=state_rank)[:6], int(limit))
        keep(sorted(states, key=lambda s: (int(sheet_h) - int(s["h"]), -int(s["area"]), int(s["w"]))), int(limit))
        return kept[:limit]

    def append_layer(state, entries):
        if not entries:
            return None
        row_counts = Counter()
        panel_count = 0
        row_w = 0
        row_h = 0
        row_area = 0
        for entry in entries:
            group = entry["group"]
            count = int(entry["count"])
            if count <= 0:
                return None
            row_counts[group["key"]] += count
            panel_count += count
            row_w += int(group["w"]) * count
            row_h = max(row_h, int(group["h"]))
            row_area += int(group["w"]) * int(group["h"]) * count
        row_w += int(kerf) * max(0, panel_count - 1)
        if row_w > int(state["w"]) or row_h <= 0:
            return None
        counts = Counter(state["counts"])
        counts.update(row_counts)
        if any(int(counts[key]) > int(available_by_key[key]) for key in counts):
            return None
        next_h = int(state["h"]) + (int(kerf) if state["layers"] else 0) + int(row_h)
        if next_h > int(sheet_h):
            return None
        layers = list(state["layers"])
        layers.append({
            "entries": [{"group": e["group"], "count": int(e["count"])} for e in entries],
            "row_w": int(row_w),
            "h": int(row_h),
        })
        return {
            "w": int(state["w"]),
            "h": int(next_h),
            "area": int(state["area"]) + int(row_area),
            "counts": counts,
            "layers": layers,
        }

    def append_run(state, group, count):
        child = state
        for _idx in range(int(count)):
            child = append_layer(child, [{"group": group, "count": 1}])
            if child is None:
                return None
        return child

    def vertical_counts(group, remaining_count, remaining_h, *, seed=False):
        per = int(group["h"]) + int(kerf)
        cap = int((int(remaining_h) + int(kerf)) // max(1, per))
        cap = min(cap, int(remaining_count))
        if cap <= 0:
            return []
        counts = {cap, 1}
        for delta in (1, 2, 3):
            if cap - delta > 0:
                counts.add(cap - delta)
        for fixed in (2, 3, 4, 5, 6, 8, 10, 12, 14, 17, 21):
            if fixed <= cap:
                counts.add(fixed)
        if not seed and cap >= 7:
            counts.add(max(1, cap // 2))
        return sorted(counts, reverse=True)

    def row_options(width_limit, height_limit, used_counts, limit):
        width_limit = int(width_limit)
        height_limit = int(height_limit)
        if width_limit <= 0 or height_limit <= 0:
            return []
        candidates = []
        row_pool = [
            group for group in oriented
            if int(group["w"]) <= width_limit
            and int(group["h"]) <= height_limit
            and int(used_counts[group["key"]]) < int(available_by_key[group["key"]])
        ]
        row_pool.sort(key=lambda g: (-int(g["area"]), int(g["w"]), int(g["h"]), g["key"]))

        def add(entries):
            counts = Counter()
            panels = 0
            row_w = 0
            row_h = 0
            row_area = 0
            for group, count in entries:
                count = int(count)
                counts[group["key"]] += count
                panels += count
                row_w += int(group["w"]) * count
                row_h = max(row_h, int(group["h"]))
                row_area += int(group["w"]) * int(group["h"]) * count
            row_w += int(kerf) * max(0, panels - 1)
            if row_w > width_limit or row_h > height_limit:
                return
            if any(int(used_counts[key]) + int(count) > int(available_by_key[key]) for key, count in counts.items()):
                return
            width_waste = width_limit - row_w
            strip_spend = 0
            if width_limit > max(220, int(sheet_w) * 0.18):
                for group, count in entries:
                    short = min(int(group["w"]), int(group["h"]))
                    long = max(int(group["w"]), int(group["h"]))
                    if short <= 180 and long >= int(sheet_h) * 0.28:
                        strip_spend += int(count)
            rank = (
                0 if width_waste <= 5 else 1 + width_waste // 50,
                int(strip_spend),
                -int(row_area),
                width_waste,
                int(row_h),
                -int(panels),
                tuple(sorted(counts.items())),
            )
            candidates.append((rank, [
                {"group": group, "count": int(count)}
                for group, count in entries
            ]))

        pool_limit = 42 if allow_expensive else 22
        for group in row_pool[:pool_limit]:
            remaining_count = int(available_by_key[group["key"]]) - int(used_counts[group["key"]])
            cap = min(
                remaining_count,
                int((width_limit + int(kerf)) // (int(group["w"]) + int(kerf))),
            )
            if cap <= 0:
                continue
            choices = {1, cap}
            for fixed in (2, 3, 4, 5, 6):
                if fixed <= cap:
                    choices.add(fixed)
            if cap > 1:
                choices.add(cap - 1)
            for count in sorted(choices, reverse=True):
                add([(group, count)])

        combo_pool = row_pool[:(32 if allow_expensive else 16)]
        for left, right in combinations(combo_pool, 2):
            add([(left, 1), (right, 1)])
            if int(available_by_key[left["key"]]) - int(used_counts[left["key"]]) >= 2:
                add([(left, 2), (right, 1)])
            if int(available_by_key[right["key"]]) - int(used_counts[right["key"]]) >= 2:
                add([(left, 1), (right, 2)])

        for a, b, c in combinations(combo_pool[:18], 3):
            if _deadline_hit(deadline, 0.005):
                break
            add([(a, 1), (b, 1), (c, 1)])

        candidates.sort(key=lambda item: item[0])
        kept = []
        seen = set()
        for _rank, entries in candidates:
            sig = row_entries_signature(entries)
            if sig in seen:
                continue
            seen.add(sig)
            kept.append(entries)
            if len(kept) >= int(limit):
                break
        return kept

    def build_columns():
        columns = []
        width_rank = []
        for width, width_groups in by_width.items():
            best_qty_area = max(int(group["qty"]) * int(group["area"]) for group in width_groups)
            width_rank.append((-best_qty_area, int(width)))
        width_limit = 40 if allow_expensive else 22
        for _rank, col_w in sorted(width_rank)[:width_limit]:
            if _deadline_hit(deadline, 0.02):
                break
            base = {"w": int(col_w), "h": 0, "area": 0, "counts": Counter(), "layers": []}
            exact_groups = sorted(
                by_width.get(col_w) or [],
                key=lambda g: (-int(g["area"]) * min(int(g["qty"]), 24), int(g["h"]), g["key"]),
            )
            states = []
            for group in exact_groups[:(10 if allow_expensive else 6)]:
                if _deadline_hit(deadline, 0.005):
                    break
                for count in vertical_counts(group, int(group["qty"]), int(sheet_h), seed=True):
                    child = append_run(base, group, count)
                    if child:
                        states.append(child)
            if not states:
                continue
            states = select_states(states, 42 if allow_expensive else 18)
            results = list(states)
            seen_states = {state_signature(state) for state in results}
            for _depth in range(8 if allow_expensive else 4):
                if _deadline_hit(deadline, 0.01):
                    break
                next_states = []
                for state in states:
                    if _deadline_hit(deadline, 0.005):
                        break
                    gap = int(kerf) if state["layers"] else 0
                    remaining_h = int(sheet_h) - int(state["h"]) - gap
                    if remaining_h <= 0:
                        continue
                    # Another exact-width run keeps lanes clean and captures
                    # stacks like 640x920 followed by 640x330.
                    for group in exact_groups[:(10 if allow_expensive else 6)]:
                        if int(state["counts"][group["key"]]) >= int(available_by_key[group["key"]]):
                            continue
                        remaining_count = int(available_by_key[group["key"]]) - int(state["counts"][group["key"]])
                        for count in vertical_counts(group, remaining_count, remaining_h):
                            child = append_run(state, group, count)
                            if not child:
                                continue
                            sig = state_signature(child)
                            if sig in seen_states:
                                continue
                            seen_states.add(sig)
                            results.append(child)
                            next_states.append(child)
                    # Cap rows let smaller parts act as packing sand inside the
                    # lane without changing the outer guillotine strip.
                    for entries in row_options(
                        int(col_w),
                        remaining_h,
                        state["counts"],
                        44 if allow_expensive else 16,
                    ):
                        child = append_layer(state, entries)
                        if not child:
                            continue
                        sig = state_signature(child)
                        if sig in seen_states:
                            continue
                        seen_states.add(sig)
                        results.append(child)
                        next_states.append(child)
                if not next_states:
                    break
                states = select_states(next_states, 96 if allow_expensive else 28)
            columns.extend(select_states(results, 96 if allow_expensive else 24))
        return select_states(columns, 1400 if allow_expensive else 260)

    # The direct lane-combo pass below is the CutList-style kernel for this
    # family. The older broad column pool is covered by
    # _v2_column_stack_candidates, and building it here can exhaust the deadline
    # before the direct width recipes run.
    columns = []

    def build_direct_exact_run_columns():
        direct = []
        width_rank = []
        for width, width_groups in by_width.items():
            best_qty_area = max(int(group["qty"]) * int(group["area"]) for group in width_groups)
            width_rank.append((-best_qty_area, int(width)))
        for _rank, col_w in sorted(width_rank)[:(44 if allow_expensive else 24)]:
            if _deadline_hit(deadline, 0.02):
                break
            exact_groups = sorted(
                by_width.get(col_w) or [],
                key=lambda g: (-int(g["area"]) * min(int(g["qty"]), 24), int(g["h"]), g["key"]),
            )
            for group in exact_groups[:(12 if allow_expensive else 6)]:
                if _deadline_hit(deadline, 0.005):
                    break
                base = {"w": int(col_w), "h": 0, "area": 0, "counts": Counter(), "layers": []}
                for count in vertical_counts(group, int(group["qty"]), int(sheet_h), seed=True):
                    state = append_run(base, group, count)
                    if not state:
                        continue
                    states = [state]
                    direct.append(state)
                    for _depth in range(7 if allow_expensive else 3):
                        next_states = []
                        for cur in states:
                            gap = int(kerf) if cur["layers"] else 0
                            remaining_h = int(sheet_h) - int(cur["h"]) - gap
                            if remaining_h <= 0:
                                continue
                            for entries in row_options(
                                int(col_w),
                                remaining_h,
                                cur["counts"],
                                36 if allow_expensive else 12,
                            ):
                                child = append_layer(cur, entries)
                                if not child:
                                    continue
                                direct.append(child)
                                next_states.append(child)
                        if not next_states:
                            break
                        states = select_states(next_states, 24 if allow_expensive else 8)
        return select_states(direct, 5000 if allow_expensive else 500)

    # Lane variants are now generated lazily per width by direct_lane_variants().
    # Prebuilding every possible direct column here is too expensive for large
    # mixed jobs and can consume the whole preview budget before any sheet
    # candidates are emitted.

    pools = {key: list(items) for key, items in groups.items()}

    def columns_width(cols):
        return sum(int(col["w"]) for col in cols) + int(kerf) * max(0, len(cols) - 1)

    def columns_counts(cols):
        counts = Counter()
        for col in cols:
            counts.update(col["counts"])
        return counts

    def build_sheet(cols):
        used_counts = Counter()
        placements = []
        free = []
        x = 0
        for col in cols:
            y = 0
            for layer in col["layers"]:
                lx = x
                for entry in layer["entries"]:
                    group = entry["group"]
                    count = int(entry["count"])
                    key = group["key"]
                    if used_counts[key] + count > available_by_key[key]:
                        return None
                    chosen = pools[key][used_counts[key]:used_counts[key] + count]
                    used_counts[key] += count
                    for piece in chosen:
                        placements.append({
                            "item": int(piece["id"]),
                            "x": int(lx),
                            "y": int(y),
                            "w": int(group["w"]),
                            "h": int(group["h"]),
                            "rotated": bool(group["rotated"]),
                        })
                        lx += int(group["w"]) + int(kerf)
                y += int(layer["h"]) + int(kerf)
            top_y = int(col["h"]) + int(kerf)
            if top_y < int(sheet_h):
                free.append((int(x), int(top_y), int(col["w"]), int(sheet_h) - int(top_y)))
            x += int(col["w"]) + int(kerf)
        used_w = int(columns_width(cols))
        right_x = used_w + int(kerf)
        if right_x < int(sheet_w):
            free.append((right_x, 0, int(sheet_w) - right_x, int(sheet_h)))
        sheet = _sheet_from_pattern(
            sheet_w,
            sheet_h,
            kerf,
            placements,
            free,
            "lane_run_mosaic",
        )
        if not _sheet_geometry_ok(sheet):
            return None
        return sheet

    candidates = []
    seen_sheets = set()
    candidate_limit = 10000 if allow_expensive else 1400

    def add_candidate(cols):
        if len(candidates) >= candidate_limit:
            return
        if len(cols) < 1 or columns_width(cols) > int(sheet_w):
            return
        if len(cols) == 1 and not (
            int(cols[0]["w"]) >= int(sheet_w) * 0.88
            or int(cols[0]["area"]) >= int(sheet_area) * 0.62
        ):
            return
        counts = columns_counts(cols)
        if any(int(count) > int(available_by_key[key]) for key, count in counts.items()):
            return
        sheet = build_sheet(cols)
        if sheet is None:
            return
        signature = _sheet_mosaic_signature(sheet)
        if signature in seen_sheets:
            return
        seen_sheets.add(signature)
        cand = _candidate_from_sheets([sheet], "v2_lane_run_mosaic")
        _append_candidate_variants(candidates, cand, allow_mirror=False)

    sorted_columns = sorted(columns, key=state_rank)
    columns_by_width = {}
    width_sources = {}
    for col in sorted_columns:
        width_sources.setdefault(int(col["w"]), []).append(col)

    def _column_sig(col):
        return state_signature(col)

    for width, source in width_sources.items():
        bucket = []
        seen_bucket = set()

        def keep(source_cols, max_items):
            for col in source_cols:
                sig = _column_sig(col)
                if sig in seen_bucket:
                    continue
                seen_bucket.add(sig)
                bucket.append(col)
                if len(bucket) >= max_items:
                    return

        limit = 72 if allow_expensive else 20
        keep(source, max(1, limit // 3))
        family_sources = {}
        for col in source:
            dominant_family = None
            if col["counts"]:
                dominant_family = max(
                    col["counts"],
                    key=lambda key: int(key[0]) * int(key[1]) * int(col["counts"][key]),
                )
            for family in col["counts"]:
                family_sources.setdefault(("has", family), []).append(col)
                family_sources.setdefault(("count", family, int(col["counts"][family])), []).append(col)
                if dominant_family and family != dominant_family:
                    family_sources.setdefault((
                        "dom_secondary",
                        dominant_family,
                        int(col["counts"][dominant_family]),
                        family,
                    ), []).append(col)
        for family in sorted(family_sources):
            keep(sorted(family_sources[family], key=state_rank)[:8 if allow_expensive else 3], limit)
        keep(sorted(source, key=lambda col: (int(sheet_h) - int(col["h"]), -int(col["area"]), tuple(sorted(col["counts"].items())))), limit)
        columns_by_width[int(width)] = bucket[:limit]

    direct_lane_cache = {}

    def direct_lane_variants(width):
        width = int(width)
        if width in direct_lane_cache:
            return direct_lane_cache[width]
        exact_groups = sorted(
            by_width.get(width) or [],
            key=lambda g: (-int(g["area"]) * min(int(g["qty"]), 24), int(g["h"]), g["key"]),
        )
        variants = []
        seen = set()

        def add_state(state):
            sig = state_signature(state)
            if sig in seen:
                return
            seen.add(sig)
            variants.append(state)

        for group in exact_groups[:(10 if allow_expensive else 5)]:
            if _deadline_hit(deadline, 0.005):
                break
            base = {"w": width, "h": 0, "area": 0, "counts": Counter(), "layers": []}
            for count in vertical_counts(group, int(group["qty"]), int(sheet_h), seed=True):
                state = append_run(base, group, count)
                if not state:
                    continue
                add_state(state)
                first_options = row_options(
                    width,
                    int(sheet_h) - int(state["h"]) - int(kerf),
                    state["counts"],
                    18 if allow_expensive else 8,
                )
                for entries in first_options:
                    child = append_layer(state, entries)
                    if not child:
                        continue
                    add_state(child)
                    # Also keep the natural greedy continuation from this cap
                    # choice. It captures filler tails like 740-strip columns
                    # followed by rows of 270x160 blocks.
                    cur = child
                    for _depth in range(6 if allow_expensive else 2):
                        gap = int(kerf) if cur["layers"] else 0
                        remaining_h = int(sheet_h) - int(cur["h"]) - gap
                        if remaining_h <= 0:
                            break
                        opts = row_options(
                            width,
                            remaining_h,
                            cur["counts"],
                            8 if allow_expensive else 4,
                        )
                        if not opts:
                            break
                        nxt = append_layer(cur, opts[0])
                        if not nxt:
                            break
                        add_state(nxt)
                        cur = nxt
        direct_lane_cache[width] = select_states(variants, 80 if allow_expensive else 24)
        return direct_lane_cache[width]

    # First try width-complement sets that use the sheet width cleanly. This is
    # where CutList's mixed stress layouts mostly live: two or three lane
    # recipes whose widths nearly add up to the stock width.
    widths = sorted(set(columns_by_width) | set(by_width))
    near_slack = max(35, int(sheet_w) // 18)
    width_best_area = {
        int(width): max((int(col["area"]) for col in cols), default=0)
        for width, cols in columns_by_width.items()
    }
    for width, width_groups in by_width.items():
        if int(width) in width_best_area:
            continue
        width_best_area[int(width)] = max(
            (
                min(
                    int(group["qty"]),
                    int((int(sheet_h) + int(kerf)) // (int(group["h"]) + int(kerf))),
                ) * int(group["area"])
                for group in width_groups
            ),
            default=0,
        )
    direct_combos = []
    for idx_a, wa in enumerate(widths):
        total_w = int(wa)
        if total_w <= int(sheet_w) and int(sheet_w) - total_w <= near_slack:
            area_hint = int(width_best_area.get(int(wa)) or 0)
            direct_combos.append((-area_hint, int(sheet_w) - total_w, (wa,)))
        for idx_b, wb in enumerate(widths[idx_a:], idx_a):
            total_w = int(wa) + int(wb) + int(kerf)
            if total_w <= int(sheet_w) and int(sheet_w) - total_w <= near_slack:
                area_hint = int(width_best_area.get(int(wa)) or 0) + int(width_best_area.get(int(wb)) or 0)
                direct_combos.append((-area_hint, int(sheet_w) - total_w, (wa, wb)))
            for wc in widths[idx_b:]:
                total_w = int(wa) + int(wb) + int(wc) + int(kerf) * 2
                if total_w <= int(sheet_w) and int(sheet_w) - total_w <= near_slack:
                    area_hint = (
                        int(width_best_area.get(int(wa)) or 0)
                        + int(width_best_area.get(int(wb)) or 0)
                        + int(width_best_area.get(int(wc)) or 0)
                    )
                    direct_combos.append((-area_hint, int(sheet_w) - total_w, (wa, wb, wc)))
    direct_combos.sort(key=lambda item: (
        0 if len(item[2]) == 2 else 1 if len(item[2]) == 3 else 2,
        item[0],
        item[1],
        item[2],
    ))
    for _area_hint, _slack, combo in direct_combos[:(1200 if allow_expensive else 120)]:
        if _deadline_hit(deadline, 0.02) or len(candidates) >= candidate_limit:
            break
        pools_for_combo = [direct_lane_variants(width)[:(24 if allow_expensive else 7)] for width in combo]
        if any(not pool for pool in pools_for_combo):
            continue
        combo_attempts = 0
        for cols in product(*pools_for_combo):
            if _deadline_hit(deadline, 0.005) or len(candidates) >= candidate_limit:
                break
            combo_attempts += 1
            if combo_attempts > (700 if allow_expensive else 120):
                break
            add_candidate(list(cols))
            if len(cols) > 1:
                add_candidate(list(reversed(cols)))

    legacy_widths = sorted(columns_by_width)
    for idx_a, wa in enumerate(legacy_widths):
        if _deadline_hit(deadline, 0.02) or len(candidates) >= candidate_limit:
            break
        for wb in legacy_widths[idx_a:]:
            if _deadline_hit(deadline, 0.01) or len(candidates) >= candidate_limit:
                break
            used_w = int(wa) + int(wb) + int(kerf)
            if used_w <= int(sheet_w) and int(sheet_w) - used_w <= near_slack:
                for left in columns_by_width[wa][:18 if allow_expensive else 6]:
                    for right in columns_by_width[wb][:18 if allow_expensive else 6]:
                        add_candidate([left, right])
                        if wa != wb:
                            add_candidate([right, left])
                        if len(candidates) >= candidate_limit:
                            break
                    if len(candidates) >= candidate_limit:
                        break
            for wc in legacy_widths:
                total_w = int(wa) + int(wb) + int(wc) + int(kerf) * 2
                if total_w > int(sheet_w):
                    continue
                if int(sheet_w) - total_w > near_slack:
                    continue
                for first in columns_by_width[wa][:12 if allow_expensive else 4]:
                    if len(candidates) >= candidate_limit:
                        break
                    for second in columns_by_width[wb][:12 if allow_expensive else 4]:
                        if len(candidates) >= candidate_limit:
                            break
                        for third in columns_by_width[wc][:12 if allow_expensive else 4]:
                            cols = [first, second, third]
                            add_candidate(cols)
                            add_candidate(list(reversed(cols)))
                            if len(candidates) >= candidate_limit:
                                break

    for col in sorted_columns[:(260 if allow_expensive else 70)]:
        if _deadline_hit(deadline, 0.01) or len(candidates) >= candidate_limit:
            break
        add_candidate([col])

    pair_pool = sorted_columns[:(420 if allow_expensive else 140)]
    for left in pair_pool:
        if _deadline_hit(deadline, 0.02) or len(candidates) >= candidate_limit:
            break
        for right in pair_pool:
            if _deadline_hit(deadline, 0.005) or len(candidates) >= candidate_limit:
                break
            cols = [left, right]
            if columns_width(cols) <= int(sheet_w):
                add_candidate(cols)
                add_candidate([right, left])

    if len(candidates) < candidate_limit and not _deadline_hit(deadline, 0.02):
        triple_pool = sorted_columns[:(320 if allow_expensive else 100)]
        width_buckets = {}
        for col in triple_pool:
            width_buckets.setdefault(int(col["w"]), []).append(col)
        third_pool = sorted_columns[:(380 if allow_expensive else 120)]
        for first in triple_pool:
            if _deadline_hit(deadline, 0.02) or len(candidates) >= candidate_limit:
                break
            for second in triple_pool:
                if _deadline_hit(deadline, 0.01) or len(candidates) >= candidate_limit:
                    break
                used_w = int(first["w"]) + int(second["w"]) + int(kerf)
                remaining_w = int(sheet_w) - used_w - int(kerf)
                if remaining_w <= 0:
                    continue
                compatible = [
                    third for third in third_pool
                    if int(third["w"]) <= remaining_w
                ]
                compatible.sort(key=lambda col: (
                    remaining_w - int(col["w"]),
                    -int(col["area"]),
                    tuple(sorted(col["counts"].items())),
                ))
                for third in compatible[:(20 if allow_expensive else 8)]:
                    cols = [first, second, third]
                    add_candidate(cols)
                    add_candidate(list(reversed(cols)))
                    if len(candidates) >= candidate_limit:
                        break

    _record_strategy_metric(stats, "v2_lane_run_candidates", _elapsed_ms(started_at), len(candidates))
    return candidates


def _v2_transposed_lane_run_candidates(remaining, sheet_w, sheet_h, kerf, *, stats=None, deadline=None, allow_expensive=False):
    """Search lane-run candidates in the quarter-turned sheet frame."""
    started_at = time.monotonic()
    if int(sheet_w) == int(sheet_h):
        return []
    pieces_by_id = {int(piece["id"]): piece for piece in remaining}
    candidates = []
    seen = set()
    raw = _v2_lane_run_candidates(
        remaining,
        int(sheet_h),
        int(sheet_w),
        kerf,
        stats=None,
        deadline=deadline,
        allow_expensive=allow_expensive,
    )
    for cand in raw:
        sheets = cand.get("sheets") or []
        if len(sheets) != 1:
            continue
        rotated = _rotate_sheet_clockwise(
            sheets[0],
            int(sheet_w),
            int(sheet_h),
            pieces_by_id=pieces_by_id,
            suffix="_long_axis",
        )
        if not _sheet_geometry_ok(rotated):
            continue
        signature = _sheet_mosaic_signature(rotated)
        if signature in seen:
            continue
        seen.add(signature)
        new_cand = _candidate_from_sheets([rotated], "v2_lane_run_mosaic_long_axis")
        _append_candidate_variants(candidates, new_cand, allow_mirror=False)
    _record_strategy_metric(stats, "v2_lane_run_long_axis_candidates", _elapsed_ms(started_at), len(candidates))
    return candidates


def _v2_exact_lane_partition_candidates(remaining, sheet_w, sheet_h, kerf, *, stats=None, deadline=None, allow_expensive=False):
    """Exact-cover search over guillotine lane partitions.

    This sits between the broad lane generators and the global beam. It builds
    candidate lanes from repeated vertical runs; the cap above a run may itself be
    split into a few smaller vertical sub-lanes. A sheet candidate is then a small
    exact-cover combination of those lanes. The search is count/dimension driven,
    so it applies to any stock size and does not know about benchmark labels.
    """
    started_at = time.monotonic()
    if len(remaining or []) < 6:
        return []

    groups = {}
    for piece in remaining:
        groups.setdefault(_piece_type_key(piece), []).append(piece)
    for items in groups.values():
        items.sort(key=lambda p: int(p["id"]))
    available = Counter({key: len(items) for key, items in groups.items()})
    sheet_area = max(1, int(sheet_w) * int(sheet_h))

    oriented = []
    seen_oriented = set()
    for key, items in sorted(groups.items()):
        sample = items[0]
        for opt in _piece_orientations(sample, sheet_w, sheet_h):
            sig = (key, int(opt["w"]), int(opt["h"]))
            if sig in seen_oriented:
                continue
            seen_oriented.add(sig)
            oriented.append({
                "key": key,
                "items": items,
                "w": int(opt["w"]),
                "h": int(opt["h"]),
                "rotated": bool(opt["rotated"]),
                "area": int(key[0]) * int(key[1]),
                "qty": len(items),
            })
    if not oriented:
        return []

    oriented.sort(key=lambda group: (
        -min(int(group["qty"]), 16) * int(group["area"]),
        int(group["w"]),
        int(group["h"]),
        group["key"],
    ))

    def count_choices(max_count, *, exhaustive=False):
        max_count = int(max_count)
        if max_count <= 0:
            return []
        choices = {max_count, 1}
        for value in (
            2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 14, 15, 17, 21,
            max_count - 1, max_count - 2, max_count - 3,
        ):
            if 1 <= int(value) <= max_count:
                choices.add(int(value))
        if exhaustive and max_count <= 14:
            choices.update(range(1, max_count + 1))
        return sorted(choices, reverse=True)

    def make_run(group, count, *, x=0, y=0):
        placements = []
        cur_y = int(y)
        for _idx in range(int(count)):
            placements.append({
                "group": group,
                "x": int(x),
                "y": int(cur_y),
            })
            cur_y += int(group["h"]) + int(kerf)
        used_h = cur_y - int(kerf) if count else 0
        return placements, int(used_h)

    def lane_sig(lane):
        return (
            int(lane["w"]),
            int(lane["h"]),
            tuple(sorted(lane["counts"].items())),
            tuple(
                (
                    pl["group"]["key"],
                    int(pl["group"]["w"]),
                    int(pl["group"]["h"]),
                    int(pl["x"]),
                    int(pl["y"]),
                )
                for pl in sorted(lane["placements"], key=lambda item: (
                    int(item["x"]),
                    int(item["y"]),
                    item["group"]["key"],
                ))
            ),
        )

    def lane_rank(lane):
        exact_rows = sum(
            1 for pl in lane["placements"]
            if int(pl["group"]["w"]) == int(lane["w"])
        )
        return (
            -int(lane["area"]),
            int(sheet_h) - int(lane["h"]),
            int(sheet_w) - int(lane["w"]),
            -int(exact_rows),
            tuple(sorted(lane["counts"].items())),
            int(lane.get("_order") or 0),
        )

    lane_options = []
    seen_lanes = set()
    lane_order = 0

    def add_lane(width, height, counts, placements):
        nonlocal lane_order
        if not placements or int(width) <= 0 or int(height) <= 0:
            return
        if int(width) > int(sheet_w) or int(height) > int(sheet_h):
            return
        if any(int(counts[key]) > int(available[key]) for key in counts):
            return
        lane_order += 1
        area = sum(
            int(pl["group"]["w"]) * int(pl["group"]["h"])
            for pl in placements
        )
        lane = {
            "w": int(width),
            "h": int(height),
            "area": int(area),
            "counts": Counter(counts),
            "placements": [dict(pl) for pl in placements],
            "_order": lane_order,
        }
        sig = lane_sig(lane)
        if sig in seen_lanes:
            return
        seen_lanes.add(sig)
        lane_options.append(lane)

    def simple_sublanes(cap_w, cap_h, used_counts, *, limit=96):
        sublanes = []
        seen = set()
        fitting = [
            group for group in oriented
            if int(group["w"]) <= int(cap_w)
            and int(group["h"]) <= int(cap_h)
            and int(used_counts.get(group["key"], 0)) < int(available[group["key"]])
        ]
        fitting.sort(key=lambda group: (
            int(group["w"]),
            -min(
                int(available[group["key"]]) - int(used_counts.get(group["key"], 0)),
                12,
            ) * int(group["area"]),
            int(group["h"]),
            group["key"],
        ))
        for group in fitting[:(44 if allow_expensive else 22)]:
            if _deadline_hit(deadline, 0.005):
                break
            remaining_qty = int(available[group["key"]]) - int(used_counts.get(group["key"], 0))
            max_rows = min(
                remaining_qty,
                int((int(cap_h) + int(kerf)) // (int(group["h"]) + int(kerf))),
            )
            for count in count_choices(max_rows, exhaustive=allow_expensive and len(remaining) <= 90):
                placements, used_h = make_run(group, count)
                width = int(group["w"])
                counts = Counter({group["key"]: int(count)})
                sig = (
                    width,
                    int(used_h),
                    tuple(sorted(counts.items())),
                    bool(group["rotated"]),
                )
                if sig in seen:
                    continue
                seen.add(sig)
                sublanes.append({
                    "w": width,
                    "h": int(used_h),
                    "area": int(group["area"]) * int(count),
                    "counts": counts,
                    "placements": placements,
                })
        sublanes.sort(key=lambda lane: (
            int(lane["w"]),
            -int(lane["area"]),
            int(cap_h) - int(lane["h"]),
            tuple(sorted(lane["counts"].items())),
        ))
        kept = []
        seen_kept = set()

        def keep(source, max_items):
            for lane in source:
                sig = (
                    int(lane["w"]),
                    int(lane["h"]),
                    tuple(sorted(lane["counts"].items())),
                )
                if sig in seen_kept:
                    continue
                seen_kept.add(sig)
                kept.append(lane)
                if len(kept) >= max_items:
                    return

        keep(sublanes, max(1, int(limit) // 2))
        by_family = {}
        by_width = {}
        for lane in sublanes:
            if lane["counts"]:
                family = max(
                    lane["counts"],
                    key=lambda key: int(key[0]) * int(key[1]) * int(lane["counts"][key]),
                )
                by_family.setdefault(family, []).append(lane)
            by_width.setdefault(int(lane["w"]), []).append(lane)
        for width in sorted(by_width):
            keep(sorted(by_width[width], key=lambda lane: (-int(lane["area"]), int(cap_h) - int(lane["h"])))[:5], int(limit))
        for family in sorted(by_family):
            keep(sorted(by_family[family], key=lambda lane: (-int(lane["area"]), int(lane["w"])))[:6], int(limit))
        return kept[:limit]

    primary_limit = 48 if allow_expensive else 24
    for base_group in oriented[:primary_limit]:
        if _deadline_hit(deadline, 0.02):
            break
        max_rows = min(
            int(base_group["qty"]),
            int((int(sheet_h) + int(kerf)) // (int(base_group["h"]) + int(kerf))),
        )
        for base_count in count_choices(max_rows, exhaustive=allow_expensive and len(remaining) <= 90):
            base_placements, base_h = make_run(base_group, base_count)
            base_counts = Counter({base_group["key"]: int(base_count)})
            add_lane(int(base_group["w"]), int(base_h), base_counts, base_placements)
            cap_h = int(sheet_h) - int(base_h) - int(kerf)
            if cap_h <= 0 or not allow_expensive:
                continue
            sublanes = simple_sublanes(
                int(base_group["w"]),
                int(cap_h),
                base_counts,
                limit=120 if len(remaining) <= 120 else 72,
            )
            if not sublanes:
                continue

            def add_cap_combo(parts):
                counts = Counter(base_counts)
                placements = [dict(pl) for pl in base_placements]
                x = 0
                used_w = 0
                used_h = 0
                cap_y = int(base_h) + int(kerf)
                for idx, part in enumerate(parts):
                    if idx:
                        x += int(kerf)
                    counts.update(part["counts"])
                    if any(int(counts[key]) > int(available[key]) for key in counts):
                        return
                    for pl in part["placements"]:
                        rel = dict(pl)
                        rel["x"] = int(x) + int(rel.get("x") or 0)
                        rel["y"] = int(cap_y) + int(rel.get("y") or 0)
                        placements.append(rel)
                    used_w = max(used_w, int(x) + int(part["w"]))
                    used_h = max(used_h, int(part["h"]))
                    x += int(part["w"])
                if used_w > int(base_group["w"]) or cap_y + used_h > int(sheet_h):
                    return
                add_lane(
                    int(base_group["w"]),
                    max(int(base_h), int(cap_y) + int(used_h)),
                    counts,
                    placements,
                )

            for part in sublanes[:24]:
                add_cap_combo([part])
            pair_pool = sublanes[:42]
            for left in pair_pool:
                if _deadline_hit(deadline, 0.005):
                    break
                for right in pair_pool:
                    if int(left["w"]) + int(kerf) + int(right["w"]) > int(base_group["w"]):
                        continue
                    add_cap_combo([left, right])
            triple_pool = sublanes[:44]
            narrow_pool = sorted(
                triple_pool,
                key=lambda lane: (int(lane["w"]), -int(lane["area"]), tuple(sorted(lane["counts"].items()))),
            )[:24]
            for left in triple_pool:
                if _deadline_hit(deadline, 0.005):
                    break
                for middle in triple_pool:
                    used_w = int(left["w"]) + int(kerf) + int(middle["w"])
                    remaining_w = int(base_group["w"]) - used_w - int(kerf)
                    if remaining_w <= 0:
                        continue
                    for right in narrow_pool:
                        if int(right["w"]) > remaining_w:
                            continue
                        add_cap_combo([left, middle, right])
                        break

    if not lane_options:
        _record_strategy_metric(stats, "v2_exact_lane_partition_candidates", _elapsed_ms(started_at), 0)
        return []

    lane_options.sort(key=lane_rank)
    kept_lanes = []
    seen_kept_lanes = set()

    def keep_lanes(source, max_items):
        for lane in source:
            sig = lane_sig(lane)
            if sig in seen_kept_lanes:
                continue
            seen_kept_lanes.add(sig)
            kept_lanes.append(lane)
            if len(kept_lanes) >= max_items:
                return

    lane_keep = 720 if allow_expensive else 180
    keep_lanes(lane_options, max(1, lane_keep // 3))
    by_width = {}
    by_family = {}
    for lane in lane_options:
        by_width.setdefault(int(lane["w"]), []).append(lane)
        if lane["counts"]:
            family = max(
                lane["counts"],
                key=lambda key: int(key[0]) * int(key[1]) * int(lane["counts"][key]),
            )
            by_family.setdefault(family, []).append(lane)
    for width in sorted(by_width):
        keep_lanes(sorted(by_width[width], key=lane_rank)[:12 if allow_expensive else 5], lane_keep)
    for family in sorted(by_family):
        keep_lanes(sorted(by_family[family], key=lane_rank)[:14 if allow_expensive else 5], lane_keep)
    lane_options = kept_lanes[:lane_keep]

    pools = {key: list(items) for key, items in groups.items()}
    candidates = []
    seen_sheets = set()
    candidate_limit = 5200 if allow_expensive else 900

    def build_sheet(lanes):
        used_counts = Counter()
        placements = []
        free = []
        x = 0
        for lane in lanes:
            if x and x + int(lane["w"]) > int(sheet_w):
                return None
            for key, count in lane["counts"].items():
                if used_counts[key] + int(count) > int(available[key]):
                    return None
            local_counts = Counter()
            for pl in sorted(lane["placements"], key=lambda item: (
                int(item["x"]),
                int(item["y"]),
                item["group"]["key"],
            )):
                group = pl["group"]
                key = group["key"]
                item_idx = used_counts[key] + local_counts[key]
                if item_idx >= len(pools[key]):
                    return None
                piece = pools[key][item_idx]
                local_counts[key] += 1
                placements.append({
                    "item": int(piece["id"]),
                    "x": int(x) + int(pl["x"]),
                    "y": int(pl["y"]),
                    "w": int(group["w"]),
                    "h": int(group["h"]),
                    "rotated": bool(group["rotated"]),
                })
            used_counts.update(lane["counts"])
            top_y = int(lane["h"]) + int(kerf)
            if top_y < int(sheet_h):
                free.append((int(x), int(top_y), int(lane["w"]), int(sheet_h) - int(top_y)))
            x += int(lane["w"]) + int(kerf)
        used_w = x - int(kerf) if lanes else 0
        right_x = used_w + int(kerf)
        if right_x < int(sheet_w):
            free.append((int(right_x), 0, int(sheet_w) - int(right_x), int(sheet_h)))
        sheet = _sheet_from_pattern(sheet_w, sheet_h, kerf, placements, free, "exact_lane_partition")
        if not _sheet_geometry_ok(sheet):
            return None
        return sheet

    def columns_width(lanes):
        return sum(int(lane["w"]) for lane in lanes) + int(kerf) * max(0, len(lanes) - 1)

    def columns_counts(lanes):
        counts = Counter()
        for lane in lanes:
            counts.update(lane["counts"])
        return counts

    def add_candidate(lanes, *, force=False):
        if len(candidates) >= candidate_limit and not force:
            return
        if not lanes or columns_width(lanes) > int(sheet_w):
            return
        counts = columns_counts(lanes)
        if any(int(count) > int(available[key]) for key, count in counts.items()):
            return
        sheet = build_sheet(lanes)
        if sheet is None:
            return
        signature = (
            _sheet_mosaic_signature(sheet),
            tuple(
                (
                    int(pl["x"]),
                    int(pl["y"]),
                    int(pl["w"]),
                    int(pl["h"]),
                )
                for pl in sorted(sheet.placements, key=lambda p: (
                    int(p["x"]),
                    int(p["y"]),
                    int(p["w"]),
                    int(p["h"]),
                ))
            ),
        )
        if signature in seen_sheets:
            return
        seen_sheets.add(signature)
        cand = _candidate_from_sheets([sheet], "v2_exact_lane_partition")
        _append_candidate_variants(candidates, cand, allow_mirror=False)

    def state_sig(state):
        return (
            int(state["width"]),
            tuple(sorted(state["counts"].items())),
            tuple(lane_sig(lane) for lane in state["lanes"]),
        )

    def state_rank(state):
        full_count = int(state["counts"] == available)
        return (
            -full_count,
            int(sheet_w) - int(state["width"]),
            -int(state["area"]),
            -sum(int(c) for c in state["counts"].values()),
            len(state["lanes"]),
            tuple(sorted(state["counts"].items())),
        )

    states = [{
        "lanes": [],
        "counts": Counter(),
        "width": 0,
        "area": 0,
    }]
    seen_states = set()
    max_cols = 6 if allow_expensive else 4
    state_keep = 1600 if allow_expensive else 360
    for _depth in range(max_cols):
        if _deadline_hit(deadline, 0.02):
            break
        next_states = []
        for state in states:
            if _deadline_hit(deadline, 0.005):
                break
            for lane in lane_options:
                new_w = int(lane["w"]) if not state["lanes"] else int(state["width"]) + int(kerf) + int(lane["w"])
                if new_w > int(sheet_w):
                    continue
                counts = Counter(state["counts"])
                counts.update(lane["counts"])
                if any(int(counts[key]) > int(available[key]) for key in counts):
                    continue
                child = {
                    "lanes": list(state["lanes"]) + [lane],
                    "counts": counts,
                    "width": int(new_w),
                    "area": int(state["area"]) + int(lane["area"]),
                }
                sig = state_sig(child)
                if sig in seen_states:
                    continue
                seen_states.add(sig)
                next_states.append(child)
                width_slack = int(sheet_w) - int(new_w)
                if counts == available:
                    add_candidate(child["lanes"], force=True)
                elif (
                    int(child["area"]) >= int(sheet_area) * 0.62
                    and width_slack <= max(180, int(sheet_w) * 0.18)
                ):
                    add_candidate(child["lanes"])
            if len(candidates) >= candidate_limit * 2:
                break
        if not next_states:
            break
        next_states.sort(key=state_rank)
        states = next_states[:state_keep]

    _record_strategy_metric(stats, "v2_exact_lane_partition_candidates", _elapsed_ms(started_at), len(candidates))
    return candidates


def _v2_transposed_exact_lane_partition_candidates(remaining, sheet_w, sheet_h, kerf, *, stats=None, deadline=None, allow_expensive=False):
    started_at = time.monotonic()
    if int(sheet_w) == int(sheet_h):
        return []
    pieces_by_id = {int(piece["id"]): piece for piece in remaining}
    candidates = []
    seen = set()
    raw = _v2_exact_lane_partition_candidates(
        remaining,
        int(sheet_h),
        int(sheet_w),
        kerf,
        stats=None,
        deadline=deadline,
        allow_expensive=allow_expensive,
    )
    for cand in raw:
        sheets = cand.get("sheets") or []
        if len(sheets) != 1:
            continue
        rotated = _rotate_sheet_clockwise(
            sheets[0],
            int(sheet_w),
            int(sheet_h),
            pieces_by_id=pieces_by_id,
            suffix="_long_axis",
        )
        if not _sheet_geometry_ok(rotated):
            continue
        signature = (
            _sheet_mosaic_signature(rotated),
            tuple(
                (
                    int(pl["x"]),
                    int(pl["y"]),
                    int(pl["w"]),
                    int(pl["h"]),
                )
                for pl in sorted(rotated.placements, key=lambda p: (
                    int(p["x"]),
                    int(p["y"]),
                    int(p["w"]),
                    int(p["h"]),
                ))
            ),
        )
        if signature in seen:
            continue
        seen.add(signature)
        new_cand = _candidate_from_sheets([rotated], "v2_exact_lane_partition_long_axis")
        _append_candidate_variants(candidates, new_cand, allow_mirror=False)
    _record_strategy_metric(stats, "v2_exact_lane_partition_long_axis_candidates", _elapsed_ms(started_at), len(candidates))
    return candidates


def _v2_long_strip_tail_candidates(remaining, sheet_w, sheet_h, kerf, *, stats=None, deadline=None, allow_expensive=False):
    """Dense final-sheet candidates built around repeated long strip lanes."""
    started_at = time.monotonic()
    if len(remaining or []) < 8:
        return []
    groups = {}
    for piece in remaining:
        groups.setdefault(_piece_type_key(piece), []).append(piece)
    for items in groups.values():
        items.sort(key=lambda p: int(p["id"]))
    available = Counter({key: len(items) for key, items in groups.items()})

    oriented = []
    seen = set()
    for key, items in sorted(groups.items()):
        for opt in _piece_orientations(items[0], sheet_w, sheet_h):
            sig = (key, int(opt["w"]), int(opt["h"]))
            if sig in seen:
                continue
            seen.add(sig)
            oriented.append({
                "key": key,
                "items": items,
                "w": int(opt["w"]),
                "h": int(opt["h"]),
                "rotated": bool(opt["rotated"]),
                "area": int(key[0]) * int(key[1]),
                "qty": len(items),
            })
    if not oriented:
        return []

    long_groups = [
        group for group in oriented
        if int(group["qty"]) >= 4
        and int(group["w"]) <= max(220, int(sheet_w) * 0.22)
        and int(group["h"]) >= int(sheet_h) * 0.35
    ]
    long_groups.sort(key=lambda group: (
        int(group["w"]),
        -min(int(group["qty"]), 24) * int(group["area"]),
        group["key"],
    ))
    if not long_groups:
        return []

    pools = {key: list(items) for key, items in groups.items()}

    def count_choices(max_count):
        max_count = int(max_count)
        if max_count <= 0:
            return []
        choices = {max_count, 1}
        for value in (2, 3, 4, 5, 6, 8, 10, 12, 14, 16, 18, max_count - 1, max_count - 2):
            if 1 <= int(value) <= max_count:
                choices.add(int(value))
        return sorted(choices, reverse=True)

    def make_stack_block(group, count, *, cap_fill=False):
        count = int(count)
        per_col = max(1, int((int(sheet_h) + int(kerf)) // (int(group["h"]) + int(kerf))))
        columns = []
        remaining_count = count
        while remaining_count > 0:
            take = min(per_col, remaining_count)
            placements = []
            y = 0
            for _idx in range(take):
                placements.append({"group": group, "x": 0, "y": int(y)})
                y += int(group["h"]) + int(kerf)
            used_h = y - int(kerf)
            columns.append({
                "w": int(group["w"]),
                "h": int(used_h),
                "area": int(group["area"]) * int(take),
                "counts": Counter({group["key"]: int(take)}),
                "placements": placements,
            })
            remaining_count -= take
        if not columns:
            return None
        placements = []
        counts = Counter()
        x = 0
        width = 0
        height = 0
        area = 0
        for idx, column in enumerate(columns):
            if idx:
                x += int(kerf)
            for pl in column["placements"]:
                rel = dict(pl)
                rel["x"] = int(x) + int(rel.get("x") or 0)
                placements.append(rel)
            counts.update(column["counts"])
            width = max(width, int(x) + int(column["w"]))
            height = max(height, int(column["h"]))
            area += int(column["area"])
            x += int(column["w"])
        block = {
            "w": int(width),
            "h": int(height),
            "area": int(area),
            "counts": counts,
            "placements": placements,
        }
        if cap_fill:
            return fill_block_cap(block)
        return block

    def append_row(block, entries):
        gap = int(kerf) if block["placements"] else 0
        y = int(block["h"]) + gap
        row_h = 0
        row_w = 0
        counts = Counter(block["counts"])
        placements = [dict(pl) for pl in block["placements"]]
        x = 0
        area = int(block["area"])
        for group, count in entries:
            count = int(count)
            if count <= 0:
                return None
            counts[group["key"]] += count
            if counts[group["key"]] > available[group["key"]]:
                return None
            for _idx in range(count):
                placements.append({"group": group, "x": int(x), "y": int(y)})
                x += int(group["w"]) + int(kerf)
                area += int(group["area"])
            row_h = max(row_h, int(group["h"]))
        row_w = x - int(kerf) if x else 0
        if row_w > int(block["w"]) or y + row_h > int(sheet_h):
            return None
        return {
            "w": int(block["w"]),
            "h": max(int(block["h"]), y + row_h),
            "area": int(area),
            "counts": counts,
            "placements": placements,
        }

    def row_options(width_limit, height_limit, used_counts):
        fitting = [
            group for group in oriented
            if int(group["w"]) <= int(width_limit)
            and int(group["h"]) <= int(height_limit)
            and used_counts[group["key"]] < available[group["key"]]
            and not (
                int(group["w"]) <= max(220, int(sheet_w) * 0.22)
                and int(group["h"]) >= int(sheet_h) * 0.35
            )
        ]
        fitting.sort(key=lambda group: (
            -int(group["area"]),
            int(group["w"]),
            int(group["h"]),
            group["key"],
        ))
        options = []
        for group in fitting[:18]:
            remaining_count = int(available[group["key"]]) - int(used_counts[group["key"]])
            cap = min(remaining_count, int((int(width_limit) + int(kerf)) // (int(group["w"]) + int(kerf))))
            for count in count_choices(cap):
                row_w = int(count) * int(group["w"]) + int(kerf) * max(0, int(count) - 1)
                if row_w <= int(width_limit):
                    options.append((int(width_limit) - row_w, -int(group["area"]) * int(count), [(group, int(count))]))
        for left, right in combinations(fitting[:12], 2):
            row_w = int(left["w"]) + int(kerf) + int(right["w"])
            if row_w <= int(width_limit):
                options.append((int(width_limit) - row_w, -int(left["area"]) - int(right["area"]), [(left, 1), (right, 1)]))
        options.sort(key=lambda item: item[:2])
        return [entries for _waste, _area_rank, entries in options[:12]]

    def fill_block_cap_variants(block, limit=12):
        states = [block]
        best = [block]
        for _depth in range(3):
            next_states = []
            for state in states:
                if _deadline_hit(deadline, 0.005):
                    break
                gap = int(kerf) if state["placements"] else 0
                remaining_h = int(sheet_h) - int(state["h"]) - gap
                if remaining_h <= 0:
                    continue
                for entries in row_options(int(state["w"]), remaining_h, state["counts"]):
                    child = append_row(state, entries)
                    if child:
                        next_states.append(child)
                        best.append(child)
            if not next_states:
                break
            next_states.sort(key=lambda state: (-int(state["area"]), int(sheet_h) - int(state["h"]), tuple(sorted(state["counts"].items()))))
            states = next_states[:8]
        ranked = []
        seen_caps = set()
        for state in best:
            cap_counts = Counter(state["counts"])
            cap_counts.subtract(block["counts"])
            sig = tuple(sorted((key, int(count)) for key, count in cap_counts.items() if int(count) > 0))
            if sig in seen_caps:
                continue
            seen_caps.add(sig)
            ranked.append(state)
        ranked.sort(key=lambda state: (-int(state["area"]), int(sheet_h) - int(state["h"]), tuple(sorted(state["counts"].items()))))
        return ranked[:int(limit)] or [block]

    def fill_block_cap(block):
        variants = fill_block_cap_variants(block, limit=1)
        return variants[0] if variants else block

    companion_groups = [
        group for group in oriented
        if group not in long_groups
        and int(group["qty"]) >= 2
        and int(group["w"]) <= int(sheet_w) * 0.45
    ]
    companion_groups.sort(key=lambda group: (
        -min(int(group["qty"]), 8) * int(group["area"]),
        int(group["w"]),
        group["key"],
    ))

    blocks = []
    for group in companion_groups[:24]:
        max_rows = min(
            int(group["qty"]),
            int((int(sheet_h) + int(kerf)) // (int(group["h"]) + int(kerf))),
        )
        for count in count_choices(max_rows):
            block = make_stack_block(group, count, cap_fill=False)
            if block:
                blocks.append(fill_block_cap(block))
    for group in long_groups[:8]:
        per_col = max(1, int((int(sheet_h) + int(kerf)) // (int(group["h"]) + int(kerf))))
        max_cols = int((int(sheet_w) + int(kerf)) // (int(group["w"]) + int(kerf)))
        max_count = min(int(group["qty"]), max_cols * per_col)
        for count in count_choices(max_count):
            block = make_stack_block(group, count)
            if block:
                blocks.append(block)
    blocks.sort(key=lambda block: (-int(block["area"]), int(block["w"]), tuple(sorted(block["counts"].items()))))

    candidates = []
    seen_sheets = set()

    def build_sheet(parts):
        counts = Counter()
        x = 0
        placements = []
        free = []
        used_w = 0
        for idx, part in enumerate(parts):
            if idx:
                x += int(kerf)
            if x + int(part["w"]) > int(sheet_w):
                return None
            counts.update(part["counts"])
            if any(counts[key] > available[key] for key in counts):
                return None
            for pl in part["placements"]:
                rel = dict(pl)
                group = rel["group"]
                rel["x"] = int(x) + int(rel.get("x") or 0)
                placements.append(rel)
            top_y = int(part["h"]) + int(kerf)
            if top_y < int(sheet_h):
                free.append((int(x), int(top_y), int(part["w"]), int(sheet_h) - int(top_y)))
            used_w = max(used_w, int(x) + int(part["w"]))
            x += int(part["w"])
        right_x = int(used_w) + int(kerf)
        if right_x < int(sheet_w):
            free.append((right_x, 0, int(sheet_w) - right_x, int(sheet_h)))

        used_counts = Counter()
        materialized = []
        for rel in sorted(placements, key=lambda pl: (int(pl["x"]), int(pl["y"]), pl["group"]["key"])):
            group = rel["group"]
            key = group["key"]
            idx = used_counts[key]
            if idx >= len(pools[key]):
                return None
            piece = pools[key][idx]
            used_counts[key] += 1
            materialized.append({
                "item": int(piece["id"]),
                "x": int(rel["x"]),
                "y": int(rel["y"]),
                "w": int(group["w"]),
                "h": int(group["h"]),
                "rotated": bool(group["rotated"]),
            })
        sheet = _sheet_from_pattern(sheet_w, sheet_h, kerf, materialized, free, "long_strip_tail")
        if not _sheet_geometry_ok(sheet):
            return None
        return sheet

    strip_blocks = [block for block in blocks if any(key in {g["key"] for g in long_groups} for key in block["counts"])]
    companion_blocks = [block for block in blocks if block not in strip_blocks]

    for strip in strip_blocks[:80 if allow_expensive else 28]:
        if _deadline_hit(deadline, 0.01):
            break
        pools_to_try = [
            [strip],
        ]
        for left in companion_blocks[:80 if allow_expensive else 24]:
            pools_to_try.append([left, strip])
            pools_to_try.append([strip, left])
        secondary_longs = [block for block in strip_blocks if block is not strip and int(block["w"]) <= int(sheet_w) * 0.18]
        for left in companion_blocks[:48 if allow_expensive else 12]:
            for right in secondary_longs[:24 if allow_expensive else 8]:
                pools_to_try.append([left, strip, right])
                pools_to_try.append([right, strip, left])
        for parts in pools_to_try:
            if _deadline_hit(deadline, 0.005):
                break
            sheet = build_sheet(parts)
            if sheet is None:
                continue
            full = _best_offcut_info([sheet], full_dim_only=True)
            if int(full.get("value") or 0) <= 0:
                continue
            sig = (
                _sheet_mosaic_signature(sheet),
                tuple(sorted((int(pl["x"]), int(pl["y"]), int(pl["w"]), int(pl["h"])) for pl in sheet.placements)),
            )
            if sig in seen_sheets:
                continue
            seen_sheets.add(sig)
            cand = _candidate_from_sheets([sheet], "v2_long_strip_tail")
            _append_candidate_variants(candidates, cand, allow_mirror=False)
            if len(candidates) >= (900 if allow_expensive else 240):
                break
        if len(candidates) >= (900 if allow_expensive else 240):
            break

    _record_strategy_metric(stats, "v2_long_strip_tail_candidates", _elapsed_ms(started_at), len(candidates))
    return candidates


def _v2_full_height_column_tail_candidates(remaining, sheet_w, sheet_h, kerf, *, stats=None, deadline=None, allow_expensive=False):
    """Tail sheets made from full-height rip columns.

    CutList-style residual sheets often look like one mixed narrow column, several
    repeated long-strip columns, and a clean full-height offcut. This generator
    searches that structure directly from dimensions/counts: build column
    components, repeat compatible components across the sheet width, then keep
    combinations that still leave a full-height reusable strip.
    """
    started_at = time.monotonic()
    if len(remaining or []) < 8:
        return []

    groups = {}
    for piece in remaining:
        groups.setdefault(_piece_type_key(piece), []).append(piece)
    for items in groups.values():
        items.sort(key=lambda p: int(p["id"]))

    available = Counter({key: len(items) for key, items in groups.items()})
    oriented = []
    seen_orientations = set()
    for key, items in sorted(groups.items()):
        for opt in _piece_orientations(items[0], sheet_w, sheet_h):
            sig = (key, int(opt["w"]), int(opt["h"]))
            if sig in seen_orientations:
                continue
            seen_orientations.add(sig)
            short = min(int(opt["w"]), int(opt["h"]))
            long = max(int(opt["w"]), int(opt["h"]))
            oriented.append({
                "key": key,
                "items": items,
                "w": int(opt["w"]),
                "h": int(opt["h"]),
                "rotated": bool(opt["rotated"]),
                "area": int(key[0]) * int(key[1]),
                "qty": len(items),
                "aspect": float(long) / float(short or 1),
            })
    if not oriented:
        return []

    pools = {key: list(items) for key, items in groups.items()}
    sheet_area = max(1, int(sheet_w) * int(sheet_h))
    kerf = int(kerf)
    component_limit = 760 if allow_expensive else 260
    candidate_limit = 2200 if allow_expensive else 260

    def layer_gap(layers):
        return kerf if layers else 0

    def column_height(layers):
        if not layers:
            return 0
        return (
            sum(int(layer["h"]) for layer in layers)
            + kerf * max(0, len(layers) - 1)
        )

    def column_area(layers):
        total = 0
        for layer in layers:
            for entry in layer["entries"]:
                group = entry["group"]
                total += int(entry["count"]) * int(group["area"])
        return total

    def column_counts(layers):
        counts = Counter()
        for layer in layers:
            for entry in layer["entries"]:
                counts[entry["group"]["key"]] += int(entry["count"])
        return counts

    def layer_signature(layer):
        return tuple(
            (
                entry["group"]["key"],
                int(entry["group"]["w"]),
                int(entry["group"]["h"]),
                int(entry["count"]),
            )
            for entry in layer["entries"]
        )

    def component_signature(component):
        return (
            int(component["w"]),
            tuple(layer_signature(layer) for layer in component["layers"]),
        )

    def make_layer(entries):
        entries = [
            {"group": group, "count": int(count)}
            for group, count in entries
            if int(count) > 0
        ]
        if not entries:
            return None
        row_w = (
            sum(int(entry["group"]["w"]) * int(entry["count"]) for entry in entries)
            + kerf * max(0, sum(int(entry["count"]) for entry in entries) - 1)
        )
        row_h = max(int(entry["group"]["h"]) for entry in entries)
        return {
            "entries": entries,
            "w": int(row_w),
            "h": int(row_h),
        }

    def state_rank(layers, col_w):
        height = column_height(layers)
        counts = column_counts(layers)
        area = column_area(layers)
        short_strip_spend = sum(
            int(count)
            for key, count in counts.items()
            if min(int(key[0]), int(key[1])) <= max(120, int(sheet_w) * 0.08)
            and max(int(key[0]), int(key[1])) >= int(sheet_h) * 0.25
        )
        large_block_fill = sum(
            int(count)
            for key, count in counts.items()
            if min(int(key[0]), int(key[1])) >= 120
            and max(int(key[0]), int(key[1])) <= int(sheet_h) * 0.22
        )
        return (
            int(sheet_h) - int(height),
            -int(area),
            int(short_strip_spend),
            -int(large_block_fill),
            int(col_w),
            tuple(sorted(counts.items())),
            tuple(layer_signature(layer) for layer in layers),
        )

    blocky_fillers = [
        group for group in oriented
        if int(group["qty"]) >= 1
        and int(group["w"]) <= int(sheet_w) * 0.45
        and int(group["h"]) <= int(sheet_h) * 0.35
        and float(group["aspect"]) <= 3.2
    ]
    blocky_fillers.sort(key=lambda group: (
        -int(group["area"]),
        int(group["w"]),
        int(group["h"]),
        group["key"],
    ))

    seed_groups = [
        group for group in oriented
        if int(group["qty"]) >= 2
        and int(group["w"]) <= int(sheet_w) * 0.40
        and int(group["h"]) >= int(sheet_h) * 0.16
    ]
    seed_groups.sort(key=lambda group: (
        int(group["w"]),
        -min(int(group["qty"]), 12) * int(group["area"]),
        -int(group["h"]),
        group["key"],
    ))

    def candidate_layers_for_cap(col_w, used_counts, remaining_h, max_options=26):
        options = []
        if remaining_h <= 0:
            return options
        for group in blocky_fillers[:(36 if allow_expensive else 22)]:
            if int(group["w"]) > int(col_w) or int(group["h"]) > int(remaining_h):
                continue
            remain = int(available[group["key"]]) - int(used_counts[group["key"]])
            if remain <= 0:
                continue
            cap = min(remain, int((int(col_w) + kerf) // (int(group["w"]) + kerf)))
            counts = {1, cap}
            if cap >= 2:
                counts.add(2)
            if cap >= 3:
                counts.add(3)
            for count in sorted(counts, reverse=True):
                layer = make_layer([(group, count)])
                if not layer or int(layer["w"]) > int(col_w):
                    continue
                width_waste = int(col_w) - int(layer["w"])
                rank = (
                    width_waste // 25,
                    -int(group["area"]) * int(count),
                    width_waste,
                    int(group["h"]),
                    -int(count),
                    group["key"],
                )
                options.append((rank, layer))

        mix_groups = [
            group for group in blocky_fillers[:(28 if allow_expensive else 16)]
            if int(group["w"]) <= int(col_w)
            and int(group["h"]) <= int(remaining_h)
            and int(available[group["key"]]) - int(used_counts[group["key"]]) > 0
        ]
        for left, right in combinations(mix_groups, 2):
            layer = make_layer([(left, 1), (right, 1)])
            if not layer or int(layer["w"]) > int(col_w) or int(layer["h"]) > int(remaining_h):
                continue
            entry_counts = Counter((left["key"], right["key"]))
            if any(int(used_counts[key]) + int(count) > int(available[key]) for key, count in entry_counts.items()):
                continue
            width_waste = int(col_w) - int(layer["w"])
            rank = (
                width_waste // 25,
                -int(left["area"]) - int(right["area"]),
                width_waste,
                int(layer["h"]),
                -2,
                (left["key"], right["key"]),
            )
            options.append((rank, layer))

        options.sort(key=lambda item: item[0])
        return [layer for _rank, layer in options[:int(max_options)]]

    def build_column_components():
        components = []
        seen = set()
        for seed in seed_groups[:(36 if allow_expensive else 20)]:
            if _deadline_hit(deadline, 0.005):
                break
            per_col = int((int(sheet_h) + kerf) // (int(seed["h"]) + kerf))
            max_seed_count = min(int(seed["qty"]), max(1, per_col))
            if max_seed_count <= 0:
                continue
            seed_counts = {max_seed_count, 1}
            for value in (2, 3, 4, 5, max_seed_count - 1):
                if 1 <= int(value) <= max_seed_count:
                    seed_counts.add(int(value))
            for seed_count in sorted(seed_counts, reverse=True):
                base_layers = []
                for _idx in range(int(seed_count)):
                    layer = make_layer([(seed, 1)])
                    if not layer:
                        break
                    base_layers.append(layer)
                if len(base_layers) != int(seed_count):
                    continue
                if column_height(base_layers) > int(sheet_h):
                    continue

                states = [{
                    "layers": base_layers,
                    "counts": column_counts(base_layers),
                }]
                best_states = list(states)
                max_depth = 5 if allow_expensive else 3
                for _depth in range(max_depth):
                    next_states = []
                    for state in states:
                        if _deadline_hit(deadline, 0.003):
                            break
                        height = column_height(state["layers"])
                        remaining_h = int(sheet_h) - height - layer_gap(state["layers"])
                        if remaining_h <= 0:
                            continue
                        for layer in candidate_layers_for_cap(
                            int(seed["w"]),
                            state["counts"],
                            remaining_h,
                            max_options=(34 if allow_expensive else 16),
                        ):
                            counts = Counter(state["counts"])
                            for entry in layer["entries"]:
                                counts[entry["group"]["key"]] += int(entry["count"])
                            if any(int(counts[key]) > int(available[key]) for key in counts):
                                continue
                            child_layers = list(state["layers"]) + [layer]
                            if column_height(child_layers) > int(sheet_h):
                                continue
                            child = {
                                "layers": child_layers,
                                "counts": counts,
                            }
                            next_states.append(child)
                            best_states.append(child)
                    if not next_states:
                        break
                    next_states.sort(key=lambda state: state_rank(state["layers"], int(seed["w"])))
                    diverse = []
                    diverse_seen = set()
                    for state in next_states:
                        sig = tuple(layer_signature(layer) for layer in state["layers"])
                        if sig in diverse_seen:
                            continue
                        diverse_seen.add(sig)
                        diverse.append(state)
                        if len(diverse) >= (80 if allow_expensive else 24):
                            break
                    states = diverse

                for state in best_states:
                    layers = state["layers"]
                    height = column_height(layers)
                    if height <= 0 or height > int(sheet_h):
                        continue
                    component = {
                        "w": int(seed["w"]),
                        "h": int(height),
                        "area": int(column_area(layers)),
                        "counts": column_counts(layers),
                        "layers": layers,
                        "kind": "column",
                    }
                    if int(component["area"]) < sheet_area * 0.04:
                        continue
                    sig = component_signature(component)
                    if sig in seen:
                        continue
                    seen.add(sig)
                    components.append(component)
                    if len(components) >= component_limit:
                        return components
        return components

    def repeat_block(component, copies):
        copies = int(copies)
        if copies <= 0:
            return None
        counts = Counter()
        placements = []
        x = 0
        width = 0
        height = 0
        area = 0
        for idx in range(copies):
            if idx:
                x += kerf
            y = 0
            for layer in component["layers"]:
                row_x = 0
                for entry in layer["entries"]:
                    group = entry["group"]
                    for _item_idx in range(int(entry["count"])):
                        placements.append({
                            "group": group,
                            "x": int(x) + int(row_x),
                            "y": int(y),
                        })
                        row_x += int(group["w"]) + kerf
                    counts[group["key"]] += int(entry["count"])
                y += int(layer["h"]) + kerf
            height = max(height, int(component["h"]))
            width = max(width, int(x) + int(component["w"]))
            area += int(component["area"])
            x += int(component["w"])
        return {
            "w": int(width),
            "h": int(height),
            "area": int(area),
            "counts": counts,
            "placements": placements,
            "copies": copies,
            "source": component,
        }

    components = build_column_components()
    if not components:
        _record_strategy_metric(stats, "v2_full_height_column_tail_candidates", _elapsed_ms(started_at), 0)
        return []

    components.sort(key=lambda component: (
        int(sheet_h) - int(component["h"]),
        int(component["w"]),
        -int(component["area"]),
        tuple(sorted(component["counts"].items())),
        component_signature(component),
    ))

    blocks = []
    block_seen = set()
    for component in components[:(520 if allow_expensive else 180)]:
        if _deadline_hit(deadline, 0.005):
            break
        counts = Counter(component["counts"])
        if not counts:
            continue
        max_by_stock = min(
            int(available[key]) // max(1, int(count))
            for key, count in counts.items()
        )
        max_by_width = int((int(sheet_w) + kerf) // (int(component["w"]) + kerf))
        max_copies = min(max_by_stock, max_by_width, 10)
        copy_choices = {1, max_copies}
        for value in (2, 3, 4, 5, 6, max_copies - 1):
            if 1 <= int(value) <= max_copies:
                copy_choices.add(int(value))
        for copies in sorted(copy_choices, reverse=True):
            block = repeat_block(component, copies)
            if not block:
                continue
            if any(int(block["counts"][key]) > int(available[key]) for key in block["counts"]):
                continue
            sig = (
                int(block["w"]),
                tuple(sorted(block["counts"].items())),
                component_signature(component),
            )
            if sig in block_seen:
                continue
            block_seen.add(sig)
            blocks.append(block)

    priority_tail_blocks = []
    for group in oriented:
        if _deadline_hit(deadline, 0.003):
            break
        if not (
            int(group["qty"]) >= 2
            and int(group["w"]) <= max(180, int(sheet_w) * 0.18)
            and int(group["h"]) >= int(sheet_h) * 0.25
        ):
            continue
        per_col = int((int(sheet_h) + kerf) // (int(group["h"]) + kerf))
        if per_col <= 0:
            continue
        row_counts = {min(int(group["qty"]), per_col)}
        if per_col >= 2:
            row_counts.add(min(int(group["qty"]), per_col - 1))
            row_counts.add(2)
        row_counts.add(1)
        for rows in sorted({int(v) for v in row_counts if int(v) > 0}, reverse=True):
            layers = []
            for _idx in range(rows):
                layer = make_layer([(group, 1)])
                if layer:
                    layers.append(layer)
            if len(layers) != rows or column_height(layers) > int(sheet_h):
                continue
            component = {
                "w": int(group["w"]),
                "h": int(column_height(layers)),
                "area": int(column_area(layers)),
                "counts": column_counts(layers),
                "layers": layers,
                "kind": "pure_strip_column",
            }
            max_by_stock = int(available[group["key"]]) // max(1, rows)
            max_by_width = int((int(sheet_w) + kerf) // (int(component["w"]) + kerf))
            max_copies = min(max_by_stock, max_by_width, 12)
            copy_choices = {1, max_copies}
            for value in (2, 3, 4, 5, 6, 8, 10, max_copies - 1):
                if 1 <= int(value) <= max_copies:
                    copy_choices.add(int(value))
            for copies in sorted(copy_choices, reverse=True):
                block = repeat_block(component, copies)
                if not block:
                    continue
                sig = (
                    int(block["w"]),
                    tuple(sorted(block["counts"].items())),
                    component_signature(component),
                )
                if sig in block_seen:
                    continue
                block_seen.add(sig)
                blocks.append(block)
                priority_tail_blocks.append(block)

    if not blocks:
        _record_strategy_metric(stats, "v2_full_height_column_tail_candidates", _elapsed_ms(started_at), 0)
        return []

    blocks.sort(key=lambda block: (
        int(sheet_w) - int(block["w"]),
        int(sheet_h) - int(block["h"]),
        -int(block["area"]),
        -int(block["copies"]),
        tuple(sorted(block["counts"].items())),
    ))

    candidates = []
    seen_sheets = set()
    active_candidate_limit = max(1, candidate_limit - (420 if allow_expensive else 50))

    def build_sheet(parts):
        counts = Counter()
        placements = []
        free = []
        x = 0
        used_w = 0
        for idx, part in enumerate(parts):
            if idx:
                x += kerf
            if x + int(part["w"]) > int(sheet_w):
                return None
            counts.update(part["counts"])
            if any(int(counts[key]) > int(available[key]) for key in counts):
                return None
            for rel in part["placements"]:
                group = rel["group"]
                placements.append({
                    "group": group,
                    "x": int(x) + int(rel["x"]),
                    "y": int(rel["y"]),
                })
            top_y = int(part["h"]) + kerf
            if top_y < int(sheet_h):
                free.append((int(x), int(top_y), int(part["w"]), int(sheet_h) - int(top_y)))
            used_w = max(used_w, int(x) + int(part["w"]))
            x += int(part["w"])
        right_x = int(used_w) + kerf
        if right_x < int(sheet_w):
            free.append((right_x, 0, int(sheet_w) - right_x, int(sheet_h)))

        used_counts = Counter()
        materialized = []
        for rel in sorted(placements, key=lambda pl: (int(pl["x"]), int(pl["y"]), pl["group"]["key"], int(pl["group"]["w"]), int(pl["group"]["h"]))):
            group = rel["group"]
            key = group["key"]
            idx = int(used_counts[key])
            if idx >= len(pools[key]):
                return None
            piece = pools[key][idx]
            used_counts[key] += 1
            materialized.append({
                "item": int(piece["id"]),
                "x": int(rel["x"]),
                "y": int(rel["y"]),
                "w": int(group["w"]),
                "h": int(group["h"]),
                "rotated": bool(group["rotated"]),
            })
        sheet = _sheet_from_pattern(sheet_w, sheet_h, kerf, materialized, free, "full_height_column_tail")
        return sheet if _sheet_geometry_ok(sheet) else None

    def add_combo(parts):
        if _deadline_hit(deadline, 0.002):
            return False
        sheet = build_sheet(parts)
        if sheet is None:
            return False
        full = _best_offcut_info([sheet], full_dim_only=True)
        if int(full.get("value") or 0) <= 0:
            return False
        used_area = sum(int(pl["w"]) * int(pl["h"]) for pl in sheet.placements or [])
        if used_area < sheet_area * 0.72:
            return False
        sig = (
            _sheet_mosaic_signature(sheet),
            tuple(sorted((int(pl["x"]), int(pl["y"]), int(pl["w"]), int(pl["h"])) for pl in sheet.placements)),
        )
        if sig in seen_sheets:
            return False
        seen_sheets.add(sig)
        cand = _candidate_from_sheets([sheet], "v2_full_height_column_tail")
        _append_candidate_variants(candidates, cand, allow_mirror=False)
        return len(candidates) >= active_candidate_limit

    dense_blocks = sorted(
        blocks,
        key=lambda block: (
            -(int(block["area"]) * 1000000 // max(1, int(block["w"]))),
            int(sheet_w) - int(block["w"]),
            tuple(sorted(block["counts"].items())),
        ),
    )
    narrow_blocks = [
        block for block in dense_blocks
        if int(block["w"]) <= int(sheet_w) * 0.35
    ]
    wide_repeat_blocks = [
        block for block in dense_blocks
        if int(block["copies"]) >= 2
    ]

    seed_pool = dense_blocks[:(160 if allow_expensive else 60)]
    repeat_pool = priority_tail_blocks + wide_repeat_blocks[:(180 if allow_expensive else 70)]
    narrow_pool = priority_tail_blocks + narrow_blocks[:(220 if allow_expensive else 90)]

    def pure_block_key(block):
        counts = Counter(block["counts"])
        return next(iter(counts)) if len(counts) == 1 else None

    mixed_column_blocks = [
        block for block in dense_blocks
        if len(Counter(block["counts"])) >= 2
        and int(block["copies"]) == 1
        and int(block["w"]) <= int(sheet_w) * 0.40
        and int(block["h"]) >= int(sheet_h) * 0.78
    ]
    mixed_column_blocks.sort(key=lambda block: (
        int(sheet_h) - int(block["h"]),
        int(block["w"]),
        -int(block["area"]),
        tuple(sorted(block["counts"].items())),
    ))
    primary_strip_blocks = [
        block for block in priority_tail_blocks
        if pure_block_key(block) is not None
        and int(block["copies"]) >= 2
    ]
    primary_strip_blocks.sort(key=lambda block: (
        int(sheet_h) - int(block["h"]),
        -int(block["copies"]),
        int(block["w"]),
        -int(block["area"]),
        tuple(sorted(block["counts"].items())),
    ))
    secondary_strip_blocks = [
        block for block in priority_tail_blocks
        if pure_block_key(block) is not None
    ]
    secondary_strip_blocks.sort(key=lambda block: (
        -int(block["area"]),
        int(sheet_h) - int(block["h"]),
        int(block["w"]),
        tuple(sorted(block["counts"].items())),
    ))

    structural_combos = []
    for mixed in mixed_column_blocks[:(160 if allow_expensive else 36)]:
        if _deadline_hit(deadline, 0.01):
            break
        for primary in primary_strip_blocks[:(220 if allow_expensive else 52)]:
            if _deadline_hit(deadline, 0.005):
                break
            if pure_block_key(primary) in Counter(mixed["counts"]):
                continue
            pair_w = int(mixed["w"]) + kerf + int(primary["w"])
            if pair_w > int(sheet_w):
                continue
            remaining_w = int(sheet_w) - pair_w - kerf
            if remaining_w < 80:
                continue
            counts = Counter(mixed["counts"]) + Counter(primary["counts"])
            if any(int(counts[key]) > int(available[key]) for key in counts):
                continue
            for secondary in secondary_strip_blocks[:(260 if allow_expensive else 60)]:
                if _deadline_hit(deadline, 0.002):
                    break
                if secondary is mixed or secondary is primary:
                    continue
                skey = pure_block_key(secondary)
                if skey is None or skey in counts:
                    continue
                if int(secondary["w"]) > remaining_w - 80:
                    continue
                combo_counts = Counter(counts) + Counter(secondary["counts"])
                if any(int(combo_counts[key]) > int(available[key]) for key in combo_counts):
                    continue
                total_w = int(mixed["w"]) + kerf + int(primary["w"]) + kerf + int(secondary["w"])
                offcut_w = int(sheet_w) - total_w - kerf
                if offcut_w < 80:
                    continue
                used_area = int(mixed["area"]) + int(primary["area"]) + int(secondary["area"])
                min_height = min(int(mixed["h"]), int(primary["h"]), int(secondary["h"]))
                structural_combos.append((
                    (
                        int(offcut_w),
                        int(sheet_h) - int(min_height),
                        -int(used_area),
                        int(mixed["w"]),
                        tuple(sorted(combo_counts.items())),
                    ),
                    [mixed, primary, secondary],
                ))
                structural_combos.append((
                    (
                        int(offcut_w),
                        int(sheet_h) - int(min_height),
                        -int(used_area),
                        int(secondary["w"]),
                        tuple(sorted(combo_counts.items())),
                    ),
                    [secondary, primary, mixed],
                ))
    structural_combos.sort(key=lambda item: item[0])
    structural_seen = set()
    for _rank, parts in structural_combos[:(2400 if allow_expensive else 160)]:
        sig = tuple(
            (
                int(part["w"]),
                tuple(sorted(part["counts"].items())),
                int(part["copies"]),
            )
            for part in parts
        )
        if sig in structural_seen:
            continue
        structural_seen.add(sig)
        if add_combo(parts):
            break

    for first in seed_pool:
        if _deadline_hit(deadline, 0.01) or len(candidates) >= active_candidate_limit:
            break
        if add_combo([first]):
            break
        for second in repeat_pool:
            if _deadline_hit(deadline, 0.004) or len(candidates) >= active_candidate_limit:
                break
            pair = [first, second]
            width = int(first["w"]) + kerf + int(second["w"])
            if width > int(sheet_w):
                continue
            counts = Counter(first["counts"]) + Counter(second["counts"])
            if any(int(counts[key]) > int(available[key]) for key in counts):
                continue
            if add_combo(pair):
                break
            remaining_w = int(sheet_w) - width - kerf
            if remaining_w <= 0:
                continue
            max_tail_filler_w = max(0, int(remaining_w) - 80)
            compatible = [
                block for block in narrow_pool
                if block is not first
                and block is not second
                and int(block["w"]) <= max_tail_filler_w
            ]
            compatible.sort(key=lambda block: (
                remaining_w - int(block["w"]),
                int(sheet_h) - int(block["h"]),
                -int(block["area"]),
                tuple(sorted(block["counts"].items())),
            ))
            for third in compatible[:(180 if allow_expensive else 28)]:
                combo = [first, second, third]
                counts = Counter()
                for part in combo:
                    counts.update(part["counts"])
                if any(int(counts[key]) > int(available[key]) for key in counts):
                    continue
                if add_combo(combo):
                    break
            if len(candidates) >= active_candidate_limit:
                break

    def extend_full_dim_strip_candidates():
        target_strip = max(160, int(min(int(sheet_w), int(sheet_h)) * 0.16))

        def refill_source_rank(cand):
            sheets = cand.get("sheets") or []
            if len(sheets) != 1:
                return (1, 999999, 999999, 999999)
            sheet = sheets[0]
            full = _best_offcut_info([sheet], full_dim_only=True)
            short = min(int(full.get("w") or 0), int(full.get("h") or 0))
            if short <= 0:
                return (1, 999999, 999999, 999999)
            return (
                0,
                abs(short - int(target_strip)),
                int(saw_metrics(sheet)[1]),
                -len(sheet.placements or []),
            )

        source = sorted(candidates, key=refill_source_rank)
        if not source:
            return
        added = 0
        for cand in source[:(3200 if allow_expensive else 200)]:
            if _deadline_hit(deadline, 0.01) or len(candidates) >= candidate_limit:
                break
            sheets = cand.get("sheets") or []
            if len(sheets) != 1:
                continue
            sheet = sheets[0]
            used = set(int(item_id) for item_id in cand.get("used") or ())
            remaining_pools = {}
            for key, items in groups.items():
                kept = [piece for piece in items if int(piece["id"]) not in used]
                if kept:
                    remaining_pools[key] = kept
            if not remaining_pools:
                continue

            def oriented_remaining(max_w, max_h):
                options = []
                seen = set()
                for key, items in sorted(remaining_pools.items()):
                    sample = items[0]
                    for opt in _piece_orientations(sample, max_w, max_h):
                        sig = (key, int(opt["w"]), int(opt["h"]))
                        if sig in seen:
                            continue
                        seen.add(sig)
                        short = min(int(opt["w"]), int(opt["h"]))
                        long = max(int(opt["w"]), int(opt["h"]))
                        if long < max(int(sheet_w), int(sheet_h)) * 0.25:
                            continue
                        options.append({
                            "key": key,
                            "items": items,
                            "w": int(opt["w"]),
                            "h": int(opt["h"]),
                            "rotated": bool(opt["rotated"]),
                            "area": int(key[0]) * int(key[1]),
                            "aspect": float(long) / float(short or 1),
                        })
                return options

            def materialize_extra(group, count, x, y, step_axis):
                materialized = []
                items = remaining_pools.get(group["key"]) or []
                if len(items) < int(count):
                    return None
                px, py = int(x), int(y)
                for piece in items[:int(count)]:
                    materialized.append({
                        "item": int(piece["id"]),
                        "x": int(px),
                        "y": int(py),
                        "w": int(group["w"]),
                        "h": int(group["h"]),
                        "rotated": bool(group["rotated"]),
                    })
                    if step_axis == "y":
                        py += int(group["h"]) + kerf
                    else:
                        px += int(group["w"]) + kerf
                return materialized

            variants = []
            for rect_index, (fx, fy, fw, fh) in enumerate(sheet.free or []):
                fx, fy, fw, fh = int(fx), int(fy), int(fw), int(fh)
                vertical_strip = fh >= int(sheet_h) - 5 and fw >= 160
                horizontal_strip = fw >= int(sheet_w) - 5 and fh >= 160
                if not vertical_strip and not horizontal_strip:
                    continue
                if vertical_strip:
                    for group in oriented_remaining(max(1, fw - 80 - kerf), fh):
                        if int(group["w"]) > fw - 80 - kerf:
                            continue
                        max_count = min(
                            len(group["items"]),
                            int((fh + kerf) // (int(group["h"]) + kerf)),
                        )
                        counts_to_try = {max_count, 1}
                        for value in (2, 3, max_count - 1):
                            if 1 <= int(value) <= max_count:
                                counts_to_try.add(int(value))
                        for count in sorted(counts_to_try, reverse=True):
                            used_h = int(count) * int(group["h"]) + kerf * max(0, int(count) - 1)
                            if used_h > fh:
                                continue
                            leftover_w = fw - int(group["w"]) - kerf
                            if leftover_w < 80:
                                continue
                            extra = materialize_extra(group, count, fx, fy, "y")
                            if not extra:
                                continue
                            free = [r for idx, r in enumerate(sheet.free or []) if idx != rect_index]
                            top_y = fy + used_h + kerf
                            if top_y < fy + fh:
                                free.append((fx, top_y, int(group["w"]), fy + fh - top_y))
                            free.append((fx + int(group["w"]) + kerf, fy, leftover_w, fh))
                            variants.append((
                                (
                                    leftover_w,
                                    fh - used_h,
                                    -int(count) * int(group["area"]),
                                    group["key"],
                                ),
                                extra,
                                free,
                            ))
                if horizontal_strip:
                    for group in oriented_remaining(fw, max(1, fh - 80 - kerf)):
                        if int(group["h"]) > fh - 80 - kerf:
                            continue
                        max_count = min(
                            len(group["items"]),
                            int((fw + kerf) // (int(group["w"]) + kerf)),
                        )
                        counts_to_try = {max_count, 1}
                        for value in (2, 3, max_count - 1):
                            if 1 <= int(value) <= max_count:
                                counts_to_try.add(int(value))
                        for count in sorted(counts_to_try, reverse=True):
                            used_w = int(count) * int(group["w"]) + kerf * max(0, int(count) - 1)
                            if used_w > fw:
                                continue
                            leftover_h = fh - int(group["h"]) - kerf
                            if leftover_h < 80:
                                continue
                            row_y = fy + leftover_h + kerf
                            extra = materialize_extra(group, count, fx, row_y, "x")
                            if not extra:
                                continue
                            free = [r for idx, r in enumerate(sheet.free or []) if idx != rect_index]
                            right_x = fx + used_w + kerf
                            if right_x < fx + fw:
                                free.append((right_x, row_y, fx + fw - right_x, int(group["h"])))
                            free.append((fx, fy, fw, leftover_h))
                            variants.append((
                                (
                                    leftover_h,
                                    fw - used_w,
                                    -int(count) * int(group["area"]),
                                    group["key"],
                                ),
                                extra,
                                free,
                            ))
            variants.sort(key=lambda item: item[0])
            for _rank, extra, free in variants[:(8 if allow_expensive else 3)]:
                placements = [dict(pl) for pl in sheet.placements or []] + [dict(pl) for pl in extra]
                new_sheet = _sheet_from_pattern(
                    int(sheet.w),
                    int(sheet.h),
                    kerf,
                    placements,
                    free,
                    "full_height_column_tail_refilled_strip",
                )
                if not _sheet_geometry_ok(new_sheet):
                    continue
                sig = (
                    _sheet_mosaic_signature(new_sheet),
                    tuple(sorted((int(pl["x"]), int(pl["y"]), int(pl["w"]), int(pl["h"])) for pl in new_sheet.placements)),
                )
                if sig in seen_sheets:
                    continue
                seen_sheets.add(sig)
                new_cand = _candidate_from_sheets([new_sheet], "v2_full_height_column_tail_refilled_strip")
                _append_candidate_variants(candidates, new_cand, allow_mirror=False)
                added += 1
                if len(candidates) >= candidate_limit or added >= (360 if allow_expensive else 40):
                    return

    extend_full_dim_strip_candidates()

    _record_strategy_metric(stats, "v2_full_height_column_tail_candidates", _elapsed_ms(started_at), len(candidates))
    return candidates


def _v2_transposed_full_height_column_tail_candidates(remaining, sheet_w, sheet_h, kerf, *, stats=None, deadline=None, allow_expensive=False):
    started_at = time.monotonic()
    if int(sheet_w) == int(sheet_h):
        return []
    pieces_by_id = {int(piece["id"]): piece for piece in remaining}
    candidates = []
    seen = set()
    raw = _v2_full_height_column_tail_candidates(
        remaining,
        int(sheet_h),
        int(sheet_w),
        kerf,
        stats=None,
        deadline=deadline,
        allow_expensive=allow_expensive,
    )
    for cand in raw:
        sheets = cand.get("sheets") or []
        if len(sheets) != 1:
            continue
        rotated = _rotate_sheet_clockwise(
            sheets[0],
            int(sheet_w),
            int(sheet_h),
            pieces_by_id=pieces_by_id,
            suffix="_long_axis",
        )
        if not _sheet_geometry_ok(rotated):
            continue
        sig = (
            _sheet_mosaic_signature(rotated),
            tuple(sorted((int(pl["x"]), int(pl["y"]), int(pl["w"]), int(pl["h"])) for pl in rotated.placements)),
        )
        if sig in seen:
            continue
        seen.add(sig)
        new_cand = _candidate_from_sheets([rotated], "v2_full_height_column_tail_long_axis")
        _append_candidate_variants(candidates, new_cand, allow_mirror=False)
    _record_strategy_metric(stats, "v2_full_height_column_tail_long_axis_candidates", _elapsed_ms(started_at), len(candidates))
    return candidates


def _v2_transposed_long_strip_tail_candidates(remaining, sheet_w, sheet_h, kerf, *, stats=None, deadline=None, allow_expensive=False):
    started_at = time.monotonic()
    if int(sheet_w) == int(sheet_h):
        return []
    pieces_by_id = {int(piece["id"]): piece for piece in remaining}
    candidates = []
    seen = set()
    raw = _v2_long_strip_tail_candidates(
        remaining,
        int(sheet_h),
        int(sheet_w),
        kerf,
        stats=None,
        deadline=deadline,
        allow_expensive=allow_expensive,
    )
    for cand in raw:
        sheets = cand.get("sheets") or []
        if len(sheets) != 1:
            continue
        rotated = _rotate_sheet_clockwise(
            sheets[0],
            int(sheet_w),
            int(sheet_h),
            pieces_by_id=pieces_by_id,
            suffix="_long_axis",
        )
        if not _sheet_geometry_ok(rotated):
            continue
        sig = (
            _sheet_mosaic_signature(rotated),
            tuple(sorted((int(pl["x"]), int(pl["y"]), int(pl["w"]), int(pl["h"])) for pl in rotated.placements)),
        )
        if sig in seen:
            continue
        seen.add(sig)
        new_cand = _candidate_from_sheets([rotated], "v2_long_strip_tail_long_axis")
        _append_candidate_variants(candidates, new_cand, allow_mirror=False)
    _record_strategy_metric(stats, "v2_long_strip_tail_long_axis_candidates", _elapsed_ms(started_at), len(candidates))
    return candidates

def _v2_strip_filler_residual_candidates(remaining, sheet_w, sheet_h, kerf, *, stats=None, deadline=None, allow_expensive=False):
    """Residual strip/filler sheets built from reusable guillotine columns.

    This corrected implementation searches components first: a repeated vertical
    stack, optionally with a filled cap, then combines those components side by
    side. It deliberately rewards components that stop short to create a useful
    filler cap, which is the general CutList pattern seen on large residual jobs.
    """
    started_at = time.monotonic()
    if len(remaining or []) < 12:
        return []

    groups = {}
    for piece in remaining or []:
        groups.setdefault(_piece_type_key(piece), []).append(piece)
    for items in groups.values():
        items.sort(key=lambda piece: int(piece["id"]))
    available = Counter({key: len(items) for key, items in groups.items()})
    sheet_area = max(1, int(sheet_w) * int(sheet_h))

    oriented = []
    seen_oriented = set()
    for key, items in sorted(groups.items()):
        for opt in _piece_orientations(items[0], sheet_w, sheet_h):
            sig = (key, int(opt["w"]), int(opt["h"]))
            if sig in seen_oriented:
                continue
            seen_oriented.add(sig)
            oriented.append({
                "key": key,
                "w": int(opt["w"]),
                "h": int(opt["h"]),
                "rotated": bool(opt["rotated"]),
                "qty": len(items),
                "area": int(key[0]) * int(key[1]),
            })
    if not oriented:
        return []

    def can_be_primary(group):
        short = min(int(group["w"]), int(group["h"]))
        long = max(int(group["w"]), int(group["h"]))
        aspect = long / float(short or 1)
        strip = int(group["w"]) >= int(sheet_w) * 0.45 and int(group["h"]) <= max(360, int(sheet_h) * 0.18)
        filler_lane = int(group["qty"]) >= 6 and int(group["w"]) <= int(sheet_w) * 0.38
        return int(group["qty"]) >= 3 and (strip or filler_lane or aspect >= 2.1)

    primary_pool = [group for group in oriented if can_be_primary(group)]
    primary_pool.sort(key=lambda group: (
        0 if int(group["w"]) >= int(sheet_w) * 0.45 and int(group["h"]) <= max(360, int(sheet_h) * 0.18) else 1,
        -min(int(group["qty"]), 24) * int(group["area"]),
        int(group["w"]),
        int(group["h"]),
        group["key"],
    ))
    filler_pool = sorted(oriented, key=lambda group: (
        0 if max(int(group["w"]), int(group["h"])) <= 360 else 1,
        -min(int(group["qty"]), 24) * int(group["area"]),
        int(group["w"]),
        int(group["h"]),
        group["key"],
    ))

    def count_choices(group):
        max_count = min(
            int(group["qty"]),
            int((int(sheet_h) + int(kerf)) // (int(group["h"]) + int(kerf))),
        )
        if max_count <= 0:
            return []
        if allow_expensive:
            return list(range(max_count, 0, -1))
        choices = {
            max_count, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 17, 20, 21,
            max_count - 1, max_count - 2, max_count - 3, max_count - 4, max_count - 5,
        }
        return sorted({int(value) for value in choices if 1 <= int(value) <= max_count}, reverse=True)

    def stack_height(group, count):
        return int(count) * int(group["h"]) + int(kerf) * max(0, int(count) - 1)

    def fill_cap(width, y0, height, used_counts):
        placements = []
        counts = Counter()
        x = 0
        while x < int(width) and not _deadline_hit(deadline, 0.002):
            room_w = int(width) - int(x)
            best = None
            for group in filler_pool[:(80 if allow_expensive else 36)]:
                if int(group["w"]) > room_w or int(group["h"]) > int(height):
                    continue
                available_count = int(available[group["key"]]) - int(used_counts[group["key"]]) - int(counts[group["key"]])
                if available_count <= 0:
                    continue
                cap = min(
                    available_count,
                    int((int(height) + int(kerf)) // (int(group["h"]) + int(kerf))),
                )
                if cap <= 0:
                    continue
                area = int(cap) * int(group["area"])
                rank = (
                    -int(area),
                    int(room_w) - int(group["w"]),
                    int(group["w"]),
                    group["key"],
                )
                if best is None or rank < best[0]:
                    best = (rank, group, cap)
            if best is None:
                break
            _rank, group, count = best
            y = int(y0)
            for _idx in range(int(count)):
                placements.append({"group": group, "x": int(x), "y": int(y)})
                y += int(group["h"]) + int(kerf)
            counts[group["key"]] += int(count)
            x += int(group["w"]) + int(kerf)
        return placements, counts

    components = []
    seen_components = set()
    for primary in primary_pool[:(64 if allow_expensive else 28)]:
        if _deadline_hit(deadline, 0.02):
            break
        for count in count_choices(primary):
            if _deadline_hit(deadline, 0.004):
                break
            used_h = stack_height(primary, count)
            if used_h > int(sheet_h):
                continue
            used_counts = Counter({primary["key"]: int(count)})
            placements = []
            y = 0
            for _idx in range(int(count)):
                placements.append({"group": primary, "x": 0, "y": int(y)})
                y += int(primary["h"]) + int(kerf)
            cap_y = int(used_h) + int(kerf)
            cap_h = int(sheet_h) - int(cap_y)
            cap_placements = []
            cap_counts = Counter()
            if cap_h >= 90:
                cap_placements, cap_counts = fill_cap(int(primary["w"]), cap_y, cap_h, used_counts)
            counts = Counter(used_counts)
            counts.update(cap_counts)
            if any(int(counts[key]) > int(available[key]) for key in counts):
                continue
            all_placements = placements + cap_placements
            area = sum(int(pl["group"]["area"]) for pl in all_placements)
            if area < sheet_area * 0.16 and (
                int(count) < 5 or int(primary["w"]) > int(sheet_w) * 0.42
            ):
                continue
            sig = (
                int(primary["w"]),
                tuple(sorted(counts.items())),
                tuple((pl["group"]["key"], int(pl["x"]), int(pl["y"])) for pl in all_placements),
            )
            if sig in seen_components:
                continue
            seen_components.add(sig)
            components.append({
                "w": int(primary["w"]),
                "area": int(area),
                "counts": counts,
                "cap_count": sum(int(value) for value in cap_counts.values()),
                "primary_count": int(count),
                "placements": all_placements,
            })

    if not components:
        _record_strategy_metric(stats, "v2_strip_filler_residual_candidates", _elapsed_ms(started_at), 0)
        return []

    components.sort(key=lambda comp: (
        0 if int(comp.get("cap_count") or 0) >= 2 else 1,
        -(int(comp.get("cap_count") or 0)),
        -int(comp["area"]),
        int(sheet_w) - int(comp["w"]),
        -sum(int(count) for count in comp["counts"].values()),
        tuple(sorted(comp["counts"].items())),
    ))

    pools = {key: list(items) for key, items in groups.items()}
    candidates = []
    seen_sheets = set()
    max_candidates = 2600 if allow_expensive else 560

    def build_sheet(comps):
        width = sum(int(comp["w"]) for comp in comps) + int(kerf) * max(0, len(comps) - 1)
        if width > int(sheet_w):
            return None
        counts = Counter()
        for comp in comps:
            counts.update(comp["counts"])
        if any(int(counts[key]) > int(available[key]) for key in counts):
            return None
        used_counts = Counter()
        materialized = []
        x = 0
        for comp in comps:
            for rel in sorted(comp["placements"], key=lambda pl: (int(pl["x"]), int(pl["y"]), pl["group"]["key"])):
                group = rel["group"]
                key = group["key"]
                idx = int(used_counts[key])
                if idx >= len(pools.get(key, [])):
                    return None
                piece = pools[key][idx]
                used_counts[key] += 1
                materialized.append({
                    "item": int(piece["id"]),
                    "x": int(x) + int(rel["x"]),
                    "y": int(rel["y"]),
                    "w": int(group["w"]),
                    "h": int(group["h"]),
                    "rotated": bool(group["rotated"]),
                })
            x += int(comp["w"]) + int(kerf)
        free = []
        right_x = int(width) + int(kerf)
        if right_x < int(sheet_w):
            free.append((right_x, 0, int(sheet_w) - right_x, int(sheet_h)))
        sheet = _sheet_from_pattern(sheet_w, sheet_h, kerf, materialized, free, "strip_filler_residual")
        return sheet if _sheet_geometry_ok(sheet) else None

    def combo_counts(comps):
        counts = Counter()
        for comp in comps:
            counts.update(comp["counts"])
        return counts

    def combo_ok(comps):
        width = sum(int(comp["w"]) for comp in comps) + int(kerf) * max(0, len(comps) - 1)
        if width > int(sheet_w):
            return False
        counts = combo_counts(comps)
        if any(int(counts[key]) > int(available[key]) for key in counts):
            return False
        area = sum(int(comp["area"]) for comp in comps)
        if width < int(sheet_w) * 0.76 and area < sheet_area * 0.72:
            return False
        return True

    def add_candidate(comps):
        if len(candidates) >= max_candidates or not combo_ok(comps):
            return
        sheet = build_sheet(comps)
        if sheet is None:
            return
        sig = (
            _sheet_mosaic_signature(sheet),
            tuple(sorted((int(pl["x"]), int(pl["y"]), int(pl["w"]), int(pl["h"])) for pl in sheet.placements)),
        )
        if sig in seen_sheets:
            return
        seen_sheets.add(sig)
        cand = _candidate_from_sheets([sheet], "v2_strip_filler_residual")
        _append_candidate_variants(candidates, cand, allow_mirror=False)

    def combo_rank(comps):
        width = sum(int(comp["w"]) for comp in comps) + int(kerf) * max(0, len(comps) - 1)
        area = sum(int(comp["area"]) for comp in comps)
        cap_count = sum(int(comp.get("cap_count") or 0) for comp in comps)
        panels = sum(sum(int(value) for value in comp["counts"].values()) for comp in comps)
        return (
            0 if cap_count >= 2 else 1,
            int(sheet_w) - int(width),
            -int(area),
            -int(cap_count),
            -int(panels),
            tuple(tuple(sorted(comp["counts"].items())) for comp in comps),
        )

    pool = components[:(420 if allow_expensive else 140)]
    for comp in pool:
        if _deadline_hit(deadline, 0.02):
            break
        add_candidate([comp])

    combos = []
    for left in pool[:(240 if allow_expensive else 80)]:
        if _deadline_hit(deadline, 0.02):
            break
        for right in pool[:(320 if allow_expensive else 90)]:
            if _deadline_hit(deadline, 0.006):
                break
            if left is right:
                continue
            pair = [left, right]
            if combo_ok(pair):
                combos.append((combo_rank(pair), pair))
    combos.sort(key=lambda item: item[0])
    for _rank, comps in combos[:(1200 if allow_expensive else 260)]:
        if _deadline_hit(deadline, 0.02):
            break
        add_candidate(comps)

    triples = []
    for first in pool[:(130 if allow_expensive else 36)]:
        if _deadline_hit(deadline, 0.02):
            break
        for second in pool[:(150 if allow_expensive else 42)]:
            if _deadline_hit(deadline, 0.006):
                break
            if second is first:
                continue
            used_w = int(first["w"]) + int(second["w"]) + int(kerf)
            remaining_w = int(sheet_w) - used_w - int(kerf)
            if remaining_w <= 0:
                continue
            compatible = [comp for comp in pool[:(220 if allow_expensive else 70)] if comp is not first and comp is not second and int(comp["w"]) <= remaining_w]
            compatible.sort(key=lambda comp: (remaining_w - int(comp["w"]), -int(comp["area"])))
            for third in compatible[:(16 if allow_expensive else 5)]:
                triple = [first, second, third]
                if combo_ok(triple):
                    triples.append((combo_rank(triple), triple))
    triples.sort(key=lambda item: item[0])
    for _rank, comps in triples[:(900 if allow_expensive else 160)]:
        if _deadline_hit(deadline, 0.02):
            break
        add_candidate(comps)
        add_candidate(list(reversed(comps)))
        if len(candidates) >= max_candidates:
            break

    _record_strategy_metric(stats, "v2_strip_filler_residual_candidates", _elapsed_ms(started_at), len(candidates))
    return candidates


def _v2_transposed_strip_filler_residual_candidates(remaining, sheet_w, sheet_h, kerf, *, stats=None, deadline=None, allow_expensive=False):
    started_at = time.monotonic()
    if int(sheet_w) == int(sheet_h):
        return []
    pieces_by_id = {int(piece["id"]): piece for piece in remaining}
    candidates = []
    seen = set()
    raw = _v2_strip_filler_residual_candidates(
        remaining,
        int(sheet_h),
        int(sheet_w),
        kerf,
        stats=None,
        deadline=deadline,
        allow_expensive=allow_expensive,
    )
    for cand in raw:
        sheets = cand.get("sheets") or []
        if len(sheets) != 1:
            continue
        rotated = _rotate_sheet_clockwise(
            sheets[0],
            int(sheet_w),
            int(sheet_h),
            pieces_by_id=pieces_by_id,
            suffix="_long_axis",
        )
        if not _sheet_geometry_ok(rotated):
            continue
        sig = (
            _sheet_mosaic_signature(rotated),
            tuple(sorted((int(pl["x"]), int(pl["y"]), int(pl["w"]), int(pl["h"])) for pl in rotated.placements)),
        )
        if sig in seen:
            continue
        seen.add(sig)
        new_cand = _candidate_from_sheets([rotated], "v2_strip_filler_residual_long_axis")
        _append_candidate_variants(candidates, new_cand, allow_mirror=False)
    _record_strategy_metric(stats, "v2_strip_filler_residual_long_axis_candidates", _elapsed_ms(started_at), len(candidates))
    return candidates


def _v2_count_complete_sequence(pieces, sheet_w, sheet_h, kerf, *, target, deadline=None):
    """Sequence v2 sheet recipes by remaining piece-size counts.

    Individual panel ids are noise for the global decision: CutList-like planning
    cares that a candidate consumes, say, 7 of 330x640 and 3 of 100x170. This
    planner searches on those counts, then assigns concrete ids once a complete
    recipe sequence has been chosen.
    """
    pieces_by_key = {}
    for piece in pieces or []:
        pieces_by_key.setdefault(_piece_type_key(piece), []).append(dict(piece))
    for items in pieces_by_key.values():
        items.sort(key=lambda piece: int(piece["id"]))
    initial_counts = Counter({key: len(items) for key, items in pieces_by_key.items()})
    total_by_key = Counter(initial_counts)
    area_by_key = {key: int(key[0]) * int(key[1]) for key in total_by_key}
    sheet_area = max(1, int(sheet_w) * int(sheet_h))
    if not initial_counts:
        return [], list(pieces)

    def count_key(counts):
        return tuple((key, int(count)) for key, count in sorted(counts.items()) if int(count) > 0)

    def counts_area(counts):
        return sum(int(key[0]) * int(key[1]) * int(count) for key, count in counts.items())

    def counts_lower_bound(counts):
        area = counts_area(counts)
        return int((area + sheet_area - 1) // sheet_area) if area else 0

    def materialize(counts):
        items = []
        for key, count in sorted(counts.items()):
            if int(count) <= 0:
                continue
            items.extend(pieces_by_key[key][:int(count)])
        return [dict(item) for item in items]

    def sheet_counts(sheet):
        counts = Counter()
        for pl in sheet.placements or []:
            counts[tuple(sorted((int(pl["w"]), int(pl["h"]))))] += 1
        return counts

    def subtract_counts(counts, used):
        remaining = Counter(counts)
        for key, count in used.items():
            remaining[key] -= int(count)
            if remaining[key] < 0:
                return None
            if remaining[key] == 0:
                del remaining[key]
        return remaining

    def placement_shape_signature(sheet):
        return tuple(sorted(
            (
                int(pl["x"]),
                int(pl["y"]),
                int(pl["w"]),
                int(pl["h"]),
            )
            for pl in sheet.placements or []
        ))

    def assign_piece_ids(template_sheets):
        pools = {key: list(items) for key, items in pieces_by_key.items()}
        assigned = []
        for sheet in template_sheets:
            clone = _clone_sheet(sheet)
            for pl in clone.placements:
                key = tuple(sorted((int(pl["w"]), int(pl["h"]))))
                if not pools.get(key):
                    return [], list(pieces)
                piece = pools[key].pop(0)
                pl["item"] = int(piece["id"])
                pl["rotated"] = int(pl["w"]) != int(piece["w"]) or int(pl["h"]) != int(piece["h"])
            assigned.append(clone)
        if any(pools[key] for key in pools):
            return [], list(pieces)
        if not all(_sheet_geometry_ok(sheet) for sheet in assigned):
            return [], list(pieces)
        return assigned, []

    def template_sheet(sheet):
        clone = _clone_sheet(sheet)
        for idx, pl in enumerate(clone.placements):
            pl["item"] = -1000000 - idx
        return clone

    portfolio_cache = {}

    def candidate_portfolio(counts, slots):
        key = (count_key(counts), int(slots))
        if key in portfolio_cache:
            return portfolio_cache[key]
        remaining = materialize(counts)
        lb = counts_lower_bound(counts)
        tight_large_job = bool(len(remaining) >= 180 and lb >= max(1, int(slots) - 1))
        portfolio_deadline = deadline
        if deadline:
            per_call = 1.0
            if tight_large_job:
                per_call = 12.0
            if lb <= 15 or slots <= 15 or len(remaining) <= 260:
                per_call = max(per_call, 6.0)
            if lb <= 10 or slots <= 10 or len(remaining) <= 140:
                per_call = max(per_call, 10.0)
            portfolio_deadline = min(float(deadline) - 0.20, time.monotonic() + per_call)
            if portfolio_deadline <= time.monotonic():
                return []

        raw = []
        raw.extend(_v2_guillotine_mosaic_candidates(
            remaining,
            sheet_w,
            sheet_h,
            kerf,
            deadline=portfolio_deadline,
            allow_expensive=False,
        ))
        if (tight_large_job or lb <= 15 or slots <= 15 or len(remaining) <= 260) and not _deadline_hit(portfolio_deadline, 0.02):
            raw.extend(_v2_transposed_lane_run_candidates(
                remaining,
                sheet_w,
                sheet_h,
                kerf,
                deadline=portfolio_deadline,
                allow_expensive=bool(tight_large_job or lb <= 13 or slots <= 13),
            ))
        if (slots <= 6 or len(remaining) <= 140) and not _deadline_hit(portfolio_deadline, 0.02):
            raw.extend(_v2_transposed_exact_lane_partition_candidates(
                remaining,
                sheet_w,
                sheet_h,
                kerf,
                deadline=portfolio_deadline,
                allow_expensive=bool(slots <= 4 or len(remaining) <= 100),
            ))
        if (slots <= 5 or len(remaining) <= 120) and not _deadline_hit(portfolio_deadline, 0.02):
            raw.extend(_v2_exact_lane_partition_candidates(
                remaining,
                sheet_w,
                sheet_h,
                kerf,
                deadline=portfolio_deadline,
                allow_expensive=bool(slots <= 4 or len(remaining) <= 90),
            ))
        if (tight_large_job or lb <= 12 or slots <= 12 or len(remaining) <= 200) and not _deadline_hit(portfolio_deadline, 0.02):
            raw.extend(_v2_lane_run_candidates(
                remaining,
                sheet_w,
                sheet_h,
                kerf,
                deadline=portfolio_deadline,
                allow_expensive=bool(tight_large_job or lb <= 10 or slots <= 10),
            ))
        if (tight_large_job or lb <= 15 or slots <= 15 or len(remaining) <= 260) and not _deadline_hit(portfolio_deadline, 0.02):
            raw.extend(_v2_transposed_simple_lane_pair_candidates(
                remaining,
                sheet_w,
                sheet_h,
                kerf,
                deadline=portfolio_deadline,
                allow_expensive=bool(tight_large_job or lb <= 12 or slots <= 12),
            ))
        if (tight_large_job or lb <= 10 or slots <= 10 or len(remaining) <= 160) and not _deadline_hit(portfolio_deadline, 0.02):
            raw.extend(_v2_simple_lane_pair_candidates(
                remaining,
                sheet_w,
                sheet_h,
                kerf,
                deadline=portfolio_deadline,
                allow_expensive=bool(tight_large_job),
            ))
        if (tight_large_job or lb <= 13 or slots <= 13 or len(remaining) <= 220) and not _deadline_hit(portfolio_deadline, 0.02):
            raw.extend(_v2_transposed_lane_recipe_candidates(
                remaining,
                sheet_w,
                sheet_h,
                kerf,
                deadline=portfolio_deadline,
                allow_expensive=bool(tight_large_job or lb <= 12 or slots <= 12),
            ))
        if (tight_large_job or lb <= 13 or slots <= 13 or len(remaining) <= 220) and not _deadline_hit(portfolio_deadline, 0.02):
            raw.extend(_v2_lane_recipe_candidates(
                remaining,
                sheet_w,
                sheet_h,
                kerf,
                deadline=portfolio_deadline,
                allow_expensive=bool(tight_large_job or lb <= 12 or slots <= 12),
            ))
        if (tight_large_job or lb <= 13 or slots <= 13 or len(remaining) <= 220) and not _deadline_hit(portfolio_deadline, 0.02):
            raw.extend(_v2_transposed_column_stack_candidates(
                remaining,
                sheet_w,
                sheet_h,
                kerf,
                deadline=portfolio_deadline,
                allow_expensive=bool(tight_large_job or lb <= 12 or slots <= 12),
            ))
        if (tight_large_job or lb <= 8 or slots <= 8 or len(remaining) <= 110) and not _deadline_hit(portfolio_deadline, 0.02):
            raw.extend(_v2_column_stack_candidates(
                remaining,
                sheet_w,
                sheet_h,
                kerf,
                deadline=portfolio_deadline,
                allow_expensive=bool(tight_large_job),
            ))
        if (tight_large_job or len(remaining) <= 220) and not _deadline_hit(portfolio_deadline, 0.02):
            raw.extend(_v2_group_mosaic_candidates(
                remaining,
                sheet_w,
                sheet_h,
                kerf,
                deadline=portfolio_deadline,
                allow_expensive=bool(tight_large_job),
            ))
        if (slots <= 5 or len(remaining) <= 95) and not _deadline_hit(portfolio_deadline, 0.02):
            raw.extend(_v2_repeat_sheet_candidates(
                remaining,
                sheet_w,
                sheet_h,
                kerf,
                deadline=portfolio_deadline,
                allow_top_aligned=True,
            ))

        candidates = []
        seen = set()
        for cand in raw:
            sheets = cand.get("sheets") or []
            if len(sheets) != 1:
                continue
            sheet = sheets[0]
            used = sheet_counts(sheet)
            if not used:
                continue
            if any(int(used[key]) > int(counts.get(key, 0)) for key in used):
                continue
            sig = (
                tuple(sorted(used.items())),
                getattr(sheet, "strategy", "") or cand.get("strategy") or "",
                placement_shape_signature(sheet),
            )
            if sig in seen:
                continue
            seen.add(sig)
            candidates.append({
                "sheet": template_sheet(sheet),
                "used": used,
                "strategy": getattr(sheet, "strategy", "") or cand.get("strategy") or "",
            })
        portfolio_cache[key] = candidates
        return candidates

    def used_area(sheet):
        return sum(int(pl["w"]) * int(pl["h"]) for pl in sheet.placements or [])

    def candidate_rank(candidate, state, target_sheets):
        used = candidate["used"]
        remaining = subtract_counts(state["counts"], used)
        if remaining is None:
            return None
        future = len(state["sheets"]) + 1 + counts_lower_bound(remaining)
        if future > target_sheets:
            return None

        progress = len(state["sheets"]) / float(max(1, target_sheets))
        early_strip_spend = 0
        preserve_penalty = 0
        rare_anchor_area = 0
        early_repeat_lane_area = 0
        for key, count in used.items():
            total_count = int(total_by_key.get(key) or 0)
            after = int(remaining.get(key) or 0)
            key_area = int(area_by_key.get(key) or 0)
            if progress < 0.35 and total_count <= 9 and key_area >= sheet_area * 0.06:
                rare_anchor_area += key_area * int(count)
            if progress < 0.25 and total_count >= 12 and key_area >= sheet_area * 0.04:
                early_repeat_lane_area += key_area * int(count)
            if progress < 0.60 and min(int(key[0]), int(key[1])) <= 180:
                early_strip_spend += int(count)
            if progress < 0.70 and total_count >= 12 and min(int(key[0]), int(key[1])) <= 220:
                min_keep = min(8, max(2, total_count // 5))
                if after < min_keep:
                    preserve_penalty += (min_keep - after) * 3
            elif progress < 0.50 and total_count >= 6 and int(area_by_key.get(key) or 0) >= sheet_area * 0.04:
                if after < 1:
                    preserve_penalty += 2

        sheet = candidate["sheet"]
        signature = _sheet_mosaic_signature(sheet)
        repeated_signature = int(state["signature_counts"].get(signature) or 0)
        area_used = max(1, used_area(sheet))
        simple_cuts = _guillotine_tree_cut_count(sheet)
        strategy = candidate.get("strategy") or getattr(sheet, "strategy", "")
        strategy_rank = (
            0 if "lane_run" in strategy
            else 1 if "simple_lane_pair" in strategy
            else 2 if "column_stack" in strategy
            else 3
        )
        return (
            future,
            repeated_signature >= 2,
            -int(rare_anchor_area),
            int(early_repeat_lane_area),
            early_strip_spend,
            preserve_penalty,
            -area_used,
            strategy_rank,
            int(simple_cuts) * 1000000 // area_used,
            -sum(used.values()),
            strategy,
        )

    def select_ranked_candidates(ranked, limit):
        """Keep good candidates without letting one family monopolise the beam."""
        if not ranked:
            return []
        limit = max(1, int(limit))
        selected = []
        seen = set()

        def candidate_sig(candidate):
            return (
                tuple(sorted(candidate["used"].items())),
                candidate.get("strategy") or getattr(candidate["sheet"], "strategy", ""),
            )

        def keep(source, max_items):
            for item in source:
                _rank, candidate = item
                sig = candidate_sig(candidate)
                if sig in seen:
                    continue
                seen.add(sig)
                selected.append(item)
                if len(selected) >= max_items:
                    return True
            return len(selected) >= max_items

        keep(ranked, max(1, limit // 3))

        family_buckets = {}
        strategy_buckets = {}
        for item in ranked:
            _rank, candidate = item
            used = candidate["used"]
            if used:
                by_individual_anchor = max(
                    used,
                    key=lambda key: (
                        int(key[0]) * int(key[1]),
                        int(used[key]),
                        key,
                    ),
                )
                by_spend = max(
                    used,
                    key=lambda key: (
                        int(key[0]) * int(key[1]) * int(used[key]),
                        int(key[0]) * int(key[1]),
                        key,
                    ),
                )
                family_buckets.setdefault(("anchor", by_individual_anchor), []).append(item)
                family_buckets.setdefault(("spend", by_spend), []).append(item)
                for key in used:
                    if int(key[0]) * int(key[1]) >= sheet_area * 0.04:
                        family_buckets.setdefault(("has", key), []).append(item)
            strategy = candidate.get("strategy") or getattr(candidate["sheet"], "strategy", "")
            strategy_buckets.setdefault(strategy, []).append(item)

        for family in sorted(family_buckets):
            if keep(family_buckets[family][:3], limit):
                return selected
        for strategy in sorted(strategy_buckets):
            if keep(strategy_buckets[strategy][:4], limit):
                return selected
        keep(ranked, limit)
        return selected[:limit]

    def state_rank(state, target_sheets):
        slots = int(target_sheets) - len(state["sheets"])
        lb = counts_lower_bound(state["counts"])
        progress = len(state["sheets"]) / float(max(1, target_sheets))
        tail_stock = sum(
            int(count)
            for key, count in state["counts"].items()
            if min(int(key[0]), int(key[1])) <= 220
        )
        return (
            lb > slots,
            slots - lb,
            -tail_stock if progress < 0.75 else 0,
            counts_area(state["counts"]),
            int(state.get("_order") or 0),
        )

    def two_sheet_tail_completion(state, ranked, target_sheets):
        """Look one sheet ahead when only the current sheet + tail remain."""
        slots = int(target_sheets) - len(state["sheets"])
        if slots != 2 or not ranked:
            return None
        best = None
        best_score = None
        best_full_dim = -1
        selected = select_ranked_candidates(ranked, min(160, max(24, candidate_keep * 2)))
        for _rank, candidate in selected:
            if _deadline_hit(deadline, 0.05):
                break
            remaining = subtract_counts(state["counts"], candidate["used"])
            if remaining is None:
                continue
            first_sheet = _clone_sheet(candidate["sheet"])
            if not remaining:
                assigned, unplaced = assign_piece_ids(list(state["sheets"]) + [first_sheet])
                if not assigned or unplaced:
                    continue
                sc = score(assigned, [])
                full = int(_best_offcut_info(assigned, full_dim_only=True).get("value") or 0)
                if best is None or (sc, -full) < (best_score, -best_full_dim):
                    best = assigned
                    best_score = sc
                    best_full_dim = full
                continue
            final_options = candidate_portfolio(remaining, 1)
            for final_candidate in final_options:
                if _deadline_hit(deadline, 0.02):
                    break
                tail_remaining = subtract_counts(remaining, final_candidate["used"])
                if tail_remaining is None or tail_remaining:
                    continue
                template = list(state["sheets"]) + [first_sheet, _clone_sheet(final_candidate["sheet"])]
                assigned, unplaced = assign_piece_ids(template)
                if not assigned or unplaced:
                    continue
                sc = score(assigned, [])
                full = int(_best_offcut_info(assigned, full_dim_only=True).get("value") or 0)
                if best is None or (sc, -full) < (best_score, -best_full_dim):
                    best = assigned
                    best_score = sc
                    best_full_dim = full
        return best

    states = [{
        "sheets": [],
        "counts": Counter(initial_counts),
        "signature_counts": Counter(),
        "_order": 0,
    }]
    order = 0
    best_complete = None
    best_score = None
    state_beam = 48 if len(pieces) >= 120 else 8
    candidate_keep = 72 if len(pieces) >= 120 else 8

    for _sheet_idx in range(int(target)):
        if _deadline_hit(deadline, 0.05):
            break
        next_states = {}
        for state in states:
            if _deadline_hit(deadline, 0.05):
                break
            if not state["counts"]:
                assigned, unplaced = assign_piece_ids(state["sheets"])
                if assigned and not unplaced:
                    sc = score(assigned, [])
                    if best_complete is None or sc < best_score:
                        best_complete = assigned
                        best_score = sc
                continue
            slots = int(target) - len(state["sheets"])
            if counts_lower_bound(state["counts"]) > slots:
                continue
            ranked = []
            for candidate in candidate_portfolio(state["counts"], slots):
                rank = candidate_rank(candidate, state, int(target))
                if rank is not None:
                    ranked.append((rank, candidate))
            ranked.sort(key=lambda item: item[0])
            tail_complete = two_sheet_tail_completion(state, ranked, int(target))
            if tail_complete:
                sc = score(tail_complete, [])
                if best_complete is None or sc < best_score:
                    best_complete = tail_complete
                    best_score = sc
                continue
            for _rank, candidate in select_ranked_candidates(ranked, candidate_keep):
                remaining = subtract_counts(state["counts"], candidate["used"])
                if remaining is None:
                    continue
                sheet = _clone_sheet(candidate["sheet"])
                new_sheets = [_clone_sheet(s) for s in state["sheets"]] + [sheet]
                signature_counts = Counter(state["signature_counts"])
                signature_counts[_sheet_mosaic_signature(sheet)] += 1
                order += 1
                new_state = {
                    "sheets": new_sheets,
                    "counts": remaining,
                    "signature_counts": signature_counts,
                    "_order": order,
                }
                if not remaining:
                    assigned, unplaced = assign_piece_ids(new_sheets)
                    if assigned and not unplaced:
                        sc = score(assigned, [])
                        if best_complete is None or sc < best_score:
                            best_complete = assigned
                            best_score = sc
                    continue
                key = (count_key(remaining), len(new_sheets))
                rank = state_rank(new_state, int(target))
                previous = next_states.get(key)
                if previous is None or rank < previous[0]:
                    next_states[key] = (rank, new_state)
        if best_complete is not None:
            break
        if not next_states:
            break
        states = [
            state for _rank, state in sorted(next_states.values(), key=lambda item: item[0])[:state_beam]
        ]

    if not best_complete:
        return [], list(pieces)
    for idx, sheet in enumerate(best_complete, 1):
        sheet.strategy = "count_sequence_%02d_%s" % (
            idx,
            getattr(sheet, "strategy", "") or "pattern",
        )
    return best_complete, []


def _v2_fast_complete_sequence(pieces, sheet_w, sheet_h, kerf, *, target, deadline=None):
    """Fast complete-layout sequencer over v2 one-sheet pattern candidates."""
    pieces_by_id = {int(piece["id"]): dict(piece) for piece in pieces}
    all_ids = tuple(sorted(pieces_by_id))
    sheet_area = max(1, int(sheet_w) * int(sheet_h))
    total_by_key = Counter(_piece_type_key(piece) for piece in pieces)
    area_by_key = {key: int(key[0]) * int(key[1]) for key in total_by_key}

    if len(pieces_by_id) != len(pieces) or not all_ids:
        return [], list(pieces)

    def used_area(sheet):
        return sum(int(pl["w"]) * int(pl["h"]) for pl in sheet.placements or [])

    def items_for(ids):
        return [pieces_by_id[item_id] for item_id in ids]

    def remaining_counter(ids):
        return Counter(_piece_type_key(pieces_by_id[item_id]) for item_id in ids)

    def candidate_portfolio(remaining_ids, slots):
        remaining = items_for(remaining_ids)
        lb = _area_lower_bound(remaining, sheet_w, sheet_h)
        allow_columns = lb <= 15 or slots <= 15 or len(remaining_ids) <= 240
        allow_expensive_columns = lb <= 12 or slots <= 12 or len(remaining_ids) <= 180
        portfolio_deadline = deadline
        if deadline:
            reserve = 0.15
            per_call = 1.50 if not allow_columns else (4.0 if not allow_expensive_columns else 7.0)
            portfolio_deadline = min(float(deadline) - reserve, time.monotonic() + per_call)
            if portfolio_deadline <= time.monotonic():
                return []

        candidates = []
        candidates.extend(_v2_guillotine_mosaic_candidates(
            remaining,
            sheet_w,
            sheet_h,
            kerf,
            deadline=portfolio_deadline,
                allow_expensive=False,
            ))
        if allow_columns and not _deadline_hit(portfolio_deadline, 0.02):
            candidates.extend(_v2_transposed_lane_run_candidates(
                remaining,
                sheet_w,
                sheet_h,
                kerf,
                deadline=portfolio_deadline,
                allow_expensive=allow_expensive_columns,
            ))
        if (slots <= 6 or len(remaining_ids) <= 140) and not _deadline_hit(portfolio_deadline, 0.02):
            candidates.extend(_v2_transposed_exact_lane_partition_candidates(
                remaining,
                sheet_w,
                sheet_h,
                kerf,
                deadline=portfolio_deadline,
                allow_expensive=bool(slots <= 4 or len(remaining_ids) <= 100),
            ))
        if (slots <= 5 or len(remaining_ids) <= 120) and not _deadline_hit(portfolio_deadline, 0.02):
            candidates.extend(_v2_exact_lane_partition_candidates(
                remaining,
                sheet_w,
                sheet_h,
                kerf,
                deadline=portfolio_deadline,
                allow_expensive=bool(slots <= 4 or len(remaining_ids) <= 90),
            ))
        if allow_expensive_columns and not _deadline_hit(portfolio_deadline, 0.02):
            candidates.extend(_v2_lane_run_candidates(
                remaining,
                sheet_w,
                sheet_h,
                kerf,
                deadline=portfolio_deadline,
                allow_expensive=False,
            ))
        if allow_columns and not _deadline_hit(portfolio_deadline, 0.02):
            candidates.extend(_v2_transposed_simple_lane_pair_candidates(
                remaining,
                sheet_w,
                sheet_h,
                kerf,
                deadline=portfolio_deadline,
                allow_expensive=allow_expensive_columns,
            ))
        if allow_columns and not _deadline_hit(portfolio_deadline, 0.02):
            candidates.extend(_v2_simple_lane_pair_candidates(
                remaining,
                sheet_w,
                sheet_h,
                kerf,
                deadline=portfolio_deadline,
                allow_expensive=allow_expensive_columns,
            ))
        if allow_columns and not _deadline_hit(portfolio_deadline, 0.02):
            candidates.extend(_v2_transposed_lane_recipe_candidates(
                remaining,
                sheet_w,
                sheet_h,
                kerf,
                deadline=portfolio_deadline,
                allow_expensive=allow_expensive_columns,
            ))
        if allow_columns and not _deadline_hit(portfolio_deadline, 0.02):
            candidates.extend(_v2_lane_recipe_candidates(
                remaining,
                sheet_w,
                sheet_h,
                kerf,
                deadline=portfolio_deadline,
                allow_expensive=allow_expensive_columns,
            ))
        if allow_columns and not _deadline_hit(portfolio_deadline, 0.02):
            candidates.extend(_v2_transposed_column_stack_candidates(
                remaining,
                sheet_w,
                sheet_h,
                kerf,
                deadline=portfolio_deadline,
                allow_expensive=allow_expensive_columns,
            ))
        if allow_columns and not _deadline_hit(portfolio_deadline, 0.02):
            candidates.extend(_v2_column_stack_candidates(
                remaining,
                sheet_w,
                sheet_h,
                kerf,
                deadline=portfolio_deadline,
                allow_expensive=allow_expensive_columns,
            ))
        if (slots <= 6 or len(remaining_ids) <= 110) and not _deadline_hit(portfolio_deadline, 0.02):
            candidates.extend(_v2_repeat_sheet_candidates(
                remaining,
                sheet_w,
                sheet_h,
                kerf,
                deadline=portfolio_deadline,
                allow_top_aligned=True,
            ))
        return _dedupe_candidates(candidates, set(remaining_ids))

    def candidate_rank(cand, state, target_sheets):
        sheets = cand.get("sheets") or []
        if len(sheets) != 1:
            return None
        sheet = sheets[0]
        used = set(cand.get("used") or ())
        if not used or not used.issubset(state["remaining"]):
            return None
        new_remaining = tuple(sorted(state["remaining"] - used))
        remaining = items_for(new_remaining)
        future = len(state["sheets"]) + 1 + _area_lower_bound(remaining, sheet_w, sheet_h)
        if future > target_sheets:
            return None

        count_by_key = Counter(
            tuple(sorted((int(pl["w"]), int(pl["h"]))))
            for pl in sheet.placements or []
        )
        remaining_by_key = remaining_counter(new_remaining)
        progress = len(state["sheets"]) / float(max(1, target_sheets))
        early_strip_spend = 0
        preserve_penalty = 0
        for key, count in count_by_key.items():
            total_count = int(total_by_key.get(key) or 0)
            if progress < 0.60 and min(int(key[0]), int(key[1])) <= 180:
                early_strip_spend += int(count)
            if total_count < 4 or int(area_by_key.get(key) or 0) < sheet_area * 0.04:
                continue
            after = int(remaining_by_key.get(key) or 0)
            min_keep = 1 if progress < 0.50 else 0
            if after < min_keep:
                preserve_penalty += min_keep - after

        signature = _sheet_mosaic_signature(sheet)
        repeated_signature = int(state["signature_counts"].get(signature) or 0)
        area_used = max(1, used_area(sheet))
        simple_cuts = _guillotine_tree_cut_count(sheet)
        strategy = getattr(sheet, "strategy", "") or cand.get("strategy") or ""
        strategy_rank = 0 if "column_stack" in strategy else (1 if "guillotine" in strategy else 2)
        return (
            future,
            repeated_signature >= 2,
            early_strip_spend,
            preserve_penalty,
            -area_used,
            strategy_rank,
            int(simple_cuts) * 1000000 // area_used,
            -len(count_by_key),
            -len(used),
            strategy,
        )

    def state_rank(state, target_sheets):
        remaining = items_for(tuple(sorted(state["remaining"])))
        slots = target_sheets - len(state["sheets"])
        lb = _area_lower_bound(remaining, sheet_w, sheet_h)
        remaining_area = sum(_area(piece) for piece in remaining)
        repeated = sum(max(0, int(count) - 2) for count in state["signature_counts"].values())
        partial = score(state["sheets"], []) if state["sheets"] else (0, 0, 0, 0)
        return (
            lb > slots,
            slots - lb,
            remaining_area,
            repeated,
            partial[1],
            partial[2],
            int(state.get("_order") or 0),
        )

    order = 0
    initial = {
        "sheets": [],
        "remaining": set(all_ids),
        "signature_counts": Counter(),
        "_order": 0,
    }
    states = [initial]
    best_complete = None
    best_score = None
    state_beam = 8 if len(pieces) >= 120 else 8
    candidate_keep = 10 if len(pieces) >= 120 else 8

    for _sheet_idx in range(int(target)):
        if _deadline_hit(deadline, 0.05):
            break
        next_states = {}
        for state in states:
            if _deadline_hit(deadline, 0.05):
                break
            if not state["remaining"]:
                sc = score(state["sheets"], [])
                if best_complete is None or sc < best_score:
                    best_complete = [_clone_sheet(sheet) for sheet in state["sheets"]]
                    best_score = sc
                continue
            slots = int(target) - len(state["sheets"])
            remaining_ids = tuple(sorted(state["remaining"]))
            if _area_lower_bound(items_for(remaining_ids), sheet_w, sheet_h) > slots:
                continue

            candidates = candidate_portfolio(remaining_ids, slots)
            ranked = []
            for cand in candidates:
                rank = candidate_rank(cand, state, int(target))
                if rank is not None:
                    ranked.append((rank, cand))
            ranked.sort(key=lambda item: item[0])
            for _rank, cand in ranked[:candidate_keep]:
                used = set(cand["used"])
                new_remaining = set(state["remaining"]) - used
                new_sheets = [_clone_sheet(sheet) for sheet in state["sheets"]] + [
                    _clone_sheet(sheet) for sheet in cand["sheets"]
                ]
                signature_counts = Counter(state["signature_counts"])
                for sheet in cand["sheets"]:
                    signature_counts[_sheet_mosaic_signature(sheet)] += 1
                order += 1
                new_state = {
                    "sheets": new_sheets,
                    "remaining": new_remaining,
                    "signature_counts": signature_counts,
                    "_order": order,
                }
                if not new_remaining:
                    sc = score(new_sheets, [])
                    if best_complete is None or sc < best_score:
                        best_complete = [_clone_sheet(sheet) for sheet in new_sheets]
                        best_score = sc
                    continue
                key = (tuple(sorted(new_remaining)), len(new_sheets))
                rank = state_rank(new_state, int(target))
                previous = next_states.get(key)
                if previous is None or rank < previous[0]:
                    next_states[key] = (rank, new_state)

        if best_complete is not None:
            break
        if not next_states:
            break
        states = [
            state for _rank, state in sorted(next_states.values(), key=lambda item: item[0])[:state_beam]
        ]

    if best_complete is None:
        return [], list(pieces)
    used_ids = _ids_in_sheets(best_complete)
    if not used_ids or len(used_ids) != len(pieces):
        return [], list(pieces)
    if not all(_sheet_geometry_ok(sheet) for sheet in best_complete):
        return [], list(pieces)
    for idx, sheet in enumerate(best_complete, 1):
        sheet.strategy = "mosaic_sequence_%02d_%s" % (
            idx,
            getattr(sheet, "strategy", "") or "pattern",
        )
    return best_complete, []


def construct_mosaic_complete_planner(pieces, sheet_w, sheet_h, *, kerf=3, seed=0, deadline=None):
    """Globally sequence one-sheet guillotine mosaics into a complete layout.

    The ordinary guillotine packer is very good at "what is the next tightest
    piece?", but CutList-style results come from a wider question: "which whole
    sheet pattern should I spend next, while still leaving enough inventory to
    make clean later patterns?". This planner keeps that global target explicit.

    It does not replay external layouts. Every candidate sheet is produced by
    the local guillotine kernel, then accepted only if the remaining inventory
    can still fit inside the target sheet count by area lower bound.
    """
    if not pieces or len(pieces) < 20:
        return [], list(pieces)

    started_at = time.monotonic()
    local_budget = 180.0 if len(pieces) >= 120 else 6.0
    local_deadline = started_at + local_budget
    if deadline:
        deadline = min(float(deadline), local_deadline)
    else:
        deadline = local_deadline
    pieces = [dict(p) for p in pieces]
    pieces_by_id = {int(p["id"]): p for p in pieces}
    if len(pieces_by_id) != len(pieces):
        return [], list(pieces)

    sheet_area = max(1, int(sheet_w) * int(sheet_h))
    total_area = sum(int(_area(p)) for p in pieces)
    min_sheets = max(1, _area_lower_bound(pieces, sheet_w, sheet_h))
    density_at_min = total_area / float(max(1, min_sheets * sheet_area))
    preferred = min_sheets + (1 if len(pieces) >= 30 and density_at_min >= 0.88 else 0)
    targets = []
    for target in (preferred, min_sheets, preferred + 1, min_sheets + 2):
        if target >= min_sheets and target not in targets:
            targets.append(target)

    total_by_key = Counter(_piece_type_key(p) for p in pieces)
    area_by_key = {key: int(key[0]) * int(key[1]) for key in total_by_key}
    all_ids = tuple(sorted(pieces_by_id))

    if len(pieces) >= 80:
        incumbent_sheets, incumbent_unplaced = construct_guillotine_baf(
            pieces,
            sheet_w,
            sheet_h,
            kerf=kerf,
            seed=0,
            deadline=min(float(deadline) - 0.10, time.monotonic() + 4.0) if deadline else time.monotonic() + 4.0,
        )
        if incumbent_sheets and not incumbent_unplaced and all(_sheet_geometry_ok(s) for s in incumbent_sheets):
            incumbent = [_clone_sheet(s) for s in incumbent_sheets]
        else:
            incumbent = None
        if incumbent and deadline and float(deadline) - time.monotonic() < 8.0:
            return incumbent, []
        for target in targets:
            if _deadline_hit(deadline, 0.05):
                break
            sheets, unplaced = _v2_count_complete_sequence(
                pieces,
                sheet_w,
                sheet_h,
                kerf,
                target=target,
                deadline=deadline,
            )
            if sheets and not unplaced:
                return sheets, []
            sheets, unplaced = _v2_fast_complete_sequence(
                pieces,
                sheet_w,
                sheet_h,
                kerf,
                target=target,
                deadline=deadline,
            )
            if sheets and not unplaced:
                return sheets, []
        if incumbent:
            return incumbent, []
        return [], list(pieces)

    # Keep the global planner deliberately bounded. Large jobs benefit most from
    # this pass, but they also cannot afford a wide exact-cover search in preview.
    state_beam = 4 if len(pieces) >= 120 else 6
    candidate_keep = 5 if len(pieces) >= 120 else 10

    def used_area(sheet):
        return sum(int(pl["w"]) * int(pl["h"]) for pl in sheet.placements or [])

    def remaining_items(remaining_ids):
        return [pieces_by_id[item_id] for item_id in remaining_ids]

    def remaining_counter(remaining_ids):
        return Counter(_piece_type_key(pieces_by_id[item_id]) for item_id in remaining_ids)

    def candidate_rank(cand, state, target):
        sheets = cand.get("sheets") or []
        if len(sheets) != 1:
            return None
        sheet = sheets[0]
        used = set(cand.get("used") or ())
        if not used:
            return None
        remaining_set = set(state["remaining"])
        if not used.issubset(remaining_set):
            return None
        new_remaining = tuple(sorted(remaining_set - used))
        remaining = remaining_items(new_remaining)
        future_sheet_count = len(state["sheets"]) + 1 + _area_lower_bound(remaining, sheet_w, sheet_h)
        if future_sheet_count > target:
            return None

        signature = _sheet_mosaic_signature(sheet)
        repeat_count = int(state["signature_counts"].get(signature) or 0)
        count_by_key = Counter(
            tuple(sorted((int(pl["w"]), int(pl["h"]))))
            for pl in sheet.placements or []
        )
        remaining_by_key = remaining_counter(new_remaining)
        progress = len(state["sheets"]) / float(max(1, target))

        preserve_penalty = 0
        for key in count_by_key:
            total_count = int(total_by_key.get(key) or 0)
            if total_count < 4 or int(area_by_key.get(key) or 0) < sheet_area * 0.04:
                continue
            after = int(remaining_by_key.get(key) or 0)
            # Early in the plan, avoid spending the last instance of a repeated
            # large family if a similarly dense capped mosaic keeps the exact
            # sheet target feasible. This is the "don't burn the glue pieces"
            # rule behind CutList-like mixed stress results.
            min_keep = 1 if progress < 0.45 else 0
            if after < min_keep:
                preserve_penalty += min_keep - after

        area_used = max(1, used_area(sheet))
        simple_cuts = _guillotine_tree_cut_count(sheet)
        best_full = _best_offcut_info([sheet], full_dim_only=True)
        best_any = _best_offcut_info([sheet])
        return (
            future_sheet_count,
            bool(repeat_count >= 2 and new_remaining),
            -area_used,
            int(simple_cuts) * 1000000 // area_used,
            preserve_penalty,
            -len(count_by_key),
            -int(best_full.get("value") or 0),
            -int(best_any.get("value") or 0),
            -len(used),
            getattr(sheet, "strategy", "") or cand.get("strategy") or "",
        )

    def state_rank(state, target):
        remaining = remaining_items(state["remaining"])
        slots = target - len(state["sheets"])
        area_lb = _area_lower_bound(remaining, sheet_w, sheet_h)
        partial_score = score(state["sheets"], []) if state["sheets"] else (0, 0, 0, 0)
        repeated = sum(max(0, int(count) - 2) for count in state["signature_counts"].values())
        simple_cuts = sum(_guillotine_tree_cut_count(sheet) for sheet in state["sheets"])
        area_used = sum(used_area(sheet) for sheet in state["sheets"]) or 1
        return (
            area_lb > slots,
            slots - area_lb,
            repeated,
            int(simple_cuts) * 1000000 // area_used,
            partial_score[1],
            partial_score[2],
            len(state["remaining"]),
            int(state.get("_order") or 0),
        )

    best_complete = None
    best_score = None
    order = 0

    for target in targets:
        if _deadline_hit(deadline, 0.05):
            break
        states = [{
            "sheets": [],
            "remaining": all_ids,
            "signature_counts": Counter(),
            "_order": 0,
        }]

        for _sheet_idx in range(target):
            if _deadline_hit(deadline, 0.05):
                break
            next_states = {}
            for state in states:
                if _deadline_hit(deadline, 0.05):
                    break
                remaining_ids = tuple(state["remaining"])
                if not remaining_ids:
                    sc = score(state["sheets"], [])
                    if best_complete is None or sc < best_score:
                        best_complete = [_clone_sheet(s) for s in state["sheets"]]
                        best_score = sc
                    continue

                remaining = remaining_items(remaining_ids)
                slots = target - len(state["sheets"])
                if _area_lower_bound(remaining, sheet_w, sheet_h) > slots:
                    continue

                candidates = _v2_guillotine_mosaic_candidates(
                    remaining,
                    sheet_w,
                    sheet_h,
                    kerf,
                    deadline=deadline,
                    allow_expensive=True,
                )
                candidates.extend(_v2_transposed_column_stack_candidates(
                    remaining,
                    sheet_w,
                    sheet_h,
                    kerf,
                    deadline=deadline,
                    allow_expensive=True,
                ))
                candidates.extend(_v2_column_stack_candidates(
                    remaining,
                    sheet_w,
                    sheet_h,
                    kerf,
                    deadline=deadline,
                    allow_expensive=True,
                ))
                candidates.extend(_v2_group_mosaic_candidates(
                    remaining,
                    sheet_w,
                    sheet_h,
                    kerf,
                    deadline=deadline,
                    allow_expensive=True,
                ))
                if slots <= 5 or len(remaining_ids) < 100:
                    candidates.extend(_v2_repeat_sheet_candidates(
                        remaining,
                        sheet_w,
                        sheet_h,
                        kerf,
                        deadline=deadline,
                        allow_top_aligned=True,
                    ))
                candidates = _dedupe_candidates(candidates, set(remaining_ids))

                ranked = []
                for cand in candidates:
                    rank = candidate_rank(cand, state, target)
                    if rank is not None:
                        ranked.append((rank, cand))
                ranked.sort(key=lambda item: item[0])

                for _rank, cand in ranked[:candidate_keep]:
                    used = set(cand["used"])
                    new_remaining = tuple(sorted(set(remaining_ids) - used))
                    new_sheets = [_clone_sheet(s) for s in state["sheets"]] + [
                        _clone_sheet(s) for s in cand["sheets"]
                    ]
                    signature_counts = Counter(state["signature_counts"])
                    for sheet in cand["sheets"]:
                        signature_counts[_sheet_mosaic_signature(sheet)] += 1
                    order += 1
                    new_state = {
                        "sheets": new_sheets,
                        "remaining": new_remaining,
                        "signature_counts": signature_counts,
                        "_order": order,
                    }
                    if not new_remaining:
                        sc = score(new_sheets, [])
                        if best_complete is None or sc < best_score:
                            best_complete = [_clone_sheet(s) for s in new_sheets]
                            best_score = sc
                        continue
                    state_key = (new_remaining, tuple(sorted(signature_counts.items())))
                    rank = state_rank(new_state, target)
                    previous = next_states.get(state_key)
                    if previous is None or rank < previous[0]:
                        next_states[state_key] = (rank, new_state)

            if not next_states:
                break
            ranked_states = sorted(next_states.values(), key=lambda item: item[0])
            states = [state for _rank, state in ranked_states[:state_beam]]

        if best_complete is not None and len(best_complete) <= target:
            break

    if best_complete is None:
        return [], list(pieces)

    used_ids = _ids_in_sheets(best_complete)
    if not used_ids or len(used_ids) != len(pieces):
        return [], list(pieces)
    if not all(_sheet_geometry_ok(sheet) for sheet in best_complete):
        return [], list(pieces)
    for idx, sheet in enumerate(best_complete, 1):
        sheet.strategy = "mosaic_complete_%02d_%s" % (
            idx,
            getattr(sheet, "strategy", "") or "guillotine",
        )
    _elapsed_ms(started_at)  # keeps the timer local for easy profiling probes
    return best_complete, []


def _v2_constructor_candidates(remaining, sheet_w, sheet_h, kerf, seed_limit, *, allow_expensive, stats=None, deadline=None):
    candidates = []
    deterministic = [("v2_mosaic_complete", construct_mosaic_complete_planner),
                     ("v2_tall_strip_columns", construct_tall_strip_columns)]
    if allow_expensive:
        deterministic.extend((
            ("v2_guillotine_baf", construct_guillotine_baf),
            ("v2_varied_dense_shelves", construct_varied_dense_shelves),
            ("v2_anchor_shelf", construct_anchor_shelf),
            ("v2_repeat_band_sequence", construct_repeat_band),
            ("v2_large_repeat_tail_sequence", construct_large_repeat_tail),
            ("v2_shelf_sequence", construct_shelf),
        ))
    for name, builder in deterministic:
        if _deadline_hit(deadline, 0.02):
            break
        started_at = time.monotonic()
        before = len(candidates)
        sheets, _unplaced = builder(remaining, sheet_w, sheet_h, kerf=kerf, seed=0, deadline=deadline)
        if len(sheets or []) == 1:
            cand = _candidate_from_sheets(sheets, name)
            _append_candidate_variants(candidates, cand)
        if sheets:
            first = _candidate_from_sheets([sheets[0]], "%s_first" % name)
            _append_candidate_variants(candidates, first)
        _record_strategy_metric(stats, name, _elapsed_ms(started_at), len(candidates) - before)

    for seed in range(max(1, min(int(seed_limit or 1), 4))):
        if _deadline_hit(deadline, 0.02):
            break
        name = "v2_free_rect_seed_%d" % seed
        started_at = time.monotonic()
        before = len(candidates)
        sheets, _unplaced = construct(remaining, sheet_w, sheet_h, kerf=kerf, seed=seed, deadline=deadline)
        if sheets:
            first = _candidate_from_sheets([sheets[0]], name)
            _append_candidate_variants(candidates, first)
        _record_strategy_metric(stats, name, _elapsed_ms(started_at), len(candidates) - before)
    return candidates


def _dedupe_candidates(candidates, remaining_set):
    deduped = []
    seen = set()
    for cand in candidates:
        used = tuple(item_id for item_id in cand.get("used") or () if item_id in remaining_set)
        if not used or len(used) != len(cand.get("used") or ()):
            continue
        signature = (
            used,
            tuple(
                (
                    getattr(sheet, "strategy", ""),
                    tuple(sorted((int(pl["item"]), int(pl["x"]), int(pl["y"]), int(pl["w"]), int(pl["h"])) for pl in sheet.placements)),
                )
                for sheet in cand.get("sheets") or []
            ),
        )
        if signature in seen:
            continue
        seen.add(signature)
        cand = dict(cand)
        cand["used"] = used
        deduped.append(cand)
    return deduped


def _v2_generate_candidates(remaining, sheet_w, sheet_h, kerf, seed_limit, *, allow_expensive, stats=None, deadline=None):
    candidates = []
    remaining_sheet_lb = _area_lower_bound(remaining, sheet_w, sheet_h)
    column_allow_expensive = bool(allow_expensive and remaining_sheet_lb <= 14)
    pool_limit = 1800 if allow_expensive else 720

    def cap_pool():
        if len(candidates) > pool_limit:
            candidates[:] = _prune_candidate_pool(candidates, sheet_w, sheet_h, pool_limit)

    candidates.extend(_v2_guillotine_mosaic_candidates(
        remaining, sheet_w, sheet_h, kerf,
        stats=stats, deadline=deadline, allow_expensive=allow_expensive,
    ))
    cap_pool()
    if column_allow_expensive and not _deadline_hit(deadline, 0.02):
        candidates.extend(_v2_transposed_lane_run_candidates(
            remaining, sheet_w, sheet_h, kerf,
            stats=stats, deadline=deadline, allow_expensive=column_allow_expensive,
        ))
        cap_pool()
    if (remaining_sheet_lb <= 6 or len(remaining) <= 140) and not _deadline_hit(deadline, 0.02):
        candidates.extend(_v2_transposed_exact_lane_partition_candidates(
            remaining, sheet_w, sheet_h, kerf,
            stats=stats, deadline=deadline,
            allow_expensive=bool(column_allow_expensive or len(remaining) <= 100),
        ))
        cap_pool()
    if (remaining_sheet_lb <= 5 or len(remaining) <= 120) and not _deadline_hit(deadline, 0.02):
        candidates.extend(_v2_exact_lane_partition_candidates(
            remaining, sheet_w, sheet_h, kerf,
            stats=stats, deadline=deadline,
            allow_expensive=bool(column_allow_expensive or len(remaining) <= 90),
        ))
        cap_pool()
    if column_allow_expensive and not _deadline_hit(deadline, 0.02):
        candidates.extend(_v2_lane_run_candidates(
            remaining, sheet_w, sheet_h, kerf,
            stats=stats, deadline=deadline, allow_expensive=False,
        ))
        cap_pool()
    candidates.extend(_v2_transposed_simple_lane_pair_candidates(
        remaining, sheet_w, sheet_h, kerf,
        stats=stats, deadline=deadline, allow_expensive=column_allow_expensive,
    ))
    cap_pool()
    candidates.extend(_v2_simple_lane_pair_candidates(
        remaining, sheet_w, sheet_h, kerf,
        stats=stats, deadline=deadline, allow_expensive=column_allow_expensive,
    ))
    cap_pool()
    candidates.extend(_v2_transposed_lane_recipe_candidates(
        remaining, sheet_w, sheet_h, kerf,
        stats=stats, deadline=deadline, allow_expensive=column_allow_expensive,
    ))
    cap_pool()
    candidates.extend(_v2_lane_recipe_candidates(
        remaining, sheet_w, sheet_h, kerf,
        stats=stats, deadline=deadline, allow_expensive=column_allow_expensive,
    ))
    cap_pool()
    candidates.extend(_v2_transposed_column_stack_candidates(
        remaining, sheet_w, sheet_h, kerf,
        stats=stats, deadline=deadline, allow_expensive=column_allow_expensive,
    ))
    cap_pool()
    candidates.extend(_v2_column_stack_candidates(
        remaining, sheet_w, sheet_h, kerf,
        stats=stats, deadline=deadline, allow_expensive=column_allow_expensive,
    ))
    cap_pool()
    candidates.extend(_v2_group_mosaic_candidates(
        remaining, sheet_w, sheet_h, kerf,
        stats=stats, deadline=deadline, allow_expensive=allow_expensive,
    ))
    cap_pool()
    candidates.extend(_v2_repeat_sheet_candidates(
        remaining, sheet_w, sheet_h, kerf,
        stats=stats, deadline=deadline, allow_top_aligned=allow_expensive,
    ))
    cap_pool()
    remaining_set = {int(p["id"]) for p in remaining}
    return _dedupe_candidates(candidates, remaining_set)


def _v2_local_improve(sheets, pieces, sheet_w, sheet_h, kerf, deadline=None):
    """Deterministic complete-layout polish pass.

    This only accepts complete alternatives that preserve or improve sheet count
    and improve the v2 score. It gives the beam a final chance to normalize repeat
    tails and strip-column jobs without routing outside the pattern constructor.
    """
    if not sheets:
        return sheets

    def best_full_dim_value(layout):
        best = 0.0
        for sheet in layout or []:
            for _x, _y, w, h in _offcut_rects(sheet):
                if abs(w - sheet.w) <= 5 or abs(h - sheet.h) <= 5:
                    best = max(best, _offcut_value(w, h, sheet.w, sheet.h))
        return best

    best = [_clone_sheet(s) for s in sheets]
    best_score = score(best, [])
    best_full_dim = best_full_dim_value(best)
    alternatives = (
        construct_mosaic_complete_planner,
        construct_guillotine_baf,
        construct_varied_dense_shelves,
        construct_large_repeat_tail,
        construct_repeat_band,
        construct_tall_strip_columns,
        construct_shelf,
    )
    for builder in alternatives:
        if _deadline_hit(deadline, 0.02):
            break
        alt_sheets, alt_unplaced = builder(pieces, sheet_w, sheet_h, kerf=kerf, seed=0, deadline=deadline)
        if alt_unplaced or not alt_sheets:
            continue
        alt_score = score(alt_sheets, [])
        alt_full_dim = best_full_dim_value(alt_sheets)
        if (
            builder is construct_large_repeat_tail
            and len(alt_sheets) <= len(best)
            and alt_score[0] <= best_score[0]
            and alt_score[1] <= best_score[1]
            and alt_full_dim >= best_full_dim
        ):
            best = [_clone_sheet(s) for s in alt_sheets]
            best_score = alt_score
            best_full_dim = alt_full_dim
            continue
        if len(alt_sheets) <= len(best) and alt_score < best_score:
            best = [_clone_sheet(s) for s in alt_sheets]
            best_score = alt_score
            best_full_dim = alt_full_dim
    return best


def construct_tail_reserved_layout(pieces, sheet_w, sheet_h, *, kerf=3, target_sheets=None, deadline=None):
    """Reserve a dense final offcut sheet, then pack the rest.

    This is a bounded complete-layout move, not a benchmark replay: generate dense
    full-dimension tail candidates from the normal strip/column-stack maths, then
    accept one only if the remaining inventory still fits within the same sheet
    count.
    """
    if not pieces or len(pieces) < 120:
        return [], list(pieces)
    started_at = time.monotonic()
    sheet_area = max(1, int(sheet_w) * int(sheet_h))
    target_sheets = int(target_sheets or 0)
    if target_sheets <= 0:
        target_sheets = max(1, _area_lower_bound(pieces, sheet_w, sheet_h) + 1)

    local_budget = 29.0 if len(pieces) >= 240 else 18.0
    local_deadline = started_at + local_budget
    if deadline:
        local_deadline = min(float(deadline), local_deadline)
    if _deadline_hit(local_deadline, 2.0):
        return [], list(pieces)

    candidate_deadline = min(float(local_deadline) - 1.0, time.monotonic() + 12.0)
    raw = []
    raw.extend(_v2_transposed_full_height_column_tail_candidates(
        pieces,
        sheet_w,
        sheet_h,
        kerf,
        deadline=candidate_deadline,
        allow_expensive=True,
    ))
    if not _deadline_hit(candidate_deadline, 0.02):
        raw.extend(_v2_full_height_column_tail_candidates(
            pieces,
            sheet_w,
            sheet_h,
            kerf,
            deadline=candidate_deadline,
            allow_expensive=True,
        ))
    raw.extend(_v2_transposed_long_strip_tail_candidates(
        pieces,
        sheet_w,
        sheet_h,
        kerf,
        deadline=candidate_deadline,
        allow_expensive=True,
    ))
    if not _deadline_hit(candidate_deadline, 0.02):
        raw.extend(_v2_long_strip_tail_candidates(
            pieces,
            sheet_w,
            sheet_h,
            kerf,
            deadline=candidate_deadline,
            allow_expensive=True,
        ))
    if not raw and not _deadline_hit(candidate_deadline, 0.02):
        raw.extend(_v2_transposed_column_stack_candidates(
            pieces,
            sheet_w,
            sheet_h,
            kerf,
            deadline=candidate_deadline,
            allow_expensive=True,
        ))
    if not raw or _deadline_hit(local_deadline, 0.5):
        return [], list(pieces)

    tails = []
    seen = set()
    order = 0
    min_tail_area = int(sheet_area * 0.80)
    for cand in raw:
        sheets = cand.get("sheets") or []
        if len(sheets) != 1:
            continue
        sheet = sheets[0]
        full = _best_offcut_info([sheet], full_dim_only=True)
        full_value = int(full.get("value") or 0)
        if full_value <= 0:
            continue
        used_area = sum(int(pl["w"]) * int(pl["h"]) for pl in sheet.placements or [])
        if used_area < min_tail_area:
            continue
        used = set(int(item_id) for item_id in (cand.get("used") or ()))
        if not used or len(used) > max(90, len(pieces) // 2):
            continue
        sig = (
            _sheet_mosaic_signature(sheet),
            tuple(sorted((int(pl["x"]), int(pl["y"]), int(pl["w"]), int(pl["h"])) for pl in sheet.placements)),
        )
        if sig in seen:
            continue
        seen.add(sig)
        order += 1
        tails.append((
            -int(full_value),
            -int(used_area),
            int(saw_metrics(sheet)[1]),
            order,
            cand,
            sheet,
        ))
    if not tails:
        return [], list(pieces)
    tails.sort(key=lambda item: item[:4])

    def select_tail_candidates(items):
        """Keep local tail winners plus offcut-size buckets.

        A pure local sort misses feasible CutList-like tails: very large offcuts can
        starve the rest of the job, while very dense tails can leave the wrong
        short-edge strip. Keeping a small spread by full-dimension offcut size lets
        the complete-layout check decide what actually works.
        """
        selected = []
        selected_keys = set()

        def item_key(item):
            sheet = item[5]
            return (
                _sheet_mosaic_signature(sheet),
                tuple(sorted(
                    (int(pl["x"]), int(pl["y"]), int(pl["w"]), int(pl["h"]))
                    for pl in sheet.placements
                )),
            )

        def add(source, limit):
            for item in source[:int(limit)]:
                key = item_key(item)
                if key in selected_keys:
                    continue
                selected_keys.add(key)
                selected.append(item)

        buckets = {}
        long_side = max(int(sheet_w), int(sheet_h))
        for item in items:
            full = _best_offcut_info([item[5]], full_dim_only=True)
            fw, fh = int(full.get("w") or 0), int(full.get("h") or 0)
            if not fw or not fh:
                continue
            short = min(fw, fh)
            long_edge = max(fw, fh) >= long_side - 5
            buckets.setdefault((bool(long_edge), int(short) // 20), []).append(item)
        bucket_order = sorted(
            buckets,
            key=lambda key: (
                not bool(key[0]),
                max(0, int(key[1]) - max(1, int(_UNSAFE_SLIVER_MM) // 20)),
                int(key[1]),
            ),
        )
        refilled = []
        for item in items:
            cand = item[4]
            sheet = item[5]
            strategy = "%s %s" % (
                cand.get("strategy") or "",
                getattr(sheet, "strategy", "") or "",
            )
            if "refilled_strip" not in strategy:
                continue
            full = _best_offcut_info([sheet], full_dim_only=True)
            short = min(int(full.get("w") or 0), int(full.get("h") or 0))
            if short < 80:
                continue
            refilled.append((short, item))
        refilled.sort(key=lambda pair: (
            int(pair[0]),
            pair[1][2],
            -pair[1][1],
            pair[1][3],
        ))
        add([item for _short, item in refilled], 128)

        for key in bucket_order:
            if key[0] and int(key[1]) <= 8:
                bucket = sorted(buckets[key], key=lambda item: (item[2], item[1], item[0], item[3]))
            else:
                bucket = sorted(buckets[key], key=lambda item: (item[1], item[2], item[0], item[3]))
            limit = 20 if key[0] and 3 <= int(key[1]) <= 6 else 8
            add(bucket, limit)

        refilled = []
        for item in items:
            cand = item[4]
            sheet = item[5]
            strategy = "%s %s" % (
                cand.get("strategy") or "",
                getattr(sheet, "strategy", "") or "",
            )
            if "refilled_strip" not in strategy:
                continue
            full = _best_offcut_info([sheet], full_dim_only=True)
            short = min(int(full.get("w") or 0), int(full.get("h") or 0))
            if short < 80:
                continue
            refilled.append((short, item))
        refilled.sort(key=lambda pair: (
            int(pair[0]),
            pair[1][2],
            -pair[1][1],
            pair[1][3],
        ))
        add([item for _short, item in refilled], 96)

        add(sorted(items, key=lambda item: (item[1], item[0], item[2], item[3])), 48)
        add(sorted(items, key=lambda item: item[:4]), 36)
        add(sorted(items, key=lambda item: (item[2], item[0], item[1], item[3])), 24)
        return selected

    best = None
    best_score = None
    best_tail_key = None
    best_full = -1
    pieces_by_id = {int(piece["id"]): piece for piece in pieces}

    def tail_layout_key(layout):
        """Large-job tail objective: full strip first, then reported cuts.

        The public score intentionally rewards the biggest reusable offcut for
        normal jobs. In large residual-tail jobs that can overrule CutList-like
        saw simplicity, so this local key keeps the full-dimension strip as a hard
        requirement and then prefers the cleaner CutList-style tree cut count.
        """
        full = int(_best_offcut_info(layout, full_dim_only=True).get("value") or 0)
        no_full = 0 if full > 0 else 1
        full_info = _best_offcut_info(layout, full_dim_only=True)
        tail_idx = int(full_info.get("sheet") or 0) - 1
        tail_used_area = 0
        if 0 <= tail_idx < len(layout or []):
            tail_used_area = sum(
                int(pl["w"]) * int(pl["h"])
                for pl in (layout[tail_idx].placements or [])
            )
        public_score = score(layout, [])
        tree_cuts = int(public_score[2]) if len(public_score) > 2 else 0
        niceness = int(public_score[3]) if len(public_score) > 3 else 0
        return (
            len(layout or []),
            no_full,
            int(tree_cuts),
            -(full // 10000),
            int(tail_used_area),
            niceness,
        )

    def improve_with_residual_lane(layout):
        """Replace late residual sheets with stronger lane mosaics if provable."""
        if not layout or len(layout) < 4 or _deadline_hit(local_deadline, 0.75):
            return layout
        current_full_info = _best_offcut_info(layout, full_dim_only=True)
        current_full = int(current_full_info.get("value") or 0)
        tail_idx = int(current_full_info.get("sheet") or 0) - 1
        if current_full <= 0 or tail_idx < 0 or tail_idx >= len(layout):
            return layout
        tail_sheet = _clone_sheet(layout[tail_idx])
        tail_ids = set(_ids_in_sheets([tail_sheet]) or ())
        if not tail_ids:
            return layout

        remaining_ids = set(pieces_by_id) - tail_ids
        remaining = [pieces_by_id[item_id] for item_id in sorted(remaining_ids)]
        rest_slots = len(layout) - 2
        if rest_slots < 1 or _area_lower_bound(remaining, sheet_w, sheet_h) > rest_slots + 1:
            return layout

        candidate_window = 5.5 if len(pieces) >= 240 else 8.0
        candidate_deadline = min(float(local_deadline) - 0.20, time.monotonic() + candidate_window)
        if candidate_deadline <= time.monotonic():
            return layout

        raw = []
        raw.extend(_v2_transposed_strip_filler_residual_candidates(
            remaining, sheet_w, sheet_h, kerf,
            deadline=candidate_deadline, allow_expensive=True,
        ))
        if not _deadline_hit(candidate_deadline, 0.02):
            raw.extend(_v2_strip_filler_residual_candidates(
                remaining, sheet_w, sheet_h, kerf,
                deadline=candidate_deadline, allow_expensive=False,
            ))
        if not _deadline_hit(candidate_deadline, 0.02):
            raw.extend(_v2_transposed_exact_lane_partition_candidates(
                remaining, sheet_w, sheet_h, kerf,
                deadline=candidate_deadline, allow_expensive=False,
            ))
        if not _deadline_hit(candidate_deadline, 0.02):
            raw.extend(_v2_transposed_lane_run_candidates(
                remaining, sheet_w, sheet_h, kerf,
                deadline=candidate_deadline, allow_expensive=True,
            ))
        if not _deadline_hit(candidate_deadline, 0.02):
            raw.extend(_v2_lane_run_candidates(
                remaining, sheet_w, sheet_h, kerf,
                deadline=candidate_deadline, allow_expensive=False,
            ))

        candidates = _dedupe_candidates(raw, remaining_ids)
        if not candidates:
            return layout

        sheet_area = max(1, int(sheet_w) * int(sheet_h))
        ranked = []
        for cand in candidates:
            sheets = cand.get("sheets") or []
            if len(sheets) != 1:
                continue
            sheet = sheets[0]
            used = set(int(item_id) for item_id in (cand.get("used") or ()))
            if not used or not used.issubset(remaining_ids):
                continue
            area_used = sum(int(pl["w"]) * int(pl["h"]) for pl in sheet.placements or [])
            if area_used < sheet_area * 0.68:
                continue
            count_by_key = Counter(
                tuple(sorted((int(pl["w"]), int(pl["h"]))))
                for pl in sheet.placements or []
            )
            repeat_panels = sum(int(count) for count in count_by_key.values() if int(count) >= 4)
            long_strip_panels = sum(
                int(count)
                for key, count in count_by_key.items()
                if min(int(key[0]), int(key[1])) <= 180
                and max(int(key[0]), int(key[1])) >= int(max(sheet_w, sheet_h)) * 0.28
            )
            filler_panels = sum(
                int(count)
                for key, count in count_by_key.items()
                if max(int(key[0]), int(key[1])) <= 340
            )
            residual_value = int(repeat_panels) + int(long_strip_panels) + int(filler_panels)
            if residual_value < 8:
                continue
            cuts = int(saw_metrics(sheet)[1])
            tree = int(_guillotine_tree_cut_count(sheet))
            ranked.append({
                "rank": (
                    cuts * 1000000 // max(1, area_used),
                    tree * 1000000 // max(1, area_used),
                    -int(residual_value),
                    -int(area_used),
                    len(count_by_key),
                    getattr(sheet, "strategy", "") or cand.get("strategy") or "",
                ),
                "cand": cand,
                "sheet": sheet,
                "used": used,
                "area": int(area_used),
                "residual_value": int(residual_value),
                "counts": Counter(count_by_key),
                "cuts": int(cuts),
                "tree": int(tree),
            })
        if not ranked:
            return layout
        ranked.sort(key=lambda item: item["rank"])

        best_layout = [_clone_sheet(sheet) for sheet in layout]
        best_key = tail_layout_key(layout)

        def materialize_template(sheet, pools, used_counts):
            placements = []
            for pl in sorted(sheet.placements or [], key=lambda p: (int(p["y"]), int(p["x"]), int(p["w"]), int(p["h"]), int(p["item"]))):
                key = tuple(sorted((int(pl["w"]), int(pl["h"]))))
                idx = int(used_counts[key])
                if idx >= len(pools.get(key, [])):
                    return None
                piece = pools[key][idx]
                used_counts[key] += 1
                new_pl = dict(pl)
                new_pl["item"] = int(piece["id"])
                new_pl["rotated"] = int(new_pl["w"]) != int(piece["w"]) or int(new_pl["h"]) != int(piece["h"])
                placements.append(new_pl)
            clone = _sheet_from_pattern(
                int(sheet.w),
                int(sheet.h),
                int(sheet.kerf),
                placements,
                list(sheet.free or []),
                getattr(sheet, "strategy", "") or "residual_lane",
            )
            return clone if _sheet_geometry_ok(clone) else None

        def materialize_templates(sheets):
            pools = {}
            for item_id in sorted(remaining_ids):
                piece = pieces_by_id[item_id]
                pools.setdefault(_piece_type_key(piece), []).append(piece)
            used_counts = Counter()
            output = []
            for sheet in sheets:
                clone = materialize_template(sheet, pools, used_counts)
                if clone is None:
                    return None
                output.append(clone)
            return output

        def accept_trial(rest_sheets, residual_sheets, variant_name):
            nonlocal best_layout, best_key
            trial = (
                [_clone_sheet(sheet) for sheet in rest_sheets]
                + [_clone_sheet(sheet) for sheet in residual_sheets]
                + [_clone_sheet(tail_sheet)]
            )
            if len(trial) > len(layout):
                return False
            if not all(_sheet_geometry_ok(sheet) for sheet in trial):
                return False
            used_ids = _ids_in_sheets(trial)
            if not used_ids or len(used_ids) != len(pieces):
                return False
            full = int(_best_offcut_info(trial, full_dim_only=True).get("value") or 0)
            if full < current_full:
                return False
            residual_start = len(rest_sheets)
            for idx, sheet in enumerate(trial, 1):
                if idx <= residual_start:
                    sheet.strategy = "tail_reserved_residual_rest_%02d_%s" % (
                        idx,
                        getattr(sheet, "strategy", "") or "rest",
                    )
                elif idx < len(trial):
                    sheet.strategy = "tail_reserved_%s_%02d_%s" % (
                        variant_name,
                        idx,
                        getattr(sheet, "strategy", "") or "lane",
                    )
                else:
                    sheet.strategy = "tail_reserved_residual_tail_%02d_%s" % (
                        idx,
                        getattr(sheet, "strategy", "") or "tail",
                    )
            key = tail_layout_key(trial)
            if key < best_key:
                best_layout = trial
                best_key = key
                return True
            return False

        for item in ranked[:220]:
            if _deadline_hit(local_deadline, 0.15):
                break
            cand = item["cand"]
            lane_sheet = item["sheet"]
            used = set(int(item_id) for item_id in (cand.get("used") or ()))
            rest_ids = remaining_ids - used
            rest_pieces = [pieces_by_id[item_id] for item_id in sorted(rest_ids)]
            if _area_lower_bound(rest_pieces, sheet_w, sheet_h) > rest_slots:
                continue
            for fitness_mode, split_mode, sort_mode in _GUILLOTINE_VARIANTS:
                if _deadline_hit(local_deadline, 0.05):
                    break
                rest_deadline = min(float(local_deadline) - 0.04, time.monotonic() + 0.35)
                rest_sheets, rest_unplaced = _guillotine_pack_variant(
                    rest_pieces,
                    sheet_w,
                    sheet_h,
                    kerf,
                    fitness_mode=fitness_mode,
                    split_mode=split_mode,
                    sort_mode=sort_mode,
                    deadline=rest_deadline,
                )
                if rest_unplaced or not rest_sheets or len(rest_sheets) > rest_slots:
                    continue
                accept_trial(
                    rest_sheets,
                    [lane_sheet],
                    "residual_lane_%s_%s_%s" % (fitness_mode, split_mode, sort_mode),
                )

        if len(layout) >= 5 and not _deadline_hit(local_deadline, 0.35):
            rest_slots_two = len(layout) - 3
            pair_pool = ranked[:90 if len(pieces) >= 240 else 140]
            available_counts = Counter(_piece_type_key(pieces_by_id[item_id]) for item_id in remaining_ids)
            pairs = []
            for i, left in enumerate(pair_pool):
                if _deadline_hit(local_deadline, 0.20):
                    break
                left_counts = Counter(left["counts"])
                for right in pair_pool[i + 1:]:
                    if _deadline_hit(local_deadline, 0.08):
                        break
                    combined = Counter(left_counts)
                    combined.update(right["counts"])
                    if any(int(combined[key]) > int(available_counts[key]) for key in combined):
                        continue
                    area = int(left["area"]) + int(right["area"])
                    if area < sheet_area * 1.38:
                        continue
                    pairs.append((
                        (
                            (int(left["cuts"]) + int(right["cuts"])) * 1000000 // max(1, area),
                            (int(left["tree"]) + int(right["tree"])) * 1000000 // max(1, area),
                            -(int(left["residual_value"]) + int(right["residual_value"])),
                            -area,
                            left["rank"],
                            right["rank"],
                        ),
                        left,
                        right,
                    ))
                    if len(pairs) >= 420:
                        break
                if len(pairs) >= 420:
                    break
            pairs.sort(key=lambda item: item[0])
            for _pair_rank, left, right in pairs[:180]:
                if _deadline_hit(local_deadline, 0.08):
                    break
                residual_sheets = materialize_templates([left["sheet"], right["sheet"]])
                if not residual_sheets:
                    continue
                residual_ids = set(_ids_in_sheets(residual_sheets) or ())
                if not residual_ids:
                    continue
                rest_ids = remaining_ids - residual_ids
                rest_pieces = [pieces_by_id[item_id] for item_id in sorted(rest_ids)]
                if _area_lower_bound(rest_pieces, sheet_w, sheet_h) > rest_slots_two:
                    continue
                for fitness_mode, split_mode, sort_mode in _GUILLOTINE_VARIANTS[:8]:
                    if _deadline_hit(local_deadline, 0.04):
                        break
                    rest_deadline = min(float(local_deadline) - 0.03, time.monotonic() + 0.28)
                    rest_sheets, rest_unplaced = _guillotine_pack_variant(
                        rest_pieces,
                        sheet_w,
                        sheet_h,
                        kerf,
                        fitness_mode=fitness_mode,
                        split_mode=split_mode,
                        sort_mode=sort_mode,
                        deadline=rest_deadline,
                    )
                    if rest_unplaced or not rest_sheets or len(rest_sheets) > rest_slots_two:
                        continue
                    accept_trial(
                        rest_sheets,
                        residual_sheets,
                        "residual_pair_%s_%s_%s" % (fitness_mode, split_mode, sort_mode),
                    )
        return best_layout

    residual_reserve = 8.5 if len(pieces) >= 240 else 0.75
    stop_tail_search = False
    for _full_rank, _used_rank, _cuts, _order, cand, tail_sheet in select_tail_candidates(tails):
        loop_reserve = residual_reserve if best is not None else 0.25
        if stop_tail_search or _deadline_hit(local_deadline, loop_reserve):
            break
        used = set(int(item_id) for item_id in cand.get("used") or ())
        remaining = [piece for item_id, piece in pieces_by_id.items() if item_id not in used]
        rest_window = 0.35 if len(pieces) >= 240 else 5.0
        rest_deadline = min(float(local_deadline) - 0.10, time.monotonic() + rest_window)
        tail_variants = _GUILLOTINE_VARIANTS[:6] if len(pieces) >= 240 else _GUILLOTINE_VARIANTS
        for fitness_mode, split_mode, sort_mode in tail_variants:
            if _deadline_hit(rest_deadline, 0.01):
                break
            rest_sheets, rest_unplaced = _guillotine_pack_variant(
                remaining,
                sheet_w,
                sheet_h,
                kerf,
                fitness_mode=fitness_mode,
                split_mode=split_mode,
                sort_mode=sort_mode,
                deadline=rest_deadline,
            )
            if rest_unplaced or not rest_sheets:
                continue
            if len(rest_sheets) + 1 > target_sheets:
                continue
            layout = [_clone_sheet(sheet) for sheet in rest_sheets] + [_clone_sheet(tail_sheet)]
            if not all(_sheet_geometry_ok(sheet) for sheet in layout):
                continue
            used_ids = _ids_in_sheets(layout)
            if not used_ids or len(used_ids) != len(pieces):
                continue
            for idx, sheet in enumerate(layout, 1):
                if idx == len(layout):
                    sheet.strategy = "tail_reserved_%02d_%s" % (
                        idx,
                        getattr(sheet, "strategy", "") or "tail",
                    )
                else:
                    sheet.strategy = "tail_reserved_rest_%02d_%s_%s_%s_%s" % (
                        idx,
                        fitness_mode,
                        split_mode,
                        sort_mode,
                        getattr(sheet, "strategy", "") or "rest",
                    )
            sc = score(layout, [])
            full = int(_best_offcut_info(layout, full_dim_only=True).get("value") or 0)
            tail_key = tail_layout_key(layout)
            if best is None or tail_key < best_tail_key:
                best = layout
                best_score = sc
                best_tail_key = tail_key
                best_full = full
    if best is None:
        return [], list(pieces)
    best = improve_with_residual_lane(best)
    return best, []


def _partial_state_key(sheets, remaining, sheet_w, sheet_h, kerf):
    remaining_area = sum(_area(p) for p in remaining)
    lower_bound = len(sheets) + _area_lower_bound(remaining, sheet_w, sheet_h)
    partial_score = score(sheets, []) if sheets else (0, 0, 0, 0)
    tail_promise = _repeat_tail_promise(remaining, sheet_w, sheet_h, kerf)
    free_promise = _free_band_promise(sheets)
    return (
        lower_bound,
        len(sheets),
        remaining_area,
        -tail_promise,
        -free_promise,
        partial_score[1],
        partial_score[2],
        _layout_strategy(sheets),
    )


def search_v2(
    pieces,
    sheet_w,
    sheet_h,
    *,
    kerf=3,
    n_seeds=400,
    time_budget_s=None,
    beam_width=160,
    first_feasible=False,
    progress_callback=None,
    cancel_callback=None,
):
    """Intrinsic CutList-style beam search over sheet pattern candidates."""
    started_at = time.monotonic()
    budget = float(time_budget_s or 0.0)
    deadline = started_at + budget if budget > 0 else None
    pieces = [dict(p) for p in pieces]
    pieces_by_id = {int(p["id"]): p for p in pieces}
    all_ids = tuple(sorted(pieces_by_id))
    stats = {
        "pattern_engine_version": "v2_beam",
        "beam_states_evaluated": 0,
        "pattern_candidates_evaluated": 0,
        "time_budget_hit": False,
        "best_pattern_strategy": "",
        "strategy_runtime_ms": {},
        "strategy_candidate_counts": {},
    }

    if not pieces:
        return [], 0, score([], []), [], stats

    beam = [{
        "sheets": [],
        "remaining": all_ids,
        "trace": (),
    }]
    best_complete = None
    best_score = None
    best_trace = ()
    state_counter = 0
    max_steps = max(1, len(pieces))
    beam_limit = max(1, int(beam_width or 160))
    next_state_soft_limit = max(beam_limit * 8, 320)
    next_state_hard_limit = max(beam_limit * 14, 640)

    def emit_progress(event):
        if not progress_callback or best_complete is None:
            return
        try:
            progress_callback({
                "event": event,
                "sheets": [_clone_sheet(sheet) for sheet in best_complete],
                "score": best_score,
                "unplaced": [],
                "stats": dict(stats),
            })
        except Exception:
            pass

    def compact_next_states(states, limit):
        if len(states or []) <= limit:
            return states
        ranked = []
        seen = {}
        for state in states:
            remaining = _remaining_items(pieces_by_id, state["remaining"])
            key = _partial_state_key(state["sheets"], remaining, sheet_w, sheet_h, kerf)
            sig = (state["remaining"], len(state["sheets"]))
            previous = seen.get(sig)
            if previous is None or key < previous[0]:
                seen[sig] = (key, state)
        for key, state in seen.values():
            ranked.append((key, state.get("_order", 0), state))
        ranked.sort(key=lambda item: (item[0], item[1]))
        return [state for _key, _order, state in ranked[:max(1, int(limit or beam_limit))]]

    if pieces and not _deadline_hit(deadline, 0.50):
        if _cancel_requested(cancel_callback):
            stats["cancelled"] = True
            return [], 0, score([], pieces), list(pieces), stats
        incumbent_deadline = None
        if deadline:
            incumbent_deadline = min(float(deadline) - 0.25, time.monotonic() + 4.0)
        incumbent_sheets, incumbent_unplaced = construct_guillotine_baf(
            pieces,
            sheet_w,
            sheet_h,
            kerf=kerf,
            seed=0,
            deadline=incumbent_deadline,
        )
        if incumbent_sheets and not incumbent_unplaced and all(_sheet_geometry_ok(s) for s in incumbent_sheets):
            best_complete = [_clone_sheet(s) for s in incumbent_sheets]
            best_score = score(best_complete, [])
            best_trace = tuple(getattr(s, "strategy", "") or "v2_guillotine_baf" for s in best_complete)
            emit_progress("first")
            if first_feasible:
                stats["best_pattern_strategy"] = _layout_strategy(best_complete) or "+".join(best_trace[-4:])
                stats.update(_layout_metric_fields(best_complete))
                return best_complete, 0, best_score, [], stats
            if (
                len(pieces) >= 120
                and int(_best_offcut_info(best_complete, full_dim_only=True).get("value") or 0) <= 0
                and not _deadline_hit(deadline, 8.0)
            ):
                tail_sheets, tail_unplaced = construct_tail_reserved_layout(
                    pieces,
                    sheet_w,
                    sheet_h,
                    kerf=kerf,
                    target_sheets=len(best_complete),
                    deadline=deadline,
                )
                if tail_sheets and not tail_unplaced:
                    tail_score = score(tail_sheets, [])
                    tail_full = int(_best_offcut_info(tail_sheets, full_dim_only=True).get("value") or 0)
                    best_full = int(_best_offcut_info(best_complete, full_dim_only=True).get("value") or 0)
                    if (tail_score, -tail_full) < (best_score, -best_full):
                        best_complete = [_clone_sheet(s) for s in tail_sheets]
                        best_score = tail_score
                        best_trace = tuple(getattr(s, "strategy", "") or "v2_tail_reserved" for s in best_complete)
                        emit_progress("best")

    for _step in range(max_steps):
        if _cancel_requested(cancel_callback):
            stats["cancelled"] = True
            break
        if deadline and time.monotonic() >= deadline:
            stats["time_budget_hit"] = True
            break
        next_states = []
        made_progress = False
        for state in beam:
            if _cancel_requested(cancel_callback):
                stats["cancelled"] = True
                break
            if deadline and time.monotonic() >= deadline:
                stats["time_budget_hit"] = True
                break
            stats["beam_states_evaluated"] += 1
            remaining_ids = tuple(state["remaining"])
            if not remaining_ids:
                sc = score(state["sheets"], [])
                if best_complete is None or sc < best_score:
                    best_complete = [_clone_sheet(s) for s in state["sheets"]]
                    best_score = sc
                    best_trace = state.get("trace") or ()
                    emit_progress("best")
                    if first_feasible:
                        stats["best_pattern_strategy"] = _layout_strategy(best_complete) or "+".join(best_trace[-4:])
                        stats.update(_layout_metric_fields(best_complete))
                        return best_complete, 0, best_score, [], stats
                continue

            remaining = _remaining_items(pieces_by_id, remaining_ids)
            allow_expensive = len(remaining_ids) == len(all_ids)
            candidates = _v2_generate_candidates(
                remaining, sheet_w, sheet_h, kerf, n_seeds,
                allow_expensive=allow_expensive, stats=stats, deadline=deadline,
            )
            if _deadline_hit(deadline):
                stats["time_budget_hit"] = True
            stats["pattern_candidates_evaluated"] += len(candidates)
            remaining_set = set(remaining_ids)
            for cand in candidates:
                used = set(cand["used"])
                if not used or not used.issubset(remaining_set):
                    continue
                new_sheets = [_clone_sheet(s) for s in state["sheets"]] + [
                    _clone_sheet(s) for s in cand["sheets"]
                ]
                if best_complete is not None and len(new_sheets) > len(best_complete):
                    continue
                new_remaining = tuple(sorted(remaining_set - used))
                new_trace = tuple(list(state.get("trace") or ()) + [cand.get("strategy") or "pattern"])
                if not new_remaining:
                    sc = score(new_sheets, [])
                    if best_complete is None or sc < best_score:
                        best_complete = [_clone_sheet(s) for s in new_sheets]
                        best_score = sc
                        best_trace = new_trace
                        emit_progress("best")
                        if first_feasible:
                            stats["best_pattern_strategy"] = _layout_strategy(best_complete) or "+".join(best_trace[-4:])
                            stats.update(_layout_metric_fields(best_complete))
                            return best_complete, 0, best_score, [], stats
                    made_progress = True
                    continue
                rem_items = _remaining_items(pieces_by_id, new_remaining)
                lb = len(new_sheets) + _area_lower_bound(rem_items, sheet_w, sheet_h)
                if best_complete is not None and lb > len(best_complete):
                    continue
                state_counter += 1
                next_states.append({
                    "sheets": new_sheets,
                    "remaining": new_remaining,
                    "trace": new_trace,
                    "_order": state_counter,
                })
                if len(next_states) > next_state_hard_limit:
                    next_states = compact_next_states(next_states, next_state_soft_limit)
                made_progress = True

        if stats["time_budget_hit"] or stats.get("cancelled"):
            break
        if not made_progress:
            break
        if not next_states:
            if best_complete is not None:
                break
            continue

        ranked = []
        seen = {}
        for state in compact_next_states(next_states, max(next_state_soft_limit, beam_limit)):
            remaining = _remaining_items(pieces_by_id, state["remaining"])
            key = _partial_state_key(state["sheets"], remaining, sheet_w, sheet_h, kerf)
            sig = (state["remaining"], len(state["sheets"]))
            previous = seen.get(sig)
            if previous is None or key < previous[0]:
                seen[sig] = (key, state)
        for key, state in seen.values():
            ranked.append((key, state.get("_order", 0), state))
        ranked.sort(key=lambda item: (item[0], item[1]))
        beam = [state for _key, _order, state in ranked[:beam_limit]]

        if best_complete is not None:
            min_possible = min(
                (
                    len(state["sheets"]) + _area_lower_bound(_remaining_items(pieces_by_id, state["remaining"]), sheet_w, sheet_h)
                    for state in beam
                ),
                default=999,
            )
            if min_possible > len(best_complete):
                break

    if best_complete is None:
        best_state = min(
            beam,
            key=lambda state: _partial_state_key(
                state["sheets"],
                _remaining_items(pieces_by_id, state["remaining"]),
                sheet_w,
                sheet_h,
                kerf,
            ),
        )
        unplaced = _remaining_items(pieces_by_id, best_state["remaining"])
        sc = score(best_state["sheets"], unplaced)
        stats["best_pattern_strategy"] = _layout_strategy(best_state["sheets"])
        stats.update(_layout_metric_fields(best_state["sheets"]))
        return best_state["sheets"], 0, sc, unplaced, stats

    if first_feasible:
        stats["best_pattern_strategy"] = _layout_strategy(best_complete) or "+".join(best_trace[-4:])
        stats.update(_layout_metric_fields(best_complete))
        return best_complete, 0, best_score, [], stats

    improved = _v2_local_improve(best_complete, pieces, sheet_w, sheet_h, kerf, deadline=deadline)
    improved_score = score(improved, [])
    if improved_score < best_score:
        best_complete = improved
        best_score = improved_score
        emit_progress("best")
    else:
        best_complete = improved
        best_score = improved_score
    stats["best_pattern_strategy"] = _layout_strategy(best_complete) or "+".join(best_trace[-4:])
    stats.update(_layout_metric_fields(best_complete))
    if _deadline_hit(deadline):
        stats["time_budget_hit"] = True
    emit_progress("final")
    return best_complete, 0, best_score, [], stats


def _v3_sheet_to_data(sheet):
    placements = tuple(
        (
            int(pl["item"]),
            int(pl["x"]),
            int(pl["y"]),
            int(pl["w"]),
            int(pl["h"]),
            bool(pl.get("rotated")),
        )
        for pl in (sheet.placements or [])
    )
    free = tuple(
        (int(x), int(y), int(w), int(h))
        for x, y, w, h in (sheet.free or [])
    )
    return (
        int(sheet.w),
        int(sheet.h),
        int(sheet.kerf),
        getattr(sheet, "strategy", "") or "pattern",
        placements,
        free,
    )


def _v3_sheet_from_data(data):
    w, h, kerf, strategy, placements, free = data
    sheet = _Sheet(int(w), int(h), int(kerf))
    sheet.placements = [
        {
            "item": int(item),
            "x": int(x),
            "y": int(y),
            "w": int(width),
            "h": int(height),
            "rotated": bool(rotated),
        }
        for item, x, y, width, height, rotated in placements
    ]
    sheet.free = [tuple(rect) for rect in free]
    sheet.strategy = strategy
    return sheet


def _v3_compile_candidate(cand, id_to_bit, pool, stats):
    sheets = [sheet for sheet in (cand.get("sheets") or []) if sheet and (sheet.placements or [])]
    if not sheets:
        return None
    used_ids = tuple(int(item_id) for item_id in (cand.get("used") or ()))
    used_mask = 0
    for item_id in used_ids:
        bit = id_to_bit.get(item_id)
        if bit is None:
            return None
        used_mask |= 1 << bit
    if not used_mask:
        return None
    sheet_data = tuple(_v3_sheet_to_data(sheet) for sheet in sheets)
    free_promise = _free_band_promise(sheets)
    best_full = _best_offcut_info(sheets, full_dim_only=True)
    best_bucket = int(best_full.get("value") or 0) // 10000
    cut_count = 0
    used_area = 0
    placement_count = 0
    for sheet in sheets:
        cut_count += _guillotine_tree_cut_count(sheet)
        for pl in sheet.placements or []:
            used_area += int(pl["w"]) * int(pl["h"])
            placement_count += 1
    ref = len(pool)
    pool.append({
        "ref": ref,
        "sheets": sheet_data,
        "used_mask": int(used_mask),
        "used_ids": used_ids,
        "strategy": cand.get("strategy") or "+".join(getattr(sheet, "strategy", "") for sheet in sheets),
        "sheet_count": len(sheet_data),
        "used_area": int(used_area),
        "placement_count": int(placement_count),
        "best_bucket": max(0, int(best_bucket)),
        "cut_count": int(cut_count),
        "free_promise": int(free_promise),
    })
    stats["candidate_pool_size"] = len(pool)
    return ref


def _v3_materialize_refs(refs, pool, stats):
    sheets = []
    for ref in refs or ():
        for data in pool[int(ref)]["sheets"]:
            sheets.append(_v3_sheet_from_data(data))
    stats["materialisations"] = int(stats.get("materialisations") or 0) + 1
    stats["materialised_sheets"] = int(stats.get("materialised_sheets") or 0) + len(sheets)
    return sheets


def _v3_layout_strategy(refs, pool):
    names = []
    for ref in refs or ():
        strategy = pool[int(ref)].get("strategy") or "pattern"
        names.extend(strategy.split("+") if strategy else ["pattern"])
    return "+".join(names[-4:]) if names else ""


def _v3_mask_items(mask, piece_by_bit, cache, stats):
    mask = int(mask)
    cached = cache.get(mask)
    if cached is not None:
        stats["remaining_cache_hits"] = int(stats.get("remaining_cache_hits") or 0) + 1
        return cached
    stats["remaining_cache_misses"] = int(stats.get("remaining_cache_misses") or 0) + 1
    items = []
    cursor = mask
    while cursor:
        bit = cursor & -cursor
        idx = bit.bit_length() - 1
        items.append(piece_by_bit[idx])
        cursor ^= bit
    cache[mask] = items
    return items


def _v3_mask_area(mask, area_by_bit, cache):
    mask = int(mask)
    cached = cache.get(mask)
    if cached is not None:
        return cached
    total = 0
    cursor = mask
    while cursor:
        bit = cursor & -cursor
        idx = bit.bit_length() - 1
        total += int(area_by_bit[idx])
        cursor ^= bit
    cache[mask] = total
    return total


def _v3_area_lower_bound(area, sheet_w, sheet_h):
    sheet_area = int(sheet_w) * int(sheet_h)
    if sheet_area <= 0:
        return 999
    return int((int(area) + sheet_area - 1) // sheet_area)


def _v3_partial_state_key(state, piece_by_bit, area_by_bit, item_cache, area_cache, pool, sheet_w, sheet_h, kerf, stats):
    remaining_mask = int(state["remaining_mask"])
    remaining_area = _v3_mask_area(remaining_mask, area_by_bit, area_cache)
    remaining = _v3_mask_items(remaining_mask, piece_by_bit, item_cache, stats)
    lower_bound = int(state["sheet_count"]) + _v3_area_lower_bound(remaining_area, sheet_w, sheet_h)
    tail_promise = _repeat_tail_promise(remaining, sheet_w, sheet_h, kerf)
    return (
        lower_bound,
        int(state["sheet_count"]),
        remaining_area,
        -int(tail_promise),
        -int(state.get("free_promise") or 0),
        -int(state.get("best_bucket") or 0),
        int(state.get("cut_count") or 0),
        _v3_layout_strategy(state.get("refs") or (), pool),
    )


def search_v3(
    pieces,
    sheet_w,
    sheet_h,
    *,
    kerf=3,
    n_seeds=400,
    time_budget_s=None,
    beam_width=160,
    first_feasible=False,
    progress_callback=None,
    cancel_callback=None,
):
    """Shadow v3 beam search using compact state and lazy sheet materialisation."""
    started_at = time.monotonic()
    budget = float(time_budget_s or 0.0)
    deadline = started_at + budget if budget > 0 else None
    pieces = [dict(p) for p in pieces]
    pieces_by_id = {int(p["id"]): p for p in pieces}
    all_ids = tuple(sorted(pieces_by_id))
    id_to_bit = {item_id: idx for idx, item_id in enumerate(all_ids)}
    piece_by_bit = [pieces_by_id[item_id] for item_id in all_ids]
    area_by_bit = [int(_area(piece)) for piece in piece_by_bit]
    all_mask = (1 << len(all_ids)) - 1
    item_cache = {}
    area_cache = {}
    candidate_cache = {}
    candidate_pool = []
    exact_score_cache = {}
    state_counter = 0
    best_complete_refs = None
    best_score = None
    best_sheet_count = 999
    best_trace = ()
    stats = {
        "pattern_engine_version": "v3_shadow_compact",
        "beam_states_evaluated": 0,
        "pattern_candidates_evaluated": 0,
        "time_budget_hit": False,
        "best_pattern_strategy": "",
        "strategy_runtime_ms": {},
        "strategy_candidate_counts": {},
        "candidate_cache_hits": 0,
        "candidate_cache_misses": 0,
        "remaining_cache_hits": 0,
        "remaining_cache_misses": 0,
        "candidate_pool_size": 0,
        "materialisations": 0,
        "materialised_sheets": 0,
        "clone_count_avoided": 0,
        "exact_score_evaluations": 0,
        "fast_incumbent_exit": False,
        "fast_incumbent_reason": "",
        "fast_incumbent_area_lower_bound": 0,
        "peak_beam_size": 1,
        "parity_status": "shadow_only",
        "candidate_generation_ms": 0,
        "candidate_compile_ms": 0,
    }

    if not pieces:
        stats["search_elapsed_ms"] = max(1, _elapsed_ms(started_at))
        return [], 0, score([], []), [], stats

    def remaining_items(mask):
        return _v3_mask_items(mask, piece_by_bit, item_cache, stats)

    def remaining_area(mask):
        return _v3_mask_area(mask, area_by_bit, area_cache)

    def materialize_refs(refs):
        return _v3_materialize_refs(refs, candidate_pool, stats)

    def exact_score(refs):
        refs = tuple(refs or ())
        cached = exact_score_cache.get(refs)
        if cached is not None:
            return cached
        sheets = materialize_refs(refs)
        stats["exact_score_evaluations"] = int(stats.get("exact_score_evaluations") or 0) + 1
        result = score(sheets, [])
        exact_score_cache[refs] = result
        return result

    def candidate_refs_for(mask, allow_expensive):
        key = (int(mask), int(sheet_w), int(sheet_h), int(kerf), bool(allow_expensive))
        cached = candidate_cache.get(key)
        if cached is not None:
            stats["candidate_cache_hits"] = int(stats.get("candidate_cache_hits") or 0) + 1
            return cached
        stats["candidate_cache_misses"] = int(stats.get("candidate_cache_misses") or 0) + 1
        started = time.monotonic()
        raw = _v2_generate_candidates(
            remaining_items(mask), sheet_w, sheet_h, kerf, n_seeds,
            allow_expensive=allow_expensive, stats=stats, deadline=deadline,
        )
        stats["candidate_generation_ms"] = int(stats.get("candidate_generation_ms") or 0) + _elapsed_ms(started)
        started = time.monotonic()
        refs = []
        for cand in raw:
            ref = _v3_compile_candidate(cand, id_to_bit, candidate_pool, stats)
            if ref is not None:
                refs.append(ref)
        stats["candidate_compile_ms"] = int(stats.get("candidate_compile_ms") or 0) + _elapsed_ms(started)
        stats["pattern_candidates_evaluated"] = int(stats.get("pattern_candidates_evaluated") or 0) + len(refs)
        refs = tuple(refs)
        candidate_cache[key] = refs
        return refs

    def emit_progress(event):
        if not progress_callback or best_complete_refs is None:
            return
        try:
            progress_callback({
                "event": event,
                "sheets": materialize_refs(best_complete_refs),
                "score": best_score,
                "unplaced": [],
                "stats": dict(stats),
            })
        except Exception:
            pass

    def install_complete(refs, trace=()):
        nonlocal best_complete_refs, best_score, best_sheet_count, best_trace
        refs = tuple(refs or ())
        sc = exact_score(refs)
        if best_complete_refs is None or sc < best_score:
            best_complete_refs = refs
            best_score = sc
            best_sheet_count = sum(int(candidate_pool[ref]["sheet_count"]) for ref in refs)
            best_trace = tuple(trace or ())
            emit_progress("best")
            return True
        return False

    if pieces and not _deadline_hit(deadline, 0.50):
        if _cancel_requested(cancel_callback):
            stats["cancelled"] = True
            stats["search_elapsed_ms"] = max(1, _elapsed_ms(started_at))
            return [], 0, score([], pieces), list(pieces), stats
        incumbent_deadline = None
        if deadline:
            incumbent_deadline = min(float(deadline) - 0.25, time.monotonic() + 4.0)
        incumbent_sheets, incumbent_unplaced = construct_guillotine_baf(
            pieces,
            sheet_w,
            sheet_h,
            kerf=kerf,
            seed=0,
            deadline=incumbent_deadline,
        )
        if incumbent_sheets and not incumbent_unplaced and all(_sheet_geometry_ok(s) for s in incumbent_sheets):
            ref = _v3_compile_candidate(
                {"sheets": incumbent_sheets, "used": all_ids, "strategy": "v3_guillotine_baf_incumbent"},
                id_to_bit, candidate_pool, stats,
            )
            if ref is not None:
                best_complete_refs = (ref,)
                best_score = exact_score(best_complete_refs)
                best_sheet_count = candidate_pool[ref]["sheet_count"]
                best_trace = tuple(getattr(s, "strategy", "") or "v3_guillotine_baf" for s in incumbent_sheets)
                emit_progress("first")
                if first_feasible:
                    sheets = materialize_refs(best_complete_refs)
                    stats["best_pattern_strategy"] = _layout_strategy(sheets) or "+".join(best_trace[-4:])
                    stats.update(_layout_metric_fields(sheets))
                    stats["search_elapsed_ms"] = max(1, _elapsed_ms(started_at))
                    return sheets, 0, best_score, [], stats
                if (
                    len(pieces) >= 120
                    and int(_best_offcut_info(incumbent_sheets, full_dim_only=True).get("value") or 0) <= 0
                    and not _deadline_hit(deadline, 8.0)
                ):
                    tail_sheets, tail_unplaced = construct_tail_reserved_layout(
                        pieces,
                        sheet_w,
                        sheet_h,
                        kerf=kerf,
                        target_sheets=len(incumbent_sheets),
                        deadline=deadline,
                    )
                    if tail_sheets and not tail_unplaced:
                        tail_ref = _v3_compile_candidate(
                            {"sheets": tail_sheets, "used": all_ids, "strategy": "v3_tail_reserved_incumbent"},
                            id_to_bit, candidate_pool, stats,
                        )
                        if tail_ref is not None:
                            tail_score = exact_score((tail_ref,))
                            tail_full = int(_best_offcut_info(tail_sheets, full_dim_only=True).get("value") or 0)
                            best_full = int(_best_offcut_info(incumbent_sheets, full_dim_only=True).get("value") or 0)
                            if (tail_score, -tail_full) < (best_score, -best_full):
                                best_complete_refs = (tail_ref,)
                                best_score = tail_score
                                best_sheet_count = candidate_pool[tail_ref]["sheet_count"]
                                best_trace = tuple(getattr(s, "strategy", "") or "v3_tail_reserved" for s in tail_sheets)
                                emit_progress("best")

                area_lower_bound = _v3_area_lower_bound(sum(area_by_bit), sheet_w, sheet_h)
                stats["fast_incumbent_area_lower_bound"] = int(area_lower_bound)
                if (
                    not first_feasible
                    and len(pieces) >= 100
                    and best_complete_refs is not None
                    and best_sheet_count <= int(area_lower_bound) + 2
                ):
                    best_complete = materialize_refs(best_complete_refs)
                    stats["fast_incumbent_exit"] = True
                    stats["fast_incumbent_reason"] = (
                        "large_complete_incumbent_within_area_lb_plus_2"
                    )
                    stats["best_pattern_strategy"] = _layout_strategy(best_complete) or "+".join(best_trace[-4:])
                    stats.update(_layout_metric_fields(best_complete))
                    stats["search_elapsed_ms"] = max(1, _elapsed_ms(started_at))
                    emit_progress("final")
                    return best_complete, 0, best_score, [], stats

    beam = [{
        "refs": (),
        "remaining_mask": all_mask,
        "sheet_count": 0,
        "remaining_area": sum(area_by_bit),
        "trace": (),
        "order": 0,
        "best_bucket": 0,
        "free_promise": 0,
        "cut_count": 0,
    }]
    max_steps = max(1, len(pieces))

    for _step in range(max_steps):
        if _cancel_requested(cancel_callback):
            stats["cancelled"] = True
            break
        if deadline and time.monotonic() >= deadline:
            stats["time_budget_hit"] = True
            break
        next_states = []
        made_progress = False
        for state in beam:
            if _cancel_requested(cancel_callback):
                stats["cancelled"] = True
                break
            if deadline and time.monotonic() >= deadline:
                stats["time_budget_hit"] = True
                break
            stats["beam_states_evaluated"] = int(stats.get("beam_states_evaluated") or 0) + 1
            mask = int(state["remaining_mask"])
            if not mask:
                if install_complete(state["refs"], state.get("trace") or ()):
                    if first_feasible:
                        sheets = materialize_refs(best_complete_refs)
                        stats["best_pattern_strategy"] = _layout_strategy(sheets) or "+".join(best_trace[-4:])
                        stats.update(_layout_metric_fields(sheets))
                        stats["search_elapsed_ms"] = max(1, _elapsed_ms(started_at))
                        return sheets, 0, best_score, [], stats
                continue

            allow_expensive = mask == all_mask
            refs = candidate_refs_for(mask, allow_expensive)
            if _deadline_hit(deadline):
                stats["time_budget_hit"] = True
            for ref in refs:
                cand = candidate_pool[int(ref)]
                used_mask = int(cand["used_mask"])
                if not used_mask or (used_mask & mask) != used_mask:
                    continue
                new_sheet_count = int(state["sheet_count"]) + int(cand["sheet_count"])
                if best_complete_refs is not None and new_sheet_count > best_sheet_count:
                    continue
                new_mask = mask & ~used_mask
                new_refs = tuple(list(state["refs"]) + [int(ref)])
                new_trace = tuple(list(state.get("trace") or ()) + [cand.get("strategy") or "pattern"])
                stats["clone_count_avoided"] = int(stats.get("clone_count_avoided") or 0) + new_sheet_count
                if not new_mask:
                    if install_complete(new_refs, new_trace):
                        if first_feasible:
                            sheets = materialize_refs(best_complete_refs)
                            stats["best_pattern_strategy"] = _layout_strategy(sheets) or "+".join(best_trace[-4:])
                            stats.update(_layout_metric_fields(sheets))
                            stats["search_elapsed_ms"] = max(1, _elapsed_ms(started_at))
                            return sheets, 0, best_score, [], stats
                    made_progress = True
                    continue
                new_remaining_area = max(0, int(state["remaining_area"]) - int(cand["used_area"]))
                lb = new_sheet_count + _v3_area_lower_bound(new_remaining_area, sheet_w, sheet_h)
                if best_complete_refs is not None and lb > best_sheet_count:
                    continue
                state_counter += 1
                next_states.append({
                    "refs": new_refs,
                    "remaining_mask": int(new_mask),
                    "sheet_count": int(new_sheet_count),
                    "remaining_area": int(new_remaining_area),
                    "trace": new_trace,
                    "order": state_counter,
                    "best_bucket": max(int(state.get("best_bucket") or 0), int(cand.get("best_bucket") or 0)),
                    "free_promise": max(int(state.get("free_promise") or 0), int(cand.get("free_promise") or 0)),
                    "cut_count": int(state.get("cut_count") or 0) + int(cand.get("cut_count") or 0),
                })
                made_progress = True

        if stats["time_budget_hit"] or stats.get("cancelled"):
            break
        if not made_progress:
            break
        if not next_states:
            if best_complete_refs is not None:
                break
            continue

        ranked = []
        seen = {}
        for state in next_states:
            key = _v3_partial_state_key(
                state, piece_by_bit, area_by_bit, item_cache, area_cache,
                candidate_pool, sheet_w, sheet_h, kerf, stats,
            )
            sig = (int(state["remaining_mask"]), int(state["sheet_count"]))
            previous = seen.get(sig)
            if previous is None or key < previous[0]:
                seen[sig] = (key, state)
        for key, state in seen.values():
            ranked.append((key, int(state.get("order") or 0), state))
        ranked.sort(key=lambda item: (item[0], item[1]))
        beam = [state for _key, _order, state in ranked[:max(1, int(beam_width or 160))]]
        stats["peak_beam_size"] = max(int(stats.get("peak_beam_size") or 0), len(beam))

        if best_complete_refs is not None:
            min_possible = min(
                (
                    int(state["sheet_count"])
                    + _v3_area_lower_bound(remaining_area(int(state["remaining_mask"])), sheet_w, sheet_h)
                    for state in beam
                ),
                default=999,
            )
            if min_possible > best_sheet_count:
                break

    if best_complete_refs is None:
        best_state = min(
            beam,
            key=lambda state: _v3_partial_state_key(
                state, piece_by_bit, area_by_bit, item_cache, area_cache,
                candidate_pool, sheet_w, sheet_h, kerf, stats,
            ),
        )
        sheets = materialize_refs(best_state.get("refs") or ())
        unplaced = remaining_items(int(best_state["remaining_mask"]))
        sc = score(sheets, unplaced)
        stats["best_pattern_strategy"] = _layout_strategy(sheets)
        stats.update(_layout_metric_fields(sheets))
        stats["search_elapsed_ms"] = max(1, _elapsed_ms(started_at))
        return sheets, 0, sc, unplaced, stats

    best_complete = materialize_refs(best_complete_refs)
    if first_feasible:
        stats["best_pattern_strategy"] = _layout_strategy(best_complete) or "+".join(best_trace[-4:])
        stats.update(_layout_metric_fields(best_complete))
        stats["search_elapsed_ms"] = max(1, _elapsed_ms(started_at))
        return best_complete, 0, best_score, [], stats

    improved = _v2_local_improve(best_complete, pieces, sheet_w, sheet_h, kerf, deadline=deadline)
    improved_score = score(improved, [])
    if improved_score < best_score:
        best_complete = improved
        best_score = improved_score
        emit_progress("best")
    else:
        best_complete = improved
        best_score = improved_score
    stats["best_pattern_strategy"] = _layout_strategy(best_complete) or "+".join(best_trace[-4:])
    stats.update(_layout_metric_fields(best_complete))
    if _deadline_hit(deadline):
        stats["time_budget_hit"] = True
    stats["search_elapsed_ms"] = max(1, _elapsed_ms(started_at))
    emit_progress("final")
    return best_complete, 0, best_score, [], stats


def search_v2_orientations(
    pieces,
    sheet_w,
    sheet_h,
    *,
    kerf=3,
    n_seeds=400,
    time_budget_s=None,
    beam_width=160,
    first_feasible=False,
    progress_callback=None,
    cancel_callback=None,
):
    """Try both sheet orientations with the v2 beam search."""
    global _LAST_V2_METRICS
    candidates = []
    total_states = 0
    total_candidates = 0
    time_hit = False
    total_strategy_runtime = {}
    total_strategy_counts = {}
    started_at = time.monotonic()
    deadline = started_at + float(time_budget_s) if time_budget_s else None
    orientations = ((sheet_w, sheet_h), (sheet_h, sheet_w))
    for idx, dims in enumerate(orientations):
        if _cancel_requested(cancel_callback):
            time_hit = True
            break
        if _deadline_hit(deadline, 0.02):
            time_hit = True
            break
        orientation_budget = None
        if deadline:
            orientations_left = max(1, len(orientations) - idx)
            remaining_budget = max(0.1, deadline - time.monotonic())
            if idx == 0 and len(pieces) >= 120 and remaining_budget >= 24.0:
                orientation_budget = min(
                    max(24.0, remaining_budget * 0.96),
                    max(0.1, remaining_budget - 0.05),
                )
            else:
                orientation_budget = max(0.1, remaining_budget / orientations_left)
        sheets, seed, sc, unplaced, stats = search_v2(
            pieces,
            dims[0],
            dims[1],
            kerf=kerf,
            n_seeds=n_seeds,
            time_budget_s=orientation_budget,
            beam_width=beam_width,
            first_feasible=first_feasible,
            progress_callback=progress_callback,
            cancel_callback=cancel_callback,
        )
        total_states += int(stats.get("beam_states_evaluated") or 0)
        total_candidates += int(stats.get("pattern_candidates_evaluated") or 0)
        time_hit = time_hit or bool(stats.get("time_budget_hit"))
        for key, value in (stats.get("strategy_runtime_ms") or {}).items():
            total_strategy_runtime[key] = int(total_strategy_runtime.get(key) or 0) + int(value or 0)
        for key, value in (stats.get("strategy_candidate_counts") or {}).items():
            total_strategy_counts[key] = int(total_strategy_counts.get(key) or 0) + int(value or 0)
        candidates.append((sc, seed, dims, sheets, unplaced, stats))
    if not candidates:
        _LAST_V2_METRICS = {
            "pattern_engine_version": "v2_beam",
            "beam_states_evaluated": total_states,
            "pattern_candidates_evaluated": total_candidates,
            "time_budget_hit": True,
            "selected_sheet_orientation": "",
            "search_elapsed_ms": max(1, int((time.monotonic() - started_at) * 1000)),
        }
        return [], 0, score([], pieces), list(pieces)
    sc, seed, dims, sheets, unplaced, stats = min(candidates, key=lambda c: (c[0], c[1]))
    _LAST_V2_METRICS = dict(stats)
    _LAST_V2_METRICS.update({
        "pattern_engine_version": "v2_beam",
        "beam_states_evaluated": total_states,
        "pattern_candidates_evaluated": total_candidates,
        "time_budget_hit": bool(time_hit),
        "selected_sheet_orientation": "%sx%s" % (dims[0], dims[1]),
        "search_elapsed_ms": max(1, int((time.monotonic() - started_at) * 1000)),
        "strategy_runtime_ms": total_strategy_runtime or stats.get("strategy_runtime_ms") or {},
        "strategy_candidate_counts": total_strategy_counts or stats.get("strategy_candidate_counts") or {},
    })
    return sheets, seed, sc, unplaced


def search_v3_orientations(
    pieces,
    sheet_w,
    sheet_h,
    *,
    kerf=3,
    n_seeds=400,
    time_budget_s=None,
    beam_width=160,
    first_feasible=False,
    progress_callback=None,
    cancel_callback=None,
):
    """Try both sheet orientations with the v3 shadow compact beam search."""
    global _LAST_V3_METRICS
    candidates = []
    total_states = 0
    total_candidates = 0
    time_hit = False
    total_strategy_runtime = {}
    total_strategy_counts = {}
    aggregate = {
        "candidate_cache_hits": 0,
        "candidate_cache_misses": 0,
        "remaining_cache_hits": 0,
        "remaining_cache_misses": 0,
        "candidate_pool_size": 0,
        "materialisations": 0,
        "materialised_sheets": 0,
        "clone_count_avoided": 0,
        "exact_score_evaluations": 0,
        "peak_beam_size": 0,
        "candidate_generation_ms": 0,
        "candidate_compile_ms": 0,
        "fast_incumbent_exit_count": 0,
        "orientation_pruned_count": 0,
    }
    started_at = time.monotonic()
    deadline = started_at + float(time_budget_s) if time_budget_s else None
    if len(pieces) >= 100:
        orientations = ((sheet_h, sheet_w), (sheet_w, sheet_h))
    else:
        orientations = ((sheet_w, sheet_h), (sheet_h, sheet_w))
    for idx, dims in enumerate(orientations):
        if _cancel_requested(cancel_callback):
            time_hit = True
            break
        if _deadline_hit(deadline, 0.02):
            time_hit = True
            break
        orientation_budget = None
        if deadline:
            orientations_left = max(1, len(orientations) - idx)
            remaining_budget = max(0.1, deadline - time.monotonic())
            if idx == 0 and len(pieces) >= 120 and remaining_budget >= 24.0:
                orientation_budget = min(
                    max(24.0, remaining_budget * 0.96),
                    max(0.1, remaining_budget - 0.05),
                )
            else:
                orientation_budget = max(0.1, remaining_budget / orientations_left)
        sheets, seed, sc, unplaced, stats = search_v3(
            pieces,
            dims[0],
            dims[1],
            kerf=kerf,
            n_seeds=n_seeds,
            time_budget_s=orientation_budget,
            beam_width=beam_width,
            first_feasible=first_feasible,
            progress_callback=progress_callback,
            cancel_callback=cancel_callback,
        )
        total_states += int(stats.get("beam_states_evaluated") or 0)
        total_candidates += int(stats.get("pattern_candidates_evaluated") or 0)
        time_hit = time_hit or bool(stats.get("time_budget_hit"))
        for key, value in (stats.get("strategy_runtime_ms") or {}).items():
            total_strategy_runtime[key] = int(total_strategy_runtime.get(key) or 0) + int(value or 0)
        for key, value in (stats.get("strategy_candidate_counts") or {}).items():
            total_strategy_counts[key] = int(total_strategy_counts.get(key) or 0) + int(value or 0)
        for key in aggregate:
            if key == "fast_incumbent_exit_count":
                aggregate[key] = int(aggregate.get(key) or 0) + (1 if stats.get("fast_incumbent_exit") else 0)
            elif key == "orientation_pruned_count":
                continue
            else:
                aggregate[key] = int(aggregate.get(key) or 0) + int(stats.get(key) or 0)
        candidates.append((sc, seed, dims, sheets, unplaced, stats))
        if (
            idx == 0
            and len(pieces) >= 100
            and stats.get("fast_incumbent_exit")
            and not unplaced
        ):
            aggregate["orientation_pruned_count"] = int(aggregate.get("orientation_pruned_count") or 0) + 1
            break
    if not candidates:
        _LAST_V3_METRICS = {
            "pattern_engine_version": "v3_shadow_compact",
            "beam_states_evaluated": total_states,
            "pattern_candidates_evaluated": total_candidates,
            "time_budget_hit": True,
            "selected_sheet_orientation": "",
            "search_elapsed_ms": max(1, int((time.monotonic() - started_at) * 1000)),
            "parity_status": "shadow_only",
        }
        _LAST_V3_METRICS.update(aggregate)
        return [], 0, score([], pieces), list(pieces)
    sc, seed, dims, sheets, unplaced, stats = min(candidates, key=lambda c: (c[0], c[1]))
    _LAST_V3_METRICS = dict(stats)
    _LAST_V3_METRICS.update({
        "pattern_engine_version": "v3_shadow_compact",
        "beam_states_evaluated": total_states,
        "pattern_candidates_evaluated": total_candidates,
        "time_budget_hit": bool(time_hit),
        "selected_sheet_orientation": "%sx%s" % (dims[0], dims[1]),
        "search_elapsed_ms": max(1, int((time.monotonic() - started_at) * 1000)),
        "strategy_runtime_ms": total_strategy_runtime or stats.get("strategy_runtime_ms") or {},
        "strategy_candidate_counts": total_strategy_counts or stats.get("strategy_candidate_counts") or {},
        "parity_status": "shadow_only",
    })
    _LAST_V3_METRICS.update(aggregate)
    return sheets, seed, sc, unplaced
