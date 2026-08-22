"""
Test suite — rollout simulation (app/rollout.py).
Run: pytest tests/test_rollout.py -v
"""

import json
from pathlib import Path

from app.models import (
    AntiAffinityRule,
    ExclusiveBaremetalRule,
    FailoverRule,
    GroupSelector,
    NodeRole,
    ResourceRequirement,
    Resources,
    RolloutRequest,
    RolloutStep,
    SolverConfig,
)
from app.rollout import solve_rollout

from .conftest import make_bm, make_vm


def make_step(name, vms=None, requirements=None, rules=None,
              max_per_bm_rules=None, exclusive_rules=None, failover_rules=None):
    return RolloutStep(
        name=name,
        vms=vms or [],
        requirements=requirements or [],
        anti_affinity_rules=rules or [],
        max_per_bm_rules=max_per_bm_rules or [],
        exclusive_bm_rules=exclusive_rules or [],
        failover_rules=failover_rules or [],
    )


def rollout(bms, steps, existing_vms=None, **config_overrides):
    """Build a RolloutRequest with test defaults and run it.

    Backfills candidate_baremetals with all BM ids on step VMs that carry
    an empty list (same convention as conftest.solve).
    """
    cfg = dict(max_solve_time_seconds=10, auto_generate_anti_affinity=False)
    cfg.update(config_overrides)
    all_bm_ids = [bm.id for bm in bms]
    filled_steps = []
    for s in steps:
        filled_vms = [
            vm.model_copy(update={"candidate_baremetals": all_bm_ids})
            if not vm.candidate_baremetals else vm
            for vm in s.vms
        ]
        filled_reqs = [
            r.model_copy(update={"candidate_baremetals": all_bm_ids})
            if not r.candidate_baremetals else r
            for r in s.requirements
        ]
        filled_steps.append(s.model_copy(update={
            "vms": filled_vms, "requirements": filled_reqs,
        }))
    request = RolloutRequest(
        baremetals=bms,
        steps=filled_steps,
        existing_vms=existing_vms or [],
        config=SolverConfig(**cfg),
    )
    return solve_rollout(request)


def used_of(result, bm_id, field="cpu_cores"):
    bm = {b.id: b for b in result.final_baremetals}[bm_id]
    return getattr(bm.used_capacity, field)


# ===========================================================================
# 1. Fold-forward ledger: used_capacity advances exactly, no drift
# ===========================================================================

class TestRolloutFoldForward:

    def test_three_steps_ledger_exact(self):
        """Each default VM is 4 cpu / 16000 mem / 100 disk; after 3 steps of
        2 VMs each the per-field ledger must equal starting used + Σ folded."""
        bms = [make_bm("bm-1", cpu=64, used_cpu=8, used_mem=32_000, used_disk=200),
               make_bm("bm-2", cpu=64)]
        steps = [
            make_step(f"s{k}", vms=[make_vm(f"s{k}-vm-{i}") for i in range(2)])
            for k in range(3)
        ]
        r = rollout(bms, steps)
        assert r.success, r.solver_status
        assert r.failed_step is None
        assert all(rep.success for rep in r.reports)
        total_used = {f: 0 for f in ("cpu_cores", "memory_mib", "storage_gb")}
        for bm in r.final_baremetals:
            for f in total_used:
                total_used[f] += getattr(bm.used_capacity, f)
        # starting used (8/32000/200) + 6 VMs × (4/16000/100)
        assert total_used == {
            "cpu_cores": 8 + 24,
            "memory_mib": 32_000 + 96_000,
            "storage_gb": 200 + 600,
        }

    def test_capacity_consumed_by_earlier_step_blocks_later(self):
        """One BM, 16 cpu: step 1 places 3 VMs (12 cpu), step 2 needs 2 more
        (8 cpu) → dead end at step 2, not phantom success."""
        bms = [make_bm("bm-1", cpu=16)]
        steps = [
            make_step("s1", vms=[make_vm(f"a-{i}") for i in range(3)]),
            make_step("s2", vms=[make_vm(f"b-{i}") for i in range(2)]),
        ]
        r = rollout(bms, steps)
        assert not r.success
        assert r.failed_step == "s2"
        assert r.reports[0].success
        assert not r.reports[1].success

    def test_no_double_count_across_steps(self):
        """8-cpu BM, step 1 places one 4-cpu VM, step 2 one more: exactly
        fills. Double-counting the fold would make step 2 infeasible."""
        bms = [make_bm("bm-1", cpu=8)]
        steps = [
            make_step("s1", vms=[make_vm("a-0")]),
            make_step("s2", vms=[make_vm("b-0")]),
        ]
        r = rollout(bms, steps)
        assert r.success, r.reports[-1].solver_status
        assert used_of(r, "bm-1") == 8

    def test_new_assignments_exclude_carried(self):
        bms = [make_bm("bm-1"), make_bm("bm-2")]
        steps = [
            make_step("s1", vms=[make_vm("a-0")]),
            make_step("s2", vms=[make_vm("b-0")]),
        ]
        r = rollout(bms, steps)
        assert [a.vm_id for a in r.reports[0].new_assignments] == ["a-0"]
        assert [a.vm_id for a in r.reports[1].new_assignments] == ["b-0"]


