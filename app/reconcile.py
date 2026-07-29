"""
Plan-vs-actual reconciliation (E4/S3) — a pure function, no persistence.

Go archives every canonical plan run (G4) and posts it back here with the
current real snapshot and its execution ledger. The solver's one hard job is
the landable-capacity recount: per-BM bin-packing of reference_vm_spec over
the actual machines (nominal sums lie under fragmentation, 決議 #5).
Everything else is a diff — per-cell predicted vs actual, four headline
metrics (決議 #9), and rule-based drift classification (決議: 漂移四分類).

Scope: ONE target month per call — the month of `actual.as_of`. A mid-month
run is a progress view (fulfillment / supply accrue naturally); the run at
month end is the official score. Weekly cadence = call weekly; the demand
book stays monthly (帳本粒度與量測節奏解耦).

Drift attribution is rule-based v1 and deliberately modest: a cell whose
capacity total moved AND whose supply arrived short gets a supply row with
the capacity delta noted, not a fake single cause; only an unexplained
capacity move becomes a fleet row.
"""

from __future__ import annotations

import logging
from collections import Counter

from .models import (
    DriftDetail,
    ReconcileCell,
    ReconcileHeadline,
    ReconcileReport,
    ReconcileRequest,
    Resources,
    config_fingerprint,
)
from .capacity_planner import _fits_count

logger = logging.getLogger(__name__)

# Placement drift is only attributed when the fab-level used total roughly
# matches (|Δ| within this fraction of predicted) while individual cells
# disagree — then the demand landed, just not where roll-forward assumed.
# A large fab-level gap means demand/supply drift dominates and blaming
# placement would be noise.
PLACEMENT_FAB_TOLERANCE = 0.05

Cell = tuple[str, str, str, str]      # (fab, bucket, network, pool)


def reconcile(request: ReconcileRequest) -> ReconcileReport:
    period = request.actual.as_of[:7]
    fp = config_fingerprint(request.config)
    plan_fp = request.plan.report.config_fingerprint

    period_reports = [
        p for p in request.plan.report.by_fab_period if p.period == period
    ]
    if not period_reports:
        planned = sorted({p.period for p in request.plan.report.by_fab_period})
        return ReconcileReport(
            success=False,
            status=(f"INPUT_ERROR: plan does not cover period {period} "
                    f"(planned periods: {planned})"),
            period=period, as_of=request.actual.as_of,
            plan_id=request.plan.plan_id,
            config_fingerprint=fp, plan_config_fingerprint=plan_fp,
        )

    cells = _diff_cells(request, period, period_reports)
    headline = ReconcileHeadline()
    drifts: list[DriftDetail] = []

    _fulfillment_and_demand_drift(request, period, period_reports,
                                  headline, drifts)
    _supply_and_fleet_drift(request, period, cells, headline, drifts)
    _placement_drift(cells, drifts)
    _forecast_error(cells, headline)

    return ReconcileReport(
        success=True,
        status="OK",
        period=period,
        as_of=request.actual.as_of,
        plan_id=request.plan.plan_id,
        headline=headline,
        cells=cells,
        drifts=drifts,
        config_fingerprint=fp,
        plan_config_fingerprint=plan_fp,
    )


# ---------------------------------------------------------------------------
# Cell diff — the landable recount lives here
# ---------------------------------------------------------------------------

