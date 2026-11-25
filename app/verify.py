"""
Independent placement verifier — re-checks C1–C7 on a finished result
WITHOUT going through CP-SAT.

Why this exists
---------------
The solver's tests mostly ask the solver whether the solver is right. A bug
in a constraint builder (a skipped bucket, a wrong cap, a missing variable)
makes the model accept a bad layout and report OPTIMAL with a straight face.
This module is the second opinion: it takes (request, result), restates
each constraint as plain counting over the returned assignments, and lists
every violation.

Independence is the whole point, so this file deliberately does NOT import
`app.solver` and does NOT reuse the solver's rule-resolution helpers
(`_expand_vm_ids`, `SolverConfig.auto_group_key`, `GroupSelector.matches`).
Group membership, auto-generated groups and caps are re-derived here from
the documented semantics (CLAUDE.md constraint catalog, model docstrings).
That is intentional duplication — the one place where "single source of
truth" is the wrong goal: two implementations that must agree are worth
more than one implementation checked against itself. When a constraint's
semantics change, change the solver AND this file; a disagreement between
them is the signal.

Scope
-----
- C1 one BM per VM, eligibility (candidates / pins), output consistency
- C2 capacity per resource dimension (pins handled without normalization)
- C3 anti-affinity caps (explicit + auto, grandfathered, split dynamic ceil)
- C4 max-per-BM (explicit + auto, grandfathered)
- C5 failover N-1 (grandfathered)
- C6 exclusive occupancy
- C7 minimum distinct BMs
Not covered: objective quality (a valid but wasteful layout passes),
splitter coverage of the requested budget, capacity-planner extras
(procurement caps, committed stock). Failed results are not verified —
there is no layout to check.

CLI:  python -m app.verify --request request.json [--result result.json]
      (without --result the request is solved in-process first)
      exit 0 = no violations, 1 = violations, 2 = result was not a success
"""

from __future__ import annotations

import argparse
import math
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Iterable

from .models import (
    GPU_DIM_PREFIX,
    SCALAR_RESOURCE_FIELDS,
    SPREAD_DIMENSIONS,
    VM,
    GroupSelector,
    PlacementRequest,
    PlacementResult,
    Resources,
)


@dataclass(frozen=True)
class Violation:
    constraint: str   # "C1" … "C7", or "OUT" for result self-consistency
    subject: str      # vm id / bm id / rule id the violation is about
    message: str

    def __str__(self) -> str:
        return f"[{self.constraint}] {self.subject}: {self.message}"


# ---------------------------------------------------------------------------
# Re-derived semantics (kept local on purpose — see module docstring)
# ---------------------------------------------------------------------------

def _selector_matches(sel: GroupSelector, vm: VM) -> bool:
    """None = wildcard; node_role is a string or a list (membership);
    labels match with a missing key read as ""."""
    if sel.cluster_id is not None and vm.cluster_id != sel.cluster_id:
        return False
    if sel.ip_type is not None and vm.ip_type != sel.ip_type:
        return False
    if sel.node_role is not None:
        roles = [sel.node_role] if isinstance(sel.node_role, str) else list(sel.node_role)
        if vm.node_role not in roles:
            return False
    if sel.labels is not None:
        for k, v in sel.labels.items():
            if vm.labels.get(k, "") != v:
                return False
    return True


def _members(rule, vms: list[VM]) -> list[str]:
    """Group membership: explicit vm_ids (deduplicated) or selector match."""
    if rule.vm_ids:
        return list(dict.fromkeys(rule.vm_ids))
    if rule.selector is None:
        return []
    return [vm.id for vm in vms if _selector_matches(rule.selector, vm)]


