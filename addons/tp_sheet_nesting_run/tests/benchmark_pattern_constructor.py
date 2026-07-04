#!/usr/bin/env python3
"""Benchmark v2 production vs v3 shadow pattern constructor on local CSVs.

Usage:
    python3 tests/benchmark_pattern_constructor.py

Set TP_PATTERN_BENCHMARK_ROOT=/path/to/root if the Comparison folders are not
under /root.
Set TP_PATTERN_BENCHMARK_FILTER=substring to run a smaller targeted subset.
Set TP_PATTERN_BENCHMARK_REPEAT_V3=1 to run v3 twice and enforce determinism.
"""
import csv
import glob
import importlib.util
import os
import sys
import time


MODULE_PATH = (
    "/opt/odoo/addons/tp_sheet_nesting_run/models/services/"
    "tp_pattern_constructor.py"
)

BENCHMARK_TARGETS = {
    "comparison 2": {
        "expected_sheets": 3,
        "min_full_dim_offcut": (2440, 659),
        "allowed_strategy_prefixes": ("anchor_", "mixed_repeat", "repeat_", "v2_"),
        "max_runtime_ms": 12000,
    },
    "comparison 3": {
        "expected_sheets": 1,
        "min_full_dim_offcut": None,
        "allowed_strategy_prefixes": ("anchor_shelf", "v2_anchor_shelf", "guillotine_"),
        "max_runtime_ms": 12000,
    },
    "comparison 4": {
        "expected_sheets": 3,
        "min_full_dim_offcut": (449, 1220),
        "allowed_strategy_prefixes": ("tall_strip", "v2_tall_strip", "repeat_", "guillotine_"),
        "max_runtime_ms": 12000,
    },
    "comparison": {
        "expected_sheets": 3,
        "min_full_dim_offcut": (150, 2440),
        "allowed_strategy_prefixes": ("two_column", "wide_shelf", "v2_", "anchor_", "repeat_", "guillotine_"),
        "max_runtime_ms": 12000,
    },
    "comparison 5": {
        "expected_sheets": 7,
        "min_full_dim_offcut": (2440, 667),
        "allowed_strategy_prefixes": ("large_repeat", "repeat_", "v2_", "guillotine_"),
        "max_runtime_ms": 12000,
    },
    "comparison 6 - 100 panel stress": {
        "expected_sheets": 7,
        "min_full_dim_offcut": None,
        "allowed_strategy_prefixes": ("guillotine_", "v2_", "anchor_", "repeat_"),
        "max_runtime_ms": 12000,
    },
    "comparison 7 - 300 panel stress": {
        "expected_sheets": 19,
        "min_full_dim_offcut": None,
        "allowed_strategy_prefixes": ("guillotine_", "v2_", "anchor_", "repeat_"),
        "max_runtime_ms": 12000,
    },
    "comparison 8 - 200 varied stress": {
        "expected_sheets": 17,
        "min_full_dim_offcut": (864, 2440),
        "allowed_strategy_prefixes": ("guillotine_", "v2_", "anchor_", "repeat_"),
        "max_runtime_ms": 12000,
    },
    "comparison 9 - 300 unique stress": {
        "expected_sheets": 33,
        "min_full_dim_offcut": (749, 2440),
        "allowed_strategy_prefixes": ("guillotine_", "v2_", "anchor_", "repeat_"),
        "max_runtime_ms": 12000,
    },
}


