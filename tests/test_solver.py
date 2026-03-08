"""
Test suite — Step 1: Hard constraints only.

Each test class maps to one type of constraint.
Run: pytest tests/test_solver.py -v
"""

import json
from fastapi.testclient import TestClient
from server import app

client = TestClient(app)

from models import (
    Resources, Topology, Baremetal, VM, NodeRole,
    AntiAffinityRule, SolverConfig,
    PlacementRequest, PlacementResult, PlacementAssignment,
)
from solver import VMPlacementSolver


# ===========================================================================
# Helpers — keep tests readable
# ===========================================================================

def make_bm(bm_id, cpu=64, mem=256_000, disk=2000, gpu=0,
            used_cpu=0, used_mem=0, used_disk=0,
            ag="ag-1", dc="dc-1", rack="rack-1"):
    # Pydantic BaseModel 只接受 keyword arguments，不接受 positional arguments
    return Baremetal(
        id=bm_id,
        total_capacity=Resources(cpu_cores=cpu, memory_mb=mem, disk_gb=disk, gpu_count=gpu),
        used_capacity=Resources(cpu_cores=used_cpu, memory_mb=used_mem, disk_gb=used_disk),
        topology=Topology(site="site-a", phase="p1", datacenter=dc, rack=rack, ag=ag),
    )

def make_vm(vm_id, cpu=4, mem=16_000, disk=100,
            role=NodeRole.WORKER, cluster="cluster-1",
            ip_type="routable", candidates=None):
    return VM(
        id=vm_id,
        demand=Resources(cpu_cores=cpu, memory_mb=mem, disk_gb=disk),
        node_role=role,
        ip_type=ip_type,
        cluster_id=cluster,
        candidate_baremetals=candidates or [],
    )

def solve(vms, bms, rules=None, **config_overrides):
    """Solve with default config, overridable."""
    cfg = dict(max_solve_time_seconds=10, auto_generate_anti_affinity=False)
    cfg.update(config_overrides)
    request = PlacementRequest(
        vms=vms, baremetals=bms,
        anti_affinity_rules=rules or [],
        config=SolverConfig(**cfg),
    )
    return VMPlacementSolver(request).solve()

def amap(result):
    """Shorthand: vm_id -> bm_id dict."""
    return result.to_assignment_map()


# ===========================================================================
# 1. Assignment constraint: each VM lands on exactly one BM
# ===========================================================================

class TestAssignment:

    def test_single_vm_single_bm(self):
        r = solve([make_vm("vm-1")], [make_bm("bm-1")])
        assert r.success
        assert amap(r)["vm-1"] == "bm-1"

    def test_ten_vms_one_bm(self):
        bms = [make_bm("bm-1")]
        vms = [make_vm(f"vm-{i}") for i in range(10)]
        r = solve(vms, bms)
        assert r.success
        assert len(r.assignments) == 10

    def test_assignment_carries_ag(self):
        r = solve([make_vm("vm-1")], [make_bm("bm-1", ag="ag-x")])
        assert r.assignments[0].ag == "ag-x"

    def test_empty_vm_list_succeeds(self):
        r = solve([], [make_bm("bm-1")])
        assert r.success

    def test_empty_bm_list_fails(self):
        r = solve([make_vm("vm-1")], [])
        assert not r.success


# ===========================================================================
# 2. Capacity constraint: don't exceed BM resources
# ===========================================================================

