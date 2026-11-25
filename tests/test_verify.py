"""Tests for the independent verifier (app/verify.py).

Three layers:
1. Tampered results — a valid layout is corrupted by hand and the verifier
   must name the constraint. Guards against a verifier that returns [] no
   matter what (which would make the suite-wide hook in conftest worthless).
2. Sabotaged solver — one constraint builder is switched off, the solver
   then happily returns a violating "OPTIMAL" layout, and the suite-wide
   hook must turn that into a failure. This is the scenario the verifier
   exists for: a model-building bug the solver cannot see in itself.
3. Sweep — mock scenarios across seeds, so layouts nobody hand-wrote also
   pass through the hook.
"""

import json
import pathlib

import pytest

from app.mockgen import GenerateRequest, generate_mock_request
from app.models import (
    AntiAffinityRule, ExclusiveBaremetalRule, FailoverRule, GroupSelector,
    MaxPerBaremetalRule, MinDistinctBaremetalRule, PlacementAssignment,
    PlacementRequest, SolverConfig,
)
from app.solver import VMPlacementSolver
from app.verify import main as verify_main, verify_placement

from .conftest import make_bm, make_vm, solve

EXAMPLES = pathlib.Path(__file__).resolve().parent.parent / "examples"


def _three_ag_bms():
    return [make_bm(f"bm-{i}", ag=f"ag-{i}") for i in (1, 2, 3)]


def _request(vms, bms, **rules):
    ids = [b.id for b in bms]
    vms = [v.model_copy(update={"candidate_baremetals": ids})
           if not v.candidate_baremetals else v for v in vms]
    return PlacementRequest(
        vms=vms, baremetals=bms,
        config=SolverConfig(max_solve_time_seconds=10,
                            auto_generate_anti_affinity=False),
        **rules)


def _constraints(violations):
    return {v.constraint for v in violations}


# ---------------------------------------------------------------------------
# 1. Tampered results
# ---------------------------------------------------------------------------

