"""
Test suite — procurement sizing (capacity planning Phase 2).
Run: pytest tests/test_capacity_planner.py -v
"""

from app.models import (
    AntiAffinityRule,
    BaremetalType,
    CommittedStock,
    GroupSelector,
    NodeRole,
    PlacementRequest,
    ProcurementCap,
    ProcurementRequest,
    ResourceRequirement,
    Resources,
    SolverConfig,
    SplitPlacementRequest,
    VM,
)
from app.capacity_planner import solve_capacity_plan
from app.solver import VMPlacementSolver
from app.split_solver import solve_split_placement

from .conftest import make_bm, make_vm


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_req(cpu=0, mem=0, disk=0, pods=0, spec=None, role=NodeRole.WORKER,
             network=""):
    return ResourceRequirement(
        total_resources=Resources(cpu_cores=cpu, memory_mib=mem, storage_gb=disk),
        node_role=role,
        vm_specs=[spec] if spec else None,
        total_pods=pods,
        network=network,
    )


def make_type(type_id, cpu, mem, disk):
    return BaremetalType(
        type_id=type_id,
        capacity=Resources(cpu_cores=cpu, memory_mib=mem, storage_gb=disk),
    )


def procure(requirements, in_stock, types, caps=None, committed=None,
            vms=None, rules=None, **cfg):
    defaults = dict(max_solve_time_seconds=10, auto_generate_anti_affinity=False)
    defaults.update(cfg)
    reqs = requirements if isinstance(requirements, list) else [requirements]
    return solve_capacity_plan(ProcurementRequest(
        requirements=reqs,
        vms=vms or [],
        in_stock=in_stock,
        procurement_types=types,
        procurement_caps=caps or [],
        committed_stock=committed or [],
        anti_affinity_rules=rules or [],
        config=SolverConfig(**defaults),
    ))


SPEC_8 = Resources(cpu_cores=8, memory_mib=16_000, storage_gb=100)


# ===========================================================================
# Core
# ===========================================================================

