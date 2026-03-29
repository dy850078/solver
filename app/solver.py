"""
VM Placement Solver — with Cross-Scheduling Constraints

Hard constraints:
  1. Each VM assigned to exactly one BM (or <= 1 if partial placement)
  2. BM resource capacity not exceeded (cpu, mem, disk, gpu)
  3. AG-based anti-affinity rules (within this scheduling run)
  4. Candidate lists from Go scheduler step 3
  5. BM VM count limits (current_vm_count + new_vms <= max_vm_count)
  6. Cross-cluster hard anti-affinity (topology-based)

Soft constraints (objective function):
  7. Cross-cluster soft anti-affinity (penalty for violations)
  8. Cross-cluster soft affinity (reward for co-location)

Two-phase solving:
  When allow_partial_placement=True AND soft rules exist:
    Phase 1: maximize placed VM count → get N
    Phase 2: fix placed count == N, maximize soft rule score
"""

from __future__ import annotations

import math
import time
import logging
from collections import defaultdict
from typing import Any

from ortools.sat.python import cp_model

from .models import (
    PlacementRequest,
    PlacementResult,
    PlacementAssignment,
    AntiAffinityRule,
    TopologyRule,
    ExistingVM,
    Baremetal,
    VM,
    Resources,
    Topology,
    TOPOLOGY_SCOPES,
)

logger = logging.getLogger(__name__)

# The resource fields we check for capacity constraints.
RESOURCE_FIELDS = ["cpu_cores", "memory_mib", "storage_gb", "gpu_count"]


def get_eligible_baremetals(
    vm: VM,
    bm_map: dict[str, Baremetal],
    baremetals: list[Baremetal],
) -> list[str]:
    """
    Which baremetals can this VM possibly go on?

    If the Go scheduler provided a candidate list (from step 3 filtering),
    we only consider those. Otherwise, we consider any BM with enough
    available capacity.

    This is a module-level function so both the solver and diagnostics
    can share the same eligibility logic.
    """
    if vm.candidate_baremetals:
        return [
            bm_id for bm_id in vm.candidate_baremetals
            if bm_id in bm_map
            and vm.demand.fits_in(bm_map[bm_id].available_capacity)
        ]
    else:
        return [
            bm.id for bm in baremetals
            if vm.demand.fits_in(bm.available_capacity)
        ]


def _get_topology_value(topo: Topology, scope: str) -> str:
    """Extract the topology value at a given scope level."""
    return getattr(topo, scope, "")


def _get_topology_zone(topo: Topology, scope: str) -> str:
    """
    Build a zone key that includes all levels from the scope upward.
    e.g. scope="datacenter" → "site-a/p1/dc-1"
    This ensures that same(lower) implies same(higher).
    """
    idx = TOPOLOGY_SCOPES.index(scope)
    # Include all scopes from the current level up to coarsest
    parts = []
    for s in reversed(TOPOLOGY_SCOPES[idx:]):
        parts.append(getattr(topo, s, ""))
    return "/".join(parts)


# -----------------------------------------------------------------------
# Validation: conflict detection and redundancy filtering
# -----------------------------------------------------------------------

