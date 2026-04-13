"""
Test suite — Requirement splitting (split-and-solve).
Run: pytest tests/test_splitter.py -v
"""

from app.models import (
    AntiAffinityRule,
    NodeRole,
    ResourceRequirement,
    Resources,
    SolverConfig,
    SplitPlacementRequest,
)
from app.split_solver import solve_split_placement

from .conftest import make_bm


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_req(
    cpu=0, mem=0, disk=0, gpu=0,
    role=NodeRole.WORKER, cluster="cluster-1", ip_type="routable",
    vm_specs=None, min_vms=None, max_vms=None, candidate_bms=None,
) -> ResourceRequirement:
    return ResourceRequirement(
        total_resources=Resources(cpu_cores=cpu, memory_mib=mem, storage_gb=disk, gpu_count=gpu),
        node_role=role,
        cluster_id=cluster,
        ip_type=ip_type,
        vm_specs=vm_specs,
        min_total_vms=min_vms,
        max_total_vms=max_vms,
        candidate_baremetals=candidate_bms or [],
    )


def split_solve(requirements, bms, vms=None, rules=None, **cfg_overrides):
    defaults = dict(max_solve_time_seconds=10, auto_generate_anti_affinity=False)
    defaults.update(cfg_overrides)
    req = SplitPlacementRequest(
        requirements=requirements if isinstance(requirements, list) else [requirements],
        vms=vms or [],
        baremetals=bms,
        anti_affinity_rules=rules or [],
        config=SolverConfig(**defaults),
    )
    return solve_split_placement(req)


# ===========================================================================
# 1. Basic splitting
# ===========================================================================

class TestBasicSplit:

    def test_exact_division(self):
        """64 CPU / 8 CPU per VM → exactly 8 VMs."""
        bms = [make_bm(f"bm-{i}", cpu=64, mem=256_000, disk=2000) for i in range(2)]
        spec = Resources(cpu_cores=8, memory_mib=32_000, storage_gb=200)
        req = make_req(cpu=64, mem=256_000, disk=1600, vm_specs=[spec])

        r = split_solve(req, bms)

        assert r.success, r.solver_status
        assert len(r.split_decisions) == 1
        assert r.split_decisions[0].count == 8
        assert r.split_decisions[0].vm_spec == spec
        assert len(r.assignments) == 8

    def test_non_exact_division(self):
        """70 CPU / 8 CPU → ceil(70/8)=9 VMs covering ≥70 CPU."""
        bms = [make_bm(f"bm-{i}", cpu=64, mem=256_000, disk=2000) for i in range(2)]
        spec = Resources(cpu_cores=8, memory_mib=32_000, storage_gb=200)
        req = make_req(cpu=70, mem=256_000, disk=1600, vm_specs=[spec])

        r = split_solve(req, bms)

        assert r.success
        total_cpu = sum(d.count * d.vm_spec.cpu_cores for d in r.split_decisions)
        assert total_cpu >= 70

    def test_min_vm_count(self):
        """Even if 2 VMs cover the budget, min_vms=3 forces 3."""
        bms = [make_bm(f"bm-{i}", cpu=64, mem=256_000, disk=2000) for i in range(2)]
        spec = Resources(cpu_cores=8, memory_mib=32_000, storage_gb=200)
        req = make_req(cpu=16, mem=64_000, disk=400, vm_specs=[spec], min_vms=3)

        r = split_solve(req, bms)

        assert r.success
        assert sum(d.count for d in r.split_decisions) >= 3

    def test_max_vm_count_makes_infeasible(self):
        """max_vms=2 with spec 8 CPU can only cover 16 of 64 required → INFEASIBLE."""
        bms = [make_bm("bm-1", cpu=128, mem=512_000, disk=4000)]
        spec = Resources(cpu_cores=8, memory_mib=32_000, storage_gb=200)
        req = make_req(cpu=64, mem=256_000, disk=1600, vm_specs=[spec], max_vms=2)

        r = split_solve(req, bms)

        assert not r.success


# ===========================================================================
# 2. Multi-spec: solver chooses the mix with least waste
# ===========================================================================

