"""
Rollout sizing — "how many machines does this build order need?" (ADR-014).

Greenfield fab planning: the caller describes a machine model plus topology
counts and an ordered list of build steps, with NO fleet. This module finds
the fewest machines that let every step place, by generating a fleet and
running the real `solve_rollout` on it.

Why a search and not one clever solve: the answer is a SEQUENTIAL-build
number. Merging every step into a single joint solve (what the capacity
planner and mockgen's elastic sizing do) lets the solver place everything
at once and find a tighter packing than any staged build can achieve — a
valid lower bound, not the answer. Only replaying the order reveals the
fragmentation that earlier steps inflict on later ones.

Why the search is a LINEAR ascending scan and not a bisect: feasibility is
not monotone in fleet size, so bisect's precondition does not hold.
ADR-008 §2 already rejected bisect for mockgen's elastic sizing on the
bucket-count argument; a rollout adds a second, stronger reason — the
simulation commits ONE optimal solution per step and pins it forward, so
a larger fleet can shift step k's arbitrary choice and break step k+1.
Scanning upward from a provable floor is both simpler and exact: every
size below the answer was either ruled out by the floor or actually tried.
"""

from __future__ import annotations

import logging
import time

from .models import (
    Baremetal,
    FleetTemplate,
    Resources,
    RolloutRequest,
    RolloutSizingRequest,
    RolloutSizingResult,
    RolloutStep,
    SizingProbe,
    Topology,
    config_fingerprint,
)
from .rollout import solve_rollout
from .sizing_floors import fleet_floor

logger = logging.getLogger(__name__)

# Spread dimensions a fleet template can collapse to a single bucket.
_TEMPLATE_DIMS = {
    "site": "sites", "phase": "phases", "datacenter": "datacenters",
    "room": "rooms", "rack": "racks", "ag": "ags",
}


def build_rack_topologies(fleet: FleetTemplate) -> list[Topology]:
    """
    One Topology per rack; every dimension above rack derived by modulo over
    the rack ordinal, so racks fan out across sites, rooms and AGs at once.
    Mirrors mockgen's `_build_racks` (and the rollout UI's generator), and
    keeps the invariant that a rack belongs to exactly one AG.
    """
    return [
        Topology(
            site=f"site-{r % fleet.sites + 1}",
            phase=f"p{r % fleet.phases + 1}",
            datacenter=f"dc-{r % fleet.datacenters + 1}",
            room=f"room-{r % fleet.rooms + 1}",
            rack=f"rack-{r + 1}",
            ag=f"ag-{r % fleet.ags + 1}",
        )
        for r in range(fleet.racks)
    ]


def build_fleet(fleet: FleetTemplate, n: int) -> list[Baremetal]:
    """`n` identical machines round-robined over the rack skeleton.

    Ids are positional and stable, so the same n always yields the same
    fleet — probes are reproducible and comparable.
    """
    racks = build_rack_topologies(fleet)
    out = []
    for i in range(n):
        seq = f"{i + 1:03d}"
        out.append(Baremetal(
            id=f"bm-{seq}",
            hostname=f"bare-{seq}",
            total_capacity=fleet.total_capacity,
            used_capacity=Resources(),
            topology=racks[i % len(racks)],
            network=fleet.network,
            pool=fleet.pool,
        ))
    return out


def per_ag_counts(baremetals: list[Baremetal]) -> dict[str, int]:
    """Machines per AG, counted from the fleet rather than re-derived."""
    counts: dict[str, int] = {}
    for bm in baremetals:
        counts[bm.topology.ag] = counts.get(bm.topology.ag, 0) + 1
    return dict(sorted(counts.items()))


def _collapsed_dims(fleet: FleetTemplate) -> set[str]:
    """Dimensions the template renders as a single bucket."""
    return {
        dim for dim, knob in _TEMPLATE_DIMS.items()
        if getattr(fleet, knob) <= 1
    }


