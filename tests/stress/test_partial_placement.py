"""
Partial placement stress — allow_partial_placement=True on oversubscribed
workloads. Verifies the -1,000,000 × total_placed priority term in the
objective actually maximizes placements, and that anti-affinity still
holds for whatever gets placed.
"""

from __future__ import annotations

from collections import Counter

import pytest

from app.models import AntiAffinityRule, NodeRole, Resources


def test_oversubscribed_partial_200_over_100(gen_bms, gen_vms, run_solve):
    """
    100 BMs × 64C each = 6400 CPU. 200 VMs × 32C each = 6400 CPU demand.
    Memory is the bottleneck: 200 × 200GiB = 40 TiB, BMs 100 × 256 GiB =
    25.6 TiB → only ~128 placeable. Bump memory demand so ~120 fit.
    """
    bms = gen_bms(100, cpu=64, mem=256_000, disk=2000, ags=10)
    vms = gen_vms(200, cpu=32, mem=200_000, disk=500)
    result = run_solve(
        vms, bms,
        timeout_s=30,
        build_overhead_s=20,
        allow_partial_placement=True,
        expect_statuses=("OPTIMAL", "FEASIBLE", "UNKNOWN"),
        expect_all_placed=False,
    )
    # Should place at least 80 (well under 128 theoretical ceiling).
    assert len(result.assignments) >= 80, (
        f"Partial placement too low: placed={len(result.assignments)}"
    )


@pytest.mark.slow
def test_oversubscribed_partial_scale(gen_bms, gen_vms, run_solve):
    """1000 VMs into 200 BMs that can fit ~700."""
    bms = gen_bms(200, cpu=64, mem=256_000, disk=2000, ags=10)
    # Each BM fits ~4 VMs at 16C/64GiB → 200 × 4 = 800 placeable.
    vms = gen_vms(1000, cpu=16, mem=64_000, disk=400)
    result = run_solve(
        vms, bms,
        timeout_s=60,
        build_overhead_s=60,
        allow_partial_placement=True,
        expect_statuses=("OPTIMAL", "FEASIBLE", "UNKNOWN"),
        expect_all_placed=False,
    )
    assert len(result.assignments) >= 400, (
        f"Partial placement too low: placed={len(result.assignments)}"
    )


def test_partial_with_antiaffinity_conflict(gen_bms, gen_vms, run_solve):
    """
    30 VMs with max_per_ag=5 across 3 AGs → only 15 can be placed even
    though capacity allows all 30.
    """
    bms = gen_bms(30, cpu=64, mem=256_000, disk=2000, ags=3)
    vms = gen_vms(30, cpu=4, mem=16_000, disk=100)
    rule = AntiAffinityRule(
        group_id="tight-spread",
        vm_ids=[v.id for v in vms],
        max_per_ag=5,
    )
    result = run_solve(
        vms, bms,
        rules=[rule],
        timeout_s=15,
        build_overhead_s=10,
        allow_partial_placement=True,
        expect_statuses=("OPTIMAL", "FEASIBLE"),
        expect_all_placed=False,
    )
    # Verify at most 5 per AG among placed VMs.
    per_ag = Counter(a.ag for a in result.assignments)
    for ag, count in per_ag.items():
        assert count <= 5, (
            f"AG {ag} has {count} placements but max_per_ag=5"
        )
    assert len(result.assignments) <= 15, (
        f"Placed {len(result.assignments)} but spread caps total at 15"
    )


def test_partial_split_oversubscribed_is_infeasible(gen_bms, run_split_solve,
                                                      requirement_factory):
    """
    Splitter coverage is a hard constraint: total_resources must always be
    covered. Oversubscribing therefore returns INFEASIBLE even with
    allow_partial_placement=True. Documents that allow_partial_placement
    only relaxes placement, not split coverage.
    """
    bms = gen_bms(10, cpu=64, mem=256_000, disk=2000, ags=3)
    # Infra: 10 × 64 = 640 CPU; request 900.
    req = requirement_factory(
        cpu=900, mem=3_600_000, disk=9000,
        vm_specs=[Resources(cpu_cores=8, memory_mib=32_000, storage_gb=200)],
    )
    run_split_solve(
        [req], bms,
        timeout_s=20,
        build_overhead_s=10,
        allow_partial_placement=True,
        expect_statuses=("INFEASIBLE",),
        expect_all_placed=False,
    )


def test_partial_split_placement_fits(gen_bms, run_split_solve,
                                        requirement_factory):
    """
    Split sized to fit infra exactly, with allow_partial_placement=True.
    Smoke test that the flag doesn't break the split-and-solve path.
    """
    bms = gen_bms(12, cpu=64, mem=256_000, disk=2000, ags=3)
    req = requirement_factory(
        cpu=96, mem=384_000, disk=2400,
        vm_specs=[Resources(cpu_cores=4, memory_mib=16_000, storage_gb=100)],
    )
    run_split_solve(
        [req], bms,
        timeout_s=15,
        build_overhead_s=10,
        allow_partial_placement=True,
        expect_statuses=("OPTIMAL", "FEASIBLE"),
        expect_all_placed=True,
    )


