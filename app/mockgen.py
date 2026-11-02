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
from dataclasses import dataclass, field
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from .models import (
    Baremetal,
    ExclusiveBaremetalRule,
    FailoverRule,
    GroupSelector,
    MaxPerBaremetalRule,
    NodeRole,
    PlacementAssignment,
    PlacementRequest,
    PlacementResult,
    Resources,
    SolverConfig,
    Topology,
    VM,
    res_get,
    resource_dims,
    validate_role,
)
from .solver import VMPlacementSolver

router = APIRouter(prefix="/api/mock", tags=["mock"])

# Hard ceiling for elastic fleet sizing. Any realistic mock scenario is a few
# hundred BMs at most; hitting this means capacity/demand are pathologically
# mismatched, and we fail loudly instead of returning a runaway fleet that
# times out every consumer downstream (browser, ingress, solver).
_MAX_ELASTIC_BMS = 5_000

# Escalation rounds for elastic sizing: the analytic lower bounds (capacity,
# spread, headcount) are usually within 0–1 BMs of the true minimum; each
# round adds one BM to the implicated pool and re-verifies. Ten rounds of
# slack means "the bounds were off by 10" — at that point the input is wrong,
# not the sizing.
_MAX_ESCALATIONS = 10


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


@dataclass
class _CapUnit:
    """One max-per-BM cap and the set of roles that share it — the unit a
    MaxPerBaremetalRule is emitted for, and the unit the fleet sizing and the
    greedy ground truth count against.

    An untagged node group is a single-role unit keyed by (scope, role,
    ip_type). Groups sharing a ``no_colocate_group`` tag form ONE multi-role
    unit: the rule's selector is the union of their roles (ADR-016), so
    ``counts`` sums every member and ``ip_type`` is None when members differ.
    ``key`` mirrors the solver's fallback group id (roles joined with ``+``).
    """
    scope: Literal["cluster", "shared"]
    roles: frozenset[str]
    ip_type: str | None
    cap: int
    counts: dict[str, int] = field(default_factory=dict)  # per instance, per role

    @property
    def key(self) -> str:
        return f"{self.ip_type or '*'}/{'+'.join(sorted(self.roles))}"

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def selector_role(self) -> str | list[str]:
        # A single role keeps the string form so untagged output is
        # byte-identical to before; GroupSelector rejects an empty list.
        return next(iter(self.roles)) if len(self.roles) == 1 else sorted(self.roles)


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
        return [validate_role(r) for r in v]


