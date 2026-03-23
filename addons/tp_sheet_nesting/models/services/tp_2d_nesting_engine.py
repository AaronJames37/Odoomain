import time

from .kernels import get_nesting_kernel
from .tp_nesting_policy import TpNestingPolicy
from .tp_nesting_engine_geometry import TpNestingEngineGeometryMixin
from .tp_nesting_engine_scoring import TpNestingEngineScoringMixin
from .tp_nesting_engine_search import TpNestingEngineSearchMixin


class Tp2DNestingEngine(
    TpNestingEngineSearchMixin,
    TpNestingEngineScoringMixin,
    TpNestingEngineGeometryMixin,
):

    """2D nesting engine with deterministic multi-start search.

    Deterministic mode keeps greedy behavior.
    Optimal mode uses beam search plus a bounded local-improvement pass.
    """

    def __init__(
        self,
        *,
        kerf_mm=3,
        timeout_ms=2000,
        sheet_size_candidate_limit=25,
        beam_width=6,
        branch_cap=12,
        mode="optimal",
        kernel_name="maxrects",
        enable_local_improvement=True,
        local_improvement_max_steps=6,
        late_acceptance_window=4,
        local_neighbor_cap=18,
        enable_exact_refinement=True,
        exact_refinement_cut_threshold=8,
        exact_refinement_timeout_ms=250,
        scoring_preset="yield_first",
        waste_priority=1.0,
        offcut_reuse_priority=1.0,
        sheet_count_penalty=1.0,
        cost_sensitivity=1.0,
        debug_enabled=False,
        max_pieces=200,
        beam_width_cap=24,
        timeout_cap_ms=15000,
    ):
        self.kerf_mm = max(int(kerf_mm or 0), 0)
        self.timeout_ms = int(timeout_ms or 0)
        self.sheet_size_candidate_limit = max(1, int(sheet_size_candidate_limit or 1))
        self.max_pieces = max(1, int(max_pieces or 1))
        self.beam_width_cap = max(1, int(beam_width_cap or 1))
        self.timeout_cap_ms = max(0, int(timeout_cap_ms or 0))
        self.beam_width = max(1, int(beam_width or 1))
        self.branch_cap = max(1, int(branch_cap or 1))
        if self.beam_width > self.beam_width_cap:
            self.beam_width = self.beam_width_cap
        if self.branch_cap > self.beam_width_cap:
            self.branch_cap = self.beam_width_cap
        if self.timeout_cap_ms > 0 and self.timeout_ms > 0:
            self.timeout_ms = min(self.timeout_ms, self.timeout_cap_ms)
        self.mode = mode
        self.kernel_name = kernel_name or "maxrects"
        self.kernel = get_nesting_kernel(self.kernel_name, kerf_mm=self.kerf_mm)
        self.enable_local_improvement = bool(enable_local_improvement)
        self.local_improvement_max_steps = max(0, int(local_improvement_max_steps or 0))
        self.late_acceptance_window = max(1, int(late_acceptance_window or 1))
        self.local_neighbor_cap = max(3, int(local_neighbor_cap or 3))
        self.enable_exact_refinement = bool(enable_exact_refinement)
        self.exact_refinement_cut_threshold = max(1, int(exact_refinement_cut_threshold or 1))
        self.exact_refinement_timeout_ms = int(exact_refinement_timeout_ms or 0)
        if self.timeout_cap_ms > 0 and self.exact_refinement_timeout_ms > 0:
            self.exact_refinement_timeout_ms = min(self.exact_refinement_timeout_ms, self.timeout_cap_ms)
        self.policy = TpNestingPolicy(
            preset=scoring_preset,
            waste_priority=waste_priority,
            offcut_reuse_priority=offcut_reuse_priority,
            sheet_count_penalty=sheet_count_penalty,
            cost_sensitivity=cost_sensitivity,
        )
        self.debug_enabled = bool(debug_enabled)
        self.policy_area_anchor = 1.0
        self.policy_cost_anchor = 1.0
        self.started_at = time.monotonic()
        self.search_nodes = 0
        self.beam_expansions = 0
        self.local_steps = 0
        self.local_moves_accepted = 0
        self.exact_states_visited = 0
        self.exact_states_pruned = 0
        self.memo_hits = 0
        self.memo_prunes = 0
        self.early_infeasible_checks = 0

    def _check_timeout(self):
        if self.timeout_ms < 0:
            raise TimeoutError("2D nesting search exceeded timeout.")
        if self.timeout_ms == 0:
            return
        elapsed_ms = int((time.monotonic() - self.started_at) * 1000)
        if elapsed_ms > self.timeout_ms:
            raise TimeoutError("2D nesting search exceeded timeout.")

    def _check_exact_timeout(self, exact_started_at):
        if self.exact_refinement_timeout_ms < 0:
            raise TimeoutError("Exact refinement exceeded timeout.")
        if self.exact_refinement_timeout_ms == 0:
            return
        elapsed_ms = int((time.monotonic() - exact_started_at) * 1000)
        if elapsed_ms > self.exact_refinement_timeout_ms:
            raise TimeoutError("Exact refinement exceeded timeout.")

    def plan(self, *, cuts, sheet_lot_sources, sheet_format_sources):
        exact_refinement_used = False
        exact_refinement_timeout = False
        exact_refinement_improved = False
        exact_refinement_ms = 0
        debug_payload = {
            "guardrails": {
                "max_pieces": int(self.max_pieces),
                "beam_width_cap": int(self.beam_width_cap),
                "timeout_cap_ms": int(self.timeout_cap_ms),
                "effective_timeout_ms": int(self.timeout_ms),
                "effective_beam_width": int(self.beam_width),
                "effective_branch_cap": int(self.branch_cap),
            },
            "orderings": [],
            "rejections": [],
            "selected": {},
            "local_improvement": {},
            "exact_refinement": {},
        }
        self._prepare_policy_context(
            cuts=cuts,
            sheet_lot_sources=sheet_lot_sources,
            sheet_format_sources=sheet_format_sources,
        )
        if not cuts:
            return {
                "ok": True,
                "bins": [],
                "metrics": {
                    "search_nodes": self.search_nodes,
                    "search_ms": 0,
                    "full_sheet_count": 0,
                    "beam_expansions": self.beam_expansions,
                    "local_improvement_steps": self.local_steps,
                    "local_improvement_moves": self.local_moves_accepted,
                    "exact_refinement_used": exact_refinement_used,
                    "exact_refinement_timeout": exact_refinement_timeout,
                    "exact_refinement_improved": exact_refinement_improved,
                    "exact_refinement_ms": exact_refinement_ms,
                    "exact_states_visited": self.exact_states_visited,
                    "exact_states_pruned": self.exact_states_pruned,
                    "memo_hits": self.memo_hits,
                    "memo_prunes": self.memo_prunes,
                    "early_infeasible_checks": self.early_infeasible_checks,
                    "policy_preset": self.policy.preset,
                    "policy_weights": dict(self.policy.weights),
                    "max_pieces": self.max_pieces,
                    "beam_width_cap": self.beam_width_cap,
                    "timeout_cap_ms": self.timeout_cap_ms,
                    "effective_timeout_ms": self.timeout_ms,
                    "effective_beam_width": self.beam_width,
                    "effective_branch_cap": self.branch_cap,
                    "infeasible_reason": "",
                    "candidate_plan_count": 0,
                    "rejected_plan_count": 0,
                    "selected_order_name": "",
                    "score_breakdown": {},
                    "debug_artifact": debug_payload if self.debug_enabled else {},
                },
            }

        early_infeasible = self._early_infeasible_cut(
            cuts=cuts,
            sheet_lot_sources=sheet_lot_sources,
            sheet_format_sources=sheet_format_sources,
        )
        if early_infeasible:
            reason = str(early_infeasible.get("_reason") or "early_infeasible")
            debug_payload["early_check"] = {
                "status": "failed",
                "reason": reason,
                "error_cut": {
                    "width_mm": int(early_infeasible.get("width_mm", 0)),
                    "height_mm": int(early_infeasible.get("height_mm", 0)),
                },
            }
            return {
                "ok": False,
                "error_cut": {
                    "width_mm": int(early_infeasible.get("width_mm", 0)),
                    "height_mm": int(early_infeasible.get("height_mm", 0)),
                },
                "metrics": {
                    "search_nodes": self.search_nodes,
                    "search_ms": int((time.monotonic() - self.started_at) * 1000),
                    "full_sheet_count": 0,
                    "beam_expansions": self.beam_expansions,
                    "local_improvement_steps": self.local_steps,
                    "local_improvement_moves": self.local_moves_accepted,
                    "exact_refinement_used": exact_refinement_used,
                    "exact_refinement_timeout": exact_refinement_timeout,
                    "exact_refinement_improved": exact_refinement_improved,
                    "exact_refinement_ms": exact_refinement_ms,
                    "exact_states_visited": self.exact_states_visited,
                    "exact_states_pruned": self.exact_states_pruned,
                    "memo_hits": self.memo_hits,
                    "memo_prunes": self.memo_prunes,
                    "early_infeasible_checks": self.early_infeasible_checks,
                    "policy_preset": self.policy.preset,
                    "policy_weights": dict(self.policy.weights),
                    "max_pieces": self.max_pieces,
                    "beam_width_cap": self.beam_width_cap,
                    "timeout_cap_ms": self.timeout_cap_ms,
                    "effective_timeout_ms": self.timeout_ms,
                    "effective_beam_width": self.beam_width,
                    "effective_branch_cap": self.branch_cap,
                    "infeasible_reason": reason,
                    "candidate_plan_count": 0,
                    "rejected_plan_count": 0,
                    "selected_order_name": "",
                    "score_breakdown": {},
                    "debug_artifact": debug_payload if self.debug_enabled else {},
                },
            }
        debug_payload["early_check"] = {"status": "passed"}

        normalized_cuts = self._normalize_cuts(cuts)
        runs = []
        orderings = self._orderings(normalized_cuts) if self.mode == "optimal" else [("deterministic", list(normalized_cuts))]
        for order_name, ordered_cuts in orderings:
            self._check_timeout()
            branch_start_nodes = self.search_nodes
            branch_start_expansions = self.beam_expansions
            if self.mode == "optimal":
                result = self._run_ordering_beam(
                    ordered_cuts,
                    sheet_lot_sources=sheet_lot_sources,
                    sheet_format_sources=sheet_format_sources,
                )
            else:
                result = self._run_ordering_greedy(
                    ordered_cuts,
                    sheet_lot_sources=sheet_lot_sources,
                    sheet_format_sources=sheet_format_sources,
                )
            branch_nodes = self.search_nodes - branch_start_nodes
            branch_expansions = self.beam_expansions - branch_start_expansions
            if not result["ok"]:
                debug_payload["orderings"].append(
                    {
                        "order_name": order_name,
                        "status": "rejected_no_fit",
                        "error_cut": {
                            "width_mm": int(result["error_cut"].get("width_mm", 0)),
                            "height_mm": int(result["error_cut"].get("height_mm", 0)),
                        },
                        "search_nodes": int(branch_nodes),
                        "beam_expansions": int(branch_expansions),
                    }
                )
                runs.append(
                    {
                        "ok": False,
                        "order_name": order_name,
                        "error_cut": result["error_cut"],
                        "nodes": branch_nodes,
                        "expansions": branch_expansions,
                    }
                )
                continue
            bins = result["bins"]
            score = self._score_final(bins, order_name)
            debug_payload["orderings"].append(
                {
                    "order_name": order_name,
                    "status": "feasible",
                    "search_nodes": int(branch_nodes),
                    "beam_expansions": int(branch_expansions),
                    "full_sheet_count": int(len(bins)),
                    "score": self._score_components(score),
                }
            )
            runs.append(
                {
                    "ok": True,
                    "order_name": order_name,
                    "ordered_cuts": self._clone_cut_sequence(ordered_cuts),
                    "bins": bins,
                    "full_sheet_count": len(bins),
                    "score": score,
                    "nodes": branch_nodes,
                    "expansions": branch_expansions,
                }
            )

        successful = [run for run in runs if run["ok"]]
        if not successful:
            first_error = runs[0]["error_cut"] if runs else {"width_mm": 0, "height_mm": 0}
            return {
                "ok": False,
                "error_cut": first_error,
                "metrics": {
                    "search_nodes": self.search_nodes,
                    "search_ms": int((time.monotonic() - self.started_at) * 1000),
                    "full_sheet_count": 0,
                    "beam_expansions": self.beam_expansions,
                    "local_improvement_steps": self.local_steps,
                    "local_improvement_moves": self.local_moves_accepted,
                    "exact_refinement_used": exact_refinement_used,
                    "exact_refinement_timeout": exact_refinement_timeout,
                    "exact_refinement_improved": exact_refinement_improved,
                    "exact_refinement_ms": exact_refinement_ms,
                    "exact_states_visited": self.exact_states_visited,
                    "exact_states_pruned": self.exact_states_pruned,
                    "memo_hits": self.memo_hits,
                    "memo_prunes": self.memo_prunes,
                    "early_infeasible_checks": self.early_infeasible_checks,
                    "policy_preset": self.policy.preset,
                    "policy_weights": dict(self.policy.weights),
                    "max_pieces": self.max_pieces,
                    "beam_width_cap": self.beam_width_cap,
                    "timeout_cap_ms": self.timeout_cap_ms,
                    "effective_timeout_ms": self.timeout_ms,
                    "effective_beam_width": self.beam_width,
                    "effective_branch_cap": self.branch_cap,
                    "infeasible_reason": "no_compatible_source",
                    "candidate_plan_count": len(runs),
                    "rejected_plan_count": len(runs),
                    "selected_order_name": "",
                    "score_breakdown": {},
                    "debug_artifact": debug_payload if self.debug_enabled else {},
                },
            }

        best = min(successful, key=lambda run: (run["score"], run["order_name"]))
        best_seed_score = best["score"]
        if self.mode == "optimal":
            best = self._run_local_improvement(
                seed_run=best,
                sheet_lot_sources=sheet_lot_sources,
                sheet_format_sources=sheet_format_sources,
            )
            debug_payload["local_improvement"] = {
                "enabled": bool(self.enable_local_improvement),
                "steps": int(self.local_steps),
                "accepted_moves": int(self.local_moves_accepted),
                "seed_score": self._score_components(best_seed_score),
                "result_score": self._score_components(best["score"]),
            }
            if self._should_run_exact_refinement(best):
                exact_refinement_used = True
                try:
                    refined = self._run_exact_refinement(
                        seed_run=best,
                        sheet_lot_sources=sheet_lot_sources,
                        sheet_format_sources=sheet_format_sources,
                    )
                    exact_refinement_ms = int(refined.get("exact_refinement_ms", 0))
                    if refined["score"] <= best["score"]:
                        exact_refinement_improved = refined["score"] < best["score"]
                        best = refined
                except TimeoutError:
                    exact_refinement_timeout = True
                except Exception:
                    # Safe fallback to the best known feasible plan.
                    pass
                debug_payload["exact_refinement"] = {
                    "used": bool(exact_refinement_used),
                    "timeout": bool(exact_refinement_timeout),
                    "improved": bool(exact_refinement_improved),
                    "ms": int(exact_refinement_ms),
                    "states_visited": int(self.exact_states_visited),
                    "states_pruned": int(self.exact_states_pruned),
                }
        else:
            debug_payload["local_improvement"] = {"enabled": False}
            debug_payload["exact_refinement"] = {"used": False}

        selected_run_key = (best.get("order_name"), self._score_components(best["score"]))
        for run in runs:
            if not run["ok"]:
                debug_payload["rejections"].append(
                    {
                        "order_name": run["order_name"],
                        "reason": "no_compatible_source",
                        "error_cut": {
                            "width_mm": int(run["error_cut"].get("width_mm", 0)),
                            "height_mm": int(run["error_cut"].get("height_mm", 0)),
                        },
                    }
                )
                continue
            run_key = (run["order_name"], self._score_components(run["score"]))
            if run_key == selected_run_key:
                continue
            debug_payload["rejections"].append(
                {
                    "order_name": run["order_name"],
                    "reason": "higher_score_than_selected",
                    "score": self._score_components(run["score"]),
                }
            )

        debug_payload["selected"] = {
            "order_name": best["order_name"],
            "score": self._score_components(best["score"]),
            "candidate_plan_count": int(len(runs)),
            "rejected_plan_count": int(len(debug_payload["rejections"])),
        }

        elapsed_ms = int((time.monotonic() - self.started_at) * 1000)
        return {
            "ok": True,
            "bins": best["bins"],
            "metrics": {
                "search_nodes": self.search_nodes,
                "search_ms": elapsed_ms,
                "full_sheet_count": best["full_sheet_count"],
                "beam_expansions": self.beam_expansions,
                "local_improvement_steps": self.local_steps,
                "local_improvement_moves": self.local_moves_accepted,
                "exact_refinement_used": exact_refinement_used,
                "exact_refinement_timeout": exact_refinement_timeout,
                "exact_refinement_improved": exact_refinement_improved,
                "exact_refinement_ms": exact_refinement_ms,
                "exact_states_visited": self.exact_states_visited,
                "exact_states_pruned": self.exact_states_pruned,
                "memo_hits": self.memo_hits,
                "memo_prunes": self.memo_prunes,
                "early_infeasible_checks": self.early_infeasible_checks,
                "policy_preset": self.policy.preset,
                "policy_weights": dict(self.policy.weights),
                "max_pieces": self.max_pieces,
                "beam_width_cap": self.beam_width_cap,
                "timeout_cap_ms": self.timeout_cap_ms,
                "effective_timeout_ms": self.timeout_ms,
                "effective_beam_width": self.beam_width,
                "effective_branch_cap": self.branch_cap,
                "infeasible_reason": "",
                "candidate_plan_count": len(runs),
                "rejected_plan_count": len(debug_payload["rejections"]),
                "selected_order_name": best["order_name"],
                "score_breakdown": self._score_components(best["score"]),
                "debug_artifact": debug_payload if self.debug_enabled else {},
            },
            "order_name": best["order_name"],
        }
