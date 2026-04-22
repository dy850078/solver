"""
Objective weight interaction.

Fixed 300-VM × 100-BM input across several weight combos. We measure
timing + that consolidation-only uses at most as many BMs as all-on.
"""

from __future__ import annotations

import pytest

from app.models import NodeRole, Resources


def _used_bm_count(result) -> int:
    return len({a.baremetal_id for a in result.assignments})


@pytest.fixture
def _std_input(gen_bms, gen_vms):
    bms = gen_bms(100, cpu=64, mem=256_000, disk=2000, ags=5)
    vms = gen_vms(300, cpu=4, mem=16_000, disk=100, bm_ids=[b.id for b in bms])
    return vms, bms


def test_consolidation_only(_std_input, run_solve):
    vms, bms = _std_input
    run_solve(
        vms, bms,
        timeout_s=20, build_overhead_s=10,
        w_consolidation=10, w_headroom=0, w_slot_score=0, w_resource_waste=0,
        expect_statuses=("OPTIMAL", "FEASIBLE"),
        expect_all_placed=True,
    )


def test_headroom_only(_std_input, run_solve):
    vms, bms = _std_input
    run_solve(
        vms, bms,
        timeout_s=20, build_overhead_s=10,
        w_consolidation=0, w_headroom=100, w_slot_score=0, w_resource_waste=0,
        expect_statuses=("OPTIMAL", "FEASIBLE", "UNKNOWN"),
        expect_all_placed=False,
    )


def test_balanced_default(_std_input, run_solve):
    vms, bms = _std_input
    run_solve(
        vms, bms,
        timeout_s=20, build_overhead_s=10,
        # defaults: w_consolidation=10, w_headroom=8
        expect_statuses=("OPTIMAL", "FEASIBLE", "UNKNOWN"),
        expect_all_placed=False,
    )


@pytest.mark.slow
def test_slot_score_on(_std_input, run_solve):
    """Non-zero w_slot_score adds per-BM × per-spec × per-dim equalities."""
    vms, bms = _std_input
    specs = [
        Resources(cpu_cores=4, memory_mib=16_000, storage_gb=100),
        Resources(cpu_cores=8, memory_mib=32_000, storage_gb=200),
        Resources(cpu_cores=16, memory_mib=64_000, storage_gb=400),
        Resources(cpu_cores=32, memory_mib=128_000, storage_gb=800),
    ]
    run_solve(
        vms, bms,
        timeout_s=30, build_overhead_s=20,
        w_consolidation=10, w_headroom=8, w_slot_score=5,
        vm_specs=specs,
        expect_statuses=("OPTIMAL", "FEASIBLE", "UNKNOWN"),
        expect_all_placed=False,
    )


@pytest.mark.slow
def test_consolidation_lower_bound(_std_input, run_solve):
    """
    Consolidation-only must not use more BMs than consolidation+headroom.
    Runs both variants and compares BM usage.
    """
    vms, bms = _std_input
    cons_only = run_solve(
        vms, bms,
        timeout_s=15, build_overhead_s=10,
        w_consolidation=10, w_headroom=0, w_slot_score=0, w_resource_waste=0,
        expect_statuses=("OPTIMAL", "FEASIBLE"),
        expect_all_placed=True,
    )
    all_on = run_solve(
        vms, bms,
        timeout_s=15, build_overhead_s=10,
        w_consolidation=10, w_headroom=8, w_slot_score=0,
        expect_statuses=("OPTIMAL", "FEASIBLE", "UNKNOWN"),
        expect_all_placed=False,
    )
    if all_on.assignments and cons_only.assignments:
        c_only_bms = _used_bm_count(cons_only)
        all_on_bms = _used_bm_count(all_on)
        assert c_only_bms <= all_on_bms, (
            f"consolidation-only used {c_only_bms} BMs, "
            f"all-on used {all_on_bms} — consolidation failing to minimize"
        )
