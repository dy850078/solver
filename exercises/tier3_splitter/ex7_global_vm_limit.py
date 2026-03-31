"""
═══════════════════════════════════════════════════════════════
  Exercise 7: 跨 Requirement 全域 VM 數量上限
  Tier 3 — Splitter & Joint Model | 預計時間: 40 分鐘
═══════════════════════════════════════════════════════════════

【學習目標】
  1. 理解 split_solver.py 的 orchestration flow（哪個時間點什麼東西已就緒）
  2. 學會在 joint model 上加入跨 splitter 和 solver 邊界的 constraint
  3. 學會處理 explicit VM + synthetic VM 混合計數

【背景故事】
  SplitPlacementRequest 可以包含多個 ResourceRequirement
  （例如 worker + master）。目前每個 requirement 有自己的
  min/max_total_vms，但沒有「跨 requirement 的全域限制」。

  真實場景: 一個 Kubernetes cluster 總共不能超過 50 台 VM，
  不管什麼 role，因為 control plane 有管理上限。

  你需要在 joint model 上加入:
    Σ count_var[(req_i, spec_j)] for ALL i, ALL j ≤ global_max_vms

  這看似簡單，但重點是你要深入理解 split_solver.py 的 flow，
  知道在哪個時間點 count_vars 已經被建好，才能在正確位置加約束。

【前置知識】
  - 完成 Exercise 1-6
  - 讀過 app/split_solver.py solve_split_placement() (全部 89 行)
  - 讀過 app/splitter.py count_vars 的建立時機 (_build_requirement)
  - 理解 shared CpModel 的概念

【你的任務】
  實作 solve_split_with_global_limit() 函式。
  可以複製 split_solver.py 的 flow 並在適當位置加入 global constraint。

【函式簽名】
  def solve_split_with_global_limit(
      requirements, bms,
      global_max_vms=None,
      global_min_vms=None,
      explicit_vms=None,
      rules=None,
      **config_overrides,
  ) -> SplitPlacementResult

  重點: explicit_vms 也算在全域計數裡！

【提示】（卡住時再展開）

  Hint 1 — Flow 順序:
  > 1. model = CpModel()
  > 2. splitter = ResourceSplitter(model=model, ...)
  > 3. synthetic_vms = splitter.build()
  >    ← 此時 count_vars 已經建好！可以加 global constraint
  > 4. 加 global constraint
  > 5. 建 PlacementRequest, VMPlacementSolver
  > 6. solve

  Hint 2 — Global max constraint:
  > all_counts = list(splitter.count_vars.values())
  > if global_max_vms is not None:
  >     # 扣掉 explicit VMs 的名額
  >     synthetic_budget = global_max_vms - len(explicit_vms or [])
  >     model.add(sum(all_counts) <= synthetic_budget)

  Hint 3 — Global min constraint:
  > if global_min_vms is not None:
  >     synthetic_min = global_min_vms - len(explicit_vms or [])
  >     model.add(sum(all_counts) >= max(0, synthetic_min))

  Hint 4 — 完整 flow:
  > 基本上是 split_solver.py 的 solve_split_placement() 加上
  > 在 splitter.build() 之後、VMPlacementSolver 建立之前加 constraint。
  > 注意要正確傳遞 active_vars 和 waste terms。

【預期效益】
  完成後你會理解:
  - split_solver.py 的 orchestration 為什麼要按那個順序執行
  - 在 joint model 中，你可以在 splitter 和 solver 之間加入跨模組約束
  - explicit + synthetic VM 混合場景的 constraint 設計

【相關閱讀】
  - 本專案: app/split_solver.py solve_split_placement() (完整 89 行)
  - 本專案: app/splitter.py count_vars, active_vars 的建立時機
  - 本專案: docs/reading-guide-splitter.md §5 兩者如何被串在一起
═══════════════════════════════════════════════════════════════
"""

from __future__ import annotations
import time

from ortools.sat.python import cp_model

from app.models import (
    PlacementRequest, SplitPlacementRequest, SplitPlacementResult,
    SolverConfig, ResourceRequirement, Baremetal, VM, AntiAffinityRule,
    NodeRole, Resources,
)
from app.splitter import ResourceSplitter
from app.solver import VMPlacementSolver


def solve_split_with_global_limit(
    requirements,
    bms,
    global_max_vms: int | None = None,
    global_min_vms: int | None = None,
    explicit_vms: list | None = None,
    rules: list | None = None,
    **config_overrides,
) -> SplitPlacementResult:
    """
    Split-and-solve with optional global VM count limits.

    Like solve_split_placement(), but adds:
      Σ all count_vars + len(explicit_vms) ≤ global_max_vms
      Σ all count_vars + len(explicit_vms) ≥ global_min_vms

    Args:
        requirements: list of ResourceRequirement
        bms: list of Baremetal
        global_max_vms: max total VMs across all requirements + explicit
        global_min_vms: min total VMs across all requirements + explicit
        explicit_vms: pre-specified VMs (count toward global limit)
        rules: anti-affinity rules
        **config_overrides: override SolverConfig fields

    Returns:
        SplitPlacementResult
    """
    raise NotImplementedError("YOUR CODE HERE")
