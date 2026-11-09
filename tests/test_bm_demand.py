"""
bm_demand — PM-level BM-equivalent demand compiled onto the rollout /
sizing engines (ADR-020), with C7 (ADR-019) supplying the footprint floor.

Reference scenario (from the engineer's PM example):
  BM 90 vCPU / 720 GiB; specs full = 1/1, half = 1/2, third = 1/3.
  A1: 11 machines of thirds (+3 masters), B1: 5 machines of halves,
  C1: 6 machines, exclusive. Every node type spreads over ≥ 6 machines.
"""

import json
import logging
from pathlib import Path

import pytest

from app.bm_demand import (
    BmDemandPlanRequest, BmSpec, ClusterDemand, FleetTopology, TenancyGroup,
    VmSpec, compile_bm_demands, fits_per_bm, plan_bm_demands,
)
from app.models import Resources, SolverConfig

GiB = 1024


def R(cpu, gib, disk=1000, gpu=None):
    return Resources(cpu_cores=cpu, memory_mib=gib * GiB, storage_gb=disk, gpu=gpu or {})


BM = R(90, 720, 6000)
SPECS = [
    VmSpec(name="full", resources=R(90, 720, 6000)),
    VmSpec(name="half", resources=R(45, 360, 3000)),
    VmSpec(name="third", resources=R(30, 240, 2000)),
    VmSpec(name="odd40", resources=R(40, 300, 2000)),
]
FAST = SolverConfig(max_solve_time_seconds=10, w_headroom=0)


def request(demands, groups=None, exclusive=(), min_distinct=6, config=FAST, **kw):
    return BmDemandPlanRequest(
        bm_specs=[BmSpec(name="bm", capacity=BM)],
        vm_specs=SPECS,
        demands=demands,
        groups=groups or [],
        exclusive_clusters=list(exclusive),
        default_min_distinct_bms=min_distinct,
        topology=FleetTopology(racks=6, ags=3),
        config=config,
        **kw,
    )


def D(cluster, spec, n=None, count=None, role="worker", ip="routable", **kw):
    return ClusterDemand(cluster_id=cluster, node_role=role, ip_type=ip, vm_spec=spec,
                         bm_equivalents=n, vm_count=count, **kw)


# ===========================================================================
# 1. fits_per_bm — the machine-count arithmetic
# ===========================================================================

class TestFitsPerBm:

    def test_exact_fractions(self):
        assert fits_per_bm(R(90, 720, 6000), BM) == 1
        assert fits_per_bm(R(45, 360, 3000), BM) == 2
        assert fits_per_bm(R(30, 240, 2000), BM) == 3

    def test_floor_not_round(self):
        # 90/40 = 2.25 → 2, never 3
        assert fits_per_bm(R(40, 100, 100), BM) == 2

    def test_min_across_dimensions(self):
        # CPU allows 2, memory allows 1 → 1
        assert fits_per_bm(R(45, 480, 100), BM) == 1

    def test_gpu_dimension_counts(self):
        cap = Resources(cpu_cores=90, memory_mib=720 * GiB, storage_gb=6000, gpu={"h200": 8})
        assert fits_per_bm(R(10, 10, 10, gpu={"h200": 3}), cap) == 2
        assert fits_per_bm(R(10, 10, 10, gpu={"a100": 1}), cap) == 0

    def test_too_big_is_zero(self):
        assert fits_per_bm(R(100, 10, 10), BM) == 0


# ===========================================================================
# 2. Compiler — demand → ResourceRequirement + rules (pure)
# ===========================================================================

