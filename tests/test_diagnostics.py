"""Tests for the diagnostics module (app/diagnostics.py)."""

from app.models import AntiAffinityRule, NodeRole

from .conftest import make_bm, make_vm, solve


class TestConstraintLayerCheck:
    """Verify that constraint_check pinpoints the failing layer."""

    def test_anti_affinity_is_failing_layer(self):
        """3 VMs, max_per_ag=1, only 2 AGs → anti_affinity should be the failing layer."""
        bms = [make_bm("bm-1", ag="ag-1"), make_bm("bm-2", ag="ag-1")]
        vms = [make_vm(f"vm-{i}") for i in range(2)]
        rules = [AntiAffinityRule(group_id="g1", vm_ids=["vm-0", "vm-1"], max_per_ag=1)]
        r = solve(vms, bms, rules)

        assert not r.success
        cc = r.diagnostics["constraint_check"]
        assert cc["one_bm_per_vm"] == "OK"
        assert cc["capacity"] == "OK"
        assert cc["anti_affinity"] == "INFEASIBLE"
        assert cc["failed_at"] == "anti_affinity"

    def test_capacity_is_failing_layer(self):
        """VM needs more resources than any BM has → capacity should fail."""
        bms = [make_bm("bm-1", cpu=2)]
        vms = [make_vm("vm-1", cpu=16)]
        r = solve(vms, bms)

        assert not r.success
        cc = r.diagnostics["constraint_check"]
        assert cc["one_bm_per_vm"] == "INFEASIBLE"
        assert cc["failed_at"] == "one_bm_per_vm"

    def test_successful_solve_has_no_diagnostics(self):
        """Successful solve should have empty diagnostics."""
        r = solve([make_vm("vm-1")], [make_bm("bm-1")])
        assert r.success
        assert r.diagnostics == {}


class TestDiagnosticsSections:
    """Verify individual diagnostic sections."""

    def test_vms_with_no_eligible_bm(self):
        """VM that can't fit anywhere should appear in diagnostics."""
        bms = [make_bm("bm-1", cpu=2)]
        vms = [make_vm("vm-big", cpu=128)]
        r = solve(vms, bms)

        assert not r.success
        assert "vm-big" in r.diagnostics["vms_with_no_eligible_bm"]

    def test_infeasible_anti_affinity_rules_reported(self):
        """Anti-affinity rule that can't be satisfied should be flagged."""
        bms = [make_bm("bm-1", ag="ag-1"), make_bm("bm-2", ag="ag-1")]
        vms = [make_vm("vm-0"), make_vm("vm-1")]
        rules = [AntiAffinityRule(group_id="spread-test", vm_ids=["vm-0", "vm-1"], max_per_ag=1)]
        r = solve(vms, bms, rules)

        assert not r.success
        aa_rules = r.diagnostics["infeasible_anti_affinity_rules"]
        assert len(aa_rules) == 1
        assert aa_rules[0]["group_id"] == "spread-test"
        assert aa_rules[0]["min_ags_needed"] == 2
        assert aa_rules[0]["reachable_ags"] == 1

    def test_counts_section(self):
        """Diagnostics should include summary counts."""
        bms = [make_bm("bm-1", cpu=2)]
        vms = [make_vm("vm-1", cpu=16)]
        r = solve(vms, bms)

        assert not r.success
        counts = r.diagnostics["counts"]
        assert counts["vms"] == 1
        assert counts["bms"] == 1