class TestProcurement:

    def test_no_procurement_when_in_stock_enough(self):
        """2×64-core BMs already cover a 32-core (4×8) demand → buy nothing."""
        in_stock = [make_bm(f"bm-{i}", cpu=64, mem=256_000, disk=2000) for i in range(2)]
        types = [make_type("big", 64, 256_000, 2000)]
        req = make_req(cpu=32, spec=SPEC_8)

        r = procure(req, in_stock, types)

        assert r.success, r.solver_status
        assert r.procured_bm_total == 0
        assert r.procurement == []
        assert r.in_stock_bm_used >= 1
        assert r.shortfall_cause == "none"

    def test_buys_when_in_stock_insufficient(self):
        """
        One tiny 16-core BM (holds 2×8) but 64-core (8×8) demand → the other 6
        VMs need a bought BM.
        """
        in_stock = [make_bm("bm-1", cpu=16, mem=256_000, disk=2000)]
        types = [make_type("big", 64, 256_000, 2000)]
        req = make_req(cpu=64, spec=SPEC_8)

        r = procure(req, in_stock, types)

        assert r.success, r.solver_status
        assert r.procured_bm_total >= 1
        assert sum(d.count for d in r.procurement) == r.procured_bm_total
        assert {d.type_id for d in r.procurement} == {"big"}

    def test_prefers_in_stock_over_buying(self):
        """In-stock capacity should be filled before any BM is bought."""
        in_stock = [make_bm("bm-1", cpu=64, mem=256_000, disk=2000)]
        types = [make_type("big", 64, 256_000, 2000)]
        # 64-core demand fits exactly in the one in-stock BM.
        req = make_req(cpu=64, spec=SPEC_8)

        r = procure(req, in_stock, types)

        assert r.success
        assert r.procured_bm_total == 0

    def test_multi_type_picks_fewest_bms(self):
        """
        Given a small (16c) and a big (64c) type, cover a 64-core residual with
        the single big BM rather than four small ones (minimize buy count).
        """
        in_stock = [make_bm("bm-1", cpu=8, mem=256_000, disk=2000)]  # holds 1×8
        types = [
            make_type("small", 16, 64_000, 400),
            make_type("big", 64, 256_000, 2000),
        ]
        req = make_req(cpu=64, spec=SPEC_8)

        r = procure(req, in_stock, types)

        assert r.success, r.solver_status
        assert r.procured_bm_total == 1
        assert r.procurement == [] or {d.type_id for d in r.procurement} == {"big"}

    def test_max_bm_cap_causes_space_shortfall(self):
        """
        Demand needs a bought BM, but the bucket's max_bm=0 forbids it →
        success=False with shortfall_cause='space'.
        """
        in_stock = [make_bm("bm-1", cpu=16, mem=256_000, disk=2000, ag="ag-1")]
        types = [make_type("big", 64, 256_000, 2000)]
        caps = [ProcurementCap(bucket="ag-1", max_bm=0)]
        req = make_req(cpu=64, spec=SPEC_8)

        r = procure(req, in_stock, types, caps=caps)

        assert not r.success
        assert r.shortfall_cause == "space"

    def test_max_bm_cap_allows_when_sufficient(self):
        """A cap that still leaves enough slots succeeds normally."""
        in_stock = [make_bm("bm-1", cpu=16, mem=256_000, disk=2000, ag="ag-1")]
        types = [make_type("big", 64, 256_000, 2000)]
        caps = [ProcurementCap(bucket="ag-1", max_bm=2)]
        req = make_req(cpu=64, spec=SPEC_8)

        r = procure(req, in_stock, types, caps=caps)

        assert r.success, r.solver_status
        assert 1 <= r.procured_bm_total <= 2

    def test_no_types_is_input_error(self):
        in_stock = [make_bm("bm-1", cpu=16, mem=256_000, disk=2000)]
        req = make_req(cpu=64, spec=SPEC_8)

        r = procure(req, in_stock, [])

        assert not r.success
        assert r.solver_status.startswith("INPUT_ERROR")

    def test_pod_floor_drives_procurement(self):
        """
        Resources fit in-stock, but 'at least 300 pods' at 110/node forces 3
        nodes; the tiny in-stock BM can't hold them all → buy the rest.
        """
        in_stock = [make_bm("bm-1", cpu=8, mem=16_000, disk=100)]  # holds 1 node
        types = [make_type("big", 64, 256_000, 2000)]
        req = make_req(cpu=8, spec=SPEC_8, pods=300)

        r = procure(req, in_stock, types, max_pods_per_node=110)

        assert r.success, r.solver_status
        nodes = sum(d.count for d in r.split_decisions)
        assert nodes == 3
        assert r.procured_bm_total >= 1


# ===========================================================================
# max_bm across machine types (bm_group_caps)
# ===========================================================================

class TestSlotCapAcrossTypes:

    def test_cap_counts_all_types_together(self):
        """
        Bucket cap max_bm=1 with two types. Demand (80c) is only coverable by
        big(64)+small(16) = 2 machines — which the 1-slot cap forbids. A
        per-type cap (the old bug) would wrongly allow 1+1. Expect `space`.
        """
        types = [make_type("small", 16, 64_000, 400),
                 make_type("big", 64, 256_000, 2000)]
        caps = [ProcurementCap(bucket="ag-1", max_bm=1)]
        req = make_req(cpu=80, spec=SPEC_8)

        r = procure(req, [], types, caps=caps)

        assert not r.success
        assert r.shortfall_cause == "space"

    def test_cap_shared_by_committed_and_buys(self):
        """Committed machines occupy slots too: cap=1 + 1 committed used → no buy fits."""
        types = [make_type("big", 64, 256_000, 2000)]
        caps = [ProcurementCap(bucket="ag-1", max_bm=1)]
        committed = [CommittedStock(type_id="big", count=1, bucket="ag-1")]
        req = make_req(cpu=128, spec=SPEC_8)  # needs 2 machines

        r = procure(req, [], types, caps=caps, committed=committed)

        assert not r.success
        assert r.shortfall_cause == "space"


# ===========================================================================
# Committed stock (缺口 3h)
# ===========================================================================