def _auto_groups(
    vms: list[VM], covered: set[str], label_keys: list[str]
) -> dict[tuple, list[str]]:
    """Auto C3/C4 grouping: (cluster_id, ip_type, node_role) + the VALUE of
    each configured label key (missing = ""). VMs already covered by an
    explicit rule, or without cluster_id / ip_type, are left out; groups of
    one VM generate no rule."""
    groups: dict[tuple, list[str]] = defaultdict(list)
    for vm in vms:
        if vm.id in covered or not vm.cluster_id or not vm.ip_type:
            continue
        key = (vm.cluster_id, vm.ip_type, vm.node_role,
               tuple(vm.labels.get(k, "") for k in label_keys))
        groups[key].append(vm.id)
    return {k: ids for k, ids in groups.items() if len(ids) >= 2}


def _group_label(prefix: str, key: tuple, label_keys: list[str]) -> str:
    cluster, ip, role, values = key
    base = f"{prefix}/{cluster}/{ip}/{role}"
    if not label_keys:
        return base
    return base + "/" + ",".join(f"{k}={v}" for k, v in zip(label_keys, values))


def _dims(resources: Iterable[Resources]) -> list[str]:
    models: set[str] = set()
    for r in resources:
        models.update(r.gpu)
    return [*SCALAR_RESOURCE_FIELDS, *(GPU_DIM_PREFIX + m for m in sorted(models))]


def _get(r: Resources, dim: str) -> int:
    if dim.startswith(GPU_DIM_PREFIX):
        return r.gpu.get(dim[len(GPU_DIM_PREFIX):], 0)
    return getattr(r, dim)


# ---------------------------------------------------------------------------
# The verifier
# ---------------------------------------------------------------------------

