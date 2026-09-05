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
    BucketMonthCell,
    BudgetRow,
    CapacityPlanRequest,
    CapacityReport,
    CommittedStock,
    PeriodFabReport,
    PlacementRequest,
    PlacementResult,
    ProcurementDecision,
    DemandCoverage,
    ProcurementRequest,
    ProcurementResult,
    RequirementCoverage,
    ResourceRequirement,
    Resources,
    ShortfallDetail,
    Topology,
    config_fingerprint,
    res_get,
    resource_dims,
)
from .splitter import (
    ResourceSplitter,
    has_demand,
    spec_count_upper_bound,
)
from .solver import VMPlacementSolver

logger = logging.getLogger(__name__)


def solve_capacity_plan(request: ProcurementRequest) -> ProcurementResult:
    """Public entry point: every return path (INPUT_ERROR bails included)
    carries the config fingerprint (E0/S4)."""
    result = _solve_capacity_plan(request)
    result.config_fingerprint = config_fingerprint(request.config)
    return result


def _solve_capacity_plan(request: ProcurementRequest) -> ProcurementResult:
    start = time.time()

    # Empty procurement_types is a valid request ("can this fit in-stock
    # without buying?") — nothing is generated to buy and any residual shows
    # up as a classified shortfall. References into the type catalog, however,
    # must resolve: a typo'd type id would otherwise silently vanish and
    # masquerade as a capacity shortfall.
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
    bad_allowed = sorted({
        t for r in request.requirements
        for t in (r.allowed_bm_types or []) if t not in type_ids
    })
    if bad_allowed:
        return ProcurementResult(
            success=False,
            solver_status=(
                f"INPUT_ERROR: allowed_bm_types references unknown type(s) "
                f"{bad_allowed}"
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
                 committed_entry_of, vm_demand, virtual_bms, splitter,
                 cp_solver, dropped):
        self.result = result
        self.buyable_type_of = buyable_type_of      # buyable bm_id -> type_id
        self.committed_type_of = committed_type_of  # committed bm_id -> type_id
        self.committed_entry_of = committed_entry_of  # bm_id -> entry index
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
    committed_entry_of: dict[str, int] = {}   # bm_id -> committed_stock index
    # (bucket, network) -> ids of virtual BMs occupying slots in that cell
    cell_members: dict[tuple[str, str], list[str]] = {}

    def add_virtual(bm_id: str, capacity: Resources, bucket: str,
                    network: str, rep: Topology, pool: str = "") -> None:
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
            pool=pool,
        )
        # Slot membership is pool-agnostic on purpose: machine slots are
        # physical, shared across pools (E2/S6).
        cell_members.setdefault((bucket, network), []).append(bm_id)

    # Committed stock first (it occupies slots and reduces what's left to buy).
    # ("floating group" = cardinality cap of one floating committed entry;
    # unrelated to the dedicated-pool dimension.)
    floating_groups: list[tuple[set[str], int]] = []
    for idx, cs in enumerate(request.committed_stock):
        if cs.count <= 0:
            continue
        capacity = type_by_id[cs.type_id].capacity
        # Named-pool machines get an id suffix so shared-pool ids stay
        # byte-identical to the pre-pool format (back-compat).
        pool_sfx = f"|{cs.pool}" if cs.pool else ""
        if cs.bucket is not None:
            rep = cells.get((cs.bucket, cs.network), Topology(**{dim: cs.bucket}))
            for k in range(cs.count):
                bm_id = (f"own{idx}-{cs.type_id}-{cs.bucket}|{cs.network}"
                         f"{pool_sfx}-{k}")
                add_virtual(bm_id, capacity, cs.bucket, cs.network, rep,
                            pool=cs.pool)
                committed_type_of[bm_id] = cs.type_id
                committed_entry_of[bm_id] = idx
        else:
            # Floating: copies in every network-compatible cell; a cardinality
            # cap keeps total usage within the owned count. Per cell, never
            # more copies than the cell's slot cap allows.
            floating_ids: set[str] = set()
            for (bucket, network), rep in cells.items():
                if cs.network and network != cs.network:
                    continue
                copies = cs.count
                bound = cell_gen_bound(bucket, network)
                if bound is not None:
                    copies = min(copies, bound)
                for k in range(copies):
                    bm_id = (f"own{idx}-{cs.type_id}-{bucket}|{network}"
                             f"{pool_sfx}-{k}")
                    add_virtual(bm_id, capacity, bucket, network, rep,
                                pool=cs.pool)
                    committed_type_of[bm_id] = cs.type_id
                    committed_entry_of[bm_id] = idx
                    floating_ids.add(bm_id)
            if floating_ids:
                floating_groups.append((floating_ids, cs.count))

    # Buyables are generated per demanded pool (E2/S6): a pool requirement's
    # new machines carry its tag (dedicated hardware stays dedicated), shared
    # demand buys shared. Generation may transiently offer the same physical
    # slot to several pools; enforcement is on USAGE via slot_groups, which
    # stay pool-agnostic.
    demanded_pools = sorted({r.pool for r in request.requirements})
    for bt in request.procurement_types:
        need = worst_case.get(bt.type_id, 0)
        if need <= 0:
            continue
        for bm_pool in demanded_pools:
            pool_sfx = f"|{bm_pool}" if bm_pool else ""
            for (bucket, network), rep in cells.items():
                slots = need
                bound = cell_gen_bound(bucket, network)
                if bound is not None:
                    slots = min(slots, bound)
                for k in range(slots):
                    bm_id = f"buy-{bt.type_id}-{bucket}|{network}{pool_sfx}-{k}"
                    add_virtual(bm_id, bt.capacity, bucket, network, rep,
                                pool=bm_pool)
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
    # restriction (缺口 3g). The pool coordinate is DIFFERENT (E2/S6): "" is
    # the shared pool, a distinct domain — never a wildcard — so pool matching
    # is exact on both sides. Candidate lists are cached per distinct
    # (network, pool) so they aren't rebuilt per requirement.
    def net_ok(req_net: str, item_net: str) -> bool:
        return req_net == "" or item_net == req_net

    ids_by_key: dict[tuple[str, str], tuple[list[str], list[str]]] = {}

    def candidates_for(net: str, pool: str) -> tuple[list[str], list[str]]:
        key = (net, pool)
        if key not in ids_by_key:
            in_stock_ids = [
                bm.id for bm in request.in_stock
                if net_ok(net, bm.network) and bm.pool == pool
            ]
            virtual_ids = [
                bm_id for bm_id, bm in virtual_bms.items()
                if net_ok(net, bm.network) and bm.pool == pool
            ]
            ids_by_key[key] = (in_stock_ids, virtual_ids)
        return ids_by_key[key]

    type_of = {**buyable_type_of, **committed_type_of}
    frozen = set(request.frozen_bm_ids)

    reqs = []
    unreachable: list[dict] = []
    for idx, r in enumerate(request.requirements):
        in_stock_ids, virtual_ids = candidates_for(r.network, r.pool)
        if r.allowed_bm_types is not None:
            # 決議 #38: this cluster may only buy/draw these machine types.
            allowed = set(r.allowed_bm_types)
            virtual_ids = [i for i in virtual_ids if type_of[i] in allowed]
        candidates = (r.candidate_baremetals or in_stock_ids) + virtual_ids
        if frozen:
            # Pre-release isolation (E2.5): a machine scheduled for release
            # hosts nothing new — filtered here (after the explicit-list
            # branch) so caller-supplied candidate lists can't reach it
            # either.
            candidates = [c for c in candidates if c not in frozen]
        if not candidates and has_demand(r):
            # Precheck with a cause-specific message: the splitter would drop
            # these too, but its generic spec-centric wording doesn't tell the
            # user what to declare.
            unreachable.append({
                "requirement_index": idx,
                "cluster_id": r.cluster_id,
                "node_role": r.node_role,
                "reason": _no_candidates_reason(r, request, cells),
            })
        reqs.append(r.model_copy(update={"candidate_baremetals": candidates}))

    if unreachable:
        result = PlacementResult(
            success=False,
            solver_status="INPUT_ERROR: unsatisfiable requirement(s)",
            bm_total_count=len(all_bms),
        )
        return _Pass(result, buyable_type_of, committed_type_of,
                     committed_entry_of, {}, virtual_bms, None, None,
                     unreachable)

    # Explicit VMs pass through with their candidate lists untouched: the
    # scheduler's step-3 filter is authoritative, so an explicit VM never
    # lands on a virtual (buyable/committed) BM and an empty candidate list
    # surfaces as the solver's documented INPUT_ERROR. Demand that should
    # drive procurement belongs in `requirements`.
    #
    # Pinned VMs are rejected on this path: the planner's health gauges and
    # roll-forward book placements against RAW in-stock used_capacity, so a
    # pinned VM (whose demand is already inside used_capacity) would be
    # double-counted. Placement/split-and-solve are the pinned-aware paths.
    pinned = sorted(vm.id for vm in request.vms if vm.pinned_to is not None)
    if pinned:
        result = PlacementResult(
            success=False,
            solver_status=(
                f"INPUT_ERROR: pinned VMs are not supported on the capacity "
                f"planning path (got {pinned})"
            ),
            bm_total_count=len(all_bms),
        )
        return _Pass(result, buyable_type_of, committed_type_of,
                     committed_entry_of, {}, virtual_bms, None, None, [])
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
                     committed_entry_of, {}, virtual_bms, splitter, None,
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
    solver.bm_group_caps = slot_groups + floating_groups

    result = solver.solve()
    cp_solver = getattr(solver, "_last_cp_solver", None)
    vm_demand = {vm.id: vm.demand for vm in placement_request.vms}
    return _Pass(result, buyable_type_of, committed_type_of,
                 committed_entry_of, vm_demand, virtual_bms, splitter,
                 cp_solver, splitter.dropped_requirements)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _no_candidates_reason(req: ResourceRequirement,
                          request: ProcurementRequest,
                          cells: dict[tuple[str, str], Topology]) -> str:
    """
    Actionable wording for a requirement no machine can serve. Cells are
    declared by the supply side only (in-stock / caps / bucketed committed,
    決議 #37) — the most common miss is a network nothing has declared.
    """
    if req.network and not any(net == req.network for (_, net) in cells):
        return (
            f"no (bucket, network) cell exists in network '{req.network}' — "
            f"declare one via an in-stock machine, a procurement_cap, or a "
            f"committed_stock entry with network='{req.network}'"
        )
    if req.pool:
        pool_supply = (
            any(bm.pool == req.pool for bm in request.in_stock)
            or any(cs.pool == req.pool for cs in request.committed_stock)
        )
        if not pool_supply and not request.procurement_types:
            # With procurement_types present, pool-tagged buyables are always
            # generated, so a pool demand cannot be candidate-less.
            return (
                f"pool '{req.pool}' has no supply — tag an in-stock machine "
                f"or committed_stock entry with pool='{req.pool}', or provide "
                f"procurement_types so machines can be bought into the pool "
                f"(pools are strictly isolated; shared stock is not reachable)"
            )
    if not request.in_stock and not request.procurement_types:
        return ("no in-stock machines and no procurement_types — "
                "nothing can host this demand")
    frozen = set(request.frozen_bm_ids)
    if (frozen and not request.procurement_types and request.in_stock
            and all(bm.id in frozen for bm in request.in_stock)):
        return (
            "every in-stock machine is frozen pending a scheduled release "
            "(fleet event) and there are no procurement_types — nothing can "
            "host this demand before the release month"
        )
    if req.allowed_bm_types is not None:
        return (
            f"allowed_bm_types {req.allowed_bm_types} matches no reachable "
            f"buyable or committed machine "
            f"(network '{req.network or 'any'}') and there are no in-stock "
            f"candidates"
        )
    return "no candidate baremetals reachable for this requirement"


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
            if (r.allowed_bm_types is not None
                    and bt.type_id not in r.allowed_bm_types):
                continue
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
    """How many `spec` VMs fit in `remaining` (per-BM, per-dimension floor).

    Dimensions come from the spec: a GPU model in `remaining` the spec
    doesn't ask for is not a bottleneck; a model the spec asks for but
    `remaining` lacks reads as 0 → count 0."""
    counts = [
        res_get(remaining, d) // res_get(spec, d)
        for d in resource_dims([spec])
        if res_get(spec, d) > 0
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
    committed_entry_used = dict(
        Counter(p.committed_entry_of[b] for b in used_own_ids)
    )

    in_stock_ids = {bm.id for bm in request.in_stock}
    in_stock_used = len({
        a.baremetal_id for a in r.assignments if a.baremetal_id in in_stock_ids
    })

    split_decisions = (
        p.splitter.get_split_decisions(p.cp_solver) if p.cp_solver else []
    )

    # Per-requirement coverage by source (E0/S2). Runs pre-roll-forward, so
    # virtual-BM ids are still recognizable. Membership in the virtual maps
    # is definitive; anything else the solver placed on must be in-stock
    # (candidates are drawn from in_stock ∪ virtual only). Explicit
    # request.vms are absent from vm_req_of and excluded by design.
    vm_req = p.splitter.vm_req_of if p.splitter is not None else {}
    cov: dict[int, Counter] = {}
    for a in r.assignments:
        ri = vm_req.get(a.vm_id)
        if ri is None:
            continue
        if a.baremetal_id in p.buyable_type_of:
            src = "new_buy"
        elif a.baremetal_id in p.committed_type_of:
            src = "committed"
        else:
            src = "in_stock"
        cov.setdefault(ri, Counter())[src] += 1
    requirement_coverage = [
        RequirementCoverage(
            requirement_index=i,
            cluster_id=req.cluster_id,
            node_role=req.node_role,
            in_stock=cov.get(i, Counter())["in_stock"],
            committed=cov.get(i, Counter())["committed"],
            new_buy=cov.get(i, Counter())["new_buy"],
            total=sum(cov.get(i, Counter()).values()),
        )
        for i, req in enumerate(request.requirements)
    ]

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
    frozen = set(request.frozen_bm_ids)
    for bm in post_bms:
        if bm.id in frozen:
            # Pending release (E2.5): the machine rejects new nodes until its
            # event month, so its free space is not usable headroom — counting
            # it would overstate every gauge (the D1 lie these gauges exist to
            # avoid). State snapshots elsewhere still show the machine.
            continue
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
        requirement_coverage=requirement_coverage,
        committed_used=committed_used,
        committed_entry_used=committed_entry_used,
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
        bm_placed=placed,
        bought_bms=[p.virtual_bms[bid] for bid in sorted(used_buy_ids)],
        committed_bms=[p.virtual_bms[bid] for bid in sorted(used_own_ids)],
        bought_type_of={bid: p.buyable_type_of[bid] for bid in used_buy_ids},
        diagnostics=r.diagnostics,
    )


