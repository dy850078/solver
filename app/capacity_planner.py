"""
Capacity planning — procurement solve (Phase 2, single fab / single period).

Answers: "given this demand and the in-stock baremetals, how many BMs of each
type must we buy — and is the current inventory enough?"

Approach: reuse the existing splitter + solver unchanged. Buyable BMs are
generated as virtual Baremetals (per type × per (bucket, network) cell) and
appended to the pool; the joint splitter+solver places demand onto
(in-stock ∪ committed ∪ buyable), and the objective weights order the
preference: in-stock (free) → committed stock (w_committed_stock) → buy new
(w_procurement). The virtual BMs the solution actually uses ARE the counts.

Covered here:
  - multi-type procurement, per-bucket max_bm slot caps enforced across types
    and committed stock via solver.bm_group_caps (缺口 2 / 決議 #29)
  - BGP network scoping: (bucket, network) cells; a requirement's network
    filters both the in-stock backfill and virtual-BM candidates (缺口 3g)
  - committed stock as a zero-ish-cost tier, bucketed or floating (缺口 3h)
  - balance objective via config.w_procurement_balance (決議 #11)
  - `space` shortfall detection (capped vs uncapped comparison, 決議 #31)
  - health gauges: nominal available, remaining_node_slots (reference spec),
    stranded capacity (min useful spec), balance_after (缺口 3c)
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
    type_ids = {t.type_id for t in request.procurement_types}
    bad_refs = [c.type_id for c in request.committed_stock if c.type_id not in type_ids]
    if bad_refs:
        return ProcurementResult(
            success=False,
            solver_status=(
                f"INPUT_ERROR: committed_stock references unknown type(s) {bad_refs}"
            ),
            solve_time_seconds=time.time() - start,
        )

    # Pass 1: honor the per-bucket max_bm slot caps.
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

    def __init__(self, result, buyable_type_of, committed_type_of,
                 vm_demand, virtual_bms, splitter, cp_solver):
        self.result = result
        self.buyable_type_of = buyable_type_of      # buyable bm_id -> type_id
        self.committed_type_of = committed_type_of  # committed bm_id -> type_id
        self.vm_demand = vm_demand                  # vm_id -> Resources
        self.virtual_bms = virtual_bms              # bm_id -> Baremetal (buy+own)
        self.splitter = splitter
        self.cp_solver = cp_solver


def _solve_once(request: ProcurementRequest, *, use_caps: bool) -> _Pass:
    config = request.config
    dim = config.procurement_spread_dimension

    caps = request.procurement_caps if use_caps else []
    cells = _derive_cells(request, dim)
    type_by_id = {t.type_id: t for t in request.procurement_types}
    worst_case = _worst_case_counts(request)

    # Slot-generation bound per cell: the tightest cap that names the cell
    # (bucket-wide "" caps included). Enforcement is via group caps below;
    # this only limits how many virtual BMs we bother creating.
    def cell_gen_bound(bucket: str, network: str) -> int | None:
        bounds = [
            c.max_bm for c in caps
            if c.bucket == bucket and c.network in ("", network)
        ]
        return min(bounds) if bounds else None

    virtual_bms: dict[str, Baremetal] = {}
    buyable_type_of: dict[str, str] = {}
    committed_type_of: dict[str, str] = {}
    # (bucket, network) -> ids of virtual BMs occupying slots in that cell
    cell_members: dict[tuple[str, str], list[str]] = {}

    def add_virtual(bm_id: str, capacity: Resources, bucket: str,
                    network: str, rep: Topology) -> None:
        virtual_bms[bm_id] = Baremetal(
            id=bm_id,
            total_capacity=capacity,
            used_capacity=Resources(),
            topology=rep.model_copy(update={dim: bucket}),
            network=network,
        )
        cell_members.setdefault((bucket, network), []).append(bm_id)

    # Committed stock first (it occupies slots and reduces what's left to buy).
    pool_groups: list[tuple[set[str], int]] = []
    for idx, cs in enumerate(request.committed_stock):
        if cs.count <= 0:
            continue
        capacity = type_by_id[cs.type_id].capacity
        if cs.bucket is not None:
            rep = cells.get((cs.bucket, cs.network), Topology(**{dim: cs.bucket}))
            for k in range(cs.count):
                bm_id = f"own{idx}-{cs.type_id}-{cs.bucket}|{cs.network}-{k}"
                add_virtual(bm_id, capacity, cs.bucket, cs.network, rep)
                committed_type_of[bm_id] = cs.type_id
        else:
            # Floating: copies in every network-compatible cell; a pool-wide
            # cardinality cap keeps total usage within the owned count.
            pool: set[str] = set()
            for (bucket, network), rep in cells.items():
                if cs.network and network != cs.network:
                    continue
                for k in range(cs.count):
                    bm_id = f"own{idx}-{cs.type_id}-{bucket}|{network}-{k}"
                    add_virtual(bm_id, capacity, bucket, network, rep)
                    committed_type_of[bm_id] = cs.type_id
                    pool.add(bm_id)
            if pool:
                pool_groups.append((pool, cs.count))

    for bt in request.procurement_types:
        need = worst_case.get(bt.type_id, 0)
        if need <= 0:
            continue
        for (bucket, network), rep in cells.items():
            slots = need
            bound = cell_gen_bound(bucket, network)
            if bound is not None:
                slots = min(slots, bound)
            for k in range(slots):
                bm_id = f"buy-{bt.type_id}-{bucket}|{network}-{k}"
                add_virtual(bm_id, bt.capacity, bucket, network, rep)
                buyable_type_of[bm_id] = bt.type_id

    # Slot caps enforced across types AND committed stock: each cap bounds how
    # many virtual BMs (machines added to the bucket) may actually be used.
    slot_groups: list[tuple[set[str], int]] = []
    for c in caps:
        members: set[str] = set()
        for (bucket, network), ids in cell_members.items():
            if bucket == c.bucket and c.network in ("", network):
                members.update(ids)
        if members:
            slot_groups.append((members, c.max_bm))

    all_bms = list(request.in_stock) + list(virtual_bms.values())

    # Candidate scoping: a requirement reaches network-matching in-stock BMs
    # (unless the caller already filtered via candidate_baremetals) plus
    # network-matching virtual BMs. network == "" on the requirement means no
    # restriction (缺口 3g).
    def net_ok(req_net: str, item_net: str) -> bool:
        return req_net == "" or item_net == req_net

    reqs = []
    for r in request.requirements:
        in_stock_ids = r.candidate_baremetals or [
            bm.id for bm in request.in_stock if net_ok(r.network, bm.network)
        ]
        virtual_ids = [
            bm_id for bm_id, bm in virtual_bms.items() if net_ok(r.network, bm.network)
        ]
        reqs.append(r.model_copy(
            update={"candidate_baremetals": in_stock_ids + virtual_ids}
        ))
    all_in_stock_ids = [bm.id for bm in request.in_stock]
    vms = [
        vm.model_copy(update={
            "candidate_baremetals":
                (vm.candidate_baremetals or all_in_stock_ids) + list(virtual_bms)
        })
        for vm in request.vms
    ]

    model = cp_model.CpModel()
    splitter = ResourceSplitter(model, reqs, all_bms, config)
    synthetic_vms = splitter.build()

    placement_request = PlacementRequest(
        vms=list(vms) + synthetic_vms,
        baremetals=all_bms,
        anti_affinity_rules=request.anti_affinity_rules,
        max_per_bm_rules=request.max_per_bm_rules,
        failover_rules=request.failover_rules,
        config=config,
    )
    solver = VMPlacementSolver(placement_request, model=model,
                              active_vars=splitter.active_vars)
    solver.splitter_waste_terms = splitter.build_waste_objective_terms()
    solver.procurement_bm_ids = set(buyable_type_of)
    solver.committed_bm_ids = set(committed_type_of)
    solver.bm_group_caps = slot_groups + pool_groups

    result = solver.solve()
    cp_solver = getattr(solver, "_last_cp_solver", None)
    vm_demand = {vm.id: vm.demand for vm in placement_request.vms}
    return _Pass(result, buyable_type_of, committed_type_of,
                 vm_demand, virtual_bms, splitter, cp_solver)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _derive_cells(request: ProcurementRequest,
                  dim: str) -> dict[tuple[str, str], Topology]:
    """
    Virtual BMs land in (bucket, network) cells — the capacity-planning unit
    (決議 #37). Cells come from the in-stock topology (adding machines next to
    existing ones) plus any cell named by a cap or a bucketed committed-stock
    entry. Each cell keeps a representative topology so anti-affinity on other
    dimensions still resolves.
    """
    cells: dict[tuple[str, str], Topology] = {}
    for bm in request.in_stock:
        cells.setdefault((getattr(bm.topology, dim), bm.network), bm.topology)
    for c in request.procurement_caps:
        cells.setdefault((c.bucket, c.network), Topology(**{dim: c.bucket}))
    for cs in request.committed_stock:
        if cs.bucket is not None:
            cells.setdefault((cs.bucket, cs.network), Topology(**{dim: cs.bucket}))
    if not cells:
        cells[("", "")] = Topology()
    return cells


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


def _fits_count(remaining: Resources, spec: Resources) -> int:
    """How many `spec` VMs fit in `remaining` (per-BM, per-dimension floor)."""
    counts = [
        getattr(remaining, f) // getattr(spec, f)
        for f in RESOURCE_FIELDS
        if getattr(spec, f) > 0
    ]
    return min(counts) if counts else 0


def _to_result(request: ProcurementRequest, p: _Pass,
               *, success: bool, shortfall_cause: str, start: float) -> ProcurementResult:
    r = p.result
    used_buy_ids = {
        a.baremetal_id for a in r.assignments if a.baremetal_id in p.buyable_type_of
    }
    used_own_ids = {
        a.baremetal_id for a in r.assignments if a.baremetal_id in p.committed_type_of
    }
    procurement = [
        ProcurementDecision(type_id=t, count=c)
        for t, c in sorted(Counter(p.buyable_type_of[b] for b in used_buy_ids).items())
    ]
    committed_used = [
        ProcurementDecision(type_id=t, count=c)
        for t, c in sorted(Counter(p.committed_type_of[b] for b in used_own_ids).items())
    ]

    in_stock_ids = {bm.id for bm in request.in_stock}
    in_stock_used = len({
        a.baremetal_id for a in r.assignments if a.baremetal_id in in_stock_ids
    })

    split_decisions = (
        p.splitter.get_split_decisions(p.cp_solver) if p.cp_solver else []
    )

    # Health gauges over the post-placement state: all in-stock BMs plus the
    # virtual BMs actually used (an unused buyable BM doesn't exist).
    placed: dict[str, Resources] = {}
    for a in r.assignments:
        demand = p.vm_demand.get(a.vm_id)
        if demand is not None:
            placed[a.baremetal_id] = placed.get(a.baremetal_id, Resources()) + demand

    config = request.config
    dim = config.procurement_spread_dimension
    nominal = Resources()
    stranded = Resources()
    slots = 0
    balance_after: dict[str, int] = {}
    post_bms = list(request.in_stock) + [
        p.virtual_bms[bid] for bid in (used_buy_ids | used_own_ids)
    ]
    for bm in post_bms:
        remaining = bm.available_capacity - placed.get(bm.id, Resources())
        nominal = nominal + remaining
        bucket = getattr(bm.topology, dim)
        balance_after[bucket] = balance_after.get(bucket, 0) + remaining.cpu_cores
        if config.reference_vm_spec is not None:
            slots += _fits_count(remaining, config.reference_vm_spec)
        if (config.min_useful_spec is not None
                and _fits_count(remaining, config.min_useful_spec) == 0):
            stranded = stranded + remaining

    return ProcurementResult(
        success=success,
        procurement=procurement,
        committed_used=committed_used,
        split_decisions=split_decisions,
        assignments=r.assignments,
        shortfall_cause=shortfall_cause,
        solver_status=r.solver_status,
        solve_time_seconds=time.time() - start,
        in_stock_bm_used=in_stock_used,
        procured_bm_total=len(used_buy_ids),
        committed_bm_used=len(used_own_ids),
        nominal_available=nominal,
        remaining_node_slots=slots if config.reference_vm_spec is not None else None,
        stranded_available=stranded if config.min_useful_spec is not None else None,
        balance_after=balance_after,
        diagnostics=r.diagnostics,
    )