# ===========================================================================
# 2. Brownfield start: existing_vms
# ===========================================================================

class TestRolloutBrownfield:

    def test_existing_vms_counted_not_refolded(self):
        """Existing VM's demand is already in starting used; the ledger must
        not grow by it again, and spread must see it."""
        bms = [
            make_bm("bm-a", ag="ag-1", used_cpu=4, used_mem=16_000, used_disk=100),
            make_bm("bm-b", ag="ag-2"),
        ]
        existing = [make_vm("old-0", role=NodeRole.MASTER, ip_type="routable",
                            pinned_to="bm-a")]
        rule = AntiAffinityRule(group_id="g", vm_ids=["old-0", "new-0"],
                                spread_on=["ag"], cap_per_bucket={"ag": 1})
        steps = [make_step("s1",
                           vms=[make_vm("new-0", role=NodeRole.MASTER,
                                        ip_type="routable")],
                           rules=[rule])]
        r = rollout(bms, steps, existing_vms=existing)
        assert r.success, r.reports[0].solver_status
        assert r.reports[0].new_assignments[0].baremetal_id == "bm-b"
        assert used_of(r, "bm-a") == 4  # unchanged: never re-folded

    def test_existing_vm_without_pin_rejected(self):
        r = rollout([make_bm("bm-1")],
                    [make_step("s1", vms=[make_vm("n-0")])],
                    existing_vms=[make_vm("old-0")])
        assert not r.success
        assert r.solver_status.startswith("INPUT_ERROR")
        assert "no pinned_to" in r.solver_status

    def test_existing_vm_unknown_host_rejected(self):
        r = rollout([make_bm("bm-1")],
                    [make_step("s1", vms=[make_vm("n-0")])],
                    existing_vms=[make_vm("old-0", pinned_to="bm-x")])
        assert not r.success
        assert r.solver_status.startswith("INPUT_ERROR")
        assert "unknown BM" in r.solver_status

    def test_existing_demand_missing_from_used_surfaces_at_step_1(self):
        """used does not contain the existing VM's demand → the solver's
        normalization guard reports it in step 1, not silently."""
        bms = [make_bm("bm-1")]  # used all zero
        existing = [make_vm("old-0", pinned_to="bm-1")]
        r = rollout(bms, [make_step("s1", vms=[make_vm("n-0")])],
                    existing_vms=existing)
        assert not r.success
        assert r.failed_step == "s1"
        assert r.reports[0].solver_status.startswith("INPUT_ERROR")
        assert "used_capacity" in r.reports[0].solver_status


# ===========================================================================
# 3. Dead end: latch + blocked stubs
# ===========================================================================

class TestRolloutDeadEnd:

    def test_failure_latches_and_blocks(self):
        bms = [make_bm("bm-1", cpu=8)]
        steps = [
            make_step("s1", vms=[make_vm("a-0")]),          # 4 cpu, fits
            make_step("s2", vms=[make_vm(f"b-{i}") for i in range(2)]),  # 8 cpu, dead end
            make_step("s3", vms=[make_vm("c-0")]),
        ]
        r = rollout(bms, steps)
        assert not r.success
        assert r.failed_step == "s2"
        assert r.reports[0].success
        assert not r.reports[1].success
        assert r.reports[2].solver_status == (
            "BLOCKED: not simulated — step 's2' failed"
        )
        assert r.reports[2].new_assignments == []
        # the failed step reports only ITS OWN VMs as unplaced — carried
        # pins from step 1 were placed and must not reappear here
        assert r.reports[1].unplaced_vms == ["b-0", "b-1"]
        # step 1's fold still reflected in the final snapshot
        assert used_of(r, "bm-1") == 4