class TestCommittedStock:

    def test_committed_drained_before_buying(self):
        """Owned machines cover the residual → draw from them, buy nothing."""
        in_stock = [make_bm("bm-1", cpu=8, mem=16_000, disk=100)]
        types = [make_type("big", 64, 256_000, 2000)]
        committed = [CommittedStock(type_id="big", count=2)]  # floating
        req = make_req(cpu=72, spec=SPEC_8)  # 9 VMs: 1 in-stock + 8 on big

        r = procure(req, in_stock, types, committed=committed)

        assert r.success, r.solver_status
        assert r.procured_bm_total == 0
        assert r.committed_bm_used >= 1
        assert sum(d.count for d in r.committed_used) == r.committed_bm_used

    def test_committed_insufficient_buys_the_rest(self):
        """1 owned big + demand for 2 bigs → use the owned one, buy 1 more."""
        types = [make_type("big", 64, 256_000, 2000)]
        committed = [CommittedStock(type_id="big", count=1)]
        req = make_req(cpu=128, spec=SPEC_8)

        r = procure(req, [], types, committed=committed)

        assert r.success, r.solver_status
        assert r.committed_bm_used == 1
        assert r.procured_bm_total == 1

    def test_floating_pool_not_double_counted(self):
        """
        A floating pool of 1 is copied into both buckets, but at most 1 total
        may be used across buckets.
        """
        in_stock = [
            make_bm("bm-1", cpu=8, mem=16_000, disk=100, ag="ag-1"),
            make_bm("bm-2", cpu=8, mem=16_000, disk=100, ag="ag-2"),
        ]
        types = [make_type("big", 64, 256_000, 2000)]
        committed = [CommittedStock(type_id="big", count=1)]
        req = make_req(cpu=144, spec=SPEC_8)  # 18 VMs: 2 in-stock + 16 → 2 bigs

        r = procure(req, in_stock, types, committed=committed)

        assert r.success, r.solver_status
        assert r.committed_bm_used == 1  # pool cap respected
        assert r.procured_bm_total == 1  # the second big is bought

    def test_committed_unknown_type_is_input_error(self):
        types = [make_type("big", 64, 256_000, 2000)]
        committed = [CommittedStock(type_id="nonexistent", count=1)]
        req = make_req(cpu=8, spec=SPEC_8)

        r = procure(req, [make_bm("bm-1")], types, committed=committed)

        assert not r.success
        assert r.solver_status.startswith("INPUT_ERROR")


# ===========================================================================
# Network (BGP) scoping (缺口 3g)
# ===========================================================================

class TestNetworkScoping:

    def test_requirement_confined_to_its_network(self):
        """
        A bgp1 cluster must not land on the roomy bgp2 in-stock BM; it buys
        into the bgp1 cell instead.
        """
        in_stock = [make_bm("bm-2", cpu=64, mem=256_000, disk=2000, ag="ag-1")]
        in_stock[0].network = "bgp2"
        types = [make_type("big", 64, 256_000, 2000)]
        caps = [ProcurementCap(bucket="ag-1", network="bgp1", max_bm=2)]
        req = make_req(cpu=32, spec=SPEC_8, network="bgp1")

        r = procure(req, in_stock, types, caps=caps)

        assert r.success, r.solver_status
        assert r.in_stock_bm_used == 0  # bgp2 BM untouched
        assert r.procured_bm_total == 1

    def test_no_network_uses_anything(self):
        """A requirement without a network uses any in-stock BM."""
        in_stock = [make_bm("bm-2", cpu=64, mem=256_000, disk=2000)]
        in_stock[0].network = "bgp2"
        types = [make_type("big", 64, 256_000, 2000)]
        req = make_req(cpu=32, spec=SPEC_8)

        r = procure(req, in_stock, types)

        assert r.success
        assert r.in_stock_bm_used == 1
        assert r.procured_bm_total == 0


# ===========================================================================
# Balance objective (決議 #11) and health gauges (缺口 3c)
# ===========================================================================

