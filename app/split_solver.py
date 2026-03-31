"""
Split-and-Solve orchestrator.

Wires ResourceSplitter and VMPlacementSolver together on a single CP-SAT
CpModel so the split decision and placement are solved jointly.
"""

from __future__ import annotations

import logging
import time

from ortools.sat.python import cp_model

from .models import PlacementRequest, SplitPlacementRequest, SplitPlacementResult
from .splitter import ResourceSplitter
from .solver import VMPlacementSolver

logger = logging.getLogger(__name__)


def solve_split_placement(request: SplitPlacementRequest) -> SplitPlacementResult:
    """
    1. Build a shared CpModel.
    2. ResourceSplitter adds split variables + coverage constraints → synthetic VMs.
    3. Combine explicit + synthetic VMs into a PlacementRequest.
    4. VMPlacementSolver adds placement variables + capacity / anti-affinity constraints
       on the same model.
    5. Inject resource-waste penalty terms into the solver's objective.
    6. Solve once.
    7. Extract split decisions and placement assignments.
    """
    start = time.time()

    model = cp_model.CpModel()

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

    placement_request = PlacementRequest(
        vms=list(request.vms) + synthetic_vms,
        baremetals=request.baremetals,
        anti_affinity_rules=request.anti_affinity_rules,
        config=request.config,
    )

    solver_instance = VMPlacementSolver(
        placement_request,
        model=model,
        active_vars=splitter.active_vars,
    )
    # Inject waste terms; _add_objective reads them in the objective builder
    solver_instance.splitter_waste_terms = splitter.build_waste_objective_terms()

    result = solver_instance.solve()

    if result.success or result.solver_status in ("OPTIMAL", "FEASIBLE"):
        cp_solver = getattr(solver_instance, "_last_cp_solver", None)
        split_decisions = splitter.get_split_decisions(cp_solver) if cp_solver else []
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