def validate_topology_rules(
    rules: list[TopologyRule],
) -> tuple[list[TopologyRule], list[dict[str, Any]]]:
    """
    Phase 1 of the solver pipeline:
      a. Detect conflicts: same cluster pair has affinity and anti-affinity
         at incompatible scope levels → MODEL_INVALID
      b. Filter redundancy: same cluster pair + same direction with multiple
         scopes → keep finest-grained, warn about coarser ones
      c. Downgrade affinity+hard to soft with warning

    Returns (validated_rules, diagnostics_warnings).
    Raises ValueError if a conflict is detected.
    """
    warnings: list[dict[str, Any]] = []
    processed: list[TopologyRule] = []

    # Downgrade affinity+hard → soft
    for rule in rules:
        if rule.type == "affinity" and rule.enforcement == "hard":
            warnings.append({
                "type": "enforcement_downgraded",
                "rule_id": rule.rule_id,
                "reason": "affinity rules cannot be hard; downgraded to soft",
            })
            rule = rule.model_copy(update={"enforcement": "soft"})
        processed.append(rule)

    # Group by (sorted cluster pair, direction) for conflict/redundancy checks
    # cluster_ids may have >2 entries; use frozenset as key
    pair_dir: dict[tuple[frozenset[str], str], list[TopologyRule]] = defaultdict(list)
    for rule in processed:
        key = (frozenset(rule.cluster_ids), rule.type)
        pair_dir[key].append(rule)

    # Check for conflicts: affinity vs anti-affinity at incompatible scopes
    cluster_sets = {frozenset(r.cluster_ids) for r in processed}
    for cs in cluster_sets:
        affinity_rules = pair_dir.get((cs, "affinity"), [])
        anti_affinity_rules = pair_dir.get((cs, "anti_affinity"), [])
        if not affinity_rules or not anti_affinity_rules:
            continue

        for aff in affinity_rules:
            for anti in anti_affinity_rules:
                aff_idx = TOPOLOGY_SCOPES.index(aff.scope)
                anti_idx = TOPOLOGY_SCOPES.index(anti.scope)
                # Conflict: affinity scope <= anti-affinity scope in hierarchy
                # (affinity wants same at aff.scope, anti wants different at anti.scope)
                # If aff scope is same or finer than anti scope → same(aff) implies
                # same(anti) which contradicts different(anti)
                if aff_idx <= anti_idx:
                    raise ValueError(
                        f"Topology rule conflict: affinity rule '{aff.rule_id}' "
                        f"at scope '{aff.scope}' conflicts with anti-affinity rule "
                        f"'{anti.rule_id}' at scope '{anti.scope}' for clusters "
                        f"{sorted(cs)}. Affinity at scope <= anti-affinity scope "
                        f"is contradictory."
                    )

    # Remove redundant rules: same pair + same direction, keep finest scope
    final_rules: list[TopologyRule] = []
    for (cs, direction), group in pair_dir.items():
        if len(group) <= 1:
            final_rules.extend(group)
            continue

        # Sort by scope hierarchy index (lowest = finest)
        group.sort(key=lambda r: TOPOLOGY_SCOPES.index(r.scope))
        finest = group[0]
        final_rules.append(finest)
        for redundant in group[1:]:
            warnings.append({
                "type": "redundant_rule_filtered",
                "rule_id": redundant.rule_id,
                "reason": (
                    f"Redundant: '{redundant.rule_id}' at scope '{redundant.scope}' "
                    f"is coarser than '{finest.rule_id}' at scope '{finest.scope}' "
                    f"for the same cluster pair and direction; filtered out"
                ),
            })

    return final_rules, warnings