# ---------------------------------------------------------------------------
# Multi-period horizon planning (Phase 3): sparse demand book →
# month-by-month roll-forward per fab → aggregated CapacityReport.
# ---------------------------------------------------------------------------

def solve_capacity_horizon(request: CapacityPlanRequest) -> CapacityReport:
    """
    Plan every (fab, month) present in the demand book, oldest month first,
    rolling state forward within each fab:

      - placements consume in-stock capacity (used_capacity advances);
      - bought / committed machines materialize as next month's in-stock
        (nodes are sticky — no reshuffling, 決議/替代方案 B);
      - each materialized machine consumes one slot from every matching
        bucket cap (max_bm decreases, 決議 #30);
      - committed pools drain as they are used;
      - committed entries with a future `available_from` are withheld until
        their month arrives (S1; inclusive gate, ISO month order);
      - fleet events (E2.5, 決議 #40): a machine scheduled for `release` is
        frozen before its event month (old load stands, hosts nothing new,
        free space excluded from gauges); at the event month its
        used_capacity zeroes and it re-enters the pool clean.

    Months absent from the book are absent from the report (unplanned ≠ zero
    growth, 決議 #26). Fabs are independent pools (決議 #4). When a month
    fails, its demand is NOT carried forward and the fab's remaining planned
    months are NOT solved — they are reported as `blocked` stubs (structural
    taint: a month planned without the failed month's demand would carry
    misleadingly optimistic numbers). Fix the inputs and replan.

    All state mutations on the rolling copies are attribute rebinds
    (used_capacity/id/count/max_bm reassignment), never in-place edits of
    nested models, so shallow model_copy is sufficient.
    """
    start = time.time()
    config = request.config
    fab_dim = config.fab_topology_dimension
    dim = config.procurement_spread_dimension

    book: dict[tuple[str, str], list] = {}
    for e in request.demand_book:
        book.setdefault((e.fab, e.period), []).append(e)
    periods = sorted({p for (_, p) in book})
    fabs = list(dict.fromkeys(e.fab for e in request.demand_book))

    reports: list[PeriodFabReport] = []
    budget: Counter = Counter()   # (fab, bucket, network, pool, period) -> bought
    all_ok = True

    for fab in fabs:
        # Per-fab rolling state. fab "" = single-fab mode: the whole pool
        # (mixing "" with named fabs is rejected by CapacityPlanRequest).
        stock = [
            bm.model_copy() for bm in request.in_stock
            if fab == "" or getattr(bm.topology, fab_dim) == fab
        ]
        caps_state = [
            c.model_copy() for c in request.procurement_caps
            if fab == "" or c.fab in ("", fab)
        ]
        committed_state = [
            cs.model_copy() for cs in request.committed_stock
            if fab == "" or cs.fab in ("", fab)
        ]
        types_f = [
            t for t in request.procurement_types
            if fab == "" or t.fab in ("", fab)
        ]
        # Fleet events (E2.5, 決議 #40): release month per machine, this
        # fab's events only (named-fab mode validates event.fab ↔ machine
        # fab consistency, so the scope filter and the machine set agree).
        release_month = {
            bid: ev.period
            for ev in request.fleet_events
            if fab == "" or ev.fab in ("", fab)
            for bid in ev.bm_ids
        }
        released_applied: set[str] = set()
        failed_period: str | None = None
        for period in periods:
            entries = book.get((fab, period))
            if not entries:
                continue  # unplanned month for this fab → absent from report

            if failed_period is not None:
                reports.append(_blocked_report(fab, period, failed_period))
                continue

            # Apply due releases exactly once, at the first planned month at
            # or after the event month (a release landing between planned
            # months takes effect at the next planned one — nothing solves
            # in between, so the states are indistinguishable). Whole-machine
            # semantics: the old cluster's load leaves with the release and
            # the machine re-enters the pool clean. Machines with a FUTURE
            # release month are frozen: present, loaded, hosting nothing new.
            released_now: list[str] = []
            for bm in stock:
                rm = release_month.get(bm.id)
                if (rm is not None and rm <= period
                        and bm.id not in released_applied):
                    bm.used_capacity = Resources()
                    released_applied.add(bm.id)
                    released_now.append(bm.id)
            frozen_ids = sorted(
                bid for bid, rm in release_month.items() if rm > period
            )

            # Month gate: an entry with a future available_from is withheld
            # (untouched, re-evaluated next period); ISO "YYYY-MM" strings
            # compare chronologically. This same list is both the index space
            # of committed_entry_used and the drain target in _roll_forward,
            # so filtering here cannot skew entry indices.
            active_committed = [
                c for c in committed_state
                if c.count > 0
                and (c.available_from is None or c.available_from <= period)
            ]
            preq = ProcurementRequest(
                requirements=[e.to_requirement() for e in entries],
                in_stock=stock,
                procurement_types=types_f,
                procurement_caps=caps_state,
                committed_stock=active_committed,
                frozen_bm_ids=frozen_ids,
                anti_affinity_rules=request.anti_affinity_rules,
                max_per_bm_rules=request.max_per_bm_rules,
                failover_rules=request.failover_rules,
                config=config,
            )
            res = solve_capacity_plan(preq)

            # Cell attribution must happen before roll-forward renames ids.
            node_adds_per_cell, bought_per_cell, own_per_cell, stock_used_per_cell = (
                _cell_attribution(res, stock, dim)
            )

            if res.success:
                _roll_forward(stock, caps_state, active_committed, res,
                              dim, period)
                for bm in res.bought_bms:
                    bucket, network, bm_pool = _cell_of(bm, dim)
                    type_id = res.bought_type_of.get(bm.id, "")
                    budget[(fab, bucket, network, bm_pool, period, type_id)] += 1
            else:
                all_ok = False
                failed_period = period

            # Join coverage rows back to demand-book rows by index —
            # preq.requirements was built as [e.to_requirement() for e in
            # entries], so requirement_index i ⇔ entries[i].
            demand_coverage = [
                DemandCoverage(
                    demand_id=entries[rc.requirement_index].demand_id,
                    cluster_id=rc.cluster_id,
                    node_role=rc.node_role,
                    period=period,
                    fab=fab,
                    in_stock=rc.in_stock,
                    committed=rc.committed,
                    new_buy=rc.new_buy,
                    total=rc.total,
                )
                for rc in res.requirement_coverage
            ]

            reports.append(_period_report(
                fab, period, res, stock, dim,
                node_adds_per_cell, bought_per_cell, own_per_cell,
                stock_used_per_cell, demand_coverage,
                released_bms=sorted(released_now), frozen_bms=frozen_ids,
                ref_spec=config.reference_vm_spec,
            ))

    budget_view = [
        BudgetRow(fab=f, bucket=b, network=n, pool=pl, period=pd, type_id=t,
                  bm_count=cnt)
        for (f, b, n, pl, pd, t), cnt in sorted(budget.items())
    ]
    # Aggregates cover SUCCESSFUL months only (consistent with budget_view);
    # a failed month's numbers are what-if output and live only in its own
    # PeriodFabReport.
    ok_reports = [r for r in reports if r.success]
    totals = {
        "node_adds": sum(r.node_adds_total for r in ok_reports),
        "bm_procurement": sum(r.bm_procurement_total for r in ok_reports),
        "committed_bm_used": sum(r.committed_bm_used for r in ok_reports),
        "in_stock_bm_used": sum(r.in_stock_bm_used for r in ok_reports),
        "by_fab": {
            fab: {
                "node_adds": sum(r.node_adds_total for r in ok_reports
                                 if r.fab == fab),
                "bm_procurement": sum(r.bm_procurement_total for r in ok_reports
                                      if r.fab == fab),
            }
            for fab in fabs
        },
    }
    return CapacityReport(
        success=all_ok,
        by_fab_period=reports,
        budget_view=budget_view,
        totals=totals,
        solve_time_seconds=time.time() - start,
        config_fingerprint=config_fingerprint(config),
    )


