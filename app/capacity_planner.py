"""
Capacity planning — procurement solve (Phase 2, single fab / single period).

Answers: "given this demand and the in-stock baremetals, how many BMs of each
type must we buy — and is the current inventory enough?"

Approach: reuse the existing splitter + solver unchanged. Buyable BMs are
generated as virtual Baremetals (per type × per spread bucket) and appended to
the pool; the joint splitter+solver places demand onto (in-stock ∪ buyable),
and config.w_procurement makes using a buyable BM expensive so in-stock is
filled first and buying is minimized. The number of buyable BMs the solution
actually uses IS the procurement count.

Scope of this increment:
  - multi-type procurement, per-bucket max_bm caps (缺口 2)
  - prefer in-stock, minimize buy count
  - `space` shortfall detection (capped vs uncapped comparison, 決議 #31)
Deferred to follow-ups: balance objective (w_procurement_balance), committed
stock (缺口 3h), BGP network scoping (缺口 3g), placeable/fragmentation report
metrics (缺口 3c).
"""

from __future__ import annotations

import logging
import math
import time
from collections import Counter

from ortools.sat.python import cp_model

from .models import (
    Baremetal,
    PlacementRequest,
    ProcurementDecision,
    ProcurementRequest,
    ProcurementResult,
    Resources,
    Topology,
)
from .splitter import RESOURCE_FIELDS, ResourceSplitter
from .solver import VMPlacementSolver

logger = logging.getLogger(__name__)


def solve_capacity_plan(request: ProcurementRequest) -> ProcurementResult:
    start = time.time()

    if not request.procurement_types:
        return ProcurementResult(
            success=False,
            solver_status="INPUT_ERROR: no procurement_types provided",
            solve_time_seconds=time.time() - start,
        )

    # Pass 1: honor the per-bucket max_bm caps.
    capped = _solve_once(request, use_caps=True)
    if capped.result.success:
        return _to_result(request, capped, success=True,
                          shortfall_cause="none", start=start)

    # Pass 2 (cause classification): if caps were limiting, re-solve without
    # them. If that succeeds, the caps (physical slots) were the blocker.
    if request.procurement_caps:
        uncapped = _solve_once(request, use_caps=False)
        if uncapped.result.success:
            # Demand can't fit within physical slots → not a success. Report
            # what *would* be needed if the slots existed, tagged `space`.
            return _to_result(request, uncapped, success=False,
                              shortfall_cause="space", start=start)
        return _to_result(request, uncapped, success=False,
                          shortfall_cause=_classify(uncapped.result), start=start)

    return _to_result(request, capped, success=False,
                      shortfall_cause=_classify(capped.result), start=start)


# ---------------------------------------------------------------------------
# One solve pass
# ---------------------------------------------------------------------------

class _Pass:
    """Bundle of one solve pass's artifacts."""
    def __init__(self, result, buyable_type_of, splitter, cp_solver):
        self.result = result
        self.buyable_type_of = buyable_type_of      # buyable bm_id -> type_id
        self.splitter = splitter
        self.cp_solver = cp_solver


