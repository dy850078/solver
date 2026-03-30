"""
Test suite — Requirement splitting (split-and-solve).

Tests the ResourceSplitter + VMPlacementSolver joint optimization.
Run: pytest tests/test_splitter.py -v
"""

from app.models import (
    Resources,
    NodeRole,
    AntiAffinityRule,
    ResourceRequirement,
    SplitPlacementRequest,
)
from app.split_solver import solve_split_placement

from .conftest import make_bm


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_requirement(
    cpu=0, mem=0, disk=0, gpu=0,
    role=NodeRole.WORKER, cluster="cluster-1", ip_type="routable",
    vm_specs=None, min_vms=None, max_vms=None,
):
    return ResourceRequirement(
        total_resources=Resources(
            cpu_cores=cpu, memory_mib=mem, storage_gb=disk, gpu_count=gpu,
        ),
        cluster_id=cluster,
        node_role=role,
        ip_type=ip_type,
        vm_specs=[Resources(**s) if isinstance(s, dict) else s for s in vm_specs] if vm_specs else None,
        min_total_vms=min_vms,
        max_total_vms=max_vms,
    )


def split_solve(requirements, bms, vms=None, rules=None, **config_overrides):
    """Convenience wrapper for split-and-solve tests."""
    from app.models import SolverConfig
    cfg = dict(max_solve_time_seconds=10, auto_generate_anti_affinity=False)
    cfg.update(config_overrides)
    request = SplitPlacementRequest(
        requirements=requirements if isinstance(requirements, list) else [requirements],
        vms=vms or [],
        baremetals=bms,
        anti_affinity_rules=rules or [],
        config=SolverConfig(**cfg),
    )
    return solve_split_placement(request)


# ===========================================================================
# 1. Basic splitting: single spec, exact division
# ===========================================================================

class TestBasicSplit:

    def test_single_spec_exact_division(self):
        """64 CPU total / 8 CPU per spec → 8 VMs."""
        bms = [make_bm(f"bm-{i}", cpu=64, mem=256_000, disk=2000) for i in range(2)]
        spec = Resources(cpu_cores=8, memory_mib=32_000, storage_gb=200)
        req = make_requirement(cpu=64, mem=256_000, disk=1600, vm_specs=[spec])

        r = split_solve(req, bms)

        assert r.success, f"Failed: {r.solver_status}"
        assert len(r.split_decisions) == 1
        assert r.split_decisions[0].count == 8
        assert r.split_decisions[0].vm_spec == spec
        assert len(r.assignments) == 8

    def test_single_spec_non_exact_division(self):
        """70 CPU total / 8 CPU per spec → ceil(70/8) = 9 VMs (72 CPU >= 70)."""
        bms = [make_bm(f"bm-{i}", cpu=64, mem=256_000, disk=2000) for i in range(2)]
        spec = Resources(cpu_cores=8, memory_mib=32_000, storage_gb=200)
        req = make_requirement(cpu=70, mem=256_000, disk=1600, vm_specs=[spec])

        r = split_solve(req, bms)

        assert r.success
        total_cpu = sum(d.count * d.vm_spec.cpu_cores for d in r.split_decisions)
        assert total_cpu >= 70

    def test_min_vm_count_enforced(self):
        """Require at least 3 VMs even if 2 would suffice for resources."""
        bms = [make_bm(f"bm-{i}", cpu=64, mem=256_000, disk=2000) for i in range(2)]
        spec = Resources(cpu_cores=8, memory_mib=32_000, storage_gb=200)
        req = make_requirement(cpu=16, mem=64_000, disk=400, vm_specs=[spec], min_vms=3)

        r = split_solve(req, bms)

        assert r.success
        total_count = sum(d.count for d in r.split_decisions)
        assert total_count >= 3

    def test_max_vm_count_enforced(self):
        """Limit to 2 VMs max even though more are needed for exact fit."""
        bms = [make_bm("bm-1", cpu=128, mem=512_000, disk=4000)]
        spec = Resources(cpu_cores=8, memory_mib=32_000, storage_gb=200)
        req = make_requirement(cpu=64, mem=256_000, disk=1600, vm_specs=[spec], max_vms=2)

        r = split_solve(req, bms)

        # Can't cover 64 CPU with 2 × 8 CPU VMs (only 16 CPU) → infeasible
        assert not r.success


# ===========================================================================
# 2. Multi-spec splitting: solver chooses optimal mix
# ===========================================================================