class NodeGroup(BaseModel):
    """One demand group: `count` VMs of `role`, all sharing an ip_type and
    spec. The same role may appear in several groups (e.g. two worker pools
    with different specs, or a role split across ip_types) — something the
    role-keyed dict fields below cannot express. When node_groups is set it
    is the sole source of demand; the roles/ip_type_by_role/spec_by_role/
    max_per_bm_by_role dicts are ignored."""
    role: str
    count: int = Field(default=0, ge=0)
    ip_type: str = ""
    spec: str = ""                 # name in vm_specs; "" = built-in baseline
    max_per_bm: int | None = None
    # "shared": ONE group serving all clusters (cluster_id="shared"), e.g.
    # 5 clusters sharing 6 F5s. Default: each cluster gets its own copy.
    scope: Literal["cluster", "shared"] = "cluster"
    # Appliance semantics (C6/ADR-011): every VM of this group owns its BM
    # outright — nothing else lands there, not even a group sibling.
    exclusive: bool = False
    # Policy tag, orthogonal to `role` (ADR-016/ADR-017): groups of the same
    # scope sharing a tag are merged into ONE MaxPerBaremetalRule whose
    # selector lists all member roles, so e.g. control-plane and
    # control-plane-learner with max_per_bm=1 never share a BM. Members must
    # agree on max_per_bm and scope; a differing ip_type widens the selector
    # to "any ip_type". None = the group caps itself only (today's behaviour).
    no_colocate_group: str | None = None

    @field_validator("role")
    @classmethod
    def _valid_role(cls, v: str) -> str:
        return validate_role(v)

    @field_validator("no_colocate_group")
    @classmethod
    def _valid_tag(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @field_validator("max_per_bm")
    @classmethod
    def _valid_max(cls, v: int | None) -> int | None:
        if v is not None and v < 1:
            raise ValueError("max_per_bm must be >= 1")
        return v


class GenerateRequest(BaseModel):
    """High-level knobs for generating a PlacementRequest. All have defaults."""
    seed: int | None = None
    target: Literal["solve"] = "solve"
    verify: bool = True

    # Cluster / VM. node_groups (when set) is the source of demand; otherwise
    # the role-keyed dicts below are used (backward compatible).
    clusters: int = 1
    node_groups: list[NodeGroup] = Field(default_factory=list)
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
        for k in v:
            validate_role(k)
        if any(n < 0 for n in v.values()):
            raise ValueError("role counts must be >= 0")
        return v

    @field_validator("max_per_bm_by_role")
    @classmethod
    def _validate_max_per_bm_by_role(cls, v: dict[str, int]) -> dict[str, int]:
        for k in v:
            validate_role(k)
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
        gbad = sorted({g.spec for g in self.node_groups
                       if g.spec and g.spec not in self.vm_specs})
        if gbad:
            raise ValueError(
                f"node_groups reference unknown vm_specs {gbad}; "
                f"defined specs: {sorted(self.vm_specs)}"
            )
        return self


class GenerateResponse(BaseModel):
    request: PlacementRequest
    ground_truth: list[PlacementAssignment] = Field(default_factory=list)
    feasibility: str = "unverified"   # "verified" | "unverified" | "infeasible"
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    # The solver's own verification result (None when verify=False): the
    # placement the solver actually chose, with bm_used_count and timing.
    # `ground_truth` above is the generator's greedy layout, which only proves
    # feasibility; consumers that report "what the solver did" (compare
    # matrices, density/utilization) read this instead of re-solving.
    verified: PlacementResult | None = None


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

class _Generator:
    def __init__(self, req: GenerateRequest):
        self.req = req
        self.rng = random.Random(req.seed)
        self.diag: dict[str, Any] = {}
        # Max-per-BM cap units from node_groups (empty in the legacy dict
        # path): one per (scope, role, ip_type) for untagged groups, one per
        # (scope, tag) for no_colocate_group members. Split by scope because a
        # shared group's cap must NOT expand into per-cluster rules — its VMs
        # live under cluster_id="shared". `_unit_index` resolves a VM's
        # (scope, role, ip_type) to its unit for sizing and ground truth.
        self._units: list[_CapUnit] = []
        self._unit_index: dict[tuple[str, str, str], _CapUnit] = {}
        self._build_cap_units(req.node_groups)
        # Roles whose VMs occupy BMs alone (C6). Role-level is enough for the
        # generator: mixing an exclusive and a non-exclusive group of the same
        # role would be a contradiction we reject below.
        self._exclusive_roles: set[str] = {g.role for g in req.node_groups if g.exclusive}
        non_excl = {g.role for g in req.node_groups if not g.exclusive}
        both = sorted(self._exclusive_roles & non_excl)
        if both:
            raise HTTPException(
                status_code=400,
                detail=f"role(s) {both} appear in both exclusive and non-exclusive "
                       f"node groups — a role is either appliance-like or not",
            )

    def _build_cap_units(self, groups: list[NodeGroup]) -> None:
        """Validate no_colocate_group tags and build `_units` / `_unit_index`.

        Tag rules (each violation → 400, never a silent fix):
        - every tagged group needs an explicit max_per_bm (the tag is a cap
          shared across roles — with no cap there is nothing to share);
        - members of one tag agree on max_per_bm and on scope;
        - a role belongs to at most one tag, and once tagged, EVERY group of
          that role (same scope) carries the tag — the selector matches by
          role, so an untagged sibling would be swept into the union anyway.
        """
        # Pass 1: tag validation.
        by_tag: dict[tuple[str, str], list[NodeGroup]] = {}
        for g in groups:
            if g.no_colocate_group is None:
                continue
            if g.max_per_bm is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"node group role={g.role!r} carries no_colocate_group="
                           f"{g.no_colocate_group!r} but no max_per_bm; the tag is a "
                           f"per-BM cap shared across roles, so the cap must be given",
                )
            by_tag.setdefault((g.scope, g.no_colocate_group), []).append(g)
        # Scope mismatch shows up as the same tag under two scope keys.
        tags_seen: dict[str, str] = {}
        for (scope, tag), members in by_tag.items():
            if tag in tags_seen and tags_seen[tag] != scope:
                raise HTTPException(
                    status_code=400,
                    detail=f"no_colocate_group {tag!r} mixes scope={tags_seen[tag]!r} and "
                           f"scope={scope!r}; one rule can only select one cluster_id",
                )
            tags_seen[tag] = scope
            caps = sorted({g.max_per_bm for g in members})  # type: ignore[type-var]
            if len(caps) > 1:
                raise HTTPException(
                    status_code=400,
                    detail=f"no_colocate_group {tag!r} members disagree on max_per_bm "
                           f"{caps}; a merged rule has one cap — make them equal",
                )
        # Role ↔ tag must be a function (per scope), covering ALL groups of a role.
        tag_of_role: dict[tuple[str, str], str | None] = {}
        for g in groups:
            k = (g.scope, g.role)
            if k in tag_of_role and tag_of_role[k] != g.no_colocate_group:
                a, b = sorted([tag_of_role[k], g.no_colocate_group], key=lambda t: t or "")
                if a is None:
                    raise HTTPException(
                        status_code=400,
                        detail=f"role {g.role!r} is tagged no_colocate_group={b!r} in one "
                               f"node group but untagged in another; the rule selects by "
                               f"role, so tag every group of that role (or none)",
                    )
                raise HTTPException(
                    status_code=400,
                    detail=f"role {g.role!r} appears under two no_colocate_group tags "
                           f"{[a, b]}; a role belongs to at most one",
                )
            tag_of_role[k] = g.no_colocate_group

        # Pass 2: build units. Untagged: min wins on (scope, role, ip) collision.
        for g in groups:
            if g.max_per_bm is None or g.no_colocate_group is not None:
                continue
            k = (g.scope, g.role, g.ip_type)
            u = self._unit_index.get(k)
            if u is None:
                u = _CapUnit(scope=g.scope, roles=frozenset({g.role}),
                             ip_type=g.ip_type or None, cap=g.max_per_bm)
                self._units.append(u)
                self._unit_index[k] = u
            u.cap = min(u.cap, g.max_per_bm)
            u.counts[g.role] = u.counts.get(g.role, 0) + g.count
        # An uncapped group whose (scope, role, ip) is capped by a sibling is
        # still selected by that sibling's rule — its VMs count toward the cap.
        for g in groups:
            if g.max_per_bm is None and g.no_colocate_group is None:
                u = self._unit_index.get((g.scope, g.role, g.ip_type))
                if u is not None:
                    u.counts[g.role] = u.counts.get(g.role, 0) + g.count
        for (scope, _tag), members in by_tag.items():
            ips = {g.ip_type for g in members}
            u = _CapUnit(
                scope=scope,
                roles=frozenset(g.role for g in members),
                ip_type=(next(iter(ips)) or None) if len(ips) == 1 else None,
                cap=members[0].max_per_bm,  # type: ignore[arg-type]
            )
            for g in members:
                u.counts[g.role] = u.counts.get(g.role, 0) + g.count
                self._unit_index[(scope, g.role, g.ip_type)] = u
            self._units.append(u)

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
        if self.req.node_groups:
            return self._build_vms_from_groups()
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
                        node_role=role,
                        ip_type=ip_type,
                        cluster_id=cluster_id,
                    ))
        return vms

    def _build_vms_from_groups(self) -> list[VM]:
        """One VM stream per (cluster, node_group). Ids stay contiguous per
        (cluster, role) even when a role spans several groups. Shared-scope
        groups are built ONCE under cluster_id="shared" — that id is also the
        auto-rule grouping key, so a shared pool spreads/caps as one group
        across all clusters (ADR-011)."""
        req = self.req
        vms: list[VM] = []
        shared = [g for g in req.node_groups if g.scope == "shared"]
        seq_s: dict[str, int] = {}
        for g in shared:
            demand = req.vm_specs.get(g.spec) if g.spec else None
            if demand is None:
                demand = _ROLE_BASELINE.get(g.role, _ROLE_BASELINE[NodeRole.WORKER.value])
            for _ in range(g.count):
                n = seq_s.get(g.role, 0) + 1
                seq_s[g.role] = n
                vms.append(VM(
                    id=f"shared-{g.role}-{n}",
                    hostname=f"{g.role}-{n}.shared",
                    demand=demand,
                    node_role=g.role,
                    ip_type=g.ip_type,
                    cluster_id="shared",
                ))
        for c in range(1, req.clusters + 1):
            cluster_id = f"cluster-{c}"
            seq: dict[str, int] = {}
            for g in (g for g in req.node_groups if g.scope != "shared"):
                demand = req.vm_specs.get(g.spec) if g.spec else None
                if demand is None:
                    demand = _ROLE_BASELINE.get(g.role, _ROLE_BASELINE[NodeRole.WORKER.value])
                for _ in range(g.count):
                    n = seq.get(g.role, 0) + 1
                    seq[g.role] = n
                    vms.append(VM(
                        id=f"{cluster_id}-{g.role}-{n}",
                        hostname=f"{g.role}-{n}.{cluster_id}",
                        demand=demand,
                        node_role=g.role,
                        ip_type=g.ip_type,
                        cluster_id=cluster_id,
                    ))
        return vms

    def _validate_ip_for_anti_affinity(self, vms: list[VM]) -> None:
        """auto-AA groups by (cluster, ip_type, role); empty ip_type is silently
        dropped by the solver. Reject up front so rules can't silently no-op."""
        if not self.req.anti_affinity:
            return
        if self.req.node_groups:
            # A (role, ip_type) may be split over several groups — aggregate.
            agg: dict[tuple[str, str], int] = {}
            for g in self.req.node_groups:
                agg[(g.role, g.ip_type)] = agg.get((g.role, g.ip_type), 0) + g.count
            offending = sorted({role for (role, ip), n in agg.items() if n >= 2 and not ip})
            if offending:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"anti_affinity=true but node group(s) for role(s) {offending} "
                        f"have no ip_type and >=2 VMs per cluster; the solver's auto "
                        f"anti-affinity silently skips empty-ip_type groups. Give those "
                        f"groups an ip_type."
                    ),
                )
            return
        counts: dict[tuple[str, str], int] = {}
        for vm in vms:
            key = (vm.cluster_id, vm.node_role)
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
        return need.fits_in(have)

    @staticmethod
    def _required(demand: Resources, tightness: float) -> Resources:
        return Resources(
            cpu_cores=math.ceil(demand.cpu_cores / tightness),
            memory_mib=math.ceil(demand.memory_mib / tightness),
            storage_gb=math.ceil(demand.storage_gb / tightness),
            gpu={m: math.ceil(c / tightness) for m, c in demand.gpu.items()},
        )

    def _headcount_bounds(self) -> list[_CapUnit]:
        """Cap units whose max-per-BM cap implies a minimum number of distinct
        BMs: n VMs capped at m per BM need ceil(n/m) BMs regardless of how big
        each BM is (a headcount bound, not a capacity bound). The caller does
        the division per elastic profile, on the roles that profile serves.

        Counting conventions:
        - a multi-role unit (no_colocate_group) SUMS its members — the emitted
          rule's selector is the union, so members compete for the same slots;
        - across ip_types of one untagged role: separate units, max not sum —
          those rules are keyed per (cluster, ip_type, role), so two ip groups
          of the same role may share BMs;
        - across clusters: counts are per cluster and clusters may reuse the
          same BMs (each cluster's rule counts separately).
        """
        if self.req.node_groups:
            return [u for u in self._units if u.total]
        units: list[_CapUnit] = []
        for role, cap in self.req.max_per_bm_by_role.items():
            n = self.req.roles.get(role, 0)
            if n:
                units.append(_CapUnit(scope="cluster", roles=frozenset({role}),
                                      ip_type=None, cap=cap, counts={role: n}))
        return units

    def _build_baremetals(self, vms: list[VM], racks: list[Topology],
                          min_copies: dict[str, int] | None = None) -> list[Baremetal]:
        req = self.req
        # Pool mode: any profile dedicates itself to specific roles.
        self._pool_mode = any(p.roles for p in req.bm_profiles)

        # Exclusive roles must live in dedicated pools: a profile serving both
        # an exclusive and a normal role would offer capacity the sizing math
        # counts twice (a solo-locked BM contributes nothing to anyone else).
        if self._exclusive_roles:
            for prof in req.bm_profiles:
                served_roles = set(prof.roles)
                if not served_roles:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"exclusive node group(s) {sorted(self._exclusive_roles)} "
                            f"require dedicated bm_profile pools, but profile "
                            f"{prof.name!r} serves all roles — give every profile "
                            f"an explicit roles list"
                        ),
                    )
                mixed = served_roles & self._exclusive_roles and served_roles - self._exclusive_roles
                if mixed:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"bm_profile {prof.name!r} mixes exclusive role(s) "
                            f"{sorted(served_roles & self._exclusive_roles)} with "
                            f"non-exclusive {sorted(served_roles - self._exclusive_roles)}; "
                            f"exclusive roles need their own dedicated profile"
                        ),
                    )
        # Each entry: (capacity, frozenset(roles))  — empty roles = serves all.
        specs: list[tuple[Resources, frozenset[str]]] = []

        for p in req.bm_profiles:
            if p.count is not None:
                specs.extend([(p.capacity, frozenset(p.roles))] * int(p.count))

        elastic = [p for p in req.bm_profiles if p.count is None]
        num_ags = len({t.ag for t in racks})
        min_pool = max(req.target_spread.values(), default=1) if req.anti_affinity else 1
        bounds = self._headcount_bounds()
        self._elastic_copies: dict[str, int] = {}
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
                if not served or vm.node_role in served:
                    demand = demand + vm.demand
            need = self._required(demand, req.tightness)
            have = Resources()
            for cap, roles in specs:
                if serves(roles, served):
                    have = have + cap
            # Fail fast: a resource field where this profile has zero capacity
            # but residual demand remains can never be covered by adding more
            # copies — the loop below would spin until the guard and hand back
            # a runaway fleet. Name the exact fields instead.
            deficient = [
                d for d in resource_dims([p.capacity, need, have])
                if res_get(p.capacity, d) == 0 and res_get(need, d) > res_get(have, d)
            ]
            if deficient:
                needs = {d: res_get(need, d) - res_get(have, d) for d in deficient}
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"bm_profile {p.name!r} has zero capacity in {deficient} but "
                        f"the VMs it serves still demand {needs}; adding more copies "
                        f"can never cover this — fill in those capacity fields or "
                        f"drop the demand"
                    ),
                )
            # Headcount floor: for each cap unit this profile serves, the
            # max-per-BM bound needs ceil(n/m) distinct BMs; BMs already in
            # `specs` that serve those roles count toward it, this profile
            # must add the worst-case remainder. Every copy added below serves
            # all of this profile's roles, so the gap shrinks 1:1 with copies.
            # Only the SUBSET of a multi-role unit this pool serves is counted:
            # with master and learner in separate pools, each pool owes its
            # own members, not the union (the union bound would over-provision
            # the first pool and starve the second into escalation).
            head_gap = 0
            for unit in bounds:
                sub = unit.roles if not served else unit.roles & served
                if not sub:
                    continue
                need_bms = math.ceil(sum(unit.counts.get(r, 0) for r in sub) / unit.cap)
                existing = sum(1 for _, roles in specs if serves(roles, sub))
                head_gap = max(head_gap, need_bms - existing)
            # Pairwise packing floor (bin-packing L2 bound): a VM demanding
            # more than half this profile's capacity in some dimension can
            # never share a BM with another such VM (two >50% items overflow),
            # so the COUNT of these big items bounds the fleet from below —
            # summed across clusters, unlike the headcount bound. The capacity
            # bound misses this entirely (it treats VMs as divisible), which
            # at tightness 1.0 left a gap escalation's +1 could not climb.
            # Existing BMs are credited with an upper estimate of how many big
            # items each could host (cap // smallest big item), so this floor
            # errs low — escalation covers any remainder, never overshoot.
            pack_gap = 0
            for d in resource_dims([p.capacity, *(vm.demand for vm in vms)]):
                cap_d = res_get(p.capacity, d)
                if cap_d <= 0:
                    continue
                bigs = [res_get(vm.demand, d) for vm in vms
                        if (not served or vm.node_role in served)
                        and res_get(vm.demand, d) * 2 > cap_d]
                if not bigs:
                    continue
                slots = sum(res_get(cap_e, d) // min(bigs)
                            for cap_e, roles in specs if serves(roles, served))
                pack_gap = max(pack_gap, len(bigs) - slots)
            # Solo floor: a pool of exclusive roles needs one BM per VM —
            # occupancy is 1 by C6, so capacity math is irrelevant here.
            solo_gap = 0
            if served and served <= self._exclusive_roles:
                total = sum(1 for vm in vms if vm.node_role in served)
                existing = sum(1 for _, roles in specs if serves(roles, served))
                solo_gap = total - existing
            floor = max(min_pool if self._pool_mode else num_ags,
                        head_gap,
                        pack_gap,
                        solo_gap,
                        (min_copies or {}).get(p.name, 0))
            copies = 0
            while not self._covers(have, need) or copies < floor:
                if added + copies >= _MAX_ELASTIC_BMS:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"elastic sizing for bm_profile {p.name!r} exceeded "
                            f"{_MAX_ELASTIC_BMS} baremetals (need={need!r}, "
                            f"have={have!r}); profile capacity is far too small "
                            f"for the demand — check the capacity fields"
                        ),
                    )
                specs.append((p.capacity, served))
                have = have + p.capacity
                copies += 1
            self._elastic_copies[p.name] = copies
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
                role = vm.node_role
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
        # per-BM, per group counts (for the max-per-BM cap)
        group_on_bm: dict[tuple[str, str], int] = {}
        # C6 solo occupancy: exclusive VMs need an untouched BM and lock it.
        bm_occupants: dict[str, int] = {}
        bm_locked: set[str] = set()

        assignments: list[PlacementAssignment] = []

        # Group VMs by the solver's auto-AA key (cluster, ip_type, role).
        groups: dict[tuple[str, str, str], list[VM]] = {}
        for vm in vms:
            key = (vm.cluster_id, vm.ip_type, vm.node_role)
            groups.setdefault(key, []).append(vm)

        def try_place_on(vm: VM, bm_id: str, group_key: str, cap_limit: int | None,
                         solo: bool = False) -> bool:
            cap = remaining[bm_id]
            if bm_id in bm_locked:
                return False
            if solo and bm_occupants.get(bm_id, 0) > 0:
                return False
            if not vm.demand.fits_in(cap):
                return False
            if cap_limit is not None and group_on_bm.get((bm_id, group_key), 0) >= cap_limit:
                return False
            remaining[bm_id] = cap - vm.demand
            group_on_bm[(bm_id, group_key)] = group_on_bm.get((bm_id, group_key), 0) + 1
            bm_occupants[bm_id] = bm_occupants.get(bm_id, 0) + 1
            if solo:
                bm_locked.add(bm_id)
            assignments.append(PlacementAssignment(
                vm_id=vm.id, vm_hostname=vm.hostname,
                baremetal_id=bm_id, bm_hostname=bm_by_id[bm_id].hostname,
                ag=bm_by_id[bm_id].topology.ag,
            ))
            return True

        for (cluster_id, ip_type, role), members in groups.items():
            # The cap counter is keyed per cap UNIT, not per role: members of
            # a no_colocate_group share one counter per BM, so the ground
            # truth honours the merged rule the same way the solver will.
            unit = self._unit_for(role, ip_type, cluster_id)
            group_key = f"{cluster_id}/{unit.key}" if unit else f"{cluster_id}/{ip_type}/{role}"
            role_cap = unit.cap if unit else None
            solo = role in self._exclusive_roles
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
                        if try_place_on(vm, bm_id, group_key, role_cap, solo):
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
                        if try_place_on(vm, bm_id, group_key, role_cap, solo):
                            placed = True
                            break
                if not placed:
                    self.diag.setdefault("unplaced_ground_truth", []).append(vm.id)

        return assignments

    # -- rules / config -----------------------------------------------------

    def _role_counts(self) -> dict[str, int]:
        """Per-cluster VM count for each role, from whichever demand source is
        active: node_groups when set (a role may span several groups —
        aggregate; shared-scope groups are NOT per-cluster and don't count),
        else the legacy roles dict."""
        if self.req.node_groups:
            counts: dict[str, int] = {}
            for g in self.req.node_groups:
                if g.scope != "shared":
                    counts[g.role] = counts.get(g.role, 0) + g.count
            return counts
        return dict(self.req.roles)

    def _build_exclusive_rules(self) -> list[ExclusiveBaremetalRule]:
        """One C6 rule per exclusive (scope-instance, role, ip): shared groups
        get a single rule over cluster_id="shared"; cluster-scope exclusive
        groups get one per cluster (mirrors _build_max_per_bm_rules)."""
        rules: list[ExclusiveBaremetalRule] = []
        seen: set[tuple[str, str, str]] = set()
        for g in self.req.node_groups:
            if not g.exclusive or g.count < 1:
                continue
            cids = (["shared"] if g.scope == "shared"
                    else [f"cluster-{c}" for c in range(1, self.req.clusters + 1)])
            for cid in cids:
                key = (cid, g.role, g.ip_type)
                if key in seen:
                    continue
                seen.add(key)
                rules.append(ExclusiveBaremetalRule(
                    group_id=f"excl/{cid}/{g.ip_type or '*'}/{g.role}",
                    selector=GroupSelector(cluster_id=cid, ip_type=(g.ip_type or None),
                                           node_role=g.role),
                ))
        return rules

    def _build_failover_rules(self) -> list[FailoverRule]:
        if not self.req.failover:
            return []
        # Require both roles to exist, else the backup selector resolves empty.
        # Counts must come from the active demand source — reading req.roles
        # here while demand came from node_groups silently skipped the rule.
        counts = self._role_counts()
        if counts.get("master", 0) < 1 or counts.get("learner", 0) < 1:
            self.diag["failover_skipped"] = "needs >=1 master and >=1 learner per cluster"
            return []
        # One rule per cluster so masters are backed by learners of the SAME
        # cluster (mirrors auto anti-affinity keying on cluster_id).
        rules: list[FailoverRule] = []
        for c in range(1, self.req.clusters + 1):
            cid = f"cluster-{c}"
            rules.append(FailoverRule(
                rule_id=f"auto-failover-{cid}",
                primary=GroupSelector(cluster_id=cid, node_role=NodeRole.MASTER.value),
                backup=GroupSelector(cluster_id=cid, node_role=NodeRole.LEARNER.value),
                fault_domain="ag",
            ))
        return rules

    def _unit_for(self, role: str, ip_type: str, cluster_id: str) -> _CapUnit | None:
        """Cap unit governing a VM of (role, ip_type) in `cluster_id`, or None
        when it has no max-per-BM cap. The legacy dict path keys by role only
        (its rules carry the role's configured ip_type, VMs may be spread over
        a weighted distribution), so it is NOT routed through `_unit_index`."""
        if self.req.node_groups:
            scope = "shared" if cluster_id == "shared" else "cluster"
            return self._unit_index.get((scope, role, ip_type))
        cap = self.req.max_per_bm_by_role.get(role)
        if cap is None:
            return None
        return _CapUnit(scope="cluster", roles=frozenset({role}), ip_type=None, cap=cap)

    def _build_max_per_bm_rules(self) -> list[MaxPerBaremetalRule]:
        """One MaxPerBaremetalRule per cap unit per cluster instance (shared
        units: a single rule on cluster_id="shared"). A single-role unit keeps
        the string selector; a no_colocate_group unit lists its roles so the
        cap spans them (ADR-016). group_id = maxbm/{cid}/{ip|*}/{role[+role]}."""
        rules: list[MaxPerBaremetalRule] = []
        if self.req.node_groups:
            for u in self._units:
                cids = (["shared"] if u.scope == "shared"
                        else [f"cluster-{c}" for c in range(1, self.req.clusters + 1)])
                for cid in cids:
                    rules.append(MaxPerBaremetalRule(
                        group_id=f"maxbm/{cid}/{u.key}",
                        selector=GroupSelector(cluster_id=cid, ip_type=u.ip_type,
                                               node_role=u.selector_role()),
                        max_per_bm=u.cap,
                    ))
            return rules
        for role, cap in self.req.max_per_bm_by_role.items():
            if self.req.roles.get(role, 0) < 1:
                continue
            ip = self.req.ip_type_by_role.get(role)
            ip = ip if isinstance(ip, str) and ip else None
            for c in range(1, self.req.clusters + 1):
                cid = f"cluster-{c}"
                rules.append(MaxPerBaremetalRule(
                    group_id=f"maxbm/{cid}/{ip or '*'}/{role}",
                    selector=GroupSelector(cluster_id=cid, ip_type=ip, node_role=role),
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

    def _escalation_targets(self, result, vms: list[VM]) -> list[str]:
        """Elastic profile names implicated by the solver's infeasibility
        diagnostics — via the role(s) in a failing max-per-BM rule's group_id
        (``maxbm/{cluster}/{ip}/{role[+role...]}``, a no_colocate_group unit
        names every member) or the role of a VM with no eligible BM. Empty
        when nothing is attributable (caller falls back to all)."""
        diag = result.diagnostics or {}
        roles: set[str] = set()
        for key in ("infeasible_max_per_bm_rules", "infeasible_exclusive_rules"):
            for r in diag.get(key, []):
                tail = str(r.get("group_id", "")).rsplit("/", 1)[-1]
                roles.update(x for x in tail.split("+") if x)
        role_by_vm = {vm.id: vm.node_role for vm in vms}
        for vm_id in diag.get("vms_with_no_eligible_bm", []):
            if vm_id in role_by_vm:
                roles.add(role_by_vm[vm_id])
        if not roles:
            return []
        return [p.name for p in self.req.bm_profiles
                if p.count is None and (not p.roles or roles & set(p.roles))]

    # -- orchestration ------------------------------------------------------

    def generate(self) -> GenerateResponse:
        req = self.req
        racks = self._build_racks()
        vms = self._build_vms()
        self._validate_ip_for_anti_affinity(vms)

        elastic_names = [p.name for p in req.bm_profiles if p.count is None]
        min_copies: dict[str, int] = {}
        trail: list[dict[str, object]] = []
        result = None

        # Escalate-until-feasible: the analytic floors in _build_baremetals
        # are necessary conditions only (packing fragmentation and rule
        # interplay can still bite), so verify with the real solver and add
        # one BM to the implicated pool per round. Fixed-count profiles are
        # the user's explicit ask — never inflated; without them (or without
        # verify) this collapses to a single build.
        for round_no in range(_MAX_ESCALATIONS + 1):
            self.diag.pop("unplaced_ground_truth", None)
            bms = self._build_baremetals(vms, racks, min_copies)
            self._assign_candidates(vms, bms)
            ground_truth = self._place(vms, bms)
            placement = PlacementRequest(
                vms=vms,
                baremetals=bms,
                max_per_bm_rules=self._build_max_per_bm_rules(),
                exclusive_bm_rules=self._build_exclusive_rules(),
                failover_rules=self._build_failover_rules(),
                config=self._build_config(),
            )
            if not req.verify:
                break
            result = VMPlacementSolver(placement).solve()
            if result.success or not elastic_names or round_no == _MAX_ESCALATIONS:
                break
            trail.append({"bms": len(bms), "status": result.solver_status})
            for name in self._escalation_targets(result, vms) or elastic_names:
                min_copies[name] = self._elastic_copies.get(name, 0) + 1

        known = {r.value for r in NodeRole}
        unknown = sorted({vm.node_role for vm in vms} - known)
        if unknown:
            self.diag["unknown_roles"] = unknown

        self.diag["num_vms"] = len(vms)
        self.diag["num_baremetals"] = len(bms)
        self.diag["num_ags"] = len({t.ag for t in racks})
        if trail:
            self.diag["auto_escalated"] = {
                "rounds": len(trail),
                "trail": trail + [{"bms": len(bms),
                                   "status": result.solver_status}],
            }

        feasibility = "unverified"
        if result is not None:
            self.diag["solver_status"] = result.solver_status
            self.diag["solver_unplaced"] = result.unplaced_vms
            feasibility = "verified" if result.success else "infeasible"

        return GenerateResponse(
            request=placement,
            ground_truth=ground_truth,
            feasibility=feasibility,
            diagnostics=self.diag,
            verified=result,
        )


def generate_mock_request(req: GenerateRequest) -> GenerateResponse:
    return _Generator(req).generate()


@router.post("/generate", response_model=GenerateResponse)
def generate(req: GenerateRequest) -> GenerateResponse:
    """Generate a complete, solver-ready PlacementRequest from high-level knobs."""
    return generate_mock_request(req)