class TestBalanceAndGauges:

    def test_balance_buys_into_emptier_bucket(self):
        """
        ag-1 has a free 64c BM, ag-2's BM is fully used. Buying is needed for
        96c of demand; with the balance term on, the bought BM lands in ag-2
        (topping up the emptier bucket) rather than ag-1.
        """
        in_stock = [
            make_bm("bm-1", cpu=64, mem=256_000, disk=2000, ag="ag-1"),
            make_bm("bm-2", cpu=64, mem=256_000, disk=2000, ag="ag-2",
                    used_cpu=64, used_mem=256_000, used_disk=2000),
        ]
        types = [make_type("big", 64, 256_000, 2000)]
        req = make_req(cpu=96, spec=SPEC_8)

        r = procure(req, in_stock, types,
                    w_procurement_balance=5, w_headroom=0)

        assert r.success, r.solver_status
        assert r.procured_bm_total == 1
        bought = [a.baremetal_id for a in r.assignments
                  if a.baremetal_id.startswith("buy-")]
        assert bought and all("ag-2" in b for b in bought)

    def test_health_gauges(self):
        """
        4×8c VMs on a 64c/256G/2T BM → 32c/192G/1.6T left. With an
        8c/16G/100G reference spec that is 4 more slots; nothing is stranded.
        """
        in_stock = [make_bm("bm-1", cpu=64, mem=256_000, disk=2000)]
        types = [make_type("big", 64, 256_000, 2000)]
        req = make_req(cpu=32, spec=SPEC_8)

        r = procure(
            req, in_stock, types,
            reference_vm_spec=Resources(cpu_cores=8, memory_mib=16_000, storage_gb=100),
            min_useful_spec=Resources(cpu_cores=8, memory_mib=16_000, storage_gb=100),
        )

        assert r.success
        assert r.nominal_available.cpu_cores == 32
        assert r.remaining_node_slots == 4
        assert r.stranded_available is not None
        assert r.stranded_available.cpu_cores == 0
        assert r.balance_after.get("ag-1") == 32

    def test_gauges_absent_when_unconfigured(self):
        in_stock = [make_bm("bm-1", cpu=64, mem=256_000, disk=2000)]
        types = [make_type("big", 64, 256_000, 2000)]
        req = make_req(cpu=32, spec=SPEC_8)

        r = procure(req, in_stock, types)

        assert r.success
        assert r.remaining_node_slots is None
        assert r.stranded_available is None


# ===========================================================================
# Code-review regressions (findings #1–#10)
# ===========================================================================