class TestCapacity:

    def test_exact_fit(self):
        r = solve(
            [make_vm("vm-1", cpu=4, mem=16_000, disk=100)],
            [make_bm("bm-1", cpu=4, mem=16_000, disk=100)],
        )
        assert r.success

    def test_over_capacity_fails(self):
        """Two VMs that each need full capacity → can't both fit."""
        r = solve(
            [make_vm("vm-1", cpu=4, mem=16_000), make_vm("vm-2", cpu=4, mem=16_000)],
            [make_bm("bm-1", cpu=4, mem=16_000)],
        )
        assert not r.success

    def test_respects_used_capacity(self):
        """BM has 8 cpu total, 6 already used → only 2 free. VM needs 4 → fail."""
        r = solve(
            [make_vm("vm-1", cpu=4)],
            [make_bm("bm-1", cpu=8, used_cpu=6)],
        )
        assert not r.success

    def test_memory_bottleneck(self):
        """Plenty of CPU but not enough memory."""
        r = solve(
            [make_vm("vm-1", cpu=4, mem=16_000)],
            [make_bm("bm-1", cpu=128, mem=8_000)],
        )
        assert not r.success

    def test_spreads_when_one_bm_not_enough(self):
        """8 VMs × 4cpu each = 32 cpu needed. Each BM has 8 cpu → need 4 BMs."""
        bms = [make_bm(f"bm-{i}", cpu=8, mem=32_000, disk=200) for i in range(4)]
        vms = [make_vm(f"vm-{i}") for i in range(8)]
        r = solve(vms, bms)
        assert r.success
        assert len(r.assignments) == 8


# ===========================================================================
# 3. Candidate list: respect step 3 filtering
# ===========================================================================

class TestCandidateList:

    def test_only_uses_candidates(self):
        """VM has candidates=[bm-2] → must go on bm-2, not bm-1 or bm-3."""
        bms = [make_bm("bm-1"), make_bm("bm-2"), make_bm("bm-3")]
        vms = [make_vm("vm-1", candidates=["bm-2"])]
        r = solve(vms, bms)
        assert r.success
        assert amap(r)["vm-1"] == "bm-2"

    def test_candidate_with_no_capacity_fails(self):
        """Candidate BM exists but has no capacity → fail."""
        bms = [make_bm("bm-1", cpu=2), make_bm("bm-2", cpu=64)]
        vms = [make_vm("vm-1", cpu=4, candidates=["bm-1"])]
        r = solve(vms, bms)
        assert not r.success  # bm-1 too small, bm-2 not a candidate


# ===========================================================================
# 4. Anti-affinity: spread across AGs
# ===========================================================================