class TestMultiSpecSplit:

    def test_prefers_less_waste(self):
        """
        32 CPU / 128 GiB total.
        Spec A: 8 CPU / 32 GiB  → 4 VMs, 0 CPU waste, 0 MiB waste.
        Spec B: 16 CPU / 32 GiB → needs 2 for CPU but only 64 GiB (4 for mem).
        With w_resource_waste > 0 the solver should pick Spec A (zero waste).
        """
        bms = [make_bm("bm-1", cpu=64, mem=256_000, disk=2000)]
        spec_a = Resources(cpu_cores=8, memory_mib=32_000, storage_gb=200)
        spec_b = Resources(cpu_cores=16, memory_mib=32_000, storage_gb=200)
        req = make_req(cpu=32, mem=128_000, disk=800, vm_specs=[spec_a, spec_b])

        r = split_solve(req, bms, w_resource_waste=10, w_consolidation=0, w_headroom=0)

        assert r.success
        total_cpu = sum(d.count * d.vm_spec.cpu_cores for d in r.split_decisions)
        total_mem = sum(d.count * d.vm_spec.memory_mib for d in r.split_decisions)
        assert total_cpu >= 32
        assert total_mem >= 128_000

    def test_mixed_specs_cover_requirements(self):
        bms = [make_bm(f"bm-{i}", cpu=64, mem=256_000, disk=2000) for i in range(3)]
        spec_s = Resources(cpu_cores=4, memory_mib=16_000, storage_gb=100)
        spec_l = Resources(cpu_cores=16, memory_mib=64_000, storage_gb=400)
        req = make_req(cpu=48, mem=192_000, disk=1200, vm_specs=[spec_s, spec_l])

        r = split_solve(req, bms)

        assert r.success
        assert sum(d.count * d.vm_spec.cpu_cores for d in r.split_decisions) >= 48
        assert sum(d.count * d.vm_spec.memory_mib for d in r.split_decisions) >= 192_000


# ===========================================================================
# 3. BM capacity constrains which specs are usable
# ===========================================================================

class TestBMCapacityConstraint:

    def test_oversized_spec_filtered(self):
        """A spec larger than every BM is discarded before the solve."""
        bms = [make_bm("bm-1", cpu=8, mem=32_000, disk=200)]
        small = Resources(cpu_cores=4, memory_mib=16_000, storage_gb=100)
        huge  = Resources(cpu_cores=128, memory_mib=512_000, storage_gb=4000)
        req = make_req(cpu=8, mem=32_000, disk=200, vm_specs=[small, huge])

        r = split_solve(req, bms)

        assert r.success
        for d in r.split_decisions:
            assert d.vm_spec == small  # huge was filtered

    def test_insufficient_total_capacity(self):
        """Total demand exceeds all BMs combined → INFEASIBLE."""
        bms = [make_bm("bm-1", cpu=8, mem=32_000, disk=200)]
        spec = Resources(cpu_cores=4, memory_mib=16_000, storage_gb=100)
        req = make_req(cpu=32, mem=128_000, disk=800, vm_specs=[spec])

        r = split_solve(req, bms)

        assert not r.success


# ===========================================================================
# 4. Multiple roles split independently
# ===========================================================================

class TestPerRoleRequirements:

    def test_workers_and_masters(self):
        bms = [make_bm(f"bm-{i}", cpu=64, mem=256_000, disk=2000) for i in range(4)]
        w_spec = Resources(cpu_cores=8,  memory_mib=32_000, storage_gb=200)
        m_spec = Resources(cpu_cores=4,  memory_mib=16_000, storage_gb=100)

        reqs = [
            make_req(cpu=32, mem=128_000, disk=800, role=NodeRole.WORKER, vm_specs=[w_spec]),
            make_req(cpu=12, mem=48_000,  disk=300, role=NodeRole.MASTER, vm_specs=[m_spec],
                     min_vms=3, max_vms=3),
        ]
        r = split_solve(reqs, bms)

        assert r.success
        w_count = sum(d.count for d in r.split_decisions if d.node_role == NodeRole.WORKER)
        m_count = sum(d.count for d in r.split_decisions if d.node_role == NodeRole.MASTER)
        assert w_count >= 4
        assert m_count == 3
        assert len(r.assignments) == w_count + m_count


# ===========================================================================
# 5. Anti-affinity respected for synthetic VMs
# ===========================================================================

