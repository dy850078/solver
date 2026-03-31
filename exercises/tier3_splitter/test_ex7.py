"""Tests for Exercise 7: Global VM Limit."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from exercises.conftest import make_bm, make_req, make_vm, Resources
from exercises.tier3_splitter.ex7_global_vm_limit import (
    solve_split_with_global_limit,
)
from app.models import NodeRole, AntiAffinityRule


class TestGlobalMaxVms:
    def test_limits_total_across_requirements(self):
        """2 reqs with multi-spec, global max forces using bigger specs."""
        bms = [make_bm(f"bm-{i}", cpu=128, mem=512_000) for i in range(6)]
        # Two specs: 8-CPU (small) and 32-CPU (large)
        # Each req needs 32 CPU: could use 4×8 or 1×32
        # Without limit: waste-min might pick 4×8 each → 8 total
        # With global_max=4: must use some 32-CPU specs to stay under limit
        specs = [
            Resources(cpu_cores=8, memory_mib=32_000),
            Resources(cpu_cores=32, memory_mib=128_000),
        ]
        req_w = make_req(cpu=32, mem=128_000, role=NodeRole.WORKER, vm_specs=specs)
        req_m = make_req(cpu=32, mem=128_000, role=NodeRole.MASTER, vm_specs=specs)
        r = solve_split_with_global_limit(
            requirements=[req_w, req_m], bms=bms, global_max_vms=4,
        )
        assert r.success
        total = sum(d.count for d in r.split_decisions)
        assert total <= 4

    def test_no_limit_places_all(self):
        """Without global limit, each req gets what it needs."""
        bms = [make_bm(f"bm-{i}", cpu=128, mem=512_000) for i in range(10)]
        spec = Resources(cpu_cores=16, memory_mib=64_000)
        req_w = make_req(cpu=32, mem=128_000, role=NodeRole.WORKER, vm_specs=[spec])
        req_m = make_req(cpu=32, mem=128_000, role=NodeRole.MASTER, vm_specs=[spec])
        r = solve_split_with_global_limit(
            requirements=[req_w, req_m], bms=bms, global_max_vms=None,
        )
        assert r.success
        total = sum(d.count for d in r.split_decisions)
        assert total >= 4  # at least 2 workers + 2 masters


class TestGlobalMinVms:
    def test_min_vms_enforced(self):
        """Global min forces more VMs than coverage alone would need."""
        bms = [make_bm(f"bm-{i}", cpu=128, mem=512_000) for i in range(10)]
        spec = Resources(cpu_cores=8, memory_mib=32_000)
        # Coverage needs ceil(16/8)=2 VMs, but we set min_vms=5 on requirement
        # to ensure upper bound is high enough for global_min to work
        req = make_req(cpu=16, mem=64_000, vm_specs=[spec], min_vms=5)
        r = solve_split_with_global_limit(
            requirements=[req], bms=bms, global_min_vms=5,
        )
        assert r.success
        total = sum(d.count for d in r.split_decisions)
        assert total >= 5

    def test_infeasible_min_exceeds_capacity(self):
        """Global min too high for BM capacity → INFEASIBLE."""
        bms = [make_bm("bm-1", cpu=32, mem=128_000)]
        spec = Resources(cpu_cores=8, memory_mib=32_000)
        req = make_req(cpu=8, mem=32_000, vm_specs=[spec])
        r = solve_split_with_global_limit(
            requirements=[req], bms=bms, global_min_vms=10,
        )
        assert not r.success


class TestGlobalLimitWithExplicitVms:
    def test_explicit_vms_count_toward_limit(self):
        """Explicit VMs + synthetic VMs together respect global max."""
        bms = [make_bm(f"bm-{i}", cpu=128, mem=512_000) for i in range(6)]
        # 16-CPU spec: coverage needs 32/16=2 synthetic VMs
        spec = Resources(cpu_cores=16, memory_mib=64_000)
        explicit = [make_vm(f"explicit-{i}") for i in range(2)]
        req = make_req(cpu=32, mem=128_000, vm_specs=[spec])
        r = solve_split_with_global_limit(
            requirements=[req], bms=bms,
            explicit_vms=explicit,
            global_max_vms=4,  # 2 explicit + 2 synthetic = 4
        )
        assert r.success
        synthetic_count = sum(d.count for d in r.split_decisions)
        total = synthetic_count + len(explicit)
        assert total <= 4


class TestGlobalLimitWithAntiAffinity:
    def test_anti_affinity_and_global_limit(self):
        """Anti-affinity + global limit both apply."""
        bms = [
            make_bm("bm-1", ag="ag-1", cpu=128, mem=512_000),
            make_bm("bm-2", ag="ag-2", cpu=128, mem=512_000),
            make_bm("bm-3", ag="ag-3", cpu=128, mem=512_000),
        ]
        spec = Resources(cpu_cores=8, memory_mib=32_000)
        req = make_req(cpu=24, mem=96_000, vm_specs=[spec])
        r = solve_split_with_global_limit(
            requirements=[req], bms=bms, global_max_vms=3,
            auto_generate_anti_affinity=True,
        )
        assert r.success
        total = sum(d.count for d in r.split_decisions)
        assert total <= 3