# ===========================================================================
# 4. Rules union across steps
# ===========================================================================

class TestRolloutRulesUnion:

    def test_step1_exclusive_vm_ids_bars_step2_outsider(self):
        """The regression for the design's central hole: a vm_ids-form C6
        rule from step 1 must still bar step 2's outsiders."""
        bms = [make_bm("bm-1"), make_bm("bm-2")]
        steps = [
            make_step("s1", vms=[make_vm("f5-1")],
                      exclusive_rules=[ExclusiveBaremetalRule(
                          group_id="ex", vm_ids=["f5-1"])]),
            make_step("s2", vms=[make_vm("w-0")]),
        ]
        r = rollout(bms, steps)
        assert r.success, r.reports[-1].solver_status
        f5_host = r.reports[0].new_assignments[0].baremetal_id
        w_host = r.reports[1].new_assignments[0].baremetal_id
        assert w_host != f5_host

    def test_step1_exclusive_selector_bars_step2_outsider(self):
        bms = [make_bm("bm-1"), make_bm("bm-2")]
        sel = GroupSelector(cluster_id="shared", node_role="f5")
        steps = [
            make_step("s1", vms=[make_vm("f5-1", role="f5", cluster="shared")],
                      exclusive_rules=[ExclusiveBaremetalRule(
                          group_id="ex", selector=sel)]),
            make_step("s2", vms=[make_vm("w-0")]),
        ]
        r = rollout(bms, steps)
        assert r.success, r.reports[-1].solver_status
        f5_host = r.reports[0].new_assignments[0].baremetal_id
        w_host = r.reports[1].new_assignments[0].baremetal_id
        assert w_host != f5_host

    def test_vm_ids_rule_referencing_unknown_id_rejected(self):
        """The solver silently drops unknown vm_ids — rollout must not."""
        bms = [make_bm("bm-1")]
        steps = [
            make_step("s1", vms=[make_vm("a-0")],
                      exclusive_rules=[ExclusiveBaremetalRule(
                          group_id="ex", vm_ids=["a-0", "future-vm"])]),
        ]
        r = rollout(bms, steps)
        assert not r.success
        assert r.solver_status.startswith("INPUT_ERROR")
        assert "future-vm" in r.solver_status

    def test_vm_ids_rule_may_reference_earlier_step(self):
        """Two appliances built in different steps, one exclusive group."""
        bms = [make_bm(f"bm-{i}") for i in range(3)]
        steps = [
            make_step("s1", vms=[make_vm("f5-1")]),
            make_step("s2", vms=[make_vm("f5-2")],
                      exclusive_rules=[ExclusiveBaremetalRule(
                          group_id="ex", vm_ids=["f5-1", "f5-2"])]),
            make_step("s3", vms=[make_vm("w-0")]),
        ]
        r = rollout(bms, steps)
        assert r.success, r.reports[-1].solver_status
        f5_1 = r.reports[0].new_assignments[0].baremetal_id
        f5_2 = r.reports[1].new_assignments[0].baremetal_id
        w = r.reports[2].new_assignments[0].baremetal_id
        assert len({f5_1, f5_2, w}) == 3

    def test_failover_rule_spans_steps(self):
        """Step 1 builds masters, step 2 adds the learner under a step-1
        failover rule: N-1 must steer it out of the masters' room."""
        bms = [
            make_bm("bm-r0-0", room="room-0"), make_bm("bm-r0-1", room="room-0"),
            make_bm("bm-r1-0", room="room-1"),
        ]
        f = FailoverRule(
            rule_id="fo",
            primary=GroupSelector(cluster_id="A", node_role=NodeRole.MASTER),
            backup=GroupSelector(cluster_id="A", node_role=NodeRole.LEARNER),
            fault_domain="room",
        )
        steps = [
            make_step("s1",
                      vms=[make_vm("m-0", role=NodeRole.MASTER, cluster="A",
                                   candidates=["bm-r0-0"]),
                           make_vm("m-1", role=NodeRole.MASTER, cluster="A",
                                   candidates=["bm-r0-1"]),
                           make_vm("l-0", role=NodeRole.LEARNER, cluster="A"),
                           make_vm("l-1", role=NodeRole.LEARNER, cluster="A")],
                      failover_rules=[f]),
            make_step("s2", vms=[make_vm("l-2", role=NodeRole.LEARNER,
                                         cluster="A")]),
        ]
        r = rollout(bms, steps)
        assert r.success, r.reports[-1].solver_status


