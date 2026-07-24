"""
Resource Splitter — decomposes a total resource budget into VM spec × count.

Shares a CP-SAT CpModel with VMPlacementSolver so the split decision and
placement are optimized **jointly** in a single solve call. This avoids the
"two-step" failure mode where a split that looks valid in isolation turns out
to be unplaceable due to anti-affinity or BM capacity constraints.

Key concepts
────────────
  ResourceRequirement   "I need 32 CPU / 128 GiB for workers"
  vm_specs              candidate VM sizes (e.g. 8 CPU / 32 GiB)
  count_var[s]          how many VMs of spec s to create (IntVar 0..upper)
  active_var[vm_id]     whether synthetic VM slot k is used (BoolVar)
  upper bound           ceil(total / spec) per resource dimension

Constraint structure
────────────────────
  ∀ field: Σ_s (count[s] × spec[s].field) ≥ total_resources.field   (coverage)
  Σ_s count[s] ∈ [min_total_vms, max_total_vms]                       (count bounds)
  Σ_k active[k] == count[s]       for each spec s                     (link count → slots)
  active[k] ≥ active[k+1]                                             (symmetry breaking)
  Σ_k active[vm_id_k] == active_var[vm_id]  → handled in solver       (placement link)
"""

from __future__ import annotations

import logging

from ortools.sat.python import cp_model

from .models import (
    Baremetal,
    Resources,
    ResourceRequirement,
    SolverConfig,
    SplitDecision,
    VM,
)

logger = logging.getLogger(__name__)

RESOURCE_FIELDS = ["cpu_cores", "memory_mib", "storage_gb", "gpu_count"]


def has_demand(req: ResourceRequirement) -> bool:
    """True when the requirement asks for anything (any resource dimension,
    a pod floor, or a VM-count floor). All-zero rows are explicit "no growth"
    markers and must never fail as unsatisfiable."""
    return (
        req.total_pods > 0
        or (req.min_total_vms or 0) > 0
        or any(getattr(req.total_resources, f) > 0 for f in RESOURCE_FIELDS)
    )


def pod_node_floor(req: ResourceRequirement, max_pods_per_node: int) -> int:
    """
    Minimum node count implied by the "at least N pods" demand:
    ceil(total_pods / max_pods_per_node). 0 when disabled or no pod demand.
    """
    if max_pods_per_node <= 0 or req.total_pods <= 0:
        return 0
    return (req.total_pods + max_pods_per_node - 1) // max_pods_per_node


