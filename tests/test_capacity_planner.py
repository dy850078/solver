"""
Test suite — procurement sizing (capacity planning Phase 2).
Run: pytest tests/test_capacity_planner.py -v
"""

from app.models import (
    AntiAffinityRule,
    BaremetalType,
    CapacityPlanRequest,
    CommittedStock,
    DemandEntry,
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
from app.capacity_planner import solve_capacity_horizon, solve_capacity_plan
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

    def test_no_types_plans_without_buying(self):
        """Empty procurement_types = 'can this fit in-stock?' — a valid ask:
        fits → success with zero buys; doesn't fit → classified shortfall,
        not an INPUT_ERROR (matters for per-fab filtered subsets)."""
        fits = make_req(cpu=16, spec=SPEC_8)
        r = procure(fits, [make_bm("bm-1", cpu=16, mem=256_000, disk=2000)], [])
        assert r.success
        assert r.procured_bm_total == 0

        too_big = make_req(cpu=64, spec=SPEC_8)
        r2 = procure(too_big, [make_bm("bm-2", cpu=16, mem=256_000, disk=2000)], [])
        assert not r2.success
        assert r2.shortfall_cause == "capacity"

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


class TestUnreachableDemandMessages:
    """INPUT_ERROR wording for demand no machine can serve must say what to
    declare — cells come from the supply side only (決議 #37)."""

    def test_network_without_any_cell_names_the_fix(self):
        """No stock and no cap declares bgp1 → the message points at the
        three ways to declare the cell, not at vm_specs."""
        types = [make_type("big", 64, 256_000, 2000)]
        req = make_req(cpu=32, spec=SPEC_8, network="bgp1")

        r = procure(req, [], types)

        assert not r.success
        assert "INPUT_ERROR" in r.solver_status
        assert "'bgp1'" in r.solver_status
        assert "procurement_cap" in r.solver_status

    def test_no_stock_and_no_types_says_so(self):
        req = make_req(cpu=32, spec=SPEC_8)

        r = procure(req, [], [])

        assert not r.success
        assert "INPUT_ERROR" in r.solver_status
        assert "no in-stock machines and no procurement_types" in r.solver_status

    def test_no_stock_without_network_still_buys(self):
        """Behavior guard: greenfield without a network works via the
        fallback cell — everything is bought."""
        types = [make_type("big", 64, 256_000, 2000)]
        req = make_req(cpu=32, spec=SPEC_8)

        r = procure(req, [], types)

        assert r.success, r.solver_status
        assert r.procured_bm_total >= 1

    def test_cap_declares_the_network_cell(self):
        """Behavior guard: a procurement_cap doubles as the cell declaration,
        so the bgp1 case succeeds once a cap names it."""
        types = [make_type("big", 64, 256_000, 2000)]
        req = make_req(cpu=32, spec=SPEC_8, network="bgp1")
        caps = [ProcurementCap(bucket="ag-1", network="bgp1", max_bm=2)]

        r = procure(req, [], types, caps=caps)

        assert r.success, r.solver_status
        assert r.procured_bm_total >= 1

    def test_zero_demand_row_is_exempt(self):
        """An all-zero "no growth" requirement with nothing to place on must
        not fail — it asks for nothing."""
        r = procure(make_req(), [], [])

        assert r.success, r.solver_status
        assert r.procured_bm_total == 0


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
            committed_entry_of = {}
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
# Multi-period horizon planning (Phase 3)
# ===========================================================================

def entry(period, cpu=0, mem=0, disk=0, pods=0, cluster="cluster-1",
          role=NodeRole.WORKER, spec=None, fab="", network="", allowed=None,
          demand_id=None):
    return DemandEntry(
        cluster_id=cluster, node_role=role, period=period,
        cpu_cores=cpu, memory_mib=mem, storage_gb=disk, pod_count=pods,
        vm_specs=[spec] if spec else None,
        fab=fab, network=network, allowed_bm_types=allowed,
        demand_id=demand_id,
    )


def plan(book, in_stock, types, caps=None, committed=None, rules=None, **cfg):
    defaults = dict(max_solve_time_seconds=10, auto_generate_anti_affinity=False)
    defaults.update(cfg)
    return solve_capacity_horizon(CapacityPlanRequest(
        demand_book=book,
        in_stock=in_stock,
        procurement_types=types,
        procurement_caps=caps or [],
        committed_stock=committed or [],
        anti_affinity_rules=rules or [],
        config=SolverConfig(**defaults),
    ))


class TestCapacityHorizon:

    def test_auto_anti_affinity_spreads_planned_nodes_across_ags(self):
        """
        Planned demand carries the "plan" ip_type sentinel, so auto
        anti-affinity groups it and spreads the 6 new nodes across the 3 AGs
        (cap ⌈6/3⌉ = 2) instead of consolidating them onto one machine.
        """
        in_stock = [make_bm(f"bm-{i}", cpu=64, mem=256_000, disk=2000,
                            ag=f"ag-{i}", rack=f"rack-{i}") for i in range(1, 4)]
        types = [make_type("big", 64, 256_000, 2000)]
        book = [entry("2026-01", cpu=48, spec=SPEC_8)]

        r = plan(book, in_stock, types, auto_generate_anti_affinity=True)

        assert r.success
        p = r.by_fab_period[0]
        assert p.bm_procurement_total == 0
        adds = {c.bucket: c.node_adds for c in p.cells if c.node_adds}
        assert adds == {"ag-1": 2, "ag-2": 2, "ag-3": 2}

    def test_state_rolls_forward_across_months(self):
        """Month 1 fills the in-stock BM; month 2's identical demand buys."""
        in_stock = [make_bm("bm-1", cpu=64, mem=256_000, disk=2000)]
        types = [make_type("big", 64, 256_000, 2000)]
        book = [entry("2026-01", cpu=64, spec=SPEC_8),
                entry("2026-02", cpu=64, spec=SPEC_8)]

        r = plan(book, in_stock, types)

        assert r.success
        by_period = {p.period: p for p in r.by_fab_period}
        assert by_period["2026-01"].bm_procurement_total == 0
        assert by_period["2026-02"].bm_procurement_total == 1

    def test_bought_bm_serves_later_months(self):
        """A BM bought in month 1 with spare room hosts month 2's demand."""
        types = [make_type("big", 64, 256_000, 2000)]
        book = [entry("2026-01", cpu=16, spec=SPEC_8),
                entry("2026-02", cpu=16, spec=SPEC_8)]

        r = plan(book, [], types)

        assert r.success
        by_period = {p.period: p for p in r.by_fab_period}
        assert by_period["2026-01"].bm_procurement_total == 1
        assert by_period["2026-02"].bm_procurement_total == 0  # reuses acq BM

    def test_caps_consumed_across_months(self):
        """(決議 #30) A 1-slot bucket bought out in month 1 leaves month 2's
        buy demand with a `space` shortfall."""
        in_stock = [make_bm("bm-1", cpu=8, mem=16_000, disk=100, ag="ag-1")]
        types = [make_type("big", 64, 256_000, 2000)]
        caps = [ProcurementCap(bucket="ag-1", max_bm=1)]
        book = [entry("2026-01", cpu=16, spec=SPEC_8),
                entry("2026-02", cpu=64, spec=SPEC_8)]

        r = plan(book, in_stock, types, caps=caps)

        assert not r.success
        by_period = {p.period: p for p in r.by_fab_period}
        assert by_period["2026-01"].success
        assert by_period["2026-01"].bm_procurement_total == 1
        m2 = by_period["2026-02"]
        assert not m2.success
        assert m2.shortfalls and m2.shortfalls[0].cause == "space"

    def test_fabs_are_independent(self):
        """(決議 #4) fab A's overflow buys instead of using fab B's idle BM."""
        bm_a = make_bm("bm-a", cpu=64, mem=256_000, disk=2000)
        bm_a.topology = bm_a.topology.model_copy(update={"site": "fab-a"})
        bm_b = make_bm("bm-b", cpu=64, mem=256_000, disk=2000)
        bm_b.topology = bm_b.topology.model_copy(update={"site": "fab-b"})
        types = [make_type("big", 64, 256_000, 2000)]
        book = [entry("2026-01", cpu=128, spec=SPEC_8, fab="fab-a"),
                entry("2026-01", cpu=32, spec=SPEC_8, fab="fab-b")]

        r = plan(book, [bm_a, bm_b], types)

        assert r.success
        by_fab = {p.fab: p for p in r.by_fab_period}
        assert by_fab["fab-a"].bm_procurement_total == 1
        assert by_fab["fab-b"].bm_procurement_total == 0

    def test_sparse_horizon_and_no_growth_month(self):
        """(決議 #26/#27) Absent months are absent (unplanned); an all-zero
        row is a planned no-growth month with zero adds."""
        in_stock = [make_bm("bm-1", cpu=64, mem=256_000, disk=2000)]
        types = [make_type("big", 64, 256_000, 2000)]
        book = [entry("2026-01", cpu=16, spec=SPEC_8),
                entry("2026-03")]  # planned, explicitly no growth

        r = plan(book, in_stock, types)

        assert r.success
        periods = [(p.period, p.node_adds_total) for p in r.by_fab_period]
        assert periods == [("2026-01", 2), ("2026-03", 0)]  # no 2026-02 row

    def test_committed_pool_drains_across_months(self):
        """(缺口 3h × roll-forward) One owned machine serves month 1; month 2
        must buy — the pool doesn't double-serve."""
        types = [make_type("big", 64, 256_000, 2000)]
        committed = [CommittedStock(type_id="big", count=1)]
        book = [entry("2026-01", cpu=16, spec=SPEC_8),
                entry("2026-02", cpu=64, spec=SPEC_8)]

        r = plan(book, [], types, committed=committed)

        assert r.success
        by_period = {p.period: p for p in r.by_fab_period}
        assert by_period["2026-01"].committed_bm_used == 1
        assert by_period["2026-01"].bm_procurement_total == 0
        assert by_period["2026-02"].committed_bm_used == 0
        assert by_period["2026-02"].bm_procurement_total == 1

    def test_budget_view_and_totals(self):
        """budget_view rolls bought counts up to fab × (bucket, network) ×
        month — the planning cell, not a representative datacenter."""
        in_stock = [make_bm("bm-1", cpu=8, mem=16_000, disk=100, ag="ag-1")]
        types = [make_type("big", 64, 256_000, 2000)]
        book = [entry("2026-01", cpu=24, spec=SPEC_8)]

        r = plan(book, in_stock, types)

        assert r.success
        assert r.totals["bm_procurement"] == 1
        assert len(r.budget_view) == 1
        row = r.budget_view[0]
        assert (row.fab, row.bucket, row.network, row.period, row.bm_count) == \
            ("", "ag-1", "", "2026-01", 1)
        assert row.type_id == "big"

    def test_budget_view_splits_by_model(self):
        """With several buyable models, budget rows carry WHICH model to buy
        (finance needs the model, not just a count)."""
        types = [make_type("small", 16, 64_000, 400),
                 make_type("big", 64, 256_000, 2000)]
        book = [entry("2026-01", cpu=32, spec=SPEC_8, cluster="c-small",
                      allowed=["small"]),
                entry("2026-01", cpu=64, spec=SPEC_8, cluster="c-big",
                      allowed=["big"])]

        r = plan(book, [], types)

        assert r.success
        by_type = {row.type_id: row.bm_count for row in r.budget_view}
        assert by_type == {"small": 2, "big": 1}

    def test_in_stock_bm_used_reported(self):
        """The report says how many EXISTING machines this month's placement
        touched — machine count, not utilization (user ask)."""
        in_stock = [make_bm("bm-1", cpu=64, mem=256_000, disk=2000,
                            ag="ag-1", rack="r1"),
                    make_bm("bm-2", cpu=64, mem=256_000, disk=2000,
                            ag="ag-2", rack="r2")]
        types = [make_type("big", 64, 256_000, 2000)]
        book = [entry("2026-01", cpu=16, spec=SPEC_8)]

        r = plan(book, in_stock, types)

        assert r.success
        p = r.by_fab_period[0]
        # 2×8c VMs consolidate onto one machine; the other is untouched.
        assert p.in_stock_bm_used == 1
        assert sum(c.in_stock_bm_used for c in p.cells) == 1
        assert r.totals["in_stock_bm_used"] == 1

    def test_allowed_bm_types_restricts_buying(self):
        """(決議 #38) A cluster limited to 'small' buys 4 smalls, not 1 big."""
        types = [make_type("small", 16, 64_000, 400),
                 make_type("big", 64, 256_000, 2000)]
        book = [entry("2026-01", cpu=64, spec=SPEC_8, allowed=["small"])]

        r = plan(book, [], types)

        assert r.success
        m1 = r.by_fab_period[0]
        assert {d.type_id for d in m1.procurement} == {"small"}
        assert m1.bm_procurement_total == 4

    def test_cells_report_post_month_state(self):
        """Cells carry the (bucket, network) in-stock snapshot after the
        month, plus that month's adds."""
        in_stock = [make_bm("bm-1", cpu=64, mem=256_000, disk=2000, ag="ag-1")]
        types = [make_type("big", 64, 256_000, 2000)]
        book = [entry("2026-01", cpu=16, spec=SPEC_8)]

        r = plan(book, in_stock, types)

        cell = r.by_fab_period[0].cells[0]
        assert (cell.bucket, cell.network) == ("ag-1", "")
        assert cell.node_adds == 2
        assert cell.bm_bought == 0
        assert cell.in_stock_used.cpu_cores == 16
        assert cell.in_stock_available.cpu_cores == 48


class TestP3ReviewFixes:

    def test_committed_drain_is_entry_exact(self):
        """(P3-#1) The pool the solver actually drew from is drained — an
        unusable floating pool must not absorb the drain and let the bucketed
        machine double-serve."""
        types = [make_type("big", 64, 256_000, 2000)]
        committed = [
            CommittedStock(type_id="big", count=1, network="net-b"),  # unusable
            CommittedStock(type_id="big", count=1, bucket="ag-1"),
        ]
        book = [entry("2026-01", cpu=64, spec=SPEC_8),
                entry("2026-02", cpu=64, spec=SPEC_8)]

        r = plan(book, [], types, committed=committed)

        assert r.success
        by_period = {p.period: p for p in r.by_fab_period}
        assert by_period["2026-01"].committed_bm_used == 1
        assert by_period["2026-02"].committed_bm_used == 0  # not double-served
        assert by_period["2026-02"].bm_procurement_total == 1
        assert r.totals["bm_procurement"] == 1

    def test_mixed_fab_sentinel_rejected(self):
        """(P3-#2) fab='' mixed with named fabs would double-plan hardware."""
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="single-fab mode"):
            CapacityPlanRequest(
                demand_book=[entry("2026-01", cpu=8, spec=SPEC_8, fab=""),
                             entry("2026-01", cpu=8, spec=SPEC_8, fab="fab-a")],
                in_stock=[],
                procurement_types=[make_type("big", 64, 256_000, 2000)],
            )

    def test_unscoped_cap_rejected_in_named_fab_mode(self):
        """(P3-#2) A fab='' cap in named-fab mode would duplicate one physical
        slot budget into every fab."""
        import pytest
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="ProcurementCap"):
            CapacityPlanRequest(
                demand_book=[entry("2026-01", cpu=8, spec=SPEC_8, fab="fab-a")],
                in_stock=[],
                procurement_types=[make_type("big", 64, 256_000, 2000)],
                procurement_caps=[ProcurementCap(bucket="ag-1", max_bm=1)],
            )

    def test_materialized_vrack_renamed(self):
        """(P3-#4) A machine bought last month must not share a synthetic rack
        with this month's regenerated virtual BM — buying twice in a row under
        a rack-spread rule succeeds."""
        in_stock = [make_bm("bm-1", cpu=8, mem=16_000, disk=100, ag="ag-1")]
        types = [make_type("big", 64, 256_000, 2000)]
        caps = [ProcurementCap(bucket="ag-1", max_bm=2)]
        rule = AntiAffinityRule(
            group_id="workers",
            selector=GroupSelector(node_role=NodeRole.WORKER),
            spread_on=["rack"],
            cap_per_bucket={"rack": 1},
        )
        # Month 2 places 2 VMs under a 1-per-rack cap: one must land on the
        # month-1 acquisition, one on a fresh buy. Without the vrack rename
        # the fresh virtual BM shares the acquisition's rack → false space.
        book = [entry("2026-01", cpu=16, spec=SPEC_8),
                entry("2026-02", cpu=16, spec=SPEC_8)]

        r = plan(book, in_stock, types, caps=caps, rules=[rule])

        assert r.success, [p.solver_status for p in r.by_fab_period]
        by_period = {p.period: p for p in r.by_fab_period}
        assert by_period["2026-02"].bm_procurement_total == 1

    def test_totals_exclude_failed_month_whatif(self):
        """(P3-#5) A failed month's what-if uncapped buys stay in its own
        report; totals and budget_view agree."""
        in_stock = [make_bm("bm-1", cpu=8, mem=16_000, disk=100, ag="ag-1")]
        types = [make_type("big", 64, 256_000, 2000)]
        caps = [ProcurementCap(bucket="ag-1", max_bm=1)]
        book = [entry("2026-01", cpu=16, spec=SPEC_8),
                entry("2026-02", cpu=64, spec=SPEC_8)]

        r = plan(book, in_stock, types, caps=caps)

        assert not r.success
        by_period = {p.period: p for p in r.by_fab_period}
        assert by_period["2026-02"].bm_procurement_total >= 1  # what-if visible
        assert r.totals["bm_procurement"] == 1                 # month 1 only
        assert sum(row.bm_count for row in r.budget_view) == 1

    def test_unknown_allowed_type_is_input_error(self):
        """(P3-#6) A typo'd allowed_bm_types id fails loudly, naming the id,
        instead of masquerading as a capacity shortfall."""
        types = [make_type("big", 64, 256_000, 2000)]
        req = make_req(cpu=64, spec=SPEC_8)
        req = req.model_copy(update={"allowed_bm_types": ["bgi"]})

        r = procure(req, [make_bm("bm-1", cpu=16, mem=256_000, disk=2000)], types)

        assert not r.success
        assert r.solver_status.startswith("INPUT_ERROR")
        assert "bgi" in r.solver_status

    def test_failed_month_cells_carry_attribution(self):
        """(P3-#7) A 'space' month's what-if buys appear in cells even when
        the cell holds no in-stock BM — drill-down reconciles with headline."""
        types = [make_type("big", 64, 256_000, 2000)]
        caps = [ProcurementCap(bucket="ag-1", max_bm=1)]
        book = [entry("2026-01", cpu=128, spec=SPEC_8)]  # needs 2 BMs, cap 1

        r = plan(book, [], types, caps=caps)

        m1 = r.by_fab_period[0]
        assert not m1.success
        assert m1.shortfalls[0].cause == "space"
        assert m1.bm_procurement_total == 2
        assert sum(c.bm_bought for c in m1.cells) == 2  # reconciles

    def test_fab_stops_after_first_failure(self):
        """(P3-#9/#10) After a failed month the fab's later planned months are
        blocked stubs — no phantom success, no extra solves."""
        in_stock = [make_bm("bm-1", cpu=8, mem=16_000, disk=100, ag="ag-1")]
        types = [make_type("big", 64, 256_000, 2000)]
        caps = [ProcurementCap(bucket="ag-1", max_bm=1)]
        book = [entry("2026-01", cpu=16, spec=SPEC_8),   # buys the only slot
                entry("2026-02", cpu=64, spec=SPEC_8),   # fails: space
                entry("2026-03", cpu=8, spec=SPEC_8)]    # would "succeed" — blocked

        r = plan(book, in_stock, types, caps=caps)

        assert not r.success
        by_period = {p.period: p for p in r.by_fab_period}
        m3 = by_period["2026-03"]
        assert not m3.success
        assert m3.solver_status.startswith("BLOCKED")
        assert m3.shortfalls[0].cause == "blocked"
        assert "2026-02" in m3.shortfalls[0].message


