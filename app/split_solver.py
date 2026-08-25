"""
Split-and-Solve orchestrator.

Wires ResourceSplitter and VMPlacementSolver together on a single CP-SAT
CpModel so the split decision and placement are solved jointly.
"""

from __future__ import annotations

import logging
import time

from ortools.sat.python import cp_model

from .models import (
    VM,
    PlacementRequest,
    SplitPlacementRequest,
    SplitPlacementResult,
    config_fingerprint,
)
from .splitter import ResourceSplitter
from .solver import VMPlacementSolver

logger = logging.getLogger(__name__)


def solve_split_placement_with_synthetics(
    request: SplitPlacementRequest,
) -> tuple[SplitPlacementResult, list[VM]]:
    """In-process entry point that also returns the splitter's synthetic VM
    objects (with their concrete demand). The result alone cannot recover
    them: SplitDecision has no per-VM attribution, and re-deriving specs
    from synthetic ids depends on the splitter's private, order-sensitive
    spec filtering. Rollout simulation needs the objects to carry placed
    synthetics forward as pinned VMs. Every return path (early NO_VMS bail
    included) carries the config fingerprint (E0/S4)."""
    result, synthetic_vms = _solve_split_placement(request)
    result.config_fingerprint = config_fingerprint(request.config)
    return result, synthetic_vms


def solve_split_placement(request: SplitPlacementRequest) -> SplitPlacementResult:
    """Public HTTP-facing entry point (wire contract unchanged)."""
    return solve_split_placement_with_synthetics(request)[0]


def _solve_split_placement(
    request: SplitPlacementRequest,
) -> tuple[SplitPlacementResult, list[VM]]:
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
            bm_total_count=len(request.baremetals),
        ), []

    logger.info(
        "Split phase: %d requirements → %d synthetic VMs + %d explicit VMs",
        len(request.requirements), len(synthetic_vms), len(request.vms),
    )

    placement_request = PlacementRequest(
        vms=list(request.vms) + synthetic_vms,
        baremetals=request.baremetals,
        anti_affinity_rules=request.anti_affinity_rules,
        max_per_bm_rules=request.max_per_bm_rules,
        exclusive_bm_rules=request.exclusive_bm_rules,
        failover_rules=request.failover_rules,
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
        bm_used_count=result.bm_used_count,
        bm_total_count=result.bm_total_count,
        diagnostics=result.diagnostics,
    ), synthetic_vms
