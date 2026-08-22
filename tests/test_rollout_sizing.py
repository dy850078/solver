"""
Test suite — rollout sizing (app/rollout_sizing.py, app/sizing_floors.py).
Run: pytest tests/test_rollout_sizing.py -v
"""

from app.models import (
    AntiAffinityRule,
    ExclusiveBaremetalRule,
    FailoverRule,
    FleetTemplate,
    GroupSelector,
    MaxPerBaremetalRule,
    NodeRole,
    ResourceRequirement,
    Resources,
    RolloutRequest,
    RolloutSizingRequest,
    RolloutStep,
    SolverConfig,
    VM,
)
from app.rollout import solve_rollout
from app.rollout_sizing import build_fleet, per_ag_counts, size_rollout
from app.sizing_floors import fleet_floor

GiB = 1024
BM = Resources(cpu_cores=64, memory_mib=256 * GiB, storage_gb=2000)


def vm(vm_id, cpu=8, mem=32, disk=200, role="worker", cluster="c1",
       ip_type="routable", **kw):
    return VM(
        id=vm_id,
        demand=Resources(cpu_cores=cpu, memory_mib=mem * GiB, storage_gb=disk),
        node_role=role, cluster_id=cluster, ip_type=ip_type, **kw,
    )


def sizing(steps, fleet=None, **cfg):
    defaults = dict(max_solve_time_seconds=10, auto_generate_anti_affinity=False)
    defaults.update(cfg)
    return size_rollout(RolloutSizingRequest(
        fleet=fleet or FleetTemplate(total_capacity=BM, racks=3, ags=3),
        steps=steps,
        config=SolverConfig(**defaults),
    ))


def rollout_at(steps, n, fleet=None, **cfg):
    """Run the rollout on a fleet of exactly n machines (the brute-force
    oracle the search is checked against)."""
    defaults = dict(max_solve_time_seconds=10, auto_generate_anti_affinity=False)
    defaults.update(cfg)
    template = fleet or FleetTemplate(total_capacity=BM, racks=3, ags=3)
    bms = build_fleet(template, n)
    ids = [b.id for b in bms]
    filled = [
        s.model_copy(update={
            "vms": [v.model_copy(update={"candidate_baremetals": ids}) for v in s.vms],
            "requirements": [r.model_copy(update={"candidate_baremetals": ids})
                             for r in s.requirements],
        })
        for s in steps
    ]
    return solve_rollout(RolloutRequest(
        baremetals=bms, steps=filled, config=SolverConfig(**defaults),
    ))


# ===========================================================================
# 1. Fleet generation from topology counts
# ===========================================================================

class TestFleetGeneration:

    def test_racks_fan_out_over_dimensions(self):
        f = FleetTemplate(total_capacity=BM, rooms=2, racks=6, ags=3)
        bms = build_fleet(f, 6)
        assert [b.topology.rack for b in bms] == [f"rack-{i}" for i in range(1, 7)]
        assert [b.topology.ag for b in bms] == [
            "ag-1", "ag-2", "ag-3", "ag-1", "ag-2", "ag-3",
        ]
        assert [b.topology.room for b in bms] == [
            "room-1", "room-2", "room-1", "room-2", "room-1", "room-2",
        ]

    def test_machines_round_robin_over_racks(self):
        f = FleetTemplate(total_capacity=BM, racks=3, ags=3)
        bms = build_fleet(f, 7)
        per_rack = {}
        for b in bms:
            per_rack[b.topology.rack] = per_rack.get(b.topology.rack, 0) + 1
        assert per_rack == {"rack-1": 3, "rack-2": 2, "rack-3": 2}

    def test_per_ag_balanced_within_one(self):
        for n in range(1, 20):
            for ags in (2, 3, 4):
                f = FleetTemplate(total_capacity=BM, racks=ags, ags=ags)
                counts = list(per_ag_counts(build_fleet(f, n)).values())
                assert max(counts) - min(counts) <= 1, (n, ags, counts)

    def test_ids_stable_across_sizes(self):
        f = FleetTemplate(total_capacity=BM, racks=3, ags=3)
        small = build_fleet(f, 4)
        big = build_fleet(f, 6)
        assert [b.id for b in small] == [b.id for b in big[:4]]
        assert [b.topology.ag for b in small] == [b.topology.ag for b in big[:4]]

    def test_collapsed_dimensions_are_single_buckets(self):
        f = FleetTemplate(total_capacity=BM, racks=4, ags=1)
        bms = build_fleet(f, 4)
        assert {b.topology.ag for b in bms} == {"ag-1"}
        assert {b.topology.site for b in bms} == {"site-1"}


