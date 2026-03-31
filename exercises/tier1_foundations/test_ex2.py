"""Tests for Exercise 2: Multi-Machine VM Assignment."""

from exercises.tier1_foundations.ex2_multi_assignment import assign_vms


class TestAssignBasic:
    def test_one_vm_one_bm(self):
        """Simplest case: 1 VM fits on 1 BM."""
        bms = [{"id": "bm-1", "cpu": 64, "mem": 256_000}]
        vms = [{"id": "vm-1", "cpu": 4, "mem": 16_000}]
        result = assign_vms(bms, vms)
        assert result == {"vm-1": "bm-1"}

    def test_three_vms_three_bms(self):
        """3 VMs, 3 BMs, each BM can hold exactly 1 VM."""
        bms = [
            {"id": "bm-1", "cpu": 8, "mem": 32_000},
            {"id": "bm-2", "cpu": 8, "mem": 32_000},
            {"id": "bm-3", "cpu": 8, "mem": 32_000},
        ]
        vms = [
            {"id": "vm-1", "cpu": 8, "mem": 32_000},
            {"id": "vm-2", "cpu": 8, "mem": 32_000},
            {"id": "vm-3", "cpu": 8, "mem": 32_000},
        ]
        result = assign_vms(bms, vms)
        assert result is not None
        assert set(result.keys()) == {"vm-1", "vm-2", "vm-3"}
        assert set(result.values()) == {"bm-1", "bm-2", "bm-3"}

    def test_empty_vms(self):
        """No VMs → empty map."""
        bms = [{"id": "bm-1", "cpu": 64, "mem": 256_000}]
        result = assign_vms(bms, [])
        assert result == {}


class TestAssignCapacity:
    def test_two_big_vms_must_split(self):
        """2 VMs too big for one BM → must go to different BMs."""
        bms = [
            {"id": "bm-1", "cpu": 32, "mem": 128_000},
            {"id": "bm-2", "cpu": 32, "mem": 128_000},
        ]
        vms = [
            {"id": "vm-1", "cpu": 24, "mem": 96_000},
            {"id": "vm-2", "cpu": 24, "mem": 96_000},
        ]
        result = assign_vms(bms, vms)
        assert result is not None
        assert result["vm-1"] != result["vm-2"]

    def test_uneven_bms(self):
        """Big VM must go to big BM."""
        bms = [
            {"id": "bm-small", "cpu": 8, "mem": 32_000},
            {"id": "bm-big", "cpu": 64, "mem": 256_000},
        ]
        vms = [
            {"id": "vm-big", "cpu": 32, "mem": 128_000},
            {"id": "vm-small", "cpu": 4, "mem": 16_000},
        ]
        result = assign_vms(bms, vms)
        assert result is not None
        assert result["vm-big"] == "bm-big"

    def test_insufficient_capacity(self):
        """Total capacity not enough → None."""
        bms = [{"id": "bm-1", "cpu": 8, "mem": 32_000}]
        vms = [
            {"id": "vm-1", "cpu": 8, "mem": 32_000},
            {"id": "vm-2", "cpu": 8, "mem": 32_000},
        ]
        result = assign_vms(bms, vms)
        assert result is None

    def test_packing_multiple_on_one_bm(self):
        """Multiple small VMs fit on one BM."""
        bms = [
            {"id": "bm-1", "cpu": 64, "mem": 256_000},
            {"id": "bm-2", "cpu": 64, "mem": 256_000},
        ]
        vms = [
            {"id": f"vm-{i}", "cpu": 4, "mem": 16_000}
            for i in range(4)
        ]
        result = assign_vms(bms, vms)
        assert result is not None
        assert len(result) == 4
