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
    L4LB = "l4lb"


class VM(BaseModel):
    """
    A VM to be placed on a baremetal.

    candidate_baremetals: from Go scheduler step 3 (filtering).
      If set, the solver ONLY considers these BMs.
      If empty, the solver considers all BMs with enough capacity.

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
# Anti-affinity: spread VMs across AGs
# ---------------------------------------------------------------------------

class AntiAffinityRule(BaseModel):
    """
    "These VMs should NOT all land in the same AG."

    Example: 3 master VMs with max_per_ag=1 means each master
    must be in a different AG (for HA).
    """
    group_id: str
    vm_ids: list[str]
    max_per_ag: int = 1


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
    # Purchase planner: penalize each hypothetical BM that the solver decides to "buy"
    # (weight is multiplied by the per-candidate `cost`). Large default so "buy one
    # fewer BM" dominates softer objectives like consolidation/headroom without
    # forcing the solver to accept infeasibility.
    w_purchase_cost: int = 1000


# ---------------------------------------------------------------------------
# Solver I/O: the JSON contract
# ---------------------------------------------------------------------------

class PlacementRequest(BaseModel):
    """Input: what the Go scheduler sends to the Python solver."""
    vms: list[VM]
    baremetals: list[Baremetal]
    anti_affinity_rules: list[AntiAffinityRule] = Field(default_factory=list)
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


# ---------------------------------------------------------------------------
# Purchase planner I/O
# ---------------------------------------------------------------------------

class PurchaseCandidate(BaseModel):
    """
    A kind of Baremetal the PM is considering purchasing.

    The solver treats each candidate as a pool of up to `max_quantity`
    identical hypothetical machines and decides how many to actually
    "buy" (i.e. place VMs on). Set `cost` to express a relative price
    ratio across candidates; leave at 1.0 to minimize total count.

    `topology_template` controls AG / rack placement for spreading.
    All hypothetical BMs from the same candidate share this topology;
    to model a multi-AG purchase, declare one candidate per AG.
    """
    spec: Resources
    topology_template: Topology = Field(default_factory=Topology)
    max_quantity: int | None = None
    cost: float = 1.0
    label: str = ""


class PurchasePlanningRequest(BaseModel):
    """Input for the purchase planning endpoint."""
    requirements: list[ResourceRequirement]
    purchase_candidates: list[PurchaseCandidate]
    existing_baremetals: list[Baremetal] = Field(default_factory=list)
    vms: list[VM] = Field(default_factory=list)
    anti_affinity_rules: list[AntiAffinityRule] = Field(default_factory=list)
    config: SolverConfig = Field(default_factory=SolverConfig)


class CandidatePurchaseDecision(BaseModel):
    """
    Solver's recommendation for one PurchaseCandidate.

    `recommended_quantity` is how many of this BM kind to buy.
    `used_count` equals `recommended_quantity` when symmetry breaking is
    working correctly (buy only machines you actually use).
    `avg_utilization` is keyed by resource field (cpu_cores/memory_mib/...)
    and ranges 0.0..1.0, computed across the purchased machines.
    """
    label: str
    spec: Resources
    recommended_quantity: int
    used_count: int
    avg_utilization: dict[str, float] = Field(default_factory=dict)


class PurchasePlanningResult(BaseModel):
    """Output for the purchase planning endpoint."""
    feasible: bool
    purchase_decisions: list[CandidatePurchaseDecision] = Field(default_factory=list)
    split_decisions: list[SplitDecision] = Field(default_factory=list)
    assignments: list[PlacementAssignment] = Field(default_factory=list)
    unplaced_vms: list[str] = Field(default_factory=list)
    solver_status: str = ""
    solve_time_seconds: float = 0.0
    diagnostics: dict[str, Any] = Field(default_factory=dict)

    def to_assignment_map(self) -> dict[str, str]:
        return {a.vm_id: a.baremetal_id for a in self.assignments}
