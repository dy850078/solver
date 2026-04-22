"""
Timeout behavior — deliberately hard problems paired with short timeouts.

The contract verified here: when CP-SAT runs out of search time, the Python
layer must still return a populated PlacementResult with `solver_status` in
{FEASIBLE, UNKNOWN, INFEASIBLE} and `solve_time_seconds` close to the
configured timeout. It must never raise and never emit MODEL_INVALID.
"""

from __future__ import annotations

import random

import pytest

from app.models import AntiAffinityRule, NodeRole, Resources

_TIMEOUT_STATUSES = ("OPTIMAL", "FEASIBLE", "UNKNOWN", "INFEASIBLE")


@pytest.mark.slow
@pytest.mark.parametrize("timeout_s", [1.0, 5.0])
def test_hard_problem_short_timeout(timeout_s, gen_bms, gen_vms, run_solve):
    """
    1000 VMs × 300 BMs with 50 overlapping anti-affinity rules. Unlikely to
    reach OPTIMAL in 1–5s — we only want a graceful status code.
    """
    num_vms, num_bms = 1000, 300
    bms = gen_bms(num_bms, cpu=64, mem=256_000, disk=2000, ags=10)
    vms = gen_vms(num_vms, cpu=6, mem=16_000, disk=100)

    rng = random.Random(4242)
    rules = [
        AntiAffinityRule(
            group_id=f"hard-g{g}",
            vm_ids=rng.sample([v.id for v in vms], 40),
            max_per_ag=5,
        )
        for g in range(50)
    ]

    run_solve(
        vms, bms,
        rules=rules,
        timeout_s=timeout_s,
        build_overhead_s=10,
        expect_statuses=_TIMEOUT_STATUSES,
        expect_all_placed=False,
    )


@pytest.mark.slow
def test_split_slot_explosion_short_timeout(
    gen_bms, run_split_solve, requirement_factory,
):
    """
    Five specs with a large total budget → ~500 synthetic slot vars. With
    a 1s CP-SAT timeout, the solver probably returns FEASIBLE or UNKNOWN.
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
        timeout_s=1.0,
        build_overhead_s=15,
        expect_statuses=_TIMEOUT_STATUSES,
        expect_all_placed=False,
    )


def test_tight_ag_short_timeout(gen_bms, gen_vms, run_solve):
    """
    500 VMs across only 3 AGs with auto-gen anti-affinity:
    max_per_ag = ceil(500/3) = 167. Tight spreading + short timeout.
    """
    bms = gen_bms(60, cpu=64, mem=256_000, disk=2000, ags=3)
    vms = gen_vms(500, cpu=2, mem=8_000, disk=50,
                  role=NodeRole.WORKER, ip_type="routable")
    run_solve(
        vms, bms,
        timeout_s=2.0,
        build_overhead_s=15,
        auto_generate_anti_affinity=True,
        expect_statuses=_TIMEOUT_STATUSES,
        expect_all_placed=False,
    )


def test_unreachable_assignments_short_timeout(gen_bms, gen_vms, run_solve):
    """
    400 VMs each with only 2 candidate BMs out of 200. Dense eligibility
    graph with local bottlenecks. Tests propagation under 3s budget.
    """
    bms = gen_bms(200, ags=10)
    bm_ids = [b.id for b in bms]

    def _pick_two(i: int, ids: list[str], r: random.Random) -> list[str]:
        return r.sample(ids, 2)

    vms = gen_vms(
        400, bm_ids=bm_ids, candidates_fn=_pick_two, seed=7,
    )
    run_solve(
        vms, bms,
        timeout_s=3.0,
        build_overhead_s=10,
        expect_statuses=_TIMEOUT_STATUSES,
        expect_all_placed=False,
    )
