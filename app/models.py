"""
VM Placement Solver — Data Models

Uses Pydantic v2 BaseModel for automatic JSON serialization/deserialization
and type validation on construction.

Topology: site > phase > datacenter > rack
Virtual:  AG (availability group) — each rack belongs to exactly 1 AG
"""

from __future__ import annotations
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


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
    Physical: site > phase > datacenter > rack
    Virtual:  AG (availability group) — the key for anti-affinity spreading
    """
    site: str = ""
    phase: str = ""
    datacenter: str = ""
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

    @property
    def available_capacity(self) -> Resources:
        return self.total_capacity - self.used_capacity


# ---------------------------------------------------------------------------
# VM: a virtual machine to place
# ---------------------------------------------------------------------------

class NodeRole(str, Enum):
    """Node role enum. str mixin allows Pydantic to parse directly from JSON strings."""
    MASTER = "master"
    WORKER = "worker"
    INFRA = "infra"
    L4LB = "l4lb-storage"


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
    node_role: NodeRole = NodeRole.WORKER
    ip_type: str = ""
    cluster_id: str = ""
    candidate_baremetals: list[str] = Field(default_factory=list)


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
    node_role: NodeRole | None = None

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
    "These VMs should NOT all land in the same AG."

    Example: 3 master VMs with max_per_ag=1 means each master
    must be in a different AG (for HA).

    Group membership: provide exactly one of `vm_ids` or `selector`.
    `selector` is resolved against the request's VM list at solve time.
    """
    group_id: str
    vm_ids: list[str] = Field(default_factory=list)
    selector: GroupSelector | None = None
    max_per_ag: int = 1


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
    # this many AGs. When infra has fewer AGs (or the group has fewer VMs),
    # the solver still succeeds but emits an advisory into diagnostics.
    target_ag_spread: int = 3
    # Objective function weights
    w_consolidation: int = 10
    w_headroom: int = 8
    headroom_upper_bound_pct: int = 90
    # Slot score: penalize placements that leave unusable leftover capacity
    w_slot_score: int = 0
    vm_specs: list[Resources] = Field(default_factory=list)
    # Requirement splitter: penalize over-allocation waste
    w_resource_waste: int = 5
    # Per-baremetal cap (C4): limit how many VMs sharing the same
    # (cluster_id, ip_type, node_role) can land on a single BM.
    # When auto_generate_max_per_bm=True, default_max_per_bm MUST be set
    # to a positive integer or the request is rejected with INPUT_ERROR.
    auto_generate_max_per_bm: bool = False
    default_max_per_bm: int | None = None


# ---------------------------------------------------------------------------
# Solver I/O: the JSON contract
# ---------------------------------------------------------------------------

class PlacementRequest(BaseModel):
    """Input: what the Go scheduler sends to the Python solver."""
    vms: list[VM]
    baremetals: list[Baremetal]
    anti_affinity_rules: list[AntiAffinityRule] = Field(default_factory=list)
    max_per_bm_rules: list[MaxPerBaremetalRule] = Field(default_factory=list)
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
    node_role: NodeRole = NodeRole.WORKER
    cluster_id: str = ""
    ip_type: str = ""
    vm_specs: list[Resources] | None = None
    min_total_vms: int | None = None
    max_total_vms: int | None = None
    candidate_baremetals: list[str] = Field(default_factory=list)


class SplitPlacementRequest(BaseModel):
    """Input for the split-and-solve endpoint."""
    requirements: list[ResourceRequirement]
    vms: list[VM] = Field(default_factory=list)
    baremetals: list[Baremetal]
    anti_affinity_rules: list[AntiAffinityRule] = Field(default_factory=list)
    max_per_bm_rules: list[MaxPerBaremetalRule] = Field(default_factory=list)
    config: SolverConfig = Field(default_factory=SolverConfig)


class SplitDecision(BaseModel):
    """How many VMs of a given spec the solver chose for one role."""
    node_role: NodeRole
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
    diagnostics: dict[str, Any] = Field(default_factory=dict)

    def to_assignment_map(self) -> dict[str, str]:
        return {a.vm_id: a.baremetal_id for a in self.assignments}
