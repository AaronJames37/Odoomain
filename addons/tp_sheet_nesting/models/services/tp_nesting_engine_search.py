import time

class TpNestingEngineSearchMixin:

    @staticmethod
    def _orderings(cuts):
        indexed = list(enumerate(cuts))

        area_sorted = sorted(
            indexed,
            key=lambda item: (
                -(item[1]["width_mm"] * item[1]["height_mm"]),
                -max(item[1]["width_mm"], item[1]["height_mm"]),
                -(item[1]["width_mm"] + item[1]["height_mm"]),
                item[0],
            ),
        )
        long_side_sorted = sorted(
            indexed,
            key=lambda item: (
                -max(item[1]["width_mm"], item[1]["height_mm"]),
                -(item[1]["width_mm"] * item[1]["height_mm"]),
                -(item[1]["width_mm"] + item[1]["height_mm"]),
                item[0],
            ),
        )
        perimeter_sorted = sorted(
            indexed,
            key=lambda item: (
                -(item[1]["width_mm"] + item[1]["height_mm"]),
                -(item[1]["width_mm"] * item[1]["height_mm"]),
                -max(item[1]["width_mm"], item[1]["height_mm"]),
                item[0],
            ),
        )

        area_rank = {idx: rank for rank, (idx, _cut) in enumerate(area_sorted)}
        long_rank = {idx: rank for rank, (idx, _cut) in enumerate(long_side_sorted)}
        perimeter_rank = {idx: rank for rank, (idx, _cut) in enumerate(perimeter_sorted)}
        hybrid_sorted = sorted(
            indexed,
            key=lambda item: (
                area_rank[item[0]] + long_rank[item[0]] + perimeter_rank[item[0]],
                -(item[1]["width_mm"] * item[1]["height_mm"]),
                -max(item[1]["width_mm"], item[1]["height_mm"]),
                -(item[1]["width_mm"] + item[1]["height_mm"]),
                item[0],
            ),
        )

        return [
            ("area_desc", [cut for _idx, cut in area_sorted]),
            ("long_side_desc", [cut for _idx, cut in long_side_sorted]),
            ("perimeter_desc", [cut for _idx, cut in perimeter_sorted]),
            ("hybrid_desc", [cut for _idx, cut in hybrid_sorted]),
        ]

    def _candidate_sources_for_node(self, node, all_lot_sources, sheet_format_sources):
        lot_sources = [source for source in all_lot_sources if source["id"] in node["unused_lot_ids"]]
        lot_sources = sorted(lot_sources, key=self._source_sort_key)
        fmt_sources = sorted(sheet_format_sources, key=self._source_sort_key)
        combined = lot_sources + fmt_sources
        return self._limit_candidates_diverse(combined)

    def _expand_existing_bins(self, node, cut):
        children = []
        for bin_idx, bin_state in enumerate(node["bins"]):
            placement = self._best_fit_in_bin(bin_state, cut)
            if not placement:
                continue
            child_bins = self._clone_bins(node["bins"])
            self._apply_placement(child_bins[bin_idx], placement, cut)
            child = {
                "bins": child_bins,
                "unused_lot_ids": set(node["unused_lot_ids"]),
                "path_key": node["path_key"]
                + (
                    f"E:{bin_idx}:{int(bool(placement['rotated']))}:{int(placement['x'])}:{int(placement['y'])}",
                ),
            }
            children.append(child)
            self.beam_expansions += 1
        return children

    def _expand_new_sources(self, node, cut, all_lot_sources, sheet_format_sources):
        children = []
        candidates = self._candidate_sources_for_node(node, all_lot_sources, sheet_format_sources)
        for source in candidates:
            bin_state = self._initial_bin(source)
            placement = self._best_fit_in_bin(bin_state, cut)
            if not placement:
                continue
            self._apply_placement(bin_state, placement, cut)
            child_bins = self._clone_bins(node["bins"])
            child_bins.append(bin_state)
            unused_lot_ids = set(node["unused_lot_ids"])
            if source["kind"] in ("sheet_lot", "sheet_product") and source["id"] in unused_lot_ids:
                unused_lot_ids.remove(source["id"])
            child = {
                "bins": child_bins,
                "unused_lot_ids": unused_lot_ids,
                "path_key": node["path_key"]
                + (
                    f"N:{source['kind']}:{self._source_stable_id(source)}:{int(bool(placement['rotated']))}",
                ),
            }
            children.append(child)
            self.beam_expansions += 1
        return children

    def _run_ordering_greedy(self, ordered_cuts, *, sheet_lot_sources, sheet_format_sources):
        bins = []
        unused_lots = sorted(list(sheet_lot_sources), key=self._source_sort_key)
        format_sources = sorted(list(sheet_format_sources), key=self._source_sort_key)

        for cut in ordered_cuts:
            self._check_timeout()

            best_existing = None
            best_existing_score = None
            for bin_idx, bin_state in enumerate(bins):
                placement = self._best_fit_in_bin(bin_state, cut)
                if not placement:
                    continue
                leftover = self._bin_leftover_area(bin_state)
                score = (
                    leftover,
                    self._source_area(bin_state["source"]),
                    float(bin_state["source"].get("unit_cost") or 0.0),
                    bin_idx,
                )
                if best_existing is None or score < best_existing_score:
                    best_existing = (bin_idx, placement)
                    best_existing_score = score

            if best_existing:
                bin_idx, placement = best_existing
                self._apply_placement(bins[bin_idx], placement, cut)
                continue

            candidate_sources = list(unused_lots) + list(format_sources)
            candidate_sources = self._limit_candidates_diverse(candidate_sources)

            best_new = None
            best_new_score = None
            for source in candidate_sources:
                temp_bin = self._initial_bin(source)
                placement = self._best_fit_in_bin(temp_bin, cut)
                if not placement:
                    continue
                source_priority = 0 if source["kind"] in ("sheet_lot", "sheet_product") else 1
                score = (
                    source_priority,
                    self._source_area(source),
                    float(source.get("unit_cost") or 0.0),
                    self._source_stable_id(source),
                )
                if best_new is None or score < best_new_score:
                    best_new = (source, placement)
                    best_new_score = score

            if not best_new:
                return {"ok": False, "error_cut": cut}

            source, placement = best_new
            bin_state = self._initial_bin(source)
            self._apply_placement(bin_state, placement, cut)
            bins.append(bin_state)
            if source["kind"] in ("sheet_lot", "sheet_product"):
                unused_lots = [lot for lot in unused_lots if lot["id"] != source["id"]]

        return {"ok": True, "bins": bins}

    def _run_ordering_beam(self, ordered_cuts, *, sheet_lot_sources, sheet_format_sources):
        sorted_lots = sorted(list(sheet_lot_sources), key=self._source_sort_key)
        sorted_formats = sorted(list(sheet_format_sources), key=self._source_sort_key)
        root = {
            "bins": [],
            "unused_lot_ids": {source["id"] for source in sorted_lots},
            "path_key": tuple(),
        }
        beam = [root]

        for cut in ordered_cuts:
            self._check_timeout()
            next_candidates_by_signature = {}
            pruned_children = 0
            for node in beam:
                self._check_timeout()
                children = []
                children.extend(self._expand_existing_bins(node, cut))
                children.extend(self._expand_new_sources(node, cut, sorted_lots, sorted_formats))
                if not children:
                    continue
                children.sort(key=self._score_node)
                for child in children[: self.branch_cap]:
                    score = self._score_node(child)
                    signature = self._node_signature(child)
                    existing = next_candidates_by_signature.get(signature)
                    if not existing or score < existing["score"]:
                        if existing:
                            self.memo_hits += 1
                            pruned_children += 1
                        next_candidates_by_signature[signature] = {
                            "score": score,
                            "child": child,
                        }
                    else:
                        self.memo_hits += 1
                        pruned_children += 1

            next_candidates = [entry["child"] for entry in next_candidates_by_signature.values()]
            if not next_candidates:
                return {"ok": False, "error_cut": cut}
            self.memo_prunes += pruned_children

            next_candidates.sort(key=self._score_node)
            beam = next_candidates[: self.beam_width]

        best_node = min(beam, key=self._score_node) if beam else root
        return {"ok": True, "bins": best_node["bins"]}

    def _swap_neighbor_ops(self, ordered_cuts, step_idx, cap):
        n = len(ordered_cuts)
        if n < 2 or cap <= 0:
            return []
        operations = []
        seen_pairs = set()

        for offset in range(n - 1):
            i = (step_idx + offset) % (n - 1)
            j = i + 1
            pair = (i, j)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            swapped = self._clone_cut_sequence(ordered_cuts)
            swapped[i], swapped[j] = swapped[j], swapped[i]
            operations.append(("swap", swapped))
            if len(operations) >= cap:
                return operations

        half = max(1, n // 2)
        for offset in range(n):
            i = (step_idx + offset) % n
            j = (i + half) % n
            if i == j:
                continue
            pair = (min(i, j), max(i, j))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            swapped = self._clone_cut_sequence(ordered_cuts)
            swapped[pair[0]], swapped[pair[1]] = swapped[pair[1]], swapped[pair[0]]
            operations.append(("swap", swapped))
            if len(operations) >= cap:
                break
        return operations

    def _reinsert_neighbor_ops(self, ordered_cuts, step_idx, cap):
        n = len(ordered_cuts)
        if n < 2 or cap <= 0:
            return []
        operations = []
        shifts = [1, -1, max(1, n // 3), -max(1, n // 3)]
        for offset in range(n):
            i = (step_idx + offset) % n
            for shift in shifts:
                j = (i + shift) % n
                if i == j:
                    continue
                reinsertion = self._clone_cut_sequence(ordered_cuts)
                cut = reinsertion.pop(i)
                reinsertion.insert(j, cut)
                operations.append(("reinsert", reinsertion))
                if len(operations) >= cap:
                    return operations
        return operations

    def _rotate_subset_neighbor_ops(self, ordered_cuts, step_idx, cap):
        n = len(ordered_cuts)
        if n == 0 or cap <= 0:
            return []
        operations = []
        subsets = []

        parity = step_idx % 2
        subsets.append([idx for idx in range(n) if idx % 2 == parity])

        span = max(1, n // 3)
        start = (step_idx * span) % n
        subsets.append([(start + idx) % n for idx in range(span)])

        top_count = max(1, min(4, n))
        subsets.append(list(range(top_count)))

        for subset in subsets:
            if not subset:
                continue
            rotated = self._clone_cut_sequence(ordered_cuts)
            for idx in subset:
                rotated[idx]["_rotation_first"] = not bool(rotated[idx].get("_rotation_first"))
            operations.append(("rotate_subset", rotated))
            if len(operations) >= cap:
                break
        return operations

    def _build_local_neighbors(self, ordered_cuts, step_idx):
        if len(ordered_cuts) < 2:
            return []
        per_operator_cap = max(1, self.local_neighbor_cap // 3)
        neighbors = []
        neighbors.extend(self._swap_neighbor_ops(ordered_cuts, step_idx, per_operator_cap))
        neighbors.extend(self._reinsert_neighbor_ops(ordered_cuts, step_idx, per_operator_cap))
        neighbors.extend(self._rotate_subset_neighbor_ops(ordered_cuts, step_idx, per_operator_cap))

        deduped = []
        seen_signatures = {self._ordering_signature(ordered_cuts)}
        for op_name, candidate in neighbors:
            signature = self._ordering_signature(candidate)
            if signature in seen_signatures:
                continue
            seen_signatures.add(signature)
            deduped.append((op_name, candidate))
            if len(deduped) >= self.local_neighbor_cap:
                break
        return deduped

    def _run_local_improvement(self, *, seed_run, sheet_lot_sources, sheet_format_sources):
        if not self.enable_local_improvement or self.mode != "optimal":
            return seed_run
        if len(seed_run["ordered_cuts"]) < 2:
            return seed_run

        max_steps = min(self.local_improvement_max_steps, max(1, len(seed_run["ordered_cuts"]) // 2))
        if max_steps <= 0:
            return seed_run

        current_order = self._clone_cut_sequence(seed_run["ordered_cuts"])
        current_bins = self._clone_bins(seed_run["bins"])
        current_score = self._score_final(current_bins, seed_run["order_name"])

        best_order = self._clone_cut_sequence(current_order)
        best_bins = self._clone_bins(current_bins)
        best_score = current_score

        late_window = self.late_acceptance_window
        late_history = [current_score for _ in range(late_window)]
        steps_taken = 0

        for step_idx in range(max_steps):
            self._check_timeout()
            neighbors = self._build_local_neighbors(current_order, step_idx)
            if not neighbors:
                break

            evaluated = []
            for op_name, candidate_order in neighbors:
                self._check_timeout()
                result = self._run_ordering_beam(
                    candidate_order,
                    sheet_lot_sources=sheet_lot_sources,
                    sheet_format_sources=sheet_format_sources,
                )
                if not result["ok"]:
                    continue
                bins = result["bins"]
                score = self._score_final(bins, f"{seed_run['order_name']}:o5:{op_name}:{step_idx}")
                evaluated.append(
                    (
                        score,
                        op_name,
                        self._ordering_signature(candidate_order),
                        candidate_order,
                        bins,
                    )
                )

            if not evaluated:
                break

            evaluated.sort(key=lambda item: (item[0], item[1], item[2]))
            candidate_score, _op_name, _signature, candidate_order, candidate_bins = evaluated[0]

            late_threshold = late_history[step_idx % late_window]
            accept = candidate_score <= current_score or candidate_score <= late_threshold
            if accept:
                current_order = self._clone_cut_sequence(candidate_order)
                current_bins = self._clone_bins(candidate_bins)
                current_score = candidate_score
                self.local_moves_accepted += 1

            late_history[step_idx % late_window] = current_score
            if current_score < best_score:
                best_score = current_score
                best_order = self._clone_cut_sequence(current_order)
                best_bins = self._clone_bins(current_bins)
            steps_taken += 1

        self.local_steps += steps_taken
        return {
            "ok": True,
            "order_name": seed_run["order_name"],
            "ordered_cuts": best_order,
            "bins": best_bins,
            "full_sheet_count": len(best_bins),
            "score": best_score,
            "nodes": seed_run.get("nodes", 0),
            "expansions": seed_run.get("expansions", 0),
        }

    @staticmethod
    def _normalize_cuts(cuts):
        normalized = []
        for idx, cut in enumerate(cuts):
            copy = dict(cut)
            copy["_cid"] = int(copy.get("_cid", idx))
            copy["_rotation_first"] = bool(copy.get("_rotation_first", False))
            normalized.append(copy)
        return normalized

    def _should_run_exact_refinement(self, run):
        if self.mode != "optimal" or not self.enable_exact_refinement:
            return False
        cut_count = len(run.get("ordered_cuts") or [])
        if cut_count <= self.exact_refinement_cut_threshold:
            return True

        # "Hard-bin" signal: a high-waste bin with a locally small cut set.
        for bin_state in run.get("bins", []):
            placements = bin_state.get("placements", [])
            if not placements:
                continue
            if len(placements) > self.exact_refinement_cut_threshold:
                continue
            source_area = self._source_area(bin_state["source"])
            if source_area <= 0:
                continue
            waste_ratio = self._bin_leftover_area(bin_state) / source_area
            if waste_ratio >= 0.35:
                return True
        return False

    def _exact_candidate_sources(self, unused_lot_ids, sorted_lots, sorted_formats):
        lot_sources = [source for source in sorted_lots if source["id"] in unused_lot_ids]
        lot_sources = sorted(lot_sources, key=self._source_sort_key)
        fmt_sources = sorted(sorted_formats, key=self._source_sort_key)
        candidates = lot_sources + fmt_sources
        return self._limit_candidates_diverse(candidates)

    def _exact_children_for_cut(self, state, cut, sorted_lots, sorted_formats):
        children = []

        for bin_idx, bin_state in enumerate(state["bins"]):
            placement = self._best_fit_in_bin(bin_state, cut)
            if not placement:
                continue
            child_bins = self._clone_bins(state["bins"])
            self._apply_placement(child_bins[bin_idx], placement, cut)
            children.append(
                {
                    "bins": child_bins,
                    "unused_lot_ids": set(state["unused_lot_ids"]),
                    "path_key": state["path_key"]
                    + (
                        f"XE:{bin_idx}:{int(bool(placement['rotated']))}:{int(placement['x'])}:{int(placement['y'])}",
                    ),
                }
            )

        source_candidates = self._exact_candidate_sources(state["unused_lot_ids"], sorted_lots, sorted_formats)
        for source in source_candidates:
            bin_state = self._initial_bin(source)
            placement = self._best_fit_in_bin(bin_state, cut)
            if not placement:
                continue
            self._apply_placement(bin_state, placement, cut)
            child_bins = self._clone_bins(state["bins"])
            child_bins.append(bin_state)
            unused_lot_ids = set(state["unused_lot_ids"])
            if source["kind"] in ("sheet_lot", "sheet_product") and source["id"] in unused_lot_ids:
                unused_lot_ids.remove(source["id"])
            children.append(
                {
                    "bins": child_bins,
                    "unused_lot_ids": unused_lot_ids,
                    "path_key": state["path_key"]
                    + (
                        f"XN:{source['kind']}:{self._source_stable_id(source)}:{int(bool(placement['rotated']))}",
                    ),
                }
            )
        children.sort(key=self._score_node)
        return children

    def _run_exact_refinement(self, *, seed_run, sheet_lot_sources, sheet_format_sources):
        exact_started_at = time.monotonic()
        sorted_lots = sorted(list(sheet_lot_sources), key=self._source_sort_key)
        sorted_formats = sorted(list(sheet_format_sources), key=self._source_sort_key)

        initial_state = {
            "bins": [],
            "unused_lot_ids": {source["id"] for source in sorted_lots},
            "path_key": tuple(),
        }
        initial_cuts = self._clone_cut_sequence(seed_run["ordered_cuts"])
        best_bins = self._clone_bins(seed_run["bins"])
        best_score = self._score_final(best_bins, seed_run["order_name"])
        best_order = self._clone_cut_sequence(seed_run["ordered_cuts"])
        seen_states = {}

        def _next_cut_indexes(remaining_cuts):
            indexed = list(enumerate(remaining_cuts))
            indexed.sort(
                key=lambda item: (
                    -(item[1]["width_mm"] * item[1]["height_mm"]),
                    -max(item[1]["width_mm"], item[1]["height_mm"]),
                    -(item[1]["width_mm"] + item[1]["height_mm"]),
                    int(item[1].get("_cid", item[0])),
                )
            )
            return [idx for idx, _cut in indexed]

        def _search(state, remaining_cuts, ordered_prefix):
            nonlocal best_bins, best_score, best_order
            self._check_timeout()
            self._check_exact_timeout(exact_started_at)
            self.exact_states_visited += 1

            # Memoization: skip equivalent states already reached with an equal or better score.
            state_signature = (
                tuple(sorted(int(cut.get("_cid", idx)) for idx, cut in enumerate(remaining_cuts))),
                self._node_signature(state),
            )
            state_score = self._score_node(state)
            seen_score = seen_states.get(state_signature)
            if seen_score is not None and state_score >= seen_score:
                self.memo_hits += 1
                self.memo_prunes += 1
                return
            seen_states[state_signature] = state_score

            # Branch-and-bound on strongest primary objective: sheet count.
            if len(state["bins"]) > best_score[1]:
                self.exact_states_pruned += 1
                return

            if not remaining_cuts:
                leaf_score = self._score_final(state["bins"], "exact_refinement")
                if leaf_score < best_score:
                    best_bins = self._clone_bins(state["bins"])
                    best_score = leaf_score
                    best_order = self._clone_cut_sequence(ordered_prefix)
                return

            cut_indexes = _next_cut_indexes(remaining_cuts)
            max_cut_branches = min(len(cut_indexes), self.exact_refinement_cut_threshold)
            for cut_idx in cut_indexes[:max_cut_branches]:
                cut = dict(remaining_cuts[cut_idx])
                next_remaining = self._clone_cut_sequence(
                    remaining_cuts[:cut_idx] + remaining_cuts[cut_idx + 1 :]
                )
                next_prefix = self._clone_cut_sequence(ordered_prefix) + [cut]
                children = self._exact_children_for_cut(state, cut, sorted_lots, sorted_formats)
                if not children:
                    continue
                for child in children[: self.branch_cap]:
                    _search(child, next_remaining, next_prefix)

        _search(initial_state, initial_cuts, [])
        exact_elapsed_ms = int((time.monotonic() - exact_started_at) * 1000)
        return {
            "ok": True,
            "order_name": seed_run["order_name"],
            "ordered_cuts": best_order,
            "bins": best_bins,
            "full_sheet_count": len(best_bins),
            "score": best_score,
            "nodes": seed_run.get("nodes", 0),
            "expansions": seed_run.get("expansions", 0),
            "exact_refinement_ms": exact_elapsed_ms,
        }

