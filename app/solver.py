"""
VM Placement Solver — Step 1: Hard Constraints Only

This is the minimal viable solver. It answers the question:
  "Is there ANY valid way to assign these VMs to these baremetals?"

What it does:
  1. Each VM is assigned to exactly one baremetal
  2. Baremetal capacity is not exceeded (cpu, mem, disk, gpu)
  3. Anti-affinity rules are respected (max N VMs per AG)
  4. Candidate lists from step 3 are respected

What it does NOT do yet (we'll add these step by step):
  - No objective function (any feasible solution is returned)
  - No optimization (no preference for "better" placements)

HOW CP-SAT WORKS (brief primer):
  CP-SAT is a constraint programming solver. You tell it:
    - Variables: things that can take different values
    - Constraints: rules the variables must satisfy
    - Objective (optional): what to minimize/maximize
  It then searches for variable assignments that satisfy all constraints.

  In our case:
    - Variables: assign[vm_i, bm_j] = 0 or 1 (boolean)
    - Constraints: capacity limits, one-BM-per-VM, anti-affinity
    - Objective: none yet (just find any feasible solution)
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict

from ortools.sat.python import cp_model

from .models import (
    SPREAD_DIMENSIONS,
    VM,
    AntiAffinityRule,
    Baremetal,
    FailoverRule,
    GroupSelector,
    MaxPerBaremetalRule,
    PlacementAssignment,
    PlacementRequest,
    PlacementResult,
    Resources,
)

logger = logging.getLogger(__name__)

# The resource fields we check for capacity constraints.
RESOURCE_FIELDS = ["cpu_cores", "memory_mib", "storage_gb", "gpu_count"]


def get_eligible_baremetals(
    vm: VM,
    bm_map: dict[str, Baremetal],
    baremetals: list[Baremetal],
) -> list[str]:
    """
    Which baremetals can this VM possibly go on?

    The Go scheduler must populate vm.candidate_baremetals with the result
    of step 3 filtering. An empty candidate list is a contract violation
    that is rejected upstream by VMPlacementSolver input validation
    (see __init__) — by the time we get here, vm.candidate_baremetals is
    guaranteed non-empty for any VM that reaches the solver.

    This is a module-level function so both the solver and diagnostics
    can share the same eligibility logic.

    Pinned VMs (upgrade workflows): a VM with pinned_bm is already running
    on that BM — its only eligible BM is the pinned one, candidate_baremetals
    is ignored, and neither the fits check nor the schedulable filter applies
    (the VM is physically there; cordoning blocks NEW VMs only).

    Non-pinned VMs skip unschedulable (cordoned) BMs. The fits check stays
    against available_capacity (total - used): used_capacity includes pinned
    demand by contract, and under transitional C2 the space a new VM can
    claim is exactly total - used — pinned demand cancels out of both sides.
    """
    if vm.pinned_bm is not None:
        return [vm.pinned_bm] if vm.pinned_bm in bm_map else []
    return [
        bm_id
        for bm_id in vm.candidate_baremetals
        if bm_id in bm_map
        and bm_map[bm_id].schedulable
        and vm.demand.fits_in(bm_map[bm_id].available_capacity)
    ]


def pinned_count_in_bucket(
    vm_ids: list[str],
    bm_ids: set[str] | frozenset[str],
    vm_map: dict[str, VM],
) -> int:
    """
    How many of these VMs are pinned to a BM inside this bucket?

    Pinned assignments are fixed (C6), so this count is a constant floor on
    the bucket's occupancy. C3/C4/C5 builders — and the diagnostics layer
    check, which must mirror them — use it to relax per-bucket caps so a
    pre-existing layout can never make the model INFEASIBLE ("don't worsen,
    but don't fail on what the solver cannot move").
    """
    count = 0
    for vid in vm_ids:
        vm = vm_map.get(vid)
        if vm is not None and vm.pinned_bm is not None and vm.pinned_bm in bm_ids:
            count += 1
    return count


class VMPlacementSolver:

    def __init__(
        self,
        request: PlacementRequest,
        *,
        model: cp_model.CpModel | None = None,
        active_vars: dict[str, cp_model.IntVar] | None = None,
    ):
        self.request = request
        self.config = request.config
        self.active_vars: dict[str, cp_model.IntVar] = active_vars or {}

        # Lookup maps for quick access
        self.vm_map: dict[str, VM] = {vm.id: vm for vm in request.vms}
        self.bm_map: dict[str, Baremetal] = {bm.id: bm for bm in request.baremetals}

        # Validate: no duplicate baremetals allowed.
        # Deduplication is the scheduler's responsibility — solver only detects
        # and rejects invalid input so the scheduler can fix the bug upstream.
        self._input_errors: list[str] = []
        seen_bm_ids: set[str] = set()
        for bm in request.baremetals:
            if bm.id in seen_bm_ids:
                self._input_errors.append(f"duplicate BM '{bm.id}' in baremetals list")
            else:
                seen_bm_ids.add(bm.id)

        for vm in request.vms:
            if vm.pinned_bm is not None:
                # Pinned VMs: candidate_baremetals is ignored (eligibility is
                # the pinned BM itself), so candidate checks don't apply.
                continue
            if not vm.candidate_baremetals:
                self._input_errors.append(
                    f"VM '{vm.id}' has empty candidate_baremetals — "
                    f"scheduler must provide step 3 filtering result"
                )
                continue
            seen_candidates: set[str] = set()
            for cand in vm.candidate_baremetals:
                if cand in seen_candidates:
                    self._input_errors.append(
                        f"duplicate candidate BM '{cand}' in VM '{vm.id}'"
                    )
                else:
                    seen_candidates.add(cand)

        # Validate upgrade-workflow fields (lifecycle / pinning / replaces).
        # All contract violations are INPUT_ERROR — never silently fixed.
        for vm in request.vms:
            if vm.pinned_bm is not None:
                if vm.pinned_bm not in self.bm_map:
                    self._input_errors.append(
                        f"VM '{vm.id}' is pinned to unknown BM '{vm.pinned_bm}'"
                    )
                if vm.lifecycle == "new":
                    self._input_errors.append(
                        f"VM '{vm.id}' has pinned_bm but lifecycle='new' — "
                        f"existing VMs must be 'keep' or 'to_be_removed'"
                    )
            elif vm.lifecycle != "new":
                self._input_errors.append(
                    f"VM '{vm.id}' has lifecycle='{vm.lifecycle}' but no "
                    f"pinned_bm — existing VMs must say where they are"
                )
            if vm.eviction_blocked and vm.lifecycle != "keep":
                self._input_errors.append(
                    f"VM '{vm.id}' has eviction_blocked=True but "
                    f"lifecycle='{vm.lifecycle}' — a PDB-blocked VM must be "
                    f"kept (scheduler decides removal only after the user "
                    f"adjusts the PDB or disables eviction)"
                )
            if vm.replaces is not None:
                target = self.vm_map.get(vm.replaces)
                if target is None:
                    self._input_errors.append(
                        f"VM '{vm.id}' replaces unknown VM '{vm.replaces}'"
                    )
                elif target.lifecycle != "to_be_removed":
                    self._input_errors.append(
                        f"VM '{vm.id}' replaces '{vm.replaces}' whose "
                        f"lifecycle is '{target.lifecycle}', expected "
                        f"'to_be_removed'"
                    )
                if vm.lifecycle != "new":
                    self._input_errors.append(
                        f"VM '{vm.id}' has replaces but lifecycle="
                        f"'{vm.lifecycle}' — only new VMs replace old ones"
                    )

        # Group baremetals by each topology dimension (needed for multi-dim
        # anti-affinity constraints C3 and failover constraints C5).
        # Shape: dim_to_bms[dim_name][bucket_value] = [bm_id, ...]
        self.dim_to_bms: dict[str, dict[str, list[str]]] = {}
        for dim in SPREAD_DIMENSIONS:
            buckets: dict[str, list[str]] = defaultdict(list)
            for bm in self.request.baremetals:
                buckets[getattr(bm.topology, dim)].append(bm.id)
            self.dim_to_bms[dim] = dict(buckets)

        # Non-fatal advisories collected during rule resolution (e.g. policy
        # target not met). Surfaced via PlacementResult.diagnostics["advisories"].
        self.advisories: list[dict] = []

        # Pinned occupancy and effective capacity (upgrade workflows).
        # Contract: used_capacity INCLUDES pinned VM demand. The model
        # re-adds pinned demand through fixed assign vars (C6), so every
        # model-facing capacity read must use effective_used/-available
        # (= aggregate usage by VMs NOT listed in the request) or pinned
        # demand would be counted twice. The drift check below is the
        # reason this contract direction was chosen: the solver can verify
        # inventory vs VM-list consistency; the reverse direction cannot.
        self.pinned_vms_by_bm: dict[str, list[VM]] = defaultdict(list)
        for vm in request.vms:
            if vm.pinned_bm is not None and vm.pinned_bm in self.bm_map:
                self.pinned_vms_by_bm[vm.pinned_bm].append(vm)

        self.effective_used: dict[str, Resources] = {}
        self.effective_available: dict[str, Resources] = {}
        for bm in request.baremetals:
            pinned_demand = Resources()
            for vm in self.pinned_vms_by_bm.get(bm.id, []):
                pinned_demand = pinned_demand + vm.demand
            eff_used = bm.used_capacity - pinned_demand
            negative = [
                field for field in RESOURCE_FIELDS
                if getattr(eff_used, field) < 0
            ]
            if negative:
                self._input_errors.append(
                    f"BM '{bm.id}': pinned VM demand exceeds used_capacity "
                    f"on {negative} — inventory and pinned VM list disagree"
                )
            self.effective_used[bm.id] = eff_used
            self.effective_available[bm.id] = bm.total_capacity - eff_used

        # PDB / eviction advisory: a cordoned BM that still hosts VMs the
        # scheduler must keep can never be fully evicted for its upgrade.
        # Emitted here (not in a builder) so it rides every return path.
        for bm in request.baremetals:
            if bm.schedulable:
                continue
            keep_vms = [
                vm for vm in self.pinned_vms_by_bm.get(bm.id, [])
                if vm.lifecycle == "keep"
            ]
            if not keep_vms:
                continue
            blocked_ids = [vm.id for vm in keep_vms if vm.eviction_blocked]
            self.advisories.append({
                "type": "bm_not_evictable",
                "severity": "warning",
                "message": (
                    f"BM '{bm.id}' is unschedulable but still hosts "
                    f"{len(keep_vms)} kept VM(s) — it cannot be emptied"
                    + (
                        f"; {len(blocked_ids)} blocked by PDB"
                        if blocked_ids else ""
                    )
                ),
                "details": {
                    "bm_id": bm.id,
                    "blocking_vm_ids": [vm.id for vm in keep_vms],
                    "eviction_blocked_vm_ids": blocked_ids,
                },
            })

        # Validate selector/vm_ids exclusivity on incoming rules (fatal).
        self._validate_rule_inputs()

        # Validate per-BM config (fatal): auto-gen requires a positive default.
        if self.config.auto_generate_max_per_bm:
            d = self.config.default_max_per_bm
            if d is None or d < 1:
                self._input_errors.append(
                    "auto_generate_max_per_bm=True requires "
                    "default_max_per_bm to be a positive integer"
                )

        # Resolve anti-affinity rules (explicit + auto-generated)
        self.effective_rules = self._resolve_anti_affinity_rules()

        # Resolve per-baremetal rules (explicit + auto-generated)
        self.max_per_bm_rules: list[MaxPerBaremetalRule] = self._resolve_max_per_bm_rules()

        # Resolve failover rules (expand selectors into concrete VM-id lists).
        # Pre-flight check (|P| > |L| under n_minus_1 → INPUT_ERROR) lives here.
        self.failover_resolved: list[tuple[FailoverRule, list[str], list[str]]] = (
            self._resolve_failover_rules()
        )

        # Waste penalty terms injected by split_solver (splitter integration)
        self.splitter_waste_terms: list[cp_model.LinearExprT] = []

        # Buyable BM ids injected by capacity_planner (procurement integration).
        # Using any of these BMs is penalized by config.w_procurement so the
        # solver fills in-stock first and buys the minimum. (Phase 2)
        self.procurement_bm_ids: set[str] = set()

        # Cardinality caps over groups of BMs, injected by capacity_planner:
        # for each (bm_ids, cap), at most `cap` of those BMs may be used.
        # Carries the per-bucket max_bm slot limit across machine types, and
        # the "at most `count` of a floating committed-stock pool" bound.
        self.bm_group_caps: list[tuple[set[str], int]] = []

        # Committed-stock BM ids (already purchased, awaiting allocation).
        # Penalized by w_committed_stock — far below w_procurement — so the
        # preference order is: in-stock, then committed, then buy new.
        self.committed_bm_ids: set[str] = set()

        # The CP-SAT model — shared with splitter when called from split_solver
        self.model = model if model is not None else cp_model.CpModel()

        # Decision variables: assign[(vm_id, bm_id)] = BoolVar
        # Only created for eligible (vm, bm) pairs — this is important
        # because it means we never even consider impossible assignments.
        self.assign: dict[tuple[str, str], cp_model.IntVar] = {}

        # Objective helper: bm_used[bm_id] = 1 if any VM is placed on that BM
        self.bm_used: dict[str, cp_model.IntVar] = {}

    # ------------------------------------------------------------------
    # Step A: Determine which (VM, BM) pairs are eligible
    # ------------------------------------------------------------------

    def _get_eligible_baremetals(self, vm: VM) -> list[str]:
        """Delegate to module-level function for reuse by diagnostics."""
        return get_eligible_baremetals(vm, self.bm_map, self.request.baremetals)

    # ------------------------------------------------------------------
    # Step B: Rule input validation + selector expansion (shared C3/C4)
    # ------------------------------------------------------------------

    def _validate_rule_inputs(self) -> None:
        """
        Each AntiAffinity / MaxPerBaremetal rule must specify exactly one of
        `vm_ids` or `selector`. Empty selectors (all fields None) are rejected
        — they would silently match every VM and almost always indicate a bug.
        """
        def check(rule: AntiAffinityRule | MaxPerBaremetalRule, kind: str) -> None:
            has_vm_ids = bool(rule.vm_ids)
            has_selector = rule.selector is not None
            if has_vm_ids and has_selector:
                self._input_errors.append(
                    f"{kind} rule '{rule.group_id}': specify either vm_ids "
                    f"or selector, not both"
                )
            elif not has_vm_ids and not has_selector:
                self._input_errors.append(
                    f"{kind} rule '{rule.group_id}': must specify vm_ids or selector"
                )
            elif rule.selector is not None and rule.selector.is_empty():
                self._input_errors.append(
                    f"{kind} rule '{rule.group_id}': selector has no fields set"
                )

        for aa_rule in self.request.anti_affinity_rules:
            check(aa_rule, "anti_affinity")
        for bm_rule in self.request.max_per_bm_rules:
            check(bm_rule, "max_per_bm")
            if bm_rule.max_per_bm < 1:
                self._input_errors.append(
                    f"max_per_bm rule '{bm_rule.group_id}': max_per_bm must be >= 1"
                )

    def _expand_vm_ids(self, rule) -> list[str]:
        """
        Resolve a rule's group membership.

        - vm_ids form: returned as-is (deduplicated, preserving order)
        - selector form: matched against self.request.vms; missing fields
          are wildcards. Unknown vm_ids are silently dropped (caller may
          submit synthetic IDs that aren't yet in the VM list — that's a
          contract error caught elsewhere, not here).
        - Malformed rules (both/neither vm_ids and selector) return [];
          they've already been recorded in self._input_errors which causes
          solve() to abort with INPUT_ERROR before any constraint is added.

        FINAL-STATE semantics (upgrade workflows): to_be_removed VMs are
        excluded on both forms — C3/C4/C5 constrain the state after the
        drain completes, and group sizes (⌈N/buckets⌉) must not be
        inflated by VMs that are on their way out.
        """
        if rule.vm_ids:
            seen: set[str] = set()
            out: list[str] = []
            for vid in rule.vm_ids:
                if vid in seen:
                    continue
                seen.add(vid)
                vm = self.vm_map.get(vid)
                if vm is not None and not vm.in_final_state:
                    continue
                out.append(vid)
            return out
        if rule.selector is None:
            return []
        sel: GroupSelector = rule.selector
        return [
            vm.id for vm in self.request.vms
            if vm.in_final_state and sel.matches(vm)
        ]

    # ------------------------------------------------------------------
    # Step B (cont.): Auto-generate anti-affinity rules
    # ------------------------------------------------------------------

    def _resolve_anti_affinity_rules(self) -> list[AntiAffinityRule]:
        """
        Combine explicit rules with auto-generated ones.

        Auto-generation: group VMs by (cluster_id, ip_type, node_role) and
        for each group with 2+ VMs, create a rule that spreads them across
        each dimension in `config.target_spread.keys()` (typically ["ag"]
        but may include "room", etc.). Including cluster_id is what makes
        multi-cluster HA correct — each cluster's masters/workers spread
        independently rather than being pooled into a single budget.

        Per-bucket caps are auto-computed at constraint time as
        ⌈N / |buckets(d)|⌉ for each dimension d in spread_on; no explicit
        cap_per_bucket is set on auto rules.

        VMs already covered by explicit rules are not auto-generated.
        VMs with empty cluster_id or ip_type are skipped (can't group
        meaningfully).
        """
        # Canonicalize explicit rules: expand selectors into vm_ids form so
        # downstream constraint building doesn't need to know about selectors.
        rules: list[AntiAffinityRule] = []
        for r in self.request.anti_affinity_rules:
            resolved_ids = self._expand_vm_ids(r)
            rules.append(AntiAffinityRule(
                group_id=r.group_id,
                vm_ids=resolved_ids,
                spread_on=list(r.spread_on),
                cap_per_bucket=(dict(r.cap_per_bucket) if r.cap_per_bucket else None),
            ))

        if not self.config.auto_generate_anti_affinity:
            return rules

        # Auto-gen spread_on follows the configured target dimensions.
        # Sorted for deterministic logging and rule construction.
        auto_spread_dims = sorted(self.config.target_spread.keys())

        # Which VMs are already in explicit rules?
        covered: set[str] = set()
        for rule in rules:
            covered.update(rule.vm_ids)

        # Group remaining VMs by (cluster_id, ip_type, role). Cluster is part of
        # the key so two clusters with the same role/ip_type spread independently.
        # Final-state membership: to_be_removed VMs never join a group; pinned
        # "keep" VMs join like any other — a new master must avoid the bucket
        # where a kept master already sits.
        groups: dict[tuple[str, str, str], list[str]] = defaultdict(list)
        for vm in self.request.vms:
            if vm.id in covered:
                continue
            if not vm.in_final_state:
                continue
            if not vm.ip_type or not vm.cluster_id:
                continue
            groups[(vm.cluster_id, vm.ip_type, vm.node_role.value)].append(vm.id)

        for (cluster_id, ip_type, role), vm_ids in groups.items():
            if len(vm_ids) < 2 or not auto_spread_dims:
                continue

            has_synthetic = any(vid in self.active_vars for vid in vm_ids)
            group_id = f"auto/{cluster_id}/{ip_type}/{role}"
            rules.append(AntiAffinityRule(
                group_id=group_id,
                vm_ids=vm_ids,
                spread_on=auto_spread_dims,
            ))
            logger.info(
                "Auto anti-affinity: %s/%s/%s (%d VMs%s → spread_on=%s)",
                cluster_id, ip_type, role, len(vm_ids),
                " inc. synthetic" if has_synthetic else "",
                auto_spread_dims,
            )

            # Per-dimension policy check: emit one spread_below_target
            # advisory per dimension that fails to meet target_spread[d].
            for dim in auto_spread_dims:
                target = self.config.target_spread[dim]
                buckets = self.dim_to_bms.get(dim, {})
                num_buckets = len(buckets)
                # Effective spread = min(infra buckets, group size).
                # For synthetic groups this is an upper-bound estimate.
                effective_spread = min(num_buckets, len(vm_ids))
                if effective_spread < target:
                    msg = (
                        f"Anti-affinity for {cluster_id}/{ip_type}/{role} below "
                        f"policy target on {dim}: actual spread={effective_spread}, "
                        f"target={target} ({num_buckets} bucket(s), {len(vm_ids)} VMs)."
                    )
                    self.advisories.append({
                        "type": "spread_below_target",
                        "severity": "warning",
                        "group_id": group_id,
                        "message": msg,
                        "details": {
                            "dimension": dim,
                            "vm_count": len(vm_ids),
                            "num_buckets": num_buckets,
                            "effective_spread": effective_spread,
                            "target_spread": target,
                            "bucket_names": sorted(buckets.keys()),
                        },
                    })
                    logger.warning("Spread advisory: %s", msg)

        return rules

    # ------------------------------------------------------------------
    # Step B (cont.): Failover rules — C5 resolve
    # ------------------------------------------------------------------

    def _resolve_failover_rules(
        self,
    ) -> list[tuple[FailoverRule, list[str], list[str]]]:
        """
        Materialize each FailoverRule into (rule, primary_vm_ids, backup_vm_ids).

        Pre-flight check: under policy `n_minus_1`, |primary| > |backup| can
        never satisfy the redundancy invariant (some primary will lack a
        backup partner after the worst-case bucket failure). Such cases are
        recorded as INPUT_ERROR so the scheduler can correct upstream.

        Returns only well-formed rules; ill-formed ones are reported via
        self._input_errors and excluded from constraint building.
        """
        resolved: list[tuple[FailoverRule, list[str], list[str]]] = []
        # Final-state membership: a to_be_removed backup must not count
        # toward |backup| — after the drain it can't take over anything.
        final_vms = [vm for vm in self.request.vms if vm.in_final_state]
        for f in self.request.failover_rules:
            primary_ids = [vm.id for vm in final_vms if f.primary.matches(vm)]
            backup_ids = [vm.id for vm in final_vms if f.backup.matches(vm)]

            if not primary_ids:
                self._input_errors.append(
                    f"failover rule '{f.rule_id}': primary selector matches no VMs"
                )
                continue
            if not backup_ids:
                self._input_errors.append(
                    f"failover rule '{f.rule_id}': backup selector matches no VMs"
                )
                continue

            overlap = set(primary_ids) & set(backup_ids)
            if overlap:
                self._input_errors.append(
                    f"failover rule '{f.rule_id}': primary and backup selectors "
                    f"overlap on VMs {sorted(overlap)}"
                )
                continue

            if f.policy == "n_minus_1" and len(primary_ids) > len(backup_ids):
                self._input_errors.append(
                    f"failover rule '{f.rule_id}': |primary|={len(primary_ids)} > "
                    f"|backup|={len(backup_ids)}; n_minus_1 redundancy is "
                    f"infeasible by counting"
                )
                continue

            if f.fault_domain not in self.dim_to_bms:
                # Defensive — Pydantic validator should already reject unknown dims.
                self._input_errors.append(
                    f"failover rule '{f.rule_id}': fault_domain "
                    f"'{f.fault_domain}' is not a known topology dimension"
                )
                continue

            resolved.append((f, primary_ids, backup_ids))
            logger.info(
                "Failover rule %s: %d primary, %d backup, fault_domain=%s",
                f.rule_id, len(primary_ids), len(backup_ids), f.fault_domain,
            )
        return resolved

    # ------------------------------------------------------------------
    # Step B (cont.): Per-baremetal rules — C4 resolve
    # ------------------------------------------------------------------

    def _resolve_max_per_bm_rules(self) -> list[MaxPerBaremetalRule]:
        """
        Combine explicit per-BM rules with auto-generated ones.

        Auto-generation grouping key matches C3: (cluster_id, ip_type, node_role).
        Each group with 2+ VMs gets a rule capped at config.default_max_per_bm.
        VMs already covered by explicit rules (by vm_ids or selector match) are
        not auto-generated.
        """
        explicit = list(self.request.max_per_bm_rules)

        # Materialize explicit rules to canonical vm_ids form so downstream
        # constraint building doesn't need to know about selectors.
        rules: list[MaxPerBaremetalRule] = []
        for r in explicit:
            resolved_ids = self._expand_vm_ids(r)
            rules.append(MaxPerBaremetalRule(
                group_id=r.group_id or self._auto_group_id_for_selector(r.selector),
                vm_ids=resolved_ids,
                max_per_bm=r.max_per_bm,
            ))
            if not resolved_ids:
                self.advisories.append({
                    "type": "max_per_bm_rule_empty",
                    "severity": "warning",
                    "group_id": r.group_id,
                    "message": (
                        f"max_per_bm rule '{r.group_id}' resolved to 0 VMs — "
                        f"the rule has no effect."
                    ),
                })

        if not self.config.auto_generate_max_per_bm:
            return rules

        default_cap = self.config.default_max_per_bm
        # If default_cap is invalid we've already recorded an _input_error;
        # bail out of auto-gen to avoid building unusable rules.
        if default_cap is None or default_cap < 1:
            return rules

        covered: set[str] = set()
        for r in rules:
            covered.update(r.vm_ids)

        groups: dict[tuple[str, str, str], list[str]] = defaultdict(list)
        for vm in self.request.vms:
            if vm.id in covered:
                continue
            if not vm.in_final_state:
                continue
            if not vm.cluster_id or not vm.ip_type:
                continue
            groups[(vm.cluster_id, vm.ip_type, vm.node_role.value)].append(vm.id)

        for (cluster_id, ip_type, role), vm_ids in groups.items():
            if len(vm_ids) < 2:
                continue
            group_id = f"auto-bm/{cluster_id}/{ip_type}/{role}"
            rules.append(MaxPerBaremetalRule(
                group_id=group_id,
                vm_ids=vm_ids,
                max_per_bm=default_cap,
            ))
            logger.info(
                "Auto max-per-bm: %s/%s/%s (%d VMs, cap=%d)",
                cluster_id, ip_type, role, len(vm_ids), default_cap,
            )

        return rules

    @staticmethod
    def _auto_group_id_for_selector(sel: GroupSelector | None) -> str:
        """Fallback group_id when caller didn't provide one for a selector rule."""
        if sel is None:
            return "anonymous"
        parts = [
            sel.cluster_id or "*",
            sel.ip_type or "*",
            sel.node_role.value if sel.node_role else "*",
        ]
        return "selector/" + "/".join(parts)

    # ------------------------------------------------------------------
    # Step C: Build the CP-SAT model
    # ------------------------------------------------------------------

    def _build_variables(self):
        """
        Create one boolean variable for each eligible (VM, BM) pair.

        assign[(vm_id, bm_id)] = 1 means "vm is placed on bm"
        assign[(vm_id, bm_id)] = 0 means "vm is NOT placed on bm"

        We only create variables for pairs where the VM can actually fit.
        This is a key optimization — if you have 100 VMs and 50 BMs,
        you might only have 500 eligible pairs instead of 5000.
        """
        for vm in self.request.vms:
            for bm_id in self._get_eligible_baremetals(vm):
                self.assign[(vm.id, bm_id)] = self.model.new_bool_var(
                    f"assign_{vm.id}__{bm_id}"
                )

    def _add_one_bm_per_vm_constraint(self):
        """
        CONSTRAINT: Each VM must be assigned to exactly one baremetal.

        For each VM: sum of all its assignment variables == 1
        (exactly one of them is "on")

        If allow_partial_placement is True, we use <= 1 instead
        (the VM might not be placed at all).

        Synthetic VMs from the splitter carry an active_var. When active=0
        the VM is unused; when active=1 it must be placed on exactly one BM.
        """
        for vm in self.request.vms:
            vm_vars = [
                self.assign[(vm.id, bm_id)]
                for bm_id in self._get_eligible_baremetals(vm)
                if (vm.id, bm_id) in self.assign
            ]
            active_var = self.active_vars.get(vm.id)

            if not vm_vars:
                if active_var is not None:
                    # Splitter slot with no eligible BM → force inactive
                    self.model.add(active_var == 0)
                    continue
                elif self.config.allow_partial_placement:
                    continue  # skip this VM, it can't be placed
                else:
                    logger.error("VM %s has no eligible BMs → infeasible", vm.id)
                    self.model.add(0 == 1)  # force infeasibility
                    return

            if active_var is not None:
                # Synthetic VM: placed on exactly one BM iff the splitter activates it
                self.model.add(sum(vm_vars) == active_var)
            elif self.config.allow_partial_placement:
                self.model.add(sum(vm_vars) <= 1)
            else:
                self.model.add(sum(vm_vars) == 1)

    def _add_pinned_assignment_constraints(self):
        """
        CONSTRAINT C6: A pinned VM stays on its pinned BM.

            assign[vm, vm.pinned_bm] == 1        for every pinned VM

        Fixing the variable (instead of folding pinned VMs into constants)
        lets every other builder — C2 sums, C3/C4/C5 bucket sums, bm_used,
        headroom, extraction — treat pinned VMs uniformly with zero special
        cases; CP-SAT's presolve substitutes fixed Booleans away at no cost.
        Eligibility (Step A) already restricted a pinned VM to exactly its
        pinned BM, so the single variable always exists here (pinned_bm
        membership in bm_map is validated as INPUT_ERROR in __init__).
        """
        for vm in self.request.vms:
            if vm.pinned_bm is None:
                continue
            var = self.assign.get((vm.id, vm.pinned_bm))
            if var is not None:
                self.model.add(var == 1)

    def _add_capacity_constraints(self):
        """
        CONSTRAINT: Total VM demand on each BM must not exceed its available capacity.

        For each baremetal, for each resource dimension (cpu, mem, disk, gpu):
          sum of (vm_demand * assign_var) for all VMs eligible on this BM <= available_capacity

        Example: BM has 64 available CPU cores.
          VM-A needs 16 cores, VM-B needs 8 cores, VM-C needs 32 cores.
          If all three are assigned here: 16+8+32 = 56 <= 64 ✓
          If we also add VM-D (16 cores): 56+16 = 72 > 64 ✗

        TRANSITIONAL-state semantics (upgrade workflows): the usage sum
        includes pinned VMs via their fixed assign vars (C6) — keep,
        to_be_removed and new VMs all consume simultaneously during the
        surge overlap. The RHS is therefore the EFFECTIVE available
        capacity (total − usage by VMs not listed in the request); using
        bm.available_capacity here would count pinned demand twice.
        """
        for bm in self.request.baremetals:
            avail = self.effective_available[bm.id]

            # Collect all (vm_id, assign_var) pairs for VMs eligible on this BM
            assigned_vars = [
                (vm_id, self.assign[(vm_id, bm.id)])
                for vm_id in self.vm_map
                if (vm_id, bm.id) in self.assign
            ]

            if not assigned_vars:
                continue

            # For each resource dimension, add a capacity constraint
            for field in RESOURCE_FIELDS:
                capacity = getattr(avail, field)

                # Build the usage expression: sum(demand * var)
                usage = sum(
                    getattr(self.vm_map[vm_id].demand, field) * var
                    for vm_id, var in assigned_vars
                )

                # The constraint: total usage <= capacity
                self.model.add(usage <= capacity)

    def _note_pinned_relaxation(self, advisory_type: str, group_id: str,
                                **details) -> None:
        """
        Record that a pre-existing pinned layout forced a per-bucket cap
        relaxation (C3/C4/C5). Non-fatal by design: the alternative — hard
        INFEASIBLE on machines the solver cannot move — would make upgrade
        solves unusable on real legacy layouts. The scheduler surfaces
        these to the user.
        """
        self.advisories.append({
            "type": advisory_type,
            "severity": "warning",
            "group_id": group_id,
            "message": (
                f"{advisory_type}: pre-existing pinned layout of group "
                f"'{group_id}' exceeds the cap; cap relaxed to the pinned "
                f"count so the solve stays feasible ({details})"
            ),
            "details": details,
        })
        logger.warning(
            "Pinned relaxation (%s) on group %s: %s",
            advisory_type, group_id, details,
        )

    def _add_anti_affinity_constraints(self):
        """
        CONSTRAINT C3: VMs in the same anti-affinity group are spread across
        buckets of each dimension named in `rule.spread_on`.

        For each rule r, for each dimension d in r.spread_on, for each
        bucket b of dimension d:
          sum(assign[vm,bm] for vm in r.vm_ids for bm in b) <= cap_d
        where
          cap_d = r.cap_per_bucket[d]   if d in r.cap_per_bucket
                = ⌈|r.vm_ids| / |buckets(d)|⌉   otherwise

        Multiple dimensions are AND'd. Example: spread_on=["ag","room"]
        produces independent per-AG and per-Room constraints, both must
        hold simultaneously.

        For auto-generated rules containing synthetic VMs (splitter slots),
        the cap is replaced by the dynamic ceil expression:
          count_in_bucket * |B_d| <= total_active + (|B_d| - 1)
        which is equivalent to:
          count_in_bucket <= ceil(total_active / |B_d|)

        Pinned relaxation (upgrade workflows): pinned group members are a
        constant floor on their bucket's sum (C6 fixes their vars to 1), so
        a pre-existing layout that already violates cap_d would make the
        model INFEASIBLE through no fault of this solve. Per bucket the cap
        is relaxed to max(cap_d, pinned_in_bucket) — the solver never
        worsens the violation (the bucket is saturated by fixed vars, no
        new VM can enter) — and a pinned_spread_violation advisory reports
        it. The dynamic path gets the additive equivalent: relax_b =
        max(0, pinned_in_b·|B| − explicit_count − (|B|−1)), derived from
        requiring feasibility at the worst case total_active =
        explicit_count (all synthetic slots inactive).
        """
        import math

        for rule in self.effective_rules:
            vm_ids = rule.vm_ids
            N = len(vm_ids)
            if N == 0:
                continue

            # Check if this auto-generated rule contains synthetic VMs
            # whose active count is a decision variable.
            is_auto = rule.group_id.startswith("auto/")
            synthetic_ids = (
                [vid for vid in vm_ids if vid in self.active_vars]
                if is_auto
                else []
            )
            use_dynamic = is_auto and len(synthetic_ids) > 0

            # Build total_active expression once per rule (reused across
            # all dimensions and buckets). Synthetic VMs contribute their
            # active var; explicit VMs contribute 1 each.
            total_active = None
            if use_dynamic:
                explicit_count = sum(
                    1 for vid in vm_ids if vid not in self.active_vars
                )
                total_active = (
                    sum(self.active_vars[vid] for vid in synthetic_ids)
                    + explicit_count
                )

            cap_overrides = rule.cap_per_bucket or {}

            for dim in rule.spread_on:
                buckets = self.dim_to_bms.get(dim, {})
                num_buckets = len(buckets)
                if num_buckets == 0:
                    continue

                has_override = dim in cap_overrides
                use_dynamic_here = use_dynamic and not has_override

                if use_dynamic_here:
                    # count * |B| <= total_active + (|B| - 1)
                    # ≡ count <= ceil(total_active / |B|)
                    # Trivially true when |B| == 1 (reduces to count <= total).
                    if num_buckets <= 1:
                        continue
                    for bucket_name, bm_ids in buckets.items():
                        vars_in_bucket = [
                            self.assign[(vm_id, bm_id)]
                            for vm_id in vm_ids
                            for bm_id in bm_ids
                            if (vm_id, bm_id) in self.assign
                        ]
                        if not vars_in_bucket:
                            continue
                        pinned_in_b = pinned_count_in_bucket(
                            vm_ids, set(bm_ids), self.vm_map
                        )
                        relax_b = max(
                            0,
                            pinned_in_b * num_buckets
                            - explicit_count - (num_buckets - 1),
                        )
                        if relax_b > 0:
                            self._note_pinned_relaxation(
                                "pinned_spread_violation", rule.group_id,
                                dimension=dim, bucket=bucket_name,
                                cap=f"ceil(total_active/{num_buckets})",
                                pinned_count=pinned_in_b,
                            )
                        self.model.add(
                            sum(vars_in_bucket) * num_buckets
                            <= total_active + (num_buckets - 1) + relax_b
                        )
                else:
                    static_cap = cap_overrides.get(
                        dim, math.ceil(N / num_buckets)
                    )
                    # Trivially true: sum within any bucket can't exceed N
                    # anyway (it's bounded by |rule.vm_ids|).
                    if static_cap >= N:
                        continue
                    for bucket_name, bm_ids in buckets.items():
                        vars_in_bucket = [
                            self.assign[(vm_id, bm_id)]
                            for vm_id in vm_ids
                            for bm_id in bm_ids
                            if (vm_id, bm_id) in self.assign
                        ]
                        if not vars_in_bucket:
                            continue
                        pinned_in_b = pinned_count_in_bucket(
                            vm_ids, set(bm_ids), self.vm_map
                        )
                        if pinned_in_b > static_cap:
                            self._note_pinned_relaxation(
                                "pinned_spread_violation", rule.group_id,
                                dimension=dim, bucket=bucket_name,
                                cap=static_cap, pinned_count=pinned_in_b,
                            )
                        self.model.add(
                            sum(vars_in_bucket)
                            <= max(static_cap, pinned_in_b)
                        )

    def _add_failover_constraints(self):
        """
        CONSTRAINT C5: For each failover rule and each bucket b of its
        fault_domain dimension d:

            sum(assign[vm,bm] for vm ∈ primary for bm ∈ b)
          + sum(assign[vm,bm] for vm ∈ backup  for bm ∈ b)
                ≤ |backup|

        This is equivalent to:
            sum(backup VMs outside b) ≥ sum(primary VMs inside b)
        so that if bucket b fails entirely, the surviving backups can take
        over the primaries that were inside b (N-1 redundancy).

        Pre-flight check (|P| > |L|) already happened in
        _resolve_failover_rules — only well-formed rules reach this method.

        Membership is FINAL-state (to_be_removed VMs excluded at resolve
        time); pinned members contribute through fixed vars. When the
        pinned layout alone already breaks the invariant in some bucket,
        the RHS is relaxed to the pinned occupancy (don't worsen, don't
        fail) and a pinned_failover_violation advisory reports it.
        """
        for f, primary_ids, backup_ids in self.failover_resolved:
            buckets = self.dim_to_bms[f.fault_domain]
            backup_total = len(backup_ids)
            for bucket_name, bm_ids in buckets.items():
                primary_in_b = [
                    self.assign[(vm_id, bm_id)]
                    for vm_id in primary_ids
                    for bm_id in bm_ids
                    if (vm_id, bm_id) in self.assign
                ]
                backup_in_b = [
                    self.assign[(vm_id, bm_id)]
                    for vm_id in backup_ids
                    for bm_id in bm_ids
                    if (vm_id, bm_id) in self.assign
                ]
                if not (primary_in_b or backup_in_b):
                    continue
                bm_id_set = set(bm_ids)
                pinned_in_b = (
                    pinned_count_in_bucket(primary_ids, bm_id_set, self.vm_map)
                    + pinned_count_in_bucket(backup_ids, bm_id_set, self.vm_map)
                )
                if pinned_in_b > backup_total:
                    self._note_pinned_relaxation(
                        "pinned_failover_violation", f.rule_id,
                        fault_domain=f.fault_domain, bucket=bucket_name,
                        cap=backup_total, pinned_count=pinned_in_b,
                    )
                self.model.add(
                    sum(primary_in_b) + sum(backup_in_b)
                    <= max(backup_total, pinned_in_b)
                )

    def _add_max_per_bm_constraints(self):
        """
        CONSTRAINT (C4): For each per-BM rule, no single baremetal hosts more
        than `max_per_bm` VMs from the group.

        For each rule, for each BM:
          sum(assign[vm, bm] for vm in rule.vm_ids if (vm, bm) eligible) <= max_per_bm

        Synthetic VMs (splitter slots) need no special handling — the C1
        constraint sum(assign[vm, *]) == active_var forces inactive synthetic
        VMs' assign vars to 0, so they naturally drop out of this sum.

        Membership is FINAL-state (to_be_removed excluded at resolve time).
        A BM whose pinned group members already exceed max_per_bm gets its
        cap relaxed to the pinned count (advisory: don't worsen, don't fail).
        """
        for rule in self.max_per_bm_rules:
            for bm_id in self.bm_map:
                vars_on_bm = [
                    self.assign[(vm_id, bm_id)]
                    for vm_id in rule.vm_ids
                    if (vm_id, bm_id) in self.assign
                ]
                if not vars_on_bm:
                    continue
                pinned_on_bm = pinned_count_in_bucket(
                    rule.vm_ids, {bm_id}, self.vm_map
                )
                if pinned_on_bm > rule.max_per_bm:
                    self._note_pinned_relaxation(
                        "pinned_max_per_bm_violation", rule.group_id,
                        bm_id=bm_id, cap=rule.max_per_bm,
                        pinned_count=pinned_on_bm,
                    )
                self.model.add(
                    sum(vars_on_bm) <= max(rule.max_per_bm, pinned_on_bm)
                )

    # ------------------------------------------------------------------
    # Step C (cont.): Objective function helpers
    # ------------------------------------------------------------------

    def _add_bm_group_cap_constraints(self):
        """
        For each injected (bm_ids, cap): at most `cap` of those BMs may host
        any VM. Enforced on the bm_used indicators so it counts machines, not
        placements — one BM hosting five VMs consumes one slot.
        """
        if not self.bm_group_caps:
            return
        self._ensure_bm_used_vars()
        for bm_ids, cap in self.bm_group_caps:
            used = [self.bm_used[bid] for bid in bm_ids if bid in self.bm_used]
            if used and cap < len(used):
                self.model.add(sum(used) <= cap)

    def _build_bm_used_vars(self):
        """
        Create bm_used[bm_id] indicator: 1 if any VM is placed on this BM.

        bm_used[bm] = max(assign[vm_1, bm], assign[vm_2, bm], ...)
        """
        for bm in self.request.baremetals:
            bm_used = self.model.new_bool_var(f"bm_used_{bm.id}")

            vm_vars_on_bm = [
                self.assign[(vm_id, bm.id)]
                for vm_id in self.vm_map
                if (vm_id, bm.id) in self.assign
            ]

            if vm_vars_on_bm:
                self.model.add_max_equality(bm_used, vm_vars_on_bm)
            else:
                self.model.add(bm_used == 0)

            self.bm_used[bm.id] = bm_used

    def _compute_headroom_penalties(self) -> list[cp_model.IntVar]:
        """
        Compute per-BM headroom penalty.

        For each BM, for each resource dimension:
        1. Compute utilization % after placement
        2. Penalize the amount exceeding headroom_upper_bound_pct
        3. Take the max across dimensions (worst-case determines penalty)

        Returns a list of penalty variables, one per BM.

        Headroom is computed on the TRANSITIONAL load (old + new VMs during
        the surge overlap) — that is the window the safety margin protects.
        used_d must be the EFFECTIVE used (pinned demand excluded), because
        pinned VMs re-enter new_usage through their fixed assign vars.
        """
        penalties = []
        for bm in self.request.baremetals:
            dim_overs = []
            for field in RESOURCE_FIELDS:
                total_d = getattr(bm.total_capacity, field)
                if total_d == 0:
                    continue  # skip zero-total dimensions (e.g. gpu_count=0)

                used_d = getattr(self.effective_used[bm.id], field)

                assigned_vars = [
                    (vm_id, self.assign[(vm_id, bm.id)])
                    for vm_id in self.vm_map
                    if (vm_id, bm.id) in self.assign
                ]
                if not assigned_vars:
                    continue

                # New VM usage on this BM
                new_usage = sum(
                    getattr(self.vm_map[vm_id].demand, field) * var
                    for vm_id, var in assigned_vars
                )

                # Step A: post-placement usage * 100 (integer arithmetic, no floats)
                # Upper bound uses max(total, used + all candidate demand) to avoid
                # false INFEASIBLE when used_d is already high.
                max_new_demand = sum(
                    getattr(self.vm_map[vm_id].demand, field)
                    for vm_id, _ in assigned_vars
                )
                upper_after = max(total_d, used_d + max_new_demand) * 100
                after_times_100 = self.model.new_int_var(
                    0, upper_after, f"a100_{bm.id}_{field}"
                )
                self.model.add(after_times_100 == (used_d + new_usage) * 100)

                # Step B: integer utilization % (can exceed 100 if BM is near-full)
                max_util = upper_after // total_d if total_d > 0 else 0
                util_pct = self.model.new_int_var(0, max_util, f"util_{bm.id}_{field}")
                self.model.add_division_equality(util_pct, after_times_100, total_d)

                # Step C: amount exceeding the safe upper bound (may be negative)
                raw = self.model.new_int_var(
                    -max_util, max_util, f"raw_{bm.id}_{field}"
                )
                self.model.add(raw == util_pct - self.config.headroom_upper_bound_pct)

                # Step D: ReLU — clamp negative values to 0
                over = self.model.new_int_var(0, max_util, f"over_{bm.id}_{field}")
                self.model.add_max_equality(over, [self.model.new_constant(0), raw])
                dim_overs.append(over)

            if dim_overs:
                # Step E: max across dimensions
                bm_penalty = self.model.new_int_var(0, 1000, f"hp_{bm.id}")
                self.model.add_max_equality(bm_penalty, dim_overs)
                penalties.append(bm_penalty)

        return penalties

    def _compute_slot_score_bonus(self) -> list[cp_model.IntVar]:
        """
        Compute per-BM slot score (how many t-shirt size VMs can still fit).

        For each BM:
        1. Compute remaining capacity per dimension after placement
        2. For each t-shirt size, floor-divide remaining by demand per dimension
        3. Take min across dimensions = actual fit count for that t-shirt
        4. Sum across all t-shirt sizes = BM slot score

        Higher score = more usable remaining space = rewarded (negated in objective).
        """
        tshirt_sizes = self.config.vm_specs
        if not tshirt_sizes:
            return []

        scores = []
        for bm in self.request.baremetals:
            assigned_vars = [
                (vm_id, self.assign[(vm_id, bm.id)])
                for vm_id in self.vm_map
                if (vm_id, bm.id) in self.assign
            ]
            if not assigned_vars:
                continue

            tshirt_slots = []
            for t_idx, tshirt in enumerate(tshirt_sizes):
                dim_slots = []
                for field in RESOURCE_FIELDS:
                    tshirt_d = getattr(tshirt, field)
                    if tshirt_d == 0:
                        continue  # no demand on this dimension, not a bottleneck

                    total_d = getattr(bm.total_capacity, field)
                    # Effective used: pinned demand re-enters via fixed vars
                    # in new_usage below (same reasoning as headroom).
                    used_d = getattr(self.effective_used[bm.id], field)

                    # New VM usage on this BM
                    new_usage = sum(
                        getattr(self.vm_map[vm_id].demand, field) * var
                        for vm_id, var in assigned_vars
                    )

                    # Remaining = total - used - new_placement
                    # Lower bound can be negative to avoid false INFEASIBLE with
                    # many candidate VMs (capacity constraint ensures actual >= 0)
                    max_new_d = sum(
                        getattr(self.vm_map[vm_id].demand, field)
                        for vm_id, _ in assigned_vars
                    )
                    remaining = self.model.new_int_var(
                        total_d - used_d - max_new_d,
                        total_d,
                        f"rem_{bm.id}_{field}_t{t_idx}",
                    )
                    self.model.add(remaining == total_d - used_d - new_usage)

                    # How many of this t-shirt size fit on this dimension
                    # (may be negative in the model; capacity constraint ensures non-negative at solution)
                    min_slots = (
                        (total_d - used_d - max_new_d) // tshirt_d
                        if tshirt_d > 0
                        else 0
                    )
                    max_slots = total_d // tshirt_d if tshirt_d > 0 else 0
                    slots_d = self.model.new_int_var(
                        min(min_slots, 0),
                        max_slots,
                        f"slotd_{bm.id}_{field}_t{t_idx}",
                    )
                    self.model.add_division_equality(slots_d, remaining, tshirt_d)
                    dim_slots.append(slots_d)

                if dim_slots:
                    # Min across dimensions = actual fit count (bottleneck dimension decides)
                    max_possible = min(
                        getattr(bm.total_capacity, f) // getattr(tshirt, f)
                        for f in RESOURCE_FIELDS
                        if getattr(tshirt, f) > 0
                    )
                    # min may be negative (capacity constraint ensures >= 0 at solution)
                    slots_for_tshirt = self.model.new_int_var(
                        -max_possible, max_possible, f"slot_{bm.id}_t{t_idx}"
                    )
                    self.model.add_min_equality(slots_for_tshirt, dim_slots)
                    tshirt_slots.append(slots_for_tshirt)

            if tshirt_slots:
                # Sum fit counts across all t-shirt sizes
                max_total = sum(
                    min(
                        getattr(bm.total_capacity, f) // getattr(ts, f)
                        for f in RESOURCE_FIELDS
                        if getattr(ts, f) > 0
                    )
                    for ts in tshirt_sizes
                    if any(getattr(ts, f) > 0 for f in RESOURCE_FIELDS)
                )
                bm_score = self.model.new_int_var(
                    -max_total, max_total, f"sscore_{bm.id}"
                )
                self.model.add(bm_score == sum(tshirt_slots))

                # Only count slot score for used BMs — otherwise solver would
                # prefer placing VMs on small BMs to keep large BMs' scores high
                effective = self.model.new_int_var(
                    -max_total, max_total, f"eff_sscore_{bm.id}"
                )
                self.model.add_multiplication_equality(
                    effective, [self.bm_used[bm.id], bm_score]
                )
                scores.append(effective)

        return scores

    def _ensure_bm_used_vars(self):
        """Build bm_used vars if not yet built. Multiple objective terms may need them."""
        if not self.bm_used:
            self._build_bm_used_vars()

    def _add_objective(self):
        """
        Combine all objective terms and set Minimize.

        Priority (high to low):
        1. Place as many VMs as possible (partial placement mode)
        2. Use as few BMs as possible (consolidation)
        3. Keep utilization below safe upper bound (headroom)
        4. Maximize usability of remaining capacity (slot score)
        """
        terms = []

        if self.config.allow_partial_placement:
            total_placed = sum(self.assign.values())
            terms.append(-1_000_000 * total_placed)

        if self.config.w_consolidation > 0:
            self._ensure_bm_used_vars()
            terms.append(self.config.w_consolidation * sum(self.bm_used.values()))

        if self.config.w_headroom > 0:
            penalties = self._compute_headroom_penalties()
            if penalties:
                terms.append(self.config.w_headroom * sum(penalties))

        if self.config.w_slot_score > 0:
            self._ensure_bm_used_vars()
            slot_scores = self._compute_slot_score_bonus()
            if slot_scores:
                # Negate: higher slot score is better (negative = reward in Minimize)
                terms.append(-self.config.w_slot_score * sum(slot_scores))

        waste_terms = self.splitter_waste_terms
        if waste_terms and self.config.w_resource_waste > 0:
            terms.append(self.config.w_resource_waste * sum(waste_terms))

        # Label preference (upgrade workflows): penalize placing a VM on a
        # BM that doesn't match its prefer_bm_labels. Range check: at most
        # w_label_preference per placed VM — deliberately above one BM's
        # consolidation cost (matching an upgraded BM beats opening one
        # fewer BM) and far below w_procurement/w_committed_stock.
        if self.config.w_label_preference > 0:
            mismatch_terms = self._compute_label_mismatch_terms()
            if mismatch_terms:
                terms.append(
                    self.config.w_label_preference * sum(mismatch_terms)
                )

        # Procurement: minimize how many buyable BMs are used (Phase 2). The
        # high weight ensures in-stock BMs are preferred and buying is minimal.
        if self.procurement_bm_ids and self.config.w_procurement > 0:
            self._ensure_bm_used_vars()
            proc_terms = [
                self.bm_used[bid] for bid in self.procurement_bm_ids
                if bid in self.bm_used
            ]
            if proc_terms:
                terms.append(self.config.w_procurement * sum(proc_terms))

        # Committed stock: cheaper than buying, dearer than in-stock.
        if self.committed_bm_ids and self.config.w_committed_stock > 0:
            self._ensure_bm_used_vars()
            own_terms = [
                self.bm_used[bid] for bid in self.committed_bm_ids
                if bid in self.bm_used
            ]
            if own_terms:
                terms.append(self.config.w_committed_stock * sum(own_terms))

        # Balance resulting per-bucket available capacity (decision #11).
        terms.extend(self._compute_procurement_balance_terms())

        if terms:
            self.model.minimize(sum(terms))

    def _compute_label_mismatch_terms(self) -> list[cp_model.IntVar]:
        """
        One assign var per (VM with prefer_bm_labels, eligible BM that does
        NOT match all of them). Soft preference only — a mismatched BM is
        penalized, never forbidden (hard filtering is the scheduler's job
        via candidate_baremetals). Pinned VMs are skipped: their placement
        is fixed, so a penalty would be a constant that can't steer anything.
        Pure linear over existing BoolVars — no new variables.
        """
        terms = []
        for (vm_id, bm_id), var in self.assign.items():
            vm = self.vm_map[vm_id]
            if vm.pinned_bm is not None or not vm.prefer_bm_labels:
                continue
            bm_labels = self.bm_map[bm_id].labels
            if any(
                bm_labels.get(k) != v
                for k, v in vm.prefer_bm_labels.items()
            ):
                terms.append(var)
        return terms

    def _compute_procurement_balance_terms(self) -> list:
        """
        Soft-minimize (max − min) of post-placement available CPU cores across
        the procurement_spread_dimension buckets, so procurement tops up the
        emptiest bucket rather than splitting buy counts evenly.

        CPU cores is the balance currency. A virtual BM's (buyable/committed)
        capacity only counts when it is actually used: bm_used × capacity.
        """
        w = self.config.w_procurement_balance
        if w <= 0:
            return []
        dim = self.config.procurement_spread_dimension
        conditional = self.procurement_bm_ids | self.committed_bm_ids

        # Buckets are seeded from REAL (in-stock) BMs only: a bucket whose
        # members are all unused virtual BMs would contribute avail=0, pinning
        # min to 0 and degenerating (max−min) into "minimize max leftover".
        # Virtual BMs still contribute conditionally to the real buckets they
        # land in.
        buckets: dict[str, list] = {}
        for bm in self.request.baremetals:
            if bm.id not in conditional:
                buckets.setdefault(getattr(bm.topology, dim), []).append(bm)
        if len(buckets) < 2:
            return []
        for bm in self.request.baremetals:
            if bm.id in conditional:
                b = getattr(bm.topology, dim)
                if b in buckets:
                    buckets[b].append(bm)

        self._ensure_bm_used_vars()

        # Group assignment vars by BM once (self.assign is keyed by (vm, bm)).
        placed_on: dict[str, list] = {}
        for (vm_id, bm_id), var in self.assign.items():
            placed_on.setdefault(bm_id, []).append(
                self.vm_map[vm_id].demand.cpu_cores * var
            )

        hi = sum(bm.total_capacity.cpu_cores for bm in self.request.baremetals)
        avail_vars = []
        for bucket, bms in buckets.items():
            expr = 0
            for bm in bms:
                if bm.id in conditional:
                    expr += bm.total_capacity.cpu_cores * self.bm_used[bm.id]
                else:
                    # Effective available: placed_on includes pinned fixed
                    # vars, so pinned demand must not also sit in the base.
                    expr += self.effective_available[bm.id].cpu_cores
                expr -= sum(placed_on.get(bm.id, []))
            v = self.model.new_int_var(0, hi, f"bal_avail_{bucket}")
            self.model.add(v == expr)
            avail_vars.append(v)

        max_v = self.model.new_int_var(0, hi, "bal_max")
        min_v = self.model.new_int_var(0, hi, "bal_min")
        self.model.add_max_equality(max_v, avail_vars)
        self.model.add_min_equality(min_v, avail_vars)
        return [w * (max_v - min_v)]

    # ------------------------------------------------------------------
    # Step D: Solve and extract results
    # ------------------------------------------------------------------

    def solve(self) -> PlacementResult:
        """
        Build the model, solve it, return results.

        This is the main entry point.
        """
        start = time.time()

        # Reject requests with input errors — scheduler must fix upstream.
        if self._input_errors:
            for err in self._input_errors:
                logger.error("Input validation failed: %s", err)
            return PlacementResult(
                success=False,
                solver_status=f"INPUT_ERROR: {self._input_errors[0]}",
                solve_time_seconds=time.time() - start,
                unplaced_vms=[vm.id for vm in self.request.vms],
                bm_total_count=len(self.request.baremetals),
                diagnostics=self._with_advisories({"input_errors": self._input_errors}),
            )

        try:
            # Build the model
            self._build_variables()
            self._add_one_bm_per_vm_constraint()
            self._add_pinned_assignment_constraints()
            self._add_capacity_constraints()
            self._add_anti_affinity_constraints()
            self._add_failover_constraints()
            self._add_max_per_bm_constraints()
            self._add_bm_group_cap_constraints()

            # Objective: consolidation + headroom (+ partial placement priority)
            self._add_objective()

            # Solve
            solver = cp_model.CpSolver()
            solver.parameters.max_time_in_seconds = self.config.max_solve_time_seconds
            solver.parameters.num_workers = self.config.num_workers

            logger.info(
                "Solving: %d VMs, %d BMs, %d variables, %d AA rules, "
                "%d per-BM rules, %d AGs",
                len(self.request.vms),
                len(self.request.baremetals),
                len(self.assign),
                len(self.effective_rules),
                len(self.max_per_bm_rules),
                len(self.dim_to_bms.get("ag", {})),
            )

            status = solver.solve(self.model)
            status_name = self._status_name(status)
            logger.info("Status: %s", status_name)

            # Extract results
            if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                self._last_cp_solver = solver
                return self._extract_solution(solver, status_name, time.time() - start)
            else:
                diagnostics = self._with_advisories(self._build_failure_diagnostics())
                logger.warning(
                    "Solver failed with %s, diagnostics: %s", status_name, diagnostics
                )
                return PlacementResult(
                    success=False,
                    solver_status=status_name,
                    solve_time_seconds=time.time() - start,
                    unplaced_vms=[vm.id for vm in self.request.vms],
                    bm_total_count=len(self.request.baremetals),
                    diagnostics=diagnostics,
                )

        except Exception as e:
            logger.exception("Solver failed")
            return PlacementResult(
                success=False,
                solver_status=f"ERROR: {e}",
                solve_time_seconds=time.time() - start,
                unplaced_vms=[vm.id for vm in self.request.vms],
                bm_total_count=len(self.request.baremetals),
                diagnostics=self._with_advisories({}),
            )

    def _with_advisories(self, diagnostics: dict) -> dict:
        """Merge collected advisories into a diagnostics dict (no-op if none)."""
        if self.advisories:
            diagnostics = dict(diagnostics)
            diagnostics["advisories"] = self.advisories
        return diagnostics

    def _build_failure_diagnostics(self) -> dict[str, object]:
        """Delegate to DiagnosticsBuilder (app/diagnostics.py)."""
        from .diagnostics import DiagnosticsBuilder

        return DiagnosticsBuilder(
            request=self.request,
            vm_map=self.vm_map,
            bm_map=self.bm_map,
            dim_to_bms=self.dim_to_bms,
            effective_rules=self.effective_rules,
            max_per_bm_rules=self.max_per_bm_rules,
            failover_resolved=self.failover_resolved,
            config=self.config,
            num_variables=len(self.assign),
            effective_available=self.effective_available,
        ).build()

    def _extract_solution(
        self, solver: cp_model.CpSolver, status: str, elapsed: float
    ) -> PlacementResult:
        """Read the solution: which assign variables are set to 1?"""
        assignments = []
        unplaced = []

        for vm in self.request.vms:
            active_var = self.active_vars.get(vm.id)
            if active_var is not None and solver.value(active_var) == 0:
                continue  # splitter decided this slot is unused
            placed = False
            for bm in self.request.baremetals:
                if (vm.id, bm.id) in self.assign:
                    if solver.value(self.assign[(vm.id, bm.id)]) == 1:
                        assignments.append(
                            PlacementAssignment(
                                vm_id=vm.id,
                                vm_hostname=vm.hostname,
                                baremetal_id=bm.id,
                                bm_hostname=bm.hostname,
                                ag=bm.topology.ag,
                            )
                        )
                        placed = True
                        break
            if not placed:
                unplaced.append(vm.id)

        bm_used_count = len({a.baremetal_id for a in assignments})

        return PlacementResult(
            success=len(unplaced) == 0,
            assignments=assignments,
            solver_status=status,
            solve_time_seconds=elapsed,
            unplaced_vms=unplaced,
            bm_used_count=bm_used_count,
            bm_total_count=len(self.request.baremetals),
            diagnostics=self._with_advisories({}),
        )

    @staticmethod
    def _status_name(status: cp_model.CpSolverStatus) -> str:
        return {
            cp_model.OPTIMAL: "OPTIMAL",
            cp_model.FEASIBLE: "FEASIBLE",
            cp_model.INFEASIBLE: "INFEASIBLE",
            cp_model.MODEL_INVALID: "MODEL_INVALID",
            cp_model.UNKNOWN: "UNKNOWN",
        }.get(status, f"STATUS_{status}")