def _solve_once(request: ProcurementRequest, *, use_caps: bool) -> _Pass:
    dim = request.config.procurement_spread_dimension
    cap_lookup = (
        {c.bucket: c.max_bm for c in request.procurement_caps} if use_caps else {}
    )

    buckets = _derive_buckets(request, dim, cap_lookup)
    worst_case = _worst_case_counts(request)

    buyable: list[Baremetal] = []
    buyable_type_of: dict[str, str] = {}
    for bt in request.procurement_types:
        need = worst_case.get(bt.type_id, 0)
        if need <= 0:
            continue
        for bucket, topo in buckets.items():
            slots = need
            if bucket in cap_lookup:
                slots = min(slots, cap_lookup[bucket])
            for k in range(slots):
                bm_id = f"buy-{bt.type_id}-{bucket}-{k}"
                buyable.append(Baremetal(
                    id=bm_id,
                    total_capacity=bt.capacity,
                    used_capacity=Resources(),
                    topology=_topo_in_bucket(topo, dim, bucket),
                ))
                buyable_type_of[bm_id] = bt.type_id

    all_bms = list(request.in_stock) + buyable
    all_bm_ids = [bm.id for bm in all_bms]
    buyable_ids = set(buyable_type_of)

    # Requirements/VMs must be able to reach in-stock AND buyable BMs.
    in_stock_ids = [bm.id for bm in request.in_stock]
    reqs = [
        r.model_copy(update={
            "candidate_baremetals": (r.candidate_baremetals or in_stock_ids)
            + list(buyable_ids)
        })
        for r in request.requirements
    ]
    vms = [
        vm.model_copy(update={
            "candidate_baremetals": (vm.candidate_baremetals or in_stock_ids)
            + list(buyable_ids)
        })
        for vm in request.vms
    ]

    model = cp_model.CpModel()
    splitter = ResourceSplitter(model, reqs, all_bms, request.config)
    synthetic_vms = splitter.build()

    placement_request = PlacementRequest(
        vms=list(vms) + synthetic_vms,
        baremetals=all_bms,
        anti_affinity_rules=request.anti_affinity_rules,
        max_per_bm_rules=request.max_per_bm_rules,
        failover_rules=request.failover_rules,
        config=request.config,
    )
    solver = VMPlacementSolver(placement_request, model=model,
                              active_vars=splitter.active_vars)
    solver.splitter_waste_terms = splitter.build_waste_objective_terms()
    solver.procurement_bm_ids = buyable_ids

    result = solver.solve()
    cp_solver = getattr(solver, "_last_cp_solver", None)
    return _Pass(result, buyable_type_of, splitter, cp_solver)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _derive_buckets(request: ProcurementRequest, dim: str,
                    cap_lookup: dict[str, int]) -> dict[str, Topology]:
    """
    Buyable BMs land in spread buckets. Buckets come from the in-stock topology
    (adding machines to existing AGs/DCs) plus any bucket named in the caps.
    Each bucket keeps a representative topology so anti-affinity on other
    dimensions still resolves.
    """
    buckets: dict[str, Topology] = {}
    for bm in request.in_stock:
        b = getattr(bm.topology, dim)
        buckets.setdefault(b, bm.topology)
    for bucket in cap_lookup:
        if bucket not in buckets:
            buckets[bucket] = Topology(**{dim: bucket})
    if not buckets:
        # No in-stock and no caps name a bucket: fall back to a single bucket.
        buckets[""] = Topology(**{dim: ""})
    return buckets


def _topo_in_bucket(rep: Topology, dim: str, bucket: str) -> Topology:
    """Representative topology with the spread dimension pinned to `bucket`."""
    return rep.model_copy(update={dim: bucket})


def _worst_case_counts(request: ProcurementRequest) -> dict[str, int]:
    """
    Upper bound on how many of each type could be needed = enough of that type
    alone to cover all demand. Bounds the number of buyable slots generated.
    """
    total = Resources()
    for r in request.requirements:
        total = total + r.total_resources
    for vm in request.vms:
        total = total + vm.demand

    counts: dict[str, int] = {}
    for bt in request.procurement_types:
        need = 0
        for field in RESOURCE_FIELDS:
            demand = getattr(total, field)
            cap = getattr(bt.capacity, field)
            if demand > 0 and cap > 0:
                need = max(need, math.ceil(demand / cap))
        counts[bt.type_id] = need
    return counts


def _classify(result) -> str:
    """Best-effort map of a failed placement to a shortfall cause."""
    check = (result.diagnostics or {}).get("constraint_check", {})
    failed_at = check.get("failed_at")
    if failed_at in ("anti_affinity", "failover", "max_per_bm"):
        return "anti_affinity"
    return "capacity"


def _to_result(request: ProcurementRequest, p: _Pass,
               *, success: bool, shortfall_cause: str, start: float) -> ProcurementResult:
    r = p.result
    used_buy_ids = {
        a.baremetal_id for a in r.assignments if a.baremetal_id in p.buyable_type_of
    }
    counts = Counter(p.buyable_type_of[bid] for bid in used_buy_ids)
    procurement = [
        ProcurementDecision(type_id=t, count=c) for t, c in sorted(counts.items())
    ]

    in_stock_ids = {bm.id for bm in request.in_stock}
    in_stock_used = len({
        a.baremetal_id for a in r.assignments if a.baremetal_id in in_stock_ids
    })

    split_decisions = (
        p.splitter.get_split_decisions(p.cp_solver) if p.cp_solver else []
    )

    return ProcurementResult(
        success=success,
        procurement=procurement,
        split_decisions=split_decisions,
        assignments=r.assignments,
        shortfall_cause=shortfall_cause,
        solver_status=r.solver_status,
        solve_time_seconds=time.time() - start,
        in_stock_bm_used=in_stock_used,
        procured_bm_total=len(used_buy_ids),
        diagnostics=r.diagnostics,
    )
