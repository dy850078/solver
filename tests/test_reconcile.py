"""
Test suite — plan-vs-actual reconciliation (E4/S3).
Run: pytest tests/test_reconcile.py -v

Strategy: build the plan with the REAL planner (solve_capacity_horizon), then
distort the actual snapshot in controlled ways and assert each metric / drift
category reacts — hand-crafted CapacityReports would just test our own
assumptions twice.
"""

from app.models import (
    ActualSnapshot,
    CapacityPlanRequest,
    DemandEntry,
    ExecutionRecord,
    MachineAdd,
    NodeRole,
    ReconcilePlan,
    ReconcileRequest,
    Resources,
    SolverConfig,
)
from app.capacity_planner import solve_capacity_horizon
from app.reconcile import reconcile

from .conftest import make_bm

SPEC_8 = Resources(cpu_cores=8, memory_mib=16_000, storage_gb=100)


def cfg(**kw):
    defaults = dict(max_solve_time_seconds=10,
                    auto_generate_anti_affinity=False,
                    reference_vm_spec=SPEC_8)
    defaults.update(kw)
    return SolverConfig(**defaults)


def build_plan(book, in_stock, types=None, config=None):
    report = solve_capacity_horizon(CapacityPlanRequest(
        demand_book=book,
        in_stock=in_stock,
        procurement_types=types or [],
        config=config or cfg(),
    ))
    assert report.success, report.by_fab_period
    return ReconcilePlan(plan_id="plan-v1", report=report,
                         demand_snapshot=book)


def rec(plan, in_stock, executions=None, machine_adds=None,
        as_of="2026-01-31", config=None):
    return reconcile(ReconcileRequest(
        plan=plan,
        actual=ActualSnapshot(
            as_of=as_of,
            in_stock=in_stock,
            executions=executions or [],
            machine_adds=machine_adds or [],
        ),
        config=config or cfg(),
    ))


def book_entry(demand_id="DMD-1", cpu=32, period="2026-01"):
    return DemandEntry(cluster_id="cluster-1", period=period,
                       cpu_cores=cpu, vm_specs=[SPEC_8],
                       demand_id=demand_id)


def execution(demand_id="DMD-1", vm_count=4, status="success",
              period="2026-01", cluster="cluster-1"):
    return ExecutionRecord(demand_id=demand_id, cluster_id=cluster,
                          node_role=NodeRole.WORKER, vm_count=vm_count,
                          status=status, period=period)


class TestReconcileHappyPath:

    def test_world_matches_plan_no_drift(self):
        """Actual == what the plan predicted → four clean headlines, zero
        drift rows. Predicted end-of-month state: 4 VMs on bm-1."""
        in_stock = [make_bm("bm-1", cpu=64, mem=256_000, disk=2000)]
        plan = build_plan([book_entry(cpu=32)], in_stock)
        # Reality: the 4 planned VMs landed on bm-1 (32c used).
        actual = [make_bm("bm-1", cpu=64, mem=256_000, disk=2000,
                          used_cpu=32, used_mem=64_000, used_disk=400)]

        r = rec(plan, actual, executions=[execution(vm_count=4)])

        assert r.success and r.status == "OK"
        assert r.period == "2026-01"
        assert r.headline.fulfillment_rate == 1.0
        assert r.headline.planned_vms == 4
        assert r.headline.forecast_error == 0.0
        assert r.headline.unplanned_ratio == 0.0
        assert r.headline.supply_hit_rate is None   # nothing was to arrive
        assert r.drifts == []

    def test_plan_not_covering_month_is_input_error(self):
        in_stock = [make_bm("bm-1")]
        plan = build_plan([book_entry()], in_stock)

        r = rec(plan, in_stock, as_of="2026-03-15")

        assert not r.success
        assert r.status.startswith("INPUT_ERROR")
        assert "2026-03" in r.status and "2026-01" in r.status

    def test_fingerprints_echoed_both_sides(self):
        in_stock = [make_bm("bm-1")]
        plan = build_plan([book_entry()], in_stock)

        r = rec(plan, in_stock)

        assert r.config_fingerprint
        assert r.plan_config_fingerprint == plan.report.config_fingerprint


