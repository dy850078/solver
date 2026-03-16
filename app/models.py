"""
VM Placement Solver — Data Models

Pydantic v2 BaseModel 取代 dataclass 的好處：
  - model_validate_json(str) 直接把 JSON 字串轉成 Python 物件（不需要 serialization.py）
  - model_dump_json() 把 Python 物件轉回 JSON 字串
  - 建立物件時自動驗證型別，送錯資料立刻有清楚的錯誤訊息

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

    為什麼用獨立的 class？因為 VM 和 Baremetal 都用同樣的維度（cpu, mem, disk, gpu），
    寫一個 class 就可以讓 solver 用統一的方式處理所有維度。

    Pydantic 注意事項：
      - 欄位宣告方式和 dataclass 一樣：cpu_cores: int = 0
      - 建立方式也一樣：Resources(cpu_cores=4, memory_mib=16000)
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
    from the inventory API. We compute available = total - used.

    Pydantic 注意事項：
      - Field(default_factory=Resources) 取代 dataclass 的 field(default_factory=Resources)
        意思一樣：「預設值是一個新建的空 Resources()」
      - available_capacity 用普通的 @property 就好，Pydantic 允許這樣做。
        因為它不是輸入欄位，只是計算用，所以不需要出現在 JSON 裡。
    """
    id: str
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
    """
    str, Enum 讓 Pydantic 可以直接從 JSON 字串（如 "master"）解析成 NodeRole.MASTER。
    不需要在 serialization.py 裡手動 NodeRole(d.get("node_role", "worker"))。
    """
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
    # 目標函數 weights
    w_consolidation: int = 10
    w_headroom: int = 8
    headroom_upper_bound_pct: int = 90
    # Slot score: penalize placements that leave unusable leftover capacity
    w_slot_score: int = 0
    slot_tshirt_sizes: list[Resources] = Field(default_factory=lambda: [
        Resources(cpu_cores=4, memory_mib=16_000, storage_gb=100),    # small
        Resources(cpu_cores=8, memory_mib=32_000, storage_gb=200),    # medium
        Resources(cpu_cores=16, memory_mib=64_000, storage_gb=400),   # large
    ])


# ---------------------------------------------------------------------------
# Solver I/O: the JSON contract
# ---------------------------------------------------------------------------

class PlacementRequest(BaseModel):
    """
    Input: what the Go scheduler sends to the Python solver.

    使用方式：
      request = PlacementRequest.model_validate_json(json_string)
      request = PlacementRequest.model_validate(python_dict)
    """
    vms: list[VM]
    baremetals: list[Baremetal]
    anti_affinity_rules: list[AntiAffinityRule] = Field(default_factory=list)
    config: SolverConfig = Field(default_factory=SolverConfig)


class PlacementAssignment(BaseModel):
    """One VM → one BM assignment, with the AG for easy verification."""
    vm_id: str
    baremetal_id: str
    ag: str = ""


class PlacementResult(BaseModel):
    """
    Output: what the Python solver returns to the Go scheduler.

    使用方式：
      json_string = result.model_dump_json(indent=2)
      python_dict = result.model_dump()
    """
    success: bool
    assignments: list[PlacementAssignment] = Field(default_factory=list)
    solver_status: str = ""
    solve_time_seconds: float = 0.0
    unplaced_vms: list[str] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)

    def to_assignment_map(self) -> dict[str, str]:
        """Convenience: vm_id -> baremetal_id."""
        return {a.vm_id: a.baremetal_id for a in self.assignments}
