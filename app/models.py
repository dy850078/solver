"""
VM Placement Solver — Data Models

Uses Pydantic v2 BaseModel for automatic JSON serialization/deserialization
and type validation on construction.

Topology: site > phase > datacenter > room > rack
Virtual:  AG (availability group) — each rack belongs to exactly 1 AG
"""

from __future__ import annotations
import hashlib
import json
import re
from enum import Enum
from functools import lru_cache
from importlib import metadata
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


SPREAD_DIMENSIONS: frozenset[str] = frozenset({
    "site", "phase", "datacenter", "room", "rack", "ag",
})


# ---------------------------------------------------------------------------
# Resources: the multi-dimensional "size" of a VM or baremetal
# ---------------------------------------------------------------------------

class Resources(BaseModel):
    """
    Represents resource capacity or demand.

    Shared by VM (demand) and Baremetal (capacity) so the solver
    can handle all resource dimensions uniformly.
    """
    cpu_cores: int = 0
    memory_mib: int = 0
    storage_gb: int = 0
    gpu_count: int = 0

    def fits_in(self, capacity: Resources) -> bool:
        """Can this demand fit within the given capacity?"""
        return (
            self.cpu_cores <= capacity.cpu_cores
            and self.memory_mib <= capacity.memory_mib
            and self.storage_gb <= capacity.storage_gb
            and self.gpu_count <= capacity.gpu_count
        )

    def __add__(self, other: Resources) -> Resources:
        return Resources(
            cpu_cores=self.cpu_cores + other.cpu_cores,
            memory_mib=self.memory_mib + other.memory_mib,
            storage_gb=self.storage_gb + other.storage_gb,
            gpu_count=self.gpu_count + other.gpu_count,
        )

    def __sub__(self, other: Resources) -> Resources:
        return Resources(
            cpu_cores=self.cpu_cores - other.cpu_cores,
            memory_mib=self.memory_mib - other.memory_mib,
            storage_gb=self.storage_gb - other.storage_gb,
            gpu_count=self.gpu_count - other.gpu_count,
        )


# ---------------------------------------------------------------------------
# Topology: where a baremetal physically lives
# ---------------------------------------------------------------------------

class Topology(BaseModel):
    """
    Physical: site > phase > datacenter > room > rack
    Virtual:  AG (availability group) — orthogonal to room, crosscuts racks
    """
    site: str = ""
    phase: str = ""
    datacenter: str = ""
    room: str = ""
    rack: str = ""
    ag: str = ""


# ---------------------------------------------------------------------------
# Baremetal: a physical server
# ---------------------------------------------------------------------------

class Baremetal(BaseModel):
    """
    A physical host. The Go scheduler fills in total_capacity and used_capacity
    from the inventory API. available_capacity is derived (total - used).
    """
    id: str
    hostname: str = ""
    total_capacity: Resources
    used_capacity: Resources = Field(default_factory=Resources)
    topology: Topology = Field(default_factory=Topology)
    # Network domain (e.g. BGP zone) this host's rack belongs to. A filter
    # attribute, not a spread dimension — clusters live entirely inside one
    # domain and never spread across domains. "" = untagged.
    network: str = ""
    # Dedicated-pool tag (E2/S6). A filter attribute, not a spread dimension.
    # "" = the shared pool — a DISTINCT domain, not a wildcard: shared demand
    # never lands on pool-tagged hosts and pool demand never lands on shared
    # hosts (strict isolation; rebalancing between pools is a manual
    # operation — retag the machine in Inventory).
    pool: str = ""

    @property
    def available_capacity(self) -> Resources:
        return self.total_capacity - self.used_capacity


# ---------------------------------------------------------------------------
# VM: a virtual machine to place
# ---------------------------------------------------------------------------

class NodeRole(str, Enum):
    """Known-roles catalog. ADVISORY, not a validation gate: node_role fields
    are open strings (the role directory is owned by the Go scheduler /
    operations, not this sidecar), and this enum only feeds defaults and UI
    suggestions. Logic that branches on a specific role (e.g. mockgen's
    master→learner failover convention) references these members by name."""
    MASTER = "master"
    LEARNER = "learner"
    WORKER = "worker"
    INFRA = "infra"
    L4LB = "l4lb-storage"
    BASTION = "bastion"


# Roles are open strings (see NodeRole docstring); only the FORMAT is hard-
# validated — membership in the known catalog is advisory, checked by callers
# that care (e.g. mockgen surfaces unknown_roles in diagnostics).
_ROLE_RE = re.compile(r"^[\w.-]+$")


def validate_role(v: str) -> str:
    if not v or not _ROLE_RE.match(v):
        raise ValueError(f"node_role {v!r} must be non-empty and match ^[\\w.-]+$")
    return v


class VM(BaseModel):
    """
    A VM to be placed on a baremetal.

    candidate_baremetals: from Go scheduler step 3 (filtering). Required —
      must be populated with the BMs that survived scheduler filtering.
      An empty list is treated as a contract violation and rejected by
      the solver with an INPUT_ERROR.

    ip_type: network type of the VM (e.g. "routable", "non-routable").
      Used together with node_role as the grouping key for auto-generated
      anti-affinity rules.
    """
    id: str
    hostname: str = ""
    demand: Resources
    node_role: str = "worker"
    ip_type: str = ""
    cluster_id: str = ""
    candidate_baremetals: list[str] = Field(default_factory=list)

    @field_validator("node_role")
    @classmethod
    def _role_format(cls, v: str) -> str:
        return validate_role(v)


# ---------------------------------------------------------------------------
# Anti-affinity & per-baremetal: spread VMs across AGs / cap per BM
# ---------------------------------------------------------------------------

class GroupSelector(BaseModel):
    """
    Declarative VM matcher. None on a field means "wildcard".

    Used by AntiAffinityRule and MaxPerBaremetalRule so the scheduler can
    describe a group by attributes instead of enumerating VM IDs.

    Example:
      GroupSelector(cluster_id="A", ip_type="non-routable", node_role=MASTER)
      → matches every VM in cluster A whose ip_type is non-routable and
        whose role is master.
    """
    cluster_id: str | None = None
    ip_type: str | None = None
    node_role: str | None = None

    def is_empty(self) -> bool:
        return self.cluster_id is None and self.ip_type is None and self.node_role is None

    def matches(self, vm: VM) -> bool:
        if self.cluster_id is not None and vm.cluster_id != self.cluster_id:
            return False
        if self.ip_type is not None and vm.ip_type != self.ip_type:
            return False
        if self.node_role is not None and vm.node_role != self.node_role:
            return False
        return True