def _blocked_report(fab: str, period: str, failed_period: str) -> PeriodFabReport:
    """
    Stub for a planned month that was not solved because an earlier month
    failed for this fab (structural taint — solving it against state missing
    the failed month's demand would produce misleadingly optimistic numbers).
    """
    return PeriodFabReport(
        fab=fab, period=period, success=False,
        solver_status=(
            f"BLOCKED: not planned — period {failed_period} failed for this fab"
        ),
        shortfalls=[ShortfallDetail(
            cause="blocked",
            message=(
                f"not planned: period {failed_period} failed for this fab; "
                f"fix the inputs and replan"
            ),
        )],
    )


def _cell_of(bm: Baremetal, dim: str) -> tuple[str, str, str]:
    """The (bucket, network, pool) planning cell a BM belongs to (決議 #37;
    pool coordinate added by E2/S6 — "" for the shared pool, so no-pool
    requests keep their exact pre-pool cell set)."""
    return (getattr(bm.topology, dim), bm.network, bm.pool)


def _cell_attribution(
    res: ProcurementResult, stock: list[Baremetal], dim: str,
) -> tuple[Counter, Counter, Counter, Counter]:
    """Count this month's node adds / buys / committed draws / distinct
    in-stock machines touched, per (bucket, network, pool) cell, using
    pre-roll-forward ids."""
    cell_by_id: dict[str, tuple[str, str, str]] = {}
    stock_ids = {bm.id for bm in stock}
    for bm in stock:
        cell_by_id[bm.id] = _cell_of(bm, dim)
    for bm in list(res.bought_bms) + list(res.committed_bms):
        cell_by_id[bm.id] = _cell_of(bm, dim)

    node_adds = Counter(
        cell_by_id[a.baremetal_id] for a in res.assignments
        if a.baremetal_id in cell_by_id
    )
    bought = Counter(_cell_of(bm, dim) for bm in res.bought_bms)
    own = Counter(_cell_of(bm, dim) for bm in res.committed_bms)
    # Distinct machines, not placements: five nodes on one BM = one machine.
    touched_stock = {
        a.baremetal_id for a in res.assignments if a.baremetal_id in stock_ids
    }
    stock_used = Counter(cell_by_id[bid] for bid in touched_stock)
    return node_adds, bought, own, stock_used


