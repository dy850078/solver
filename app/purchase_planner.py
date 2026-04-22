"""
Purchase Planner — decides how many Baremetals the PM should buy.

Use case: PM is evaluating a hardware purchase. The machines don't exist in
the Inventory API yet, so we can't run the normal placement flow. Instead the
PM provides a set of `PurchaseCandidate`s (spec + topology + quantity cap) and
total resource requirements, and this planner:

  1. Synthesizes a pool of hypothetical Baremetals (zero `used_capacity`) from
     each candidate.
  2. Feeds them through the existing split + placement pipeline alongside any
     real `existing_baremetals`.
  3. Tells `VMPlacementSolver` to penalize each hypothetical BM that ends up
     `bm_used=1` (weighted by `candidate.cost`) so the solver minimizes the
     purchase.
  4. Adds symmetry breaking across identical hypothetical BMs for speed.
  5. Reads back the solution and reports `recommended_quantity` and per-spec
     average utilization to the PM.

The splitter and solver are reused unchanged; this file is the thin glue.
"""

from __future__ import annotations

import math
import time

from ortools.sat.python import cp_model

from .models import (
    Baremetal,
    CandidatePurchaseDecision,
    PlacementRequest,
    PurchaseCandidate,
    PurchasePlanningRequest,
    PurchasePlanningResult,
    ResourceRequirement,
    Resources,
)
from .solver import RESOURCE_FIELDS, VMPlacementSolver
from .splitter import ResourceSplitter

# PurchaseCandidate.cost is a float; CP-SAT objective terms must be integer.
# Scale by COST_SCALE then round so ratios like 0.5 / 1.0 are preserved.
COST_SCALE = 100


def plan_purchase(request: PurchasePlanningRequest) -> PurchasePlanningResult:
    """Entry point: solve a purchase planning request."""
    start = time.time()

    synthesized, groups_by_candidate = _synthesize_hypothetical_baremetals(
        request.purchase_candidates, request.requirements
    )

    all_baremetals = list(request.existing_baremetals) + synthesized

    if not all_baremetals:
        return PurchasePlanningResult(
            feasible=False,
            solver_status="NO_BAREMETALS: no existing_baremetals and no purchase_candidates produced hypothetical BMs",
            solve_time_seconds=time.time() - start,
        )

    model = cp_model.CpModel()
    splitter = ResourceSplitter(
        model=model,
        requirements=request.requirements,
        baremetals=all_baremetals,
        config=request.config,
    )
    synthetic_vms = splitter.build()

    if not synthetic_vms and not request.vms:
        return PurchasePlanningResult(
            feasible=False,
            solver_status="NO_VMS: requirements produced no synthetic VMs and no explicit vms provided",
            solve_time_seconds=time.time() - start,
        )

    placement_request = PlacementRequest(
        vms=list(request.vms) + synthetic_vms,
        baremetals=all_baremetals,
        anti_affinity_rules=request.anti_affinity_rules,
        config=request.config,
    )
    solver_instance = VMPlacementSolver(
        placement_request, model=model, active_vars=splitter.active_vars,
    )
    solver_instance.splitter_waste_terms = splitter.build_waste_objective_terms()

    solver_instance.purchase_cost_weights = _build_cost_weights(
        request.purchase_candidates, groups_by_candidate
    )
    solver_instance.purchase_symmetry_groups = [
        group for group in groups_by_candidate if len(group) > 1
    ]

    result = solver_instance.solve()

    cp_solver = getattr(solver_instance, "_last_cp_solver", None)
    feasible = result.success or result.solver_status in ("OPTIMAL", "FEASIBLE")

    if feasible and cp_solver is not None:
        split_decisions = splitter.get_split_decisions(cp_solver)
        all_vms = list(request.vms) + synthetic_vms
        purchase_decisions = _build_purchase_decisions(
            request.purchase_candidates,
            groups_by_candidate,
            synthesized,
            result.assignments,
            all_vms,
        )
    else:
        split_decisions = []
        purchase_decisions = []

    return PurchasePlanningResult(
        feasible=feasible,
        purchase_decisions=purchase_decisions,
        split_decisions=split_decisions,
        assignments=result.assignments,
        unplaced_vms=result.unplaced_vms,
        solver_status=result.solver_status,
        solve_time_seconds=time.time() - start,
        diagnostics=result.diagnostics,
    )