class AntiAffinityRule(BaseModel):
    """
    "Spread these VMs across topology buckets in one or more dimensions."

    Each dimension d in `spread_on` adds an independent constraint:
      ∀ bucket b ∈ buckets(d): Σ assign[vm,bm for bm in b] ≤ cap_d
    where
      cap_d = cap_per_bucket[d]   if d in cap_per_bucket
            = ⌈|VMs| / |buckets(d)|⌉   otherwise (auto-balance)

    Multiple dimensions are AND'd, not the Cartesian product of buckets.
    Example: spread_on=["ag","room"] enforces both an AG-per-bucket cap
    AND a Room-per-bucket cap.

    Group membership: provide exactly one of `vm_ids` or `selector`.
    `selector` is resolved against the request's VM list at solve time.
    """
    group_id: str
    vm_ids: list[str] = Field(default_factory=list)
    selector: GroupSelector | None = None
    spread_on: list[str]
    cap_per_bucket: dict[str, int] | None = None

    @field_validator("spread_on")
    @classmethod
    def _validate_spread_on(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("spread_on must contain at least one dimension")
        unknown = [d for d in v if d not in SPREAD_DIMENSIONS]
        if unknown:
            raise ValueError(
                f"spread_on contains unknown dimension(s) {unknown}; "
                f"valid: {sorted(SPREAD_DIMENSIONS)}"
            )
        if len(set(v)) != len(v):
            raise ValueError(f"spread_on must not contain duplicates: {v}")
        return v

    @model_validator(mode="after")
    def _validate_cap_per_bucket(self) -> AntiAffinityRule:
        cap = self.cap_per_bucket
        if cap is not None:
            spread_set = set(self.spread_on)
            bad_keys = [k for k in cap if k not in spread_set]
            if bad_keys:
                raise ValueError(
                    f"cap_per_bucket keys {bad_keys} must be a subset of "
                    f"spread_on {self.spread_on}"
                )
            bad_values = {k: v for k, v in cap.items() if v < 1}
            if bad_values:
                raise ValueError(
                    f"cap_per_bucket values must be >= 1; got {bad_values}. "
                    f"To disable spreading on a dimension, remove it from spread_on."
                )
        return self


class FailoverRule(BaseModel):
    """
    Cross-group N-1 redundancy constraint.

    Semantics: for each bucket b of `fault_domain`,
        sum(backup VMs not in b) >= sum(primary VMs in b)

    Equivalent form used by the solver (easier on CP-SAT):
        sum(primary in b) + sum(backup in b) <= |backup|

    Result: when bucket b fails entirely, the surviving backups outside b
    are enough to take over the primaries that were inside b.

    `primary` and `backup` are GroupSelectors resolved against the request's
    VM list. Role-pair semantics are more stable than enumerating VM IDs
    and play well with auto-generation of resource splits.
    """
    rule_id: str
    primary: GroupSelector
    backup: GroupSelector
    fault_domain: str
    policy: Literal["n_minus_1"] = "n_minus_1"

    @field_validator("fault_domain")
    @classmethod
    def _validate_fault_domain(cls, v: str) -> str:
        if v not in SPREAD_DIMENSIONS:
            raise ValueError(
                f"fault_domain {v!r} is not a valid dimension; "
                f"valid: {sorted(SPREAD_DIMENSIONS)}"
            )
        return v

    @model_validator(mode="after")
    def _validate_selectors_nonempty(self) -> FailoverRule:
        if self.primary.is_empty():
            raise ValueError(f"FailoverRule {self.rule_id}: primary selector must not be empty")
        if self.backup.is_empty():
            raise ValueError(f"FailoverRule {self.rule_id}: backup selector must not be empty")
        return self


class MaxPerBaremetalRule(BaseModel):
    """
    "At most N VMs from this group on any single baremetal."

    Example: 3 routable masters from cluster-A with max_per_bm=1
    means no BM hosts more than 1 of them.

    Group membership: provide exactly one of `vm_ids` or `selector`.
    """
    group_id: str = ""
    vm_ids: list[str] = Field(default_factory=list)
    selector: GroupSelector | None = None
    max_per_bm: int


class ExclusiveBaremetalRule(BaseModel):
    """
    "Every member of this group occupies its baremetal ALONE."

    Appliance semantics (C6): a BM hosting a group member hosts nothing
    else — no outsider VM AND no other member of the same group (e.g. each
    F5 owns one machine outright). Cluster-shared eco-system pools are
    expressed by selecting on the shared cluster_id.

    Group membership: provide exactly one of `vm_ids` or `selector`.
    """
    group_id: str = ""
    vm_ids: list[str] = Field(default_factory=list)
    selector: GroupSelector | None = None


# ---------------------------------------------------------------------------
# Solver config: tuning knobs
# ---------------------------------------------------------------------------

class SolverConfig(BaseModel):
    """
    Solver behavior settings.
    We'll add objective weights later when we build the scoring function.
    """
    max_solve_time_seconds: float = 30.0
    num_workers: int = 8
    allow_partial_placement: bool = False
    auto_generate_anti_affinity: bool = True
    # HA policy: VMs in an auto-generated group should spread across at least
    # `target_spread[d]` distinct buckets for each dimension d. When infra has
    # fewer buckets in dimension d (or the group has fewer VMs than the
    # target), the solver still succeeds but emits a `spread_below_target`
    # advisory into diagnostics for that dimension.
    #
    # Keys are topology dimension names (see SPREAD_DIMENSIONS). The key set
    # also determines which dimensions auto-generated anti-affinity rules
    # spread on.
    target_spread: dict[str, int] = Field(default_factory=lambda: {"ag": 3})
    # Objective function weights
    w_consolidation: int = 10
    w_headroom: int = 8
    headroom_upper_bound_pct: int = 90
    # Slot score: penalize placements that leave unusable leftover capacity
    w_slot_score: int = 0
    vm_specs: list[Resources] = Field(default_factory=list)
    # Requirement splitter: penalize over-allocation waste
    w_resource_waste: int = 5
    # Pod-count dimension (capacity planning Phase 1): a single global cap on
    # how many pods one VM-as-K8s-node can hold. Because the cap is global and
    # spec-independent, a pod-count requirement reduces to a floor on the node
    # count (ceil(total_pods / max_pods_per_node)) rather than a placement
    # resource dimension — BMs have no pod capacity, so the solver's placement
    # path is untouched. 0 (default) disables the constraint.
    max_pods_per_node: int = Field(default=0, ge=0)
    # Per-baremetal cap (C4): limit how many VMs sharing the same
    # (cluster_id, ip_type, node_role) can land on a single BM.
    # When auto_generate_max_per_bm=True, default_max_per_bm MUST be set
    # to a positive integer or the request is rejected with INPUT_ERROR.
    auto_generate_max_per_bm: bool = False
    default_max_per_bm: int | None = None
    # Procurement (capacity planning Phase 2): weight for minimizing how many
    # buyable BMs are used. Set high so in-stock BMs are always filled before
    # buying and the buy count is minimized. procurement_spread_dimension is
    # the topology dimension buyable BMs bucket on (must be a spread dimension).
    w_procurement: int = 10_000
    procurement_spread_dimension: str = "ag"
    # Committed stock (already purchased, 缺口 3h) costs less than buying new
    # but more than in-stock, giving the order: in-stock → committed → buy.
    w_committed_stock: int = 100
    # Balance the *resulting* per-bucket available capacity (decision #11):
    # soft-minimize (max − min) of post-placement available CPU cores across
    # the procurement_spread_dimension buckets, so buying tops up the emptiest
    # bucket instead of splitting counts evenly. CPU cores is the balance
    # currency. 0 (default) disables the term.
    w_procurement_balance: int = 0
    # Health-gauge yardsticks (decision #34/#35, both optional):
    # reference_vm_spec — "how many more reference VMs still fit" gauge
    # (remaining_node_slots); min_useful_spec — remaining space that cannot
    # fit even this spec counts as stranded (fragmentation).
    reference_vm_spec: Resources | None = None
    min_useful_spec: Resources | None = None

    # Which topology dimension identifies a fab (site or phase in practice).
    # The multi-period planner groups in-stock BMs into per-fab pools by this
    # dimension; a demand entry's fab "" means single-fab mode (match all).
    fab_topology_dimension: str = "site"

    @field_validator("procurement_spread_dimension", "fab_topology_dimension")
    @classmethod
    def _validate_procurement_spread_dimension(cls, v: str) -> str:
        if v not in SPREAD_DIMENSIONS:
            raise ValueError(
                f"dimension {v!r} is not a valid topology dimension; "
                f"valid: {sorted(SPREAD_DIMENSIONS)}"
            )
        return v

    @field_validator("target_spread")
    @classmethod
    def _validate_target_spread(cls, v: dict[str, int]) -> dict[str, int]:
        unknown = [k for k in v if k not in SPREAD_DIMENSIONS]
        if unknown:
            raise ValueError(
                f"target_spread contains unknown dimension(s) {unknown}; "
                f"valid: {sorted(SPREAD_DIMENSIONS)}"
            )
        bad = {k: val for k, val in v.items() if val < 1}
        if bad:
            raise ValueError(f"target_spread values must be >= 1; got {bad}")
        return v


@lru_cache(maxsize=1)
def _engine_versions() -> tuple[str, str]:
    """(engine, ortools) versions; process-constant, cached. Fallback keeps
    the fingerprint computable when package metadata is absent."""
    def _ver(pkg: str) -> str:
        try:
            return metadata.version(pkg)
        except metadata.PackageNotFoundError:
            return "0+unknown"
    return _ver("vm-placement-solver"), _ver("ortools")


def config_fingerprint(config: SolverConfig) -> str:
    """
    12-hex-char sha256 over (effective SolverConfig, engine version, ortools
    version) — echoed on every response (E0/S4) so the caller can correlate
    a result with the exact solver behavior knobs that produced it, and a
    reconcile pass can flag config/engine drift between plan and execution.
    json.dumps(sort_keys=True) canonicalizes recursively (covers the
    target_spread dict); list order (e.g. vm_specs) stays significant on
    purpose — a different spec order is a different effective config.
    """
    engine, ortools_ver = _engine_versions()
    payload = {
        "config": config.model_dump(mode="json"),
        "engine": engine,
        "ortools": ortools_ver,
    }
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Solver I/O: the JSON contract
# ---------------------------------------------------------------------------

class PlacementRequest(BaseModel):
    """Input: what the Go scheduler sends to the Python solver."""
    vms: list[VM]
    baremetals: list[Baremetal]
    anti_affinity_rules: list[AntiAffinityRule] = Field(default_factory=list)
    max_per_bm_rules: list[MaxPerBaremetalRule] = Field(default_factory=list)
    exclusive_bm_rules: list[ExclusiveBaremetalRule] = Field(default_factory=list)
    failover_rules: list[FailoverRule] = Field(default_factory=list)
    config: SolverConfig = Field(default_factory=SolverConfig)


class PlacementAssignment(BaseModel):
    """One VM → one BM assignment, with the AG for easy verification."""
    vm_id: str
    vm_hostname: str = ""
    baremetal_id: str
    bm_hostname: str = ""
    ag: str = ""


class PlacementResult(BaseModel):
    """Output: what the Python solver returns to the Go scheduler."""
    success: bool
    assignments: list[PlacementAssignment] = Field(default_factory=list)
    solver_status: str = ""
    solve_time_seconds: float = 0.0
    unplaced_vms: list[str] = Field(default_factory=list)
    # BM utilization for this solve: distinct BMs actually placed on, and the
    # total number of BMs provided in the request (len(request.baremetals)).
    bm_used_count: int = 0
    bm_total_count: int = 0
    # sha256[:12] over effective config + engine/ortools versions (E0/S4);
    # "" only in payloads from pre-upgrade peers.
    config_fingerprint: str = ""
    diagnostics: dict[str, Any] = Field(default_factory=dict)

    def to_assignment_map(self) -> dict[str, str]:
        """Convenience: vm_id -> baremetal_id."""
        return {a.vm_id: a.baremetal_id for a in self.assignments}


# ---------------------------------------------------------------------------
# Requirement splitter I/O
# ---------------------------------------------------------------------------

class ResourceRequirement(BaseModel):
    """
    A total resource budget for one node role that the splitter will
    decompose into concrete VM instances.

    vm_specs overrides config.vm_specs for this requirement only.
    min/max_total_vms constrain how many VMs the splitter may create.
    candidate_baremetals is required (scheduler step 3 filtering result).
    Empty candidate_baremetals → no synthetic VMs are produced and any
    explicit VMs with empty candidates are rejected with INPUT_ERROR.
    """
    total_resources: Resources
    node_role: str = "worker"
    cluster_id: str = ""
    ip_type: str = ""
    vm_specs: list[Resources] | None = None
    min_total_vms: int | None = None
    max_total_vms: int | None = None
    # Minimum pod-hosting capacity required for this role ("at least N pods").
    # Combined with SolverConfig.max_pods_per_node it guarantees the split
    # provisions enough nodes to host at least this many pods:
    #   node_count >= ceil(total_pods / max_pods_per_node)
    # so provisioned_capacity = node_count * max_pods_per_node >= total_pods.
    # It is a lower bound, never an exact target or a cap. 0 = no pod demand.
    total_pods: int = Field(default=0, ge=0)
    # Network domain (BGP zone) this cluster lives in. "" = no restriction.
    # Honored by the splitter on every path (split-and-solve and the capacity
    # planner): candidate_baremetals is narrowed to BMs whose network matches,
    # and the planner additionally scopes in-stock backfill and buyable-BM
    # candidates to the domain (whole-cluster filter, 缺口 3g).
    network: str = ""
    # Per-cluster machine-type allowlist for procurement (決議 #38): when set,
    # this requirement's residual demand may only buy / draw these
    # BaremetalType ids. None = any type in the fab.
    allowed_bm_types: list[str] | None = None
    # Dedicated-pool membership (E2/S6). Consumed ONLY by the capacity
    # planner's candidate assembly (like allowed_bm_types); the splitter and
    # plain placement ignore it. Unlike `network`, "" is a distinct domain
    # (shared pool), never a wildcard: pool="" demand sees only shared BMs,
    # pool="X" demand sees only pool-X BMs (strict isolation).
    pool: str = ""
    candidate_baremetals: list[str] = Field(default_factory=list)


class SplitPlacementRequest(BaseModel):
    """Input for the split-and-solve endpoint."""
    requirements: list[ResourceRequirement]
    vms: list[VM] = Field(default_factory=list)
    baremetals: list[Baremetal]
    anti_affinity_rules: list[AntiAffinityRule] = Field(default_factory=list)
    max_per_bm_rules: list[MaxPerBaremetalRule] = Field(default_factory=list)
    failover_rules: list[FailoverRule] = Field(default_factory=list)
    config: SolverConfig = Field(default_factory=SolverConfig)


class SplitDecision(BaseModel):
    """How many VMs of a given spec the solver chose for one role."""
    node_role: str
    vm_spec: Resources
    count: int


class SplitPlacementResult(BaseModel):
    """Output for the split-and-solve endpoint."""
    success: bool
    assignments: list[PlacementAssignment] = Field(default_factory=list)
    split_decisions: list[SplitDecision] = Field(default_factory=list)
    solver_status: str = ""
    solve_time_seconds: float = 0.0
    unplaced_vms: list[str] = Field(default_factory=list)
    # BM utilization for this solve: distinct BMs actually placed on, and the
    # total number of BMs provided in the request (len(request.baremetals)).
    bm_used_count: int = 0
    bm_total_count: int = 0
    # sha256[:12] over effective config + engine/ortools versions (E0/S4).
    config_fingerprint: str = ""
    diagnostics: dict[str, Any] = Field(default_factory=dict)

    def to_assignment_map(self) -> dict[str, str]:
        return {a.vm_id: a.baremetal_id for a in self.assignments}


# ---------------------------------------------------------------------------
# Procurement I/O (capacity planning Phase 2): single fab/period —
# "given demand + in-stock, how many BMs of each type must we buy?"
# ---------------------------------------------------------------------------

class BaremetalType(BaseModel):
    """A buyable BM machine type (no fixed id). 1U assumed (1 BM = 1 slot)."""
    type_id: str
    capacity: Resources
    fab: str = ""


class ProcurementCap(BaseModel):
    """
    Per-bucket procurement slot limit. bucket is a value of the config's
    procurement_spread_dimension (e.g. an AG). A bucket with no cap is treated
    as unlimited (idealized). network is the BGP domain (reserved; unused in
    the single-fab Phase 2 core).
    """
    bucket: str
    max_bm: int = Field(ge=0)
    fab: str = ""
    network: str = ""


class ProcurementDecision(BaseModel):
    """How many BMs of a given type to buy (or draw from committed stock)."""
    type_id: str
    count: int


class CommittedStock(BaseModel):
    """
    Already-purchased machines awaiting allocation (缺口 3h). Modeled as a
    zero-cost procurement tier: the solver drains these before buying new.
    type_id must reference one of the request's procurement_types.
    bucket set → the machines land in that bucket; None → floating, the
    solver picks landing buckets (at most `count` used across all buckets).

    available_from: optional "YYYY-MM" gate, maintained by hand (delivery
    dates float; no automated ETA). In the multi-period planner
    (/v1/capacity/plan) the entry is offered only from that month onward,
    inclusive. The single-shot /v1/capacity/procure endpoint has no period
    concept, so the field is ignored there (entry treated as available now).
    """
    type_id: str
    count: int = Field(ge=0)
    bucket: str | None = None
    network: str = ""
    fab: str = ""
    # Dedicated-pool destination (E2/S6): machines from this PO belong to the
    # named pool; "" = shared pool. Same distinct-domain semantics as
    # Baremetal.pool.
    pool: str = ""
    available_from: str | None = None

    @field_validator("available_from")
    @classmethod
    def _validate_available_from(cls, v: str | None) -> str | None:
        # A malformed month must not pass: lexical compare against "YYYY-MM"
        # periods would sort e.g. "2026/07" after every valid month and
        # silently gate the entry forever.
        if v is not None and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", v):
            raise ValueError(f"available_from must be 'YYYY-MM', got {v!r}")
        return v


class ProcurementRequest(BaseModel):
    """
    Input for the procurement endpoint (single fab/period).

    vms: explicit VMs pass through with their candidate_baremetals untouched
    (the scheduler's step-3 filter is authoritative) — they are placed on
    in-stock BMs only and never on virtual (buyable/committed) ones. Demand
    that should drive procurement belongs in `requirements`.
    """
    requirements: list[ResourceRequirement] = Field(default_factory=list)
    vms: list[VM] = Field(default_factory=list)
    in_stock: list[Baremetal]
    procurement_types: list[BaremetalType]
    procurement_caps: list[ProcurementCap] = Field(default_factory=list)
    committed_stock: list[CommittedStock] = Field(default_factory=list)
    # Machines present but out of candidacy (E2.5): scheduled for a future
    # fleet-event release, they keep their old load, appear in state
    # snapshots, but host nothing new and their free space is NOT usable
    # headroom (excluded from health gauges). Set by the multi-period
    # planner; direct callers may freeze machines the same way.
    frozen_bm_ids: list[str] = Field(default_factory=list)
    anti_affinity_rules: list[AntiAffinityRule] = Field(default_factory=list)
    max_per_bm_rules: list[MaxPerBaremetalRule] = Field(default_factory=list)
    failover_rules: list[FailoverRule] = Field(default_factory=list)
    config: SolverConfig = Field(default_factory=SolverConfig)


class RequirementCoverage(BaseModel):
    """
    Planned-VM counts for one requirement, by supply source (E0/S2).
    Classification is pre-roll-forward: "in_stock" means in-stock at the
    START of this solve — machines acquired in earlier planner months have
    already materialized into in-stock and count as such.
    """
    requirement_index: int
    cluster_id: str = ""
    node_role: str = "worker"
    in_stock: int = 0
    committed: int = 0
    new_buy: int = 0
    total: int = 0                    # == in_stock + committed + new_buy


class ProcurementResult(BaseModel):
    """Output for the procurement endpoint."""
    success: bool
    procurement: list[ProcurementDecision] = Field(default_factory=list)
    # Per-requirement coverage by source; one row per requirement, zeros on
    # a failed solve. Requirement-driven (synthetic) VMs only.
    requirement_coverage: list[RequirementCoverage] = Field(default_factory=list)
    # Draws from committed_stock (already-owned machines put to use).
    committed_used: list[ProcurementDecision] = Field(default_factory=list)
    # Draws per committed_stock entry INDEX (exact-entry accounting so
    # roll-forward drains the pool the solver actually drew from; note JSON
    # serializes the int keys as strings).
    committed_entry_used: dict[int, int] = Field(default_factory=dict)
    split_decisions: list[SplitDecision] = Field(default_factory=list)
    assignments: list[PlacementAssignment] = Field(default_factory=list)
    # "none" | "space" (bucket max_bm exhausted) | "capacity" | "anti_affinity"
    # | "unknown" (solve not proven INFEASIBLE — e.g. time limit — so no cause
    #   can be honestly attributed; see solver_status for the raw status)
    shortfall_cause: str = "none"
    solver_status: str = ""
    solve_time_seconds: float = 0.0
    # sha256[:12] over effective config + engine/ortools versions (E0/S4).
    config_fingerprint: str = ""
    in_stock_bm_used: int = 0
    procured_bm_total: int = 0
    committed_bm_used: int = 0
    # Health gauges (缺口 3c), computed over the post-placement state
    # (in-stock ∪ used committed ∪ bought). nominal_available is the naive
    # sum of leftover capacity (overstates usable space). remaining_node_slots
    # counts how many more config.reference_vm_spec VMs still fit (None when
    # no reference spec configured). stranded_available sums leftovers on BMs
    # that cannot fit even config.min_useful_spec (None when unconfigured).
    nominal_available: Resources = Field(default_factory=Resources)
    remaining_node_slots: int | None = None
    stranded_available: Resources | None = None
    # Post-solve available CPU cores per spread bucket (balance evidence).
    balance_after: dict[str, int] = Field(default_factory=dict)
    # Roll-forward hooks (used by the multi-period planner, Phase 3):
    # per-BM resources consumed by this solve's placement, and the virtual
    # BMs that materialized (bought / drawn from committed stock) with their
    # synthetic topology — append them to in-stock for the next period.
    bm_placed: dict[str, Resources] = Field(default_factory=dict)
    bought_bms: list[Baremetal] = Field(default_factory=list)
    committed_bms: list[Baremetal] = Field(default_factory=list)
    # Machine type of each bought BM (id -> type_id), so downstream reports
    # (budget by model) never have to parse it back out of synthetic ids.
    bought_type_of: dict[str, str] = Field(default_factory=dict)
    diagnostics: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Multi-period capacity planning I/O (Phase 3): sparse demand book →
# month-by-month roll-forward per fab → aggregated CapacityReport.
# ---------------------------------------------------------------------------

class DemandEntry(BaseModel):
    """
    One row of the demand book: the incremental demand for (cluster, role,
    month). Sparse — only filled months exist; revising a month means
    replacing its row (upsert is the caller's job; the solver is stateless).

    Three-state month semantics (決議 #26): a missing row = the month is
    unplanned (absent from the report); a row with all-zero demand = an
    explicit "no growth" month (reported with zero adds); any dimension > 0 =
    demand (a 0 dimension is unconstrained, not "uses 0").
    """
    cluster_id: str
    node_role: str = "worker"
    period: str                       # e.g. "2026-07"; ISO order = sort order
    # Incremental demand; 0 on a dimension = no lower bound on it.
    cpu_cores: int = Field(default=0, ge=0)
    memory_mib: int = Field(default=0, ge=0)
    storage_gb: int = Field(default=0, ge=0)
    pod_count: int = Field(default=0, ge=0)
    vm_specs: list[Resources] | None = None
    min_total_vms: int | None = None
    max_total_vms: int | None = None
    fab: str = ""                     # "" = single-fab mode (matches all BMs)
    network: str = ""                 # BGP domain filter (缺口 3g)
    allowed_bm_types: list[str] | None = None   # 決議 #38
    # Dedicated-pool membership (E2/S6), system-filled from the cluster
    # registry. "" = shared pool (distinct domain, not a wildcard).
    pool: str = ""
    # Caller-supplied opaque id (E0/S2), echoed in demand_coverage. The
    # solver never interprets it; rows without one are still identified by
    # (cluster_id, node_role, period, fab).
    demand_id: str | None = None

    def to_requirement(self) -> ResourceRequirement:
        return ResourceRequirement(
            total_resources=Resources(
                cpu_cores=self.cpu_cores,
                memory_mib=self.memory_mib,
                storage_gb=self.storage_gb,
            ),
            node_role=self.node_role,
            cluster_id=self.cluster_id,
            # Planned demand has no IP assignment yet, but auto anti-affinity
            # groups by (cluster, ip_type, role) and skips empty ip_type; a
            # fixed sentinel keeps planned nodes eligible for AG spreading.
            ip_type="plan",
            vm_specs=self.vm_specs,
            min_total_vms=self.min_total_vms,
            max_total_vms=self.max_total_vms,
            total_pods=self.pod_count,
            network=self.network,
            pool=self.pool,
            allowed_bm_types=self.allowed_bm_types,
        )


class FleetEvent(BaseModel):
    """
    One fleet-event-book row (決議 #40, E2.5): scheduled whole-machine
    release — the named in-stock machines leave their old cluster in `period`
    and return to the pool with used_capacity zeroed. Three fixed semantics:

      - action is `release` only for now (`retire` / `add` are future rows in
        the same book, added as new Literal members when needed);
      - whole-machine granularity: the old load leaves WITH the machine, no
        partial release;
      - pre-event isolation: in every planned month BEFORE `period` the
        machine keeps its old load and is frozen out of candidacy (it hosts
        nothing new), so the plan can never build onto a machine that is
        about to be wiped.

    The single-shot /v1/capacity/procure endpoint has no period concept and
    ignores the event book entirely.
    """
    period: str                       # "YYYY-MM" the release takes effect
    action: Literal["release"]
    bm_ids: list[str] = Field(min_length=1)
    fab: str = ""                     # required in named-fab planning mode

    @field_validator("period")
    @classmethod
    def _validate_period(cls, v: str) -> str:
        # Same guard as CommittedStock.available_from: a malformed month
        # would compare lexically against valid "YYYY-MM" periods and gate
        # (or fire) the event at the wrong time, silently.
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", v):
            raise ValueError(f"period must be 'YYYY-MM', got {v!r}")
        return v


class CapacityPlanRequest(BaseModel):
    """
    Input for the multi-period planning endpoint. The horizon is derived from
    the demand book (the distinct periods present, sorted) — never a fixed 12
    months (決議 #27). Fabs are self-sufficient: each fab's months are solved
    independently with its own rolling state (決議 #4).
    """
    demand_book: list[DemandEntry]
    in_stock: list[Baremetal]
    procurement_types: list[BaremetalType]
    procurement_caps: list[ProcurementCap] = Field(default_factory=list)
    committed_stock: list[CommittedStock] = Field(default_factory=list)
    fleet_events: list[FleetEvent] = Field(default_factory=list)
    anti_affinity_rules: list[AntiAffinityRule] = Field(default_factory=list)
    max_per_bm_rules: list[MaxPerBaremetalRule] = Field(default_factory=list)
    failover_rules: list[FailoverRule] = Field(default_factory=list)
    config: SolverConfig = Field(default_factory=SolverConfig)

    @model_validator(mode="after")
    def _validate_fab_scoping(self) -> CapacityPlanRequest:
        """
        fab="" is the single-fab sentinel (the whole pool). Mixing it with
        named fabs would plan the same physical machines in two independent
        rolling states (capacity sold twice), so the mix is rejected. In
        named-fab mode, stateful supply (caps = physical slots, committed
        stock = owned machines) must name its fab for the same reason;
        procurement_types may stay fab="" — a catalog is stateless.
        """
        fabs = {e.fab for e in self.demand_book}
        if "" in fabs and len(fabs) > 1:
            raise ValueError(
                "demand_book mixes fab='' (single-fab mode) with named fabs; "
                "resolve every entry's fab, or none"
            )
        if fabs and "" not in fabs:
            unscoped_caps = [c.bucket for c in self.procurement_caps if not c.fab]
            if unscoped_caps:
                raise ValueError(
                    "named-fab planning requires every ProcurementCap to name "
                    "its fab (physical slots exist in exactly one fab); "
                    f"unscoped cap bucket(s): {unscoped_caps}"
                )
            unscoped_committed = [
                c.type_id for c in self.committed_stock if not c.fab
            ]
            if unscoped_committed:
                raise ValueError(
                    "named-fab planning requires every CommittedStock entry "
                    "to name its fab (owned machines sit in one fab); "
                    f"unscoped entry type(s): {unscoped_committed}"
                )
            unscoped_events = [
                ev.bm_ids for ev in self.fleet_events if not ev.fab
            ]
            if unscoped_events:
                raise ValueError(
                    "named-fab planning requires every FleetEvent to name "
                    "its fab (a machine sits in one fab); "
                    f"unscoped event machine(s): {unscoped_events}"
                )
        return self

    @model_validator(mode="after")
    def _validate_fleet_events(self) -> CapacityPlanRequest:
        """
        Event-book referential integrity (決議 #40): a dangling bm_id would
        silently release nothing (the event book is a plan of record, typos
        must fail loudly); one machine in two events is ambiguous (released
        twice); a named-fab event must reference machines actually sitting in
        that fab, or the release would be applied in the wrong rolling state.
        """
        if not self.fleet_events:
            return self
        fab_dim = self.config.fab_topology_dimension
        bm_fab = {
            bm.id: getattr(bm.topology, fab_dim) for bm in self.in_stock
        }
        seen: set[str] = set()
        for ev in self.fleet_events:
            for bid in ev.bm_ids:
                if bid not in bm_fab:
                    raise ValueError(
                        f"fleet_events references unknown in-stock machine "
                        f"{bid!r}"
                    )
                if bid in seen:
                    raise ValueError(
                        f"machine {bid!r} appears in more than one fleet "
                        f"event; a machine can be released once"
                    )
                seen.add(bid)
                if ev.fab and bm_fab[bid] != ev.fab:
                    raise ValueError(
                        f"fleet event scoped to fab {ev.fab!r} references "
                        f"machine {bid!r} which sits in fab {bm_fab[bid]!r}"
                    )
        return self


class ShortfallDetail(BaseModel):
    """Structured shortfall cause (決議 #33): what blocked, where, and why."""
    # "capacity" | "anti_affinity" | "space" | "unknown" | "input_error"
    # | "blocked" (month not planned because an earlier month failed this fab)
    cause: str
    bucket: str | None = None
    dimension: str | None = None
    needed: int | None = None
    available: int | None = None
    message: str = ""


class BudgetRow(BaseModel):
    """
    One budgeting line: bought BMs per (fab, bucket, network, month, model).
    Keyed on the planning cell (決議 #37) — a representative datacenter would
    be unreliable when an AG spans several DCs; with
    procurement_spread_dimension="datacenter" the bucket IS the DC. type_id
    splits the count by machine model so finance sees WHAT to buy, not just
    how many.
    """
    fab: str
    bucket: str
    network: str
    # Dedicated-pool destination of the purchase (E2/S6); "" = shared pool.
    pool: str = ""
    period: str
    type_id: str = ""
    bm_count: int


class BucketMonthCell(BaseModel):
    """
    Planning-report drill-down cell at the (fab, bucket, network[, pool],
    month) granularity — never per BM/rack (決議 #21/#37). The in-stock
    figures are the post-month state (what the next month starts from).
    The pool coordinate (E2/S6) only produces extra rows where dedicated
    pools exist; pool-less requests keep their pre-pool cell set.
    """
    fab: str
    bucket: str
    network: str
    # Dedicated-pool coordinate; "" = shared pool.
    pool: str = ""
    period: str
    node_adds: int = 0
    bm_bought: int = 0
    committed_used: int = 0
    # Distinct pre-existing machines this month's placement touched in the
    # cell (a machine hosting five new nodes counts once). Machines bought or
    # drawn from committed in EARLIER months count here too — they are
    # in-stock by the time this month plans.
    in_stock_bm_used: int = 0
    in_stock_total: Resources = Field(default_factory=Resources)
    in_stock_used: Resources = Field(default_factory=Resources)
    in_stock_available: Resources = Field(default_factory=Resources)
    # Landable node slots in this cell's post-month stock (S3): per-BM
    # bin-pack of config.reference_vm_spec, summed over the cell — the
    # fragmentation-honest capacity number nominal Resources cannot give
    # (決議 #5). None when no reference_vm_spec is configured. Frozen
    # machines (pending release, E2.5) contribute zero, same as the gauges.
    in_stock_slots: int | None = None


class DemandCoverage(BaseModel):
    """RequirementCoverage joined back to its demand-book row (E0/S2)."""
    demand_id: str | None = None
    cluster_id: str
    node_role: str
    period: str
    fab: str = ""
    in_stock: int = 0
    committed: int = 0
    new_buy: int = 0
    total: int = 0


class PeriodFabReport(BaseModel):
    """One fab × month: headline counts + evidence + drill-down cells."""
    fab: str
    period: str
    success: bool
    # Headline (node adds vs BM buys are distinct counts, 決議 #23)
    node_adds_total: int = 0
    bm_procurement_total: int = 0
    committed_bm_used: int = 0
    in_stock_bm_used: int = 0
    procurement: list[ProcurementDecision] = Field(default_factory=list)
    committed_used: list[ProcurementDecision] = Field(default_factory=list)
    split_decisions: list[SplitDecision] = Field(default_factory=list)
    shortfalls: list[ShortfallDetail] = Field(default_factory=list)
    solver_status: str = ""
    # Health gauges (缺口 3c)
    nominal_available: Resources = Field(default_factory=Resources)
    remaining_node_slots: int | None = None
    stranded_available: Resources | None = None
    balance_after: dict[str, int] = Field(default_factory=dict)
    cells: list[BucketMonthCell] = Field(default_factory=list)
    # Per-demand coverage by source (E0/S2); empty on blocked stubs.
    demand_coverage: list[DemandCoverage] = Field(default_factory=list)
    # Fleet-event annotations (E2.5): machines whose scheduled release took
    # effect this month (capacity re-entered the pool clean), and machines
    # still frozen this month pending a future release (present in the
    # snapshot, hosting nothing new).
    released_bms: list[str] = Field(default_factory=list)
    frozen_bms: list[str] = Field(default_factory=list)


class CapacityReport(BaseModel):
    """
    Canonical planning output (決議 #24): Web UI and Excel render this JSON.
    Months absent from the demand book are absent here too — absence means
    "unplanned", not "zero growth" (決議 #26).
    """
    success: bool                     # every planned fab-month succeeded
    by_fab_period: list[PeriodFabReport] = Field(default_factory=list)
    # Budgeting projection: bought BMs per (fab, bucket, network, month).
    # Committed stock is already paid for and excluded. Counts only months
    # that succeeded (failed months carry what-if numbers in their own
    # PeriodFabReport, never here).
    budget_view: list[BudgetRow] = Field(default_factory=list)
    # Aggregates over SUCCESSFUL months only, consistent with budget_view.
    totals: dict[str, Any] = Field(default_factory=dict)
    solve_time_seconds: float = 0.0
    # sha256[:12] over effective config + engine/ortools versions (E0/S4).
    config_fingerprint: str = ""


# ---------------------------------------------------------------------------
# Reconcile I/O (E4/S3): plan vs actual calibration — a pure function.
# The solver stores nothing (決議 #25): Go archives each canonical run (G4)
# and posts it back here together with the current real snapshot. The one
# computation Go cannot do is the landable-capacity recount (bin-pack of
# reference_vm_spec per BM) — nominal sums lie under fragmentation (決議 #5);
# everything else is a diff.
# ---------------------------------------------------------------------------

class ExecutionRecord(BaseModel):
    """
    One executed build/add-node batch, as booked by the Go scheduler.
    demand_id joins it back to the plan's demand book; None = the execution
    bypassed the book (a meteor — it feeds unplanned_ratio and demand drift).
    """
    demand_id: str | None = None
    cluster_id: str = ""
    node_role: str = "worker"
    vm_count: int = Field(ge=0)
    status: Literal["success", "failed"]
    period: str                       # "YYYY-MM" the execution belongs to
    fab: str = ""
    infeasible_cause: str | None = None

    @field_validator("period")
    @classmethod
    def _validate_period(cls, v: str) -> str:
        # A malformed month would silently fall out of every period join and
        # the record would vanish from all four metrics.
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", v):
            raise ValueError(f"period must be 'YYYY-MM', got {v!r}")
        return v


class MachineAdd(BaseModel):
    """
    Machines that actually became ready in a cell during a period, counted by
    Go from inventory history (a count diff needs no solver). Compared against
    the plan's bm_bought + committed_used per cell for supply hit rate.
    """
    fab: str = ""
    bucket: str
    network: str = ""
    pool: str = ""
    period: str
    count: int = Field(ge=0)

    @field_validator("period")
    @classmethod
    def _validate_period(cls, v: str) -> str:
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", v):
            raise ValueError(f"period must be 'YYYY-MM', got {v!r}")
        return v


class ActualSnapshot(BaseModel):
    """The world as it is at reconcile time — same formats as planning input."""
    as_of: str                        # "YYYY-MM-DD"; its month is the target
    in_stock: list[Baremetal]
    committed_stock: list[CommittedStock] = Field(default_factory=list)
    executions: list[ExecutionRecord] = Field(default_factory=list)
    machine_adds: list[MachineAdd] = Field(default_factory=list)

    @field_validator("as_of")
    @classmethod
    def _validate_as_of(cls, v: str) -> str:
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])", v):
            raise ValueError(f"as_of must be 'YYYY-MM-DD', got {v!r}")
        return v


