"""
Resource Splitter — splits total resource requirements into VM spec × count.

Works jointly with VMPlacementSolver by sharing the same CP-SAT CpModel.
The splitter creates decision variables for how many VMs of each spec to
produce, and the solver adds placement constraints on top.

This ensures global optimality: the split considers placement feasibility
(anti-affinity, topology, BM capacity) rather than optimizing in isolation.
"""

from __future__ import annotations

import logging
from typing import Any

from ortools.sat.python import cp_model

from .models import (
    Baremetal,
    NodeRole,
    Resources,
    ResourceRequirement,
    SolverConfig,
    SplitDecision,
    VM,
)

logger = logging.getLogger(__name__)

RESOURCE_FIELDS = ["cpu_cores", "memory_mib", "storage_gb", "gpu_count"]


class ResourceSplitter:
    """
    Builds CP-SAT decision variables and constraints for requirement splitting.

    For each ResourceRequirement (one per role):
      - Determines the spec pool (from requirement or config fallback)
      - Creates count[spec] integer variables
      - Creates synthetic VM objects with active[vm_id] boolean variables
      - Adds resource coverage constraints (>= total_resources)
      - Adds VM count constraints (min/max_total_vms)

    The synthetic VMs are then passed to VMPlacementSolver for placement.
    """

    def __init__(
        self,
        model: cp_model.CpModel,
        requirements: list[ResourceRequirement],
        baremetals: list[Baremetal],
        config: SolverConfig,
    ):
        self.model = model
        self.requirements = requirements
        self.baremetals = baremetals
        self.config = config

        # Decision variables
        self.count_vars: dict[tuple[int, int], cp_model.IntVar] = {}  # (req_idx, spec_idx) -> count
        self.active_vars: dict[str, cp_model.IntVar] = {}  # vm_id -> BoolVar
        self.synthetic_vms: list[VM] = []

        # Track specs per requirement for solution extraction
        self._req_specs: dict[int, list[Resources]] = {}

    def build(self) -> list[VM]:
        """
        Build split variables and constraints. Returns synthetic VMs.

        Each synthetic VM has an associated active_var in self.active_vars.
        The solver must constrain: assign[(vm_id, bm_id)] <= active[vm_id].
        """
        for req_idx, req in enumerate(self.requirements):
            specs = self._resolve_specs(req)
            if not specs:
                logger.warning(
                    "Requirement %d (role=%s) has no usable VM specs; skipping",
                    req_idx, req.node_role.value,
                )
                continue
            self._req_specs[req_idx] = specs
            self._build_requirement(req_idx, req, specs)

        return self.synthetic_vms

    def _resolve_specs(self, req: ResourceRequirement) -> list[Resources]:
        """Determine the spec pool: requirement-level or config-level fallback."""
        candidates = req.vm_specs if req.vm_specs is not None else self.config.vm_specs
        if not candidates:
            return []

        # Filter out specs that don't fit in any BM's available capacity
        usable = []
        for spec in candidates:
            if any(spec.fits_in(bm.available_capacity) for bm in self.baremetals):
                usable.append(spec)
            else:
                logger.debug(
                    "Spec %s filtered out: doesn't fit any BM", spec,
                )
        return usable

    def _build_requirement(
        self,
        req_idx: int,
        req: ResourceRequirement,
        specs: list[Resources],
    ) -> None:
        """Build variables and constraints for one ResourceRequirement."""
        count_vars_for_req: list[cp_model.IntVar] = []

        for spec_idx, spec in enumerate(specs):
            # Compute upper bound: max VMs of this spec that could cover the total
            upper = self._compute_upper_bound(req, spec)
            if upper <= 0:
                continue

            count_var = self.model.new_int_var(
                0, upper, f"count_r{req_idx}_s{spec_idx}"
            )
            self.count_vars[(req_idx, spec_idx)] = count_var
            count_vars_for_req.append(count_var)

            # Create synthetic VMs with active vars
            active_vars_for_spec: list[cp_model.IntVar] = []
            for k in range(upper):
                vm_id = f"split-r{req_idx}-s{spec_idx}-{k}"
                active_var = self.model.new_bool_var(f"active_{vm_id}")
                self.active_vars[vm_id] = active_var
                active_vars_for_spec.append(active_var)

                self.synthetic_vms.append(VM(
                    id=vm_id,
                    demand=spec,
                    node_role=req.node_role,
                    ip_type=req.ip_type,
                    cluster_id=req.cluster_id,
                ))

            # Link active vars to count: sum(active[0..upper-1]) == count
            self.model.add(sum(active_vars_for_spec) == count_var)

            # Symmetry breaking: active[k] >= active[k+1]
            for k in range(len(active_vars_for_spec) - 1):
                self.model.add(
                    active_vars_for_spec[k] >= active_vars_for_spec[k + 1]
                )

        if not count_vars_for_req:
            logger.warning(
                "Requirement %d (role=%s): no spec has positive upper bound",
                req_idx, req.node_role.value,
            )
            return

        # Resource coverage constraint: sum(count[s] * spec[s].field) >= total.field
        for field in RESOURCE_FIELDS:
            total_demand = getattr(req.total_resources, field)
            if total_demand <= 0:
                continue
            allocated = sum(
                self.count_vars[(req_idx, si)] * getattr(specs[si], field)
                for si in range(len(specs))
                if (req_idx, si) in self.count_vars
            )
            self.model.add(allocated >= total_demand)

        # VM count constraints
        total_vms = sum(count_vars_for_req)
        if req.min_total_vms is not None:
            self.model.add(total_vms >= req.min_total_vms)
        if req.max_total_vms is not None:
            self.model.add(total_vms <= req.max_total_vms)

    def _compute_upper_bound(self, req: ResourceRequirement, spec: Resources) -> int:
        """Max VMs of this spec that could be needed to cover the requirement."""
        upper = 0
        for field in RESOURCE_FIELDS:
            spec_val = getattr(spec, field)
            total_val = getattr(req.total_resources, field)
            if spec_val > 0 and total_val > 0:
                # ceil(total / spec) gives the max needed on this dimension
                needed = (total_val + spec_val - 1) // spec_val
                upper = max(upper, needed)

        # Ensure upper bound is at least min_total_vms (so the constraint can be satisfied)
        if req.min_total_vms is not None:
            upper = max(upper, req.min_total_vms)

        # Respect max_total_vms if set
        if req.max_total_vms is not None and upper > 0:
            upper = min(upper, req.max_total_vms)

        return upper

    def build_waste_objective_terms(self) -> list[cp_model.LinearExpr]:
        """
        Build objective terms that penalize resource waste (over-allocation).

        waste = sum(count[s] * spec[s].field) - total_resources.field
        Returns a list of waste expressions (one per requirement × dimension).
        """
        terms = []
        for req_idx, req in enumerate(self.requirements):
            specs = self._req_specs.get(req_idx, [])
            for field in RESOURCE_FIELDS:
                total_demand = getattr(req.total_resources, field)
                if total_demand <= 0:
                    continue
                allocated = sum(
                    self.count_vars[(req_idx, si)] * getattr(specs[si], field)
                    for si in range(len(specs))
                    if (req_idx, si) in self.count_vars
                )
                # waste = allocated - total_demand (always >= 0 due to coverage constraint)
                terms.append(allocated - total_demand)
        return terms

    def get_split_decisions(self, solver: cp_model.CpSolver) -> list[SplitDecision]:
        """Extract split decisions from a solved model."""
        decisions = []
        for req_idx, req in enumerate(self.requirements):
            specs = self._req_specs.get(req_idx, [])
            for spec_idx, spec in enumerate(specs):
                if (req_idx, spec_idx) not in self.count_vars:
                    continue
                count = solver.value(self.count_vars[(req_idx, spec_idx)])
                if count > 0:
                    decisions.append(SplitDecision(
                        node_role=req.node_role,
                        vm_spec=spec,
                        count=count,
                    ))
        return decisions
