"""Nesting layout regression suite.

Runs a set of reference jobs through the live guillotine optimiser and checks
each against assertions describing the DESIRED CutList-style layout. Run via:
    odoo shell -d cutmyplastic < nesting_suite.py

Each case asserts properties (not exact coordinates) so the test is robust to
trivial position shifts but catches real layout regressions:
  - util_pct / sheet count (efficiency)
  - max_cuts (panel-saw effort)
  - uniform_rows: every band (pieces sharing a y) has a single piece height
  - all_same_orientation: optional, pieces not mixed rotated/un-rotated in a band
"""
from collections import defaultdict

Job = env["tp.nesting.job"].sudo()
SF = env["tp.sheet.format"].sudo()
Sandbox = env["tp.nesting.sandbox"].sudo()


def sources_for(code):
    f = SF.search([("product_id.default_code", "=", code)], limit=1)
    return [Sandbox._tp_sheet_format_source(s) for s in f._tp_quote_sibling_sheets(f)]


def util_and_sheets(plan):
    s = u = 0.0
    sheets = 0
    for b in plan["bins"]:
        src = b["source"]
        if src.get("is_offcut"):
            continue
        sheets += 1
        s += src["width_mm"] * src["height_mm"]
        u += sum(p["fit_w"] * p["fit_h"] for p in b["placements"])
    return (100 * u / s if s else 0.0), sheets


def bands_uniform(plan):
    """True if every band (pieces sharing y) has a single piece height."""
    for b in plan["bins"]:
        rows = defaultdict(set)
        for p in b["placements"]:
            rows[int(p["y"])].add(int(p["fit_h"]))
        for hs in rows.values():
            if len(hs) > 1:
                return False
    return True


CASES = [
    {
        # DESIRED (CutList): 4 panels rotated to 755w x 300h, stacked as 4 rows
        # filling the 1220 height; small piece beside. One big clean offcut.
        "name": "8mm 4x300x755 + 250x300",
        "code": "ACR-CLR-000-8MM-2440X1220",
        "cuts": [{"width_mm": 300, "height_mm": 755}] * 4 + [{"width_mm": 300, "height_mm": 250}],
        "min_util": 30.0, "max_sheets": 1, "max_cuts": 7, "uniform_rows": False,
        "want_rows": True,  # the four 755-long panels should run horizontally (fit_w >= fit_h)
    },
    {
        "name": "5x 867x147 (clean bands)",
        "code": "ACR-CLR-000-3MM-2440X1220",
        "cuts": [{"width_mm": 867, "height_mm": 147}] * 5,
        "min_util": 20.0, "max_sheets": 1, "max_cuts": 6, "uniform_rows": True,
    },
    {
        # mixed sizes that previously produced a NON-guillotine (uncuttable) nest
        "name": "12pc mixed (guillotine-valid)",
        "code": "ACR-CLR-000-3MM-2440X1220",
        "cuts": [{"width_mm": 333, "height_mm": 333}] * 4
                + [{"width_mm": 730, "height_mm": 500}] * 2
                + [{"width_mm": 1200, "height_mm": 180}] * 6,
        "min_util": 30.0, "max_sheets": 1, "max_cuts": 40, "uniform_rows": False,
    },
    {
        # 36 identical pieces must pack into a CLEAN GRID (1 edge offcut), not
        # scattered — the chaotic real-world S00423 layout.
        "name": "36x 190x365 clean grid",
        "code": "ACR-CLR-000-3MM-2440X1220",
        "cuts": [{"width_mm": 190, "height_mm": 365}] * 36,
        "min_util": 80.0, "max_sheets": 1, "max_cuts": 60, "uniform_rows": False,
        "max_offcuts": 3,
    },
    {
        # real 21-piece job (18x S00423 + 2x S00429 + 1x S00417) — must fit ONE
        # sheet, grouped, not scattered.
        "name": "21pc real (S00423/429/417)",
        "code": "ACR-CLR-000-3MM-2440X1220",
        "cuts": [{"width_mm": 190, "height_mm": 365}] * 18
                + [{"width_mm": 600, "height_mm": 800}] * 2
                + [{"width_mm": 455, "height_mm": 650}] * 1,
        "min_util": 60.0, "max_sheets": 1, "max_cuts": 60, "uniform_rows": False,
        "max_offcuts": 6,
    },
    {
        # full real 3mm job: 36x S00423 + 8 distinct S00432 + 2x S00429 + S00417.
        # Must dedicate ONE clean grid sheet to the 36 identical S00423s (CutList
        # trick), not scatter them among the big pieces.
        "name": "full mixed real (grid sheet)",
        "code": "ACR-CLR-000-3MM-2440X1220",
        "cuts": ([{"width_mm": 365, "height_mm": 190}] * 36
                 + [{"width_mm": 600, "height_mm": 800}] * 2
                 + [{"width_mm": 455, "height_mm": 650}]
                 + [{"width_mm": 351, "height_mm": 490}, {"width_mm": 404, "height_mm": 305},
                    {"width_mm": 597, "height_mm": 420}]
                 + [{"width_mm": 597, "height_mm": 422}] * 3
                 + [{"width_mm": 598, "height_mm": 429}, {"width_mm": 606, "height_mm": 422},
                    {"width_mm": 720, "height_mm": 771}]),
        # NOTE: only 2440x1220 sheets are stocked now (the 2490/3050 formats were
        # intentionally removed), so this needs 3 sheets, not 2.
        "min_util": 65.0, "max_sheets": 3, "max_cuts": 90, "uniform_rows": False,
        "grid_sheet_of": (365, 190, 36),  # one sheet must hold all 36 of this type
    },
    {
        # S00033 — only 2440x1220 is stocked now (2490/3050 formats removed), so
        # the multi-size 91% mix is no longer achievable; ~74% on 5 sheets is the
        # realistic best. Kept as a regression guard against worse.
        "name": "S00033 (2440-only)",
        "code": "ACR-CLR-000-4.5MM-2440X1220",
        "cuts": [{"width_mm": W, "height_mm": L} for (L, W, Q) in [
            (805, 850, 2), (990, 850, 1), (990, 1165, 1), (975, 840, 2), (810, 840, 2),
            (805, 335, 2), (990, 335, 1), (975, 1160, 2), (975, 435, 2), (810, 435, 2),
        ] for _ in range(Q)],
        "min_util": 70.0, "max_sheets": 5, "max_cuts": 45, "uniform_rows": False,
    },
    {
        "name": "10mm 4-piece compact",
        "code": "ACR-CLR-000-10MM-2440X1220",
        "cuts": [{"width_mm": 950, "height_mm": 165}, {"width_mm": 550, "height_mm": 527},
                 {"width_mm": 550, "height_mm": 350}, {"width_mm": 250, "height_mm": 300}],
        "min_util": 23.0, "max_sheets": 1, "max_cuts": 10, "uniform_rows": False,
    },
]

