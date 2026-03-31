"""Reference solution for Exercise 5: GPU Affinity Constraint."""

from __future__ import annotations
import time
import logging

from ortools.sat.python import cp_model

from app.models import PlacementRequest, PlacementResult, SolverConfig
from app.solver import VMPlacementSolver

logger = logging.getLogger(__name__)


class GpuAffinitySolver(VMPlacementSolver):

    def __init__(self, request, *, min_gpu_numerator=1, min_gpu_denominator=2, **kwargs):
        super().__init__(request, **kwargs)
        self.min_gpu_numerator = min_gpu_numerator
        self.min_gpu_denominator = min_gpu_denominator

    def _add_gpu_affinity_constraints(self):
        gpu_vm_ids = {vm.id for vm in self.request.vms if vm.demand.gpu_count > 0}

        self._ensure_bm_used_vars()

        for bm in self.request.baremetals:
            if bm.total_capacity.gpu_count <= 0:
                continue

            assigned = [
                (vm_id, self.assign[(vm_id, bm.id)])
                for vm_id in self.vm_map
                if (vm_id, bm.id) in self.assign
            ]
            if not assigned:
                continue

            gpu_on_bm = sum(var for vm_id, var in assigned if vm_id in gpu_vm_ids)
            total_on_bm = sum(var for _, var in assigned)

            self.model.add(
                gpu_on_bm * self.min_gpu_denominator
                >= total_on_bm * self.min_gpu_numerator
            ).only_enforce_if(self.bm_used[bm.id])

    def solve(self) -> PlacementResult:
        start = time.time()

        if self._input_errors:
            for err in self._input_errors:
                logger.error("Input validation failed: %s", err)
            return PlacementResult(
                success=False,
                solver_status="INPUT_ERROR: duplicate baremetals detected",
                solve_time_seconds=time.time() - start,
                unplaced_vms=[vm.id for vm in self.request.vms],
                diagnostics={"input_errors": self._input_errors},
            )

        try:
            self._build_variables()
            self._add_one_bm_per_vm_constraint()
            self._add_capacity_constraints()
            self._add_anti_affinity_constraints()
            self._add_gpu_affinity_constraints()
            self._add_objective()

            solver = cp_model.CpSolver()
            solver.parameters.max_time_in_seconds = self.config.max_solve_time_seconds
            solver.parameters.num_workers = self.config.num_workers

            status = solver.solve(self.model)
            status_name = self._status_name(status)

            if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                self._last_cp_solver = solver
                return self._extract_solution(solver, status_name, time.time() - start)
            else:
                return PlacementResult(
                    success=False,
                    solver_status=status_name,
                    solve_time_seconds=time.time() - start,
                    unplaced_vms=[vm.id for vm in self.request.vms],
                )
        except Exception as e:
            return PlacementResult(
                success=False,
                solver_status=f"ERROR: {e}",
                solve_time_seconds=time.time() - start,
                unplaced_vms=[vm.id for vm in self.request.vms],
            )


def solve_with_gpu_affinity(
    vms, bms, min_gpu_numerator=1, min_gpu_denominator=2, **config_overrides,
) -> PlacementResult:
    cfg = dict(max_solve_time_seconds=10, auto_generate_anti_affinity=False)
    cfg.update(config_overrides)
    request = PlacementRequest(
        vms=vms, baremetals=bms,
        anti_affinity_rules=[],
        config=SolverConfig(**cfg),
    )
    solver = GpuAffinitySolver(
        request,
        min_gpu_numerator=min_gpu_numerator,
        min_gpu_denominator=min_gpu_denominator,
    )
    return solver.solve()