def _synthesize_hypothetical_baremetals(
    candidates: list[PurchaseCandidate],
    requirements: list[ResourceRequirement],
) -> tuple[list[Baremetal], list[list[str]]]:
    """
    Expand each PurchaseCandidate into a list of identical hypothetical BMs.

    Returns:
      - synthesized: the flat list of Baremetals to feed the solver
      - groups_by_candidate: parallel to `candidates`; each entry is the list
        of synthesized BM ids for that candidate (used for symmetry breaking
        and utilization reporting).
    """
    synthesized: list[Baremetal] = []
    groups: list[list[str]] = []
    total_demand = _aggregate_demand(requirements)

    for c_idx, candidate in enumerate(candidates):
        quantity = candidate.max_quantity
        if quantity is None:
            quantity = _default_upper_bound(candidate.spec, total_demand)
        if quantity <= 0:
            groups.append([])
            continue

        label_part = candidate.label or f"c{c_idx}"
        group_ids: list[str] = []
        for k in range(quantity):
            bm_id = f"planned-{label_part}-{k}"
            synthesized.append(
                Baremetal(
                    id=bm_id,
                    hostname=bm_id,
                    total_capacity=candidate.spec,
                    topology=candidate.topology_template,
                )
            )
            group_ids.append(bm_id)
        groups.append(group_ids)

    return synthesized, groups


def _aggregate_demand(requirements: list[ResourceRequirement]) -> Resources:
    """Sum total_resources across all requirements for upper-bound estimation."""
    total = Resources()
    for req in requirements:
        total = total + req.total_resources
    return total


def _default_upper_bound(spec: Resources, total_demand: Resources) -> int:
    """
    Upper bound on how many of this spec could be needed to satisfy the
    aggregate demand, across all resource dimensions (ceil division, take max).
    """
    upper = 0
    for field in RESOURCE_FIELDS:
        spec_val = getattr(spec, field)
        demand_val = getattr(total_demand, field)
        if spec_val > 0 and demand_val > 0:
            upper = max(upper, math.ceil(demand_val / spec_val))
    return upper


def _build_cost_weights(
    candidates: list[PurchaseCandidate],
    groups_by_candidate: list[list[str]],
) -> dict[str, int]:
    """
    Map each hypothetical BM id to an integer cost weight.

    `cost=0` would let the solver buy unused machines for free; we floor at 1
    so the objective always prefers buying fewer.
    """
    weights: dict[str, int] = {}
    for candidate, bm_ids in zip(candidates, groups_by_candidate):
        int_cost = max(1, round(candidate.cost * COST_SCALE))
        for bm_id in bm_ids:
            weights[bm_id] = int_cost
    return weights


def _build_purchase_decisions(
    candidates: list[PurchaseCandidate],
    groups_by_candidate: list[list[str]],
    synthesized: list[Baremetal],
    assignments: list,
    all_vms: list,
) -> list[CandidatePurchaseDecision]:
    """
    For each candidate, count BMs that received at least one VM and compute
    average utilization across those BMs (placed demand / total capacity).
    """
    bm_map: dict[str, Baremetal] = {bm.id: bm for bm in synthesized}
    vm_demand: dict[str, Resources] = {vm.id: vm.demand for vm in all_vms}

    # bm_id -> sum of placed VM demand
    usage_per_bm: dict[str, Resources] = {bm.id: Resources() for bm in synthesized}
    for a in assignments:
        bm_id = a.baremetal_id
        if bm_id in usage_per_bm and a.vm_id in vm_demand:
            usage_per_bm[bm_id] = usage_per_bm[bm_id] + vm_demand[a.vm_id]

    decisions: list[CandidatePurchaseDecision] = []
    for candidate, bm_ids in zip(candidates, groups_by_candidate):
        used_in_group = [bm_id for bm_id in bm_ids if _has_any_demand(usage_per_bm.get(bm_id))]
        recommended = len(used_in_group)

        avg_util = _avg_utilization(used_in_group, bm_map, usage_per_bm)

        decisions.append(
            CandidatePurchaseDecision(
                label=candidate.label or "",
                spec=candidate.spec,
                recommended_quantity=recommended,
                used_count=recommended,
                avg_utilization=avg_util,
            )
        )
    return decisions


def _has_any_demand(r: Resources | None) -> bool:
    if r is None:
        return False
    return any(getattr(r, f) > 0 for f in RESOURCE_FIELDS)


def _avg_utilization(
    used_bm_ids: list[str],
    bm_map: dict[str, Baremetal],
    usage_per_bm: dict[str, Resources],
) -> dict[str, float]:
    """
    Average per-field utilization (placed_demand / total_capacity) across the
    listed BMs. Returns 0.0 per field when the group has no used BMs, and
    skips fields with total_capacity == 0 (records 0.0 for those).
    """
    if not used_bm_ids:
        return {field: 0.0 for field in RESOURCE_FIELDS}

    per_field_sum: dict[str, float] = {field: 0.0 for field in RESOURCE_FIELDS}
    per_field_count: dict[str, int] = {field: 0 for field in RESOURCE_FIELDS}

    for bm_id in used_bm_ids:
        bm = bm_map.get(bm_id)
        usage = usage_per_bm.get(bm_id)
        if bm is None or usage is None:
            continue
        for field in RESOURCE_FIELDS:
            total = getattr(bm.total_capacity, field)
            used = getattr(usage, field)
            if total > 0:
                per_field_sum[field] += used / total
                per_field_count[field] += 1

    return {
        field: (per_field_sum[field] / per_field_count[field]) if per_field_count[field] else 0.0
        for field in RESOURCE_FIELDS
    }