def _diff_cells(request: ReconcileRequest, period: str,
                period_reports: list) -> list[ReconcileCell]:
    """
    Union of predicted cells (the plan's post-month state for the target
    month) and actual cells (grouped from the real snapshot). The actual
    side recounts landable slots with the SAME per-BM bin-pack the planner
    uses — the one number Go cannot compute from nominal sums.
    """
    config = request.config
    fab_dim = config.fab_topology_dimension
    dim = config.procurement_spread_dimension
    ref_spec = config.reference_vm_spec

    predicted: dict[Cell, dict] = {}
    for rep in period_reports:
        for c in rep.cells:
            predicted[(c.fab, c.bucket, c.network, c.pool)] = {
                "slots": c.in_stock_slots,
                "total": c.in_stock_total,
                "used": c.in_stock_used,
                "adds": c.bm_bought + c.committed_used,
            }

    actual: dict[Cell, dict] = {}
    for bm in request.actual.in_stock:
        key = (getattr(bm.topology, fab_dim), getattr(bm.topology, dim),
               bm.network, bm.pool)
        agg = actual.setdefault(key, {
            "slots": 0 if ref_spec is not None else None,
            "total": Resources(), "used": Resources(),
        })
        agg["total"] = agg["total"] + bm.total_capacity
        agg["used"] = agg["used"] + bm.used_capacity
        if ref_spec is not None:
            agg["slots"] += _fits_count(bm.available_capacity, ref_spec)

    # Single-fab plans carry fab="" in their cells while real machines carry
    # their topology value; align by blanking the actual fab when every
    # predicted cell is fab-less.
    if predicted and all(k[0] == "" for k in predicted):
        actual = {("",) + k[1:]: v for k, v in actual.items()}

    # Whether the plan carries landable numbers at all: decides if a cell
    # MISSING from the prediction reads as "predicted 0 slots" (honest zero)
    # or "not measured" (plan built without reference_vm_spec → None; an
    # invented 0 would poison forecast_error).
    plan_has_slots = any(v["slots"] is not None for v in predicted.values())

    out: list[ReconcileCell] = []
    for key in sorted(set(predicted) | set(actual)):
        fab, bucket, network, pool = key
        p = predicted.get(key)
        a = actual.get(key)
        p_slots = p["slots"] if p else (0 if plan_has_slots else None)
        a_slots = a["slots"] if a else (0 if ref_spec is not None else None)
        p_total = p["total"] if p else Resources()
        p_used = p["used"] if p else Resources()
        a_total = a["total"] if a else Resources()
        a_used = a["used"] if a else Resources()
        out.append(ReconcileCell(
            fab=fab, bucket=bucket, network=network, pool=pool, period=period,
            predicted_slots=p_slots,
            actual_slots=a_slots,
            slots_delta=(a_slots - p_slots
                         if p_slots is not None and a_slots is not None
                         else None),
            predicted_total=p_total, actual_total=a_total,
            predicted_used=p_used, actual_used=a_used,
            predicted_available=p_total - p_used,
            actual_available=a_total - a_used,
        ))
    return out


# ---------------------------------------------------------------------------
# Metrics + drift rules
# ---------------------------------------------------------------------------

def _fulfillment_and_demand_drift(request, period: str, period_reports,
                                  headline: ReconcileHeadline,
                                  drifts: list[DriftDetail]) -> None:
    """
    承諾兌現率: fulfilled = Σ_d min(planned_d, executed_success_d), joined by
    demand_id. Coverage rows without a demand_id cannot join — they are
    EXCLUDED from the rate and surfaced as unjoinable (a book-hygiene
    signal), never silently counted as misses.
    """
    planned: Counter = Counter()
    for rep in period_reports:
        for cov in rep.demand_coverage:
            if cov.demand_id:
                planned[cov.demand_id] += cov.total
            else:
                headline.unjoinable_planned_vms += cov.total

    executed_ok: Counter = Counter()
    executed_all = 0
    unplanned = 0
    plan_ids = set(planned) | {
        e.demand_id for e in request.plan.demand_snapshot if e.demand_id
    }
    for ex in request.actual.executions:
        if ex.period != period:
            continue
        executed_all += ex.vm_count
        if ex.demand_id and ex.demand_id in plan_ids:
            if ex.status == "success":
                executed_ok[ex.demand_id] += ex.vm_count
        else:
            # Meteor: bypassed the book, or carries an id the plan never saw.
            unplanned += ex.vm_count
            drifts.append(DriftDetail(
                category="demand", fab=ex.fab, period=period,
                delta=ex.vm_count,
                demand_ids=[ex.demand_id] if ex.demand_id else [],
                message=(
                    f"unplanned execution: {ex.vm_count} VMs for cluster "
                    f"{ex.cluster_id!r} "
                    + (f"carry unknown demand_id {ex.demand_id!r}"
                       if ex.demand_id else "carry no demand_id")
                ),
            ))

    headline.planned_vms = sum(planned.values())
    headline.fulfilled_vms = sum(
        min(n, executed_ok[d]) for d, n in planned.items()
    )
    if headline.planned_vms:
        headline.fulfillment_rate = (
            headline.fulfilled_vms / headline.planned_vms
        )
    headline.executed_vms = executed_all
    headline.unplanned_vms = unplanned
    if executed_all:
        headline.unplanned_ratio = unplanned / executed_all

    for d, n in sorted(planned.items()):
        delta = executed_ok[d] - n
        if delta != 0:
            drifts.append(DriftDetail(
                category="demand", period=period, delta=delta,
                demand_ids=[d],
                message=(
                    f"demand {d}: planned {n} VMs, "
                    f"{executed_ok[d]} executed successfully "
                    + ("(under-execution or still in progress)"
                       if delta < 0 else "(executed beyond plan)")
                ),
            ))


