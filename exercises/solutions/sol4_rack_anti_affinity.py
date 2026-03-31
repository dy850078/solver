"""Reference solution for Exercise 4: Rack-Level Anti-Affinity."""

from __future__ import annotations
from collections import defaultdict

from pydantic import BaseModel

from app.models import (
    PlacementRequest, PlacementResult, SolverConfig,
    AntiAffinityRule,
)
from app.solver import VMPlacementSolver


class RackAntiAffinityRule(BaseModel):
    group_id: str
    vm_ids: list[str]
    max_per_rack: int = 2


class RackAwareSolver(VMPlacementSolver):

    def __init__(self, request, *, rack_rules=None, **kwargs):
        super().__init__(request, **kwargs)
        self.rack_rules = rack_rules or []

    def _add_anti_affinity_constraints(self):
        # AG-level (inherited)
        super()._add_anti_affinity_constraints()

        # Rack-level: group BMs by rack
        rack_to_bms: dict[str, list[str]] = defaultdict(list)
        for bm in self.request.baremetals:
            rack_to_bms[bm.topology.rack].append(bm.id)

        for rule in self.rack_rules:
            for rack, rack_bm_ids in rack_to_bms.items():
                vars_in_rack = [
                    self.assign[(vm_id, bm_id)]
                    for vm_id in rule.vm_ids
                    for bm_id in rack_bm_ids
                    if (vm_id, bm_id) in self.assign
                ]
                if vars_in_rack:
                    self.model.add(sum(vars_in_rack) <= rule.max_per_rack)


def solve_with_rack_anti_affinity(
    vms, bms, rack_rules=None, ag_rules=None, **config_overrides,
) -> PlacementResult:
    cfg = dict(max_solve_time_seconds=10, auto_generate_anti_affinity=False)
    cfg.update(config_overrides)
    request = PlacementRequest(
        vms=vms, baremetals=bms,
        anti_affinity_rules=ag_rules or [],
        config=SolverConfig(**cfg),
    )
    solver = RackAwareSolver(request, rack_rules=rack_rules or [])
    return solver.solve()
