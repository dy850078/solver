"""
BM-equivalent demand → placement plan (ADR-020).

At procurement time a PM does not hand over a VM list. The input is
"cluster X needs N machines of model M, VMs of spec S" (or "count VMs of
spec S"), plus policy: which clusters may share machines, which clusters
own their machines outright, and how many distinct machines each node
type must at least spread over. This module compiles that into the
request shapes the existing engines already solve, runs them, and
aggregates the answers into PM-facing reports.

  ClusterDemand ─┐
  TenancyGroup ──┼─ compile ─▶ RolloutStep[] per group ─▶ size_rollout /
  policy ────────┘                                         solve_rollout
                                                              │
                 placement_matrix / bm_layouts / legacy_template ◀┘

Design decisions (details and alternatives in ADR-020):

- Equivalent BMs use MACHINE-COUNT semantics: k = fits_per_bm(spec, cap)
  VMs per machine, count = N × k, pinned exactly (min = max = count).
  N is the PM's purchase number and is treated as hard; if the spec does
  not tile the machine, the resource shortfall vs N × cap is reported as
  an advisory rather than silently buying machine N+1.
- Tenancy groups are POOLS: clusters that may co-reside form one group
  with its own fleet; groups never share machines. Each group is sized
  (or placed on its fixed fleet) independently and the totals add up.
- Nothing here touches CP-SAT. The only solver-side primitive the plan
  needed — "at least N distinct machines" — is C7 (ADR-019).
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import Any

from pydantic import BaseModel, Field, model_validator

from .models import (
    Baremetal,
    ExclusiveBaremetalRule,
    FleetTemplate,
    GroupSelector,
    MinDistinctBaremetalRule,
    ResourceRequirement,
    Resources,
    RolloutRequest,
    RolloutResult,
    RolloutSizingRequest,
    RolloutSizingResult,
    RolloutStep,
    SolverConfig,
    VM,
    config_fingerprint,
    res_get,
    resource_dims,
)
from .placement_report import (
    BmLayout,
    PlacementMatrixRow,
    bm_layouts,
    legacy_template,
    placement_matrix,
)
from .rollout import solve_rollout
from .rollout_sizing import build_fleet, point_steps_at_fleet, size_rollout

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------

class BmSpec(BaseModel):
    """A machine model: name + usable capacity (what VMs may consume)."""
    name: str = Field(min_length=1)
    capacity: Resources


class VmSpec(BaseModel):
    """A named VM shape from the catalog (typically 1/1, 1/2, 1/3 of a BM)."""
    name: str = Field(min_length=1)
    resources: Resources


class ClusterDemand(BaseModel):
    """
    One node type of one cluster, in PM terms.

    Exactly one sizing basis is required, both may be given if consistent:
    - bm_equivalents: "N machines' worth" → count = N × fits_per_bm(spec)
    - vm_count: "count VMs" → bm_equivalents is derived (ceil(count / k))
    A cluster listed in BmDemandPlanRequest.exclusive_clusters places one
    VM per machine regardless of spec (k is forced to 1).
    """
    cluster_id: str = Field(min_length=1)
    node_role: str = "worker"
    ip_type: str = ""
    vm_spec: str = Field(min_length=1)
    bm_equivalents: int | None = Field(default=None, ge=1)
    vm_count: int | None = Field(default=None, ge=1)
    # Overrides BmDemandPlanRequest.default_min_distinct_bms; 0 disables.
    min_distinct_bms: int | None = Field(default=None, ge=0)
    # Build step this demand belongs to; default = its cluster_id.
    step: str | None = None
    # Tenancy group override: place THIS node type in a different pool than
    # the rest of its cluster — the "these roles may only live on tagged
    # machines" policy, with exclusive semantics (nobody else uses that
    # pool). None = the group that lists the cluster.
    group: str | None = None

    @model_validator(mode="after")
    def _one_basis(self) -> ClusterDemand:
        if self.bm_equivalents is None and self.vm_count is None:
            raise ValueError(
                f"demand {self.cluster_id}/{self.node_role}: give bm_equivalents "
                f"or vm_count"
            )
        return self


class TenancyGroup(BaseModel):
    """
    Clusters that may share machines, and the machine model they run on.
    Groups never share machines with each other (pool semantics).

    Membership is per cluster by default; a ClusterDemand.group override
    moves one node type of a cluster into another group (so a cluster can
    span groups, e.g. its gpu-workers on a GPU pool).

    fleet_size: None → size the group ("how many machines?"); an int →
    place on exactly that many machines ("does it fit?").
    """
    name: str = Field(min_length=1)
    # May be empty for a group populated only through demand overrides.
    clusters: list[str] = Field(default_factory=list)
    bm_spec: str = Field(min_length=1)
    fleet_size: int | None = Field(default=None, ge=1)


class FleetTopology(BaseModel):
    """Topology counts shared by every group's fleet (FleetTemplate minus
    the machine model, which comes from the group's bm_spec)."""
    sites: int = Field(default=1, ge=1)
    phases: int = Field(default=1, ge=1)
    datacenters: int = Field(default=1, ge=1)
    rooms: int = Field(default=1, ge=1)
    racks: int = Field(default=6, ge=1)
    ags: int = Field(default=3, ge=1)
    network: str = ""