def verify_placement(
    request: PlacementRequest,
    result: PlacementResult,
    *,
    slots: Iterable[str] = (),
) -> list[Violation]:
    """
    Check a successful result against the request. Returns every violation
    found (empty list = the layout satisfies C1–C7).

    `slots`: ids of splitter slot VMs (split-and-solve path). A slot is
    optional — it may be absent from the assignments (inactive) — and an
    auto C3 group containing slots caps each bucket at
    ⌈active members / |buckets|⌉ instead of the static ⌈N / |buckets|⌉.
    """
    out: list[Violation] = []
    slot_ids = set(slots)
    cfg = request.config
    vm_by_id = {vm.id: vm for vm in request.vms}
    bm_by_id = {bm.id: bm for bm in request.baremetals}

    # ---- C1: every VM on exactly one BM, and only where it is allowed -----
    seen = Counter(a.vm_id for a in result.assignments)
    for vm_id, n in seen.items():
        if n > 1:
            out.append(Violation("C1", vm_id, f"assigned {n} times"))
        if vm_id not in vm_by_id:
            out.append(Violation("C1", vm_id, "assignment for a VM not in the request"))

    placed: dict[str, str] = {}     # vm_id -> bm_id (first assignment wins)
    for a in result.assignments:
        vm = vm_by_id.get(a.vm_id)
        if vm is None or a.vm_id in placed:
            continue
        placed[a.vm_id] = a.baremetal_id
        bm = bm_by_id.get(a.baremetal_id)
        if bm is None:
            out.append(Violation("C1", a.vm_id, f"placed on unknown BM '{a.baremetal_id}'"))
            continue
        if vm.pinned_to is not None:
            if a.baremetal_id != vm.pinned_to:
                out.append(Violation(
                    "C1", a.vm_id,
                    f"pinned to '{vm.pinned_to}' but reported on '{a.baremetal_id}'"))
        elif a.baremetal_id not in vm.candidate_baremetals:
            out.append(Violation(
                "C1", a.vm_id, f"placed on '{a.baremetal_id}', not a candidate"))
        if a.pinned != (vm.pinned_to is not None):
            out.append(Violation(
                "OUT", a.vm_id,
                f"pinned flag is {a.pinned} but VM.pinned_to is {vm.pinned_to!r}"))
        if a.ag != bm.topology.ag:
            out.append(Violation(
                "OUT", a.vm_id,
                f"assignment says ag '{a.ag}', BM '{bm.id}' is in '{bm.topology.ag}'"))

    unplaced_reported = set(result.unplaced_vms)
    for vm in request.vms:
        if vm.id in placed:
            if vm.id in unplaced_reported:
                out.append(Violation("OUT", vm.id, "both assigned and listed as unplaced"))
            continue
        if vm.id in slot_ids:
            continue                                   # inactive splitter slot
        if vm.pinned_to is not None:
            out.append(Violation("C1", vm.id, "pinned VM missing from the result"))
        elif not cfg.allow_partial_placement:
            out.append(Violation("C1", vm.id, "not placed"))
        elif vm.id not in unplaced_reported:
            out.append(Violation("OUT", vm.id, "neither assigned nor listed as unplaced"))

    used_bms = set(placed.values())
    if result.bm_used_count != len(used_bms):
        out.append(Violation(
            "OUT", "bm_used_count",
            f"reported {result.bm_used_count}, assignments use {len(used_bms)} BMs"))

    # Layout indexes shared by C2–C7.
    vms_on: dict[str, list[str]] = defaultdict(list)
    for vm_id, bm_id in placed.items():
        vms_on[bm_id].append(vm_id)
    buckets: dict[str, dict[str, set[str]]] = {}
    for dim in SPREAD_DIMENSIONS:
        by_value: dict[str, set[str]] = defaultdict(set)
        for bm in request.baremetals:
            by_value[getattr(bm.topology, dim)].add(bm.id)
        buckets[dim] = dict(by_value)

    def load(ids: Iterable[str], bm_ids: set[str]) -> int:
        return sum(1 for v in ids if placed.get(v) in bm_ids)

    def pinned_in(ids: Iterable[str], bm_ids: set[str]) -> int:
        return sum(1 for v in ids
                   if (vm := vm_by_id.get(v)) is not None and vm.pinned_to in bm_ids)

    # ---- C2: capacity ----------------------------------------------------
    # used_capacity is inventory truth and already contains the demand of
    # pinned VMs, so only the NEWLY placed VMs draw on (total - used). This
    # states the same thing as the solver's "subtract pinned demand, re-add
    # through the fixed assignment" without repeating that arithmetic.
    dims = _dims([
        *(bm.total_capacity for bm in request.baremetals),
        *(bm.used_capacity for bm in request.baremetals),
        *(vm.demand for vm in request.vms),
    ])
    for bm in request.baremetals:
        new_vms = [vm_by_id[v] for v in vms_on.get(bm.id, ())
                   if vm_by_id[v].pinned_to is None]
        if not new_vms:
            continue
        for dim in dims:
            free = _get(bm.total_capacity, dim) - _get(bm.used_capacity, dim)
            need = sum(_get(vm.demand, dim) for vm in new_vms)
            if need > free:
                out.append(Violation(
                    "C2", bm.id, f"{dim}: new VMs need {need}, only {free} free"))

    # ---- C3: anti-affinity -----------------------------------------------
    # (rule id, members, spread dims, cap overrides, is_auto)
    aa: list[tuple[str, list[str], list[str], dict[str, int], bool]] = []
    for r in request.anti_affinity_rules:
        aa.append((r.group_id, _members(r, request.vms), list(r.spread_on),
                   dict(r.cap_per_bucket or {}), False))
    if cfg.auto_generate_anti_affinity and cfg.target_spread:
        covered = {v for _, ids, *_ in aa for v in ids}
        for key, ids in _auto_groups(request.vms, covered, cfg.auto_group_labels).items():
            aa.append((_group_label("auto", key, cfg.auto_group_labels), ids,
                       sorted(cfg.target_spread), {}, True))

    for rule_id, ids, spread_on, overrides, is_auto in aa:
        if not ids:
            continue
        dynamic = is_auto and any(v in slot_ids for v in ids)
        # Dynamic population mirrors "active members": every non-slot member
        # counts, a slot counts only when the splitter activated it.
        population = (sum(1 for v in ids if v not in slot_ids or v in placed)
                      if dynamic else len(ids))
        for dim in spread_on:
            dim_buckets = buckets[dim]
            if not dim_buckets:
                continue
            base_cap = overrides.get(dim, math.ceil(population / len(dim_buckets)))
            for name, bm_ids in dim_buckets.items():
                cap = max(base_cap, pinned_in(ids, bm_ids))     # grandfathered
                n = load(ids, bm_ids)
                if n > cap:
                    out.append(Violation(
                        "C3", rule_id,
                        f"{dim}='{name}' holds {n} members, cap {cap}"))

    # ---- C4: max per BM --------------------------------------------------
    c4: list[tuple[str, list[str], int]] = [
        (r.group_id or "(unnamed)", _members(r, request.vms), r.max_per_bm)
        for r in request.max_per_bm_rules
    ]
    if (cfg.auto_generate_max_per_bm and cfg.default_max_per_bm is not None
            and cfg.default_max_per_bm >= 1):
        covered = {v for _, ids, _ in c4 for v in ids}
        for key, ids in _auto_groups(request.vms, covered, cfg.auto_group_labels).items():
            c4.append((_group_label("auto-bm", key, cfg.auto_group_labels), ids,
                       cfg.default_max_per_bm))
    for rule_id, ids, max_per_bm in c4:
        for bm_id in bm_by_id:
            one = {bm_id}
            cap = max(max_per_bm, pinned_in(ids, one))          # grandfathered
            n = load(ids, one)
            if n > cap:
                out.append(Violation(
                    "C4", rule_id, f"BM '{bm_id}' holds {n} members, cap {cap}"))

    # ---- C5: failover N-1 ------------------------------------------------
    for f in request.failover_rules:
        primary = [vm.id for vm in request.vms if _selector_matches(f.primary, vm)]
        backup = [vm.id for vm in request.vms if _selector_matches(f.backup, vm)]
        both = primary + backup
        for name, bm_ids in buckets[f.fault_domain].items():
            cap = max(len(backup), pinned_in(both, bm_ids))     # grandfathered
            n = load(both, bm_ids)
            if n > cap:
                out.append(Violation(
                    "C5", f.rule_id,
                    f"{f.fault_domain}='{name}' holds {n} primary+backup, "
                    f"only {len(backup)} backups exist"))

    # ---- C6: exclusive occupancy -----------------------------------------
    for r in request.exclusive_bm_rules:
        members = set(_members(r, request.vms))
        for bm_id, on_bm in vms_on.items():
            if members.intersection(on_bm) and len(on_bm) > 1:
                out.append(Violation(
                    "C6", r.group_id or "(unnamed)",
                    f"BM '{bm_id}' must be exclusive but hosts {sorted(on_bm)}"))

    # ---- C7: minimum distinct BMs ----------------------------------------
    for r in request.min_distinct_bm_rules:
        ids = _members(r, request.vms)
        if not ids:
            continue
        distinct = {placed[v] for v in ids if v in placed}
        if len(distinct) < r.min_distinct_bms:
            out.append(Violation(
                "C7", r.group_id or "(unnamed)",
                f"members sit on {len(distinct)} BM(s), need {r.min_distinct_bms}"))

    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Independently verify a placement result against C1–C7")
    parser.add_argument("--request", required=True, help="PlacementRequest JSON")
    parser.add_argument("--result", help="PlacementResult JSON (omit to solve in-process)")
    args = parser.parse_args(argv)

    with open(args.request) as fh:
        request = PlacementRequest.model_validate_json(fh.read())
    if args.result:
        with open(args.result) as fh:
            result = PlacementResult.model_validate_json(fh.read())
    else:
        # Imported here, not at module top: the verifier itself must stay
        # importable (and auditable) without the solver.
        from .solver import VMPlacementSolver
        result = VMPlacementSolver(request).solve()

    if not result.success:
        print(f"result is not a success ({result.solver_status}) — nothing to verify")
        return 2
    violations = verify_placement(request, result)
    for v in violations:
        print(v)
    print(f"{len(result.assignments)} assignments, {len(violations)} violation(s)")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
