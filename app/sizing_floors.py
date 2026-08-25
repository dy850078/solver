"""
Analytic lower bounds on fleet size for rollout sizing (ADR-014).

Every function here returns a number the real answer can never fall below.
That direction matters: the sizing search starts at these floors and never
probes beneath them, so an over-estimate would silently hide a smaller
feasible fleet and make "minimum" a lie. When a bound cannot be argued
soundly, the honest contribution is 0.

The bounds are computed over the UNION of every step's demand, which is a
valid lower bound for a sequential build: `solve_rollout` never removes
load, so by the last step every step's VMs are resident simultaneously.
A sequential build can only need MORE than that (earlier steps fragment
the fleet for later ones) — hence "lower bound", not "answer".

Pure arithmetic: no solver import, no CP-SAT.
"""

from __future__ import annotations

from .models import (
    ExclusiveBaremetalRule,
    MaxPerBaremetalRule,
    ResourceRequirement,
    Resources,
    RolloutStep,
    SolverConfig,
    VM,
)
from .splitter import RESOURCE_FIELDS, pod_node_floor


def _ceil_div(a: int, b: int) -> int:
    return -(-a // b) if b > 0 else 0


def requirement_vm_floor(req: ResourceRequirement, config: SolverConfig) -> int:
    """
    Fewest VMs the splitter could possibly create for this requirement.

    The spec is the splitter's decision, so the resource term divides by
    the LARGEST usable spec in each dimension — anything smaller needs more
    VMs, never fewer. (`splitter.spec_count_upper_bound` answers the
    opposite question and must not be used here.)
    """
    specs = req.vm_specs if req.vm_specs is not None else config.vm_specs
    specs = [s for s in (specs or []) if any(getattr(s, f) > 0 for f in RESOURCE_FIELDS)]
    resource_floor = 0
    if specs:
        for field in RESOURCE_FIELDS:
            total = getattr(req.total_resources, field)
            if total <= 0:
                continue
            biggest = max(getattr(s, field) for s in specs)
            if biggest > 0:
                resource_floor = max(resource_floor, _ceil_div(total, biggest))
    floor = max(resource_floor, pod_node_floor(req, config.max_pods_per_node))
    if req.min_total_vms is not None:
        floor = max(floor, req.min_total_vms)
    return floor


def _smallest_specs(req: ResourceRequirement, config: SolverConfig) -> list[Resources]:
    specs = req.vm_specs if req.vm_specs is not None else config.vm_specs
    return [s for s in (specs or []) if any(getattr(s, f) > 0 for f in RESOURCE_FIELDS)]


def capacity_floor(
    vms: list[VM], reqs: list[ResourceRequirement],
    capacity: Resources, config: SolverConfig,
) -> int:
    """
    Volume bound: per resource field, the total demand cannot exceed what
    the fleet physically holds, so `ceil(Σ demand_f / capacity_f)` machines
    are needed in the worst field.

    Deliberately NOT "group VMs by size, divide each group by how many fit
    per machine, and add the results" — that assumes differently-sized VMs
    never share a machine, which over-estimates (an 8-core VM happily rides
    along with a 40-core one). An over-estimate here would make the search
    skip past a feasible smaller fleet and report a "minimum" that is not
    one. Requirements contribute their total_resources directly; their
    spec is the splitter's choice and volume is spec-independent.
    """
    floor = 0
    for field in RESOURCE_FIELDS:
        cap = getattr(capacity, field)
        if cap <= 0:
            continue
        total = sum(getattr(vm.demand, field) for vm in vms)
        total += sum(getattr(r.total_resources, field) for r in reqs)
        if total > 0:
            floor = max(floor, _ceil_div(total, cap))
    return floor


def headcount_floor(
    vms: list[VM], max_per_bm_rules: list[MaxPerBaremetalRule], config: SolverConfig,
) -> int:
    """
    C4 projected onto fleet size. Summing the per-BM constraint
    `Σ assign[vm∈group, bm] ≤ m` over all machines gives `|group| ≤ m·|BMs|`,
    i.e. `|BMs| ≥ ceil(|group|/m)` — a counting bound independent of capacity
    (ADR-008).

    Groups never multiply: the max across groups is the bound, because
    distinct groups may share machines.
    """
    floor = 0
    for rule in max_per_bm_rules:
        if rule.max_per_bm < 1:
            continue
        if rule.vm_ids:
            n = sum(1 for vm in vms if vm.id in set(rule.vm_ids))
        elif rule.selector is not None:
            n = sum(1 for vm in vms if rule.selector.matches(vm))
        else:
            continue
        floor = max(floor, _ceil_div(n, rule.max_per_bm))

    if config.auto_generate_max_per_bm and config.default_max_per_bm:
        groups: dict[tuple[str, str, str], int] = {}
        for vm in vms:
            key = (vm.cluster_id, vm.ip_type, vm.node_role)
            groups[key] = groups.get(key, 0) + 1
        for n in groups.values():
            floor = max(floor, _ceil_div(n, config.default_max_per_bm))
    return floor


def pack_floor(
    vms: list[VM], reqs: list[ResourceRequirement],
    capacity: Resources, config: SolverConfig,
) -> int:
    """
    Bin-packing L2 bound: a VM using more than half of some dimension can
    never share that machine with another such VM, so each needs its own.

    A requirement's spec is a decision variable, so it only counts when
    EVERY usable spec is oversized — otherwise the splitter can pick a
    small one and the bound evaporates.
    """
    def is_big(demand: Resources) -> bool:
        return any(
            getattr(demand, f) * 2 > getattr(capacity, f) > 0
            for f in RESOURCE_FIELDS
        )

    count = sum(1 for vm in vms if is_big(vm.demand))
    for req in reqs:
        specs = _smallest_specs(req, config)
        if specs and all(is_big(s) for s in specs):
            count += requirement_vm_floor(req, config)
    return count


def solo_floor(vms: list[VM], exclusive_rules: list[ExclusiveBaremetalRule]) -> int:
    """C6 members occupy a machine alone, so each one costs a whole machine.

    Counted once per VM even if several rules name it.
    """
    members: set[str] = set()
    for rule in exclusive_rules:
        if rule.vm_ids:
            members.update(rule.vm_ids)
        elif rule.selector is not None:
            members.update(vm.id for vm in vms if rule.selector.matches(vm))
    known = {vm.id for vm in vms}
    return len(members & known)


def fleet_floor(steps: list[RolloutStep], capacity: Resources,
                config: SolverConfig, ags: int) -> tuple[int, dict[str, int]]:
    """
    Combined lower bound over the union of all steps, plus its breakdown.

    Exclusive machines are ADDED rather than max'd: a C6 member's machine
    serves nobody else, so it cannot also absorb the capacity/headcount
    demand of the rest.

    `ags` enters because a fleet spread over K availability groups needs at
    least K machines to put one in each — a topology floor, not a demand
    one, so the answer is "minimum for the fleet shape you asked for".
    """
    vms = [vm for step in steps for vm in step.vms]
    reqs = [r for step in steps for r in step.requirements]
    mpb = [r for step in steps for r in step.max_per_bm_rules]
    excl = [r for step in steps for r in step.exclusive_bm_rules]

    solo = solo_floor(vms, excl)
    solo_ids = set()
    for rule in excl:
        if rule.vm_ids:
            solo_ids.update(rule.vm_ids)
        elif rule.selector is not None:
            solo_ids.update(vm.id for vm in vms if rule.selector.matches(vm))
    rest = [vm for vm in vms if vm.id not in solo_ids]

    breakdown = {
        "ags": ags,
        "solo": solo,
        "capacity": capacity_floor(rest, reqs, capacity, config),
        "headcount": headcount_floor(rest, mpb, config),
        "pack": pack_floor(rest, reqs, capacity, config),
    }
    demand_side = max(
        breakdown["capacity"], breakdown["headcount"], breakdown["pack"],
    )
    floor = max(ags, solo + demand_side, 1)
    breakdown["total"] = floor
    return floor, breakdown