class ReconcilePlan(BaseModel):
    """
    The archived canonical run being reconciled against: the CapacityReport
    it produced (predicted cells live in report.by_fab_period[].cells) plus
    the demand book it was fed (the demand_id join space).
    """
    plan_id: str = ""
    created_at: str = ""
    report: CapacityReport
    demand_snapshot: list[DemandEntry] = Field(default_factory=list)


class ReconcileRequest(BaseModel):
    plan: ReconcilePlan
    actual: ActualSnapshot
    config: SolverConfig = Field(default_factory=SolverConfig)


class ReconcileCell(BaseModel):
    """Predicted vs actual state of one (fab, bucket, network, pool) cell for
    the target month. Slots are the landable measure (headline); nominal
    Resources are the auxiliary columns (決議 #5: nominal alone lies)."""
    fab: str = ""
    bucket: str
    network: str = ""
    pool: str = ""
    period: str
    predicted_slots: int | None = None
    actual_slots: int | None = None
    slots_delta: int | None = None    # actual - predicted
    predicted_total: Resources = Field(default_factory=Resources)
    actual_total: Resources = Field(default_factory=Resources)
    predicted_used: Resources = Field(default_factory=Resources)
    actual_used: Resources = Field(default_factory=Resources)
    predicted_available: Resources = Field(default_factory=Resources)
    actual_available: Resources = Field(default_factory=Resources)


