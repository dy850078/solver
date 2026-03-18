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
from ortools.sat.python.cp_model_helper import CpSolverStatus

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


class VMPlacementSolver:

    def __init__(self, request: PlacementRequest):
        self.request = request
        self.config = request.config

        # Lookup maps for quick access
        self.vm_map: dict[str, VM] = {vm.id: vm for vm in request.vms}
        self.bm_map: dict[str, Baremetal] = {bm.id: bm for bm in request.baremetals}

        # Deduplicate baremetals — the Go scheduler may send duplicate BM entries
        # (e.g. when filtering candidates per-VM without dedup). Duplicates cause
        # anti-affinity constraints to count the same assign variable multiple times,
        # making valid placements appear infeasible.
        seen_bm_ids: set[str] = set()
        unique_bms: list[Baremetal] = []
        for bm in request.baremetals:
            if bm.id not in seen_bm_ids:
                seen_bm_ids.add(bm.id)
                unique_bms.append(bm)
            else:
                logger.warning("Duplicate BM %s removed from input", bm.id)
        self.request = request.model_copy(update={"baremetals": unique_bms})

        # Also deduplicate candidate_baremetals per VM
        for vm in self.request.vms:
            if vm.candidate_baremetals:
                deduped = list(dict.fromkeys(vm.candidate_baremetals))
                if len(deduped) < len(vm.candidate_baremetals):
                    logger.warning(
                        "VM %s had %d duplicate candidate BMs removed",
                        vm.id, len(vm.candidate_baremetals) - len(deduped),
                    )
                    vm.candidate_baremetals = deduped

        # Group baremetals by AG (needed for anti-affinity constraints)
        self.ag_to_bms: dict[str, list[str]] = defaultdict(list)
        for bm in self.request.baremetals:
            self.ag_to_bms[bm.topology.ag].append(bm.id)

        # Resolve anti-affinity rules (explicit + auto-generated)
        self.effective_rules = self._resolve_anti_affinity_rules()

        # The CP-SAT model — we'll add variables and constraints to this
        self.model = cp_model.CpModel()

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
        """
        Which baremetals can this VM possibly go on?

        If the Go scheduler provided a candidate list (from step 3 filtering),
        we only consider those. Otherwise, we consider any BM with enough
        available capacity.

        This is a PRE-FILTER — it reduces the problem size before we even
        start building the CP-SAT model. Fewer variables = faster solving.
        """
        if vm.candidate_baremetals:
            # Trust step 3, but double-check capacity
            return [
                bm_id for bm_id in vm.candidate_baremetals
                if bm_id in self.bm_map
                and vm.demand.fits_in(self.bm_map[bm_id].available_capacity)
            ]
        else:
            # Fallback: any BM with enough room
            return [
                bm.id for bm in self.request.baremetals
                if vm.demand.fits_in(bm.available_capacity)
            ]

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

        for (ip_type, role), vm_ids in groups.items():
            if len(vm_ids) >= 2 and num_ags > 0:
                import math
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
        """
        for vm in self.request.vms:
            vm_vars = [
                self.assign[(vm.id, bm_id)]
                for bm_id in self._get_eligible_baremetals(vm)
                if (vm.id, bm_id) in self.assign
            ]

            if not vm_vars:
                if self.config.allow_partial_placement:
                    continue  # skip this VM, it can't be placed
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
        建立 bm_used[bm_id] 變數：這台 BM 是否被使用。

        bm_used[bm] = max(assign[vm_1, bm], assign[vm_2, bm], ...)
        → 只要有任何 VM 放在這台 BM，bm_used = 1
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
        計算每台 BM 的 headroom penalty。

        對每台 BM：
        1. 計算每個資源維度的利用率（after_usage / total）
        2. 超過 headroom_upper_bound_pct 的部分計為 penalty
        3. 跨維度取最大值（最壞情況決定 penalty）

        返回每台 BM 的 penalty 變數列表。
        """
        penalties = []
        for bm in self.request.baremetals:
            dim_overs = []
            for field in RESOURCE_FIELDS:
                total_d = getattr(bm.total_capacity, field)
                if total_d == 0:
                    continue  # 避免除以零（gpu_count=0 的機器）

                used_d = getattr(bm.used_capacity, field)

                assigned_vars = [
                    (vm_id, self.assign[(vm_id, bm.id)])
                    for vm_id in self.vm_map
                    if (vm_id, bm.id) in self.assign
                ]
                if not assigned_vars:
                    continue

                # 新放入的 VM 消耗
                new_usage = sum(
                    getattr(self.vm_map[vm_id].demand, field) * var
                    for vm_id, var in assigned_vars
                )

                # Step A: 計算放置後的使用量 × 100（避免浮點）
                # 上界用 max(total, used + 所有候選 VM demand 加總) 避免已有高 used_d 時
                # 造成假性 INFEASIBLE（capacity constraint 會正確限制實際放置量）
                max_new_demand = sum(
                    getattr(self.vm_map[vm_id].demand, field)
                    for vm_id, _ in assigned_vars
                )
                upper_after = max(total_d, used_d + max_new_demand) * 100
                after_times_100 = self.model.new_int_var(
                    0, upper_after, f"a100_{bm.id}_{field}"
                )
                self.model.add(after_times_100 == (used_d + new_usage) * 100)

                # Step B: 整數百分比（可能 > 100 如果 BM 已接近滿載且有多個候選 VM）
                max_util = upper_after // total_d if total_d > 0 else 0
                util_pct = self.model.new_int_var(0, max_util, f"util_{bm.id}_{field}")
                self.model.add_division_equality(util_pct, after_times_100, total_d)

                # Step C: 超過安全上限的量（可能為負）
                raw = self.model.new_int_var(-max_util, max_util, f"raw_{bm.id}_{field}")
                self.model.add(raw == util_pct - self.config.headroom_upper_bound_pct)

                # Step D: ReLU：截斷負值
                over = self.model.new_int_var(0, max_util, f"over_{bm.id}_{field}")
                self.model.add_max_equality(over, [self.model.new_constant(0), raw])
                dim_overs.append(over)

            if dim_overs:
                # Step E: 跨維度取最大值
                bm_penalty = self.model.new_int_var(0, 1000, f"hp_{bm.id}")
                self.model.add_max_equality(bm_penalty, dim_overs)
                penalties.append(bm_penalty)

        return penalties

    def _compute_slot_score_bonus(self) -> list[cp_model.IntVar]:
        """
        計算每台 BM 的 slot score（可容納的 t-shirt size VM 數量）。

        對每台 BM：
        1. 計算放置後每個資源維度的剩餘容量
        2. 對每個 t-shirt size，計算每個維度能容納幾個（floor division）
        3. 取跨維度最小值 = 該 t-shirt size 實際可容納數量
        4. 加總所有 t-shirt size → 該 BM 的 slot score

        返回每台 BM 的 slot score 變數列表。
        Slot score 越高 = 剩餘空間越有用 → 應被獎勵（在目標函數中取負號）。
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
                        continue  # 此維度無需求，不構成瓶頸

                    total_d = getattr(bm.total_capacity, field)
                    used_d = getattr(bm.used_capacity, field)

                    # 新放入的 VM 消耗
                    new_usage = sum(
                        getattr(self.vm_map[vm_id].demand, field) * var
                        for vm_id, var in assigned_vars
                    )

                    # 剩餘容量 = total - used - new_placement
                    # 下界用負值避免多 VM 候選時 variable bound 過緊造成假性 INFEASIBLE
                    # （capacity constraint 保證實際放置時 remaining >= 0）
                    max_new_d = sum(
                        getattr(self.vm_map[vm_id].demand, field)
                        for vm_id, _ in assigned_vars
                    )
                    remaining = self.model.new_int_var(
                        total_d - used_d - max_new_d, total_d,
                        f"rem_{bm.id}_{field}_t{t_idx}",
                    )
                    self.model.add(remaining == total_d - used_d - new_usage)

                    # 此維度可容納幾個此 t-shirt size
                    # remaining 可能為負（capacity constraint 保證實際放置時不會）
                    # division 的結果也可能為負，需要允許
                    min_slots = (total_d - used_d - max_new_d) // tshirt_d if tshirt_d > 0 else 0
                    max_slots = total_d // tshirt_d if tshirt_d > 0 else 0
                    slots_d = self.model.new_int_var(
                        min(min_slots, 0), max_slots,
                        f"slotd_{bm.id}_{field}_t{t_idx}",
                    )
                    self.model.add_division_equality(slots_d, remaining, tshirt_d)
                    dim_slots.append(slots_d)

                if dim_slots:
                    # 跨維度取最小值 = 實際可容納數量（瓶頸維度決定）
                    max_possible = min(
                        getattr(bm.total_capacity, f) // getattr(tshirt, f)
                        for f in RESOURCE_FIELDS
                        if getattr(tshirt, f) > 0
                    )
                    # min 可能為負（capacity constraint 保證實際放置時 >= 0）
                    slots_for_tshirt = self.model.new_int_var(
                        -max_possible, max_possible, f"slot_{bm.id}_t{t_idx}"
                    )
                    self.model.add_min_equality(slots_for_tshirt, dim_slots)
                    tshirt_slots.append(slots_for_tshirt)

            if tshirt_slots:
                # 加總所有 t-shirt size 的可容納數量
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

                # 只計算被使用的 BM 的 slot score（未使用的 BM 不計入）
                # 否則 solver 會偏好把 VM 放小 BM，讓大 BM 保持高 slot score
                effective = self.model.new_int_var(
                    -max_total, max_total, f"eff_sscore_{bm.id}"
                )
                self.model.add_multiplication_equality(
                    effective, [self.bm_used[bm.id], bm_score]
                )
                scores.append(effective)

        return scores

    def _ensure_bm_used_vars(self):
        """建立 bm_used 變數（如果尚未建立）。多個目標項可能都需要它。"""
        if not self.bm_used:
            self._build_bm_used_vars()

    def _add_objective(self):
        """
        組合所有目標函數項並設定 Minimize。

        優先級（由高到低）：
        1. 放置盡量多的 VM（partial placement 模式）
        2. 使用盡量少的 BM（consolidation）
        3. 利用率不超過安全上限（headroom）
        4. 剩餘容量的可用性（slot score）
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
                # 負號：slot score 越高越好（Minimize 中用負值 = 獎勵）
                terms.append(-self.config.w_slot_score * sum(slot_scores))

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
                return self._extract_solution(solver, status_name, time.time() - start)
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

        except Exception as e:
            logger.exception("Solver failed")
            return PlacementResult(
                success=False,
                solver_status=f"ERROR: {e}",
                solve_time_seconds=time.time() - start,
                unplaced_vms=[vm.id for vm in self.request.vms],
            )

    def _build_failure_diagnostics(self) -> dict[str, object]:
        """
        Collect concise diagnostic info for INFEASIBLE / UNKNOWN results.

        Designed to be readable at a glance by the Go scheduler caller.
        """
        diag: dict[str, object] = {}

        # 1. VMs with no eligible BMs — the most common root cause
        no_eligible: list[str] = []
        for vm in self.request.vms:
            if not self._get_eligible_baremetals(vm):
                no_eligible.append(vm.id)
        if no_eligible:
            diag["vms_with_no_eligible_bm"] = no_eligible

        # 2. Anti-affinity rules — only flag infeasible ones
        infeasible_rules = []
        for rule in self.effective_rules:
            reachable_ags: set[str] = set()
            for vm_id in rule.vm_ids:
                if vm_id in self.vm_map:
                    for bm_id in self._get_eligible_baremetals(self.vm_map[vm_id]):
                        if bm_id in self.bm_map:
                            reachable_ags.add(self.bm_map[bm_id].topology.ag)
            min_ags_needed = -(-len(rule.vm_ids) // rule.max_per_ag)  # ceil division
            if len(reachable_ags) < min_ags_needed:
                infeasible_rules.append({
                    "group_id": rule.group_id,
                    "vm_count": len(rule.vm_ids),
                    "max_per_ag": rule.max_per_ag,
                    "min_ags_needed": min_ags_needed,
                    "reachable_ags": len(reachable_ags),
                })
        if infeasible_rules:
            diag["infeasible_anti_affinity_rules"] = infeasible_rules

        # 3. Constraint check — which layer causes INFEASIBLE
        diag["constraint_check"] = self._constraint_layer_check()

        # 4. Summary counts
        diag["counts"] = {
            "vms": len(self.request.vms),
            "bms": len(self.request.baremetals),
            "ags": len(self.ag_to_bms),
            "variables": len(self.assign),
            "rules": len(self.effective_rules),
        }

        return diag

    def _constraint_layer_check(self) -> dict[str, object]:
        """
        Incrementally add constraint layers and solve each to pinpoint
        which layer first causes INFEASIBLE.

        Returns a compact result like:
          {"one_bm_per_vm": "OK", "capacity": "OK", "anti_affinity": "INFEASIBLE",
           "failed_at": "anti_affinity"}
        """
        eligible: dict[str, list[str]] = {
            vm.id: self._get_eligible_baremetals(vm) for vm in self.request.vms
        }

        def make_vars(model: cp_model.CpModel) -> dict[tuple[str, str], cp_model.IntVar]:
            return {
                (vm.id, bm_id): model.new_bool_var(f"t_{vm.id}__{bm_id}")
                for vm in self.request.vms
                for bm_id in eligible[vm.id]
            }

        def add_one_bm_per_vm(model, assign):
            for vm in self.request.vms:
                vm_vars = [assign[(vm.id, bid)] for bid in eligible[vm.id]
                           if (vm.id, bid) in assign]
                if not vm_vars:
                    model.add(0 == 1)
                    return
                if self.config.allow_partial_placement:
                    model.add(sum(vm_vars) <= 1)
                else:
                    model.add(sum(vm_vars) == 1)

        def add_capacity(model, assign):
            for bm in self.request.baremetals:
                avail = bm.available_capacity
                avars = [(vid, assign[(vid, bm.id)]) for vid in self.vm_map
                         if (vid, bm.id) in assign]
                if not avars:
                    continue
                for field in RESOURCE_FIELDS:
                    usage = sum(getattr(self.vm_map[vid].demand, field) * v
                                for vid, v in avars)
                    model.add(usage <= getattr(avail, field))

        def add_anti_affinity(model, assign):
            for rule in self.effective_rules:
                for ag, ag_bm_ids in self.ag_to_bms.items():
                    vag = [assign[(vid, bid)] for vid in rule.vm_ids
                           for bid in ag_bm_ids if (vid, bid) in assign]
                    if vag:
                        model.add(sum(vag) <= rule.max_per_ag)

        def quick_solve(model) -> str:
            s = cp_model.CpSolver()
            s.parameters.max_time_in_seconds = 5.0
            status = s.solve(model)
            return "OK" if status in (cp_model.OPTIMAL, cp_model.FEASIBLE) \
                else self._status_name(status)

        # Layer 1: one-BM-per-VM
        layers = [
            ("one_bm_per_vm", [add_one_bm_per_vm]),
            ("capacity", [add_one_bm_per_vm, add_capacity]),
            ("anti_affinity", [add_one_bm_per_vm, add_capacity, add_anti_affinity]),
        ]

        results: dict[str, object] = {}
        failed_at = None
        for name, builders in layers:
            m = cp_model.CpModel()
            a = make_vars(m)
            for build in builders:
                build(m, a)
            results[name] = quick_solve(m)
            if results[name] != "OK" and failed_at is None:
                failed_at = name

        results["failed_at"] = failed_at
        return results

    def _extract_solution(
        self, solver: cp_model.CpSolver, status: str, elapsed: float
    ) -> PlacementResult:
        """Read the solution: which assign variables are set to 1?"""
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
        )

    @staticmethod
    def _status_name(status: CpSolverStatus) -> str:
        return {
            cp_model.OPTIMAL: "OPTIMAL",
            cp_model.FEASIBLE: "FEASIBLE",
            cp_model.INFEASIBLE: "INFEASIBLE",
            cp_model.MODEL_INVALID: "MODEL_INVALID",
            cp_model.UNKNOWN: "UNKNOWN",
        }.get(status, f"STATUS_{status}")
