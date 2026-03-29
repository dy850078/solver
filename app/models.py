"""
VM Placement Solver — Data Models

Uses Pydantic v2 BaseModel for automatic JSON serialization/deserialization
and type validation on construction.

Topology: site > phase > datacenter > rack
Virtual:  AG (availability group) — each rack belongs to exactly 1 AG
"""

from __future__ import annotations
from enum import Enum
from typing import Any, Literal

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
    bm_role: str = ""
    max_vm_count: int | None = None
    current_vm_count: int = 0

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
# Cross-cluster constraints
# ---------------------------------------------------------------------------

class ExistingVM(BaseModel):
    """
    A VM that already exists on a BM (from a previous scheduling run).
    Used for cross-cluster topology rules.
    """
    vm_id: str
    cluster_id: str
    baremetal_id: str
    topology: Topology = Field(default_factory=Topology)


# Topology scope hierarchy (finest → coarsest)
TOPOLOGY_SCOPES = ["rack", "datacenter", "phase", "site"]


class TopologyRule(BaseModel):
    """
    Cross-cluster topology affinity/anti-affinity rule.

    - anti_affinity: VMs from these clusters must not share the same topology zone
    - affinity: VMs from these clusters should prefer the same topology zone

    enforcement:
      - anti_affinity defaults to "hard" (can be "soft")
      - affinity is always "soft" (hard is auto-downgraded with a warning)
    """
    rule_id: str
    cluster_ids: list[str]
    scope: Literal["rack", "datacenter", "phase", "site"]
    type: Literal["affinity", "anti_affinity"]
    enforcement: Literal["hard", "soft"] = "hard"
    weight: int = 1


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
    # Objective function weights
    w_consolidation: int = 10
    w_headroom: int = 8
    headroom_upper_bound_pct: int = 90
    # Slot score: penalize placements that leave unusable leftover capacity
    w_slot_score: int = 0
    slot_tshirt_sizes: list[Resources] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Solver I/O: the JSON contract
# ---------------------------------------------------------------------------

class PlacementRequest(BaseModel):
    """Input: what the Go scheduler sends to the Python solver."""
    vms: list[VM]
    baremetals: list[Baremetal]
    anti_affinity_rules: list[AntiAffinityRule] = Field(default_factory=list)
    config: SolverConfig = Field(default_factory=SolverConfig)
    existing_vms: list[ExistingVM] = Field(default_factory=list)
    topology_rules: list[TopologyRule] = Field(default_factory=list)


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
