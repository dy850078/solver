"""Tests for the diagnostics module (app/diagnostics.py)."""

from app.models import AntiAffinityRule, NodeRole

from .conftest import make_bm, make_vm, solve


class TestConstraintLayerCheck:
    """Verify that constraint_check pinpoints the failing layer."""

    def test_anti_affinity_is_failing_layer(self):
        """2 VMs spread by AG with cap 1, only 1 AG → anti_affinity should be the failing layer."""
        bms = [make_bm("bm-1", ag="ag-1"), make_bm("bm-2", ag="ag-1")]
        vms = [make_vm(f"vm-{i}") for i in range(2)]
        rules = [AntiAffinityRule(group_id="g1", vm_ids=["vm-0", "vm-1"], spread_on=["ag"], cap_per_bucket={"ag": 1})]
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
        rules = [AntiAffinityRule(group_id="spread-test", vm_ids=["vm-0", "vm-1"], spread_on=["ag"], cap_per_bucket={"ag": 1})]
        r = solve(vms, bms, rules)

        assert not r.success
        aa_rules = r.diagnostics["infeasible_anti_affinity_rules"]
        assert len(aa_rules) == 1
        assert aa_rules[0]["group_id"] == "spread-test"
        assert aa_rules[0]["per_dimension_caps"] == {"ag": 1}
        failed = aa_rules[0]["failed_dimensions"]
        assert len(failed) == 1
        assert failed[0]["dimension"] == "ag"
        assert failed[0]["min_buckets_needed"] == 2
        assert failed[0]["reachable_buckets"] == 1

    def test_counts_section(self):
        """Diagnostics should include summary counts."""
        bms = [make_bm("bm-1", cpu=2)]
        vms = [make_vm("vm-1", cpu=16)]
        r = solve(vms, bms)

        assert not r.success
        counts = r.diagnostics["counts"]
        assert counts["vms"] == 1
        assert counts["bms"] == 1


class TestPinnedDiagnostics:
    """The layer check mirrors pin fixing AND grandfathered caps — without
    the mirror, a grandfathered skew would falsely attribute the failure
    to anti_affinity instead of the real layer."""

    def test_grandfathered_skew_does_not_blame_anti_affinity(self):
        from app.models import ExclusiveBaremetalRule

        # 3 pinned masters all in ag-0: cap ceil(3/3)=1 is violated by
        # history, but grandfathered — the REAL infeasibility is the
        # exclusive group: 2 members, only 1 reachable BM.
        bms = [
            make_bm("bm-a0", ag="ag-0",
                    used_cpu=12, used_mem=48_000, used_disk=300),
            make_bm("bm-a1", ag="ag-1"),
            make_bm("bm-a2", ag="ag-2"),
        ]
        # Distinct roles keep the two appliances out of any shared auto
        # anti-affinity group — the ONLY structural problem left is C6.
        vms = [
            make_vm(f"m-{i}", role=NodeRole.MASTER, ip_type="routable",
                    pinned_to="bm-a0")
            for i in range(3)
        ] + [
            make_vm("f5-a", role="f5", candidates=["bm-a1"]),
            make_vm("f5-b", role="f5b", candidates=["bm-a1"]),
        ]
        rule = ExclusiveBaremetalRule(group_id="ex", vm_ids=["f5-a", "f5-b"])
        r = solve(vms, bms, exclusive_rules=[rule],
                  auto_generate_anti_affinity=True)

        assert not r.success
        cc = r.diagnostics["constraint_check"]
        assert cc["anti_affinity"] == "OK", (
            f"grandfathered skew must not fail the AA layer: {cc}"
        )
        assert cc["failed_at"] == "exclusive", cc