class TestMultiSpecSplit:

    def test_prefers_less_waste(self):
        """
        Total: 32 CPU, 128000 MiB.
        Spec A: 8 CPU, 32000 MiB → 4 VMs, 0 waste.
        Spec B: 16 CPU, 32000 MiB → 2 VMs for CPU but 64000 MiB (needs more).
        With waste minimization, solver should prefer Spec A.
        """
        bms = [make_bm("bm-1", cpu=64, mem=256_000, disk=2000)]
        spec_a = Resources(cpu_cores=8, memory_mib=32_000, storage_gb=200)
        spec_b = Resources(cpu_cores=16, memory_mib=32_000, storage_gb=200)
        req = make_requirement(
            cpu=32, mem=128_000, disk=800,
            vm_specs=[spec_a, spec_b],
        )

        r = split_solve(req, bms, w_resource_waste=10, w_consolidation=0, w_headroom=0)

        assert r.success
        total_cpu = sum(d.count * d.vm_spec.cpu_cores for d in r.split_decisions)
        total_mem = sum(d.count * d.vm_spec.memory_mib for d in r.split_decisions)
        assert total_cpu >= 32
        assert total_mem >= 128_000

    def test_mixed_specs_cover_requirements(self):
        """Solver can mix specs to cover requirements."""
        bms = [make_bm(f"bm-{i}", cpu=64, mem=256_000, disk=2000) for i in range(3)]
        spec_small = Resources(cpu_cores=4, memory_mib=16_000, storage_gb=100)
        spec_large = Resources(cpu_cores=16, memory_mib=64_000, storage_gb=400)
        req = make_requirement(
            cpu=48, mem=192_000, disk=1200,
            vm_specs=[spec_small, spec_large],
        )

        r = split_solve(req, bms)

        assert r.success
        total_cpu = sum(d.count * d.vm_spec.cpu_cores for d in r.split_decisions)
        total_mem = sum(d.count * d.vm_spec.memory_mib for d in r.split_decisions)
        assert total_cpu >= 48
        assert total_mem >= 192_000


# ===========================================================================
# 3. BM capacity affects split
# ===========================================================================

class TestBMCapacityAffectsSplit:

    def test_spec_too_large_for_bms_filtered(self):
        """If a spec doesn't fit any BM, it's filtered out."""
        bms = [make_bm("bm-1", cpu=8, mem=32_000, disk=200)]
        spec_small = Resources(cpu_cores=4, memory_mib=16_000, storage_gb=100)
        spec_huge = Resources(cpu_cores=128, memory_mib=512_000, storage_gb=4000)
        req = make_requirement(cpu=8, mem=32_000, disk=200, vm_specs=[spec_small, spec_huge])

        r = split_solve(req, bms)

        assert r.success
        # Only small spec should be used (huge doesn't fit any BM)
        for d in r.split_decisions:
            assert d.vm_spec == spec_small

    def test_infeasible_when_bm_capacity_insufficient(self):
        """Total demand exceeds total BM capacity → infeasible."""
        bms = [make_bm("bm-1", cpu=8, mem=32_000, disk=200)]
        spec = Resources(cpu_cores=4, memory_mib=16_000, storage_gb=100)
        req = make_requirement(cpu=32, mem=128_000, disk=800, vm_specs=[spec])

        r = split_solve(req, bms)

        assert not r.success


# ===========================================================================
# 4. Per-role requirements
# ===========================================================================

class TestPerRoleRequirements:

    def test_multiple_roles(self):
        """Split workers and masters separately with different specs."""
        bms = [make_bm(f"bm-{i}", cpu=64, mem=256_000, disk=2000) for i in range(4)]

        worker_spec = Resources(cpu_cores=8, memory_mib=32_000, storage_gb=200)
        master_spec = Resources(cpu_cores=4, memory_mib=16_000, storage_gb=100)

        reqs = [
            make_requirement(
                cpu=32, mem=128_000, disk=800,
                role=NodeRole.WORKER, vm_specs=[worker_spec],
            ),
            make_requirement(
                cpu=12, mem=48_000, disk=300,
                role=NodeRole.MASTER, vm_specs=[master_spec],
                min_vms=3, max_vms=3,
            ),
        ]

        r = split_solve(reqs, bms)

        assert r.success
        worker_decisions = [d for d in r.split_decisions if d.node_role == NodeRole.WORKER]
        master_decisions = [d for d in r.split_decisions if d.node_role == NodeRole.MASTER]

        worker_count = sum(d.count for d in worker_decisions)
        master_count = sum(d.count for d in master_decisions)

        assert worker_count >= 4  # 32/8 = 4
        assert master_count == 3  # fixed at 3

        total_assignments = len(r.assignments)
        assert total_assignments == worker_count + master_count


