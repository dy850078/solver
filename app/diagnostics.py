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
        ag_to_bms: dict[str, list[str]],
        effective_rules: list[AntiAffinityRule],
        max_per_bm_rules: list[MaxPerBaremetalRule],
        config: SolverConfig,
        num_variables: int,
    ):
        self.request = request
        self.vm_map = vm_map
        self.bm_map = bm_map
        self.ag_to_bms = ag_to_bms
        self.effective_rules = effective_rules
        self.max_per_bm_rules = max_per_bm_rules
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

        # 3. Constraint layer check — which layer first causes INFEASIBLE
        diag["constraint_check"] = self._constraint_layer_check()

        # 4. Summary counts
        diag["counts"] = {
            "vms": len(self.request.vms),
            "bms": len(self.request.baremetals),
            "ags": len(self.ag_to_bms),
            "variables": self.num_variables,
            "rules": len(self.effective_rules),
            "max_per_bm_rules": len(self.max_per_bm_rules),
        }

        return diag

    def _check_anti_affinity_feasibility(self) -> list[dict]:
        infeasible = []
        for rule in self.effective_rules:
            reachable_ags: set[str] = set()
            for vm_id in rule.vm_ids:
                if vm_id in self.vm_map:
                    for bm_id in self._eligible(self.vm_map[vm_id]):
                        if bm_id in self.bm_map:
                            reachable_ags.add(self.bm_map[bm_id].topology.ag)
            min_ags_needed = -(-len(rule.vm_ids) // rule.max_per_ag)  # ceil division
            if len(reachable_ags) < min_ags_needed:
                infeasible.append({
                    "group_id": rule.group_id,
                    "vm_count": len(rule.vm_ids),
                    "max_per_ag": rule.max_per_ag,
                    "min_ags_needed": min_ags_needed,
                    "reachable_ags": len(reachable_ags),
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
            for rule in self.effective_rules:
                for ag, ag_bm_ids in self.ag_to_bms.items():
                    vag = [assign[(vid, bid)] for vid in rule.vm_ids
                           for bid in ag_bm_ids if (vid, bid) in assign]
                    if vag:
                        model.add(sum(vag) <= rule.max_per_ag)

        def add_max_per_bm(model, assign):
            for rule in self.max_per_bm_rules:
                for bm_id in self.bm_map:
                    vbm = [assign[(vid, bm_id)] for vid in rule.vm_ids
                           if (vid, bm_id) in assign]
                    if vbm:
                        model.add(sum(vbm) <= rule.max_per_bm)

        def quick_solve(model) -> str:
            s = cp_model.CpSolver()
            s.parameters.max_time_in_seconds = 5.0
            st = s.solve(model)
            return "OK" if st in (cp_model.OPTIMAL, cp_model.FEASIBLE) else status_name(st)

        layers = [
            ("one_bm_per_vm", [add_one_bm_per_vm]),
            ("capacity", [add_one_bm_per_vm, add_capacity]),
            ("anti_affinity", [add_one_bm_per_vm, add_capacity, add_anti_affinity]),
            ("max_per_bm", [add_one_bm_per_vm, add_capacity, add_anti_affinity, add_max_per_bm]),
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
