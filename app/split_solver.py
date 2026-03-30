"""
Split-and-Solve orchestrator.

Combines ResourceSplitter and VMPlacementSolver into a single CP-SAT model
that jointly optimizes requirement splitting and VM placement.
"""

from __future__ import annotations

import logging
import time

from ortools.sat.python import cp_model

from .models import (
    PlacementRequest,
    SplitPlacementRequest,
    SplitPlacementResult,
    SolverConfig,
)
from .splitter import ResourceSplitter
from .solver import VMPlacementSolver

logger = logging.getLogger(__name__)


def solve_split_placement(request: SplitPlacementRequest) -> SplitPlacementResult:
    """
    Orchestrate split + placement in a single CP-SAT model.

    1. Create a shared CpModel
    2. ResourceSplitter builds split variables + constraints → synthetic VMs
    3. Combine explicit VMs + synthetic VMs into a PlacementRequest
    4. VMPlacementSolver builds placement variables + constraints on the same model
    5. Add resource waste objective term
    6. Solve jointly
    7. Extract split decisions + placement assignments
    """
    start = time.time()

    # 1. Shared model
    model = cp_model.CpModel()

    # 2. Build splitter
    splitter = ResourceSplitter(
        model=model,
        requirements=request.requirements,
        baremetals=request.baremetals,
        config=request.config,
    )
    synthetic_vms = splitter.build()

    if not synthetic_vms and not request.vms:
        return SplitPlacementResult(
            success=False,
            solver_status="NO_VMS: no synthetic or explicit VMs to place",
            solve_time_seconds=time.time() - start,
        )

    logger.info(
        "Split phase: %d requirements → %d synthetic VMs + %d explicit VMs",
        len(request.requirements), len(synthetic_vms), len(request.vms),
    )

    # 3. Combine into PlacementRequest
    all_vms = list(request.vms) + synthetic_vms
    placement_request = PlacementRequest(
        vms=all_vms,
        baremetals=request.baremetals,
        anti_affinity_rules=request.anti_affinity_rules,
        config=request.config,
        existing_vms=request.existing_vms,
        topology_rules=request.topology_rules,
    )

    # 4. Build solver on shared model with active vars
    solver_instance = VMPlacementSolver(
        placement_request,
        model=model,
        active_vars=splitter.active_vars,
    )

    # 5. Add resource waste to objective (handled inside solver's _add_objective via
    #    the waste terms from splitter)
    #    We inject waste terms by storing them on the solver instance
    solver_instance._splitter_waste_terms = splitter.build_waste_objective_terms()

    # 6. Solve
    result = solver_instance.solve()

    # 7. Extract split decisions
    if result.success or result.solver_status in ("OPTIMAL", "FEASIBLE"):
        # Need the CpSolver to read variable values — re-solve is wasteful.
        # Instead, extract from the solver that was used internally.
        # The _last_solver is set by _run_solver after successful solve.
        cp_solver = getattr(solver_instance, "_last_cp_solver", None)
        split_decisions = (
            splitter.get_split_decisions(cp_solver) if cp_solver else []
        )
    else:
        split_decisions = []

    return SplitPlacementResult(
        success=result.success,
        assignments=result.assignments,
        split_decisions=split_decisions,
        solver_status=result.solver_status,
        solve_time_seconds=time.time() - start,
        unplaced_vms=result.unplaced_vms,
        diagnostics=result.diagnostics,
    )