def _supply_and_fleet_drift(request, period: str,
                            cells: list[ReconcileCell],
                            headline: ReconcileHeadline,
                            drifts: list[DriftDetail]) -> None:
    """
    供給命中率: planned machine adds per cell (plan's bm_bought +
    committed_used) vs Go-counted actual arrivals. A cell with an adds
    mismatch gets a supply row (its capacity delta noted, no fake single
    cause); a capacity move WITHOUT an adds mismatch is unexplained by
    supply → fleet row (repair / decommission / borrow that never entered
    the model — feed it back as a fleet event).
    """
    planned_adds: Counter = Counter()
    for rep in request.plan.report.by_fab_period:
        if rep.period != period:
            continue
        for c in rep.cells:
            if c.bm_bought or c.committed_used:
                planned_adds[(c.fab, c.bucket, c.network, c.pool)] += (
                    c.bm_bought + c.committed_used
                )
    actual_adds: Counter = Counter()
    for ma in request.actual.machine_adds:
        if ma.period == period:
            actual_adds[(ma.fab, ma.bucket, ma.network, ma.pool)] += ma.count

    headline.planned_machine_adds = sum(planned_adds.values())
    headline.actual_machine_adds = sum(actual_adds.values())
    if headline.planned_machine_adds:
        headline.supply_hit_rate = sum(
            min(n, actual_adds[k]) for k, n in planned_adds.items()
        ) / headline.planned_machine_adds

    capacity_delta = {
        (c.fab, c.bucket, c.network, c.pool):
            c.actual_total.cpu_cores - c.predicted_total.cpu_cores
        for c in cells
    }
    for key in sorted(set(planned_adds) | set(actual_adds)):
        fab, bucket, network, pool = key
        delta = actual_adds[key] - planned_adds[key]
        if delta == 0:
            continue
        cap_note = ""
        if capacity_delta.get(key):
            cap_note = (f"; cell capacity total moved "
                        f"{capacity_delta[key]:+d} cpu_cores")
        drifts.append(DriftDetail(
            category="supply", fab=fab, bucket=bucket, network=network,
            pool=pool, period=period, delta=delta,
            message=(
                f"planned {planned_adds[key]} machine add(s), "
                f"{actual_adds[key]} arrived"
                + (" (late or missing)" if delta < 0 else " (extra arrival)")
                + cap_note
            ),
        ))
    for c in cells:
        key = (c.fab, c.bucket, c.network, c.pool)
        if key in planned_adds or key in actual_adds:
            continue  # adds mismatch (or clean match) owns this cell's story
        delta = capacity_delta.get(key, 0)
        if delta != 0:
            drifts.append(DriftDetail(
                category="fleet", fab=c.fab, bucket=c.bucket,
                network=c.network, pool=c.pool, period=period, delta=delta,
                message=(
                    f"capacity total moved {delta:+d} cpu_cores with no "
                    f"machine adds planned or recorded — unmodeled fleet "
                    f"change (repair/decommission/borrow); consider a fleet "
                    f"event"
                ),
            ))


def _placement_drift(cells: list[ReconcileCell],
                     drifts: list[DriftDetail]) -> None:
    """
    放置漂移: the fab's used total landed (within tolerance) but individual
    cells disagree — the demand went in, just not where roll-forward assumed.
    Feeds the consistency work (M1/M3), not a person.
    """
    by_fab: dict[str, list[ReconcileCell]] = {}
    for c in cells:
        by_fab.setdefault(c.fab, []).append(c)
    for fab, fab_cells in by_fab.items():
        pred_used = sum(c.predicted_used.cpu_cores for c in fab_cells)
        actual_used = sum(c.actual_used.cpu_cores for c in fab_cells)
        if abs(actual_used - pred_used) > PLACEMENT_FAB_TOLERANCE * max(
                1, pred_used):
            continue  # fab-level gap → demand/supply story, not placement
        for c in fab_cells:
            delta = c.actual_used.cpu_cores - c.predicted_used.cpu_cores
            if delta != 0:
                drifts.append(DriftDetail(
                    category="placement", fab=c.fab, bucket=c.bucket,
                    network=c.network, pool=c.pool, period=c.period,
                    delta=delta,
                    message=(
                        f"used capacity is {delta:+d} cpu_cores vs the "
                        f"plan's assumed placement while the fab total "
                        f"matches — real placement landed elsewhere"
                    ),
                ))


def _forecast_error(cells: list[ReconcileCell],
                    headline: ReconcileHeadline) -> None:
    """
    容量預測誤差 = Σ|Δslots| / Σ predicted slots — relative L1 over LANDABLE
    slots, not nominal Resources. None when the plan carries no slot numbers
    (built without reference_vm_spec) or predicts zero slots: an error rate
    over nothing is not 0%.
    """
    pairs = [
        (c.predicted_slots, c.actual_slots) for c in cells
        if c.predicted_slots is not None and c.actual_slots is not None
    ]
    total_pred = sum(p for p, _ in pairs)
    if pairs and total_pred > 0:
        headline.forecast_error = (
            sum(abs(a - p) for p, a in pairs) / total_pred
        )
