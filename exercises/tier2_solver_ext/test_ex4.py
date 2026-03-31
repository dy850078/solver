"""Tests for Exercise 4: Rack-Level Anti-Affinity."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from exercises.conftest import make_bm, make_vm, amap
from exercises.tier2_solver_ext.ex4_rack_anti_affinity import (
    RackAntiAffinityRule,
    solve_with_rack_anti_affinity,
)
from app.models import NodeRole, AntiAffinityRule


class TestRackAntiAffinity:
    def test_three_racks_max_per_rack_1(self):
        """3 BMs in 3 different racks → rack rule max_per_rack=1 spreads VMs across racks."""
        bms = [
            make_bm("bm-1", ag="ag-1", rack="rack-1"),
            make_bm("bm-2", ag="ag-2", rack="rack-2"),
            make_bm("bm-3", ag="ag-3", rack="rack-3"),
        ]
        vms = [
            make_vm("vm-1", role=NodeRole.MASTER),
            make_vm("vm-2", role=NodeRole.MASTER),
            make_vm("vm-3", role=NodeRole.MASTER),
        ]
        rack_rules = [
            RackAntiAffinityRule(
                group_id="masters",
                vm_ids=["vm-1", "vm-2", "vm-3"],
                max_per_rack=1,
            )
        ]
        r = solve_with_rack_anti_affinity(vms, bms, rack_rules=rack_rules)
        assert r.success
        m = amap(r)
        # Each VM on a different rack
        racks_used = {b for b in m.values()}
        assert len(racks_used) == 3

    def test_two_racks_max_per_rack_2(self):
        """6 BMs / 2 racks / 3 AGs → max_per_rack=2 distributes VMs."""
        bms = [
            make_bm("bm-1", ag="ag-1", rack="rack-1"),
            make_bm("bm-2", ag="ag-2", rack="rack-1"),
            make_bm("bm-3", ag="ag-3", rack="rack-1"),
            make_bm("bm-4", ag="ag-1", rack="rack-2"),
            make_bm("bm-5", ag="ag-2", rack="rack-2"),
            make_bm("bm-6", ag="ag-3", rack="rack-2"),
        ]
        vms = [make_vm(f"vm-{i}") for i in range(4)]
        rack_rules = [
            RackAntiAffinityRule(
                group_id="workers",
                vm_ids=[f"vm-{i}" for i in range(4)],
                max_per_rack=2,
            )
        ]
        r = solve_with_rack_anti_affinity(vms, bms, rack_rules=rack_rules)
        assert r.success
        m = amap(r)
        rack1_bms = {"bm-1", "bm-2", "bm-3"}
        rack1_count = sum(1 for b in m.values() if b in rack1_bms)
        rack2_count = sum(1 for b in m.values() if b not in rack1_bms)
        assert rack1_count <= 2
        assert rack2_count <= 2

    def test_no_rack_rule_same_as_original(self):
        """Without rack rules, behaves like the original solver."""
        bms = [make_bm("bm-1"), make_bm("bm-2")]
        vms = [make_vm("vm-1"), make_vm("vm-2")]
        r = solve_with_rack_anti_affinity(vms, bms, rack_rules=[])
        assert r.success
        assert len(amap(r)) == 2

    def test_rack_and_ag_rules_together(self):
        """Rack rules and AG rules both apply simultaneously."""
        bms = [
            make_bm("bm-1", ag="ag-1", rack="rack-1"),
            make_bm("bm-2", ag="ag-2", rack="rack-1"),
            make_bm("bm-3", ag="ag-1", rack="rack-2"),
            make_bm("bm-4", ag="ag-2", rack="rack-2"),
        ]
        vms = [make_vm(f"vm-{i}", role=NodeRole.MASTER) for i in range(4)]
        vm_ids = [f"vm-{i}" for i in range(4)]
        ag_rules = [AntiAffinityRule(group_id="ag-spread", vm_ids=vm_ids, max_per_ag=2)]
        rack_rules = [RackAntiAffinityRule(group_id="rack-spread", vm_ids=vm_ids, max_per_rack=2)]
        r = solve_with_rack_anti_affinity(
            vms, bms, ag_rules=ag_rules, rack_rules=rack_rules,
        )
        assert r.success
        m = amap(r)
        ag1_count = sum(1 for b in m.values() if b in ("bm-1", "bm-3"))
        ag2_count = sum(1 for b in m.values() if b in ("bm-2", "bm-4"))
        assert ag1_count <= 2
        assert ag2_count <= 2
        rack1_count = sum(1 for b in m.values() if b in ("bm-1", "bm-2"))
        rack2_count = sum(1 for b in m.values() if b in ("bm-3", "bm-4"))
        assert rack1_count <= 2
        assert rack2_count <= 2

    def test_infeasible_rack_constraint(self):
        """Not enough racks → infeasible."""
        bms = [
            make_bm("bm-1", ag="ag-1", rack="rack-1"),
            make_bm("bm-2", ag="ag-2", rack="rack-1"),
        ]
        vms = [make_vm("vm-1"), make_vm("vm-2")]
        rack_rules = [
            RackAntiAffinityRule(
                group_id="test",
                vm_ids=["vm-1", "vm-2"],
                max_per_rack=1,
            )
        ]
        r = solve_with_rack_anti_affinity(vms, bms, rack_rules=rack_rules)
        assert not r.success
