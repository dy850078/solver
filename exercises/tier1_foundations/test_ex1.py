"""Tests for Exercise 1: Single-Machine Bin Packing."""

from exercises.tier1_foundations.ex1_bin_packing import bin_pack


class TestBinPackBasic:
    def test_all_fit(self):
        """All VMs fit → all True."""
        result = bin_pack(
            bm_cpu=64, bm_mem=256_000,
            vms=[(4, 16_000), (8, 32_000), (4, 16_000)],
        )
        assert result == [True, True, True]

    def test_none_fit(self):
        """BM too small for any VM."""
        result = bin_pack(
            bm_cpu=2, bm_mem=4_000,
            vms=[(4, 16_000), (8, 32_000)],
        )
        assert result == [False, False]

    def test_empty_vms(self):
        """No VMs → empty list."""
        result = bin_pack(bm_cpu=64, bm_mem=256_000, vms=[])
        assert result == []


class TestBinPackCapacity:
    def test_cpu_bottleneck(self):
        """Memory fits all, but CPU doesn't → must choose subset."""
        result = bin_pack(
            bm_cpu=64, bm_mem=256_000,
            vms=[(32, 16_000), (32, 16_000), (32, 16_000)],
        )
        assert sum(result) == 2
        assert len(result) == 3

    def test_mem_bottleneck(self):
        """CPU fits all, but memory doesn't → must choose subset."""
        result = bin_pack(
            bm_cpu=64, bm_mem=256_000,
            vms=[(4, 128_000), (4, 128_000), (4, 128_000)],
        )
        assert sum(result) == 2
        assert len(result) == 3

    def test_maximize_count(self):
        """Packs the maximum number of VMs, not just any subset."""
        result = bin_pack(
            bm_cpu=64, bm_mem=256_000,
            vms=[(48, 16_000), (8, 16_000), (8, 16_000), (8, 16_000)],
        )
        assert sum(result) >= 3

    def test_exact_fit(self):
        """VMs exactly fill the BM."""
        result = bin_pack(
            bm_cpu=16, bm_mem=64_000,
            vms=[(8, 32_000), (8, 32_000)],
        )
        assert result == [True, True]
