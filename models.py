"""
VM Placement Solver — Data Models

Step 1: Define the data structures that flow between Go scheduler and Python solver.

Think of this as the "language" both sides speak.
The Go scheduler produces a PlacementRequest (JSON), and the Python solver
returns a PlacementResult (JSON).

Topology: site > phase > datacenter > rack
Virtual:  AG (availability group) — each rack belongs to exactly 1 AG
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Resources: the multi-dimensional "size" of a VM or baremetal
# ---------------------------------------------------------------------------

@dataclass
class Resources:
    """
    Represents resource capacity or demand.

    Why a separate class? Because both VMs and baremetals use the same
    dimensions (cpu, mem, disk, gpu). Having one class lets us write
    generic code that works across all dimensions.
    """
    cpu_cores: int = 0
    memory_mb: int = 0
    disk_gb: int = 0
    gpu_count: int = 0

    def fits_in(self, capacity: Resources) -> bool:
        """Can this demand fit within the given capacity?"""
        return (
            self.cpu_cores <= capacity.cpu_cores
            and self.memory_mb <= capacity.memory_mb
            and self.disk_gb <= capacity.disk_gb
            and self.gpu_count <= capacity.gpu_count
        )

    def __add__(self, other: Resources) -> Resources:
        return Resources(
            cpu_cores=self.cpu_cores + other.cpu_cores,
            memory_mb=self.memory_mb + other.memory_mb,
            disk_gb=self.disk_gb + other.disk_gb,
            gpu_count=self.gpu_count + other.gpu_count,
        )

    def __sub__(self, other: Resources) -> Resources:
        return Resources(
            cpu_cores=self.cpu_cores - other.cpu_cores,
            memory_mb=self.memory_mb - other.memory_mb,
            disk_gb=self.disk_gb - other.disk_gb,
            gpu_count=self.gpu_count - other.gpu_count,
        )


# ---------------------------------------------------------------------------
# Topology: where a baremetal physically lives
# ---------------------------------------------------------------------------

@dataclass
class Topology:
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

@dataclass
class Baremetal:
    """
    A physical host. The Go scheduler fills in total_capacity and used_capacity
    from the inventory API. We compute available = total - used.
    """
    id: str
    total_capacity: Resources
    used_capacity: Resources = field(default_factory=Resources)
    topology: Topology = field(default_factory=Topology)

    @property
    def available_capacity(self) -> Resources:
        return self.total_capacity - self.used_capacity


# ---------------------------------------------------------------------------
# VM: a virtual machine to place
# ---------------------------------------------------------------------------

class NodeRole(str, Enum):
    MASTER = "master"
    WORKER = "worker"
    INFRA = "infra"
    L4LB = "l4lb"


@dataclass
class VM:
    """
    A VM to be placed on a baremetal.

    candidate_baremetals: from Go scheduler step 3 (filtering).
      If set, the solver ONLY considers these BMs.
      If empty, the solver considers all BMs with enough capacity.

    ip_type: network type of the VM (e.g. "routable", "non-routable").
      Used together with node_role as the grouping key for auto-generated
      anti-affinity rules: VMs with the same (ip_type, node_role) should
      spread across different AGs.
    """
    id: str
    demand: Resources
    node_role: NodeRole = NodeRole.WORKER
    ip_type: str = ""
    cluster_id: str = ""
    candidate_baremetals: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Anti-affinity: spread VMs across AGs
# ---------------------------------------------------------------------------

@dataclass
class AntiAffinityRule:
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

@dataclass
class SolverConfig:
    """
    For now, just solver behavior settings.
    We'll add objective weights later when we build the scoring function.
    """
    max_solve_time_seconds: float = 30.0
    num_workers: int = 8
    allow_partial_placement: bool = False

    # Auto-generate anti-affinity rules from (cluster_id, node_role) groups.
    # max_per_ag is computed automatically: ceil(num_vms / num_ags)
    auto_generate_anti_affinity: bool = True


# ---------------------------------------------------------------------------
# Solver I/O: the JSON contract
# ---------------------------------------------------------------------------

@dataclass
class PlacementRequest:
    """Input: what the Go scheduler sends to the Python solver."""
    vms: list[VM]
    baremetals: list[Baremetal]
    anti_affinity_rules: list[AntiAffinityRule] = field(default_factory=list)
    config: SolverConfig = field(default_factory=SolverConfig)


@dataclass
class PlacementAssignment:
    """One VM → one BM assignment, with the AG for easy verification."""
    vm_id: str
    baremetal_id: str
    ag: str = ""


@dataclass
class PlacementResult:
    """Output: what the Python solver returns to the Go scheduler."""
    success: bool
    assignments: list[PlacementAssignment] = field(default_factory=list)
    solver_status: str = ""
    solve_time_seconds: float = 0.0
    unplaced_vms: list[str] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_assignment_map(self) -> dict[str, str]:
        """Convenience: vm_id -> baremetal_id."""
        return {a.vm_id: a.baremetal_id for a in self.assignments}
