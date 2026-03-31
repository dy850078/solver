"""Tests for Exercise 5: GPU Affinity Constraint."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from exercises.conftest import make_bm, make_vm, amap
from exercises.tier2_solver_ext.ex5_gpu_affinity import solve_with_gpu_affinity
from app.models import NodeRole


class TestGpuAffinity:
    def test_ratio_half(self):
        """GPU BM must have at least half GPU VMs."""
        bms = [
            make_bm("bm-gpu", cpu=64, mem=256_000, gpu=4),
            make_bm("bm-no-gpu", cpu=64, mem=256_000, gpu=0),
        ]
        vms = [
            make_vm("vm-gpu-1", cpu=8, mem=32_000, gpu=1),
            make_vm("vm-gpu-2", cpu=8, mem=32_000, gpu=1),
            make_vm("vm-nogpu-1", cpu=8, mem=32_000, gpu=0),
            make_vm("vm-nogpu-2", cpu=8, mem=32_000, gpu=0),
        ]
        r = solve_with_gpu_affinity(vms, bms, min_gpu_numerator=1, min_gpu_denominator=2)
        assert r.success
        m = amap(r)
        on_gpu_bm = [v for v, b in m.items() if b == "bm-gpu"]
        gpu_vms_on_gpu_bm = [v for v in on_gpu_bm if "gpu" in v and "nogpu" not in v]
        if on_gpu_bm:
            assert len(gpu_vms_on_gpu_bm) * 2 >= len(on_gpu_bm) * 1

    def test_ratio_full(self):
        """ratio=1/1 → GPU BM only accepts GPU VMs."""
        bms = [
            make_bm("bm-gpu", cpu=64, mem=256_000, gpu=4),
            make_bm("bm-no-gpu", cpu=64, mem=256_000, gpu=0),
        ]
        vms = [
            make_vm("vm-gpu-1", cpu=8, mem=32_000, gpu=1),
            make_vm("vm-nogpu-1", cpu=8, mem=32_000, gpu=0),
        ]
        r = solve_with_gpu_affinity(vms, bms, min_gpu_numerator=1, min_gpu_denominator=1)
        assert r.success
        m = amap(r)
        assert m["vm-nogpu-1"] == "bm-no-gpu"

    def test_no_gpu_bm(self):
        """No GPU BMs → constraint is a no-op, behaves like original solver."""
        bms = [make_bm("bm-1"), make_bm("bm-2")]
        vms = [make_vm("vm-1"), make_vm("vm-2")]
        r = solve_with_gpu_affinity(vms, bms, min_gpu_numerator=1, min_gpu_denominator=2)
        assert r.success
        assert len(amap(r)) == 2

    def test_empty_gpu_bm_no_constraint(self):
        """GPU BM with no VMs placed → constraint doesn't trigger."""
        bms = [
            make_bm("bm-gpu", cpu=8, mem=32_000, gpu=4),
            make_bm("bm-big", cpu=128, mem=512_000, gpu=0),
        ]
        vms = [make_vm(f"vm-{i}", gpu=0) for i in range(3)]
        r = solve_with_gpu_affinity(vms, bms, min_gpu_numerator=1, min_gpu_denominator=1)
        assert r.success
        m = amap(r)
        for v, b in m.items():
            assert b == "bm-big"

    def test_gpu_vm_must_go_to_gpu_bm(self):
        """GPU VM needs GPU capacity → must be on GPU BM."""
        bms = [
            make_bm("bm-gpu", cpu=64, mem=256_000, gpu=4),
            make_bm("bm-no-gpu", cpu=64, mem=256_000, gpu=0),
        ]
        vms = [make_vm("vm-gpu", cpu=8, mem=32_000, gpu=2)]
        r = solve_with_gpu_affinity(vms, bms, min_gpu_numerator=1, min_gpu_denominator=2)
        assert r.success
        assert amap(r)["vm-gpu"] == "bm-gpu"