class TestCompile:

    def _one(self, req):
        c = compile_bm_demands(req)
        assert not c.errors, c.errors
        return c

    def test_bm_equivalents_use_machine_count_semantics(self):
        c = self._one(request([D("A1", "third", n=11)]))
        step = c.groups[0].steps[0]
        r = step.requirements[0]
        assert r.min_total_vms == r.max_total_vms == 33
        assert r.total_resources.cpu_cores == 33 * 30
        assert r.vm_specs == [R(30, 240, 2000)]
        assert c.groups[0].demands[0].fits_per_bm == 3
        assert c.groups[0].demands[0].bm_equivalents == 11
        rule = step.min_distinct_bm_rules[0]
        assert rule.min_distinct_bms == 6
        assert rule.selector.cluster_id == "A1" and rule.selector.node_role == "worker"

    def test_vm_count_form_derives_equivalents(self):
        a = self._one(request([D("A1", "half", count=22)]))
        b = self._one(request([D("A1", "half", n=11)]))
        assert a.groups[0].demands[0].bm_equivalents == 11
        assert a.groups[0].steps[0].requirements == b.groups[0].steps[0].requirements

    def test_inconsistent_basis_is_input_error(self):
        c = compile_bm_demands(request([D("A1", "half", n=11, count=20)]))
        assert any("make them agree" in e for e in c.errors)

    def test_count_below_min_distinct_is_input_error(self):
        """B1: 5 machines of full-size VMs can never span 6 machines —
        caught before any solve, with the fix spelled out."""
        c = compile_bm_demands(request([D("B1", "full", n=5)]))
        assert len(c.errors) == 1
        assert "cannot spread over 6 machines" in c.errors[0]
        r = plan_bm_demands(request([D("B1", "full", n=5)]))
        assert r.solver_status.startswith("INPUT_ERROR")

    def test_half_spec_makes_b1_feasible(self):
        c = self._one(request([D("B1", "half", n=5)]))
        assert c.groups[0].demands[0].vm_count == 10

    def test_non_tiling_spec_keeps_machine_count_and_warns(self):
        """odd40 on a 90-core machine: 2 per machine (80 of 90 cores).
        5 machines stay 5 machines (10 VMs); the 10-core-per-machine
        shortfall is an advisory, not an extra machine."""
        c = self._one(request([D("B1", "odd40", n=5)]))
        d = c.groups[0].demands[0]
        assert d.fits_per_bm == 2 and d.vm_count == 10 and d.bm_equivalents == 5
        kinds = [a for a in c.advisories if a["type"] == "spec_does_not_tile_bm"]
        cpu = next(a for a in kinds if a["dimension"] == "cpu_cores")
        assert cpu["per_machine_capacity"] == 90 and cpu["per_machine_allocated"] == 80

    def test_exclusive_cluster_forces_one_per_machine(self):
        c = self._one(request([D("C1", "half", n=6)], exclusive=["C1"]))
        d = c.groups[0].demands[0]
        assert d.exclusive and d.fits_per_bm == 1 and d.vm_count == 6
        step = c.groups[0].steps[0]
        assert step.exclusive_bm_rules[0].selector.cluster_id == "C1"
        assert any(a["type"] == "spec_does_not_tile_bm" for a in c.advisories)

    def test_spec_that_does_not_fit_is_input_error(self):
        req = request([D("A1", "full", n=1)])
        req = req.model_copy(update={"bm_specs": [BmSpec(name="bm", capacity=R(64, 256, 2000))]})
        c = compile_bm_demands(req)
        assert any("does not fit" in e for e in c.errors)

    def test_unknown_spec_and_duplicate_demand(self):
        c = compile_bm_demands(request([D("A1", "nope", n=1)]))
        assert any("unknown vm_spec" in e for e in c.errors)
        c = compile_bm_demands(request([D("A1", "third", n=1), D("A1", "third", n=2)]))
        assert any("duplicate demand" in e for e in c.errors)

    def test_groups_partition_clusters(self):
        req = request([D("A1", "third", n=6), D("B1", "half", n=3), D("C1", "full", n=6)],
                      groups=[TenancyGroup(name="shared", clusters=["A1", "B1"], bm_spec="bm"),
                              TenancyGroup(name="own", clusters=["C1"], bm_spec="bm")],
                      exclusive=["C1"])
        c = self._one(req)
        assert [g.name for g in c.groups] == ["shared", "own"]
        assert [s.name for s in c.groups[0].steps] == ["A1", "B1"]
        assert c.groups[0].fleet.pool == "shared"
        assert c.groups[0].fleet.id_prefix == "shared/bm-"
        assert c.groups[0].steps[0].requirements[0].pool == "shared"

    def test_cluster_in_two_groups_or_none_is_input_error(self):
        req = request([D("A1", "third", n=6), D("B1", "half", n=3)],
                      groups=[TenancyGroup(name="g1", clusters=["A1"], bm_spec="bm"),
                              TenancyGroup(name="g2", clusters=["A1"], bm_spec="bm")])
        errs = compile_bm_demands(req).errors
        assert any("exactly one tenancy group" in e for e in errs)
        assert any("in no tenancy group" in e for e in errs)

    def test_build_order_and_step_override(self):
        req = request([D("A1", "third", n=6), D("B1", "half", n=3),
                       D("A1", "third", n=3, role="master", ip="routable", step="B1")],
                      build_order=["B1", "A1"])
        c = self._one(req)
        steps = c.groups[0].steps
        assert [s.name for s in steps] == ["B1", "A1"]
        assert len(steps[0].requirements) == 2  # B1 workers + A1 masters
        bad = compile_bm_demands(req.model_copy(update={"build_order": ["Z"]}))
        assert any("unknown steps" in e for e in bad.errors)

    def test_zero_min_distinct_disables_rule(self):
        c = self._one(request([D("A1", "third", n=1)], min_distinct=0))
        assert c.groups[0].steps[0].min_distinct_bm_rules == []


# ===========================================================================
# 3. End to end — the three-cluster PM scenario
# ===========================================================================

