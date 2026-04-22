"""
Anti-affinity / AG stress.

Exercises the auto-generated anti-affinity rule flow, explicit rule
stacking, and dynamic max_per_ag behavior for synthetic VMs produced by
the splitter.
"""

from __future__ import annotations

import random

import pytest

from app.models import AntiAffinityRule, NodeRole, Resources


@pytest.mark.slow
def test_many_ags_one_bm_each(gen_bms, gen_vms, run_solve):
    """
    500 VMs / 500 BMs / one AG per BM. With auto-gen anti-affinity the
    solver must spread 500 VMs across 500 AGs → max_per_ag=1 effectively.
    """
    bms = gen_bms(500)  # ags=None → one AG per BM
    vms = gen_vms(
        500, bm_ids=[b.id for b in bms],
        role=NodeRole.WORKER, ip_type="routable",
    )
    run_solve(
        vms, bms,
        timeout_s=30,
        build_overhead_s=60,
        auto_generate_anti_affinity=True,
        expect_statuses=("OPTIMAL", "FEASIBLE", "UNKNOWN"),
        expect_all_placed=False,
    )


@pytest.mark.parametrize(
    "max_per_ag, expect_status, expect_placed",
    [
        pytest.param(101, ("OPTIMAL", "FEASIBLE"), True, id="loose"),
        pytest.param(100, ("OPTIMAL", "FEASIBLE"), True, id="exact"),
        pytest.param(99, ("INFEASIBLE",), False, id="infeasible"),
    ],
)
def test_tight_max_per_ag(max_per_ag, expect_status, expect_placed,
                          gen_bms, gen_vms, run_solve):
    """300 VMs × 3 AGs. Boundary at max_per_ag = 100."""
    bms = gen_bms(60, ags=3)
    vms = gen_vms(300, cpu=2, mem=8_000, disk=50)
    rule = AntiAffinityRule(
        group_id="tight-workers",
        vm_ids=[v.id for v in vms],
        max_per_ag=max_per_ag,
    )
    run_solve(
        vms, bms,
        rules=[rule],
        timeout_s=20,
        build_overhead_s=10,
        expect_statuses=expect_status,
        expect_all_placed=expect_placed,
    )


def test_auto_gen_huge_group(gen_bms, gen_vms, run_solve):
    """600 VMs same (ip_type, role) / 6 AGs → auto-gen max_per_ag=100."""
    bms = gen_bms(120, ags=6)
    vms = gen_vms(
        600, cpu=1, mem=4_000, disk=25,
        role=NodeRole.WORKER, ip_type="routable",
    )
    run_solve(
        vms, bms,
        timeout_s=30,
        build_overhead_s=15,
        auto_generate_anti_affinity=True,
        expect_statuses=("OPTIMAL", "FEASIBLE", "UNKNOWN"),
        expect_all_placed=False,
    )


@pytest.mark.slow
def test_many_explicit_rules(gen_bms, gen_vms, run_solve):
    """
    200 VMs / 50 AGs / 40 overlapping rules each containing 10 VMs.
    Stresses rule-count scaling of anti-affinity constraints.
    """
    bms = gen_bms(100, ags=50)
    vms = gen_vms(200)
    rng = random.Random(13)
    vm_ids = [v.id for v in vms]
    rules = [
        AntiAffinityRule(
            group_id=f"expl-g{g}",
            vm_ids=rng.sample(vm_ids, 10),
            max_per_ag=1,
        )
        for g in range(40)
    ]
    run_solve(
        vms, bms,
        rules=rules,
        timeout_s=30,
        build_overhead_s=15,
        expect_statuses=("OPTIMAL", "FEASIBLE", "UNKNOWN"),
        expect_all_placed=False,
    )


def test_mixed_iptype_role_groups(gen_bms, gen_vms, run_solve):
    """400 VMs distributed across 4 (ip_type, role) combos / 10 AGs."""
    bms = gen_bms(80, ags=10)
    roles = [NodeRole.WORKER, NodeRole.MASTER, NodeRole.INFRA, NodeRole.L4LB]
    ip_types = ["routable", "non-routable"]
    vms = gen_vms(
        400, cpu=2, mem=8_000, disk=50,
        role_cycle=roles, ip_type_cycle=ip_types,
    )
    run_solve(
        vms, bms,
        timeout_s=20,
        build_overhead_s=10,
        auto_generate_anti_affinity=True,
        expect_statuses=("OPTIMAL", "FEASIBLE", "UNKNOWN"),
        expect_all_placed=False,
    )


def test_infeasible_ag_count(gen_bms, gen_vms, run_solve):
    """
    5 masters / 2 AGs / max_per_ag=1 → only 2 placeable → INFEASIBLE.
    Check that we return quickly (propagation catches it).
    """
    bms = gen_bms(4, cpu=128, mem=512_000, disk=4000, ags=2)
    vms = gen_vms(
        5, cpu=8, mem=32_000, disk=200, role=NodeRole.MASTER,
    )
    rule = AntiAffinityRule(
        group_id="masters",
        vm_ids=[v.id for v in vms],
        max_per_ag=1,
    )
    run_solve(
        vms, bms,
        rules=[rule],
        timeout_s=10,
        build_overhead_s=5,
        expect_statuses=("INFEASIBLE",),
        expect_all_placed=False,
    )


def test_dynamic_max_per_ag_split(gen_bms, run_split_solve, requirement_factory):
    """
    Split produces ~60 synthetic VMs across 4 AGs — exercises the dynamic
    max_per_ag arithmetic in VMPlacementSolver._add_anti_affinity_constraints.
    """
    bms = gen_bms(40, cpu=64, mem=256_000, disk=2000, ags=4)
    req = requirement_factory(
        cpu=240, mem=960_000, disk=6000,
        role=NodeRole.WORKER, ip_type="routable",
        vm_specs=[
            Resources(cpu_cores=4, memory_mib=16_000, storage_gb=100),
        ],
    )
    run_split_solve(
        [req], bms,
        timeout_s=30,
        build_overhead_s=10,
        auto_generate_anti_affinity=True,
        expect_statuses=("OPTIMAL", "FEASIBLE"),
        expect_all_placed=True,
    )


def test_nested_overlap_rules(gen_bms, gen_vms, run_solve):
    """
    10 explicit rules, each a random 20-VM subset of 100 VMs → heavy
    constraint overlap. 5 AGs.
    """
    bms = gen_bms(50, ags=5)
    vms = gen_vms(100, cpu=2, mem=8_000, disk=50)
    rng = random.Random(21)
    vm_ids = [v.id for v in vms]
    rules = [
        AntiAffinityRule(
            group_id=f"nested-{g}",
            vm_ids=rng.sample(vm_ids, 20),
            max_per_ag=5,
        )
        for g in range(10)
    ]
    run_solve(
        vms, bms,
        rules=rules,
        timeout_s=15,
        build_overhead_s=10,
        expect_statuses=("OPTIMAL", "FEASIBLE", "UNKNOWN"),
        expect_all_placed=False,
    )
