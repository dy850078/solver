"""
Rollout simulation — replay a user-specified build order step by step.

Each step runs a REAL solve (full C1–C6 via the split-and-solve path;
a step with only explicit VMs is the exact degenerate case of pure solve).
Placed VMs are carried into every later step as pinned VMs (ADR-012), so
spread counts, per-BM caps, failover census and exclusive occupancy all see
the full population — the simulation answers "does this build order hit a
dead end, and at which step?" before any real machine is racked.

Fold-forward contract (the step k → k+1 hand-off):
- every VM placed at step k becomes a pinned VM in step k+1's request
  (pinned_to = its host, candidate_baremetals = [host] — the solver rejects
  empty candidate lists even for pinned VMs), AND
- its demand is added to the host's rolling used_capacity, because the
  pinned contract defines used_capacity as inventory truth INCLUDING
  pinned consumption; the solver's normalization subtracts it back out, so
  the ledger nets to zero at every step. Resources arithmetic is exact
  per-field integer math — no drift accumulates across steps.
- existing_vms (brownfield starting state) are NEVER folded: their demand
  is already inside the starting used_capacity by contract.

Synthetic VMs are renamed "{step_name}/{synthetic_id}" when carried (and in
reports): the splitter's ids (split-r0-s0-0) repeat identically every step
and would otherwise collide. Explicit VM ids are kept verbatim — a later
step's vm_ids rule may therefore reference an earlier step's explicit VMs
(e.g. two appliances built in different steps sharing one exclusive group).
Renaming those too would silently detach such rules from their members.

Rules accumulate: step k is solved under the union of rules from steps
1..k, so protections established by earlier steps (C6 exclusivity above
all) keep binding later steps. Selectors are the mechanism that spans
steps and reaches synthetic VMs; vm_ids can only reference explicit ids
already introduced (validated here, because the solver silently ignores
unknown vm_ids).
"""

from __future__ import annotations

import logging
import time

from .models import (
    VM,
    PlacementAssignment,
    RolloutRequest,
    RolloutResult,
    RolloutStepReport,
    SplitPlacementRequest,
    config_fingerprint,
)
from .split_solver import solve_split_placement_with_synthetics

logger = logging.getLogger(__name__)


def solve_rollout(request: RolloutRequest) -> RolloutResult:
    """Simulate the build order; every return path carries the fingerprint."""
    start = time.time()
    fingerprint = config_fingerprint(request.config)

    errors = _validate(request)
    if errors:
        for err in errors:
            logger.error("Rollout input validation failed: %s", err)
        return RolloutResult(
            success=False,
            solver_status=f"INPUT_ERROR: {errors[0]}",
            config_fingerprint=fingerprint,
            diagnostics={"input_errors": errors},
        )

    rolling_bms = [bm.model_copy(deep=True) for bm in request.baremetals]
    bm_by_id = {bm.id: bm for bm in rolling_bms}
    carried: list[VM] = [_normalize_existing(vm) for vm in request.existing_vms]

    acc_aa, acc_mpb, acc_excl, acc_fo = [], [], [], []
    reports: list[RolloutStepReport] = []
    failed_step: str | None = None

    for step in request.steps:
        if failed_step is not None:
            reports.append(RolloutStepReport(
                name=step.name,
                success=False,
                solver_status=(
                    f"BLOCKED: not simulated — step '{failed_step}' failed"
                ),
            ))
            continue

        acc_aa.extend(step.anti_affinity_rules)
        acc_mpb.extend(step.max_per_bm_rules)
        acc_excl.extend(step.exclusive_bm_rules)
        acc_fo.extend(step.failover_rules)

        step_request = SplitPlacementRequest(
            requirements=step.requirements,
            vms=carried + step.vms,
            baremetals=rolling_bms,
            anti_affinity_rules=acc_aa,
            max_per_bm_rules=acc_mpb,
            exclusive_bm_rules=acc_excl,
            failover_rules=acc_fo,
            config=request.config,
        )
        result, synthetics = solve_split_placement_with_synthetics(step_request)

        explicit_of = {vm.id: vm for vm in step.vms}
        synth_of = {vm.id: vm for vm in synthetics}
        carried_ids = {vm.id for vm in carried}

        # A failed solve lists EVERY request VM as unplaced — but carried
        # pins were placed by earlier steps; only this step's own VMs
        # belong in the step report (synthetics namespaced like elsewhere).
        unplaced = [
            u if u in explicit_of else f"{step.name}/{u}"
            for u in result.unplaced_vms
            if u not in carried_ids
        ]

        new_assignments: list[PlacementAssignment] = []
        for a in result.assignments:
            if a.pinned:
                continue  # carried from an earlier step / existing_vms
            if a.vm_id in explicit_of:
                new_assignments.append(a)
            else:
                # synthetic: namespace the id for rollout-wide uniqueness
                new_assignments.append(
                    a.model_copy(update={"vm_id": f"{step.name}/{a.vm_id}"})
                )

        reports.append(RolloutStepReport(
            name=step.name,
            success=result.success,
            solver_status=result.solver_status,
            new_assignments=new_assignments,
            split_decisions=result.split_decisions,
            unplaced_vms=unplaced,
            bm_used_count=result.bm_used_count,
            bm_total_count=result.bm_total_count,
            solve_time_seconds=result.solve_time_seconds,
            diagnostics=result.diagnostics,
        ))

        if not result.success:
            failed_step = step.name
            continue

        # Fold forward: pin this step's placements and advance the ledger.
        for a in new_assignments:
            if a.vm_id in explicit_of:
                src = explicit_of[a.vm_id]
                new_id = a.vm_id
            else:
                orig_id = a.vm_id[len(step.name) + 1:]
                src = synth_of[orig_id]
                new_id = a.vm_id
            carried.append(src.model_copy(update={
                "id": new_id,
                "pinned_to": a.baremetal_id,
                "candidate_baremetals": [a.baremetal_id],
            }))
            host = bm_by_id[a.baremetal_id]
            host.used_capacity = host.used_capacity + src.demand

    logger.info(
        "Rollout: %d steps simulated in %.2fs, failed_step=%s",
        len(request.steps), time.time() - start, failed_step,
    )
    return RolloutResult(
        success=failed_step is None,
        reports=reports,
        failed_step=failed_step,
        final_baremetals=rolling_bms,
        config_fingerprint=fingerprint,
    )


