"""
Failure diagnostics for the VM Placement Solver.

Extracted from solver.py to keep diagnostic logic (which runs AFTER
a solve failure) separate from the main solve path.

The constraint layer check rebuilds small throwaway models to pinpoint
which constraint layer first causes INFEASIBLE.
"""

from __future__ import annotations

from collections import defaultdict

from ortools.sat.python import cp_model

from .models import (
    PlacementRequest,
    AntiAffinityRule,
    Baremetal,
    ExclusiveBaremetalRule,
    FailoverRule,
    MaxPerBaremetalRule,
    VM,
    SolverConfig,
)
from .solver import get_eligible_baremetals, RESOURCE_FIELDS


def status_name(status: cp_model.CpSolverStatus) -> str:
    return {
        cp_model.OPTIMAL: "OPTIMAL",
        cp_model.FEASIBLE: "FEASIBLE",
        cp_model.INFEASIBLE: "INFEASIBLE",
        cp_model.MODEL_INVALID: "MODEL_INVALID",
        cp_model.UNKNOWN: "UNKNOWN",
    }.get(status, f"STATUS_{status}")


class DiagnosticsBuilder:
    """
    Builds diagnostic info after an INFEASIBLE / UNKNOWN solve result.

    Designed to produce output readable at a glance by the Go scheduler.
    """

    def __init__(
        self,
        request: PlacementRequest,
        vm_map: dict[str, VM],
        bm_map: dict[str, Baremetal],
        dim_to_bms: dict[str, dict[str, list[str]]],
        effective_rules: list[AntiAffinityRule],
        max_per_bm_rules: list[MaxPerBaremetalRule],
        exclusive_rules: list[ExclusiveBaremetalRule],
        failover_resolved: list[tuple[FailoverRule, list[str], list[str]]],
        config: SolverConfig,
        num_variables: int,
    ):
        self.request = request
        self.vm_map = vm_map
        self.bm_map = bm_map
        self.dim_to_bms = dim_to_bms
        self.effective_rules = effective_rules
        self.max_per_bm_rules = max_per_bm_rules
        self.exclusive_rules = exclusive_rules
        self.failover_resolved = failover_resolved
        self.config = config
        self.num_variables = num_variables

    def _eligible(self, vm: VM) -> list[str]:
        return get_eligible_baremetals(vm, self.bm_map, self.request.baremetals)

    def build(self) -> dict[str, object]:
        """Main entry point — collect all diagnostic sections."""
        diag: dict[str, object] = {}

        # 1. VMs with no eligible BMs — the most common root cause
        no_eligible = [vm.id for vm in self.request.vms if not self._eligible(vm)]
        if no_eligible:
            diag["vms_with_no_eligible_bm"] = no_eligible

        # 2. Anti-affinity rules — only flag infeasible ones
        infeasible_rules = self._check_anti_affinity_feasibility()
        if infeasible_rules:
            diag["infeasible_anti_affinity_rules"] = infeasible_rules

        # 2b. Per-baremetal rules — flag rules where cap × eligible BMs < vm count
        infeasible_bm_rules = self._check_max_per_bm_feasibility()
        if infeasible_bm_rules:
            diag["infeasible_max_per_bm_rules"] = infeasible_bm_rules

        # 2c'. Exclusive rules — every member needs a whole BM to itself, so
        # a group with more members than reachable BMs can never place.
        infeasible_exclusive = self._check_exclusive_feasibility()
        if infeasible_exclusive:
            diag["infeasible_exclusive_rules"] = infeasible_exclusive

        # 2c. Failover rules — flag structurally infeasible cases (e.g.
        # |primary in worst bucket| > |backup outside that bucket|).
        infeasible_failover_rules = self._check_failover_feasibility()
        if infeasible_failover_rules:
            diag["infeasible_failover_rules"] = infeasible_failover_rules

        # 3. Constraint layer check — which layer first causes INFEASIBLE
        diag["constraint_check"] = self._constraint_layer_check()

        # 4. Summary counts
        diag["counts"] = {
            "vms": len(self.request.vms),
            "bms": len(self.request.baremetals),
            "ags": len(self.dim_to_bms.get("ag", {})),
            "variables": self.num_variables,
            "rules": len(self.effective_rules),
            "max_per_bm_rules": len(self.max_per_bm_rules),
            "exclusive_rules": len(self.exclusive_rules),
        }

        return diag

    def _check_anti_affinity_feasibility(self) -> list[dict]:
        """
        For each rule and each dimension in spread_on, check whether the
        reachable buckets in that dimension can accommodate the group's VMs
        under cap_d. If any (rule, dimension) pair is structurally
        infeasible, record it.
        """
        import math

        infeasible = []
        for rule in self.effective_rules:
            N = len(rule.vm_ids)
            if N == 0:
                continue

            # Reachable BMs per dim's bucket (only counts BMs that some VM
            # in the rule can actually reach via candidate_baremetals).
            per_dim_caps: dict[str, int] = {}
            failed_dims: list[dict] = []
            cap_overrides = rule.cap_per_bucket or {}

            for dim in rule.spread_on:
                reachable_buckets: set[str] = set()
                for vm_id in rule.vm_ids:
                    if vm_id in self.vm_map:
                        for bm_id in self._eligible(self.vm_map[vm_id]):
                            if bm_id in self.bm_map:
                                reachable_buckets.add(
                                    getattr(self.bm_map[bm_id].topology, dim)
                                )
                num_buckets_global = len(self.dim_to_bms.get(dim, {}))
                cap = cap_overrides.get(
                    dim,
                    math.ceil(N / max(num_buckets_global, 1)),
                )
                per_dim_caps[dim] = cap
                if cap < 1:
                    continue
                min_buckets_needed = -(-N // cap)  # ceil division
                if len(reachable_buckets) < min_buckets_needed:
                    failed_dims.append({
                        "dimension": dim,
                        "cap_per_bucket": cap,
                        "min_buckets_needed": min_buckets_needed,
                        "reachable_buckets": len(reachable_buckets),
                    })

            if failed_dims:
                infeasible.append({
                    "group_id": rule.group_id,
                    "vm_count": N,
                    "per_dimension_caps": per_dim_caps,
                    "failed_dimensions": failed_dims,
                })
        return infeasible

    def _check_failover_feasibility(self) -> list[dict]:
        """
        Structural check: for each failover rule, find the bucket of its
        fault_domain that reaches the most primaries and check whether
        |backup| - (max backups potentially in that bucket) >= primaries
        in that bucket. Since assignment isn't known yet, we report any rule
        where primaries are confined to too few buckets to satisfy the
        invariant under any placement.

        Specifically, when |primary| > |backup|, the rule is unconditionally
        infeasible (caught earlier as INPUT_ERROR, but we surface it here too
        when diagnostics is invoked on a solve failure).
        """
        infeasible = []
        for f, primary_ids, backup_ids in self.failover_resolved:
            # Already validated in solver._resolve_failover_rules, but stay
            # defensive — if it slips through, it lands here as a flag.
            if f.policy == "n_minus_1" and len(primary_ids) > len(backup_ids):
                infeasible.append({
                    "rule_id": f.rule_id,
                    "primary_count": len(primary_ids),
                    "backup_count": len(backup_ids),
                    "fault_domain": f.fault_domain,
                    "details": "|primary| > |backup| under n_minus_1",
                })
        return infeasible

    def _check_max_per_bm_feasibility(self) -> list[dict]:
        """
        A per-BM rule is structurally infeasible when:
          cap × (# distinct BMs reachable by group's VMs) < group size

        Catches the common "1 BM but 3 masters with max_per_bm=1" case
        without running the full solver.
        """
        infeasible = []
        for rule in self.max_per_bm_rules:
            reachable_bms: set[str] = set()
            for vm_id in rule.vm_ids:
                if vm_id in self.vm_map:
                    reachable_bms.update(self._eligible(self.vm_map[vm_id]))
            capacity = rule.max_per_bm * len(reachable_bms)
            if capacity < len(rule.vm_ids):
                infeasible.append({
                    "group_id": rule.group_id,
                    "vm_count": len(rule.vm_ids),
                    "max_per_bm": rule.max_per_bm,
                    "reachable_bms": len(reachable_bms),
                    "slots_available": capacity,
                })
        return infeasible

    def _check_exclusive_feasibility(self) -> list[dict]:
        """
        C6 counting bound: solo occupancy means |G| members need |G| distinct
        reachable BMs — capacity is irrelevant, this is pure counting.
        """
        infeasible = []
        for rule in self.exclusive_rules:
            reachable: set[str] = set()
            for vm_id in rule.vm_ids:
                if vm_id in self.vm_map:
                    reachable.update(self._eligible(self.vm_map[vm_id]))
            if len(reachable) < len(rule.vm_ids):
                infeasible.append({
                    "group_id": rule.group_id,
                    "vm_count": len(rule.vm_ids),
                    "reachable_bms": len(reachable),
                    "bms_needed": len(rule.vm_ids),
                })
        return infeasible

    def _constraint_layer_check(self) -> dict[str, object]:
        """
        Incrementally add constraint layers and solve each to pinpoint
        which layer first causes INFEASIBLE.

        Returns e.g.:
          {"one_bm_per_vm": "OK", "capacity": "OK", "anti_affinity": "INFEASIBLE",
           "failed_at": "anti_affinity"}
        """
        eligible: dict[str, list[str]] = {
            vm.id: self._eligible(vm) for vm in self.request.vms
        }

        def make_vars(model: cp_model.CpModel):
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
            import math
            for rule in self.effective_rules:
                N = len(rule.vm_ids)
                if N == 0:
                    continue
                cap_overrides = rule.cap_per_bucket or {}
                for dim in rule.spread_on:
                    buckets = self.dim_to_bms.get(dim, {})
                    if not buckets:
                        continue
                    cap = cap_overrides.get(dim, math.ceil(N / len(buckets)))
                    if cap >= N:
                        continue
                    for bm_ids in buckets.values():
                        vbucket = [assign[(vid, bid)] for vid in rule.vm_ids
                                   for bid in bm_ids if (vid, bid) in assign]
                        if vbucket:
                            model.add(sum(vbucket) <= cap)

        def add_max_per_bm(model, assign):
            for rule in self.max_per_bm_rules:
                for bm_id in self.bm_map:
                    vbm = [assign[(vid, bm_id)] for vid in rule.vm_ids
                           if (vid, bm_id) in assign]
                    if vbm:
                        model.add(sum(vbm) <= rule.max_per_bm)

        def add_failover(model, assign):
            for f, primary_ids, backup_ids in self.failover_resolved:
                buckets = self.dim_to_bms.get(f.fault_domain, {})
                for bm_ids in buckets.values():
                    pin = [assign[(vid, bid)] for vid in primary_ids
                           for bid in bm_ids if (vid, bid) in assign]
                    bin_ = [assign[(vid, bid)] for vid in backup_ids
                            for bid in bm_ids if (vid, bid) in assign]
                    if pin or bin_:
                        model.add(sum(pin) + sum(bin_) <= len(backup_ids))

        def add_exclusive(model, assign):
            for rule in self.exclusive_rules:
                members = set(rule.vm_ids)
                if not members:
                    continue
                for bm_id in self.bm_map:
                    mvars = [assign[(vid, bm_id)] for vid in rule.vm_ids
                             if (vid, bm_id) in assign]
                    if not mvars:
                        continue
                    z = model.new_bool_var(f"lx_{rule.group_id}__{bm_id}")
                    model.add_max_equality(z, mvars)
                    model.add(sum(mvars) <= 1)
                    for vid in self.vm_map:
                        if vid in members:
                            continue
                        if (vid, bm_id) in assign:
                            model.add(assign[(vid, bm_id)] + z <= 1)

        def quick_solve(model) -> str:
            s = cp_model.CpSolver()
            s.parameters.max_time_in_seconds = 5.0
            st = s.solve(model)
            return "OK" if st in (cp_model.OPTIMAL, cp_model.FEASIBLE) else status_name(st)

        layers = [
            ("one_bm_per_vm", [add_one_bm_per_vm]),
            ("capacity", [add_one_bm_per_vm, add_capacity]),
            ("anti_affinity", [add_one_bm_per_vm, add_capacity, add_anti_affinity]),
            ("failover", [add_one_bm_per_vm, add_capacity, add_anti_affinity, add_failover]),
            ("max_per_bm", [add_one_bm_per_vm, add_capacity, add_anti_affinity, add_failover, add_max_per_bm]),
            ("exclusive", [add_one_bm_per_vm, add_capacity, add_anti_affinity, add_failover, add_max_per_bm, add_exclusive]),
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