class TestTamperedResults:
    def _solved(self, **rules):
        vms = [make_vm(f"vm-{i}") for i in (1, 2, 3)]
        req = _request(vms, _three_ag_bms(), **rules)
        res = VMPlacementSolver(req).solve()
        assert res.success and verify_placement(req, res) == []
        return req, res

    @staticmethod
    def _move(res, vm_id, bm_id, ag=None):
        for a in res.assignments:
            if a.vm_id == vm_id:
                a.baremetal_id = bm_id
                a.ag = ag if ag is not None else f"ag-{bm_id[-1]}"
        res.bm_used_count = len({a.baremetal_id for a in res.assignments})

    def _all_on(self, res, bm_id):
        for a in res.assignments:
            self._move(res, a.vm_id, bm_id)

    def test_dropped_vm_is_c1(self):
        req, res = self._solved()
        res.assignments.pop()
        res.bm_used_count = len({a.baremetal_id for a in res.assignments})
        assert _constraints(verify_placement(req, res)) == {"C1"}

    def test_duplicated_vm_is_c1(self):
        req, res = self._solved()
        res.assignments.append(res.assignments[0].model_copy())
        assert "C1" in _constraints(verify_placement(req, res))

    def test_non_candidate_bm_is_c1(self):
        vms = [make_vm("vm-1", candidates=["bm-1"])]
        req = _request(vms, _three_ag_bms())
        res = VMPlacementSolver(req).solve()
        self._move(res, "vm-1", "bm-2")
        assert _constraints(verify_placement(req, res)) == {"C1"}

    def test_unknown_bm_is_c1(self):
        req, res = self._solved()
        self._move(res, "vm-1", "bm-9")
        assert "C1" in _constraints(verify_placement(req, res))

    def test_overfilled_bm_is_c2(self):
        vms = [make_vm(f"vm-{i}", cpu=40) for i in (1, 2, 3)]
        req = _request(vms, _three_ag_bms())
        res = VMPlacementSolver(req).solve()
        self._all_on(res, "bm-1")
        violations = verify_placement(req, res)
        assert _constraints(violations) == {"C2"}
        assert "cpu_cores" in violations[0].message

    def test_gpu_models_are_not_fungible_c2(self):
        bms = [make_bm("bm-1", gpu={"h200": 2}), make_bm("bm-2", gpu={"a100": 2})]
        vms = [make_vm("vm-1", gpu={"h200": 1})]
        req = _request(vms, bms)
        res = VMPlacementSolver(req).solve()
        assert res.to_assignment_map() == {"vm-1": "bm-1"}
        self._move(res, "vm-1", "bm-2", ag="ag-1")
        violations = verify_placement(req, res)
        assert _constraints(violations) == {"C2"}
        assert "gpu:h200" in violations[0].message

    def test_clustered_group_is_c3(self):
        req, res = self._solved(anti_affinity_rules=[AntiAffinityRule(
            group_id="g", vm_ids=["vm-1", "vm-2", "vm-3"], spread_on=["ag"])])
        self._all_on(res, "bm-1")
        assert _constraints(verify_placement(req, res)) == {"C3"}

    def test_auto_group_is_c3(self):
        """Auto-generated groups are re-derived by the verifier, not read
        from the solver: same cluster/ip/role ⇒ one group, spread over AGs."""
        vms = [make_vm(f"vm-{i}") for i in (1, 2, 3)]
        req = _request(vms, _three_ag_bms())
        req.config.auto_generate_anti_affinity = True
        res = VMPlacementSolver(req).solve()
        assert len(set(res.to_assignment_map().values())) == 3
        self._all_on(res, "bm-1")
        violations = verify_placement(req, res)
        assert _constraints(violations) == {"C3"}
        assert violations[0].subject == "auto/cluster-1/routable/worker"

    def test_stacked_group_is_c4(self):
        req, res = self._solved(max_per_bm_rules=[MaxPerBaremetalRule(
            group_id="g", selector=GroupSelector(node_role="worker"), max_per_bm=1)])
        self._move(res, "vm-1", res.to_assignment_map()["vm-2"])
        assert _constraints(verify_placement(req, res)) == {"C4"}

    def test_failover_bucket_overload_is_c5(self):
        vms = ([make_vm(f"p-{i}", role="control-plane") for i in (1, 2)]
               + [make_vm(f"b-{i}", role="control-plane-learner") for i in (1, 2)])
        req = _request(vms, _three_ag_bms(), failover_rules=[FailoverRule(
            rule_id="f", fault_domain="ag",
            primary=GroupSelector(node_role="control-plane"),
            backup=GroupSelector(node_role="control-plane-learner"))])
        res = VMPlacementSolver(req).solve()
        assert res.success
        self._all_on(res, "bm-1")
        assert _constraints(verify_placement(req, res)) == {"C5"}

    def test_shared_exclusive_bm_is_c6(self):
        vms = [make_vm("f5", role="f5"), make_vm("vm-1"), make_vm("vm-2")]
        req = _request(vms, _three_ag_bms(), exclusive_bm_rules=[
            ExclusiveBaremetalRule(group_id="f5", vm_ids=["f5"])])
        res = VMPlacementSolver(req).solve()
        self._move(res, "vm-1", res.to_assignment_map()["f5"])
        assert _constraints(verify_placement(req, res)) == {"C6"}

    def test_too_small_footprint_is_c7(self):
        req, res = self._solved(min_distinct_bm_rules=[MinDistinctBaremetalRule(
            group_id="g", vm_ids=["vm-1", "vm-2", "vm-3"], min_distinct_bms=3)])
        self._all_on(res, "bm-1")
        assert _constraints(verify_placement(req, res)) == {"C7"}

    def test_moved_pin_is_c1(self):
        bms = _three_ag_bms()
        bms[0] = make_bm("bm-1", ag="ag-1", used_cpu=4, used_mem=16_000, used_disk=100)
        vms = [make_vm("old", pinned_to="bm-1"), make_vm("new")]
        req = _request(vms, bms)
        res = VMPlacementSolver(req).solve()
        assert verify_placement(req, res) == []
        self._move(res, "old", "bm-2")
        assert "C1" in _constraints(verify_placement(req, res))

    def test_grandfathered_layout_is_not_a_violation(self):
        """Two pinned members already share an AG under a cap of 1: frozen,
        not illegal. The verifier must apply max(cap, pinned count) too, or
        every add-node request on a skewed fleet would be flagged."""
        bms = [make_bm("bm-1", ag="ag-1", used_cpu=8, used_mem=32_000, used_disk=200),
               make_bm("bm-2", ag="ag-2")]
        vms = [make_vm("old-1", pinned_to="bm-1"), make_vm("old-2", pinned_to="bm-1"),
               make_vm("new")]
        req = _request(vms, bms, anti_affinity_rules=[AntiAffinityRule(
            group_id="g", vm_ids=["old-1", "old-2", "new"], spread_on=["ag"],
            cap_per_bucket={"ag": 1})])
        res = VMPlacementSolver(req).solve()
        assert res.success and res.to_assignment_map()["new"] == "bm-2"
        assert verify_placement(req, res) == []
        # …but the frozen bucket must not take a NEW member.
        self._move(res, "new", "bm-1")
        assert _constraints(verify_placement(req, res)) == {"C3"}

    def test_wrong_bm_used_count_is_reported(self):
        req, res = self._solved()
        res.bm_used_count += 1
        assert _constraints(verify_placement(req, res)) == {"OUT"}


# ---------------------------------------------------------------------------
# 2. Sabotaged solver — the suite-wide hook must catch it
# ---------------------------------------------------------------------------

def _c2():
    return dict(vms=[make_vm(f"vm-{i}", cpu=40) for i in (1, 2, 3)])


def _c3():
    return dict(rules=[AntiAffinityRule(
        group_id="g", vm_ids=["vm-1", "vm-2", "vm-3"], spread_on=["ag"])])


