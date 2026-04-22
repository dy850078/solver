"""
Pathological inputs — contention, no candidates, heterogeneous BMs,
fragmented candidates, dimension-zero edge cases, and input-error paths.
"""

from __future__ import annotations

import random

import pytest

from app.models import NodeRole, Resources


def test_all_vms_want_same_bm(gen_bms, gen_vms, run_solve):
    """
    100 VMs all list only bm-0 as candidate. bm-0 (64C) fits 16 default
    VMs → INFEASIBLE (no partial placement).
    """
    bms = gen_bms(20, ags=2)
    target = bms[0].id

    def _same_bm(i: int, ids: list[str], r: random.Random) -> list[str]:
        return [target]

    vms = gen_vms(100, bm_ids=[b.id for b in bms], candidates_fn=_same_bm)
    run_solve(
        vms, bms,
        timeout_s=10,
        build_overhead_s=5,
        expect_statuses=("INFEASIBLE",),
        expect_all_placed=False,
    )


def test_implicit_gpu_filter(gen_bms, gen_vms, run_solve):
    """
    50 VMs each require 1 GPU; only 5 BMs have GPUs. Implicit filtering
    via fits_in should succeed when capacity allows (5 × 8 = 40 < 50).
    Reduce VM count so it remains feasible.
    """
    gpu_bms = gen_bms(5, cpu=64, mem=256_000, disk=2000, gpu=8,
                      id_prefix="gpu", ags=2)
    cpu_bms = gen_bms(50, cpu=64, mem=256_000, disk=2000, gpu=0,
                      id_prefix="cpu", ags=5)
    bms = gpu_bms + cpu_bms

    vms = gen_vms(30, cpu=4, mem=16_000, disk=100, gpu=1,
                  role=NodeRole.WORKER)
    run_solve(
        vms, bms,
        timeout_s=10,
        build_overhead_s=5,
        expect_statuses=("OPTIMAL", "FEASIBLE"),
        expect_all_placed=True,
    )


def test_heterogeneous_bm_sizes(gen_bms, gen_vms, run_solve):
    """4 BM sizes cycled across 100 BMs, 200 VMs with mixed demands."""
    bms = gen_bms(
        100,
        sizes=[
            (8, 32_000, 500, 0),
            (32, 128_000, 1500, 0),
            (128, 512_000, 8000, 0),
            (64, 256_000, 2000, 8),
        ],
        ags=10,
    )
    vms = gen_vms(200, cpu=4, mem=16_000, disk=100)
    run_solve(
        vms, bms,
        timeout_s=20,
        build_overhead_s=10,
        expect_statuses=("OPTIMAL", "FEASIBLE", "UNKNOWN"),
        expect_all_placed=False,
    )


def test_deep_candidate_fragmentation(gen_bms, gen_vms, run_solve):
    """500 VMs each with 5 random candidates from 100 BMs. Sparse graph."""
    bms = gen_bms(100, ags=10)
    bm_ids = [b.id for b in bms]

    def _five(i: int, ids: list[str], r: random.Random) -> list[str]:
        return r.sample(ids, 5)

    vms = gen_vms(
        500, cpu=2, mem=8_000, disk=50,
        bm_ids=bm_ids, candidates_fn=_five, seed=42,
    )
    run_solve(
        vms, bms,
        timeout_s=30,
        build_overhead_s=15,
        expect_statuses=("OPTIMAL", "FEASIBLE", "UNKNOWN"),
        expect_all_placed=False,
    )


def test_zero_dim_bms_and_vms(gen_bms, gen_vms, run_solve):
    """
    No GPU anywhere. Exercises the total_d == 0 branch in
    _compute_headroom_penalties (which shouldn't divide by zero).
    """
    bms = gen_bms(20, cpu=64, mem=256_000, disk=2000, gpu=0, ags=5)
    vms = gen_vms(40, cpu=4, mem=16_000, disk=100, gpu=0)
    run_solve(
        vms, bms,
        timeout_s=10,
        build_overhead_s=5,
        expect_statuses=("OPTIMAL", "FEASIBLE"),
        expect_all_placed=True,
    )


def test_single_giant_bm(gen_bms, gen_vms, run_solve):
    """1 BM with 1024C / 4TiB / 32TB hosting 256 × 4C VMs. Storage sized so
    256 × 100 GB = 25.6 TB fits inside 32 TB capacity."""
    bms = gen_bms(1, cpu=1024, mem=4_096_000, disk=32000, ags=1)
    vms = gen_vms(256, cpu=4, mem=16_000, disk=100)
    run_solve(
        vms, bms,
        timeout_s=20,
        build_overhead_s=10,
        expect_statuses=("OPTIMAL", "FEASIBLE", "UNKNOWN"),
        expect_all_placed=False,
    )


def test_duplicate_bm_rejection(gen_bms, gen_vms, run_solve):
    """Duplicate baremetal IDs should return INPUT_ERROR quickly."""
    bms = gen_bms(5, ags=2)
    # Inject a duplicate by appending a copy of bm-0 with same id.
    bms.append(bms[0])
    vms = gen_vms(2)
    run_solve(
        vms, bms,
        timeout_s=10,
        build_overhead_s=2,
        expect_statuses=("INPUT_ERROR",),
        expect_all_placed=False,
    )


def test_duplicate_candidate_rejection(gen_bms, gen_vms, run_solve):
    """Duplicate candidate_baremetals list should return INPUT_ERROR."""
    bms = gen_bms(5, ags=2)

    def _dup(i: int, ids: list[str], r: random.Random) -> list[str]:
        return [ids[0], ids[0]]

    vms = gen_vms(1, bm_ids=[b.id for b in bms], candidates_fn=_dup)
    run_solve(
        vms, bms,
        timeout_s=10,
        build_overhead_s=2,
        expect_statuses=("INPUT_ERROR",),
        expect_all_placed=False,
    )


def test_empty_vms_list(gen_bms, run_solve):
    """0 VMs, 100 BMs → success with empty assignments."""
    bms = gen_bms(100, ags=5)
    run_solve(
        [], bms,
        timeout_s=5,
        build_overhead_s=2,
        expect_statuses=("OPTIMAL",),
        expect_all_placed=True,
    )


def test_empty_bms_list(gen_vms, run_solve):
    """100 VMs, 0 BMs → INFEASIBLE or INPUT_ERROR."""
    vms = gen_vms(100)
    run_solve(
        vms, [],
        timeout_s=5,
        build_overhead_s=2,
        expect_statuses=("INFEASIBLE", "INPUT_ERROR", "MODEL_INVALID"),
        expect_all_placed=False,
    )
