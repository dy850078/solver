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
from pydantic import BaseModel, Field, field_validator, model_validator

from .models import (
    Baremetal,
    FailoverRule,
    GroupSelector,
    MaxPerBaremetalRule,
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
# Built-in per-role baseline demand — the fallback when a role has no explicit
# vm_specs/spec_by_role assignment.
# ---------------------------------------------------------------------------

_ROLE_BASELINE: dict[str, Resources] = {
    NodeRole.MASTER.value:  Resources(cpu_cores=8,  memory_mib=32_000, storage_gb=200),
    NodeRole.LEARNER.value: Resources(cpu_cores=8,  memory_mib=32_000, storage_gb=200),
    NodeRole.WORKER.value:  Resources(cpu_cores=16, memory_mib=64_000, storage_gb=400),
    NodeRole.INFRA.value:   Resources(cpu_cores=4,  memory_mib=16_000, storage_gb=100),
    NodeRole.L4LB.value:    Resources(cpu_cores=4,  memory_mib=16_000, storage_gb=200),
    NodeRole.BASTION.value: Resources(cpu_cores=2,  memory_mib=8_000,  storage_gb=50),
}

_DEFAULT_BM_CAPACITY = Resources(cpu_cores=64, memory_mib=256_000, storage_gb=2000)


# ---------------------------------------------------------------------------
# Input / output models
# ---------------------------------------------------------------------------

class BmProfile(BaseModel):
    """A fixed baremetal spec. ``count`` omitted → elastic (sized by tightness).

    ``roles``: which node roles may use these baremetals (a dedicated pool).
    Empty = usable by all roles. When ANY profile sets ``roles``, candidate
    assignment becomes pool-based (a VM may only land on BMs whose pool serves
    its role); otherwise every VM may use any baremetal.
    """
    name: str
    capacity: Resources
    count: int | None = None
    roles: list[str] = Field(default_factory=list)

    @field_validator("count")
    @classmethod
    def _count_positive(cls, v: int | None) -> int | None:
        if v is not None and v < 1:
            raise ValueError("bm_profile count must be >= 1 when given")
        return v

    @field_validator("roles")
    @classmethod
    def _validate_roles(cls, v: list[str]) -> list[str]:
        valid = {r.value for r in NodeRole}
        bad = [r for r in v if r not in valid]
        if bad:
            raise ValueError(f"bm_profile roles {bad} invalid; valid: {sorted(valid)}")
        return v


class GenerateRequest(BaseModel):
    """High-level knobs for generating a PlacementRequest. All have defaults."""
    seed: int | None = None
    target: Literal["solve"] = "solve"
    verify: bool = True

    # Cluster / VM
    clusters: int = 1
    roles: dict[str, int] = Field(default_factory=lambda: {"master": 3, "worker": 3, "infra": 2})
    # value: a single ip_type string, or a weighted distribution {ip_type: weight}
    ip_type_by_role: dict[str, str | dict[str, float]] = Field(default_factory=dict)
    # Named VM specs (a reusable catalog) and which spec each role uses.
    # spec_by_role key is "<role>" or "<role>:<ip_type>" (the latter wins).
    vm_specs: dict[str, Resources] = Field(default_factory=dict)
    spec_by_role: dict[str, str] = Field(default_factory=dict)

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
    # Per-role cap: at most N VMs of (each cluster, role's ip_type, role) on one
    # baremetal. Expanded into one MaxPerBaremetalRule per cluster.
    max_per_bm_by_role: dict[str, int] = Field(default_factory=dict)

    # Misc
    tightness: float = 0.7
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

    @field_validator("max_per_bm_by_role")
    @classmethod
    def _validate_max_per_bm_by_role(cls, v: dict[str, int]) -> dict[str, int]:
        valid = {r.value for r in NodeRole}
        bad = [k for k in v if k not in valid]
        if bad:
            raise ValueError(f"max_per_bm_by_role has unknown role(s) {bad}; valid: {sorted(valid)}")
        bad_vals = {k: n for k, n in v.items() if n < 1}
        if bad_vals:
            raise ValueError(f"max_per_bm_by_role values must be >= 1; got {bad_vals}")
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

    @model_validator(mode="after")
    def _validate_spec_assignment(self) -> GenerateRequest:
        bad = sorted({v for v in self.spec_by_role.values() if v not in self.vm_specs})
        if bad:
            raise ValueError(
                f"spec_by_role references unknown vm_specs {bad}; "
                f"defined specs: {sorted(self.vm_specs)}"
            )
        return self


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

    def _demand_for(self, role: str, ip_type: str) -> Resources:
        # 1. named spec assigned to this (role, ip_type) or role
        sa = self.req.spec_by_role
        if sa:
            name = sa.get(f"{role}:{ip_type}") if ip_type else None
            name = name or sa.get(role)
            if name and name in self.req.vm_specs:
                return self.req.vm_specs[name]
        # 2. built-in per-role baseline
        return _ROLE_BASELINE.get(role, _ROLE_BASELINE[NodeRole.WORKER.value])

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
                for n in range(1, count + 1):
                    ip_type = self._resolve_ip_type(role)
                    vms.append(VM(
                        id=f"{cluster_id}-{role}-{n}",
                        hostname=f"{role}-{n}.{cluster_id}",
                        demand=self._demand_for(role, ip_type),
                        node_role=NodeRole(role),
                        ip_type=ip_type,
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

    @staticmethod
    def _covers(have: Resources, need: Resources) -> bool:
        return (have.cpu_cores >= need.cpu_cores and have.memory_mib >= need.memory_mib
                and have.storage_gb >= need.storage_gb and have.gpu_count >= need.gpu_count)

    @staticmethod
    def _required(demand: Resources, tightness: float) -> Resources:
        return Resources(
            cpu_cores=math.ceil(demand.cpu_cores / tightness),
            memory_mib=math.ceil(demand.memory_mib / tightness),
            storage_gb=math.ceil(demand.storage_gb / tightness),
            gpu_count=math.ceil(demand.gpu_count / tightness),
        )

    def _build_baremetals(self, vms: list[VM], racks: list[Topology]) -> list[Baremetal]:
        req = self.req
        # Pool mode: any profile dedicates itself to specific roles.
        self._pool_mode = any(p.roles for p in req.bm_profiles)
        # Each entry: (capacity, frozenset(roles))  — empty roles = serves all.
        specs: list[tuple[Resources, frozenset[str]]] = []

        for p in req.bm_profiles:
            if p.count is not None:
                specs.extend([(p.capacity, frozenset(p.roles))] * int(p.count))

        elastic = [p for p in req.bm_profiles if p.count is None]
        num_ags = len({t.ag for t in racks})
        min_pool = max(req.target_spread.values(), default=1) if req.anti_affinity else 1
        added = 0

        def serves(roles: frozenset[str], target: frozenset[str]) -> bool:
            # A BM serves the target role-set if it's a shared pool (empty) or
            # its roles overlap the target.
            return not roles or not target or bool(roles & target)

        for p in elastic:
            served = frozenset(p.roles)  # empty = all
            # Demand this profile must help cover.
            demand = Resources()
            for vm in vms:
                if not served or vm.node_role.value in served:
                    demand = demand + vm.demand
            need = self._required(demand, req.tightness)
            have = Resources()
            for cap, roles in specs:
                if serves(roles, served):
                    have = have + cap
            copies = 0
            guard = 0
            while (not self._covers(have, need) or copies < (min_pool if self._pool_mode else num_ags)) and guard < 100_000:
                specs.append((p.capacity, served))
                have = have + p.capacity
                copies += 1
                guard += 1
            added += copies
        if elastic:
            self.diag["elastic_added"] = added

        # Spread each pool's BMs across racks independently (round-robin), so
        # every pool spans AGs evenly — a shared global index would let one
        # pool miss AGs and break anti-affinity spread.
        pools: dict[frozenset[str], list[Resources]] = {}
        for cap, roles in specs:
            pools.setdefault(roles, []).append(cap)

        self._bm_pool_roles: dict[str, frozenset[str]] = {}
        bms: list[Baremetal] = []
        idx = 0
        for roles, caps in pools.items():
            for j, cap in enumerate(caps):
                topo = racks[j % len(racks)]
                idx += 1
                bm_id = f"bm-{idx:03d}"
                self._bm_pool_roles[bm_id] = roles
                bms.append(Baremetal(
                    id=bm_id,
                    hostname=f"bare-{idx:03d}.{topo.rack}.{topo.site}",
                    total_capacity=cap,
                    used_capacity=Resources(),
                    topology=topo,
                ))
        return bms

    # -- candidates ---------------------------------------------------------

    def _assign_candidates(self, vms: list[VM], bms: list[Baremetal]) -> None:
        """Set each VM's candidate_baremetals.

        Pool mode (any bm_profile sets ``roles``): a VM may only land on BMs
        whose pool serves its role. Otherwise every VM may use any baremetal.
        """
        all_ids = [bm.id for bm in bms]

        if getattr(self, "_pool_mode", False):
            self.diag["candidate_mode"] = "by_role_pool"
            for vm in vms:
                role = vm.node_role.value
                pool = [bm.id for bm in bms
                        if not self._bm_pool_roles[bm.id] or role in self._bm_pool_roles[bm.id]]
                if not pool:
                    raise HTTPException(
                        status_code=400,
                        detail=f"no baremetal pool serves role '{role}'; "
                               f"add a bm_profile whose roles include it (or leave roles empty for a shared pool)",
                    )
                vm.candidate_baremetals = pool
            return

        self.diag["candidate_mode"] = "all"
        for vm in vms:
            vm.candidate_baremetals = list(all_ids)

    # -- constructive placement (ground truth) ------------------------------

    def _place(self, vms: list[VM], bms: list[Baremetal]) -> list[PlacementAssignment]:
        bm_by_id = {bm.id: bm for bm in bms}
        remaining = {bm.id: bm.total_capacity for bm in bms}
        # per-BM, per group counts (for max_per_bm_by_role)
        group_on_bm: dict[tuple[str, str], int] = {}
        caps = self.req.max_per_bm_by_role

        assignments: list[PlacementAssignment] = []

        # Group VMs by the solver's auto-AA key (cluster, ip_type, role).
        groups: dict[tuple[str, str, str], list[VM]] = {}
        for vm in vms:
            key = (vm.cluster_id, vm.ip_type, vm.node_role.value)
            groups.setdefault(key, []).append(vm)

        def try_place_on(vm: VM, bm_id: str, group_key: str, cap_limit: int | None) -> bool:
            cap = remaining[bm_id]
            if not vm.demand.fits_in(cap):
                return False
            if cap_limit is not None and group_on_bm.get((bm_id, group_key), 0) >= cap_limit:
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
            role_cap = caps.get(role)
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
                        if try_place_on(vm, bm_id, group_key, role_cap):
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
                        if try_place_on(vm, bm_id, group_key, role_cap):
                            placed = True
                            break
                if not placed:
                    self.diag.setdefault("unplaced_ground_truth", []).append(vm.id)

        return assignments

    # -- rules / config -----------------------------------------------------

    def _build_failover_rules(self) -> list[FailoverRule]:
        if not self.req.failover:
            return []
        # Require both roles to exist, else the backup selector resolves empty.
        if self.req.roles.get("master", 0) < 1 or self.req.roles.get("learner", 0) < 1:
            self.diag["failover_skipped"] = "needs >=1 master and >=1 learner per cluster"
            return []
        # One rule per cluster so masters are backed by learners of the SAME
        # cluster (mirrors auto anti-affinity keying on cluster_id).
        rules: list[FailoverRule] = []
        for c in range(1, self.req.clusters + 1):
            cid = f"cluster-{c}"
            rules.append(FailoverRule(
                rule_id=f"auto-failover-{cid}",
                primary=GroupSelector(cluster_id=cid, node_role=NodeRole.MASTER),
                backup=GroupSelector(cluster_id=cid, node_role=NodeRole.LEARNER),
                fault_domain="ag",
            ))
        return rules

    def _build_max_per_bm_rules(self) -> list[MaxPerBaremetalRule]:
        """Per-role cap → one MaxPerBaremetalRule per cluster, scoped to the
        role (and its declared ip_type when it's a single string)."""
        rules: list[MaxPerBaremetalRule] = []
        for role, cap in self.req.max_per_bm_by_role.items():
            if self.req.roles.get(role, 0) < 1:
                continue
            ip = self.req.ip_type_by_role.get(role)
            ip = ip if isinstance(ip, str) and ip else None
            for c in range(1, self.req.clusters + 1):
                cid = f"cluster-{c}"
                rules.append(MaxPerBaremetalRule(
                    group_id=f"maxbm/{cid}/{ip or '*'}/{role}",
                    selector=GroupSelector(cluster_id=cid, ip_type=ip, node_role=NodeRole(role)),
                    max_per_bm=cap,
                ))
        return rules

    def _build_config(self) -> SolverConfig:
        req = self.req
        cfg: dict[str, Any] = {
            "auto_generate_anti_affinity": req.anti_affinity,
            "target_spread": dict(req.target_spread),
        }
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
            max_per_bm_rules=self._build_max_per_bm_rules(),
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
