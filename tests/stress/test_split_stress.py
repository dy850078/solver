"""
Split-and-solve specific stress — slot explosion, many requirements,
min/max bounds, waste weight, candidate restrictions.
"""

from __future__ import annotations

import pytest

from app.models import NodeRole, Resources


@pytest.mark.slow
def test_slot_explosion_single_spec(gen_bms, run_split_solve, requirement_factory):
    """
    total cpu=1000 / spec cpu=10 → upper=100 slots (one spec). 50 BMs.
    """
    bms = gen_bms(50, cpu=64, mem=256_000, disk=2000, ags=5)
    req = requirement_factory(
        cpu=1000, mem=500_000, disk=5000,
        vm_specs=[Resources(cpu_cores=10, memory_mib=5_000, storage_gb=50)],
    )
    run_split_solve(
        [req], bms,
        timeout_s=30,
        build_overhead_s=20,
        expect_statuses=("OPTIMAL", "FEASIBLE", "UNKNOWN"),
        expect_all_placed=False,
    )


@pytest.mark.slow
def test_slot_explosion_five_specs(gen_bms, run_split_solve, requirement_factory):
    """
    Five specs with a large total budget: upper ≈ 100 per spec ≈ 500 slots.
    100 BMs.
    """
    bms = gen_bms(100, cpu=64, mem=256_000, disk=2000, ags=5)
    req = requirement_factory(
        cpu=1000, mem=4_000_000, disk=20_000,
        vm_specs=[
            Resources(cpu_cores=2, memory_mib=8_000, storage_gb=40),
            Resources(cpu_cores=4, memory_mib=16_000, storage_gb=80),
            Resources(cpu_cores=8, memory_mib=32_000, storage_gb=160),
            Resources(cpu_cores=16, memory_mib=64_000, storage_gb=320),
            Resources(cpu_cores=32, memory_mib=128_000, storage_gb=640),
        ],
    )
    run_split_solve(
        [req], bms,
        timeout_s=45,
        build_overhead_s=45,
        expect_statuses=("OPTIMAL", "FEASIBLE", "UNKNOWN"),
        expect_all_placed=False,
    )


@pytest.mark.huge
def test_small_specs_vs_huge_total(gen_bms, run_split_solve, requirement_factory):
    """
    Ceiling probe: total cpu=5000 / single spec cpu=1 → upper=5000 slots.
    May MODEL_INVALID or consume lots of memory. We only fail on crash.
    """
    bms = gen_bms(200, cpu=64, mem=256_000, disk=2000, ags=10)
    req = requirement_factory(
        cpu=5000, mem=250_000, disk=2500,
        vm_specs=[Resources(cpu_cores=1, memory_mib=50, storage_gb=1)],
    )
    run_split_solve(
        [req], bms,
        timeout_s=90,
        build_overhead_s=180,
        expect_statuses=("OPTIMAL", "FEASIBLE", "UNKNOWN"),
        expect_all_placed=False,
    )


def test_many_requirements(gen_bms, run_split_solve, requirement_factory):
    """
    20 requirements, each producing ~20 slots. 50 BMs. Exercises
    multiple auto-generated anti-affinity groups after split.
    """
    bms = gen_bms(50, cpu=64, mem=256_000, disk=2000, ags=5)
    spec = [Resources(cpu_cores=4, memory_mib=16_000, storage_gb=100)]
    reqs = [
        requirement_factory(
            cpu=80, mem=320_000, disk=2000,
            role=NodeRole.WORKER,
            ip_type=f"net-{i % 4}",
            cluster=f"cluster-{i}",
            vm_specs=spec,
        )
        for i in range(20)
    ]
    run_split_solve(
        reqs, bms,
        timeout_s=30,
        build_overhead_s=20,
        expect_statuses=("OPTIMAL", "FEASIBLE", "UNKNOWN"),
        expect_all_placed=False,
    )


def test_multi_req_mixed_sizes(gen_bms, run_split_solve, requirement_factory):
    """
    Realistic cluster: masters + workers + infra + L4LB + GPU workers.
    30 BMs with heterogeneous sizes.
    """
    bms = gen_bms(
        30,
        sizes=[
            (32, 128_000, 1000, 0),
            (64, 256_000, 2000, 0),
            (128, 512_000, 4000, 0),
            (64, 256_000, 2000, 8),  # GPU nodes
        ],
        ags=5,
    )

    reqs = [
        requirement_factory(
            cpu=24, mem=96_000, disk=600,
            role=NodeRole.MASTER,
            vm_specs=[Resources(cpu_cores=8, memory_mib=32_000, storage_gb=200)],
        ),
        requirement_factory(
            cpu=160, mem=640_000, disk=4000,
            role=NodeRole.INFRA,
            vm_specs=[Resources(cpu_cores=16, memory_mib=64_000, storage_gb=400)],
        ),
        requirement_factory(
            cpu=200, mem=800_000, disk=5000,
            role=NodeRole.WORKER,
            vm_specs=[
                Resources(cpu_cores=4, memory_mib=16_000, storage_gb=100),
                Resources(cpu_cores=8, memory_mib=32_000, storage_gb=200),
            ],
        ),
        requirement_factory(
            cpu=160, mem=80_000, disk=500,
            role=NodeRole.L4LB,
            vm_specs=[Resources(cpu_cores=32, memory_mib=16_000, storage_gb=100)],
        ),
        requirement_factory(
            cpu=32, mem=128_000, disk=800, gpu=8,
            role=NodeRole.WORKER, cluster="gpu-cluster",
            vm_specs=[Resources(cpu_cores=4, memory_mib=16_000, storage_gb=100, gpu_count=1)],
        ),
    ]

    run_split_solve(
        reqs, bms,
        timeout_s=30,
        build_overhead_s=15,
        expect_statuses=("OPTIMAL", "FEASIBLE", "UNKNOWN"),
        expect_all_placed=False,
    )


