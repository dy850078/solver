"""Reference solution for Exercise 7: Global VM Limit."""

from __future__ import annotations
import time

from ortools.sat.python import cp_model

from app.models import (
    PlacementRequest, SplitPlacementResult,
    SolverConfig, ResourceRequirement, Baremetal, VM, AntiAffinityRule,
    Resources,
)
from app.splitter import ResourceSplitter
from app.solver import VMPlacementSolver


def solve_split_with_global_limit(
    requirements,
    bms,
    global_max_vms=None,
    global_min_vms=None,
    explicit_vms=None,
    rules=None,
    **config_overrides,
) -> SplitPlacementResult:
    start = time.time()
    explicit_vms = explicit_vms or []
    rules = rules or []

    cfg = dict(max_solve_time_seconds=10, auto_generate_anti_affinity=False)
    cfg.update(config_overrides)
    config = SolverConfig(**cfg)

    reqs = requirements if isinstance(requirements, list) else [requirements]

    # 1. Shared model
    model = cp_model.CpModel()

    # 2. Splitter builds split variables + coverage constraints
    splitter = ResourceSplitter(
        model=model,
        requirements=reqs,
        baremetals=bms,
        config=config,
    )
    synthetic_vms = splitter.build()

    if not synthetic_vms and not explicit_vms:
        return SplitPlacementResult(
            success=False,
            solver_status="NO_VMS",
            solve_time_seconds=time.time() - start,
        )

    # 3. Global VM count constraints (AFTER splitter.build(), BEFORE solver)
    all_counts = list(splitter.count_vars.values())
    n_explicit = len(explicit_vms)

    if global_max_vms is not None and all_counts:
        synthetic_budget = global_max_vms - n_explicit
        model.add(sum(all_counts) <= max(0, synthetic_budget))

    if global_min_vms is not None and all_counts:
        synthetic_min = global_min_vms - n_explicit
        model.add(sum(all_counts) >= max(0, synthetic_min))

    # 4. Combine explicit + synthetic VMs
    placement_request = PlacementRequest(
        vms=list(explicit_vms) + synthetic_vms,
        baremetals=bms,
        anti_affinity_rules=rules,
        config=config,
    )

    # 5. Solver on the same shared model
    solver_instance = VMPlacementSolver(
        placement_request,
        model=model,
        active_vars=splitter.active_vars,
    )
    solver_instance.splitter_waste_terms = splitter.build_waste_objective_terms()

    result = solver_instance.solve()

    # 6. Extract split decisions
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
