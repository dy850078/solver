"""
Test suite — Purchase planning (PM evaluates how many Baremetals to buy).
Run: pytest tests/test_purchase_planning.py -v
"""

from app.models import (
    NodeRole,
    PurchaseCandidate,
    PurchasePlanningRequest,
    ResourceRequirement,
    Resources,
    SolverConfig,
    Topology,
)
from app.purchase_planner import plan_purchase

from .conftest import make_bm


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_req(
    cpu=0, mem=0, disk=0, gpu=0,
    role=NodeRole.WORKER, cluster="cluster-1", ip_type="routable",
    vm_specs=None, min_vms=None, max_vms=None,
) -> ResourceRequirement:
    return ResourceRequirement(
        total_resources=Resources(cpu_cores=cpu, memory_mib=mem, storage_gb=disk, gpu_count=gpu),
        node_role=role,
        cluster_id=cluster,
        ip_type=ip_type,
        vm_specs=vm_specs,
        min_total_vms=min_vms,
        max_total_vms=max_vms,
    )


def make_candidate(
    cpu=64, mem=256_000, disk=2000, gpu=0,
    ag="ag-1", dc="dc-1", rack="rack-1",
    max_quantity=10, cost=1.0, label="",
) -> PurchaseCandidate:
    return PurchaseCandidate(
        spec=Resources(cpu_cores=cpu, memory_mib=mem, storage_gb=disk, gpu_count=gpu),
        topology_template=Topology(site="site-a", phase="p1", datacenter=dc, rack=rack, ag=ag),
        max_quantity=max_quantity,
        cost=cost,
        label=label,
    )


def plan(requirements, candidates, existing=None, **cfg_overrides):
    defaults = dict(max_solve_time_seconds=10, auto_generate_anti_affinity=False)
    defaults.update(cfg_overrides)
    request = PurchasePlanningRequest(
        requirements=requirements if isinstance(requirements, list) else [requirements],
        purchase_candidates=candidates if isinstance(candidates, list) else [candidates],
        existing_baremetals=existing or [],
        config=SolverConfig(**defaults),
    )
    return plan_purchase(request)


# ===========================================================================
# 1. Recommend the minimum number of BMs to buy
# ===========================================================================

class TestMinimumPurchase:

    def test_exact_fit_single_candidate(self):
        """64 CPU worker need, 64-core candidate → buy 1 machine."""
        req = make_req(
            cpu=64, mem=256_000, disk=800,
            vm_specs=[Resources(cpu_cores=8, memory_mib=32_000, storage_gb=100)],
        )
        candidate = make_candidate(max_quantity=5, cost=1.0, label="big")

        result = plan(req, candidate)

        assert result.feasible
        assert len(result.purchase_decisions) == 1
        assert result.purchase_decisions[0].recommended_quantity == 1
        assert result.purchase_decisions[0].used_count == 1

    def test_multiple_bms_needed(self):
        """128 CPU need, 64-core candidate → buy 2 machines."""
        req = make_req(
            cpu=128, mem=512_000, disk=1600,
            vm_specs=[Resources(cpu_cores=8, memory_mib=32_000, storage_gb=100)],
        )
        candidate = make_candidate(max_quantity=10, cost=1.0, label="big")

        result = plan(req, candidate)

        assert result.feasible
        assert result.purchase_decisions[0].recommended_quantity == 2


# ===========================================================================
# 2. Cost-weighted candidate selection
# ===========================================================================

class TestCostPriority:

    def test_cheaper_candidate_preferred(self):
        """Two candidates of same spec; lower-cost one should be chosen."""
        req = make_req(
            cpu=64, mem=256_000, disk=800,
            vm_specs=[Resources(cpu_cores=8, memory_mib=32_000, storage_gb=100)],
        )
        cheap = make_candidate(ag="ag-1", max_quantity=5, cost=1.0, label="cheap")
        expensive = make_candidate(ag="ag-2", max_quantity=5, cost=3.0, label="expensive")

        result = plan(req, [cheap, expensive])

        assert result.feasible
        by_label = {d.label: d for d in result.purchase_decisions}
        assert by_label["cheap"].recommended_quantity >= 1
        assert by_label["expensive"].recommended_quantity == 0

    def test_cost_reversal_flips_choice(self):
        """Reversing the costs flips which candidate is preferred."""
        req = make_req(
            cpu=64, mem=256_000, disk=800,
            vm_specs=[Resources(cpu_cores=8, memory_mib=32_000, storage_gb=100)],
        )
        a = make_candidate(ag="ag-1", max_quantity=5, cost=3.0, label="a")
        b = make_candidate(ag="ag-2", max_quantity=5, cost=1.0, label="b")

        result = plan(req, [a, b])

        by_label = {d.label: d for d in result.purchase_decisions}
        assert by_label["a"].recommended_quantity == 0
        assert by_label["b"].recommended_quantity >= 1


# ===========================================================================
# 3. Mixed mode: existing inventory + new purchases
# ===========================================================================