print("=" * 70)
passed = 0
for c in CASES:
    plan = Job._tp_run_guillotine_saw_optimal(
        c["cuts"], sources_for(c["code"]), kerf_mm=3, trim_edge_mm=0,
        lot_sources=[], time_budget_s=10,
    )
    util, sheets = util_and_sheets(plan)
    cuts = Job._tp_count_guillotine_cuts(plan)
    uniform = bands_uniform(plan)
    fails = []
    if util < c["min_util"]:
        fails.append("util %.1f%% < %.1f%%" % (util, c["min_util"]))
    if sheets > c["max_sheets"]:
        fails.append("sheets %d > %d" % (sheets, c["max_sheets"]))
    if cuts > c["max_cuts"]:
        fails.append("cuts %d > %d" % (cuts, c["max_cuts"]))
    if c.get("uniform_rows") and not uniform:
        fails.append("bands NOT uniform-height")
    if c.get("want_rows"):
        # the long panels (longest side >= 700) should run horizontally
        longpanels = [p for b in plan["bins"] for p in b["placements"]
                      if max(p["fit_w"], p["fit_h"]) >= 700]
        if longpanels and not all(p["fit_w"] >= p["fit_h"] for p in longpanels):
            fails.append("long panels not laid as horizontal rows")
    if c.get("max_offcuts") is not None:
        from odoo.addons.tp_sheet_nesting.models.services.tp_guillotine_cuts import offcut_rects
        n_off = 0
        for b in plan["bins"]:
            rects = [(p["x"], p["y"], p["fit_w"], p["fit_h"]) for p in b["placements"]]
            n_off += len(offcut_rects(rects, b["source"]["width_mm"], b["source"]["height_mm"], min_side=80))
        if n_off > c["max_offcuts"]:
            fails.append("offcuts %d > %d (scattered)" % (n_off, c["max_offcuts"]))
    if c.get("grid_sheet_of"):
        gw, gh, gn = c["grid_sheet_of"]
        ok = any(sum(1 for p in b["placements"]
                     if {int(p["fit_w"]), int(p["fit_h"])} == {gw, gh}) >= gn
                 for b in plan["bins"])
        if not ok:
            fails.append("no dedicated grid sheet for %dx%d x%d" % (gw, gh, gn))
    # EVERY layout must be panel-saw cuttable — hard requirement.
    if not Job._tp_plan_is_guillotine(plan):
        fails.append("NOT guillotine-cuttable")
    status = "PASS" if not fails else "FAIL"
    if not fails:
        passed += 1
    print("[%s] %-28s util=%.1f%% sheets=%d cuts=%d uniform=%s"
          % (status, c["name"], util, sheets, cuts, uniform))
    for f in fails:
        print("        -> %s" % f)
print("=" * 70)
print("RESULT: %d/%d passed" % (passed, len(CASES)))
