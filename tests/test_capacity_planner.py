"""
Test suite — procurement sizing (capacity planning Phase 2).
Run: pytest tests/test_capacity_planner.py -v
"""

from app.models import (
    BaremetalType,
    NodeRole,
    ProcurementCap,
    ProcurementRequest,
    ResourceRequirement,
    Resources,
    SolverConfig,
)
from app.capacity_planner import solve_capacity_plan

from .conftest import make_bm


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_req(cpu=0, mem=0, disk=0, pods=0, spec=None, role=NodeRole.WORKER):
    return ResourceRequirement(
        total_resources=Resources(cpu_cores=cpu, memory_mib=mem, storage_gb=disk),
        node_role=role,
        vm_specs=[spec] if spec else None,
        total_pods=pods,
    )


def make_type(type_id, cpu, mem, disk):
    return BaremetalType(
        type_id=type_id,
        capacity=Resources(cpu_cores=cpu, memory_mib=mem, storage_gb=disk),
    )


def procure(requirements, in_stock, types, caps=None, **cfg):
    defaults = dict(max_solve_time_seconds=10, auto_generate_anti_affinity=False)
    defaults.update(cfg)
    reqs = requirements if isinstance(requirements, list) else [requirements]
    return solve_capacity_plan(ProcurementRequest(
        requirements=reqs,
        in_stock=in_stock,
        procurement_types=types,
        procurement_caps=caps or [],
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