def _three_clusters(**kw):
    return request(
        [
            D("A1", "third", n=11),
            D("A1", "third", count=3, role="master", min_distinct_bms=3),
            D("B1", "half", n=5, ip="non-routable"),
            D("C1", "full", n=6),
        ],
        groups=[TenancyGroup(name="shared", clusters=["A1", "B1"], bm_spec="bm"),
                TenancyGroup(name="c1-own", clusters=["C1"], bm_spec="bm")],
        exclusive=["C1"],
        **kw,
    )


@pytest.fixture(scope="module")
def plan():
    logging.disable(logging.CRITICAL)
    try:
        return plan_bm_demands(_three_clusters())
    finally:
        logging.disable(logging.NOTSET)


class TestPlanEndToEnd:

    def test_every_group_places_and_totals_add_up(self, plan):
        assert plan.success, plan.solver_status
        by = {g.name: g for g in plan.groups}
        assert by["c1-own"].required_baremetals == 6
        # A1 fills 12 machines (36 thirds); B1 needs 6 (C7) → 18, floor 17.
        assert by["shared"].required_baremetals == 18
        assert by["shared"].sizing.analytic_floor == 17
        assert plan.total_baremetals == 24

    def test_matrix_rows_meet_footprint_and_span_ags(self, plan):
        rows = {(r.cluster_id, r.node_role): r for r in plan.placement_matrix}
        assert set(rows) == {("A1", "worker"), ("A1", "master"), ("B1", "worker"), ("C1", "worker")}
        assert rows[("A1", "worker")].vm_count == 33 and rows[("A1", "worker")].distinct_bms >= 6
        assert rows[("A1", "master")].distinct_bms == 3
        assert rows[("B1", "worker")].vm_count == 10 and rows[("B1", "worker")].distinct_bms >= 6
        assert rows[("B1", "worker")].ip_type == "non-routable"
        c1 = rows[("C1", "worker")]
        assert c1.vm_count == c1.distinct_bms == 6
        for r in rows.values():
            assert set(r.per_ag) == {"ag-1", "ag-2", "ag-3"}

    def test_layouts_respect_capacity_and_pools(self, plan):
        assert plan.bm_layouts
        for l in plan.bm_layouts:
            assert l.used.fits_in(l.capacity)
            clusters = {e.cluster_id for e in l.entries}
            if l.pool == "c1-own":
                assert clusters == {"C1"} and len(l.entries) == 1
            else:
                assert "C1" not in clusters
        assert {l.bm_id.split("/")[0] for l in plan.bm_layouts} == {"shared", "c1-own"}

    def test_legacy_template_shape(self, plan):
        t = plan.legacy_template
        assert len(t) == len(plan.bm_layouts)
        c1_rows = [rows for bm, rows in t.items() if bm.startswith("c1-own/")]
        assert c1_rows and all(rows == [["worker", "full", "R"]] for rows in c1_rows)
        assert any(["worker", "half", "NR"] in rows for rows in t.values())

    def test_fixed_fleet_one_short_fails(self, plan):
        n = {g.name: g.required_baremetals for g in plan.groups}
        req = _three_clusters()
        req = req.model_copy(update={"groups": [
            TenancyGroup(name="shared", clusters=["A1", "B1"], bm_spec="bm", fleet_size=n["shared"] - 1),
            TenancyGroup(name="c1-own", clusters=["C1"], bm_spec="bm", fleet_size=n["c1-own"]),
        ]})
        r = plan_bm_demands(req)
        assert not r.success
        assert r.solver_status.startswith("shared:")
        assert next(g for g in r.groups if g.name == "c1-own").success

    def test_fixed_fleet_at_answer_succeeds(self, plan):
        n = {g.name: g.required_baremetals for g in plan.groups}
        req = _three_clusters()
        req = req.model_copy(update={"groups": [
            TenancyGroup(name="shared", clusters=["A1", "B1"], bm_spec="bm", fleet_size=n["shared"]),
            TenancyGroup(name="c1-own", clusters=["C1"], bm_spec="bm", fleet_size=n["c1-own"]),
        ]})
        r = plan_bm_demands(req)
        assert r.success, r.solver_status
        assert r.total_baremetals == plan.total_baremetals


class TestSumOfMachinesAlwaysFits:
    """ADR-020 D1 corollary: with exact-fraction specs, no footprint floor
    and no AG spreading, N machines per cluster always suffice — each
    cluster can take its own machines, so the solver can never do worse.
    C3 is the one constraint that breaks this (next class)."""

    @pytest.mark.parametrize("seed", range(4))
    def test_fixed_fleet_of_sum_n_places(self, seed):
        import random
        rnd = random.Random(seed)
        names = ["full", "half", "third"]
        demands, total = [], 0
        for i in range(rnd.randint(2, 4)):
            n = rnd.randint(1, 4)
            demands.append(D(f"c{i}", rnd.choice(names), n=n))
            total += n
        req = request(demands, groups=[TenancyGroup(name="p", clusters=[d.cluster_id for d in demands],
                                                    bm_spec="bm", fleet_size=total)],
                      min_distinct=0,
                      config=SolverConfig(max_solve_time_seconds=10, w_headroom=0,
                                          auto_generate_anti_affinity=False))
        r = plan_bm_demands(req)
        assert r.success, r.solver_status