class TestFulfillmentAndDemandDrift:

    def test_under_execution_lowers_fulfillment(self):
        """Planned 4 VMs, only 2 executed successfully → 50% + demand drift
        row with the negative delta."""
        in_stock = [make_bm("bm-1", cpu=64, mem=256_000, disk=2000)]
        plan = build_plan([book_entry(cpu=32)], in_stock)
        actual = [make_bm("bm-1", cpu=64, mem=256_000, disk=2000,
                          used_cpu=16, used_mem=32_000, used_disk=200)]

        r = rec(plan, actual, executions=[execution(vm_count=2)])

        assert r.headline.fulfillment_rate == 0.5
        assert r.headline.fulfilled_vms == 2
        d = [x for x in r.drifts if x.category == "demand"]
        assert len(d) == 1 and d[0].delta == -2 and d[0].demand_ids == ["DMD-1"]

    def test_failed_executions_do_not_fulfill(self):
        in_stock = [make_bm("bm-1", cpu=64, mem=256_000, disk=2000)]
        plan = build_plan([book_entry(cpu=32)], in_stock)

        r = rec(plan, in_stock,
                executions=[execution(vm_count=4, status="failed")])

        assert r.headline.fulfillment_rate == 0.0
        # The failed batch still counts as executed volume (it went through
        # the book — not a meteor).
        assert r.headline.executed_vms == 4
        assert r.headline.unplanned_ratio == 0.0

    def test_meteor_execution_unplanned_ratio_and_drift(self):
        """An execution with no demand_id (or an id the plan never saw) is a
        meteor: raises unplanned_ratio, never fulfillment."""
        in_stock = [make_bm("bm-1", cpu=64, mem=256_000, disk=2000)]
        plan = build_plan([book_entry(cpu=32)], in_stock)

        r = rec(plan, in_stock, executions=[
            execution(vm_count=4),
            execution(demand_id=None, vm_count=2, cluster="rogue"),
            execution(demand_id="DMD-ghost", vm_count=2, cluster="ghost"),
        ])

        assert r.headline.unplanned_ratio == 0.5   # 4 of 8 VMs off-book
        assert r.headline.fulfillment_rate == 1.0  # planned part fully landed
        meteors = [d for d in r.drifts
                   if d.category == "demand" and "unplanned" in d.message]
        assert len(meteors) == 2
        assert any(d.demand_ids == ["DMD-ghost"] for d in meteors)

    def test_over_execution_capped_and_flagged(self):
        """6 successes against a 4-VM plan: fulfillment caps at 100% (not
        150%), the surplus shows as a positive demand-drift delta."""
        in_stock = [make_bm("bm-1", cpu=64, mem=256_000, disk=2000)]
        plan = build_plan([book_entry(cpu=32)], in_stock)

        r = rec(plan, in_stock, executions=[execution(vm_count=6)])

        assert r.headline.fulfillment_rate == 1.0
        d = [x for x in r.drifts if x.category == "demand"]
        assert len(d) == 1 and d[0].delta == 2

    def test_rows_without_demand_id_are_unjoinable_not_misses(self):
        """A book row without demand_id can't join executions — it must be
        excluded from the rate and reported, not counted as a 0% miss."""
        in_stock = [make_bm("bm-1", cpu=64, mem=256_000, disk=2000)]
        book = [book_entry(), DemandEntry(cluster_id="c2", period="2026-01",
                                          cpu_cores=16, vm_specs=[SPEC_8])]
        plan = build_plan(book, in_stock)

        r = rec(plan, in_stock, executions=[execution(vm_count=4)])

        assert r.headline.planned_vms == 4          # joinable row only
        assert r.headline.unjoinable_planned_vms == 2
        assert r.headline.fulfillment_rate == 1.0

    def test_executions_outside_period_ignored(self):
        in_stock = [make_bm("bm-1", cpu=64, mem=256_000, disk=2000)]
        plan = build_plan([book_entry(cpu=32)], in_stock)

        r = rec(plan, in_stock,
                executions=[execution(vm_count=4, period="2026-02")])

        assert r.headline.executed_vms == 0
        assert r.headline.fulfillment_rate == 0.0