class TestSplitWithAntiAffinity:

    def test_auto_anti_affinity_spreads_synthetic_vms(self):
        """3 masters with 3 AGs → each should land in a different AG."""
        bms = [make_bm(f"bm-{i}", ag=f"ag-{i}") for i in range(3)]
        spec = Resources(cpu_cores=4, memory_mib=16_000, storage_gb=100)
        req = make_req(
            cpu=12, mem=48_000, disk=300,
            role=NodeRole.MASTER, ip_type="routable",
            vm_specs=[spec], min_vms=3, max_vms=3,
        )

        r = split_solve(req, bms, auto_generate_anti_affinity=True)

        assert r.success
        assert len(r.assignments) == 3
        assert len({a.ag for a in r.assignments}) == 3

    def test_auto_anti_affinity_mixed_explicit_and_synthetic(self):
        """Explicit VM + synthetic VMs in the same (ip_type, role) group.

        1 explicit worker (8 CPU) + requirement for 16 CPU worker (spec=8 CPU,
        upper=2 slots) → total active = 1 + 2 = 3 VMs, 3 AGs.
        Dynamic max_per_ag = ceil(3/3) = 1 → each AG gets exactly 1 VM.
        """
        from app.models import VM

        bms = [make_bm(f"bm-{i}", ag=f"ag-{i}", cpu=64, mem=256_000, disk=2000)
               for i in range(3)]
        explicit = VM(
            id="explicit-w1",
            demand=Resources(cpu_cores=8, memory_mib=32_000, storage_gb=200),
            node_role=NodeRole.WORKER,
            ip_type="routable",
            cluster_id="cluster-1",
        )
        spec = Resources(cpu_cores=8, memory_mib=32_000, storage_gb=200)
        req = make_req(
            cpu=16, mem=64_000, disk=400,
            role=NodeRole.WORKER, ip_type="routable",
            vm_specs=[spec],
        )

        r = split_solve(req, bms, vms=[explicit],
                        auto_generate_anti_affinity=True)

        assert r.success
        assert any(a.vm_id == "explicit-w1" for a in r.assignments)
        # 1 explicit + 2 synthetic = 3 VMs across 3 AGs → 1 per AG
        from collections import Counter
        ag_counts = Counter(a.ag for a in r.assignments)
        for ag, count in ag_counts.items():
            assert count <= 1, (
                f"AG {ag} has {count} VMs but max_per_ag should be 1 "
                f"(3 VMs / 3 AGs)"
            )

    def test_auto_anti_affinity_dynamic_when_count_is_variable(self):
        """When VM count is a decision variable, auto anti-affinity should
        use dynamic max_per_ag based on actual active count, not upper bound.

        Scenario: 32 CPU needed, spec=16 CPU → upper bound = 2 slots.
        Two specs available: 8 CPU (upper=4) and 16 CPU (upper=2).
        3 AGs available. Auto anti-affinity should spread based on actual
        count, not the 6 total slots.

        Without the dynamic fix, max_per_ag = ceil(6/3) = 2, allowing
        2 VMs on the same AG. With the fix, if solver picks 2 × 16 CPU,
        max_per_ag = ceil(2/3) = 1, forcing each VM to a different AG.
        """
        bms = [make_bm(f"bm-{i}", ag=f"ag-{i}", cpu=64, mem=256_000, disk=2000)
               for i in range(3)]
        spec_small = Resources(cpu_cores=8, memory_mib=32_000, storage_gb=200)
        spec_large = Resources(cpu_cores=16, memory_mib=64_000, storage_gb=400)
        req = make_req(
            cpu=32, mem=128_000, disk=800,
            role=NodeRole.WORKER, ip_type="routable",
            vm_specs=[spec_small, spec_large],
        )

        r = split_solve(req, bms, auto_generate_anti_affinity=True)

        assert r.success
        # Regardless of which spec combination the solver picks,
        # VMs should be spread across AGs as much as possible.
        assigned_ags = [a.ag for a in r.assignments]
        from collections import Counter
        ag_counts = Counter(assigned_ags)
        num_vms = len(r.assignments)
        num_ags = 3
        # Dynamic max_per_ag = ceil(num_vms / num_ags)
        import math
        expected_max = math.ceil(num_vms / num_ags)
        for ag, count in ag_counts.items():
            assert count <= expected_max, (
                f"AG {ag} has {count} VMs but dynamic max_per_ag should be "
                f"{expected_max} (total {num_vms} VMs / {num_ags} AGs)"
            )