class TestAgSpreadAdvisory:
    """1 machine of thirds = 3 VMs; auto C3 over 3 AGs caps 1 per AG, so
    3 machines are needed. The compiler warns; the solver confirms."""

    def test_advisory_and_actual_requirement(self):
        req = request([D("A1", "third", n=1)], min_distinct=0)
        c = compile_bm_demands(req)
        adv = [a for a in c.advisories if a["type"] == "ag_spread_exceeds_equivalents"]
        assert adv and adv[0]["machines_needed_for_spread"] == 3
        r = plan_bm_demands(req)
        assert r.success and r.total_baremetals == 3

    def test_no_advisory_when_spread_fits_in_n(self):
        c = compile_bm_demands(request([D("A1", "third", n=3)], min_distinct=0))
        assert not any(a["type"] == "ag_spread_exceeds_equivalents" for a in c.advisories)


class TestFootprintAdvisory:
    """B1: 5 machines of halves (10 VMs) under a ≥6-machine policy. Legal,
    but the plan will use 6 machines, not 5 — say so before solving."""

    def test_advisory_and_actual_requirement(self):
        req = request([D("B1", "half", n=5)])
        c = compile_bm_demands(req)
        adv = [a for a in c.advisories if a["type"] == "footprint_exceeds_equivalents"]
        assert adv and adv[0]["machines_needed_for_footprint"] == 6
        assert adv[0]["bm_equivalents"] == 5
        r = plan_bm_demands(req)
        assert r.success and r.total_baremetals == 6

    def test_no_advisory_when_n_meets_policy(self):
        c = compile_bm_demands(request([D("B1", "half", n=6)]))
        assert not any(a["type"] == "footprint_exceeds_equivalents" for a in c.advisories)

    def test_exclusive_cluster_is_input_error_not_advisory(self):
        c = compile_bm_demands(request([D("C1", "full", n=5)], exclusive=["C1"]))
        assert c.errors and not any(
            a["type"] == "footprint_exceeds_equivalents" for a in c.advisories)


class TestEndpoints:

    def test_compile_endpoint_returns_groups_or_400(self, client):
        body = json.loads(request([D("A1", "third", n=6)]).model_dump_json())
        resp = client.post("/v1/placement/bm-demand/compile", json=body)
        assert resp.status_code == 200
        assert resp.json()[0]["steps"][0]["requirements"][0]["min_total_vms"] == 18
        bad = json.loads(request([D("B1", "full", n=5)]).model_dump_json())
        resp = client.post("/v1/placement/bm-demand/compile", json=bad)
        assert resp.status_code == 400
        assert "cannot spread" in resp.json()["detail"]["input_errors"][0]

    def test_plan_endpoint(self, client):
        body = json.loads(request([D("A1", "third", n=2)], min_distinct=0).model_dump_json())
        resp = client.post("/v1/placement/bm-demand/plan", json=body)
        assert resp.status_code == 200
        data = resp.json()
        # 6 thirds over 3 AGs → 3 machines, not 2 (see TestAgSpreadAdvisory)
        assert data["success"] and data["total_baremetals"] == 3
        assert data["placement_matrix"][0]["vm_count"] == 6
        assert any(a["type"] == "ag_spread_exceeds_equivalents" for a in data["advisories"])


class TestExample:
    """examples/bm_demand/*.json double as UI templates: `_name` /
    `_description` ride along and pydantic ignores them."""

    EX = Path(__file__).parent.parent / "examples" / "bm_demand"

    def test_pm_three_clusters_example(self):
        req = BmDemandPlanRequest.model_validate_json((self.EX / "pm_three_clusters.json").read_text())
        c = compile_bm_demands(req)
        assert not c.errors, c.errors
        assert {g.name for g in c.groups} == {"shared", "c1-own"}

    def test_single_pool_example_plans(self):
        req = BmDemandPlanRequest.model_validate_json((self.EX / "single_pool_two_clusters.json").read_text())
        r = plan_bm_demands(req)
        assert r.success, r.solver_status
        assert [g.name for g in r.groups] == ["shared"]
        assert all(row.distinct_bms >= 6 for row in r.placement_matrix)

    def test_examples_api_hint(self, client):
        items = client.get("/api/examples").json()
        hints = {i["name"]: i["endpoint_hint"] for i in items}
        assert hints["bm_demand/pm_three_clusters.json"] == "bm-demand"