class TestForecastError:

    def test_missing_machine_shows_in_slots_error(self):
        """Plan predicted bm-2's 8 slots; the machine is gone at reconcile
        time → forecast_error = missing/predicted, cell delta negative."""
        in_stock = [make_bm("bm-1", cpu=64, mem=256_000, disk=2000,
                            ag="ag-1", rack="rack-1"),
                    make_bm("bm-2", cpu=64, mem=256_000, disk=2000,
                            ag="ag-2", rack="rack-2")]
        plan = build_plan([book_entry(cpu=0, demand_id=None)], in_stock)
        actual = [make_bm("bm-1", cpu=64, mem=256_000, disk=2000,
                          ag="ag-1", rack="rack-1")]

        r = rec(plan, actual)

        assert r.headline.forecast_error == 0.5    # 8 of 16 slots missing
        gone = [c for c in r.cells if c.bucket == "ag-2"]
        assert gone[0].predicted_slots == 8 and gone[0].actual_slots == 0
        assert gone[0].slots_delta == -8

    def test_fragmentation_hits_slots_not_nominal(self):
        """The reason this runs in the solver: 64 free cores as 8×8 frags
        land 0 reference VMs. Nominal available matches the plan exactly;
        only the landable recount sees the loss."""
        in_stock = [make_bm(f"bm-{i}", cpu=8, mem=256_000, disk=2000,
                            ag=f"ag-{i}", rack=f"rack-{i}") for i in range(8)]
        plan = build_plan([book_entry(cpu=0, demand_id=None)], in_stock)
        # Same nominal free total (64c), but each machine now has 1 core
        # used → no 8c VM fits anywhere. (Total 71c... keep totals equal:
        # cpu=9, used=1 → 8 free per machine? No — fits_count uses free 8:
        # still fits. Use cpu=8, used=1 → 7 free, nothing fits.)
        actual = [make_bm(f"bm-{i}", cpu=8, mem=256_000, disk=2000,
                          used_cpu=1, ag=f"ag-{i}", rack=f"rack-{i}")
                  for i in range(8)]

        r = rec(plan, actual)

        assert r.headline.forecast_error == 1.0    # all 8 slots gone
        assert sum(c.actual_available.cpu_cores for c in r.cells) == 56

    def test_no_reference_spec_error_is_none(self):
        """Plan built without reference_vm_spec has no slot numbers — the
        error must be None (unmeasurable), never a fake 0."""
        c = cfg(reference_vm_spec=None)
        in_stock = [make_bm("bm-1", cpu=64, mem=256_000, disk=2000)]
        plan = build_plan([book_entry(cpu=0, demand_id=None)], in_stock,
                          config=c)

        r = rec(plan, in_stock, config=c)

        assert r.headline.forecast_error is None
        assert all(x.slots_delta is None for x in r.cells)


class TestSupplyAndFleetDrift:

    def test_supply_late_lowers_hit_rate(self):
        """Plan buys 1 machine in the month; nothing arrived → hit rate 0,
        supply drift row carries the shortage."""
        types = [dict(type_id="big",
                      capacity=Resources(cpu_cores=64, memory_mib=256_000,
                                         storage_gb=2000))]
        from app.models import BaremetalType
        plan = build_plan([book_entry(cpu=32)], [],
                          types=[BaremetalType(**t) for t in types])
        r = rec(plan, [], executions=[])

        assert r.headline.planned_machine_adds == 1
        assert r.headline.supply_hit_rate == 0.0
        d = [x for x in r.drifts if x.category == "supply"]
        assert len(d) == 1 and d[0].delta == -1
        assert "late or missing" in d[0].message

    def test_supply_arrival_hits(self):
        # A plan with no in-stock and no caps has no declared buckets — its
        # buy lands in the bucket-less cell, so the arrival record and the
        # arrived machine must sit in that same cell to match.
        from app.models import BaremetalType
        big = BaremetalType(type_id="big",
                            capacity=Resources(cpu_cores=64,
                                               memory_mib=256_000,
                                               storage_gb=2000))
        plan = build_plan([book_entry(cpu=32)], [], types=[big])
        arrived = [make_bm("bm-new", cpu=64, mem=256_000, disk=2000, ag="",
                           used_cpu=32, used_mem=64_000, used_disk=400)]

        r = rec(plan, arrived,
                executions=[execution(vm_count=4)],
                machine_adds=[MachineAdd(bucket="", period="2026-01",
                                         count=1)])

        assert r.headline.supply_hit_rate == 1.0
        assert [x for x in r.drifts if x.category == "supply"] == []

    def test_unmodeled_capacity_loss_is_fleet_drift(self):
        """No adds planned or recorded, but a machine shrank (repair swap) →
        fleet drift, pointing at fleet events as the fix."""
        in_stock = [make_bm("bm-1", cpu=64, mem=256_000, disk=2000)]
        plan = build_plan([book_entry(cpu=0, demand_id=None)], in_stock)
        actual = [make_bm("bm-1", cpu=32, mem=256_000, disk=2000)]

        r = rec(plan, actual)

        d = [x for x in r.drifts if x.category == "fleet"]
        assert len(d) == 1 and d[0].delta == -32
        assert "fleet event" in d[0].message

    def test_supply_mismatch_owns_cell_no_duplicate_fleet_row(self):
        """A cell whose adds are short explains its own capacity gap: one
        supply row (capacity delta noted in message), no fleet row."""
        from app.models import BaremetalType
        big = BaremetalType(type_id="big",
                            capacity=Resources(cpu_cores=64,
                                               memory_mib=256_000,
                                               storage_gb=2000))
        plan = build_plan([book_entry(cpu=32)], [], types=[big])

        r = rec(plan, [])

        cats = [x.category for x in r.drifts if x.category in ("supply", "fleet")]
        assert cats == ["supply"]
        assert "capacity total moved" in r.drifts[-1].message