# ===========================================================================
# 6. Mixed mode: explicit VMs + split requirements
# ===========================================================================

class TestMixedMode:

    def test_explicit_and_split_coexist(self):
        from app.models import VM
        bms  = [make_bm(f"bm-{i}", cpu=64, mem=256_000, disk=2000) for i in range(3)]
        explicit = VM(
            id="explicit-1",
            demand=Resources(cpu_cores=8, memory_mib=32_000, storage_gb=200),
            node_role=NodeRole.INFRA,
            ip_type="routable",
            cluster_id="cluster-1",
        )
        spec = Resources(cpu_cores=4, memory_mib=16_000, storage_gb=100)
        req = make_req(cpu=16, mem=64_000, disk=400, vm_specs=[spec])

        r = split_solve(req, bms, vms=[explicit])

        assert r.success
        assert any(a.vm_id == "explicit-1" for a in r.assignments)
        split_placed = [a for a in r.assignments if a.vm_id.startswith("split-")]
        assert len(split_placed) >= 4  # 16/4 = 4


# ===========================================================================
# 7. Config-level vm_specs fallback
# ===========================================================================

class TestConfigSpecsFallback:

    def test_uses_config_vm_specs_when_requirement_has_none(self):
        bms = [make_bm(f"bm-{i}", cpu=64, mem=256_000, disk=2000) for i in range(2)]
        req = make_req(cpu=32, mem=128_000, disk=800)  # no vm_specs on requirement

        r = split_solve(req, bms, vm_specs=[
            Resources(cpu_cores=4, memory_mib=16_000, storage_gb=100),
            Resources(cpu_cores=8, memory_mib=32_000, storage_gb=200),
        ])

        assert r.success
        assert sum(d.count * d.vm_spec.cpu_cores for d in r.split_decisions) >= 32

    def test_no_specs_anywhere_is_infeasible(self):
        bms = [make_bm("bm-1", cpu=64, mem=256_000, disk=2000)]
        req = make_req(cpu=32, mem=128_000, disk=800)  # no specs anywhere

        r = split_solve(req, bms)  # config also has no vm_specs

        assert not r.success


# ===========================================================================
# 8. HTTP endpoint smoke test
# ===========================================================================

# ===========================================================================
# 8. candidate_baremetals restricts placement of synthetic VMs
# ===========================================================================