def _roll_forward(stock: list[Baremetal], caps_state: list,
                  active_committed: list[CommittedStock],
                  res: ProcurementResult, dim: str, period: str) -> None:
    placed = res.bm_placed

    # 1. Placements consume in-stock capacity.
    for bm in stock:
        if bm.id in placed:
            bm.used_capacity = bm.used_capacity + placed[bm.id]

    # 2. Bought / committed machines materialize as in-stock for the next
    #    month. Ids are re-prefixed with the acquisition period so a later
    #    month's virtual-BM generation can never collide with them — and the
    #    synthetic rack is renamed along with the id, otherwise next month's
    #    regenerated virtual BM (same original id → same vrack) would be
    #    treated as rack-colocated with this machine.
    for mbm in list(res.bought_bms) + list(res.committed_bms):
        new_bm = mbm.model_copy()
        new_bm.used_capacity = placed.get(mbm.id, Resources())
        new_bm.id = f"acq-{period}-{mbm.id}"
        if mbm.topology.rack == f"vrack-{mbm.id}":
            new_bm.topology = mbm.topology.model_copy(
                update={"rack": f"vrack-{new_bm.id}"}
            )
        stock.append(new_bm)

        # 3. Each materialized machine consumes one physical slot from every
        #    cap tracking its (bucket, network) cell (決議 #30).
        bucket = _cell_of(mbm, dim)[0]
        for cap in caps_state:
            if cap.bucket == bucket and cap.network in ("", mbm.network):
                cap.max_bm = max(0, cap.max_bm - 1)

    # 4. Committed pools drain exactly the entries the solver drew from:
    #    committed_entry_used is keyed by index into the committed list this
    #    month's ProcurementRequest carried (`active_committed`).
    for entry_idx, used in res.committed_entry_used.items():
        active_committed[entry_idx].count = max(
            0, active_committed[entry_idx].count - used
        )


