"""
Scale ladder — how does the solver behave as VM/BM counts grow?

Observations from initial calibration (no objective, single worker):
  50 × 50   → OPTIMAL in ~0.2s
  100 × 100 → OPTIMAL in ~0.9s
  200 × 200 → OPTIMAL in ~4.8s
  500 × 500 → UNKNOWN after 35s (model build dominates)
  1000 × 1000 → UNKNOWN after 90s

Ladder values below are calibrated to keep mid-size cases inside a
couple of minutes while still exercising model-build scaling at the
upper end. Huge cases are expected to return FEASIBLE or UNKNOWN —
we verify graceful return, not optimality.
"""

from __future__ import annotations

import pytest

from app.models import Resources


@pytest.mark.parametrize(
    "num_vms, num_bms, timeout_s, build_overhead_s",
    [
        pytest.param(50, 50, 5, 5, id="50x50"),
        pytest.param(200, 100, 10, 10, id="200x100"),
        pytest.param(500, 200, 30, 45, marks=pytest.mark.slow, id="500x200"),
        pytest.param(1000, 500, 60, 120, marks=pytest.mark.huge, id="1000x500"),
    ],
)
def test_scale_solve(num_vms, num_bms, timeout_s, build_overhead_s,
                     gen_bms, gen_vms, run_solve):
    """
    Baseline scaling: default VM (4C/16GiB/100GB) onto default BMs
    (64C/256GiB/2TB). Problem is loose (each BM fits ~16 VMs) so the stress
    comes from model-build + propagation, not feasibility. For small sizes
    we require all VMs placed; for slow/huge we only require a graceful
    non-MODEL_INVALID return.
    """
    bms = gen_bms(num_bms, ags=max(2, num_bms // 25))
    vms = gen_vms(num_vms, bm_ids=[b.id for b in bms])

    # Small sizes should hit OPTIMAL or FEASIBLE with all placed.
    # Larger sizes may time out in CP-SAT; accept UNKNOWN too.
    if num_vms <= 200:
        expect = ("OPTIMAL", "FEASIBLE")
        expect_all_placed = True
    else:
        expect = ("OPTIMAL", "FEASIBLE", "UNKNOWN")
        expect_all_placed = False

    run_solve(
        vms, bms,
        timeout_s=timeout_s,
        build_overhead_s=build_overhead_s,
        expect_statuses=expect,
        expect_all_placed=expect_all_placed,
    )


@pytest.mark.parametrize(
    "num_vms, num_bms, timeout_s, build_overhead_s",
    [
        pytest.param(50, 50, 5, 5, id="50x50"),
        pytest.param(200, 100, 15, 15, id="200x100"),
        pytest.param(500, 200, 45, 60, marks=pytest.mark.slow, id="500x200"),
        pytest.param(1000, 500, 60, 150, marks=pytest.mark.huge, id="1000x500"),
    ],
)
def test_scale_split(num_vms, num_bms, timeout_s, build_overhead_s,
                     gen_bms, run_split_solve, requirement_factory):
    """
    Same aggregate demand, but expressed as one ResourceRequirement the
    splitter decomposes. Two specs (4C/16GiB and 8C/32GiB) give the solver
    spec-mix freedom.
    """
    bms = gen_bms(num_bms, ags=max(2, num_bms // 25))
    req = requirement_factory(
        cpu=num_vms * 4,
        mem=num_vms * 16_000,
        disk=num_vms * 100,
        vm_specs=[
            Resources(cpu_cores=4, memory_mib=16_000, storage_gb=100),
            Resources(cpu_cores=8, memory_mib=32_000, storage_gb=200),
        ],
    )

    if num_vms <= 200:
        expect = ("OPTIMAL", "FEASIBLE")
        expect_all_placed = True
    else:
        expect = ("OPTIMAL", "FEASIBLE", "UNKNOWN")
        expect_all_placed = False

    run_split_solve(
        [req], bms,
        timeout_s=timeout_s,
        build_overhead_s=build_overhead_s,
        expect_statuses=expect,
        expect_all_placed=expect_all_placed,
    )


def test_scale_200vm_tall_bms(gen_bms, gen_vms, run_solve):
    """
    Width-vs-density: 200 VMs onto 20 very large BMs. Far fewer decision
    variables than 200x100 (~4000 eligible pairs) but each capacity
    constraint has larger coefficients and more terms per BM — exercises
    the arithmetic side of the model rather than the combinatorial side.
    """
    bms = gen_bms(20, cpu=256, mem=1_024_000, disk=8000, ags=4)
    vms = gen_vms(200, bm_ids=[b.id for b in bms])
    run_solve(vms, bms, timeout_s=10, build_overhead_s=10)