class TestMixedMode:

    def test_existing_bms_preferred_over_purchase(self):
        """Two existing BMs fit the demand → solver should buy zero."""
        existing = [
            make_bm("existing-01", cpu=64, mem=256_000, disk=2000, ag="ag-1"),
            make_bm("existing-02", cpu=64, mem=256_000, disk=2000, ag="ag-2"),
        ]
        req = make_req(
            cpu=64, mem=256_000, disk=800,
            vm_specs=[Resources(cpu_cores=8, memory_mib=32_000, storage_gb=100)],
        )
        candidate = make_candidate(max_quantity=5, cost=1.0, label="extra")

        result = plan(req, candidate, existing=existing)

        assert result.feasible
        assert result.purchase_decisions[0].recommended_quantity == 0
        # All assignments should go to existing BMs
        for a in result.assignments:
            assert a.baremetal_id.startswith("existing-")

    def test_partial_existing_plus_purchase(self):
        """
        One existing BM of 64-core + 128 CPU demand → solver must buy
        at least one more to cover the 64-core gap.
        """
        existing = [make_bm("existing-01", cpu=64, mem=256_000, disk=2000, ag="ag-1")]
        req = make_req(
            cpu=128, mem=512_000, disk=1600,
            vm_specs=[Resources(cpu_cores=8, memory_mib=32_000, storage_gb=100)],
        )
        candidate = make_candidate(ag="ag-2", max_quantity=5, cost=1.0, label="extra")

        result = plan(req, candidate, existing=existing)

        assert result.feasible
        assert result.purchase_decisions[0].recommended_quantity >= 1
        assert result.purchase_decisions[0].recommended_quantity <= 2


# ===========================================================================
# 4. Cross-AG purchases with auto anti-affinity
# ===========================================================================

class TestCrossAGPurchase:

    def test_workers_spread_across_candidate_ags(self):
        """
        Two candidates on different AGs, auto anti-affinity enabled.
        3 worker VMs should spread: at least one VM on each AG used.
        """
        req = make_req(
            cpu=24, mem=96_000, disk=300,
            vm_specs=[Resources(cpu_cores=8, memory_mib=32_000, storage_gb=100)],
        )
        a = make_candidate(ag="ag-a", rack="rack-a", max_quantity=3, cost=1.0, label="a")
        b = make_candidate(ag="ag-b", rack="rack-b", max_quantity=3, cost=1.0, label="b")

        result = plan(req, [a, b], auto_generate_anti_affinity=True)

        assert result.feasible
        ags_used = {a.ag for a in result.assignments}
        assert "ag-a" in ags_used
        assert "ag-b" in ags_used


# ===========================================================================
# 5. Infeasible when candidates collectively can't cover demand
# ===========================================================================

class TestInfeasible:

    def test_insufficient_candidate_capacity(self):
        """
        Need 128 CPU but only 1 BM of 8 CPU allowed → infeasible.
        """
        req = make_req(
            cpu=128, mem=512_000, disk=1600,
            vm_specs=[Resources(cpu_cores=8, memory_mib=32_000, storage_gb=100)],
        )
        candidate = make_candidate(
            cpu=8, mem=32_000, disk=200, max_quantity=1, cost=1.0, label="tiny",
        )

        result = plan(req, candidate)

        assert not result.feasible


# ===========================================================================
# 6. Symmetry breaking uses the lowest-index BMs first
# ===========================================================================

class TestSymmetryBreaking:

    def test_low_index_bms_used_first(self):
        """
        Given max_quantity=5 and we only need 2 BMs, used slots should be
        planned-*-0 and planned-*-1 (not scattered indices).
        """
        req = make_req(
            cpu=128, mem=512_000, disk=1600,
            vm_specs=[Resources(cpu_cores=8, memory_mib=32_000, storage_gb=100)],
        )
        candidate = make_candidate(max_quantity=5, cost=1.0, label="big")

        result = plan(req, candidate)

        assert result.feasible
        used_bm_ids = sorted({a.baremetal_id for a in result.assignments})
        assert result.purchase_decisions[0].recommended_quantity == 2
        assert used_bm_ids == ["planned-big-0", "planned-big-1"]


# ===========================================================================
# 7. Utilization reporting
# ===========================================================================

class TestUtilizationReport:

    def test_utilization_between_0_and_1(self):
        """Reported avg_utilization should be reasonable (0.0 < x <= 1.0)."""
        req = make_req(
            cpu=32, mem=128_000, disk=400,
            vm_specs=[Resources(cpu_cores=8, memory_mib=32_000, storage_gb=100)],
        )
        candidate = make_candidate(max_quantity=5, cost=1.0, label="big")

        result = plan(req, candidate)

        assert result.feasible
        util = result.purchase_decisions[0].avg_utilization
        assert 0.0 < util["cpu_cores"] <= 1.0
        assert 0.0 < util["memory_mib"] <= 1.0


# ===========================================================================
# 8. HTTP endpoint smoke test
# ===========================================================================

class TestEndpoint:

    def test_purchase_planning_endpoint(self, client):
        payload = {
            "requirements": [{
                "total_resources": {"cpu_cores": 32, "memory_mib": 128000, "storage_gb": 400},
                "node_role": "worker",
                "cluster_id": "cluster-1",
                "ip_type": "routable",
                "vm_specs": [{"cpu_cores": 8, "memory_mib": 32000, "storage_gb": 100}],
            }],
            "purchase_candidates": [{
                "spec": {"cpu_cores": 64, "memory_mib": 256000, "storage_gb": 2000},
                "topology_template": {"site": "site-a", "ag": "ag-1"},
                "max_quantity": 5,
                "cost": 1.0,
                "label": "big",
            }],
            "config": {
                "max_solve_time_seconds": 10,
                "auto_generate_anti_affinity": False,
            },
        }
        response = client.post("/v1/purchase-planning", json=payload)
        assert response.status_code == 200
        body = response.json()
        assert body["feasible"] is True
        assert len(body["purchase_decisions"]) == 1
        assert body["purchase_decisions"][0]["recommended_quantity"] >= 1
