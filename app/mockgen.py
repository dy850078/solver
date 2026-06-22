"""
Mock Request Generator.

Programmatically builds a complete, solver-ready ``PlacementRequest`` from a
handful of high-level knobs, so users can spin up realistic placement
scenarios without hand-authoring fixtures.

v1 scope: greenfield (empty baremetals, ``used_capacity = 0``) with
constructive feasibility. The generator lays down a valid placement greedily
(the "ground truth"), then optionally re-solves the produced request with the
real solver to *prove* feasibility rather than relying on hand-derived
invariants.

See docs/mock-request-generator.md for the full design.
"""

from __future__ import annotations

import math
import random
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from .models import (
    Baremetal,
    FailoverRule,
    GroupSelector,
    NodeRole,
    PlacementAssignment,
    PlacementRequest,
    Resources,
    SolverConfig,
    Topology,
    VM,
)
from .solver import VMPlacementSolver

router = APIRouter(prefix="/api/mock", tags=["mock"])


# ---------------------------------------------------------------------------
# Demand profiles: per-role baseline, scaled by vm_size_profile
# ---------------------------------------------------------------------------

# Baseline demand per role (medium profile, scale 1.0).
_ROLE_BASELINE: dict[str, Resources] = {
    NodeRole.MASTER.value:  Resources(cpu_cores=8,  memory_mib=32_000, storage_gb=200),
    NodeRole.LEARNER.value: Resources(cpu_cores=8,  memory_mib=32_000, storage_gb=200),
    NodeRole.WORKER.value:  Resources(cpu_cores=16, memory_mib=64_000, storage_gb=400),
    NodeRole.INFRA.value:   Resources(cpu_cores=4,  memory_mib=16_000, storage_gb=100),
    NodeRole.L4LB.value:    Resources(cpu_cores=4,  memory_mib=16_000, storage_gb=200),
    NodeRole.BASTION.value: Resources(cpu_cores=2,  memory_mib=8_000,  storage_gb=50),
}

_SIZE_SCALE: dict[str, float] = {"small": 0.5, "medium": 1.0, "large": 2.0}

_DEFAULT_BM_CAPACITY = Resources(cpu_cores=64, memory_mib=256_000, storage_gb=2000)


def _scaled(base: Resources, scale: float) -> Resources:
    return Resources(
        cpu_cores=max(1, int(base.cpu_cores * scale)),
        memory_mib=max(1, int(base.memory_mib * scale)),
        storage_gb=max(1, int(base.storage_gb * scale)),
        gpu_count=int(base.gpu_count * scale),
    )


# ---------------------------------------------------------------------------
# Input / output models
# ---------------------------------------------------------------------------

class BmProfile(BaseModel):
    """A fixed baremetal spec. ``count`` omitted → elastic (sized by tightness)."""
    name: str
    capacity: Resources
    count: int | None = None

    @field_validator("count")
    @classmethod
    def _count_positive(cls, v: int | None) -> int | None:
        if v is not None and v < 1:
            raise ValueError("bm_profile count must be >= 1 when given")
        return v


class GenerateRequest(BaseModel):
    """High-level knobs for generating a PlacementRequest. All have defaults."""
    seed: int | None = None
    target: Literal["solve"] = "solve"
    verify: bool = True

    # Cluster / VM
    clusters: int = 1
    roles: dict[str, int] = Field(default_factory=lambda: {"master": 3, "worker": 3, "infra": 2})
    vm_size_profile: Literal["small", "medium", "large"] = "medium"
    role_demands: dict[str, Resources] | None = None
    # value: a single ip_type string, or a weighted distribution {ip_type: weight}
    ip_type_by_role: dict[str, str | dict[str, float]] = Field(default_factory=dict)

    # Baremetal
    bm_profiles: list[BmProfile] = Field(
        default_factory=lambda: [BmProfile(name="standard", capacity=_DEFAULT_BM_CAPACITY)]
    )

    # Topology
    sites: int = 1
    phases: int = 1
    datacenters: int = 1
    rooms: int = 1
    racks: int = 4
    ags: int = 3

    # Rules
    anti_affinity: bool = True
    target_spread: dict[str, int] = Field(default_factory=lambda: {"ag": 3})
    failover: bool = False
    max_per_bm: int | None = None

    # Misc
    tightness: float = 0.7
    candidate_strategy: Literal["all", "same_site", "same_room", "topology_affinity"] = "same_site"
    config_overrides: dict[str, Any] = Field(default_factory=dict)

    @field_validator("roles")
    @classmethod
    def _validate_roles(cls, v: dict[str, int]) -> dict[str, int]:
        valid = {r.value for r in NodeRole}
        bad = [k for k in v if k not in valid]
        if bad:
            raise ValueError(f"unknown role(s) {bad}; valid: {sorted(valid)}")
        if any(n < 0 for n in v.values()):
            raise ValueError("role counts must be >= 0")
        return v

    @field_validator("bm_profiles")
    @classmethod
    def _validate_profiles(cls, v: list[BmProfile]) -> list[BmProfile]:
        if not v:
            raise ValueError("bm_profiles must contain at least one profile")
        return v

    @field_validator("tightness")
    @classmethod
    def _validate_tightness(cls, v: float) -> float:
        if not 0.0 < v <= 1.0:
            raise ValueError("tightness must be in (0, 1]")
        return v


