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
from .models import res_get
from .solver import get_eligible_baremetals, request_dims


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

    def _pinned_in_bucket(self, vm_ids: list[str], bm_ids: list[str]) -> int:
        """Pinned census — mirrors VMPlacementSolver._pinned_count_in_bucket
        so grandfathered caps here match the main model exactly."""
        bucket = set(bm_ids)
        count = 0
        for vm_id in vm_ids:
            vm = self.vm_map.get(vm_id)
            if vm is not None and vm.pinned_to in bucket:
                count += 1
        return count

    def _rule_has_pinned(self, vm_ids: list[str]) -> bool:
        return any(
            (vm := self.vm_map.get(vid)) is not None and vm.pinned_to is not None
            for vid in vm_ids
        )

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
                if not self._rule_has_pinned(rule.vm_ids):
                    min_buckets_needed = -(-N // cap)  # ceil division
                    if len(reachable_buckets) < min_buckets_needed:
                        failed_dims.append({
                            "dimension": dim,
                            "cap_per_bucket": cap,
                            "min_buckets_needed": min_buckets_needed,
                            "reachable_buckets": len(reachable_buckets),
                        })
                    continue
                # Pinned-aware variant: under grandfathered caps a bucket
                # at/over cap is frozen (0 seats for new members), the rest
                # offer cap − pinned seats. Structural infeasibility means
                # the FREE members outnumber the seats.
                pinned_by_bucket: dict[str, int] = {}
                for vm_id in rule.vm_ids:
                    vm = self.vm_map.get(vm_id)
                    if vm is not None and vm.pinned_to in self.bm_map:
                        label = getattr(
                            self.bm_map[vm.pinned_to].topology, dim
                        )
                        pinned_by_bucket[label] = (
                            pinned_by_bucket.get(label, 0) + 1
                        )
                n_free = N - sum(pinned_by_bucket.values())
                seats = sum(
                    max(cap, pinned_by_bucket.get(b, 0))
                    - pinned_by_bucket.get(b, 0)
                    for b in reachable_buckets
                )
                if seats < n_free:
                    failed_dims.append({
                        "dimension": dim,
                        "cap_per_bucket": cap,
                        "free_vms": n_free,
                        "seats_under_grandfathered_caps": seats,
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
            # Rules with pinned members are exempt (mirrors the solver:
            # grandfathered buckets make the counting argument non-binding).
            if self._rule_has_pinned(primary_ids + backup_ids):
                continue
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
            if not self._rule_has_pinned(rule.vm_ids):
                capacity = rule.max_per_bm * len(reachable_bms)
                if capacity < len(rule.vm_ids):
                    infeasible.append({
                        "group_id": rule.group_id,
                        "vm_count": len(rule.vm_ids),
                        "max_per_bm": rule.max_per_bm,
                        "reachable_bms": len(reachable_bms),
                        "slots_available": capacity,
                    })
                continue
            # Pinned-aware variant: per BM the grandfathered cap leaves
            # max(cap, pinned) − pinned seats for new members; free members
            # must fit into the sum of those seats.
            pinned_total = 0
            seats = 0
            for bm_id in reachable_bms:
                p = self._pinned_in_bucket(rule.vm_ids, [bm_id])
                pinned_total += p
                seats += max(rule.max_per_bm, p) - p
            n_free = len(rule.vm_ids) - pinned_total
            if seats < n_free:
                infeasible.append({
                    "group_id": rule.group_id,
                    "vm_count": len(rule.vm_ids),
                    "max_per_bm": rule.max_per_bm,
                    "reachable_bms": len(reachable_bms),
                    "free_vms": n_free,
                    "seats_under_grandfathered_caps": seats,
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
                if vm.pinned_to is not None:
                    # Mirror the main model: a pin is a fact, forced even
                    # under allow_partial_placement.
                    model.add(assign[(vm.id, vm.pinned_to)] == 1)
                elif self.config.allow_partial_placement:
                    model.add(sum(vm_vars) <= 1)
                else:
                    model.add(sum(vm_vars) == 1)

        def add_capacity(model, assign):
            # Shadow C2: iterates request_dims(...) — the IDENTICAL dimension
            # list the real C2 uses — so failing-layer attribution can't
            # diverge from the main model.
            dims = request_dims(self.request)
            for bm in self.request.baremetals:
                avail = bm.available_capacity
                avars = [(vid, assign[(vid, bm.id)]) for vid in self.vm_map
                         if (vid, bm.id) in assign]
                if not avars:
                    continue
                for rdim in dims:
                    usage = sum(res_get(self.vm_map[vid].demand, rdim) * v
                                for vid, v in avars)
                    model.add(usage <= res_get(avail, rdim))

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
                            # Mirror grandfathered caps or the layer
                            # attribution contradicts the main model.
                            cap_b = max(
                                cap, self._pinned_in_bucket(rule.vm_ids, bm_ids)
                            )
                            model.add(sum(vbucket) <= cap_b)

        def add_max_per_bm(model, assign):
            for rule in self.max_per_bm_rules:
                for bm_id in self.bm_map:
                    vbm = [assign[(vid, bm_id)] for vid in rule.vm_ids
                           if (vid, bm_id) in assign]
                    if vbm:
                        cap = max(
                            rule.max_per_bm,
                            self._pinned_in_bucket(rule.vm_ids, [bm_id]),
                        )
                        model.add(sum(vbm) <= cap)

        def add_failover(model, assign):
            for f, primary_ids, backup_ids in self.failover_resolved:
                buckets = self.dim_to_bms.get(f.fault_domain, {})
                for bm_ids in buckets.values():
                    pin = [assign[(vid, bid)] for vid in primary_ids
                           for bid in bm_ids if (vid, bid) in assign]
                    bin_ = [assign[(vid, bid)] for vid in backup_ids
                            for bid in bm_ids if (vid, bid) in assign]
                    if pin or bin_:
                        rhs = max(
                            len(backup_ids),
                            self._pinned_in_bucket(primary_ids, bm_ids)
                            + self._pinned_in_bucket(backup_ids, bm_ids),
                        )
                        model.add(sum(pin) + sum(bin_) <= rhs)

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