def test_split_min_max_bounds(gen_bms, run_split_solve, requirement_factory):
    """
    total cpu=80 with 3 specs [2, 4, 8]; min_total_vms=12 max_total_vms=20.
    """
    bms = gen_bms(10, cpu=64, mem=256_000, disk=2000, ags=3)
    req = requirement_factory(
        cpu=80, mem=320_000, disk=2000,
        vm_specs=[
            Resources(cpu_cores=2, memory_mib=8_000, storage_gb=50),
            Resources(cpu_cores=4, memory_mib=16_000, storage_gb=100),
            Resources(cpu_cores=8, memory_mib=32_000, storage_gb=200),
        ],
        min_vms=12, max_vms=20,
    )
    result = run_split_solve(
        [req], bms,
        timeout_s=15,
        build_overhead_s=10,
        expect_statuses=("OPTIMAL", "FEASIBLE"),
        expect_all_placed=True,
    )
    total_count = sum(d.count for d in result.split_decisions)
    assert 12 <= total_count <= 20, (
        f"Count {total_count} violated [12, 20] bounds"
    )


@pytest.mark.parametrize("w_waste", [0, 5, 50])
def test_split_waste_weight_interaction(w_waste, gen_bms,
                                         run_split_solve, requirement_factory):
    """Validate w_resource_waste scaling doesn't break correctness."""
    bms = gen_bms(30, cpu=64, mem=256_000, disk=2000, ags=5)
    reqs = [
        requirement_factory(
            cpu=100, mem=400_000, disk=2500,
            role=NodeRole.WORKER,
            vm_specs=[
                Resources(cpu_cores=4, memory_mib=16_000, storage_gb=100),
                Resources(cpu_cores=8, memory_mib=32_000, storage_gb=200),
                Resources(cpu_cores=16, memory_mib=64_000, storage_gb=400),
            ],
        ),
    ]
    run_split_solve(
        reqs, bms,
        timeout_s=20,
        build_overhead_s=10,
        w_resource_waste=w_waste,
        expect_statuses=("OPTIMAL", "FEASIBLE"),
        expect_all_placed=True,
    )


def test_split_with_candidate_restrictions(gen_bms, run_split_solve,
                                             requirement_factory):
    """3 reqs each restricted to disjoint 10-BM subsets of 40 BMs."""
    bms = gen_bms(40, cpu=64, mem=256_000, disk=2000, ags=4)
    bm_ids = [b.id for b in bms]

    reqs = [
        requirement_factory(
            cpu=64, mem=256_000, disk=2000,
            role=NodeRole.WORKER, cluster=f"c-{i}",
            vm_specs=[Resources(cpu_cores=8, memory_mib=32_000, storage_gb=200)],
            candidate_bms=bm_ids[i * 10:(i + 1) * 10],
        )
        for i in range(3)
    ]
    run_split_solve(
        reqs, bms,
        timeout_s=15,
        build_overhead_s=10,
        expect_statuses=("OPTIMAL", "FEASIBLE"),
        expect_all_placed=True,
    )


def test_split_with_explicit_vms(gen_bms, gen_vms, run_split_solve,
                                   requirement_factory):
    """50 explicit VMs + 1 requirement producing ~50 more synthetic."""
    bms = gen_bms(30, cpu=64, mem=256_000, disk=2000, ags=5)
    explicit_vms = gen_vms(
        50, cpu=4, mem=16_000, disk=100,
        id_prefix="explicit",
    )
    req = requirement_factory(
        cpu=200, mem=800_000, disk=5000,
        role=NodeRole.WORKER, ip_type="non-routable",
        vm_specs=[Resources(cpu_cores=4, memory_mib=16_000, storage_gb=100)],
    )
    run_split_solve(
        [req], bms, vms=explicit_vms,
        timeout_s=20,
        build_overhead_s=10,
        expect_statuses=("OPTIMAL", "FEASIBLE"),
        expect_all_placed=True,
    )


def test_split_infeasible_coverage(gen_bms, run_split_solve, requirement_factory):
    """
    max_total_vms * biggest_spec.cpu < total.cpu → INFEASIBLE at coverage.
    """
    bms = gen_bms(20, cpu=64, mem=256_000, disk=2000, ags=2)
    req = requirement_factory(
        cpu=1000, mem=4_000_000, disk=20_000,
        vm_specs=[Resources(cpu_cores=32, memory_mib=128_000, storage_gb=640)],
        max_vms=20,  # 20 × 32 = 640 < 1000 needed
    )
    run_split_solve(
        [req], bms,
        timeout_s=10,
        build_overhead_s=5,
        expect_statuses=("INFEASIBLE",),
        expect_all_placed=False,
    )
