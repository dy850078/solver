"""Tests for Exercise 6: Spec Preference Soft Constraint."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from ortools.sat.python import cp_model

from exercises.conftest import make_bm, make_req, Resources
from exercises.tier3_splitter.ex6_spec_preference import (
    PreferenceSplitter,
)
from app.models import SolverConfig, NodeRole


def _solve_preference_split(
    requirements, bms, spec_preferences=None, w_resource_waste=5, w_preference=3,
):
    """Helper: build a model with preference splitter and solve it."""
    config = SolverConfig(
        vm_specs=[],
        w_resource_waste=w_resource_waste,
        max_solve_time_seconds=10,
    )
    model = cp_model.CpModel()
    splitter = PreferenceSplitter(
        model=model,
        requirements=requirements if isinstance(requirements, list) else [requirements],
        baremetals=bms,
        config=config,
        spec_preferences=spec_preferences or {},
        w_preference=w_preference,
    )
    synthetic_vms = splitter.build()
    waste_terms = splitter.build_waste_objective_terms()
    pref_terms = splitter.build_preference_terms()

    obj_terms = []
    if waste_terms:
        obj_terms.append(config.w_resource_waste * sum(waste_terms))
    if pref_terms:
        obj_terms.extend(pref_terms)
    if obj_terms:
        model.minimize(sum(obj_terms))

    solver = cp_model.CpSolver()
    status = solver.solve(model)

    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return splitter.get_split_decisions(solver)
    return []


class TestSpecPreference:
    def test_both_zero_waste_pick_preferred(self):
        """Two specs both achieve zero waste → pick the one with higher preference."""
        bms = [make_bm("bm-1", cpu=64, mem=256_000)]
        spec_8 = Resources(cpu_cores=8, memory_mib=32_000)
        spec_12 = Resources(cpu_cores=12, memory_mib=48_000)
        req = make_req(
            cpu=24, mem=96_000,
            vm_specs=[spec_8, spec_12],
        )
        prefs = {
            (12, 48_000, 0, 0): 10,
            (8, 32_000, 0, 0): 1,
        }
        decisions = _solve_preference_split([req], bms, spec_preferences=prefs)
        assert any(d.vm_spec.cpu_cores == 12 and d.count == 2 for d in decisions)

    def test_waste_overrides_preference(self):
        """High-preference spec has more waste → waste weight wins."""
        bms = [make_bm("bm-1", cpu=64, mem=256_000)]
        spec_8 = Resources(cpu_cores=8, memory_mib=32_000)
        spec_12 = Resources(cpu_cores=12, memory_mib=48_000)
        req = make_req(cpu=40, mem=160_000, vm_specs=[spec_8, spec_12])
        prefs = {
            (12, 48_000, 0, 0): 5,
            (8, 32_000, 0, 0): 1,
        }
        decisions = _solve_preference_split(
            [req], bms, spec_preferences=prefs, w_resource_waste=10, w_preference=1,
        )
        total_waste = sum(
            d.vm_spec.cpu_cores * d.count for d in decisions
        ) - 40
        assert total_waste == 0

    def test_no_preference_uses_waste_only(self):
        """Without preferences, behaves like original splitter."""
        bms = [make_bm("bm-1", cpu=64, mem=256_000)]
        spec_8 = Resources(cpu_cores=8, memory_mib=32_000)
        req = make_req(cpu=32, mem=128_000, vm_specs=[spec_8])
        decisions = _solve_preference_split([req], bms, spec_preferences={})
        assert any(d.vm_spec.cpu_cores == 8 and d.count == 4 for d in decisions)

    def test_preference_terms_returned(self):
        """build_preference_terms() returns non-empty list when preferences exist."""
        bms = [make_bm("bm-1", cpu=64, mem=256_000)]
        spec_8 = Resources(cpu_cores=8, memory_mib=32_000)
        req = make_req(cpu=32, mem=128_000, vm_specs=[spec_8])
        config = SolverConfig(vm_specs=[], max_solve_time_seconds=10)
        model = cp_model.CpModel()
        splitter = PreferenceSplitter(
            model=model,
            requirements=[req],
            baremetals=bms,
            config=config,
            spec_preferences={(8, 32_000, 0, 0): 5},
            w_preference=3,
        )
        splitter.build()
        terms = splitter.build_preference_terms()
        assert len(terms) > 0