def _normalize_existing(vm: VM) -> VM:
    """Empty candidates → [pinned_to]; validated non-empty ones pass as-is."""
    if not vm.candidate_baremetals:
        return vm.model_copy(update={"candidate_baremetals": [vm.pinned_to]})
    return vm


def _validate(request: RolloutRequest) -> list[str]:
    """
    Rollout-level contract checks. Everything here is a violation the
    underlying solver either cannot see (cross-step id references) or
    would report with a misleading step-local message.
    """
    errors: list[str] = []
    bm_ids = {bm.id for bm in request.baremetals}

    if not request.steps:
        errors.append("rollout requires at least one step")

    seen_names: set[str] = set()
    for step in request.steps:
        if not step.name:
            errors.append("every rollout step needs a non-empty name")
        elif step.name in seen_names:
            errors.append(f"duplicate step name '{step.name}'")
        else:
            seen_names.add(step.name)
        if not step.vms and not step.requirements:
            errors.append(
                f"step '{step.name}' has neither vms nor requirements"
            )

    # Global explicit-id uniqueness (synthetic ids are namespaced per step).
    known_ids: set[str] = set()
    for vm in request.existing_vms:
        if vm.id in known_ids:
            errors.append(f"duplicate VM id '{vm.id}' in existing_vms")
        known_ids.add(vm.id)
        if vm.pinned_to is None:
            errors.append(
                f"existing VM '{vm.id}' has no pinned_to — existing_vms "
                f"describe machines that are already placed"
            )
        elif vm.pinned_to not in bm_ids:
            errors.append(
                f"existing VM '{vm.id}' is pinned to unknown BM "
                f"'{vm.pinned_to}' — its host must be in baremetals"
            )
        elif vm.candidate_baremetals and vm.pinned_to not in vm.candidate_baremetals:
            errors.append(
                f"existing VM '{vm.id}': candidate_baremetals does not "
                f"contain its host '{vm.pinned_to}'"
            )

    # vm_ids rules may only reference ids introduced by this step or
    # earlier — the solver silently drops unknown vm_ids, which for C6
    # means the protection would evaporate without a sound.
    for step in request.steps:
        for vm in step.vms:
            if vm.id in known_ids:
                errors.append(
                    f"VM id '{vm.id}' in step '{step.name}' collides with "
                    f"an earlier step or existing_vms"
                )
            known_ids.add(vm.id)
        for kind, rules in (
            ("anti_affinity", step.anti_affinity_rules),
            ("max_per_bm", step.max_per_bm_rules),
            ("exclusive_bm", step.exclusive_bm_rules),
        ):
            for rule in rules:
                unknown = [v for v in rule.vm_ids if v not in known_ids]
                if unknown:
                    errors.append(
                        f"step '{step.name}' {kind} rule '{rule.group_id}' "
                        f"references unknown VM ids {sorted(unknown)} — "
                        f"vm_ids may only name explicit VMs from this or "
                        f"earlier steps (use a selector to match synthetic "
                        f"or future VMs)"
                    )

    return errors