class TestReviewFixes:

    def test_pod_floor_included_in_buyable_generation(self):
        """(#1) Pod floor forces 3 nodes; enough buyable BMs must be generated
        even though raw resources need only 1."""
        types = [make_type("small", 8, 16_000, 100)]
        req = make_req(cpu=8, spec=SPEC_8, pods=300)

        r = procure(req, [], types, max_pods_per_node=110)

        assert r.success, r.solver_status
        assert sum(d.count for d in r.split_decisions) == 3
        assert r.procured_bm_total == 3

    def test_fragmentation_included_in_buyable_generation(self):
        """(#1) 3×40c VMs need 3×64c BMs (one VM per BM); a naive
        ceil(120/64)=2 bound would under-generate → false shortfall."""
        types = [make_type("big", 64, 256_000, 2000)]
        spec40 = Resources(cpu_cores=40, memory_mib=64_000, storage_gb=500)
        req = make_req(cpu=120, spec=spec40)

        r = procure(req, [], types)

        assert r.success, r.solver_status
        assert r.procured_bm_total == 3

    def test_unhostable_requirement_is_input_error(self):
        """(#2) A spec that fits no in-stock BM and no buyable type must be an
        INPUT_ERROR, not success-with-no-purchase."""
        types = [make_type("big", 64, 256_000, 2000)]
        huge = Resources(cpu_cores=128, memory_mib=512_000, storage_gb=4000)
        req = make_req(cpu=128, spec=huge)

        r = procure(req, [make_bm("bm-1")], types)

        assert not r.success
        assert r.solver_status.startswith("INPUT_ERROR")

    def test_network_without_cell_is_input_error(self):
        """(#2) A network with no matching in-stock BM or cell must be an
        INPUT_ERROR, not a silent 'inventory sufficient'."""
        types = [make_type("big", 64, 256_000, 2000)]
        req = make_req(cpu=64, spec=SPEC_8, network="bgp1")

        r = procure(req, [make_bm("bm-1")], types)  # untagged in-stock only

        assert not r.success
        assert r.solver_status.startswith("INPUT_ERROR")

    def test_explicit_vm_candidates_are_authoritative(self):
        """(#3) A pinned VM must not be diluted onto virtual BMs — pinning to
        a full host fails instead of triggering a buy."""
        full = make_bm("bm-1", cpu=8, mem=16_000, disk=100,
                       used_cpu=8, used_mem=16_000, used_disk=100)
        types = [make_type("big", 64, 256_000, 2000)]
        vm = make_vm("vm-1", cpu=8, candidates=["bm-1"])

        r = procure([], [full], types, vms=[vm])

        assert not r.success
        assert all(not a.baremetal_id.startswith(("buy-", "own"))
                   for a in r.assignments)

    def test_anti_affinity_shortfall_classified(self):
        """(#4) A structurally anti-affinity-blocked plan (3 masters, cap 1
        per AG, only 2 AGs anywhere) must not be reported as 'capacity'."""
        in_stock = [make_bm("bm-1", ag="ag-1"), make_bm("bm-2", ag="ag-2")]
        types = [make_type("big", 64, 256_000, 2000)]
        req = make_req(cpu=24, spec=SPEC_8, role=NodeRole.MASTER)
        rule = AntiAffinityRule(
            group_id="masters",
            selector=GroupSelector(node_role=NodeRole.MASTER),
            spread_on=["ag"],
            cap_per_bucket={"ag": 1},
        )

        r = procure(req, in_stock, types, rules=[rule])

        assert not r.success
        assert r.shortfall_cause == "anti_affinity"

    def test_unknown_status_not_classified_as_space(self, monkeypatch):
        """(#5) A capped pass failing without proven INFEASIBLE (e.g. time
        limit → UNKNOWN) yields cause 'unknown' and skips the uncapped
        re-solve."""
        from app import capacity_planner as cp

        calls = []

        class FakeResult:
            success = False
            solver_status = "UNKNOWN"
            assignments = []
            diagnostics = {}

        class FakePass:
            result = FakeResult()
            buyable_type_of = {}
            committed_type_of = {}
            vm_demand = {}
            virtual_bms = {}
            splitter = None
            cp_solver = None
            dropped = []

        def fake_solve_once(request, *, use_caps):
            calls.append(use_caps)
            return FakePass()

        monkeypatch.setattr(cp, "_solve_once", fake_solve_once)
        r = cp.solve_capacity_plan(ProcurementRequest(
            requirements=[make_req(cpu=64, spec=SPEC_8)],
            in_stock=[make_bm("bm-1")],
            procurement_types=[make_type("big", 64, 256_000, 2000)],
            procurement_caps=[ProcurementCap(bucket="ag-1", max_bm=1)],
            config=SolverConfig(),
        ))

        assert not r.success
        assert r.shortfall_cause == "unknown"
        assert calls == [True]  # no second (uncapped) solve

    def test_bought_bms_get_unique_racks(self):
        """(#6) Rack-spread anti-affinity must not treat bought machines as
        colocated with the cell representative's rack."""
        in_stock = [make_bm("bm-1", ag="ag-1", rack="rack-1")]
        types = [make_type("big", 64, 256_000, 2000)]
        req = make_req(cpu=24, spec=SPEC_8, role=NodeRole.MASTER)
        rule = AntiAffinityRule(
            group_id="masters",
            selector=GroupSelector(node_role=NodeRole.MASTER),
            spread_on=["rack"],
            cap_per_bucket={"rack": 1},
        )

        r = procure(req, in_stock, types, rules=[rule])

        assert r.success, r.solver_status
        assert r.procured_bm_total == 2  # 1 on in-stock rack + 2 new racks

    def test_committed_copies_bounded_by_cell_cap(self):
        """(#7) Floating committed copies per cell are bounded by the cell's
        slot cap instead of materializing count× per cell."""
        from app import capacity_planner as cp

        request = ProcurementRequest(
            requirements=[make_req(cpu=16, spec=SPEC_8)],
            in_stock=[make_bm("bm-1", ag="ag-1"), make_bm("bm-2", ag="ag-2")],
            procurement_types=[make_type("big", 64, 256_000, 2000)],
            procurement_caps=[ProcurementCap(bucket="ag-1", max_bm=1),
                              ProcurementCap(bucket="ag-2", max_bm=1)],
            committed_stock=[CommittedStock(type_id="big", count=100)],
            config=SolverConfig(auto_generate_anti_affinity=False),
        )

        p = cp._solve_once(request, use_caps=True)

        assert len(p.committed_type_of) <= 2  # 100 owned, 1 slot per cell

    def test_balance_ignores_virtual_only_buckets(self):
        """(#8) Buckets containing only virtual BMs must not join the balance
        objective (they would pin min to 0 and degenerate max−min)."""
        bms = [make_bm("real-1", ag="ag-1"), make_bm("real-2", ag="ag-2"),
               make_bm("virt-1", ag="ag-9")]
        vm = VM(id="vm-1",
                demand=Resources(cpu_cores=4, memory_mib=8_000, storage_gb=50),
                candidate_baremetals=["real-1", "real-2", "virt-1"])
        request = PlacementRequest(
            vms=[vm], baremetals=bms,
            config=SolverConfig(w_procurement_balance=5,
                                auto_generate_anti_affinity=False),
        )

        s = VMPlacementSolver(request)
        s.procurement_bm_ids = {"virt-1"}
        s._build_variables()
        assert s._compute_procurement_balance_terms()  # 2 real buckets → on

        s2 = VMPlacementSolver(request)
        s2.procurement_bm_ids = {"virt-1", "real-2"}
        s2._build_variables()
        assert s2._compute_procurement_balance_terms() == []  # 1 real bucket

    def test_pod_floor_with_zero_max_vms_is_infeasible(self):
        """(#9) max_total_vms=0 contradicting the pod floor must fail, not
        silently drop the requirement (sibling VM keeps the request non-empty
        so the NO_VMS guard doesn't mask the path)."""
        bm = make_bm("bm-1")
        bad = ResourceRequirement(
            total_resources=Resources(),
            vm_specs=[SPEC_8],
            total_pods=300,
            max_total_vms=0,
            candidate_baremetals=["bm-1"],
        )
        vm = make_vm("vm-1", candidates=["bm-1"])

        r = solve_split_placement(SplitPlacementRequest(
            requirements=[bad], vms=[vm], baremetals=[bm],
            config=SolverConfig(max_pods_per_node=110,
                                auto_generate_anti_affinity=False),
        ))

        assert not r.success

    def test_split_and_solve_honors_network(self):
        """(#10) The split-and-solve path narrows candidates to the
        requirement's network domain."""
        bm = make_bm("bm-x")
        bm.network = "bgp2"
        req = ResourceRequirement(
            total_resources=Resources(cpu_cores=8),
            vm_specs=[SPEC_8],
            network="bgp1",
            candidate_baremetals=["bm-x"],
        )
        r = solve_split_placement(SplitPlacementRequest(
            requirements=[req], baremetals=[bm],
            config=SolverConfig(auto_generate_anti_affinity=False),
        ))
        assert not r.success  # bgp2 host is not eligible for a bgp1 cluster

        bm2 = make_bm("bm-y")
        bm2.network = "bgp1"
        req2 = req.model_copy(update={"candidate_baremetals": ["bm-y"]})
        r2 = solve_split_placement(SplitPlacementRequest(
            requirements=[req2], baremetals=[bm2],
            config=SolverConfig(auto_generate_anti_affinity=False),
        ))
        assert r2.success  # matching network places normally


# ===========================================================================
# HTTP endpoint
# ===========================================================================

class TestProcureEndpoint:

    def test_endpoint_smoke(self, client):
        body = {
            "requirements": [{
                "total_resources": {"cpu_cores": 64, "memory_mib": 0, "storage_gb": 0},
                "node_role": "worker",
                "vm_specs": [{"cpu_cores": 8, "memory_mib": 16000, "storage_gb": 100}],
            }],
            "in_stock": [{
                "id": "bm-1",
                "total_capacity": {"cpu_cores": 16, "memory_mib": 256000, "storage_gb": 2000},
                "topology": {"ag": "ag-1"},
            }],
            "procurement_types": [{
                "type_id": "big",
                "capacity": {"cpu_cores": 64, "memory_mib": 256000, "storage_gb": 2000},
            }],
            "config": {"auto_generate_anti_affinity": False},
        }
        resp = client.post("/v1/capacity/procure", json=body)
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"]
        assert data["procured_bm_total"] >= 1