def _load_constructor():
    spec = importlib.util.spec_from_file_location("tp_pattern_constructor", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cell(row, *names):
    lowered = {str(k).strip().lower(): v for k, v in row.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value not in (None, ""):
            return value
    return None


def _parse_csv(path):
    pieces = []
    next_id = 0
    with open(path, newline="") as handle:
        for row in csv.DictReader(handle):
            enabled = str(_cell(row, "enabled") or "true").strip().lower()
            if enabled in ("false", "0", "no", "n", "off", "disabled"):
                continue
            width_value = _cell(row, "width", "width_mm", "w")
            height_value = _cell(row, "height", "height_mm", "h", "length", "len")
            if width_value is None or height_value is None:
                continue
            qty = int(round(float(_cell(row, "qty", "quantity", "count") or 1)))
            width = int(round(float(width_value)))
            height = int(round(float(height_value)))
            for _idx in range(qty):
                pieces.append({"id": next_id, "w": width, "h": height})
                next_id += 1
    return pieces


def _comparison_paths(root):
    patterns = [
        os.path.join(root, "Comparison*", "**", "*.csv"),
        os.path.join(root, "comparison*", "**", "*.csv"),
        os.path.join(root, "Small order comparison", "**", "*.csv"),
    ]
    paths = []
    for pattern in patterns:
        paths.extend(glob.glob(pattern, recursive=True))
    return sorted(set(paths))


def _expected_sheet_count(path):
    return len(glob.glob(os.path.join(os.path.dirname(path), "cutlistoptimiser sheet *.png")))


def _target_for_path(path):
    name = os.path.basename(os.path.dirname(path)).lower()
    return BENCHMARK_TARGETS.get(name, {})


def _offcut_info(module, sheets, *, full_dim_only=False):
    best = {"value": 0.0, "w": 0, "h": 0, "sheet": 0, "full_dim": False}
    for sheet_idx, sheet in enumerate(sheets, 1):
        for _x, _y, w, h in module._offcut_rects(sheet):
            full_dim = abs(w - sheet.w) <= 5 or abs(h - sheet.h) <= 5
            if full_dim_only and not full_dim:
                continue
            value = module._offcut_value(w, h, sheet.w, sheet.h)
            if value > best["value"]:
                best = {
                    "value": float(value),
                    "w": int(w),
                    "h": int(h),
                    "sheet": int(sheet_idx),
                    "full_dim": bool(full_dim),
                }
    return best


def _fmt_offcut(info):
    if not info or not info.get("w") or not info.get("h"):
        return "-"
    suffix = " full" if info.get("full_dim") else ""
    return "%dx%d%s sheet%d" % (info["w"], info["h"], suffix, info["sheet"])


def _offcut_meets(target_dims, info):
    if not target_dims:
        return True
    if not info or not info.get("full_dim"):
        return False
    target = sorted((int(target_dims[0]), int(target_dims[1])))
    actual = sorted((int(info.get("w") or 0), int(info.get("h") or 0)))
    return actual[0] >= target[0] and actual[1] >= target[1]


def _sliver_count(module, sheets):
    count = 0
    for sheet in sheets:
        for _x, _y, w, h in module._offcut_rects(sheet):
            if min(w, h) < getattr(module, "_UNSAFE_SLIVER_MM", 60):
                count += 1
    return count


def _saw_totals(module, sheets):
    fence = 0
    cuts = 0
    for sheet in sheets:
        f, c = module.saw_metrics(sheet)
        fence += int(f)
        cuts += int(c)
    return fence, cuts


def _geometry_reasons(sheets, total_pieces):
    reasons = []
    seen = set()
    for sheet_idx, sheet in enumerate(sheets, 1):
        placements = list(sheet.placements or [])
        for idx, a in enumerate(placements):
            item = int(a["item"])
            if item in seen:
                reasons.append("duplicate_piece_id=%s" % item)
            seen.add(item)
            ax0 = int(a["x"])
            ay0 = int(a["y"])
            ax1 = ax0 + int(a["w"])
            ay1 = ay0 + int(a["h"])
            if ax0 < 0 or ay0 < 0 or ax1 > int(sheet.w) or ay1 > int(sheet.h):
                reasons.append("sheet%d_out_of_bounds_piece=%s" % (sheet_idx, item))
            for b in placements[idx + 1:]:
                bx0 = int(b["x"])
                by0 = int(b["y"])
                bx1 = bx0 + int(b["w"])
                by1 = by0 + int(b["h"])
                if ax0 < bx1 and bx0 < ax1 and ay0 < by1 and by0 < ay1:
                    reasons.append("sheet%d_overlap=%s/%s" % (sheet_idx, item, int(b["item"])))
    if len(seen) != total_pieces:
        reasons.append("placed_unique=%d expected=%d" % (len(seen), total_pieces))
    return reasons


def _strategy_allowed(strategies, prefixes):
    if not prefixes:
        return True
    for strategy in strategies:
        if not strategy:
            return False
        if not any(strategy.startswith(prefix) or ("+" in strategy and any(part.startswith(prefix) for part in strategy.split("+"))) for prefix in prefixes):
            return False
    return True


def _layout_signature(sheets):
    return tuple(
        (
            int(sheet.w),
            int(sheet.h),
            getattr(sheet, "strategy", ""),
            tuple(
                sorted(
                    (
                        int(pl["item"]),
                        int(pl["x"]),
                        int(pl["y"]),
                        int(pl["w"]),
                        int(pl["h"]),
                    )
                    for pl in (sheet.placements or [])
                )
            ),
        )
        for sheet in (sheets or [])
    )


def _score_better(a, b):
    return a < b


def _score_worse(a, b):
    return a > b


def main():
    root = os.environ.get("TP_PATTERN_BENCHMARK_ROOT", "/root")
    path_filter = (os.environ.get("TP_PATTERN_BENCHMARK_FILTER") or "").strip().lower()
    repeat_v3 = str(os.environ.get("TP_PATTERN_BENCHMARK_REPEAT_V3") or "").strip().lower() in ("1", "true", "yes", "on")
    module = _load_constructor()
    paths = _comparison_paths(root)
    if path_filter:
        paths = [path for path in paths if path_filter in path.lower()]
    if not paths:
        print("No comparison CSVs found under %s" % root)
        return 1

    ok = True
    for path in paths:
        pieces = _parse_csv(path)
        target = _target_for_path(path)
        expected = int(target.get("expected_sheets") or _expected_sheet_count(path) or 0)

        if hasattr(module, "reset_shadow_profile"):
            module.reset_shadow_profile(True)
        t0 = time.monotonic()
        v2_sheets, _v2_seed, v2_score, v2_unplaced = module.search_v2_orientations(
            pieces, 2440, 1220, kerf=3, n_seeds=20, time_budget_s=10, beam_width=160,
        )
        v2_ms = int((time.monotonic() - t0) * 1000)
        metrics = module.last_search_v2_metrics()
        v2_profile = module.shadow_profile_metrics() if hasattr(module, "shadow_profile_metrics") else {}
        if hasattr(module, "reset_shadow_profile"):
            module.reset_shadow_profile(False)

        v2_best = _offcut_info(module, v2_sheets)
        v2_full = _offcut_info(module, v2_sheets, full_dim_only=True)
        fence, cuts = _saw_totals(module, v2_sheets)
        slivers = _sliver_count(module, v2_sheets)
        strategies = [getattr(sheet, "strategy", "") for sheet in v2_sheets]

        t0 = time.monotonic()
        v3_sheets, _v3_seed, v3_score, v3_unplaced = module.search_v3_orientations(
            pieces, 2440, 1220, kerf=3, n_seeds=20, time_budget_s=10, beam_width=160,
        )
        v3_ms = int((time.monotonic() - t0) * 1000)
        v3_metrics = module.last_search_v3_metrics()
        v3_best = _offcut_info(module, v3_sheets)
        v3_full = _offcut_info(module, v3_sheets, full_dim_only=True)
        v3_fence, v3_cuts = _saw_totals(module, v3_sheets)
        v3_slivers = _sliver_count(module, v3_sheets)
        v3_strategies = [getattr(sheet, "strategy", "") for sheet in v3_sheets]
        v3_repeat = None
        if repeat_v3:
            r_sheets, _r_seed, r_score, r_unplaced = module.search_v3_orientations(
                pieces, 2440, 1220, kerf=3, n_seeds=20, time_budget_s=10, beam_width=160,
            )
            v3_repeat = {
                "score": r_score,
                "unplaced": len(r_unplaced),
                "signature": _layout_signature(r_sheets),
            }

        status = "PASS"
        reasons = []
        v3_reasons = []
        if v2_unplaced:
            reasons.append("v2_unplaced=%d" % len(v2_unplaced))
        if expected and len(v2_sheets) != expected:
            reasons.append("v2_sheets=%d expected=%d" % (len(v2_sheets), expected))
        if not _offcut_meets(target.get("min_full_dim_offcut"), v2_full):
            reasons.append(
                "full_dim_offcut=%s target>=%dx%d"
                % (_fmt_offcut(v2_full), target["min_full_dim_offcut"][0], target["min_full_dim_offcut"][1])
            )
        if target.get("max_runtime_ms") and v2_ms > int(target["max_runtime_ms"]) and not metrics.get("time_budget_hit"):
            reasons.append("runtime_ms=%d max=%d" % (v2_ms, int(target["max_runtime_ms"])))
        if not _strategy_allowed(strategies, target.get("allowed_strategy_prefixes")):
            reasons.append("unexpected_strategies=%s" % strategies)
        reasons.extend(_geometry_reasons(v2_sheets, len(pieces)))

        if v3_unplaced:
            v3_reasons.append("v3_unplaced=%d" % len(v3_unplaced))
        v3_reasons.extend(_geometry_reasons(v3_sheets, len(pieces)))
        if len(v3_sheets) > len(v2_sheets):
            v3_reasons.append("v3_sheets=%d > v2_sheets=%d" % (len(v3_sheets), len(v2_sheets)))
        if _score_worse(v3_score, v2_score):
            v3_reasons.append("v3_score=%s worse_than_v2=%s" % (v3_score, v2_score))
        if int(v3_full.get("value") or 0) < int(v2_full.get("value") or 0):
            v3_reasons.append("v3_full_dim_offcut=%s < v2=%s" % (_fmt_offcut(v3_full), _fmt_offcut(v2_full)))
        if int(v3_best.get("value") or 0) < int(v2_best.get("value") or 0):
            v3_reasons.append("v3_best_offcut=%s < v2=%s" % (_fmt_offcut(v3_best), _fmt_offcut(v2_best)))
        if v3_cuts > cuts and not _score_better(v3_score, v2_score):
            v3_reasons.append("v3_cuts=%d > v2_cuts=%d" % (v3_cuts, cuts))
        if v2_ms and v3_ms > int(v2_ms * 1.10) and not _score_better(v3_score, v2_score):
            v3_reasons.append("v3_ms=%d slower_than_v2_ms=%d by >10%%" % (v3_ms, v2_ms))
        basename = os.path.basename(os.path.dirname(path)).lower()
        if basename in (
            "comparison 7 - 300 panel stress",
            "comparison 8 - 200 varied stress",
            "comparison 9 - 300 unique stress",
        ) and v3_ms and v2_ms and (float(v2_ms) / float(v3_ms)) < 2.0:
            v3_reasons.append("v3_speedup=%.2fx < 2.00x stress_target" % (float(v2_ms) / float(v3_ms)))
        if v3_repeat and (
            v3_repeat["score"] != v3_score
            or v3_repeat["unplaced"] != len(v3_unplaced)
            or v3_repeat["signature"] != _layout_signature(v3_sheets)
        ):
            v3_reasons.append("v3_repeat_run_not_deterministic")

        if reasons:
            status = "FAIL"
            ok = False
        v3_status = "PASS" if not v3_reasons else "SHADOW-FAIL"
        if v3_reasons:
            ok = False

        print("\n%s %s" % (status if status == "FAIL" else v3_status, path))
        print(
            "  v2:  sheets=%d score=%s unplaced=%d ms=%d states=%s candidates=%s hit=%s"
            % (
                len(v2_sheets), v2_score, len(v2_unplaced), v2_ms,
                metrics.get("beam_states_evaluated"),
                metrics.get("pattern_candidates_evaluated"),
                metrics.get("time_budget_hit"),
            )
        )
        print("       placed=%d/%d saw_cuts=%d fence=%d slivers=%d" % (
            sum(len(sheet.placements or []) for sheet in v2_sheets),
            len(pieces),
            cuts,
            fence,
            slivers,
        ))
        print("       best_offcut=%s best_full_dim=%s" % (_fmt_offcut(v2_best), _fmt_offcut(v2_full)))
        print("       strategies=%s" % strategies)
        print("       profile=%s" % v2_profile)
        print("       strategy_runtime_ms=%s" % (metrics.get("strategy_runtime_ms") or {}))
        print("       strategy_candidate_counts=%s" % (metrics.get("strategy_candidate_counts") or {}))
        speedup = (float(v2_ms) / float(v3_ms)) if v3_ms else 0.0
        print(
            "  v3:  sheets=%d score=%s unplaced=%d ms=%d speedup=%.2fx states=%s candidates=%s hit=%s"
            % (
                len(v3_sheets), v3_score, len(v3_unplaced), v3_ms, speedup,
                v3_metrics.get("beam_states_evaluated"),
                v3_metrics.get("pattern_candidates_evaluated"),
                v3_metrics.get("time_budget_hit"),
            )
        )
        print("       placed=%d/%d saw_cuts=%d fence=%d slivers=%d" % (
            sum(len(sheet.placements or []) for sheet in v3_sheets),
            len(pieces),
            v3_cuts,
            v3_fence,
            v3_slivers,
        ))
        print("       best_offcut=%s best_full_dim=%s" % (_fmt_offcut(v3_best), _fmt_offcut(v3_full)))
        print("       strategies=%s" % v3_strategies)
        print("       metrics=%s" % {
            "cache_hits": v3_metrics.get("candidate_cache_hits"),
            "cache_misses": v3_metrics.get("candidate_cache_misses"),
            "remaining_hits": v3_metrics.get("remaining_cache_hits"),
            "materialisations": v3_metrics.get("materialisations"),
            "clone_count_avoided": v3_metrics.get("clone_count_avoided"),
            "candidate_pool_size": v3_metrics.get("candidate_pool_size"),
            "peak_beam_size": v3_metrics.get("peak_beam_size"),
            "exact_scores": v3_metrics.get("exact_score_evaluations"),
            "generation_ms": v3_metrics.get("candidate_generation_ms"),
            "compile_ms": v3_metrics.get("candidate_compile_ms"),
            "fast_incumbent_exit": v3_metrics.get("fast_incumbent_exit"),
            "fast_incumbent_exit_count": v3_metrics.get("fast_incumbent_exit_count"),
            "fast_incumbent_reason": v3_metrics.get("fast_incumbent_reason"),
            "area_lb": v3_metrics.get("fast_incumbent_area_lower_bound"),
            "orientation_pruned_count": v3_metrics.get("orientation_pruned_count"),
        })
        print("       strategy_runtime_ms=%s" % (v3_metrics.get("strategy_runtime_ms") or {}))
        print("       strategy_candidate_counts=%s" % (v3_metrics.get("strategy_candidate_counts") or {}))
        if _layout_signature(v2_sheets) == _layout_signature(v3_sheets):
            print("       parity=exact_geometry_match")
        else:
            print("       parity=shadow_compare_only")
        if v3_repeat:
            print("       repeat_v3=deterministic")
        if reasons:
            print("  v2 reasons: %s" % ", ".join(reasons))
        if v3_reasons:
            print("  v3 reasons: %s" % ", ".join(v3_reasons))

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