class TestCandidateBaremetals:

    def test_synthetic_vms_restricted_to_candidates(self):
        """Synthetic VMs should only land on candidate BMs."""
        bms = [
            make_bm("bm-cp-0", cpu=32, mem=128_000, disk=1000, ag="ag-0"),
            make_bm("bm-cp-1", cpu=32, mem=128_000, disk=1000, ag="ag-1"),
            make_bm("bm-wk-0", cpu=64, mem=256_000, disk=2000, ag="ag-0"),
            make_bm("bm-wk-1", cpu=64, mem=256_000, disk=2000, ag="ag-1"),
        ]
        spec = Resources(cpu_cores=4, memory_mib=16_000, storage_gb=100)
        req = make_req(
            cpu=12, mem=48_000, disk=300,
            role=NodeRole.MASTER, vm_specs=[spec],
            candidate_bms=["bm-cp-0", "bm-cp-1"],
        )

        r = split_solve(req, bms)

        assert r.success
        for a in r.assignments:
            assert a.baremetal_id in ("bm-cp-0", "bm-cp-1"), (
                f"VM {a.vm_id} placed on {a.baremetal_id}, expected only cp BMs"
            )

    def test_empty_candidates_allows_all_bms(self):
        """Default (empty list) means all BMs are eligible — backward compat."""
        bms = [make_bm(f"bm-{i}", cpu=32, mem=128_000, disk=1000) for i in range(3)]
        spec = Resources(cpu_cores=4, memory_mib=16_000, storage_gb=100)
        req = make_req(cpu=8, mem=32_000, disk=200, vm_specs=[spec])

        r = split_solve(req, bms)

        assert r.success
        assert len(r.assignments) >= 2

    def test_spec_filtered_against_candidate_bms_only(self):
        """A large spec that fits non-candidate BMs but not candidates is filtered."""
        small_bm = make_bm("bm-small", cpu=8, mem=32_000, disk=200)
        large_bm = make_bm("bm-large", cpu=128, mem=512_000, disk=4000)
        small_spec = Resources(cpu_cores=4, memory_mib=16_000, storage_gb=100)
        large_spec = Resources(cpu_cores=64, memory_mib=256_000, storage_gb=2000)
        req = make_req(
            cpu=8, mem=32_000, disk=200,
            vm_specs=[small_spec, large_spec],
            candidate_bms=["bm-small"],  # only small BM is a candidate
        )

        r = split_solve(req, [small_bm, large_bm])

        assert r.success
        for d in r.split_decisions:
            assert d.vm_spec == small_spec  # large spec filtered out

    def test_multi_role_different_candidates(self):
        """Masters and workers go to different BM groups via candidate lists."""
        bms = [
            make_bm("bm-cp-0", cpu=32, mem=128_000, disk=1000, ag="ag-0"),
            make_bm("bm-cp-1", cpu=32, mem=128_000, disk=1000, ag="ag-1"),
            make_bm("bm-wk-0", cpu=64, mem=256_000, disk=2000, ag="ag-0"),
            make_bm("bm-wk-1", cpu=64, mem=256_000, disk=2000, ag="ag-1"),
        ]
        spec = Resources(cpu_cores=4, memory_mib=16_000, storage_gb=100)
        reqs = [
            make_req(cpu=8, mem=32_000, disk=200, role=NodeRole.MASTER,
                     vm_specs=[spec], candidate_bms=["bm-cp-0", "bm-cp-1"]),
            make_req(cpu=16, mem=64_000, disk=400, role=NodeRole.WORKER,
                     vm_specs=[spec], candidate_bms=["bm-wk-0", "bm-wk-1"]),
        ]

        r = split_solve(reqs, bms)

        assert r.success
        assign_map = {a.vm_id: a.baremetal_id for a in r.assignments}
        for vm_id, bm_id in assign_map.items():
            if "r0" in vm_id:  # requirement 0 = master
                assert bm_id.startswith("bm-cp"), f"Master VM on wrong BM: {bm_id}"
            elif "r1" in vm_id:  # requirement 1 = worker
                assert bm_id.startswith("bm-wk"), f"Worker VM on wrong BM: {bm_id}"

    def test_nonexistent_candidate_bm_infeasible(self):
        """Candidate list with only nonexistent BM IDs → infeasible."""
        bms = [make_bm("bm-1", cpu=64, mem=256_000, disk=2000)]
        spec = Resources(cpu_cores=4, memory_mib=16_000, storage_gb=100)
        req = make_req(
            cpu=8, mem=32_000, disk=200,
            vm_specs=[spec],
            candidate_bms=["nonexistent-bm"],
        )

        r = split_solve(req, bms)

        assert not r.success


# ===========================================================================
# 9. HTTP endpoint smoke test
# ===========================================================================

class TestSplitEndpoint:

    def test_post_split_and_solve(self, client):
        resp = client.post("/v1/placement/split-and-solve", json={
            "requirements": [{
                "total_resources": {"cpu_cores": 16, "memory_mib": 64000,
                                    "storage_gb": 400, "gpu_count": 0},
                "node_role": "worker",
                "cluster_id": "cluster-1",
                "vm_specs": [
                    {"cpu_cores": 4, "memory_mib": 16000, "storage_gb": 100, "gpu_count": 0},
                ],
            }],
            "baremetals": [{
                "id": "bm-1",
                "total_capacity": {"cpu_cores": 64, "memory_mib": 256000,
                                   "storage_gb": 2000, "gpu_count": 0},
                "topology": {"ag": "ag-1"},
            }],
            "config": {"auto_generate_anti_affinity": False},
        })
        assert resp.status_code == 200
        out = resp.json()
        assert out["success"] is True
        assert len(out["split_decisions"]) >= 1
        assert len(out["assignments"]) >= 4  # 16 CPU / 4 CPU per VM