# ===========================================================================
# 2. Analytic floors — must never over-estimate
# ===========================================================================

class TestSizingFloors:

    def test_capacity_floor_counts_whole_vms(self):
        """9 VMs of 8c/32g: 8 fit per 64c/256g machine → 2 machines."""
        steps = [RolloutStep(name="s", vms=[vm(f"v{i}") for i in range(9)])]
        floor, parts = fleet_floor(steps, BM, SolverConfig(), ags=1)
        assert parts["capacity"] == 2
        assert floor == 2

    def test_ags_floor(self):
        steps = [RolloutStep(name="s", vms=[vm("v0")])]
        floor, parts = fleet_floor(steps, BM, SolverConfig(), ags=5)
        assert parts["ags"] == 5
        assert floor == 5

    def test_headcount_floor_from_max_per_bm(self):
        rule = MaxPerBaremetalRule(
            group_id="g", selector=GroupSelector(cluster_id="c1"), max_per_bm=2)
        steps = [RolloutStep(name="s", vms=[vm(f"v{i}") for i in range(9)],
                             max_per_bm_rules=[rule])]
        floor, parts = fleet_floor(steps, BM, SolverConfig(), ags=1)
        assert parts["headcount"] == 5   # ceil(9/2)
        assert floor == 5

    def test_solo_floor_is_additive(self):
        """Exclusive machines serve nobody else, so they add to the rest."""
        excl = ExclusiveBaremetalRule(group_id="e", vm_ids=["f0", "f1", "f2"])
        steps = [RolloutStep(
            name="s",
            vms=[vm(f"f{i}", role="f5") for i in range(3)]
                + [vm(f"v{i}") for i in range(9)],
            exclusive_bm_rules=[excl])]
        floor, parts = fleet_floor(steps, BM, SolverConfig(), ags=1)
        assert parts["solo"] == 3
        assert parts["capacity"] == 2    # the 9 non-exclusive VMs
        assert floor == 5                # 3 + 2, not max(3, 2)

    def test_pack_floor_for_oversized_vms(self):
        """40c on a 64c machine: two never share, so one machine each."""
        steps = [RolloutStep(name="s", vms=[vm(f"v{i}", cpu=40) for i in range(4)])]
        floor, parts = fleet_floor(steps, BM, SolverConfig(), ags=1)
        assert parts["pack"] == 4
        assert floor == 4

    def test_requirement_floor_uses_largest_spec(self):
        """A requirement's spec is the splitter's choice, so the floor must
        assume the most favourable (largest) one."""
        req = ResourceRequirement(
            total_resources=Resources(cpu_cores=64, memory_mib=256 * GiB,
                                      storage_gb=1600),
            node_role="worker", cluster_id="c1",
            vm_specs=[Resources(cpu_cores=8, memory_mib=32 * GiB, storage_gb=200),
                      Resources(cpu_cores=32, memory_mib=128 * GiB, storage_gb=800)],
        )
        steps = [RolloutStep(name="s", requirements=[req])]
        floor, parts = fleet_floor(steps, BM, SolverConfig(), ags=1)
        # 2 VMs at the 32c spec, both fit one machine
        assert parts["capacity"] == 1
        assert floor == 1

    def test_floor_never_exceeds_the_real_answer(self):
        """The soundness guard: for a spread of fixtures the floor must be
        ≤ the size the search actually settles on."""
        fixtures = [
            [RolloutStep(name="s", vms=[vm(f"v{i}") for i in range(5)])],
            [RolloutStep(name="s", vms=[vm(f"v{i}", cpu=40) for i in range(3)])],
            [RolloutStep(name="a", vms=[vm(f"a{i}") for i in range(4)]),
             RolloutStep(name="b", vms=[vm(f"b{i}", cpu=32, mem=128) for i in range(3)])],
        ]
        for steps in fixtures:
            r = sizing(steps)
            assert r.success, r.solver_status
            assert r.analytic_floor <= r.required_baremetals