def _period_report(fab: str, period: str, res: ProcurementResult,
                   stock_after: list[Baremetal], dim: str,
                   node_adds_per_cell: Counter, bought_per_cell: Counter,
                   own_per_cell: Counter,
                   stock_used_per_cell: Counter,
                   demand_coverage: list[DemandCoverage],
                   released_bms: list[str] | None = None,
                   frozen_bms: list[str] | None = None,
                   ref_spec: Resources | None = None) -> PeriodFabReport:
    # Post-month in-stock snapshot per (bucket, network, pool) cell — for a
    # failed month the state is unchanged, so the snapshot shows what was
    # available. Pool-less requests only ever produce pool="" keys, keeping
    # the pre-pool cell set intact (E2/S6).
    cell_stock: dict[tuple[str, str, str], dict[str, Resources]] = {}
    # Landable node slots per cell (S3): per-BM bin-pack of the reference
    # spec. Frozen machines contribute zero — same usability stance as the
    # gauges (E2.5): space that can't take new nodes is not capacity.
    cell_slots: Counter = Counter()
    frozen_set = set(frozen_bms or [])
    for bm in stock_after:
        cell = _cell_of(bm, dim)
        agg = cell_stock.setdefault(cell, {
            "total": Resources(), "used": Resources(),
        })
        agg["total"] = agg["total"] + bm.total_capacity
        agg["used"] = agg["used"] + bm.used_capacity
        if ref_spec is not None and bm.id not in frozen_set:
            cell_slots[cell] += _fits_count(bm.available_capacity, ref_spec)

    # Cells cover every cell with stock OR activity: a failed month's what-if
    # buys land in cells that may hold no stock (state unchanged), and the
    # drill-down must still reconcile with the headline counts.
    all_cells = (
        set(cell_stock) | set(node_adds_per_cell)
        | set(bought_per_cell) | set(own_per_cell)
    )
    empty = {"total": Resources(), "used": Resources()}
    cells = [
        BucketMonthCell(
            fab=fab, bucket=bucket, network=network, pool=bm_pool,
            period=period,
            node_adds=node_adds_per_cell.get(cell, 0),
            bm_bought=bought_per_cell.get(cell, 0),
            committed_used=own_per_cell.get(cell, 0),
            in_stock_bm_used=stock_used_per_cell.get(cell, 0),
            in_stock_total=agg["total"],
            in_stock_used=agg["used"],
            in_stock_available=agg["total"] - agg["used"],
            in_stock_slots=(cell_slots.get(cell, 0)
                            if ref_spec is not None else None),
        )
        for cell in sorted(all_cells)
        for (bucket, network, bm_pool) in [cell]
        for agg in [cell_stock.get(cell, empty)]
    ]

    return PeriodFabReport(
        fab=fab, period=period,
        success=res.success,
        node_adds_total=sum(d.count for d in res.split_decisions),
        bm_procurement_total=res.procured_bm_total,
        committed_bm_used=res.committed_bm_used,
        in_stock_bm_used=res.in_stock_bm_used,
        procurement=res.procurement,
        committed_used=res.committed_used,
        split_decisions=res.split_decisions,
        shortfalls=_shortfall_details(res),
        solver_status=res.solver_status,
        nominal_available=res.nominal_available,
        remaining_node_slots=res.remaining_node_slots,
        stranded_available=res.stranded_available,
        balance_after=res.balance_after,
        cells=cells,
        demand_coverage=demand_coverage,
        released_bms=released_bms or [],
        frozen_bms=frozen_bms or [],
    )


