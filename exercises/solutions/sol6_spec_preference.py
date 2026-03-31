"""Reference solution for Exercise 6: Spec Preference Soft Constraint."""

from __future__ import annotations

from ortools.sat.python import cp_model

from app.models import Baremetal, ResourceRequirement, SolverConfig, Resources
from app.splitter import ResourceSplitter


class PreferenceSplitter(ResourceSplitter):

    def __init__(
        self,
        *,
        spec_preferences: dict[tuple[int, int, int, int], int] | None = None,
        w_preference: int = 3,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.spec_preferences = spec_preferences or {}
        self.w_preference = w_preference

    def build_preference_terms(self) -> list:
        if not self.spec_preferences:
            return []

        terms = []
        for (req_idx, spec_idx), count_var in self.count_vars.items():
            specs = self._req_specs.get(req_idx, [])
            if spec_idx >= len(specs):
                continue
            spec = specs[spec_idx]
            key = (spec.cpu_cores, spec.memory_mib, spec.storage_gb, spec.gpu_count)
            weight = self.spec_preferences.get(key, 0)
            if weight > 0:
                terms.append(-self.w_preference * weight * count_var)
        return terms