# ===========================================================================
# 3. Search behaviour
# ===========================================================================

class TestSizingSearch:

    def test_floor_hit_returns_immediately(self):
        steps = [RolloutStep(name="s", vms=[vm(f"v{i}") for i in range(6)])]
        r = sizing(steps)
        assert r.success
        assert r.required_baremetals == r.analytic_floor
        assert len(r.probes) == 1

    def test_answer_is_minimal(self):
        """One fewer machine must actually fail — otherwise 'minimum' lies."""
        steps = [
            RolloutStep(name="a", vms=[vm(f"a{i}", cpu=40) for i in range(2)]),
            RolloutStep(name="b", vms=[vm(f"b{i}", cpu=40) for i in range(2)]),
        ]
        r = sizing(steps)
        assert r.success, r.solver_status
        n = r.required_baremetals
        assert not rollout_at(steps, n - 1).success
        assert rollout_at(steps, n).success

    def test_escalates_past_a_short_floor(self):
        """max_per_bm=1 on a group the floor cannot see through: the scan
        climbs until the fleet is wide enough."""
        rule = MaxPerBaremetalRule(
            group_id="g", selector=GroupSelector(node_role="master"), max_per_bm=1)
        steps = [
            RolloutStep(name="a",
                        vms=[vm(f"m{i}", role="master") for i in range(3)],
                        max_per_bm_rules=[rule]),
            RolloutStep(name="b",
                        vms=[vm(f"m2{i}", role="master") for i in range(3)],
                        max_per_bm_rules=[rule]),
        ]
        r = sizing(steps)
        assert r.success, r.solver_status
        assert r.required_baremetals >= 6   # six masters, one per machine
        assert not rollout_at(steps, r.required_baremetals - 1).success

    def test_probe_trail_is_recorded(self):
        steps = [RolloutStep(name="s", vms=[vm(f"v{i}", cpu=40) for i in range(5)])]
        r = sizing(steps)
        assert r.success
        assert [p.baremetals for p in r.probes] == sorted(p.baremetals for p in r.probes)
        assert r.probes[-1].success
        assert all(not p.success for p in r.probes[:-1])

    def test_budget_exhaustion_reports_bounds(self):
        rule = MaxPerBaremetalRule(
            group_id="g", selector=GroupSelector(node_role="master"), max_per_bm=1)
        steps = [RolloutStep(name="s",
                             vms=[vm(f"m{i}", role="master") for i in range(20)],
                             max_per_bm_rules=[rule])]
        r = size_rollout(RolloutSizingRequest(
            fleet=FleetTemplate(total_capacity=BM, racks=3, ags=3),
            steps=steps,
            config=SolverConfig(max_solve_time_seconds=5,
                                auto_generate_anti_affinity=False),
            max_baremetals=5,
        ))
        assert not r.success
        assert r.solver_status.startswith("BUDGET_EXHAUSTED")
        assert r.lower_bound >= 1
        assert r.upper_bound is None


# ===========================================================================
# 4. Non-monotonicity: why the scan is linear (ADR-014)
# ===========================================================================

class TestSizingNonMonotonic:

    def test_ascending_scan_returns_the_true_minimum(self):
        """Feasibility is not monotone in fleet size, so a bisect could
        land anywhere. The ascending scan's answer must be exactly the
        smallest size that really works — verified against brute force."""
        steps = [
            RolloutStep(name="seed", vms=[vm("s0", cpu=8)]),
            RolloutStep(name="big", vms=[vm(f"b{i}", cpu=40) for i in range(3)]),
        ]
        r = sizing(steps, auto_generate_anti_affinity=True)
        assert r.success, r.solver_status
        n = r.required_baremetals
        # brute force: every smaller fleet must fail
        for smaller in range(1, n):
            assert not rollout_at(steps, smaller,
                                  auto_generate_anti_affinity=True).success, (
                f"{smaller} machines worked, so {n} is not the minimum"
            )


# ===========================================================================
# 5. Pre-flight: reject what no fleet size could fix
# ===========================================================================