# ===========================================================================
# 5. Naming: synthetic namespacing + explicit-id collisions
# ===========================================================================

class TestRolloutRenaming:

    def _split_req(self):
        spec = Resources(cpu_cores=8, memory_mib=32_000, storage_gb=200)
        return ResourceRequirement(
            total_resources=Resources(cpu_cores=16, memory_mib=64_000,
                                      storage_gb=400),
            node_role="worker", cluster_id="c1", ip_type="routable",
            vm_specs=[spec],
        )

    def test_synthetic_ids_namespaced_per_step(self):
        """Two splitting steps would both emit split-r0-s0-0 — the rollout
        must namespace them (duplicate ids are INPUT_ERROR since commit 2)."""
        bms = [make_bm(f"bm-{i}", cpu=64, mem=256_000, disk=2000)
               for i in range(3)]
        steps = [
            make_step("s1", requirements=[self._split_req()]),
            make_step("s2", requirements=[self._split_req()]),
        ]
        r = rollout(bms, steps)
        assert r.success, r.reports[-1].solver_status
        ids_1 = {a.vm_id for a in r.reports[0].new_assignments}
        ids_2 = {a.vm_id for a in r.reports[1].new_assignments}
        assert all(i.startswith("s1/") for i in ids_1)
        assert all(i.startswith("s2/") for i in ids_2)
        assert not ids_1 & ids_2

    def test_duplicate_explicit_id_across_steps_rejected(self):
        bms = [make_bm("bm-1")]
        steps = [
            make_step("s1", vms=[make_vm("vm-x")]),
            make_step("s2", vms=[make_vm("vm-x")]),
        ]
        r = rollout(bms, steps)
        assert not r.success
        assert r.solver_status.startswith("INPUT_ERROR")
        assert "collides" in r.solver_status

    def test_duplicate_step_name_rejected(self):
        bms = [make_bm("bm-1")]
        steps = [
            make_step("s1", vms=[make_vm("a-0")]),
            make_step("s1", vms=[make_vm("b-0")]),
        ]
        r = rollout(bms, steps)
        assert not r.success
        assert "duplicate step name" in r.solver_status

    def test_empty_step_rejected(self):
        r = rollout([make_bm("bm-1")], [make_step("s1")])
        assert not r.success
        assert "neither vms nor requirements" in r.solver_status


# ===========================================================================
# 6. Example + HTTP endpoint
# ===========================================================================

class TestRolloutExample:
    """examples/rollout/*.json are invisible to test_examples.py (top-level
    glob only) — cover them here explicitly."""

    def test_basic_two_step_example(self):
        path = Path(__file__).parent.parent / "examples" / "rollout" / "basic_two_step.json"
        request = RolloutRequest.model_validate_json(path.read_text())
        r = solve_rollout(request)
        assert r.success, [rep.solver_status for rep in r.reports]
        assert all(rep.success for rep in r.reports)
        assert r.failed_step is None


class TestRolloutEndpoint:

    def test_post_rollout(self, client):
        bms = [make_bm("bm-1"), make_bm("bm-2")]
        body = RolloutRequest(
            baremetals=bms,
            steps=[
                RolloutStep(name="s1", vms=[
                    make_vm("a-0", candidates=["bm-1", "bm-2"])]),
                RolloutStep(name="s2", vms=[
                    make_vm("b-0", candidates=["bm-1", "bm-2"])]),
            ],
            config=SolverConfig(max_solve_time_seconds=10,
                                auto_generate_anti_affinity=False),
        ).model_dump(mode="json")
        resp = client.post("/v1/placement/rollout", json=body)
        assert resp.status_code == 200
        out = resp.json()
        assert out["success"] is True
        assert len(out["reports"]) == 2
        import re
        assert re.fullmatch(r"[0-9a-f]{12}", out["config_fingerprint"])