def _validate(request: RolloutSizingRequest) -> list[str]:
    """
    Contract checks plus the "infeasible at every size" traps.

    The traps matter: without them a request that can never place burns the
    whole probe budget climbing to max_baremetals before giving up, and the
    report would blame fleet size for a modelling mistake.
    """
    errors: list[str] = []
    fleet = request.fleet
    cap = fleet.total_capacity
    collapsed = _collapsed_dims(fleet)

    if not request.steps:
        errors.append("sizing requires at least one step")

    for step in request.steps:
        for vm in step.vms:
            if vm.pinned_to is not None:
                errors.append(
                    f"VM '{vm.id}' in step '{step.name}' is pinned — sizing "
                    f"is greenfield only (no fleet exists to pin to)"
                )
            if vm.candidate_baremetals:
                errors.append(
                    f"VM '{vm.id}' in step '{step.name}' pre-sets "
                    f"candidate_baremetals — the fleet is generated per probe, "
                    f"so candidates must be left empty"
                )
            if not vm.demand.fits_in(cap):
                errors.append(
                    f"VM '{vm.id}' in step '{step.name}' does not fit the "
                    f"machine model — no fleet size can place it"
                )
        for req in step.requirements:
            if req.candidate_baremetals:
                errors.append(
                    f"requirement '{req.cluster_id}/{req.node_role}' in step "
                    f"'{step.name}' pre-sets candidate_baremetals — the fleet "
                    f"is generated per probe, so candidates must be left empty"
                )
            if req.network not in ("", fleet.network):
                errors.append(
                    f"requirement '{req.cluster_id}/{req.node_role}' in step "
                    f"'{step.name}' wants network '{req.network}' but the "
                    f"fleet is '{fleet.network or ''}' — it would match no "
                    f"machine at any size"
                )
            specs = req.vm_specs if req.vm_specs is not None else request.config.vm_specs
            usable = [s for s in (specs or []) if s.fits_in(cap)]
            if specs and not usable:
                errors.append(
                    f"requirement '{req.cluster_id}/{req.node_role}' in step "
                    f"'{step.name}' has no vm_spec that fits the machine model"
                )
        for rule in step.failover_rules:
            if rule.fault_domain in collapsed:
                errors.append(
                    f"failover rule '{rule.rule_id}' spreads on "
                    f"'{rule.fault_domain}', which this fleet template "
                    f"collapses to one bucket — unsatisfiable at any size"
                )
        for rule in step.anti_affinity_rules:
            for dim in rule.spread_on:
                if dim in collapsed and len(rule.vm_ids or []) > 1:
                    errors.append(
                        f"anti-affinity rule '{rule.group_id}' spreads on "
                        f"'{dim}', which this fleet template collapses to one "
                        f"bucket — unsatisfiable at any size"
                    )
                    break
    return errors


def _strip_candidates(steps: list[RolloutStep], bm_ids: list[str]) -> list[RolloutStep]:
    """Point every VM and requirement at this probe's fleet."""
    out = []
    for step in steps:
        out.append(step.model_copy(update={
            "vms": [vm.model_copy(update={"candidate_baremetals": bm_ids})
                    for vm in step.vms],
            "requirements": [r.model_copy(update={"candidate_baremetals": bm_ids})
                             for r in step.requirements],
        }))
    return out


def _probe_status(result) -> tuple[str, str | None]:
    """The status that explains a probe, and which step it came from.

    `RolloutResult.solver_status` is only populated for request-level
    INPUT_ERROR (contract); a simulated run keeps per-step statuses.
    """
    if result.solver_status:
        return result.solver_status, None
    for report in result.reports:
        if not report.success:
            return report.solver_status, report.name
    return "OPTIMAL", None


