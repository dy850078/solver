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
from dataclasses import dataclass, field as dataclass_field
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

RESOURCE_FIELDS = ["cpu_cores", "memory_mb", "disk_gb", "gpu_count"]


@dataclass
class _AssumptionRecord:
    """
    Tracks one assumable constraint in the diagnostic model.

    category:
      - "vm_placement"  — this VM must be placed
      - "anti_affinity" — per-AG limit for an anti-affinity rule
      - "bm_count_limit"— BM VM count limit
      - "topology_rule" — cross-cluster hard anti-affinity topology constraint
    """
    var: Any  # cp_model.IntVar (BoolVar)
    category: str
    message: str
    detail: dict[str, Any] = dataclass_field(default_factory=dict)


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

        # Group baremetals by AG
        self.ag_to_bms: dict[str, list[str]] = defaultdict(list)
        for bm in request.baremetals:
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

    # ------------------------------------------------------------------
    # Eligibility pre-filter
    # ------------------------------------------------------------------

    def _get_eligible_baremetals(self, vm: VM) -> list[str]:
        if vm.candidate_baremetals:
            return [
                bm_id for bm_id in vm.candidate_baremetals
                if bm_id in self.bm_map
                and vm.demand.fits_in(self.bm_map[bm_id].available_capacity)
            ]
        else:
            return [
                bm.id for bm in self.request.baremetals
                if vm.demand.fits_in(bm.available_capacity)
            ]

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
                    f"Auto anti-affinity: {ip_type}/{role} "
                    f"({len(vm_ids)} VMs / {num_ags} AGs -> max_per_ag={max_per_ag})"
                )

        return rules

    # ------------------------------------------------------------------
    # Build CP-SAT model
    # ------------------------------------------------------------------

    def _build_variables(self):
        for vm in self.request.vms:
            for bm_id in self._get_eligible_baremetals(vm):
                self.assign[(vm.id, bm_id)] = self.model.NewBoolVar(
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
                    logger.error(f"VM {vm.id} has no eligible BMs -> infeasible")
                    self.model.Add(0 == 1)
                    return

            if self.config.allow_partial_placement:
                self.model.Add(sum(vm_vars) <= 1)
            else:
                self.model.Add(sum(vm_vars) == 1)

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
                self.model.Add(usage <= capacity)

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
                    self.model.Add(sum(vars_in_ag) <= rule.max_per_ag)

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

            # Build soft objective terms
            soft_terms = self._build_soft_objective_terms(validated_rules)

            # Decide solving strategy
            needs_two_phase = (
                self.config.allow_partial_placement and has_soft_rules
            )

            if needs_two_phase:
                result = self._solve_two_phase(soft_terms, start)
            else:
                result = self._solve_single(soft_terms, start)

            # Enrich INFEASIBLE results with a structured UNSAT core explanation
            if result.solver_status == "INFEASIBLE":
                reasons = self._diagnose_infeasibility(validated_rules)
                result = result.model_copy(
                    update={"infeasibility_reasons": reasons}
                )

            return result

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

    def _extract_solution(
        self, solver: cp_model.CpSolver, status: str, elapsed: float
    ) -> PlacementResult:
        assignments = []
        unplaced = []

        for vm in self.request.vms:
            placed = False
            for bm in self.request.baremetals:
                if (vm.id, bm.id) in self.assign:
                    if solver.Value(self.assign[(vm.id, bm.id)]) == 1:
                        assignments.append(PlacementAssignment(
                            vm_id=vm.id,
                            baremetal_id=bm.id,
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

    # ------------------------------------------------------------------
    # Infeasibility diagnosis via CP-SAT assumptions
    # ------------------------------------------------------------------

    def _diagnose_infeasibility(
        self, validated_rules: list[TopologyRule]
    ) -> list[dict[str, Any]]:
        """
        Build a fresh diagnostic CP-SAT model where each high-level constraint
        is guarded by an assumption BoolVar.  Then call
        SufficientAssumptionsForInfeasibility() to get a minimal UNSAT core.

        Hard constraints kept always-on (not assumable):
          - BM resource capacity (physical limit, never negotiable)
          - at-most-1-BM-per-VM

        Assumable constraints (each gets its own BoolVar):
          1. vm_placement:   VM must be placed somewhere  (sum >= 1)
          2. anti_affinity:  per-AG max limit for each rule
          3. bm_count_limit: BM total VM count cap
          4. topology_rule:  cross-cluster hard anti-affinity zone blocks

        Returns a list of reason dicts (category, message, + detail fields).
        """
        diag_model = cp_model.CpModel()

        # Re-build assignment variables in the diagnostic model
        diag_assign: dict[tuple[str, str], cp_model.IntVar] = {}
        for vm in self.request.vms:
            for bm_id in self._get_eligible_baremetals(vm):
                diag_assign[(vm.id, bm_id)] = diag_model.NewBoolVar(
                    f"d_{vm.id}__{bm_id}"
                )

        # --- Always-hard: capacity ---
        for bm in self.request.baremetals:
            avail = bm.available_capacity
            assigned_vars = [
                (vm_id, diag_assign[(vm_id, bm.id)])
                for vm_id in self.vm_map
                if (vm_id, bm.id) in diag_assign
            ]
            if not assigned_vars:
                continue
            for f in RESOURCE_FIELDS:
                capacity = getattr(avail, f)
                usage = sum(
                    getattr(self.vm_map[vm_id].demand, f) * var
                    for vm_id, var in assigned_vars
                )
                diag_model.Add(usage <= capacity)

        # --- Always-hard: at most one BM per VM ---
        for vm in self.request.vms:
            vm_vars = [
                diag_assign[(vm.id, bm_id)]
                for bm_id in self._get_eligible_baremetals(vm)
                if (vm.id, bm_id) in diag_assign
            ]
            if vm_vars:
                diag_model.Add(sum(vm_vars) <= 1)

        # Assumption registry: literal index → record
        records: dict[int, _AssumptionRecord] = {}
        assumption_lits: list[cp_model.IntVar] = []

        def _register(rec: _AssumptionRecord) -> None:
            records[rec.var.Index()] = rec
            assumption_lits.append(rec.var)

        # --- Assumable: VM placement (must place each VM) ---
        for vm in self.request.vms:
            vm_vars = [
                diag_assign[(vm.id, bm_id)]
                for bm_id in self._get_eligible_baremetals(vm)
                if (vm.id, bm_id) in diag_assign
            ]
            must_place = diag_model.NewBoolVar(f"must_place_{vm.id}")
            if vm_vars:
                diag_model.Add(sum(vm_vars) >= 1).OnlyEnforceIf(must_place)
            else:
                # No eligible BMs at all — force contradiction when assumed
                sentinel = diag_model.NewBoolVar(f"no_bm_sentinel_{vm.id}")
                diag_model.Add(sentinel == 0)
                diag_model.Add(sentinel == 1).OnlyEnforceIf(must_place)
            _register(_AssumptionRecord(
                var=must_place,
                category="vm_placement",
                message=(
                    f"VM '{vm.id}' must be placed "
                    f"(role={vm.node_role.value}, {len(vm_vars)} eligible BM(s))"
                ),
                detail={
                    "vm_id": vm.id,
                    "node_role": vm.node_role.value,
                    "cluster_id": vm.cluster_id,
                    "eligible_bm_count": len(vm_vars),
                },
            ))

        # --- Assumable: AG anti-affinity (one assumption per rule × AG) ---
        for rule in self.effective_rules:
            for ag, ag_bm_ids in self.ag_to_bms.items():
                vars_in_ag = [
                    diag_assign[(vm_id, bm_id)]
                    for vm_id in rule.vm_ids
                    for bm_id in ag_bm_ids
                    if (vm_id, bm_id) in diag_assign
                ]
                if not vars_in_ag:
                    continue
                aa_var = diag_model.NewBoolVar(
                    f"aa_{rule.group_id.replace('/', '_')}_{ag}"
                )
                diag_model.Add(
                    sum(vars_in_ag) <= rule.max_per_ag
                ).OnlyEnforceIf(aa_var)
                _register(_AssumptionRecord(
                    var=aa_var,
                    category="anti_affinity",
                    message=(
                        f"Anti-affinity '{rule.group_id}': "
                        f"max {rule.max_per_ag} VM(s) per AG '{ag}'"
                    ),
                    detail={
                        "group_id": rule.group_id,
                        "ag": ag,
                        "max_per_ag": rule.max_per_ag,
                        "vm_ids": list(rule.vm_ids),
                    },
                ))

        # --- Assumable: BM VM count limits ---
        for bm in self.request.baremetals:
            if bm.max_vm_count is None:
                continue
            new_vars = [
                diag_assign[(vm_id, bm.id)]
                for vm_id in self.vm_map
                if (vm_id, bm.id) in diag_assign
            ]
            if not new_vars:
                continue
            count_var = diag_model.NewBoolVar(f"bm_count_{bm.id}")
            diag_model.Add(
                bm.current_vm_count + sum(new_vars) <= bm.max_vm_count
            ).OnlyEnforceIf(count_var)
            available_slots = bm.max_vm_count - bm.current_vm_count
            _register(_AssumptionRecord(
                var=count_var,
                category="bm_count_limit",
                message=(
                    f"BM '{bm.id}' VM count limit: "
                    f"current={bm.current_vm_count}, max={bm.max_vm_count} "
                    f"({available_slots} slot(s) available)"
                ),
                detail={
                    "bm_id": bm.id,
                    "current_vm_count": bm.current_vm_count,
                    "max_vm_count": bm.max_vm_count,
                    "available_slots": available_slots,
                },
            ))

        # --- Assumable: cross-cluster hard topology anti-affinity ---
        current_cluster_ids = {vm.cluster_id for vm in self.request.vms}
        for rule in validated_rules:
            if rule.type != "anti_affinity" or rule.enforcement != "hard":
                continue
            other_cids = set(rule.cluster_ids) - current_cluster_ids
            occupied_zones: set[str] = set()
            for cid in other_cids:
                occupied_zones.update(
                    self.existing_cluster_zones.get(cid, {}).get(rule.scope, set())
                )
            if not occupied_zones:
                continue

            topo_var = diag_model.NewBoolVar(f"topo_{rule.rule_id}")
            for vm in self.request.vms:
                if vm.cluster_id not in rule.cluster_ids:
                    continue
                for bm in self.request.baremetals:
                    if (vm.id, bm.id) not in diag_assign:
                        continue
                    bm_zone = _get_topology_zone(bm.topology, rule.scope)
                    if bm_zone in occupied_zones:
                        diag_model.Add(
                            diag_assign[(vm.id, bm.id)] == 0
                        ).OnlyEnforceIf(topo_var)
            _register(_AssumptionRecord(
                var=topo_var,
                category="topology_rule",
                message=(
                    f"Cross-cluster hard anti-affinity '{rule.rule_id}' "
                    f"at scope '{rule.scope}': "
                    f"zones {sorted(occupied_zones)} already occupied"
                ),
                detail={
                    "rule_id": rule.rule_id,
                    "scope": rule.scope,
                    "cluster_ids": sorted(rule.cluster_ids),
                    "occupied_zones": sorted(occupied_zones),
                },
            ))

        # Set assumptions and solve the diagnostic model
        diag_model.AddAssumptions(assumption_lits)

        diag_solver = cp_model.CpSolver()
        diag_solver.parameters.max_time_in_seconds = 30.0
        diag_solver.parameters.num_workers = 1

        status = diag_solver.Solve(diag_model)

        if status != cp_model.INFEASIBLE:
            logger.warning(
                f"Diagnostic model returned {self._status_name(status)}, "
                f"expected INFEASIBLE — returning empty reasons"
            )
            return []

        core_lits = diag_solver.SufficientAssumptionsForInfeasibility()
        logger.info(
            f"UNSAT core: {len(core_lits)} assumption(s) out of {len(assumption_lits)}"
        )

        reasons: list[dict[str, Any]] = []
        for lit in core_lits:
            rec = records.get(lit)
            if rec is None:
                logger.debug(f"Unknown literal {lit} in UNSAT core — skipping")
                continue
            reasons.append({"category": rec.category, "message": rec.message, **rec.detail})

        return reasons

    @staticmethod
    def _status_name(status: int) -> str:
        return {
            cp_model.OPTIMAL: "OPTIMAL",
            cp_model.FEASIBLE: "FEASIBLE",
            cp_model.INFEASIBLE: "INFEASIBLE",
            cp_model.MODEL_INVALID: "MODEL_INVALID",
            cp_model.UNKNOWN: "UNKNOWN",
        }.get(status, f"STATUS_{status}")
