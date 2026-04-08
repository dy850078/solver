"""
VM Placement Solver — Step 1: Hard Constraints Only

This is the minimal viable solver. It answers the question:
  "Is there ANY valid way to assign these VMs to these baremetals?"

What it does:
  1. Each VM is assigned to exactly one baremetal
  2. Baremetal capacity is not exceeded (cpu, mem, disk, gpu)
  3. Anti-affinity rules are respected (max N VMs per AG)
  4. Candidate lists from step 3 are respected

What it does NOT do yet (we'll add these step by step):
  - No objective function (any feasible solution is returned)
  - No optimization (no preference for "better" placements)

HOW CP-SAT WORKS (brief primer):
  CP-SAT is a constraint programming solver. You tell it:
    - Variables: things that can take different values
    - Constraints: rules the variables must satisfy
    - Objective (optional): what to minimize/maximize
  It then searches for variable assignments that satisfy all constraints.

  In our case:
    - Variables: assign[vm_i, bm_j] = 0 or 1 (boolean)
    - Constraints: capacity limits, one-BM-per-VM, anti-affinity
    - Objective: none yet (just find any feasible solution)
"""

from __future__ import annotations

import time
import logging
from collections import defaultdict

from ortools.sat.python import cp_model

from .models import (
    PlacementRequest,
    PlacementResult,
    PlacementAssignment,
    AntiAffinityRule,
    Baremetal,
    VM,
    Resources,
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


class VMPlacementSolver:

    def __init__(
        self,
        request: PlacementRequest,
        *,
        model: cp_model.CpModel | None = None,
        active_vars: dict[str, cp_model.IntVar] | None = None,
    ):
        self.request = request
        self.config = request.config
        self.active_vars: dict[str, cp_model.IntVar] = active_vars or {}

        # Lookup maps for quick access
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

        # Non-fatal advisories collected during rule resolution (e.g. policy
        # target not met). Surfaced via PlacementResult.diagnostics["advisories"].
        self.advisories: list[dict] = []

        # Resolve anti-affinity rules (explicit + auto-generated)
        self.effective_rules = self._resolve_anti_affinity_rules()

        # Waste penalty terms injected by split_solver (splitter integration)
        self.splitter_waste_terms: list[cp_model.LinearExprT] = []

        # The CP-SAT model — shared with splitter when called from split_solver
        self.model = model if model is not None else cp_model.CpModel()

        # Decision variables: assign[(vm_id, bm_id)] = BoolVar
        # Only created for eligible (vm, bm) pairs — this is important
        # because it means we never even consider impossible assignments.
        self.assign: dict[tuple[str, str], cp_model.IntVar] = {}

        # Objective helper: bm_used[bm_id] = 1 if any VM is placed on that BM
        self.bm_used: dict[str, cp_model.IntVar] = {}

    # ------------------------------------------------------------------
    # Step A: Determine which (VM, BM) pairs are eligible
    # ------------------------------------------------------------------

    def _get_eligible_baremetals(self, vm: VM) -> list[str]:
        """Delegate to module-level function for reuse by diagnostics."""
        return get_eligible_baremetals(vm, self.bm_map, self.request.baremetals)

    # ------------------------------------------------------------------
    # Step B: Auto-generate anti-affinity rules
    # ------------------------------------------------------------------

    def _resolve_anti_affinity_rules(self) -> list[AntiAffinityRule]:
        """
        Combine explicit rules with auto-generated ones.

        Auto-generation: group VMs by (ip_type, node_role), and for each
        group with 2+ VMs, create a rule that spreads them across AGs.

        max_per_ag is computed dynamically:
          max_per_ag = ceil(num_vms_in_group / num_ags)

        Example: 5 routable masters, 3 AGs → ceil(5/3) = 2 → allows 2/2/1
        Example: 3 non-routable workers, 3 AGs → ceil(3/3) = 1 → each in different AG

        VMs already covered by explicit rules are not auto-generated.
        VMs with empty ip_type are skipped (can't group them meaningfully).
        """
        rules = list(self.request.anti_affinity_rules)

        if not self.config.auto_generate_anti_affinity:
            return rules

        # How many AGs do we have?
        num_ags = len(self.ag_to_bms)

        # Which VMs are already in explicit rules?
        covered: set[str] = set()
        for rule in rules:
            covered.update(rule.vm_ids)

        # Group remaining VMs by (ip_type, role) — this is the anti-affinity grouping key.
        # VMs with the same ip_type and node_role should spread across AGs.
        groups: dict[tuple[str, str], list[str]] = defaultdict(list)
        for vm in self.request.vms:
            if vm.id not in covered and vm.ip_type:
                groups[(vm.ip_type, vm.node_role.value)].append(vm.id)

        target_spread = self.config.target_ag_spread

        for (ip_type, role), vm_ids in groups.items():
            if len(vm_ids) >= 2 and num_ags > 0:
                import math
                max_per_ag = math.ceil(len(vm_ids) / num_ags)
                group_id = f"auto/{ip_type}/{role}"
                rules.append(AntiAffinityRule(
                    group_id=group_id,
                    vm_ids=vm_ids,
                    max_per_ag=max_per_ag,
                ))
                logger.info(
                    "Auto anti-affinity: %s/%s (%d VMs / %d AGs → max_per_ag=%d)",
                    ip_type, role, len(vm_ids), num_ags, max_per_ag,
                )

                # Policy check: is the actual spread below the policy target?
                # effective_spread is the most AGs we could possibly use for
                # this group, bounded by both infra and group size.
                effective_spread = min(num_ags, len(vm_ids))
                if effective_spread < target_spread:
                    msg = (
                        f"Anti-affinity for {ip_type}/{role} below policy target: "
                        f"actual spread={effective_spread}, target={target_spread} "
                        f"({num_ags} AG(s), {len(vm_ids)} VMs)."
                    )
                    self.advisories.append({
                        "type": "ag_spread_below_target",
                        "severity": "warning",
                        "group_id": group_id,
                        "message": msg,
                        "details": {
                            "vm_count": len(vm_ids),
                            "num_ags": num_ags,
                            "effective_spread": effective_spread,
                            "target_ag_spread": target_spread,
                            "max_per_ag": max_per_ag,
                            "ag_names": sorted(self.ag_to_bms.keys()),
                        },
                    })
                    logger.warning("Spread advisory: %s", msg)

        return rules

    # ------------------------------------------------------------------
    # Step C: Build the CP-SAT model
    # ------------------------------------------------------------------

    def _build_variables(self):
        """
        Create one boolean variable for each eligible (VM, BM) pair.

        assign[(vm_id, bm_id)] = 1 means "vm is placed on bm"
        assign[(vm_id, bm_id)] = 0 means "vm is NOT placed on bm"

        We only create variables for pairs where the VM can actually fit.
        This is a key optimization — if you have 100 VMs and 50 BMs,
        you might only have 500 eligible pairs instead of 5000.
        """
        for vm in self.request.vms:
            for bm_id in self._get_eligible_baremetals(vm):
                self.assign[(vm.id, bm_id)] = self.model.new_bool_var(
                    f"assign_{vm.id}__{bm_id}"
                )

    def _add_one_bm_per_vm_constraint(self):
        """
        CONSTRAINT: Each VM must be assigned to exactly one baremetal.

        For each VM: sum of all its assignment variables == 1
        (exactly one of them is "on")

        If allow_partial_placement is True, we use <= 1 instead
        (the VM might not be placed at all).

        Synthetic VMs from the splitter carry an active_var. When active=0
        the VM is unused; when active=1 it must be placed on exactly one BM.
        """
        for vm in self.request.vms:
            vm_vars = [
                self.assign[(vm.id, bm_id)]
                for bm_id in self._get_eligible_baremetals(vm)
                if (vm.id, bm_id) in self.assign
            ]
            active_var = self.active_vars.get(vm.id)

            if not vm_vars:
                if active_var is not None:
                    # Splitter slot with no eligible BM → force inactive
                    self.model.add(active_var == 0)
                    continue
                elif self.config.allow_partial_placement:
                    continue  # skip this VM, it can't be placed
                else:
                    logger.error("VM %s has no eligible BMs → infeasible", vm.id)
                    self.model.add(0 == 1)  # force infeasibility
                    return

            if active_var is not None:
                # Synthetic VM: placed on exactly one BM iff the splitter activates it
                self.model.add(sum(vm_vars) == active_var)
            elif self.config.allow_partial_placement:
                self.model.add(sum(vm_vars) <= 1)
            else:
                self.model.add(sum(vm_vars) == 1)

    def _add_capacity_constraints(self):
        """
        CONSTRAINT: Total VM demand on each BM must not exceed its available capacity.

        For each baremetal, for each resource dimension (cpu, mem, disk, gpu):
          sum of (vm_demand * assign_var) for all VMs eligible on this BM <= available_capacity

        Example: BM has 64 available CPU cores.
          VM-A needs 16 cores, VM-B needs 8 cores, VM-C needs 32 cores.
          If all three are assigned here: 16+8+32 = 56 <= 64 ✓
          If we also add VM-D (16 cores): 56+16 = 72 > 64 ✗
        """
        for bm in self.request.baremetals:
            avail = bm.available_capacity

            # Collect all (vm_id, assign_var) pairs for VMs eligible on this BM
            assigned_vars = [
                (vm_id, self.assign[(vm_id, bm.id)])
                for vm_id in self.vm_map
                if (vm_id, bm.id) in self.assign
            ]

            if not assigned_vars:
                continue

            # For each resource dimension, add a capacity constraint
            for field in RESOURCE_FIELDS:
                capacity = getattr(avail, field)

                # Build the usage expression: sum(demand * var)
                usage = sum(
                    getattr(self.vm_map[vm_id].demand, field) * var
                    for vm_id, var in assigned_vars
                )

                # The constraint: total usage <= capacity
                self.model.add(usage <= capacity)

    def _add_anti_affinity_constraints(self):
        """
        CONSTRAINT: VMs in the same anti-affinity group are spread across AGs.

        For each rule, for each AG:
          count of VMs from this group assigned to BMs in this AG <= max_per_ag

        Example: 3 master VMs, max_per_ag=1, 3 AGs
          → at most 1 master per AG → each master in a different AG ✓

        Example: 6 worker VMs, max_per_ag=2, 3 AGs
          → at most 2 workers per AG → workers spread across AGs ✓
        """
        for rule in self.effective_rules:
            for ag, ag_bm_ids in self.ag_to_bms.items():
                # Collect assign vars for VMs in this rule × BMs in this AG
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
        tshirt_sizes = self.config.vm_specs
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

        waste_terms = self.splitter_waste_terms
        if waste_terms and self.config.w_resource_waste > 0:
            terms.append(self.config.w_resource_waste * sum(waste_terms))

        if terms:
            self.model.minimize(sum(terms))

    # ------------------------------------------------------------------
    # Step D: Solve and extract results
    # ------------------------------------------------------------------

    def solve(self) -> PlacementResult:
        """
        Build the model, solve it, return results.

        This is the main entry point.
        """
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
                diagnostics=self._with_advisories({"input_errors": self._input_errors}),
            )

        try:
            # Build the model
            self._build_variables()
            self._add_one_bm_per_vm_constraint()
            self._add_capacity_constraints()
            self._add_anti_affinity_constraints()

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

            # Extract results
            if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                self._last_cp_solver = solver
                return self._extract_solution(solver, status_name, time.time() - start)
            else:
                diagnostics = self._with_advisories(self._build_failure_diagnostics())
                logger.warning("Solver failed with %s, diagnostics: %s", status_name, diagnostics)
                return PlacementResult(
                    success=False,
                    solver_status=status_name,
                    solve_time_seconds=time.time() - start,
                    unplaced_vms=[vm.id for vm in self.request.vms],
                    diagnostics=diagnostics,
                )

        except Exception as e:
            logger.exception("Solver failed")
            return PlacementResult(
                success=False,
                solver_status=f"ERROR: {e}",
                solve_time_seconds=time.time() - start,
                unplaced_vms=[vm.id for vm in self.request.vms],
                diagnostics=self._with_advisories({}),
            )

    def _with_advisories(self, diagnostics: dict) -> dict:
        """Merge collected advisories into a diagnostics dict (no-op if none)."""
        if self.advisories:
            diagnostics = dict(diagnostics)
            diagnostics["advisories"] = self.advisories
        return diagnostics

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
        """Read the solution: which assign variables are set to 1?"""
        assignments = []
        unplaced = []

        for vm in self.request.vms:
            active_var = self.active_vars.get(vm.id)
            if active_var is not None and solver.value(active_var) == 0:
                continue  # splitter decided this slot is unused
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
            diagnostics=self._with_advisories({}),
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