class TestSizingPreflight:

    def _err(self, result):
        assert not result.success
        assert result.solver_status.startswith("INPUT_ERROR")
        return result.solver_status

    def test_vm_larger_than_machine_model(self):
        steps = [RolloutStep(name="s", vms=[vm("v0", cpu=128)])]
        assert "does not fit the machine model" in self._err(sizing(steps))

    def test_preset_candidates_rejected(self):
        steps = [RolloutStep(name="s",
                             vms=[vm("v0", candidate_baremetals=["bm-001"])])]
        assert "candidate_baremetals" in self._err(sizing(steps))

    def test_pinned_vm_rejected(self):
        steps = [RolloutStep(name="s", vms=[vm("v0", pinned_to="bm-001")])]
        assert "greenfield" in self._err(sizing(steps))

    def test_requirement_network_mismatch(self):
        req = ResourceRequirement(
            total_resources=Resources(cpu_cores=16, memory_mib=64 * GiB,
                                      storage_gb=400),
            node_role="worker", cluster_id="c1", network="bgp-x",
            vm_specs=[Resources(cpu_cores=8, memory_mib=32 * GiB, storage_gb=200)],
        )
        steps = [RolloutStep(name="s", requirements=[req])]
        assert "network" in self._err(sizing(steps))

    def test_failover_on_collapsed_dimension(self):
        f = FailoverRule(
            rule_id="fo",
            primary=GroupSelector(node_role=NodeRole.MASTER),
            backup=GroupSelector(node_role=NodeRole.LEARNER),
            fault_domain="room",
        )
        steps = [RolloutStep(
            name="s",
            vms=[vm("m0", role=NodeRole.MASTER), vm("l0", role=NodeRole.LEARNER)],
            failover_rules=[f])]
        # rooms defaults to 1 → the whole fleet is one bucket
        assert "collapses to one bucket" in self._err(sizing(steps))

    def test_spec_does_not_fit_machine_model(self):
        req = ResourceRequirement(
            total_resources=Resources(cpu_cores=256, memory_mib=1024 * GiB,
                                      storage_gb=4000),
            node_role="worker", cluster_id="c1",
            vm_specs=[Resources(cpu_cores=128, memory_mib=512 * GiB,
                                storage_gb=1000)],
        )
        steps = [RolloutStep(name="s", requirements=[req])]
        assert "fits the machine model" in self._err(sizing(steps))


# ===========================================================================
# 6. Multi-cluster with different spec mixes (the headline scenario)
# ===========================================================================

class TestSizingMultiCluster:

    def test_three_clusters_each_with_its_own_specs(self):
        steps = [
            RolloutStep(name="cluster-a", vms=(
                [vm(f"a-m{i}", 8, 32, 200, "master", "cluster-a") for i in range(3)]
                + [vm(f"a-w{i}", 16, 64, 400, "worker", "cluster-a") for i in range(2)])),
            RolloutStep(name="cluster-b", vms=(
                [vm(f"b-m{i}", 4, 16, 100, "master", "cluster-b") for i in range(3)]
                + [vm(f"b-w{i}", 32, 128, 800, "worker", "cluster-b") for i in range(3)])),
            RolloutStep(name="cluster-c", vms=(
                [vm(f"c-m{i}", 8, 32, 200, "master", "cluster-c") for i in range(3)])),
        ]
        r = sizing(steps, auto_generate_anti_affinity=True)
        assert r.success, r.solver_status
        assert r.rollout is not None
        assert [rep.name for rep in r.rollout.reports] == [
            "cluster-a", "cluster-b", "cluster-c",
        ]
        assert sum(r.per_ag.values()) == r.required_baremetals
        assert not rollout_at(steps, r.required_baremetals - 1,
                              auto_generate_anti_affinity=True).success


class TestRolloutSizingEndpoint:

    def test_post_size(self, client):
        body = RolloutSizingRequest(
            fleet=FleetTemplate(total_capacity=BM, racks=3, ags=3),
            steps=[RolloutStep(name="s", vms=[vm(f"v{i}") for i in range(4)])],
            config=SolverConfig(max_solve_time_seconds=10,
                                auto_generate_anti_affinity=False),
        ).model_dump(mode="json")
        resp = client.post("/v1/placement/rollout/size", json=body)
        assert resp.status_code == 200
        out = resp.json()
        assert out["success"] is True
        assert out["required_baremetals"] >= 1
        assert len(out["baremetals"]) == out["required_baremetals"]
        import re
        assert re.fullmatch(r"[0-9a-f]{12}", out["config_fingerprint"])