class BmDemandPlanRequest(BaseModel):
    bm_specs: list[BmSpec] = Field(min_length=1)
    vm_specs: list[VmSpec] = Field(min_length=1)
    demands: list[ClusterDemand] = Field(min_length=1)
    # Empty → one group named "shared" on the first bm_spec with every cluster.
    groups: list[TenancyGroup] = Field(default_factory=list)
    # Clusters whose VMs each own a machine outright (C6).
    exclusive_clusters: list[str] = Field(default_factory=list)
    # Applied to every demand that does not set min_distinct_bms. None/0 = off.
    default_min_distinct_bms: int | None = Field(default=None, ge=0)
    topology: FleetTopology = Field(default_factory=FleetTopology)
    # Step names (default: cluster ids) in build order; unlisted steps follow
    # in first-appearance order.
    build_order: list[str] | None = None
    # w_headroom defaults to 0 here (not SolverConfig's 8): the headroom
    # term exists for production placement, where leaving every machine
    # under ~90% buys growth room. A PM's specs are exact machine fractions
    # MEANT to fill machines; with headroom on, the solver places two
    # thirds per machine instead of three, and the sequential rollout then
    # finds no machine with room for the next cluster's half — 24 machines
    # where 18 suffice in the reference scenario (ADR-020 D6). Override
    # by passing a full SolverConfig.
    config: SolverConfig = Field(default_factory=lambda: SolverConfig(w_headroom=0))
    max_baremetals: int = Field(default=200, ge=1)
    max_probes: int = Field(default=12, ge=1)
    deadline_seconds: float = Field(default=120.0, gt=0)
    # legacy_template field order; see placement_report.legacy_template.
    template_fields: list[str] = Field(
        default_factory=lambda: ["node_role", "vm_spec", "ip_type"],
    )


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------

class CompiledDemand(BaseModel):
    """What one ClusterDemand became — echoed so a PM can audit the math."""
    cluster_id: str
    node_role: str
    ip_type: str
    vm_spec: str
    fits_per_bm: int
    bm_equivalents: int
    vm_count: int
    min_distinct_bms: int
    exclusive: bool
    step: str


class CompiledGroup(BaseModel):
    name: str
    clusters: list[str]
    bm_spec: str
    fleet: FleetTemplate
    fleet_size: int | None = None
    steps: list[RolloutStep]
    demands: list[CompiledDemand]


class GroupPlanResult(BaseModel):
    name: str
    clusters: list[str]
    bm_spec: str
    success: bool
    solver_status: str = ""
    required_baremetals: int = 0
    sizing: RolloutSizingResult | None = None
    rollout: RolloutResult | None = None
    baremetals: list[Baremetal] = Field(default_factory=list)


class BmDemandPlanResult(BaseModel):
    """
    success is True only when every group placed. total_baremetals is the
    SUM over groups (pools never share machines). Reports span all groups;
    machine ids are prefixed "<group>/" so they never collide.
    """
    success: bool
    solver_status: str = ""
    groups: list[GroupPlanResult] = Field(default_factory=list)
    compiled: list[CompiledGroup] = Field(default_factory=list)
    total_baremetals: int = 0
    placement_matrix: list[PlacementMatrixRow] = Field(default_factory=list)
    bm_layouts: list[BmLayout] = Field(default_factory=list)
    legacy_template: dict[str, list[list[str]]] = Field(default_factory=dict)
    advisories: list[dict[str, Any]] = Field(default_factory=list)
    config_fingerprint: str = ""
    diagnostics: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Pure arithmetic