class TestPlacementDrift:

    def test_same_total_different_cells_is_placement_drift(self):
        """Fab-level used matches the plan but it landed in the other AG →
        placement rows on both cells, ± the moved cores."""
        in_stock = [make_bm("bm-1", cpu=64, mem=256_000, disk=2000,
                            ag="ag-1", rack="rack-1"),
                    make_bm("bm-2", cpu=64, mem=256_000, disk=2000,
                            ag="ag-2", rack="rack-2")]
        plan = build_plan([book_entry(cpu=32)], in_stock)
        pred_used = {c.bucket: c.in_stock_used.cpu_cores
                     for p in plan.report.by_fab_period for c in p.cells}
        # Planner consolidates the 32c onto one machine; put reality entirely
        # on the OTHER one.
        src = max(pred_used, key=pred_used.get)
        dst = "ag-2" if src == "ag-1" else "ag-1"
        actual = [
            make_bm("bm-1", cpu=64, mem=256_000, disk=2000,
                    ag="ag-1", rack="rack-1",
                    used_cpu=32 if dst == "ag-1" else 0,
                    used_mem=64_000 if dst == "ag-1" else 0,
                    used_disk=400 if dst == "ag-1" else 0),
            make_bm("bm-2", cpu=64, mem=256_000, disk=2000,
                    ag="ag-2", rack="rack-2",
                    used_cpu=32 if dst == "ag-2" else 0,
                    used_mem=64_000 if dst == "ag-2" else 0,
                    used_disk=400 if dst == "ag-2" else 0),
        ]

        r = rec(plan, actual, executions=[execution(vm_count=4)])

        d = {x.bucket: x.delta for x in r.drifts if x.category == "placement"}
        assert d == {src: -32, dst: 32}
        assert r.headline.fulfillment_rate == 1.0

    def test_fab_level_gap_suppresses_placement_attribution(self):
        """When the fab total itself is off (demand never landed), cell-level
        used deltas are a demand story — no placement rows."""
        in_stock = [make_bm("bm-1", cpu=64, mem=256_000, disk=2000)]
        plan = build_plan([book_entry(cpu=32)], in_stock)

        r = rec(plan, in_stock, executions=[])   # nothing executed

        assert [x for x in r.drifts if x.category == "placement"] == []


class TestReconcileEndpoint:

    def test_endpoint_round_trip(self):
        from fastapi.testclient import TestClient
        from app.server import api

        in_stock = [make_bm("bm-1", cpu=64, mem=256_000, disk=2000)]
        plan = build_plan([book_entry(cpu=32)], in_stock)
        payload = ReconcileRequest(
            plan=plan,
            actual=ActualSnapshot(as_of="2026-01-31",
                                  in_stock=in_stock,
                                  executions=[execution(vm_count=4)]),
            config=cfg(),
        )
        resp = TestClient(api).post("/v1/capacity/reconcile",
                                    json=payload.model_dump(mode="json"))
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] and body["period"] == "2026-01"
        assert body["headline"]["fulfillment_rate"] == 1.0