class DriftDetail(BaseModel):
    """
    One structured drift finding (mirrors ShortfallDetail's shape). Rule-based
    v1: a cell can legitimately appear under several categories at once
    (e.g. supply late + fleet change) — multiple rows, no forced single
    attribution.
    """
    # "demand" | "supply" | "placement" | "fleet"
    category: str
    fab: str = ""
    bucket: str | None = None
    network: str | None = None
    pool: str | None = None
    period: str = ""
    delta: int = 0
    demand_ids: list[str] = Field(default_factory=list)
    message: str = ""


class ReconcileHeadline(BaseModel):
    """
    The four metrics (決議 #9), each None when its denominator is empty —
    an unmeasurable rate must not masquerade as 0% or 100%. The int fields
    expose numerators/denominators so the UI can show "17/20", not just 85%.
    """
    fulfillment_rate: float | None = None     # 承諾兌現率 (headline)
    planned_vms: int = 0                      # denominator (joinable rows)
    fulfilled_vms: int = 0                    # numerator
    # Planned VMs whose coverage row carries no demand_id — invisible to the
    # join, EXCLUDED from the rate; nonzero means the book has gaps.
    unjoinable_planned_vms: int = 0
    forecast_error: float | None = None       # Σ|Δslots| / Σ predicted slots
    supply_hit_rate: float | None = None
    planned_machine_adds: int = 0
    actual_machine_adds: int = 0
    unplanned_ratio: float | None = None      # meteor share of executed VMs
    executed_vms: int = 0
    unplanned_vms: int = 0


class ReconcileReport(BaseModel):
    """Drift report for the as_of month: headline + per-cell diff + findings.
    Pure output — nothing is persisted on the solver side."""
    success: bool
    status: str = "OK"
    period: str = ""                  # the reconciled month (as_of's month)
    as_of: str = ""
    plan_id: str = ""
    headline: ReconcileHeadline = Field(default_factory=ReconcileHeadline)
    cells: list[ReconcileCell] = Field(default_factory=list)
    drifts: list[DriftDetail] = Field(default_factory=list)
    # Fingerprint of THIS reconcile's config; plan.report carries its own —
    # a mismatch between the two is itself worth flagging in the UI (M2).
    config_fingerprint: str = ""
    plan_config_fingerprint: str = ""