class TestCommittedAvailableFrom:
    """S1: CommittedStock.available_from — hand-maintained month gate."""

    def test_available_from_gates_until_month(self):
        """A pool arriving in month 2 must not serve month 1: month 1 buys,
        month 2 drains the now-available pool instead of buying again."""
        types = [make_type("big", 64, 256_000, 2000)]
        committed = [CommittedStock(type_id="big", count=1,
                                    available_from="2026-02")]
        book = [entry("2026-01", cpu=64, spec=SPEC_8),
                entry("2026-02", cpu=64, spec=SPEC_8)]

        r = plan(book, [], types, committed=committed)

        assert r.success
        by_period = {p.period: p for p in r.by_fab_period}
        assert by_period["2026-01"].committed_bm_used == 0
        assert by_period["2026-01"].bm_procurement_total == 1
        assert by_period["2026-02"].committed_bm_used == 1
        assert by_period["2026-02"].bm_procurement_total == 0

    def test_available_from_equal_month_usable(self):
        """The gate is inclusive: available_from == first period drains in
        month 1 (mirror of test_committed_pool_drains_across_months)."""
        types = [make_type("big", 64, 256_000, 2000)]
        committed = [CommittedStock(type_id="big", count=1,
                                    available_from="2026-01")]
        book = [entry("2026-01", cpu=16, spec=SPEC_8)]

        r = plan(book, [], types, committed=committed)

        assert r.success
        assert r.by_fab_period[0].committed_bm_used == 1
        assert r.by_fab_period[0].bm_procurement_total == 0

    def test_available_from_leftover_carries(self):
        """A gated-then-active pool behaves like any pool afterwards: partial
        drain leaves the remainder for later months, no buying."""
        types = [make_type("big", 64, 256_000, 2000)]
        committed = [CommittedStock(type_id="big", count=2,
                                    available_from="2026-01")]
        book = [entry("2026-01", cpu=64, spec=SPEC_8),
                entry("2026-02", cpu=64, spec=SPEC_8)]

        r = plan(book, [], types, committed=committed)

        assert r.success
        by_period = {p.period: p for p in r.by_fab_period}
        assert by_period["2026-01"].committed_bm_used == 1
        assert by_period["2026-02"].committed_bm_used == 1
        assert r.totals["bm_procurement"] == 0

    def test_available_from_gating_preserves_entry_indices(self):
        """The month-1 filter must not shift committed_entry_used onto the
        gated entry: entry 0 (available now) drains in month 1, entry 1
        (arrives month 2) still has its full count for month 2. If gating
        skewed indices, month 1's drain would eat entry 1 and month 2 would
        be forced to buy."""
        types = [make_type("big", 64, 256_000, 2000)]
        committed = [
            CommittedStock(type_id="big", count=1),
            CommittedStock(type_id="big", count=1, available_from="2026-02"),
        ]
        book = [entry("2026-01", cpu=64, spec=SPEC_8),
                entry("2026-02", cpu=64, spec=SPEC_8)]

        r = plan(book, [], types, committed=committed)

        assert r.success
        by_period = {p.period: p for p in r.by_fab_period}
        assert by_period["2026-01"].committed_bm_used == 1
        assert by_period["2026-02"].committed_bm_used == 1
        assert r.totals["bm_procurement"] == 0

    def test_available_from_beyond_horizon_never_used(self):
        """Stock arriving after the last planned month is legitimate input:
        never offered, never drained, no error — every month buys."""
        types = [make_type("big", 64, 256_000, 2000)]
        committed = [CommittedStock(type_id="big", count=5,
                                    available_from="2027-01")]
        book = [entry("2026-01", cpu=64, spec=SPEC_8),
                entry("2026-02", cpu=64, spec=SPEC_8)]

        r = plan(book, [], types, committed=committed)

        assert r.success
        assert r.totals["bm_procurement"] == 2
        assert all(p.committed_bm_used == 0 for p in r.by_fab_period)

    def test_available_from_bad_format_rejected(self):
        """Malformed months must fail validation: '2026/07' sorts after every
        valid 'YYYY-MM' and would silently gate the entry forever."""
        import pytest
        from pydantic import ValidationError

        for bad in ("2026/07", "202607", "2026-13", "2026-1"):
            with pytest.raises(ValidationError, match="available_from"):
                CommittedStock(type_id="big", count=1, available_from=bad)

    def test_procure_ignores_available_from(self):
        """The single-shot endpoint has no period concept: a far-future
        available_from is ignored and the pool still drains before buying."""
        types = [make_type("big", 64, 256_000, 2000)]
        committed = [CommittedStock(type_id="big", count=1,
                                    available_from="2099-01")]

        r = procure(make_req(cpu=16, spec=SPEC_8), [], types,
                    committed=committed)

        assert r.success
        assert r.committed_bm_used == 1
        assert r.procured_bm_total == 0