def _c4():
    return dict(max_per_bm_rules=[MaxPerBaremetalRule(
        group_id="g", vm_ids=["vm-1", "vm-2", "vm-3"], max_per_bm=1)])


def _c5():
    return dict(
        vms=([make_vm(f"p-{i}", role="control-plane") for i in (1, 2)]
             + [make_vm(f"b-{i}", role="control-plane-learner") for i in (1, 2)]),
        failover_rules=[FailoverRule(
            rule_id="f", fault_domain="ag",
            primary=GroupSelector(node_role="control-plane"),
            backup=GroupSelector(node_role="control-plane-learner"))])


def _c6():
    return dict(exclusive_rules=[ExclusiveBaremetalRule(group_id="x", vm_ids=["vm-1"])])


def _c7():
    return dict(min_distinct_rules=[MinDistinctBaremetalRule(
        group_id="g", vm_ids=["vm-1", "vm-2", "vm-3"], min_distinct_bms=3)])


@pytest.mark.parametrize("label, builder, scenario", [
    ("C2", "_add_capacity_constraints", _c2),
    ("C3", "_add_anti_affinity_constraints", _c3),
    ("C4", "_add_max_per_bm_constraints", _c4),
    ("C5", "_add_failover_constraints", _c5),
    ("C6", "_add_exclusive_constraints", _c6),
    ("C7", "_add_min_distinct_constraints", _c7),
])
def test_disabled_constraint_is_caught(monkeypatch, label, builder, scenario):
    """Consolidation pulls every VM onto one BM; only the constraint under
    test keeps them apart. Remove its builder and the solver still says
    success — the hook is the only thing standing between that and green."""
    kwargs = scenario()
    vms = kwargs.pop("vms", None) or [make_vm(f"vm-{i}") for i in (1, 2, 3)]
    config = dict(w_consolidation=10, w_headroom=0)

    assert solve(vms, _three_ag_bms(), **kwargs, **config).success

    monkeypatch.setattr(VMPlacementSolver, builder, lambda self: None)
    with pytest.raises(AssertionError, match=rf"\[{label}\]"):
        solve(vms, _three_ag_bms(), **kwargs, **config)


@pytest.mark.no_verify
def test_no_verify_marker_switches_the_hook_off(monkeypatch):
    monkeypatch.setattr(VMPlacementSolver, "_add_max_per_bm_constraints",
                        lambda self: None)
    res = solve([make_vm(f"vm-{i}") for i in (1, 2, 3)], _three_ag_bms(),
                w_headroom=0, **_c4())
    assert res.success and len(set(res.to_assignment_map().values())) == 1


def test_every_solver_constraint_label_has_a_verifier_section():
    """A new `CONSTRAINT Cn` in the solver with no `# ---- Cn:` section in
    the verifier would pass every other test here — nothing fails when a
    check simply does not exist. This makes the omission loud."""
    import re
    app = EXAMPLES.parent / "app"
    in_solver = set(re.findall(r"CONSTRAINT \(?(C\d+)", (app / "solver.py").read_text()))
    in_verifier = set(re.findall(r"# ---- (C\d+):", (app / "verify.py").read_text()))
    assert in_solver, "label convention changed — update this test"
    assert in_solver <= in_verifier, (
        f"solver constraints without a verifier section: {sorted(in_solver - in_verifier)}")


# ---------------------------------------------------------------------------
# 3. Sweep + CLI
# ---------------------------------------------------------------------------

MOCKS = sorted((EXAMPLES / "mock").glob("*.json"))


@pytest.mark.parametrize("path", MOCKS, ids=lambda p: p.stem)
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_mock_scenarios_verify_across_seeds(path, seed):
    """mockgen solves each generated request itself (verify=True), so the
    hook sees a layout per scenario × seed; the explicit call below keeps
    the test meaningful even if mockgen ever stops solving in-process."""
    params = {k: v for k, v in json.loads(path.read_text()).items()
              if not k.startswith("_")}
    params["seed"] = seed
    resp = generate_mock_request(GenerateRequest.model_validate(params))
    if resp.verified is not None and resp.verified.success:
        assert verify_placement(resp.request, resp.verified) == []


def test_cli_exit_codes(tmp_path, capsys):
    ok = EXAMPLES / "success_basic.json"
    assert verify_main(["--request", str(ok)]) == 0
    assert "0 violation(s)" in capsys.readouterr().out

    assert verify_main(["--request", str(EXAMPLES / "error_infeasible.json")]) == 2

    req = PlacementRequest.model_validate_json(ok.read_text())
    res = VMPlacementSolver(req).solve()
    res.assignments[0] = PlacementAssignment(
        vm_id=res.assignments[0].vm_id, baremetal_id="nowhere")
    bad = tmp_path / "bad.json"
    bad.write_text(res.model_dump_json())
    assert verify_main(["--request", str(ok), "--result", str(bad)]) == 1
    assert "[C1]" in capsys.readouterr().out
