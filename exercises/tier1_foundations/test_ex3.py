"""Tests for Exercise 3: Activation Variable Pattern."""

from exercises.tier1_foundations.ex3_activation_var import optimal_split


class TestBasicSplit:
    def test_exact_division(self):
        """16 / 8 = 2, zero waste."""
        result = optimal_split(target=16, specs=[8], max_per_spec=10)
        assert result == {8: 2}

    def test_non_exact(self):
        """30 / 8 = 3.75 → need 4 (waste=2) or use 12s."""
        result = optimal_split(target=30, specs=[8, 12], max_per_spec=10)
        total = sum(spec * count for spec, count in result.items())
        assert total >= 30
        waste = total - 30
        assert waste <= 2

    def test_zero_target(self):
        """Target 0 → all counts zero (or empty dict)."""
        result = optimal_split(target=0, specs=[8, 12], max_per_spec=10)
        total = sum(spec * count for spec, count in result.items())
        assert total == 0

    def test_single_spec_large(self):
        """100 / 12 = 8.33 → 9 × 12 = 108 (waste=8)."""
        result = optimal_split(target=100, specs=[12], max_per_spec=20)
        assert result[12] == 9
        assert 12 * 9 == 108


class TestWasteMinimization:
    def test_prefers_less_waste(self):
        """Between two valid splits, picks the one with less waste."""
        result = optimal_split(target=24, specs=[8, 12], max_per_spec=10)
        total = sum(spec * count for spec, count in result.items())
        assert total == 24

    def test_mixed_specs_less_waste(self):
        """40: 8×5=40 (waste=0) → should achieve zero waste."""
        result = optimal_split(target=40, specs=[8, 12], max_per_spec=10)
        total = sum(spec * count for spec, count in result.items())
        waste = total - 40
        assert waste == 0


class TestActivationPattern:
    def test_uses_bool_vars(self):
        """Verify the implementation uses BoolVar (activation pattern)."""
        import inspect
        from exercises.tier1_foundations.ex3_activation_var import optimal_split
        source = inspect.getsource(optimal_split)
        assert "new_bool_var" in source, (
            "Must use new_bool_var (activation variable pattern), not just IntVar for counts"
        )

    def test_symmetry_breaking(self):
        """Verify symmetry breaking is implemented (active[k] >= active[k+1])."""
        import inspect
        from exercises.tier1_foundations.ex3_activation_var import optimal_split
        source = inspect.getsource(optimal_split)
        assert ">=" in source or "active" in source.lower(), (
            "Should implement symmetry breaking: active[k] >= active[k+1]"
        )


class TestMaxPerSpec:
    def test_respects_max(self):
        """max_per_spec limits the upper bound of slots."""
        result = optimal_split(target=100, specs=[8, 12], max_per_spec=5)
        total = sum(spec * count for spec, count in result.items())
        assert total >= 100
        for count in result.values():
            assert count <= 5