class TestRequirementCoverage:
    """S2: per-requirement / per-demand coverage by supply source."""

    def test_coverage_sums_equal_planned_vms(self):
        """Invariant: per row total == in_stock+committed+new_buy, and the
        grand total equals the planned VM count from split_decisions."""
        # A bought "mid" holds only 4 of the 6 planned VMs, so the free
        # in-stock machine must host the rest — both sources appear.
        in_stock = [make_bm("bm-1", cpu=16, mem=64_000, disk=400, ag="ag-1")]
        types = [make_type("mid", 32, 128_000, 1000)]

        r = procure(make_req(cpu=48, spec=SPEC_8), in_stock, types)

        assert r.success
        assert len(r.requirement_coverage) == 1
        row = r.requirement_coverage[0]
        assert row.total == row.in_stock + row.committed + row.new_buy
        assert row.total == sum(d.count for d in r.split_decisions)
        assert row.in_stock == 2 and row.new_buy == 4

    def test_coverage_classifies_three_sources(self):
        """One requirement forced across all three tiers: in-stock fills
        first (free), then committed (w_committed_stock), then a new buy."""
        in_stock = [make_bm("bm-1", cpu=16, mem=64_000, disk=400, ag="ag-1")]
        types = [make_type("small", 16, 64_000, 400)]
        committed = [CommittedStock(type_id="small", count=1)]

        r = procure(make_req(cpu=48, spec=SPEC_8), in_stock, types,
                    committed=committed)

        assert r.success
        row = r.requirement_coverage[0]
        assert (row.in_stock, row.committed, row.new_buy) == (2, 2, 2)
        assert r.committed_bm_used == 1 and r.procured_bm_total == 1

    def test_coverage_excludes_explicit_vms(self):
        """Explicit request.vms are pass-through placement, not requirement-
        driven: they never appear in coverage counts."""
        in_stock = [make_bm("bm-1", cpu=64, mem=256_000, disk=2000, ag="ag-1")]
        types = [make_type("big", 64, 256_000, 2000)]
        vm = make_vm("vm-explicit", cpu=8, mem=16_000, disk=100,
                     candidates=["bm-1"])

        r = procure(make_req(cpu=16, spec=SPEC_8), in_stock, types, vms=[vm])

        assert r.success
        row = r.requirement_coverage[0]
        assert row.total == 2  # the two synthetic VMs only, not vm-explicit

    def test_coverage_rows_zero_on_infeasible(self):
        """A failed solve still emits one row per requirement, all zeros —
        consistent shape for consumers."""
        in_stock = [make_bm("bm-1", cpu=8, mem=16_000, disk=100, ag="ag-1")]

        r = procure(make_req(cpu=64, spec=SPEC_8), in_stock, [])

        assert not r.success
        assert len(r.requirement_coverage) == 1
        row = r.requirement_coverage[0]
        assert (row.in_stock, row.committed, row.new_buy, row.total) == (0, 0, 0, 0)

    def test_demand_coverage_echoes_demand_id(self):
        """The horizon joins coverage back to demand rows: demand_id is a
        pass-through echo, absent ids stay None but rows remain identified."""
        types = [make_type("big", 64, 256_000, 2000)]
        book = [entry("2026-01", cpu=16, spec=SPEC_8, cluster="c-a",
                      demand_id="D-42"),
                entry("2026-01", cpu=16, spec=SPEC_8, cluster="c-b")]

        r = plan(book, [], types)

        assert r.success
        cov = {c.cluster_id: c for c in r.by_fab_period[0].demand_coverage}
        assert cov["c-a"].demand_id == "D-42"
        assert cov["c-b"].demand_id is None
        assert cov["c-a"].period == "2026-01"
        assert cov["c-a"].total == 2

    def test_demand_coverage_pre_roll_forward_across_months(self):
        """A machine bought in month 1 is month 2's in-stock: month 2 demand
        landing on it classifies as in_stock, not new_buy (documented
        RequirementCoverage semantics)."""
        types = [make_type("big", 64, 256_000, 2000)]
        book = [entry("2026-01", cpu=32, spec=SPEC_8),
                entry("2026-02", cpu=16, spec=SPEC_8)]

        r = plan(book, [], types)

        assert r.success
        by_period = {p.period: p for p in r.by_fab_period}
        m1 = by_period["2026-01"].demand_coverage[0]
        m2 = by_period["2026-02"].demand_coverage[0]
        assert m1.new_buy == 4 and m1.in_stock == 0
        assert m2.in_stock == m2.total == 2 and m2.new_buy == 0


class TestPlanEndpoint:

    def test_endpoint_smoke(self, client):
        body = {
            "demand_book": [{
                "cluster_id": "c1",
                "period": "2026-01",
                "cpu_cores": 64,
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
        resp = client.post("/v1/capacity/plan", json=body)
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"]
        assert data["totals"]["bm_procurement"] >= 1
        cov = data["by_fab_period"][0]["demand_coverage"]
        assert cov and cov[0]["cluster_id"] == "c1"
        assert cov[0]["total"] == cov[0]["in_stock"] + cov[0]["committed"] + cov[0]["new_buy"]
        import re
        assert re.fullmatch(r"[0-9a-f]{12}", data["config_fingerprint"])


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
        assert data["requirement_coverage"][0]["new_buy"] >= 1
        import re
        assert re.fullmatch(r"[0-9a-f]{12}", data["config_fingerprint"])
