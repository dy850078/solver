"""
Constraint tightness stress — push utilization and candidate filtering
until the solver is near-infeasible or exactly matched.
"""

from __future__ import annotations

import random

import pytest

from app.models import NodeRole, Resources


def test_high_utilization_packing(gen_bms, gen_vms, run_solve):
    """
    100 BMs (64C / 256GiB / 2TB) vs 1500 VMs (4C / 16GiB / 100GB).
    1500 × 4 = 6000C; 100 × 64 = 6400C → 93.75% CPU utilization.
    Memory 1500 × 16 = 24TB vs 100 × 256 = 25.6TB → 93.75%.
    """
    bms = gen_bms(100, ags=10)
    vms = gen_vms(1500, bm_ids=[b.id for b in bms])
    run_solve(
        vms, bms,
        timeout_s=45,
        build_overhead_s=45,
        expect_statuses=("OPTIMAL", "FEASIBLE", "UNKNOWN"),
        expect_all_placed=False,
    )


def test_99pct_pre_used_capacity(gen_bms, gen_vms, run_solve):
    """
    50 BMs pre-filled to 99% (used_frac=0.99) leaving ~1% residual.
    50 VMs demand exactly that residual slice.
    """
    # 64C × 0.99 = 63 used → 1 available. Each VM demands 1C.
    bms = gen_bms(50, cpu=100, mem=100_000, disk=1000, used_frac=0.99, ags=5)
    vms = gen_vms(50, cpu=1, mem=1_000, disk=10, bm_ids=[b.id for b in bms])
    run_solve(
        vms, bms,
        timeout_s=15,
        build_overhead_s=10,
        expect_statuses=("OPTIMAL", "FEASIBLE"),
        expect_all_placed=True,
    )


def test_one_candidate_per_vm(gen_bms, gen_vms, run_solve):
    """
    200 VMs each with exactly 1 candidate. Random perfect matching.
    Near-unique assignment, tiny solution space.
    """
    bms = gen_bms(200, ags=10)
    bm_ids = [b.id for b in bms]
    rng = random.Random(99)
    perm = list(bm_ids)
    rng.shuffle(perm)

    def _one_cand(i: int, ids: list[str], r: random.Random) -> list[str]:
        return [perm[i]]

    vms = gen_vms(200, bm_ids=bm_ids, candidates_fn=_one_cand, seed=99)
    run_solve(
        vms, bms,
        timeout_s=15,
        build_overhead_s=10,
        expect_statuses=("OPTIMAL", "FEASIBLE"),
        expect_all_placed=True,
    )


def test_two_candidates_per_vm(gen_bms, gen_vms, run_solve):
    """300 VMs × 150 BMs, each VM lists 2 candidates. Near-bipartite matching."""
    bms = gen_bms(150, ags=10)
    bm_ids = [b.id for b in bms]

    def _two_cands(i: int, ids: list[str], r: random.Random) -> list[str]:
        return r.sample(ids, 2)

    vms = gen_vms(300, cpu=2, mem=8_000, disk=50,
                  bm_ids=bm_ids, candidates_fn=_two_cands, seed=123)
    run_solve(
        vms, bms,
        timeout_s=20,
        build_overhead_s=10,
        expect_statuses=("OPTIMAL", "FEASIBLE", "UNKNOWN"),
        expect_all_placed=False,
    )


def test_restricted_candidates_near_infeasible(gen_bms, gen_vms, run_solve):
    """
    100 VMs all list the same 2 candidate BMs. Those 2 BMs fit only 32 VMs
    each at default 2C → ≤ 64 placeable total → INFEASIBLE. Must return fast.
    """
    bms = gen_bms(10, ags=2)
    bm_ids = [b.id for b in bms]
    hot = bm_ids[:2]

    def _hot_pair(i: int, ids: list[str], r: random.Random) -> list[str]:
        return list(hot)

    vms = gen_vms(100, cpu=2, mem=8_000, disk=50,
                  bm_ids=bm_ids, candidates_fn=_hot_pair)
    run_solve(
        vms, bms,
        timeout_s=10,
        build_overhead_s=5,
        expect_statuses=("INFEASIBLE",),
        expect_all_placed=False,
    )


def test_bottleneck_single_dimension(gen_bms, gen_vms, run_solve):
    """
    Storage is the bottleneck: each BM has 500 GB but 100 VMs demand 100 GB
    each → 10 000 GB need vs 5 000 GB supply → must reduce utilization.
    Test with half the VM count to keep it feasible.
    """
    bms = gen_bms(10, cpu=64, mem=256_000, disk=500, ags=2)
    vms = gen_vms(40, cpu=1, mem=1_000, disk=100, bm_ids=[b.id for b in bms])
    run_solve(
        vms, bms,
        timeout_s=15,
        build_overhead_s=10,
        expect_statuses=("OPTIMAL", "FEASIBLE"),
        expect_all_placed=True,
    )


def test_gpu_scarcity(gen_bms, gen_vms, run_solve):
    """
    GPU-scarce mix: 5 GPU BMs + 20 CPU BMs. 10 GPU VMs must land on GPU BMs;
    40 CPU VMs can go anywhere. Eligibility filtering via fits_in enforces
    the GPU dimension.
    """
    gpu_bms = [
        b for b in gen_bms(5, cpu=64, mem=256_000, disk=2000, gpu=8,
                           id_prefix="gbm", ags=2)
    ]
    cpu_bms = [
        b for b in gen_bms(20, cpu=64, mem=256_000, disk=2000, gpu=0,
                           id_prefix="cbm", ags=4)
    ]
    bms = gpu_bms + cpu_bms

    gpu_vms = gen_vms(10, cpu=4, mem=16_000, disk=100, gpu=1,
                       id_prefix="gvm")
    cpu_vms = gen_vms(40, cpu=4, mem=16_000, disk=100, gpu=0,
                       id_prefix="cvm")
    vms = gpu_vms + cpu_vms

    run_solve(
        vms, bms,
        timeout_s=15,
        build_overhead_s=10,
        expect_statuses=("OPTIMAL", "FEASIBLE"),
        expect_all_placed=True,
    )