def _shortfall_details(res: ProcurementResult) -> list[ShortfallDetail]:
    """Structured causes (決議 #33): what blocked, where, and a human line."""
    if res.success:
        return []
    details: list[ShortfallDetail] = []
    diag = res.diagnostics or {}

    if res.shortfall_cause == "anti_affinity":
        for entry in diag.get("infeasible_anti_affinity_rules", []):
            for fd in entry.get("failed_dimensions", []):
                details.append(ShortfallDetail(
                    cause="anti_affinity",
                    dimension=fd.get("dimension"),
                    needed=fd.get("min_buckets_needed"),
                    available=fd.get("reachable_buckets"),
                    message=(
                        f"group {entry.get('group_id')}: needs "
                        f"{fd.get('min_buckets_needed')} distinct "
                        f"{fd.get('dimension')} buckets, only "
                        f"{fd.get('reachable_buckets')} reachable"
                    ),
                ))

    if not details:
        if res.solver_status.startswith("INPUT_ERROR"):
            details.append(ShortfallDetail(
                cause="input_error", message=res.solver_status,
            ))
        else:
            message = {
                "space": "bucket slot caps (max_bm) exhausted — expanding "
                         "physical slots would make the plan feasible",
                "capacity": "insufficient resource capacity even with "
                            "procurement",
                "anti_affinity": "topology spread constraints cannot be "
                                 "satisfied",
                "unknown": f"solve not proven infeasible "
                           f"(status {res.solver_status}); consider raising "
                           f"max_solve_time_seconds",
            }.get(res.shortfall_cause, res.shortfall_cause)
            details.append(ShortfallDetail(
                cause=res.shortfall_cause, message=message,
            ))
    return details