class GenerateResponse(BaseModel):
    request: PlacementRequest
    ground_truth: list[PlacementAssignment] = Field(default_factory=list)
    feasibility: str = "unverified"   # "verified" | "unverified" | "infeasible"
    diagnostics: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

class _Generator:
    def __init__(self, req: GenerateRequest):
        self.req = req
        self.rng = random.Random(req.seed)
        self.diag: dict[str, Any] = {}

    # -- topology -----------------------------------------------------------

    def _build_racks(self) -> list[Topology]:
        req = self.req
        ags = max(1, req.ags)
        racks = max(1, req.racks)

        # Auto-bump so infra can actually satisfy the requested spread targets.
        ag_target = req.target_spread.get("ag", 0)
        if ags < ag_target:
            self.diag.setdefault("auto_bumped", {})["ags"] = {"from": ags, "to": ag_target}
            ags = ag_target
        rack_target = req.target_spread.get("rack", 0)
        if racks < rack_target:
            self.diag.setdefault("auto_bumped", {})["racks"] = {"from": racks, "to": rack_target}
            racks = rack_target

        topos: list[Topology] = []
        for r in range(racks):
            topos.append(Topology(
                site=f"site-{r % max(1, req.sites) + 1}",
                phase=f"p{r % max(1, req.phases) + 1}",
                datacenter=f"dc-{r % max(1, req.datacenters) + 1}",
                room=f"room-{r % max(1, req.rooms) + 1}",
                rack=f"rack-{r + 1}",
                ag=f"ag-{r % ags + 1}",
            ))
        return topos

    # -- VMs ----------------------------------------------------------------

    def _demand_for(self, role: str) -> Resources:
        if self.req.role_demands and role in self.req.role_demands:
            return self.req.role_demands[role]
        base = _ROLE_BASELINE.get(role, _ROLE_BASELINE[NodeRole.WORKER.value])
        return _scaled(base, _SIZE_SCALE[self.req.vm_size_profile])

    def _resolve_ip_type(self, role: str) -> str:
        spec = self.req.ip_type_by_role.get(role)
        if spec is None:
            return ""
        if isinstance(spec, str):
            return spec
        # weighted distribution
        choices = list(spec.keys())
        weights = list(spec.values())
        return self.rng.choices(choices, weights=weights, k=1)[0]

    def _build_vms(self) -> list[VM]:
        req = self.req
        vms: list[VM] = []
        for c in range(1, req.clusters + 1):
            cluster_id = f"cluster-{c}"
            for role, count in req.roles.items():
                demand = self._demand_for(role)
                for n in range(1, count + 1):
                    vm_id = f"{cluster_id}-{role}-{n}"
                    vms.append(VM(
                        id=vm_id,
                        hostname=f"{role}-{n}.{cluster_id}",
                        demand=demand,
                        node_role=NodeRole(role),
                        ip_type=self._resolve_ip_type(role),
                        cluster_id=cluster_id,
                    ))
        return vms

    def _validate_ip_for_anti_affinity(self, vms: list[VM]) -> None:
        """auto-AA groups by (cluster, ip_type, role); empty ip_type is silently
        dropped by the solver. Reject up front so rules can't silently no-op."""
        if not self.req.anti_affinity:
            return
        counts: dict[tuple[str, str], int] = {}
        for vm in vms:
            key = (vm.cluster_id, vm.node_role.value)
            counts[key] = counts.get(key, 0) + 1
        offending = sorted({
            role for (_, role), n in counts.items()
            if n >= 2 and not self.req.ip_type_by_role.get(role)
        })
        if offending:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"anti_affinity=true but ip_type_by_role missing for role(s) "
                    f"{offending}. Roles with >=2 VMs need an explicit ip_type, "
                    f"otherwise the solver's auto anti-affinity silently skips them."
                ),
            )

    # -- baremetals ---------------------------------------------------------

    def _build_baremetals(self, vms: list[VM], racks: list[Topology]) -> list[Baremetal]:
        req = self.req
        specs: list[Resources] = []  # one capacity per BM instance

        fixed = [p for p in req.bm_profiles if p.count is not None]
        elastic = [p for p in req.bm_profiles if p.count is None]

        for p in fixed:
            specs.extend([p.capacity] * int(p.count))

        if elastic:
            # Size elastic fleet so total capacity covers demand at the target
            # tightness (demand/capacity), with at least one BM per AG.
            total_demand = Resources()
            for vm in vms:
                total_demand = total_demand + vm.demand
            required = Resources(
                cpu_cores=math.ceil(total_demand.cpu_cores / req.tightness),
                memory_mib=math.ceil(total_demand.memory_mib / req.tightness),
                storage_gb=math.ceil(total_demand.storage_gb / req.tightness),
                gpu_count=math.ceil(total_demand.gpu_count / req.tightness),
            )

            def covered(have: Resources) -> bool:
                return (required.cpu_cores <= have.cpu_cores
                        and required.memory_mib <= have.memory_mib
                        and required.storage_gb <= have.storage_gb
                        and required.gpu_count <= have.gpu_count)

            have = Resources()
            for s in specs:
                have = have + s
            num_ags = len({t.ag for t in racks})
            i = 0
            guard = 0
            max_iter = 100_000
            while (not covered(have) or len(specs) < num_ags) and guard < max_iter:
                cap = elastic[i % len(elastic)].capacity
                specs.append(cap)
                have = have + cap
                i += 1
                guard += 1
            self.diag["elastic_added"] = i

        # Spread BM instances evenly across racks (round-robin).
        bms: list[Baremetal] = []
        for idx, cap in enumerate(specs):
            topo = racks[idx % len(racks)]
            bms.append(Baremetal(
                id=f"bm-{idx + 1:03d}",
                hostname=f"bare-{idx + 1:03d}.{topo.rack}.{topo.site}",
                total_capacity=cap,
                used_capacity=Resources(),
                topology=topo,
            ))
        return bms

    # -- candidates ---------------------------------------------------------

    def _assign_candidates(self, vms: list[VM], bms: list[Baremetal]) -> None:
        """Set each VM's candidate_baremetals according to candidate_strategy.

        For scoped strategies each cluster gets a deterministic home scope and
        its VMs may only land on BMs inside that scope.
        """
        strategy = self.req.candidate_strategy
        all_ids = [bm.id for bm in bms]

        if strategy == "all":
            for vm in vms:
                vm.candidate_baremetals = list(all_ids)
            return

        # Map cluster_id -> scope key, then BMs grouped by scope.
        def scope_key(topo: Topology) -> str:
            if strategy == "same_site":
                return topo.site
            return topo.room  # same_room / topology_affinity

        by_scope: dict[str, list[str]] = {}
        for bm in bms:
            by_scope.setdefault(scope_key(bm.topology), []).append(bm.id)
        scopes = sorted(by_scope.keys())

        cluster_ids = sorted({vm.cluster_id for vm in vms})
        cluster_scope = {cid: scopes[i % len(scopes)] for i, cid in enumerate(cluster_ids)}
        for vm in vms:
            candidates = by_scope[cluster_scope[vm.cluster_id]]
            vm.candidate_baremetals = list(candidates)

    # -- constructive placement (ground truth) ------------------------------

    def _place(self, vms: list[VM], bms: list[Baremetal]) -> list[PlacementAssignment]:
        bm_by_id = {bm.id: bm for bm in bms}
        remaining = {bm.id: bm.total_capacity for bm in bms}
        # per-BM, per auto-AA group counts (for max_per_bm)
        group_on_bm: dict[tuple[str, str], int] = {}
        max_per_bm = self.req.max_per_bm

        assignments: list[PlacementAssignment] = []

        # Group VMs by the solver's auto-AA key (cluster, ip_type, role).
        groups: dict[tuple[str, str, str], list[VM]] = {}
        for vm in vms:
            key = (vm.cluster_id, vm.ip_type, vm.node_role.value)
            groups.setdefault(key, []).append(vm)

        def try_place_on(vm: VM, bm_id: str, group_key: str) -> bool:
            cap = remaining[bm_id]
            if not vm.demand.fits_in(cap):
                return False
            if max_per_bm is not None and group_on_bm.get((bm_id, group_key), 0) >= max_per_bm:
                return False
            remaining[bm_id] = cap - vm.demand
            group_on_bm[(bm_id, group_key)] = group_on_bm.get((bm_id, group_key), 0) + 1
            assignments.append(PlacementAssignment(
                vm_id=vm.id, vm_hostname=vm.hostname,
                baremetal_id=bm_id, bm_hostname=bm_by_id[bm_id].hostname,
                ag=bm_by_id[bm_id].topology.ag,
            ))
            return True

        for (cluster_id, ip_type, role), members in groups.items():
            group_key = f"{cluster_id}/{ip_type}/{role}"
            # Candidate BMs shared by the group (VMs in a group share candidates).
            cand_ids = members[0].candidate_baremetals
            cand_ags = sorted({bm_by_id[i].topology.ag for i in cand_ids})
            n_buckets = max(1, len(cand_ags))
            spread = ip_type and len(members) >= 2 and self.req.anti_affinity
            cap_per_ag = math.ceil(len(members) / n_buckets) if spread else len(members)

            per_ag_count: dict[str, int] = {ag: 0 for ag in cand_ags}
            # BMs grouped by AG, preferring most free capacity first each pick.
            ag_bms: dict[str, list[str]] = {ag: [] for ag in cand_ags}
            for i in cand_ids:
                ag_bms[bm_by_id[i].topology.ag].append(i)

            ag_cursor = 0
            for vm in members:
                placed = False
                # Try AGs round-robin, honoring the per-AG cap for spreading.
                for off in range(len(cand_ags)):
                    ag = cand_ags[(ag_cursor + off) % len(cand_ags)]
                    if per_ag_count[ag] >= cap_per_ag:
                        continue
                    for bm_id in sorted(ag_bms[ag],
                                        key=lambda b: remaining[b].cpu_cores, reverse=True):
                        if try_place_on(vm, bm_id, group_key):
                            per_ag_count[ag] += 1
                            ag_cursor = (cand_ags.index(ag) + 1) % len(cand_ags)
                            placed = True
                            break
                    if placed:
                        break
                if not placed:
                    # Fall back: any candidate BM with capacity (cap may be relaxed).
                    for bm_id in sorted(cand_ids,
                                        key=lambda b: remaining[b].cpu_cores, reverse=True):
                        if try_place_on(vm, bm_id, group_key):
                            placed = True
                            break
                if not placed:
                    self.diag.setdefault("unplaced_ground_truth", []).append(vm.id)

        return assignments

    # -- rules / config -----------------------------------------------------

    def _build_failover_rules(self) -> list[FailoverRule]:
        if not self.req.failover:
            return []
        # master (primary) backed by learner across AG fault domain.
        return [FailoverRule(
            rule_id="auto-master-learner",
            primary=GroupSelector(node_role=NodeRole.MASTER),
            backup=GroupSelector(node_role=NodeRole.LEARNER),
            fault_domain="ag",
        )]

    def _build_config(self) -> SolverConfig:
        req = self.req
        cfg: dict[str, Any] = {
            "auto_generate_anti_affinity": req.anti_affinity,
            "target_spread": dict(req.target_spread),
        }
        if req.max_per_bm is not None:
            cfg["auto_generate_max_per_bm"] = True
            cfg["default_max_per_bm"] = req.max_per_bm
        cfg.update(req.config_overrides)
        return SolverConfig(**cfg)

    # -- orchestration ------------------------------------------------------

    def generate(self) -> GenerateResponse:
        req = self.req
        racks = self._build_racks()
        vms = self._build_vms()
        self._validate_ip_for_anti_affinity(vms)
        bms = self._build_baremetals(vms, racks)
        self._assign_candidates(vms, bms)
        ground_truth = self._place(vms, bms)

        placement = PlacementRequest(
            vms=vms,
            baremetals=bms,
            failover_rules=self._build_failover_rules(),
            config=self._build_config(),
        )

        self.diag["num_vms"] = len(vms)
        self.diag["num_baremetals"] = len(bms)
        self.diag["num_ags"] = len({t.ag for t in racks})

        feasibility = "unverified"
        if req.verify:
            result = VMPlacementSolver(placement).solve()
            self.diag["solver_status"] = result.solver_status
            self.diag["solver_unplaced"] = result.unplaced_vms
            feasibility = "verified" if result.success else "infeasible"

        return GenerateResponse(
            request=placement,
            ground_truth=ground_truth,
            feasibility=feasibility,
            diagnostics=self.diag,
        )


def generate_mock_request(req: GenerateRequest) -> GenerateResponse:
    return _Generator(req).generate()


@router.post("/generate", response_model=GenerateResponse)
def generate(req: GenerateRequest) -> GenerateResponse:
    """Generate a complete, solver-ready PlacementRequest from high-level knobs."""
    return generate_mock_request(req)