# ===========================================================================
# 5. Split + anti-affinity (joint optimization)
# ===========================================================================

class TestSplitWithAntiAffinity:

    def test_split_respects_auto_anti_affinity(self):
        """Synthetic VMs participate in auto anti-affinity spreading."""
        bms = [make_bm(f"bm-{i}", ag=f"ag-{i}") for i in range(3)]
        spec = Resources(cpu_cores=4, memory_mib=16_000, storage_gb=100)
        req = make_requirement(
            cpu=12, mem=48_000, disk=300,
            role=NodeRole.MASTER, ip_type="routable",
            vm_specs=[spec], min_vms=3, max_vms=3,
        )

        r = split_solve(req, bms, auto_generate_anti_affinity=True)

        assert r.success
        assert len(r.assignments) == 3
        ags = {a.ag for a in r.assignments}
        assert len(ags) == 3  # each master in a different AG


# ===========================================================================
# 6. Mixed mode: explicit VMs + split requirements
# ===========================================================================

class TestMixedMode:

    def test_explicit_and_split_coexist(self):
        """Explicit VMs + split requirements placed together."""
        from app.models import VM
        bms = [make_bm(f"bm-{i}", cpu=64, mem=256_000, disk=2000) for i in range(3)]

        explicit_vm = VM(
            id="explicit-vm-1",
            demand=Resources(cpu_cores=8, memory_mib=32_000, storage_gb=200),
            node_role=NodeRole.INFRA,
            ip_type="routable",
            cluster_id="cluster-1",
        )

        spec = Resources(cpu_cores=4, memory_mib=16_000, storage_gb=100)
        req = make_requirement(cpu=16, mem=64_000, disk=400, vm_specs=[spec])

        r = split_solve(req, bms, vms=[explicit_vm])

        assert r.success
        # Explicit VM should be placed
        explicit_placed = [a for a in r.assignments if a.vm_id == "explicit-vm-1"]
        assert len(explicit_placed) == 1
        # Split VMs should also be placed
        split_placed = [a for a in r.assignments if a.vm_id.startswith("split-")]
        assert len(split_placed) >= 4  # 16/4 = 4


# ===========================================================================
# 7. Auto-select specs from config
# ===========================================================================

class TestAutoSelectSpecs:

    def test_uses_config_vm_specs_when_not_specified(self):
        """When requirement doesn't specify vm_specs, uses config.vm_specs."""
        bms = [make_bm(f"bm-{i}", cpu=64, mem=256_000, disk=2000) for i in range(2)]
        config_specs = [
            Resources(cpu_cores=4, memory_mib=16_000, storage_gb=100),
            Resources(cpu_cores=8, memory_mib=32_000, storage_gb=200),
        ]
        req = make_requirement(cpu=32, mem=128_000, disk=800)  # no vm_specs

        r = split_solve(req, bms, vm_specs=config_specs)

        assert r.success
        total_cpu = sum(d.count * d.vm_spec.cpu_cores for d in r.split_decisions)
        assert total_cpu >= 32

    def test_no_specs_anywhere_fails(self):
        """No specs in requirement or config → no VMs can be created."""
        bms = [make_bm("bm-1", cpu=64, mem=256_000, disk=2000)]
        req = make_requirement(cpu=32, mem=128_000, disk=800)

        r = split_solve(req, bms)  # no vm_specs in config either

        assert not r.success


# ===========================================================================
# 8. HTTP endpoint
# ===========================================================================

class TestSplitEndpoint:

    def test_http_split_and_solve(self, client):
        """POST /v1/placement/split-and-solve returns correct result."""
        resp = client.post("/v1/placement/split-and-solve", json={
            "requirements": [{
                "total_resources": {
                    "cpu_cores": 16, "memory_mib": 64000,
                    "storage_gb": 400, "gpu_count": 0,
                },
                "cluster_id": "cluster-1",
                "node_role": "worker",
                "vm_specs": [
                    {"cpu_cores": 4, "memory_mib": 16000, "storage_gb": 100, "gpu_count": 0},
                ],
            }],
            "baremetals": [{
                "id": "bm-1",
                "total_capacity": {
                    "cpu_cores": 64, "memory_mib": 256000,
                    "storage_gb": 2000, "gpu_count": 0,
                },
                "topology": {"ag": "ag-1"},
            }],
            "config": {"auto_generate_anti_affinity": False},
        })
        assert resp.status_code == 200
        out = resp.json()
        assert out["success"] is True
        assert len(out["split_decisions"]) >= 1
        assert len(out["assignments"]) >= 4  # 16/4 = 4