def size_rollout(request: RolloutSizingRequest) -> RolloutSizingResult:
    """Find the smallest fleet that lets the whole build order place."""
    start = time.time()
    fingerprint = config_fingerprint(request.config)

    errors = _validate(request)
    if errors:
        for err in errors:
            logger.error("Sizing input validation failed: %s", err)
        return RolloutSizingResult(
            success=False,
            solver_status=f"INPUT_ERROR: {errors[0]}",
            config_fingerprint=fingerprint,
            diagnostics={"input_errors": errors},
        )

    floor, breakdown = fleet_floor(
        request.steps, request.fleet.total_capacity, request.config,
        request.fleet.ags,
    )
    diagnostics: dict[str, object] = {}
    if request.fleet.ags < request.config.target_spread.get("ag", 0):
        diagnostics["advisories"] = [{
            "type": "fleet_ags_below_target",
            "severity": "warning",
            "message": (
                f"Fleet spreads over {request.fleet.ags} AG(s) but "
                f"target_spread['ag'] is {request.config.target_spread['ag']}; "
                f"sizing answers the question as asked and does not widen it."
            ),
        }]

    probes: list[SizingProbe] = []
    n = floor
    last_failure = floor - 1
    while True:
        if n > request.max_baremetals:
            return _exhausted(request, probes, floor, breakdown, last_failure,
                              fingerprint, diagnostics,
                              f"fleet would exceed max_baremetals={request.max_baremetals}")
        if len(probes) >= request.max_probes:
            return _exhausted(request, probes, floor, breakdown, last_failure,
                              fingerprint, diagnostics,
                              f"probe budget spent (max_probes={request.max_probes})")
        remaining = request.deadline_seconds - (time.time() - start)
        if remaining <= 0:
            return _exhausted(request, probes, floor, breakdown, last_failure,
                              fingerprint, diagnostics, "deadline reached")

        baremetals = build_fleet(request.fleet, n)
        # Share what's left of the deadline across the steps of this probe.
        per_solve = max(1.0, remaining / max(1, len(request.steps)))
        config = request.config.model_copy(update={
            "max_solve_time_seconds": min(
                request.config.max_solve_time_seconds, per_solve,
            ),
        })
        probe_start = time.time()
        result = solve_rollout(RolloutRequest(
            baremetals=baremetals,
            steps=_strip_candidates(request.steps, [b.id for b in baremetals]),
            config=config,
        ))
        status, failed_step = _probe_status(result)
        probes.append(SizingProbe(
            baremetals=n, success=result.success, solver_status=status,
            failed_step=failed_step,
            elapsed_seconds=round(time.time() - probe_start, 3),
        ))
        logger.info("Sizing probe: %d BMs → %s", n, status)

        if result.success:
            return RolloutSizingResult(
                success=True,
                required_baremetals=n,
                per_ag=per_ag_counts(baremetals),
                analytic_floor=floor,
                floor_breakdown=breakdown,
                lower_bound=n,
                upper_bound=n,
                probes=probes,
                rollout=result,
                baremetals=baremetals,
                config_fingerprint=fingerprint,
                diagnostics=diagnostics,
            )

        if status.startswith("UNKNOWN"):
            # A timeout is not a proof of infeasibility; climbing further
            # would only make the model slower and the answer more wrong.
            diagnostics["stopped_on"] = (
                f"solver returned UNKNOWN at {n} baremetals — raise "
                f"max_solve_time_seconds or shrink the rollout"
            )
            return _exhausted(request, probes, floor, breakdown, last_failure,
                              fingerprint, diagnostics, "solver timed out")
        if status.startswith("INPUT_ERROR"):
            # Not about fleet size at all — surface it verbatim.
            return RolloutSizingResult(
                success=False,
                solver_status=status,
                analytic_floor=floor,
                floor_breakdown=breakdown,
                probes=probes,
                config_fingerprint=fingerprint,
                diagnostics=diagnostics,
            )

        last_failure = n
        n += 1


def _exhausted(request, probes, floor, breakdown, last_failure,
               fingerprint, diagnostics, reason) -> RolloutSizingResult:
    """Budget ran out: report the bracket instead of a bare failure."""
    return RolloutSizingResult(
        success=False,
        solver_status=f"BUDGET_EXHAUSTED: {reason}",
        analytic_floor=floor,
        floor_breakdown=breakdown,
        lower_bound=max(floor, last_failure + 1),
        upper_bound=None,
        probes=probes,
        config_fingerprint=fingerprint,
        diagnostics=diagnostics,
    )