def spec_count_upper_bound(
    req: ResourceRequirement, spec: Resources, max_pods_per_node: int,
) -> int:
    """
    Worst-case number of `spec` VMs needed to satisfy `req` on its own:
    the max across resource dimensions of ceil(total / spec), raised to the
    pod-node floor and min_total_vms, capped by max_total_vms.

    Shared by the splitter (slot generation) and the capacity planner
    (buyable-BM sizing) so the two can never disagree.
    """
    upper = 0
    for field in RESOURCE_FIELDS:
        spec_val = getattr(spec, field)
        total_val = getattr(req.total_resources, field)
        if spec_val > 0 and total_val > 0:
            upper = max(upper, (total_val + spec_val - 1) // spec_val)

    floor = pod_node_floor(req, max_pods_per_node)
    if req.min_total_vms is not None:
        floor = max(floor, req.min_total_vms)
    if floor > 0:
        upper = max(upper, floor)
    if req.max_total_vms is not None and upper > 0:
        upper = min(upper, req.max_total_vms)
    return upper


class ResourceSplitter:

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

        # (req_idx, spec_idx) → IntVar representing count of that spec
        self.count_vars: dict[tuple[int, int], cp_model.IntVar] = {}
        # vm_id → BoolVar (1 = this synthetic slot is active / must be placed)
        self.active_vars: dict[str, cp_model.IntVar] = {}
        self.synthetic_vms: list[VM] = []

        # specs actually used per requirement (for solution extraction)
        self._req_specs: dict[int, list[Resources]] = {}

        # Requirements with real demand that could not produce any synthetic
        # VM (no usable specs / candidates, or count bounds collapsed to 0).
        # Each entry also posts an infeasibility into the model so the demand
        # can never silently succeed; callers may inspect this list to fail
        # fast with a clearer INPUT_ERROR instead.
        self.dropped_requirements: list[dict] = []

    def build(self) -> list[VM]:
        """
        Add split variables and constraints to the shared model.
        Returns synthetic VM objects to be appended to the placement request.
        """
        for req_idx, req in enumerate(self.requirements):
            eligible_ids = self._eligible_candidate_ids(req)
            specs = self._resolve_specs(req, eligible_ids)
            if not specs:
                self._drop_requirement(
                    req_idx, req,
                    "no usable vm_specs (missing specs, empty or "
                    "network-filtered candidate_baremetals, or no spec fits "
                    "any candidate BM)",
                )
                continue
            self._req_specs[req_idx] = specs
            self._build_requirement(req_idx, req, specs, eligible_ids)
        return self.synthetic_vms

    def _eligible_candidate_ids(self, req: ResourceRequirement) -> list[str]:
        """
        The requirement's candidate BMs, narrowed to its network domain when
        one is declared (缺口 3g: a cluster lives entirely inside one network;
        "" on the requirement means no restriction).
        """
        ids = req.candidate_baremetals
        if req.network:
            network_of = {bm.id: bm.network for bm in self.baremetals}
            ids = [i for i in ids if network_of.get(i) == req.network]
        return ids

    def _drop_requirement(self, req_idx: int, req: ResourceRequirement,
                          reason: str) -> None:
        """
        A requirement that cannot produce synthetic VMs. Zero-demand
        requirements are skipped silently; anything with real demand is
        recorded and makes the model infeasible — silently succeeding while
        ignoring demand would report "inventory sufficient" for load that was
        never placed.
        """
        logger.warning(
            "Requirement %d (role=%s) produced no synthetic VMs: %s",
            req_idx, req.node_role.value, reason,
        )
        if not has_demand(req):
            return
        self.dropped_requirements.append({
            "requirement_index": req_idx,
            "cluster_id": req.cluster_id,
            "node_role": req.node_role.value,
            "reason": reason,
        })
        lit = self.model.new_bool_var(f"dropped_req_{req_idx}")
        self.model.add(lit == 1)
        self.model.add(lit == 0)

    def _resolve_specs(self, req: ResourceRequirement,
                       eligible_ids: list[str]) -> list[Resources]:
        """
        Spec pool: requirement-level vm_specs takes precedence; falls back to
        config.vm_specs. Specs that don't fit any eligible BM's available
        capacity are filtered out immediately (they can never be placed).

        Requirements must have candidate_baremetals populated by the scheduler
        (step 3 filtering result). Empty candidate_baremetals is treated as a
        contract violation: no specs are usable and the requirement is
        dropped (see _drop_requirement).
        """
        candidates = req.vm_specs if req.vm_specs is not None else self.config.vm_specs
        if not candidates:
            return []
        if not eligible_ids:
            logger.error(
                "Requirement (role=%s) has no eligible candidate_baremetals — "
                "scheduler must provide step 3 filtering result",
                req.node_role.value,
            )
            return []
        candidate_set = set(eligible_ids)
        eligible_bms = [bm for bm in self.baremetals if bm.id in candidate_set]
        usable = [
            spec for spec in candidates
            if any(spec.fits_in(bm.available_capacity) for bm in eligible_bms)
        ]
        filtered_out = len(candidates) - len(usable)
        if filtered_out:
            logger.debug("%d spec(s) filtered: doesn't fit any BM", filtered_out)
        return usable

    def _build_requirement(
        self,
        req_idx: int,
        req: ResourceRequirement,
        specs: list[Resources],
        eligible_ids: list[str],
    ) -> None:
        count_vars_for_req: list[cp_model.IntVar] = []

        for spec_idx, spec in enumerate(specs):
            upper = self._compute_upper_bound(req, spec)
            if upper <= 0:
                continue

            count_var = self.model.new_int_var(0, upper, f"count_r{req_idx}_s{spec_idx}")
            self.count_vars[(req_idx, spec_idx)] = count_var
            count_vars_for_req.append(count_var)

            # Create `upper` synthetic VM slots, one active_var each
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
                    candidate_baremetals=eligible_ids,
                ))

            # Σ active == count  (so count drives how many slots are used)
            self.model.add(sum(active_vars_for_spec) == count_var)

            # Symmetry breaking: active[k] ≥ active[k+1]
            # Without this, swapping two identical inactive slots gives an
            # equivalent but different solution — symmetry breaks speed.
            for k in range(len(active_vars_for_spec) - 1):
                self.model.add(active_vars_for_spec[k] >= active_vars_for_spec[k + 1])

        if not count_vars_for_req:
            self._drop_requirement(
                req_idx, req,
                "no spec has a positive upper bound (count bounds such as "
                "max_total_vms may conflict with the demand or pod floor)",
            )
            return

        # Resource coverage: Σ_s count[s] × spec[s].field ≥ total.field
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

        # Optional count bounds
        total_vms = sum(count_vars_for_req)
        if req.min_total_vms is not None:
            self.model.add(total_vms >= req.min_total_vms)
        pod_floor = self._pod_node_floor(req)
        if pod_floor > 0:
            self.model.add(total_vms >= pod_floor)
        if req.max_total_vms is not None:
            self.model.add(total_vms <= req.max_total_vms)

    def _compute_upper_bound(self, req: ResourceRequirement, spec: Resources) -> int:
        """
        Worst-case maximum VMs of this spec needed to satisfy the requirement,
        with the pod-node floor and min/max_total_vms folded in (so the count
        var can actually reach the floor; the hard constraints live in
        _build_requirement). Delegates to the shared module-level helper.
        """
        return spec_count_upper_bound(req, spec, self.config.max_pods_per_node)

    def _pod_node_floor(self, req: ResourceRequirement) -> int:
        """
        Minimum node count implied by the "at least N pods" demand — a lower
        bound, never an exact target: provisioned capacity (node_count ×
        max_pods_per_node) is guaranteed >= total_pods, and extra pod headroom
        from resource-driven nodes is harmless. Delegates to the shared
        module-level helper.
        """
        return pod_node_floor(req, self.config.max_pods_per_node)

    def build_waste_objective_terms(self) -> list:
        """
        Returns CP-SAT expressions representing over-allocation waste per
        (requirement, resource dimension). These are injected into the solver's
        objective so the split prefers tight fits.

        waste = Σ_s count[s] × spec[s].field − total_demand  (always ≥ 0)
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
                terms.append(allocated - total_demand)
        return terms

    def get_split_decisions(self, solver: cp_model.CpSolver) -> list[SplitDecision]:
        """Extract the chosen spec × count pairs from a solved model."""
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