class TestAntiAffinity:

    def test_spread_3_vms_across_3_ags(self):
        bms = [make_bm(f"bm-{i}", ag=f"ag-{i}") for i in range(3)]
        vms = [make_vm(f"vm-{i}") for i in range(3)]
        rules = [AntiAffinityRule(group_id="g1", vm_ids=["vm-0", "vm-1", "vm-2"], max_per_ag=1)]
        r = solve(vms, bms, rules)
        assert r.success
        ags = {a.ag for a in r.assignments}
        assert len(ags) == 3  # all different AGs

    def test_infeasible_not_enough_ags(self):
        """2 VMs, max_per_ag=1, but only 1 AG → impossible."""
        bms = [make_bm("bm-1", ag="ag-a"), make_bm("bm-2", ag="ag-a")]
        vms = [make_vm("vm-0"), make_vm("vm-1")]
        rules = [AntiAffinityRule(group_id="g1", vm_ids=["vm-0", "vm-1"], max_per_ag=1)]
        r = solve(vms, bms, rules)
        assert not r.success

    def test_max_per_ag_2(self):
        """4 VMs, max_per_ag=2, 2 AGs → 2 VMs per AG."""
        bms = [
            make_bm("bm-a1", ag="ag-a"), make_bm("bm-a2", ag="ag-a"),
            make_bm("bm-b1", ag="ag-b"),
        ]
        vms = [make_vm(f"vm-{i}") for i in range(4)]
        rules = [AntiAffinityRule(group_id="g1", vm_ids=[f"vm-{i}" for i in range(4)], max_per_ag=2)]
        r = solve(vms, bms, rules)
        assert r.success
        ag_counts = {}
        for a in r.assignments:
            ag_counts[a.ag] = ag_counts.get(a.ag, 0) + 1
        assert all(c <= 2 for c in ag_counts.values())

    def test_auto_generate_rules(self):
        """With auto_generate=True, masters auto-spread across AGs."""
        bms = [make_bm(f"bm-{i}", ag=f"ag-{i}") for i in range(3)]
        vms = [make_vm(f"m-{i}", role=NodeRole.MASTER, ip_type="routable") for i in range(3)]
        r = solve(vms, bms, auto_generate_anti_affinity=True)
        assert r.success
        assert len({a.ag for a in r.assignments}) == 3

    def test_auto_generate_max_per_ag_dynamic(self):
        """5 masters / 3 AGs → ceil(5/3)=2 → allows 2/2/1 distribution."""
        bms = []
        for ag_i in range(3):
            for bm_i in range(2):
                bms.append(make_bm(f"bm-ag{ag_i}-{bm_i}", ag=f"ag-{ag_i}"))
        vms = [make_vm(f"m-{i}", role=NodeRole.MASTER, ip_type="routable") for i in range(5)]
        r = solve(vms, bms, auto_generate_anti_affinity=True)

        assert r.success, f"Failed: {r.solver_status}"
        assert len(r.assignments) == 5

        ag_counts = {}
        for a in r.assignments:
            ag_counts[a.ag] = ag_counts.get(a.ag, 0) + 1
        assert max(ag_counts.values()) <= 2, f"AG distribution: {ag_counts}"
        assert len(ag_counts) == 3, f"Expected 3 AGs used, got: {ag_counts}"

    def test_auto_generate_6_workers_3_ags(self):
        """6 workers / 3 AGs → ceil(6/3)=2 → exactly 2/2/2."""
        bms = []
        for ag_i in range(3):
            for bm_i in range(2):
                bms.append(make_bm(f"bm-ag{ag_i}-{bm_i}", ag=f"ag-{ag_i}"))
        vms = [make_vm(f"w-{i}", role=NodeRole.WORKER, ip_type="routable") for i in range(6)]
        r = solve(vms, bms, auto_generate_anti_affinity=True)

        assert r.success
        ag_counts = {}
        for a in r.assignments:
            ag_counts[a.ag] = ag_counts.get(a.ag, 0) + 1
        assert max(ag_counts.values()) <= 2, f"AG distribution: {ag_counts}"
        assert len(ag_counts) == 3

    def test_auto_generate_groups_by_ip_type(self):
        """VMs with different ip_types form separate groups."""
        bms = [make_bm(f"bm-{i}", ag=f"ag-{i}") for i in range(3)]
        vms = [
            make_vm("r-master-0", role=NodeRole.MASTER, ip_type="routable"),
            make_vm("r-master-1", role=NodeRole.MASTER, ip_type="routable"),
            make_vm("nr-master-0", role=NodeRole.MASTER, ip_type="non-routable"),
            make_vm("nr-master-1", role=NodeRole.MASTER, ip_type="non-routable"),
        ]
        r = solve(vms, bms, auto_generate_anti_affinity=True)

        assert r.success
        # routable masters should be in different AGs
        r_ags = {a.ag for a in r.assignments if a.vm_id.startswith("r-")}
        assert len(r_ags) == 2
        # non-routable masters should also be in different AGs
        nr_ags = {a.ag for a in r.assignments if a.vm_id.startswith("nr-")}
        assert len(nr_ags) == 2


# ===========================================================================
# 5. Partial placement
# ===========================================================================

class TestPartialPlacement:

    def test_places_as_many_as_possible(self):
        """3 VMs but only room for 2 → 2 placed, 1 unplaced."""
        bms = [make_bm("bm-1", cpu=8, mem=32_000, disk=200)]
        vms = [make_vm(f"vm-{i}") for i in range(3)]
        r = solve(vms, bms, allow_partial_placement=True)
        assert len(r.assignments) == 2
        assert len(r.unplaced_vms) == 1


# ===========================================================================
# 6. All node roles work
# ===========================================================================

class TestNodeRoles:

    def test_all_four_roles(self):
        bms = [make_bm(f"bm-{i}") for i in range(4)]
        vms = [
            make_vm("master-0", role=NodeRole.MASTER),
            make_vm("worker-0", role=NodeRole.WORKER),
            make_vm("infra-0", role=NodeRole.INFRA),
            make_vm("l4lb-0", role=NodeRole.L4LB),
        ]
        r = solve(vms, bms)
        assert r.success
        assert len(r.assignments) == 4