class VMPlacementSolver:

    def __init__(self, request: PlacementRequest):
        self.request = request
        self.config = request.config
        self.diagnostics: dict[str, Any] = {}

        # Lookup maps
        self.vm_map: dict[str, VM] = {vm.id: vm for vm in request.vms}
        self.bm_map: dict[str, Baremetal] = {bm.id: bm for bm in request.baremetals}

        # Validate: no duplicate baremetals allowed.
        # Deduplication is the scheduler's responsibility — solver only detects
        # and rejects invalid input so the scheduler can fix the bug upstream.
        self._input_errors: list[str] = []
        seen_bm_ids: set[str] = set()
        for bm in request.baremetals:
            if bm.id in seen_bm_ids:
                self._input_errors.append(f"duplicate BM '{bm.id}' in baremetals list")
            else:
                seen_bm_ids.add(bm.id)

        for vm in request.vms:
            if vm.candidate_baremetals:
                seen_candidates: set[str] = set()
                for cand in vm.candidate_baremetals:
                    if cand in seen_candidates:
                        self._input_errors.append(
                            f"duplicate candidate BM '{cand}' in VM '{vm.id}'"
                        )
                    else:
                        seen_candidates.add(cand)

        # Group baremetals by AG (needed for anti-affinity constraints)
        self.ag_to_bms: dict[str, list[str]] = defaultdict(list)
        for bm in self.request.baremetals:
            self.ag_to_bms[bm.topology.ag].append(bm.id)

        # Resolve AG-based anti-affinity rules (existing logic)
        self.effective_rules = self._resolve_anti_affinity_rules()

        # Build existing VM topology index: cluster_id → set of zone keys per scope
        self.existing_cluster_zones: dict[str, dict[str, set[str]]] = defaultdict(
            lambda: defaultdict(set)
        )
        for evm in request.existing_vms:
            for scope in TOPOLOGY_SCOPES:
                zone = _get_topology_zone(evm.topology, scope)
                self.existing_cluster_zones[evm.cluster_id][scope].add(zone)

        # CP-SAT model and variables (created fresh per solve phase)
        self.model = cp_model.CpModel()
        self.assign: dict[tuple[str, str], cp_model.IntVar] = {}

        # Objective helper: bm_used[bm_id] = 1 if any VM is placed on that BM
        self.bm_used: dict[str, cp_model.IntVar] = {}

    # ------------------------------------------------------------------
    # Eligibility pre-filter
    # ------------------------------------------------------------------

    def _get_eligible_baremetals(self, vm: VM) -> list[str]:
        """Delegate to module-level function for reuse by diagnostics."""
        return get_eligible_baremetals(vm, self.bm_map, self.request.baremetals)

    # ------------------------------------------------------------------
    # AG-based anti-affinity (existing logic, unchanged)
    # ------------------------------------------------------------------

    def _resolve_anti_affinity_rules(self) -> list[AntiAffinityRule]:
        rules = list(self.request.anti_affinity_rules)

        if not self.config.auto_generate_anti_affinity:
            return rules

        num_ags = len(self.ag_to_bms)

        covered: set[str] = set()
        for rule in rules:
            covered.update(rule.vm_ids)

        groups: dict[tuple[str, str], list[str]] = defaultdict(list)
        for vm in self.request.vms:
            if vm.id not in covered and vm.ip_type:
                groups[(vm.ip_type, vm.node_role.value)].append(vm.id)

        for (ip_type, role), vm_ids in groups.items():
            if len(vm_ids) >= 2 and num_ags > 0:
                max_per_ag = math.ceil(len(vm_ids) / num_ags)
                rules.append(AntiAffinityRule(
                    group_id=f"auto/{ip_type}/{role}",
                    vm_ids=vm_ids,
                    max_per_ag=max_per_ag,
                ))
                logger.info(
                    "Auto anti-affinity: %s/%s (%d VMs / %d AGs → max_per_ag=%d)",
                    ip_type, role, len(vm_ids), num_ags, max_per_ag,
                )

        return rules

    # ------------------------------------------------------------------
    # Build CP-SAT model
    # ------------------------------------------------------------------

    def _build_variables(self):
        for vm in self.request.vms:
            for bm_id in self._get_eligible_baremetals(vm):
                self.assign[(vm.id, bm_id)] = self.model.new_bool_var(
                    f"assign_{vm.id}__{bm_id}"
                )

    def _add_one_bm_per_vm_constraint(self):
        for vm in self.request.vms:
            vm_vars = [
                self.assign[(vm.id, bm_id)]
                for bm_id in self._get_eligible_baremetals(vm)
                if (vm.id, bm_id) in self.assign
            ]

            if not vm_vars:
                if self.config.allow_partial_placement:
                    continue
                else:
                    # No eligible BM → impossible to solve
                    logger.error("VM %s has no eligible BMs → infeasible", vm.id)
                    self.model.add(0 == 1)  # force infeasibility
                    return

            if self.config.allow_partial_placement:
                self.model.add(sum(vm_vars) <= 1)
            else:
                self.model.add(sum(vm_vars) == 1)

    def _add_capacity_constraints(self):
        for bm in self.request.baremetals:
            avail = bm.available_capacity
            assigned_vars = [
                (vm_id, self.assign[(vm_id, bm.id)])
                for vm_id in self.vm_map
                if (vm_id, bm.id) in self.assign
            ]
            if not assigned_vars:
                continue
            for field in RESOURCE_FIELDS:
                capacity = getattr(avail, field)
                usage = sum(
                    getattr(self.vm_map[vm_id].demand, field) * var
                    for vm_id, var in assigned_vars
                )

                # The constraint: total usage <= capacity
                self.model.add(usage <= capacity)

    def _add_ag_anti_affinity_constraints(self):
        for rule in self.effective_rules:
            for ag, ag_bm_ids in self.ag_to_bms.items():
                vars_in_ag = [
                    self.assign[(vm_id, bm_id)]
                    for vm_id in rule.vm_ids
                    for bm_id in ag_bm_ids
                    if (vm_id, bm_id) in self.assign
                ]
                if vars_in_ag:
                    self.model.add(sum(vars_in_ag) <= rule.max_per_ag)

    # ------------------------------------------------------------------
    # Step C (cont.): Objective function helpers
    # ------------------------------------------------------------------

    def _build_bm_used_vars(self):
        """
        Create bm_used[bm_id] indicator: 1 if any VM is placed on this BM.

        bm_used[bm] = max(assign[vm_1, bm], assign[vm_2, bm], ...)
        """
        for bm in self.request.baremetals:
            bm_used = self.model.new_bool_var(f"bm_used_{bm.id}")

            vm_vars_on_bm = [
                self.assign[(vm_id, bm.id)]
                for vm_id in self.vm_map
                if (vm_id, bm.id) in self.assign
            ]

            if vm_vars_on_bm:
                self.model.add_max_equality(bm_used, vm_vars_on_bm)
            else:
                self.model.add(bm_used == 0)

            self.bm_used[bm.id] = bm_used

    def _compute_headroom_penalties(self) -> list[cp_model.IntVar]:
        """
        Compute per-BM headroom penalty.

        For each BM, for each resource dimension:
        1. Compute utilization % after placement
        2. Penalize the amount exceeding headroom_upper_bound_pct
        3. Take the max across dimensions (worst-case determines penalty)

        Returns a list of penalty variables, one per BM.
        """
        penalties = []
        for bm in self.request.baremetals:
            dim_overs = []
            for field in RESOURCE_FIELDS:
                total_d = getattr(bm.total_capacity, field)
                if total_d == 0:
                    continue  # skip zero-total dimensions (e.g. gpu_count=0)

                used_d = getattr(bm.used_capacity, field)

                assigned_vars = [
                    (vm_id, self.assign[(vm_id, bm.id)])
                    for vm_id in self.vm_map
                    if (vm_id, bm.id) in self.assign
                ]
                if not assigned_vars:
                    continue

                # New VM usage on this BM
                new_usage = sum(
                    getattr(self.vm_map[vm_id].demand, field) * var
                    for vm_id, var in assigned_vars
                )

                # Step A: post-placement usage * 100 (integer arithmetic, no floats)
                # Upper bound uses max(total, used + all candidate demand) to avoid
                # false INFEASIBLE when used_d is already high.
                max_new_demand = sum(
                    getattr(self.vm_map[vm_id].demand, field)
                    for vm_id, _ in assigned_vars
                )
                upper_after = max(total_d, used_d + max_new_demand) * 100
                after_times_100 = self.model.new_int_var(
                    0, upper_after, f"a100_{bm.id}_{field}"
                )
                self.model.add(after_times_100 == (used_d + new_usage) * 100)

                # Step B: integer utilization % (can exceed 100 if BM is near-full)
                max_util = upper_after // total_d if total_d > 0 else 0
                util_pct = self.model.new_int_var(0, max_util, f"util_{bm.id}_{field}")
                self.model.add_division_equality(util_pct, after_times_100, total_d)

                # Step C: amount exceeding the safe upper bound (may be negative)
                raw = self.model.new_int_var(-max_util, max_util, f"raw_{bm.id}_{field}")
                self.model.add(raw == util_pct - self.config.headroom_upper_bound_pct)

                # Step D: ReLU — clamp negative values to 0
                over = self.model.new_int_var(0, max_util, f"over_{bm.id}_{field}")
                self.model.add_max_equality(over, [self.model.new_constant(0), raw])
                dim_overs.append(over)

            if dim_overs:
                # Step E: max across dimensions
                bm_penalty = self.model.new_int_var(0, 1000, f"hp_{bm.id}")
                self.model.add_max_equality(bm_penalty, dim_overs)
                penalties.append(bm_penalty)

        return penalties

    def _compute_slot_score_bonus(self) -> list[cp_model.IntVar]:
        """
        Compute per-BM slot score (how many t-shirt size VMs can still fit).

        For each BM:
        1. Compute remaining capacity per dimension after placement
        2. For each t-shirt size, floor-divide remaining by demand per dimension
        3. Take min across dimensions = actual fit count for that t-shirt
        4. Sum across all t-shirt sizes = BM slot score

        Higher score = more usable remaining space = rewarded (negated in objective).
        """
        tshirt_sizes = self.config.slot_tshirt_sizes
        if not tshirt_sizes:
            return []

        scores = []
        for bm in self.request.baremetals:
            assigned_vars = [
                (vm_id, self.assign[(vm_id, bm.id)])
                for vm_id in self.vm_map
                if (vm_id, bm.id) in self.assign
            ]
            if not assigned_vars:
                continue

            tshirt_slots = []
            for t_idx, tshirt in enumerate(tshirt_sizes):
                dim_slots = []
                for field in RESOURCE_FIELDS:
                    tshirt_d = getattr(tshirt, field)
                    if tshirt_d == 0:
                        continue  # no demand on this dimension, not a bottleneck

                    total_d = getattr(bm.total_capacity, field)
                    used_d = getattr(bm.used_capacity, field)

                    # New VM usage on this BM
                    new_usage = sum(
                        getattr(self.vm_map[vm_id].demand, field) * var
                        for vm_id, var in assigned_vars
                    )

                    # Remaining = total - used - new_placement
                    # Lower bound can be negative to avoid false INFEASIBLE with
                    # many candidate VMs (capacity constraint ensures actual >= 0)
                    max_new_d = sum(
                        getattr(self.vm_map[vm_id].demand, field)
                        for vm_id, _ in assigned_vars
                    )
                    remaining = self.model.new_int_var(
                        total_d - used_d - max_new_d, total_d,
                        f"rem_{bm.id}_{field}_t{t_idx}",
                    )
                    self.model.add(remaining == total_d - used_d - new_usage)

                    # How many of this t-shirt size fit on this dimension
                    # (may be negative in the model; capacity constraint ensures non-negative at solution)
                    min_slots = (total_d - used_d - max_new_d) // tshirt_d if tshirt_d > 0 else 0
                    max_slots = total_d // tshirt_d if tshirt_d > 0 else 0
                    slots_d = self.model.new_int_var(
                        min(min_slots, 0), max_slots,
                        f"slotd_{bm.id}_{field}_t{t_idx}",
                    )
                    self.model.add_division_equality(slots_d, remaining, tshirt_d)
                    dim_slots.append(slots_d)

                if dim_slots:
                    # Min across dimensions = actual fit count (bottleneck dimension decides)
                    max_possible = min(
                        getattr(bm.total_capacity, f) // getattr(tshirt, f)
                        for f in RESOURCE_FIELDS
                        if getattr(tshirt, f) > 0
                    )
                    # min may be negative (capacity constraint ensures >= 0 at solution)
                    slots_for_tshirt = self.model.new_int_var(
                        -max_possible, max_possible, f"slot_{bm.id}_t{t_idx}"
                    )
                    self.model.add_min_equality(slots_for_tshirt, dim_slots)
                    tshirt_slots.append(slots_for_tshirt)

            if tshirt_slots:
                # Sum fit counts across all t-shirt sizes
                max_total = sum(
                    min(
                        getattr(bm.total_capacity, f) // getattr(ts, f)
                        for f in RESOURCE_FIELDS
                        if getattr(ts, f) > 0
                    )
                    for ts in tshirt_sizes
                    if any(getattr(ts, f) > 0 for f in RESOURCE_FIELDS)
                )
                bm_score = self.model.new_int_var(
                    -max_total, max_total, f"sscore_{bm.id}"
                )
                self.model.add(bm_score == sum(tshirt_slots))

                # Only count slot score for used BMs — otherwise solver would
                # prefer placing VMs on small BMs to keep large BMs' scores high
                effective = self.model.new_int_var(
                    -max_total, max_total, f"eff_sscore_{bm.id}"
                )
                self.model.add_multiplication_equality(
                    effective, [self.bm_used[bm.id], bm_score]
                )
                scores.append(effective)

        return scores

    def _ensure_bm_used_vars(self):
        """Build bm_used vars if not yet built. Multiple objective terms may need them."""
        if not self.bm_used:
            self._build_bm_used_vars()

    def _add_objective(self):
        """
        Combine all objective terms and set Minimize.

        Priority (high to low):
        1. Place as many VMs as possible (partial placement mode)
        2. Use as few BMs as possible (consolidation)
        3. Keep utilization below safe upper bound (headroom)
        4. Maximize usability of remaining capacity (slot score)
        """
        terms = []

        if self.config.allow_partial_placement:
            total_placed = sum(self.assign.values())
            terms.append(-1_000_000 * total_placed)

        if self.config.w_consolidation > 0:
            self._ensure_bm_used_vars()
            terms.append(self.config.w_consolidation * sum(self.bm_used.values()))

        if self.config.w_headroom > 0:
            penalties = self._compute_headroom_penalties()
            if penalties:
                terms.append(self.config.w_headroom * sum(penalties))

        if self.config.w_slot_score > 0:
            self._ensure_bm_used_vars()
            slot_scores = self._compute_slot_score_bonus()
            if slot_scores:
                # Negate: higher slot score is better (negative = reward in Minimize)
                terms.append(-self.config.w_slot_score * sum(slot_scores))

        if terms:
            self.model.minimize(sum(terms))

    # ------------------------------------------------------------------
    # BM VM count limit
    # ------------------------------------------------------------------

    def _add_vm_count_constraints(self):
        """current_vm_count + newly_assigned <= max_vm_count"""
        for bm in self.request.baremetals:
            if bm.max_vm_count is None:
                continue
            assigned_vars = [
                self.assign[(vm_id, bm.id)]
                for vm_id in self.vm_map
                if (vm_id, bm.id) in self.assign
            ]
            if not assigned_vars:
                continue
            new_count = sum(assigned_vars)
            self.model.Add(bm.current_vm_count + new_count <= bm.max_vm_count)

    # ------------------------------------------------------------------
    # Cross-cluster topology constraints
    # ------------------------------------------------------------------

    def _add_hard_topology_constraints(self, rules: list[TopologyRule]):
        """
        Hard anti-affinity: for each rule, if an existing VM from a related
        cluster occupies a topology zone, no new VM from this scheduling run
        (whose cluster_id is in the rule) can be placed in a BM in that zone.
        """
        current_cluster_ids = {vm.cluster_id for vm in self.request.vms}

        for rule in rules:
            if rule.type != "anti_affinity" or rule.enforcement != "hard":
                continue

            # Which clusters in this rule are "other" (have existing VMs)?
            other_cluster_ids = set(rule.cluster_ids) - current_cluster_ids
            # Collect zones occupied by other clusters at this rule's scope
            occupied_zones: set[str] = set()
            for cid in other_cluster_ids:
                occupied_zones.update(
                    self.existing_cluster_zones.get(cid, {}).get(rule.scope, set())
                )

            if not occupied_zones:
                continue

            # For each new VM whose cluster_id is in this rule, forbid BMs in occupied zones
            for vm in self.request.vms:
                if vm.cluster_id not in rule.cluster_ids:
                    continue
                for bm in self.request.baremetals:
                    if (vm.id, bm.id) not in self.assign:
                        continue
                    bm_zone = _get_topology_zone(bm.topology, rule.scope)
                    if bm_zone in occupied_zones:
                        self.model.Add(self.assign[(vm.id, bm.id)] == 0)

    def _build_soft_objective_terms(
        self, rules: list[TopologyRule]
    ) -> list[tuple[cp_model.IntVar, int]]:
        """
        Build objective terms for soft rules.
        Returns list of (bool_var, weight) where weight is positive for
        reward (affinity) and negative for penalty (anti-affinity).
        """
        terms: list[tuple[cp_model.IntVar, int]] = []
        current_cluster_ids = {vm.cluster_id for vm in self.request.vms}

        for rule in rules:
            if rule.enforcement != "soft":
                continue

            other_cluster_ids = set(rule.cluster_ids) - current_cluster_ids
            occupied_zones: set[str] = set()
            for cid in other_cluster_ids:
                occupied_zones.update(
                    self.existing_cluster_zones.get(cid, {}).get(rule.scope, set())
                )

            if rule.type == "affinity":
                if not occupied_zones:
                    # No existing VMs → no effect, already warned in diagnostics
                    continue
                # For each new VM in rule's clusters: +weight if placed in occupied zone
                for vm in self.request.vms:
                    if vm.cluster_id not in rule.cluster_ids:
                        continue
                    colocated_vars = []
                    for bm in self.request.baremetals:
                        if (vm.id, bm.id) not in self.assign:
                            continue
                        bm_zone = _get_topology_zone(bm.topology, rule.scope)
                        if bm_zone in occupied_zones:
                            colocated_vars.append(self.assign[(vm.id, bm.id)])
                    if colocated_vars:
                        # score(vm, rule) = 1 if any of these is set
                        # Since exactly one BM is chosen, sum == 0 or 1
                        indicator = self.model.NewBoolVar(
                            f"aff_{rule.rule_id}_{vm.id}"
                        )
                        self.model.Add(sum(colocated_vars) >= 1).OnlyEnforceIf(indicator)
                        self.model.Add(sum(colocated_vars) == 0).OnlyEnforceIf(indicator.Not())
                        terms.append((indicator, rule.weight))

            elif rule.type == "anti_affinity":
                if not occupied_zones:
                    continue
                # Penalty for each VM placed in an occupied zone
                for vm in self.request.vms:
                    if vm.cluster_id not in rule.cluster_ids:
                        continue
                    violation_vars = []
                    for bm in self.request.baremetals:
                        if (vm.id, bm.id) not in self.assign:
                            continue
                        bm_zone = _get_topology_zone(bm.topology, rule.scope)
                        if bm_zone in occupied_zones:
                            violation_vars.append(self.assign[(vm.id, bm.id)])
                    if violation_vars:
                        indicator = self.model.NewBoolVar(
                            f"soft_anti_{rule.rule_id}_{vm.id}"
                        )
                        self.model.Add(sum(violation_vars) >= 1).OnlyEnforceIf(indicator)
                        self.model.Add(sum(violation_vars) == 0).OnlyEnforceIf(indicator.Not())
                        # Negative weight = penalty
                        terms.append((indicator, -rule.weight))

        return terms

    # ------------------------------------------------------------------
    # Solve
    # ------------------------------------------------------------------

    def solve(self) -> PlacementResult:
        start = time.time()

        # Reject requests with duplicate BMs — scheduler must fix upstream.
        if self._input_errors:
            for err in self._input_errors:
                logger.error("Input validation failed: %s", err)
            return PlacementResult(
                success=False,
                solver_status="INPUT_ERROR: duplicate baremetals detected — "
                              "scheduler must deduplicate before calling solver",
                solve_time_seconds=time.time() - start,
                unplaced_vms=[vm.id for vm in self.request.vms],
                diagnostics={"input_errors": self._input_errors},
            )

        try:
            # Phase 1: Validate topology rules
            validated_rules, validation_warnings = validate_topology_rules(
                list(self.request.topology_rules)
            )
            self.diagnostics["warnings"] = validation_warnings

            # Check for affinity rules with no existing VMs
            current_cluster_ids = {vm.cluster_id for vm in self.request.vms}
            for rule in validated_rules:
                if rule.type == "affinity":
                    other_cids = set(rule.cluster_ids) - current_cluster_ids
                    has_existing = any(
                        self.existing_cluster_zones.get(cid, {}).get(rule.scope)
                        for cid in other_cids
                    )
                    if not has_existing:
                        self.diagnostics.setdefault("warnings", []).append({
                            "type": "affinity_rule_no_effect",
                            "rule_id": rule.rule_id,
                            "reason": (
                                f"Target clusters {sorted(other_cids)} have no "
                                f"existing VMs; affinity rule has no effect"
                            ),
                        })

            # Determine if we have soft rules
            soft_rules = [r for r in validated_rules if r.enforcement == "soft"]
            has_soft_rules = len(soft_rules) > 0

            # Build model
            self._build_variables()
            self._add_one_bm_per_vm_constraint()
            self._add_capacity_constraints()
            self._add_ag_anti_affinity_constraints()
            self._add_vm_count_constraints()
            self._add_hard_topology_constraints(validated_rules)

            # Objective: consolidation + headroom (+ partial placement priority)
            self._add_objective()

            # Solve
            solver = cp_model.CpSolver()
            solver.parameters.max_time_in_seconds = self.config.max_solve_time_seconds
            solver.parameters.num_workers = self.config.num_workers

            logger.info(
                "Solving: %d VMs, %d BMs, %d variables, %d rules, %d AGs",
                len(self.request.vms), len(self.request.baremetals),
                len(self.assign), len(self.effective_rules), len(self.ag_to_bms),
            )

            status = solver.solve(self.model)
            status_name = self._status_name(status)
            logger.info("Status: %s", status_name)

            if needs_two_phase:
                return self._solve_two_phase(soft_terms, start)
            else:
                diagnostics = self._build_failure_diagnostics()
                logger.warning("Solver failed with %s, diagnostics: %s", status_name, diagnostics)
                return PlacementResult(
                    success=False,
                    solver_status=status_name,
                    solve_time_seconds=time.time() - start,
                    unplaced_vms=[vm.id for vm in self.request.vms],
                    diagnostics=diagnostics,
                )

        except ValueError as e:
            # Validation error (e.g. topology conflict)
            return PlacementResult(
                success=False,
                solver_status="MODEL_INVALID",
                solve_time_seconds=time.time() - start,
                unplaced_vms=[vm.id for vm in self.request.vms],
                diagnostics={"error": str(e), **self.diagnostics},
            )
        except Exception as e:
            logger.exception("Solver failed")
            return PlacementResult(
                success=False,
                solver_status=f"ERROR: {e}",
                solve_time_seconds=time.time() - start,
                unplaced_vms=[vm.id for vm in self.request.vms],
                diagnostics=self.diagnostics,
            )

    def _solve_single(
        self,
        soft_terms: list[tuple[cp_model.IntVar, int]],
        start: float,
    ) -> PlacementResult:
        """Single-phase solve: hard constraints + optional soft objective."""
        # Build objective
        objective_parts = []

        if self.config.allow_partial_placement:
            # Maximize placed VMs (weight much higher than soft terms)
            total_placed = sum(self.assign[key] for key in self.assign)
            # Use a large multiplier so placement count always dominates
            placement_weight = 1000 * (max(abs(w) for _, w in soft_terms) + 1) if soft_terms else 1
            objective_parts.append(total_placed * placement_weight)

        for var, weight in soft_terms:
            objective_parts.append(var * weight)

        if objective_parts:
            self.model.Maximize(sum(objective_parts))

        return self._run_solver(self.config.max_solve_time_seconds, start)

    def _solve_two_phase(
        self,
        soft_terms: list[tuple[cp_model.IntVar, int]],
        start: float,
    ) -> PlacementResult:
        """
        Two-phase solve for partial placement + soft rules:
          Phase 1: maximize placed VM count → N
          Phase 2: fix count == N, maximize soft rule score
        """
        phase_timeout = self.config.max_solve_time_seconds / 2

        # Phase 1: maximize placement count
        total_placed = sum(self.assign[key] for key in self.assign)
        self.model.Maximize(total_placed)

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = phase_timeout
        solver.parameters.num_workers = self.config.num_workers

        logger.info("Two-phase solve: Phase 1 — maximize placement count")
        status = solver.Solve(self.model)

        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            status_name = self._status_name(status)
            return PlacementResult(
                success=False,
                solver_status=status_name,
                solve_time_seconds=time.time() - start,
                unplaced_vms=[vm.id for vm in self.request.vms],
                diagnostics=self.diagnostics,
            )

        # Get optimal placement count
        optimal_count = int(solver.ObjectiveValue())
        logger.info(f"Phase 1 result: {optimal_count} VMs placed")

        # Phase 2: fix count, maximize soft score
        # Add constraint: total_placed == optimal_count
        self.model.Add(total_placed == optimal_count)

        if soft_terms:
            soft_obj = sum(var * weight for var, weight in soft_terms)
            self.model.Maximize(soft_obj)
        else:
            # Clear the objective — just find any feasible with count fixed
            self.model.Maximize(0)

        logger.info("Two-phase solve: Phase 2 — maximize soft rule score")
        return self._run_solver(phase_timeout, start)

    def _run_solver(self, timeout: float, start: float) -> PlacementResult:
        """Run CP-SAT solver and extract results."""
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = timeout
        solver.parameters.num_workers = self.config.num_workers

        logger.info(
            f"Solving: {len(self.request.vms)} VMs, "
            f"{len(self.request.baremetals)} BMs, "
            f"{len(self.assign)} variables, "
            f"{len(self.effective_rules)} AG rules, "
            f"{len(self.ag_to_bms)} AGs"
        )

        status = solver.Solve(self.model)
        status_name = self._status_name(status)
        logger.info(f"Status: {status_name}")

        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return self._extract_solution(solver, status_name, time.time() - start)
        else:
            return PlacementResult(
                success=False,
                solver_status=status_name,
                solve_time_seconds=time.time() - start,
                unplaced_vms=[vm.id for vm in self.request.vms],
                diagnostics=self.diagnostics,
            )

    def _build_failure_diagnostics(self) -> dict[str, object]:
        """Delegate to DiagnosticsBuilder (app/diagnostics.py)."""
        from .diagnostics import DiagnosticsBuilder

        return DiagnosticsBuilder(
            request=self.request,
            vm_map=self.vm_map,
            bm_map=self.bm_map,
            ag_to_bms=self.ag_to_bms,
            effective_rules=self.effective_rules,
            config=self.config,
            num_variables=len(self.assign),
        ).build()

    def _extract_solution(
        self, solver: cp_model.CpSolver, status: str, elapsed: float
    ) -> PlacementResult:
        assignments = []
        unplaced = []

        for vm in self.request.vms:
            placed = False
            for bm in self.request.baremetals:
                if (vm.id, bm.id) in self.assign:
                    if solver.value(self.assign[(vm.id, bm.id)]) == 1:
                        assignments.append(PlacementAssignment(
                            vm_id=vm.id,
                            vm_hostname=vm.hostname,
                            baremetal_id=bm.id,
                            bm_hostname=bm.hostname,
                            ag=bm.topology.ag,
                        ))
                        placed = True
                        break
            if not placed:
                unplaced.append(vm.id)

        return PlacementResult(
            success=len(unplaced) == 0,
            assignments=assignments,
            solver_status=status,
            solve_time_seconds=elapsed,
            unplaced_vms=unplaced,
            diagnostics=self.diagnostics,
        )

    @staticmethod
    def _status_name(status: cp_model.CpSolverStatus) -> str:
        return {
            cp_model.OPTIMAL: "OPTIMAL",
            cp_model.FEASIBLE: "FEASIBLE",
            cp_model.INFEASIBLE: "INFEASIBLE",
            cp_model.MODEL_INVALID: "MODEL_INVALID",
            cp_model.UNKNOWN: "UNKNOWN",
        }.get(status, f"STATUS_{status}")
