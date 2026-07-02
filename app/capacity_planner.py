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
import time
from collections import Counter

from ortools.sat.python import cp_model

from .models import (
    Baremetal,
    PlacementRequest,
    PlacementResult,
    ProcurementDecision,
    ProcurementRequest,
    ProcurementResult,
    Resources,
    Topology,
)
from .splitter import RESOURCE_FIELDS, ResourceSplitter, spec_count_upper_bound
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

    # Requirements the splitter could not represent (no usable spec/candidate,
    # collapsed count bounds) must fail loudly — silently succeeding would
    # report "inventory sufficient" for demand that was never placed.
    if capped.dropped:
        detail = "; ".join(
            f"requirement {d['requirement_index']} "
            f"(cluster={d['cluster_id']!r}, role={d['node_role']}): {d['reason']}"
            for d in capped.dropped
        )
        return ProcurementResult(
            success=False,
            solver_status=f"INPUT_ERROR: unsatisfiable requirement(s) — {detail}",
            solve_time_seconds=time.time() - start,
        )

    if capped.result.success:
        return _to_result(request, capped, success=True,
                          shortfall_cause="none", start=start)

    # Only a proven INFEASIBLE can be attributed to a cause. UNKNOWN (time
    # limit) or other statuses must not be classified — re-solving without
    # caps would misreport a merely-unsolved model as a `space` shortfall.
    if capped.result.solver_status != "INFEASIBLE":
        return _to_result(request, capped, success=False,
                          shortfall_cause="unknown", start=start)

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
                 vm_demand, virtual_bms, splitter, cp_solver, dropped):
        self.result = result
        self.buyable_type_of = buyable_type_of      # buyable bm_id -> type_id
        self.committed_type_of = committed_type_of  # committed bm_id -> type_id
        self.vm_demand = vm_demand                  # vm_id -> Resources
        self.virtual_bms = virtual_bms              # bm_id -> Baremetal (buy+own)
        self.splitter = splitter
        self.cp_solver = cp_solver
        self.dropped = dropped                      # splitter.dropped_requirements


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
        # Each virtual BM gets a unique synthetic rack: a newly purchased
        # machine can always be racked apart (rack placement is the DC
        # hardware team's call, 決議 #29), so rack-level anti-affinity must
        # not treat bought machines as colocated with the cell representative.
        # Dimensions above rack inherit the representative (conservative).
        updates: dict = {dim: bucket}
        if dim != "rack":
            updates["rack"] = f"vrack-{bm_id}"
        virtual_bms[bm_id] = Baremetal(
            id=bm_id,
            total_capacity=capacity,
            used_capacity=Resources(),
            topology=rep.model_copy(update=updates),
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
            # cardinality cap keeps total usage within the owned count. Per
            # cell, never more copies than the cell's slot cap allows.
            pool: set[str] = set()
            for (bucket, network), rep in cells.items():
                if cs.network and network != cs.network:
                    continue
                copies = cs.count
                bound = cell_gen_bound(bucket, network)
                if bound is not None:
                    copies = min(copies, bound)
                for k in range(copies):
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
    # restriction (缺口 3g). Candidate lists are cached per distinct network so
    # they aren't rebuilt per requirement.
    def net_ok(req_net: str, item_net: str) -> bool:
        return req_net == "" or item_net == req_net

    in_stock_ids_by_net: dict[str, list[str]] = {}
    virtual_ids_by_net: dict[str, list[str]] = {}

    def candidates_for(net: str) -> tuple[list[str], list[str]]:
        if net not in virtual_ids_by_net:
            in_stock_ids_by_net[net] = [
                bm.id for bm in request.in_stock if net_ok(net, bm.network)
            ]
            virtual_ids_by_net[net] = [
                bm_id for bm_id, bm in virtual_bms.items()
                if net_ok(net, bm.network)
            ]
        return in_stock_ids_by_net[net], virtual_ids_by_net[net]

    reqs = []
    for r in request.requirements:
        in_stock_ids, virtual_ids = candidates_for(r.network)
        reqs.append(r.model_copy(update={
            "candidate_baremetals":
                (r.candidate_baremetals or in_stock_ids) + virtual_ids
        }))

    # Explicit VMs pass through with their candidate lists untouched: the
    # scheduler's step-3 filter is authoritative, so an explicit VM never
    # lands on a virtual (buyable/committed) BM and an empty candidate list
    # surfaces as the solver's documented INPUT_ERROR. Demand that should
    # drive procurement belongs in `requirements`.
    vms = list(request.vms)

    model = cp_model.CpModel()
    splitter = ResourceSplitter(model, reqs, all_bms, config)
    synthetic_vms = splitter.build()

    if splitter.dropped_requirements:
        # Unrepresentable demand — don't waste a solve (and its failure
        # diagnostics); the caller turns this into an INPUT_ERROR.
        result = PlacementResult(
            success=False,
            solver_status="INPUT_ERROR: unsatisfiable requirement(s)",
            bm_total_count=len(all_bms),
        )
        return _Pass(result, buyable_type_of, committed_type_of,
                     {}, virtual_bms, splitter, None,
                     splitter.dropped_requirements)

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
                 vm_demand, virtual_bms, splitter, cp_solver,
                 splitter.dropped_requirements)


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
    Sound upper bound on how many BMs of each type could be needed if that
    type alone hosted all synthetic demand.

    Per requirement and spec: the splitter may create up to
    spec_count_upper_bound(...) VMs of that spec (which already folds in the
    pod-node floor and min/max_total_vms), and one type-t BM hosts at most
    _fits_count(type, spec) of them — further tightened by any max_per_bm
    rule or rack-spread anti-affinity cap matching the requirement's group
    (each bought BM occupies its own synthetic rack, so a per-rack cap acts
    as a per-BM cap on bought machines). Summing ceil(upper / per_bm) per
    spec is sound because each spec's count var is bounded by its upper.

    A naive ceil(total_demand / type_capacity) is NOT sound: it ignores the
    floors and per-BM packing (e.g. a 64c BM fits only one 40c VM), which
    under-generates slots and turns feasible plans into false shortfalls.

    Explicit VMs contribute nothing: their candidate lists are authoritative,
    so they never land on virtual BMs.
    """
    mpn = request.config.max_pods_per_node
    counts: dict[str, int] = {}
    for bt in request.procurement_types:
        need = 0
        for r in request.requirements:
            specs = r.vm_specs if r.vm_specs is not None else request.config.vm_specs
            group_cap = _group_per_bm_cap(request, r)
            for spec in specs or []:
                per_bm = _fits_count(bt.capacity, spec)
                if per_bm <= 0:
                    continue
                if group_cap is not None:
                    per_bm = min(per_bm, group_cap)
                upper = spec_count_upper_bound(r, spec, mpn)
                need += -(-upper // per_bm)  # ceil division
        counts[bt.type_id] = need
    return counts


def _group_per_bm_cap(request: ProcurementRequest,
                      req) -> int | None:
    """
    Tightest per-BM VM cap applying to this requirement's group: explicit or
    auto-generated max_per_bm rules, plus rack-spread anti-affinity caps
    (bought BMs each live in their own synthetic rack, so a per-rack cap is a
    per-BM cap for them; a rack rule without cap_per_bucket auto-balances to
    ~1 per rack, so 1 is the sound assumption).
    """
    caps: list[int] = []
    cfg = request.config
    if cfg.auto_generate_max_per_bm and (cfg.default_max_per_bm or 0) > 0:
        caps.append(cfg.default_max_per_bm)
    for rule in request.max_per_bm_rules:
        if _selector_matches_req(rule.selector, req):
            caps.append(rule.max_per_bm)
    for aa_rule in request.anti_affinity_rules:
        if "rack" in aa_rule.spread_on and _selector_matches_req(aa_rule.selector, req):
            caps.append((aa_rule.cap_per_bucket or {}).get("rack", 1))
    return min(caps) if caps else None


def _selector_matches_req(selector, req) -> bool:
    """GroupSelector match against a requirement's (cluster, ip_type, role)."""
    if selector is None:
        return False
    return (
        (selector.cluster_id is None or selector.cluster_id == req.cluster_id)
        and (selector.ip_type is None or selector.ip_type == req.ip_type)
        and (selector.node_role is None or selector.node_role == req.node_role)
    )


def _classify(result) -> str:
    """
    Map a proven-INFEASIBLE placement to a shortfall cause. Structural checks
    from diagnostics come first — they are computed directly from rules and
    reachable buckets. The layer check's failed_at is only a fallback: its
    throwaway model forces every worst-case synthetic slot to be placed (it
    doesn't know splitter active_vars), which biases it toward 'capacity'.
    """
    diag = result.diagnostics or {}
    structural = (
        "infeasible_anti_affinity_rules",
        "infeasible_max_per_bm_rules",
        "infeasible_failover_rules",
    )
    if any(diag.get(k) for k in structural):
        return "anti_affinity"
    check = diag.get("constraint_check", {})
    if check.get("failed_at") in ("anti_affinity", "failover", "max_per_bm"):
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