# ===========================================================================
# 7. Realistic scenario
# ===========================================================================

class TestRealisticCluster:
    """3 master + 3 worker + 2 infra + 2 l4lb across 3 AGs."""

    def test_full_cluster(self):
        bms = []
        for ag_i in range(3):
            for bm_i in range(2):
                bms.append(make_bm(
                    f"bm-ag{ag_i}-{bm_i}", cpu=64, mem=256_000, disk=2000,
                    ag=f"ag-{ag_i}", dc=f"dc-{ag_i}", rack=f"r-{bm_i}",
                ))

        vms = (
            [make_vm(f"master-{i}", cpu=8, mem=32_000, disk=200, role=NodeRole.MASTER, ip_type="routable") for i in range(3)]
            + [make_vm(f"worker-{i}", cpu=16, mem=64_000, disk=400, role=NodeRole.WORKER, ip_type="routable") for i in range(3)]
            + [make_vm(f"infra-{i}", cpu=8, mem=32_000, disk=200, role=NodeRole.INFRA, ip_type="routable") for i in range(2)]
            + [make_vm(f"l4lb-{i}", cpu=4, mem=16_000, disk=100, role=NodeRole.L4LB, ip_type="non-routable") for i in range(2)]
        )

        r = solve(vms, bms, auto_generate_anti_affinity=True)

        assert r.success, f"Failed: {r.solver_status}"
        assert len(r.assignments) == 10

        # Each role should spread across AGs
        for prefix, count in [("master-", 3), ("worker-", 3), ("infra-", 2), ("l4lb-", 2)]:
            ags = {a.ag for a in r.assignments if a.vm_id.startswith(prefix)}
            assert len(ags) == count, f"{prefix} spread: expected {count} AGs, got {ags}"


# ===========================================================================
# 8. JSON round-trip
# ===========================================================================

class TestSerialization:

    def test_round_trip(self):
        req = json.dumps({
            "vms": [{"id": "vm-1", "demand": {"cpu_cores": 4, "memory_mb": 16000, "disk_gb": 100}}],
            "baremetals": [{
                "id": "bm-1",
                "total_capacity": {"cpu_cores": 64, "memory_mb": 256000, "disk_gb": 2000},
                "topology": {"ag": "ag-1"},
            }],
            "config": {"auto_generate_anti_affinity": False},
        })

        # Pydantic: JSON string → model (取代 load_request_from_json)
        request = PlacementRequest.model_validate_json(req)
        result = VMPlacementSolver(request).solve()

        # Pydantic: model → JSON string → dict (取代 result_to_json)
        out = json.loads(result.model_dump_json())

        assert out["success"] is True
        assert out["assignments"][0]["vm_id"] == "vm-1"
        assert out["assignments"][0]["ag"] == "ag-1"

    def test_http_solve_endpoint(self):
        """FastAPI POST /v1/placement/solve 回傳正確結果。"""
        resp = client.post("/v1/placement/solve", json={
            "vms": [{"id": "vm-1", "demand": {"cpu_cores": 4, "memory_mb": 16000, "disk_gb": 100}}],
            "baremetals": [{
                "id": "bm-1",
                "total_capacity": {"cpu_cores": 64, "memory_mb": 256000, "disk_gb": 2000},
                "topology": {"ag": "ag-1"},
            }],
            "config": {"auto_generate_anti_affinity": False},
        })
        assert resp.status_code == 200
        out = resp.json()
        assert out["success"] is True
        assert out["assignments"][0]["vm_id"] == "vm-1"

    def test_http_healthz(self):
        """GET /healthz 回傳 healthy。"""
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "healthy"

    def test_http_invalid_request_returns_422(self):
        """送出缺少必要欄位的 JSON，FastAPI 自動回傳 422。"""
        resp = client.post("/v1/placement/solve", json={"vms": "not-a-list"})
        assert resp.status_code == 422