# ---------------------------------------------------------------------------

def fits_per_bm(spec: Resources, capacity: Resources) -> int:
    """
    How many VMs of `spec` one machine of `capacity` holds:
    min over demanded dimensions of floor(capacity_d / spec_d).

    floor, because a VM is whole: 90 cores hold two 40-core VMs, not 2.25.
    min across dimensions, because a VM must fit in CPU, memory, storage
    and every GPU model at once — the tightest dimension decides. This is
    C2 solved by hand for the single-spec, single-machine case. 0 means
    the spec does not fit at all.
    """
    k = None
    for dim in resource_dims([spec, capacity]):
        need = res_get(spec, dim)
        if need <= 0:
            continue
        have = res_get(capacity, dim)
        fit = have // need
        k = fit if k is None else min(k, fit)
    return 0 if k is None else k


def _ceil_div(a: int, b: int) -> int:
    return -(-a // b)


def _selector(d: ClusterDemand) -> GroupSelector:
    return GroupSelector(
        cluster_id=d.cluster_id,
        ip_type=d.ip_type or None,
        node_role=d.node_role,
    )


# ---------------------------------------------------------------------------
# Compiler
# ---------------------------------------------------------------------------

class _Compiled:
    def __init__(self):
        self.groups: list[CompiledGroup] = []
        self.errors: list[str] = []
        self.advisories: list[dict[str, Any]] = []


def compile_bm_demands(req: BmDemandPlanRequest) -> _Compiled:
    """Turn PM demand + policy into per-group rollout steps. Pure."""
    out = _Compiled()
    bm_specs = {b.name: b for b in req.bm_specs}
    vm_specs = {v.name: v for v in req.vm_specs}
    exclusive = set(req.exclusive_clusters)
    demand_clusters = [d.cluster_id for d in req.demands]
    demand_cluster_set = set(demand_clusters)

    if len(bm_specs) != len(req.bm_specs):
        out.errors.append("bm_specs names must be unique")
    if len(vm_specs) != len(req.vm_specs):
        out.errors.append("vm_specs names must be unique")

    seen_keys: set[tuple[str, str, str]] = set()
    for d in req.demands:
        key = (d.cluster_id, d.ip_type, d.node_role)
        if key in seen_keys:
            out.errors.append(
                f"duplicate demand for {d.cluster_id}/{d.ip_type or '*'}/{d.node_role}"
            )
        seen_keys.add(key)

    for c in exclusive - demand_cluster_set:
        out.advisories.append({
            "type": "exclusive_cluster_without_demand", "severity": "info",
            "cluster_id": c,
            "message": f"exclusive cluster '{c}' has no demand — nothing to protect",
        })

    # --- tenancy groups -----------------------------------------------
    groups = list(req.groups) or [TenancyGroup(
        name="shared", clusters=list(OrderedDict.fromkeys(demand_clusters)),
        bm_spec=req.bm_specs[0].name,
    )]
    owner: dict[str, str] = {}
    for g in groups:
        if g.bm_spec not in bm_specs:
            out.errors.append(f"group '{g.name}': unknown bm_spec '{g.bm_spec}'")
        for c in g.clusters:
            if c in owner:
                out.errors.append(
                    f"cluster '{c}' is in groups '{owner[c]}' and '{g.name}' — "
                    f"a cluster belongs to exactly one tenancy group"
                )
            owner[c] = g.name
            has_demand = any(
                d.cluster_id == c and (d.group is None or d.group == g.name)
                for d in req.demands
            )
            if not has_demand:
                out.advisories.append({
                    "type": "group_cluster_without_demand", "severity": "info",
                    "cluster_id": c, "group": g.name,
                    "message": f"group '{g.name}' lists cluster '{c}' which has no demand",
                })
    names = [g.name for g in groups]
    if len(set(names)) != len(names):
        out.errors.append("group names must be unique")

    def owner_of(d: ClusterDemand) -> str | None:
        return d.group if d.group is not None else owner.get(d.cluster_id)

    for d in req.demands:
        label = f"{d.cluster_id}/{d.ip_type or '*'}/{d.node_role}"
        if d.group is not None and d.group not in names:
            out.errors.append(f"demand {label}: unknown group '{d.group}'")
        elif owner_of(d) is None:
            out.errors.append(
                f"demand {label}: cluster '{d.cluster_id}' is in no tenancy "
                f"group and the demand names none — list the cluster in a "
                f"group, set the demand's group, or omit groups entirely for "
                f"a single shared pool"
            )

    # --- build order ----------------------------------------------------
    step_names_in_order = list(OrderedDict.fromkeys(
        d.step or d.cluster_id for d in req.demands
    ))
    if req.build_order is not None:
        unknown = [n for n in req.build_order if n not in step_names_in_order]
        if unknown:
            out.errors.append(f"build_order names unknown steps {unknown}")
        ordered = [n for n in req.build_order if n in step_names_in_order]
        ordered += [n for n in step_names_in_order if n not in ordered]
        step_names_in_order = ordered

    if out.errors:
        return out

    # --- per-demand compile ---------------------------------------------
    per_group_steps: dict[str, dict[str, RolloutStep]] = {g.name: OrderedDict() for g in groups}
    per_group_compiled: dict[str, list[CompiledDemand]] = {g.name: [] for g in groups}
    group_by_name = {g.name: g for g in groups}
    emitted_exclusive: set[tuple[str, str]] = set()

    for d in req.demands:
        gname = owner_of(d)
        g = group_by_name[gname]
        cap = bm_specs[g.bm_spec].capacity
        spec = vm_specs.get(d.vm_spec)
        label = f"{d.cluster_id}/{d.ip_type or '*'}/{d.node_role}"
        if spec is None:
            out.errors.append(f"demand {label}: unknown vm_spec '{d.vm_spec}'")
            continue
        k_phys = fits_per_bm(spec.resources, cap)
        if k_phys == 0:
            out.errors.append(
                f"demand {label}: vm_spec '{d.vm_spec}' does not fit one "
                f"'{g.bm_spec}' machine"
            )
            continue
        is_exclusive = d.cluster_id in exclusive
        k = 1 if is_exclusive else k_phys

        if d.vm_count is not None:
            count = d.vm_count
            derived_n = _ceil_div(count, k)
            if d.bm_equivalents is not None and d.bm_equivalents != derived_n:
                out.errors.append(
                    f"demand {label}: vm_count={count} of '{d.vm_spec}' is "
                    f"{derived_n} machine(s) at {k} per machine, but "
                    f"bm_equivalents={d.bm_equivalents} — give one or make "
                    f"them agree"
                )
                continue
            n = derived_n
        else:
            n = d.bm_equivalents
            count = n * k

        min_distinct = (
            d.min_distinct_bms if d.min_distinct_bms is not None
            else (req.default_min_distinct_bms or 0)
        )
        if min_distinct and count < min_distinct:
            out.errors.append(
                f"demand {label}: {count} VM(s) ({n} × '{d.vm_spec}' at {k} per "
                f"machine) cannot spread over {min_distinct} machines — use a "
                f"smaller spec or more machines"
            )
            continue

        # Advisory: spec does not tile the machine → PM gets fewer
        # resources than N × cap (the machine count stays N; ADR-020 D1).
        for dim in resource_dims([spec.resources, cap]):
            cap_d = res_get(cap, dim)
            got = k * res_get(spec.resources, dim)
            if cap_d > 0 and got < cap_d:
                out.advisories.append({
                    "type": "spec_does_not_tile_bm", "severity": "warning",
                    "cluster_id": d.cluster_id, "node_role": d.node_role,
                    "dimension": dim, "per_machine_capacity": cap_d,
                    "per_machine_allocated": got,
                    "message": (
                        f"demand {label}: {k} × '{d.vm_spec}' uses {got} of "
                        f"{cap_d} {dim} per machine; {n} machine(s) provide "
                        f"{n * got} instead of {n * cap_d}"
                        + (" (exclusive: one VM per machine)" if is_exclusive else "")
                    ),
                })

        # Advisories: "each cluster takes its own N machines" is always a
        # legal layout under C1/C2/C6 (D1), so sum(N) machines fit — but two
        # LOWER-bound constraints can demand more than N for one cluster:
        #   * C3 (auto anti-affinity) spreads the group over every AG with
        #     cap ceil(count/ags), so it must touch min(count, ags) AGs and
        #     hence that many machines;
        #   * C7 (min_distinct_bms) asks for N' distinct machines outright.
        # Say so before the solve does; the sizing answer will show it anyway.
        if min_distinct and n < min_distinct:
            out.advisories.append({
                "type": "footprint_exceeds_equivalents", "severity": "warning",
                "cluster_id": d.cluster_id, "node_role": d.node_role,
                "bm_equivalents": n, "machines_needed_for_footprint": min_distinct,
                "message": (
                    f"demand {label}: policy requires at least {min_distinct} "
                    f"distinct machines, more than the {n} equivalent machine(s) "
                    f"requested — expect the plan to use {min_distinct}"
                ),
            })
        if req.config.auto_generate_anti_affinity:
            ag_floor = min(count, req.topology.ags)
            if n < ag_floor:
                out.advisories.append({
                    "type": "ag_spread_exceeds_equivalents", "severity": "warning",
                    "cluster_id": d.cluster_id, "node_role": d.node_role,
                    "bm_equivalents": n, "machines_needed_for_spread": ag_floor,
                    "message": (
                        f"demand {label}: {count} VM(s) spread over "
                        f"{req.topology.ags} AGs need at least {ag_floor} machines, "
                        f"more than the {n} equivalent machine(s) requested"
                    ),
                })

        step_name = d.step or d.cluster_id
        step = per_group_steps[gname].get(step_name)
        if step is None:
            step = RolloutStep(name=step_name)
            per_group_steps[gname][step_name] = step
        step.requirements.append(ResourceRequirement(
            total_resources=Resources(
                cpu_cores=spec.resources.cpu_cores * count,
                memory_mib=spec.resources.memory_mib * count,
                storage_gb=spec.resources.storage_gb * count,
                gpu={m: q * count for m, q in spec.resources.gpu.items()},
            ),
            node_role=d.node_role, cluster_id=d.cluster_id, ip_type=d.ip_type,
            vm_specs=[spec.resources],
            min_total_vms=count, max_total_vms=count,
            network=req.topology.network, pool=gname,
        ))
        if is_exclusive and (gname, d.cluster_id) not in emitted_exclusive:
            step.exclusive_bm_rules.append(ExclusiveBaremetalRule(
                group_id=f"exclusive/{d.cluster_id}",
                selector=GroupSelector(cluster_id=d.cluster_id),
            ))
            emitted_exclusive.add((gname, d.cluster_id))
        if min_distinct:
            step.min_distinct_bm_rules.append(MinDistinctBaremetalRule(
                group_id=f"footprint/{label}",
                selector=_selector(d),
                min_distinct_bms=min_distinct,
            ))
        per_group_compiled[gname].append(CompiledDemand(
            cluster_id=d.cluster_id, node_role=d.node_role, ip_type=d.ip_type,
            vm_spec=d.vm_spec, fits_per_bm=k, bm_equivalents=n, vm_count=count,
            min_distinct_bms=min_distinct, exclusive=is_exclusive, step=step_name,
        ))

    if out.errors:
        return out

    for g in groups:
        steps_by_name = per_group_steps[g.name]
        if not steps_by_name:
            continue  # a group with no demand has nothing to size
        ordered = [steps_by_name[n] for n in step_names_in_order if n in steps_by_name]
        t = req.topology
        fleet = FleetTemplate(
            total_capacity=bm_specs[g.bm_spec].capacity,
            sites=t.sites, phases=t.phases, datacenters=t.datacenters,
            rooms=t.rooms, racks=t.racks, ags=t.ags, network=t.network,
            pool=g.name, id_prefix=f"{g.name}/bm-",
        )
        # Report the clusters that actually have demand here (overrides can
        # pull a cluster in or move all of its demand out).
        present = list(OrderedDict.fromkeys(
            cd.cluster_id for cd in per_group_compiled[g.name]
        ))
        out.groups.append(CompiledGroup(
            name=g.name, clusters=present, bm_spec=g.bm_spec,
            fleet=fleet, fleet_size=g.fleet_size, steps=ordered,
            demands=per_group_compiled[g.name],
        ))
    return out


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def _run_group(cg: CompiledGroup, req: BmDemandPlanRequest) -> GroupPlanResult:
    if cg.fleet_size is None:
        sizing = size_rollout(RolloutSizingRequest(
            fleet=cg.fleet, steps=cg.steps, config=req.config,
            max_baremetals=req.max_baremetals, max_probes=req.max_probes,
            deadline_seconds=req.deadline_seconds,
        ))
        return GroupPlanResult(
            name=cg.name, clusters=cg.clusters, bm_spec=cg.bm_spec,
            success=sizing.success, solver_status=sizing.solver_status,
            required_baremetals=sizing.required_baremetals,
            sizing=sizing, rollout=sizing.rollout, baremetals=sizing.baremetals,
        )
    bms = build_fleet(cg.fleet, cg.fleet_size)
    rollout = solve_rollout(RolloutRequest(
        baremetals=bms,
        steps=point_steps_at_fleet(cg.steps, [b.id for b in bms]),
        config=req.config,
    ))
    status = rollout.solver_status
    if not status:
        failed = next((r for r in rollout.reports if not r.success), None)
        status = failed.solver_status if failed else "OPTIMAL"
    return GroupPlanResult(
        name=cg.name, clusters=cg.clusters, bm_spec=cg.bm_spec,
        success=rollout.success, solver_status=status,
        required_baremetals=cg.fleet_size if rollout.success else 0,
        rollout=rollout, baremetals=bms,
    )


def plan_bm_demands(req: BmDemandPlanRequest) -> BmDemandPlanResult:
    """Compile, run every tenancy group, aggregate the PM-facing reports.
    Every return path carries the config fingerprint."""
    start = time.time()
    fingerprint = config_fingerprint(req.config)
    compiled = compile_bm_demands(req)
    if compiled.errors:
        for err in compiled.errors:
            logger.error("bm-demand compile failed: %s", err)
        return BmDemandPlanResult(
            success=False,
            solver_status=f"INPUT_ERROR: {compiled.errors[0]}",
            advisories=compiled.advisories,
            config_fingerprint=fingerprint,
            diagnostics={"input_errors": compiled.errors},
        )

    catalog = {v.name: v.resources for v in req.vm_specs}
    group_results: list[GroupPlanResult] = []
    all_assign, all_vms, all_bms = [], [], []
    for cg in compiled.groups:
        gr = _run_group(cg, req)
        group_results.append(gr)
        if gr.rollout is not None:
            # Synthetic ids are only unique within one rollout ("A1/split-r0-
            # s0-3" exists in every group whose build order has a step A1),
            # so namespace them by group before pooling the reports — the
            # machines already carry the group prefix via FleetTemplate.
            for rep in gr.rollout.reports:
                for a, vm in zip(rep.new_assignments, rep.new_vms):
                    all_assign.append(a.model_copy(update={"vm_id": f"{gr.name}/{a.vm_id}"}))
                    all_vms.append(vm.model_copy(update={"id": f"{gr.name}/{vm.id}"}))
            all_bms.extend(gr.baremetals)

    layouts = bm_layouts(all_assign, all_vms, all_bms, catalog)
    success = all(g.success for g in group_results)
    first_bad = next((g for g in group_results if not g.success), None)
    logger.info(
        "bm-demand plan: %d group(s), success=%s, %.2fs",
        len(group_results), success, time.time() - start,
    )
    return BmDemandPlanResult(
        success=success,
        solver_status="" if success else f"{first_bad.name}: {first_bad.solver_status}",
        groups=group_results,
        compiled=compiled.groups,
        total_baremetals=sum(g.required_baremetals for g in group_results),
        placement_matrix=placement_matrix(all_assign, all_vms, all_bms, catalog),
        bm_layouts=layouts,
        legacy_template=legacy_template(layouts, tuple(req.template_fields)),
        advisories=compiled.advisories,
        config_fingerprint=fingerprint,
        diagnostics={"elapsed_seconds": round(time.time() - start, 3)},
    )
