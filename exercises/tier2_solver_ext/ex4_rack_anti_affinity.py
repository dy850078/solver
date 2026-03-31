"""
═══════════════════════════════════════════════════════════════
  Exercise 4: Rack-Level Anti-Affinity
  Tier 2 — Solver Extensions | 預計時間: 20 分鐘
═══════════════════════════════════════════════════════════════

【學習目標】
  1. 學會繼承 VMPlacementSolver 並 override 方法來擴展功能
  2. 學會按 topology 欄位（rack）分組，對每組加約束
  3. 理解「同一個 model 疊加多層 constraint」的效果

【背景故事】
  目前 solver 的 anti-affinity 是 AG-level:
    ∀ rule, ∀ AG: count(VMs in group on AG) ≤ max_per_ag

  但真實場景中，一個 rack 斷電會影響該 rack 上的所有 BM。
  所以需要 rack-level anti-affinity:
    ∀ rule, ∀ rack: count(VMs in group on rack) ≤ max_per_rack

  你需要在不修改原始 solver.py 的情況下，透過繼承加入這個約束。

【前置知識】
  - 完成 Exercise 1-3
  - 讀過 app/solver.py _add_anti_affinity_constraints() (L307-331)
  - 理解 self.ag_to_bms 如何按 AG 分組 BM

【你的任務】
  1. 定義 RackAntiAffinityRule model
  2. 繼承 VMPlacementSolver 為 RackAwareSolver
  3. Override _add_anti_affinity_constraints() 加入 rack-level 約束
  4. 實作 solve_with_rack_anti_affinity() 便利函式

【提示】（卡住時再展開）

  Hint 1 — Rack 分組:
  > 類似 self.ag_to_bms 的做法，建 rack_to_bms:
  > rack_to_bms = defaultdict(list)
  > for bm in self.request.baremetals:
  >     rack_to_bms[bm.topology.rack].append(bm.id)

  Hint 2 — Rack 約束:
  > for rule in self.rack_rules:
  >     for rack, rack_bm_ids in rack_to_bms.items():
  >         vars_in_rack = [
  >             self.assign[(vm_id, bm_id)]
  >             for vm_id in rule.vm_ids
  >             for bm_id in rack_bm_ids
  >             if (vm_id, bm_id) in self.assign
  >         ]
  >         if vars_in_rack:
  >             self.model.add(sum(vars_in_rack) <= rule.max_per_rack)

  Hint 3 — 繼承策略:
  > class RackAwareSolver(VMPlacementSolver):
  >     def __init__(self, request, rack_rules=None, **kwargs):
  >         super().__init__(request, **kwargs)
  >         self.rack_rules = rack_rules or []

  Hint 4 — Override:
  > def _add_anti_affinity_constraints(self):
  >     super()._add_anti_affinity_constraints()  # 保留 AG-level
  >     # 加 rack-level ...

【預期效益】
  完成後你會理解:
  - 如何在不動原始 code 的情況下擴展 solver 功能
  - Anti-affinity 約束可以疊加在不同 topology 層級
  - 多層約束同時作用時 solver 如何找到滿足所有層級的解

【相關閱讀】
  - 本專案: app/solver.py _add_anti_affinity_constraints() (L307-331)
  - 本專案: app/solver.py __init__() self.ag_to_bms 的建構 (L121-124)
  - 本專案: app/models.py Topology (L64-73)
  - 本專案: docs/constraints.md anti-affinity 章節
═══════════════════════════════════════════════════════════════
"""

from __future__ import annotations
from collections import defaultdict

from pydantic import BaseModel

from app.models import (
    PlacementRequest, PlacementResult, SolverConfig,
    AntiAffinityRule,
)
from app.solver import VMPlacementSolver


class RackAntiAffinityRule(BaseModel):
    """Rack-level anti-affinity: at most max_per_rack VMs from this group per rack."""
    group_id: str
    vm_ids: list[str]
    max_per_rack: int = 2


class RackAwareSolver(VMPlacementSolver):
    """
    Extended solver with rack-level anti-affinity support.

    Inherits all AG-level anti-affinity from VMPlacementSolver,
    adds rack-level constraints on top.
    """

    def __init__(
        self,
        request: PlacementRequest,
        *,
        rack_rules: list[RackAntiAffinityRule] | None = None,
        **kwargs,
    ):
        super().__init__(request, **kwargs)
        self.rack_rules = rack_rules or []

    def _add_anti_affinity_constraints(self):
        """Add AG-level (inherited) + rack-level anti-affinity constraints."""
        super()._add_anti_affinity_constraints()

        # YOUR CODE HERE: add rack-level constraints
        raise NotImplementedError("YOUR CODE HERE")


def solve_with_rack_anti_affinity(
    vms, bms, rack_rules=None, ag_rules=None, **config_overrides,
) -> PlacementResult:
    """Convenience wrapper for testing."""
    cfg = dict(max_solve_time_seconds=10, auto_generate_anti_affinity=False)
    cfg.update(config_overrides)
    request = PlacementRequest(
        vms=vms, baremetals=bms,
        anti_affinity_rules=ag_rules or [],
        config=SolverConfig(**cfg),
    )
    solver = RackAwareSolver(request, rack_rules=rack_rules or [])
    return solver.solve()
