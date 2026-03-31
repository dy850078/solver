"""
═══════════════════════════════════════════════════════════════
  Exercise 5: GPU Affinity Constraint
  Tier 2 — Solver Extensions | 預計時間: 25 分鐘
═══════════════════════════════════════════════════════════════

【學習目標】
  1. 學會新增一個全新的 hard constraint（從需求到建模）
  2. 學會用整數算術避免浮點（ratio 用分子/分母表達）
  3. 學會條件式 constraint（只在 bm_used 時生效）
  4. 學會在 solve pipeline 中插入新 constraint

【背景故事】
  GPU 伺服器很貴。如果非 GPU 的 workload 把 GPU BM 佔滿了，
  真正需要 GPU 的 VM 就沒地方放了。

  規則: 對每台有 GPU 的 BM，如果上面放了任何 VM，
  則 GPU VM 的數量必須佔至少 min_gpu_ratio 的比例。

  例如 ratio=1/2: GPU BM 上至少一半的 VM 是 GPU workload。
  例如 ratio=1/1: GPU BM 上只能放 GPU VM。

  浮點數在 CP-SAT 中不能直接使用（CP-SAT 是整數 solver），
  所以 ratio 用 numerator/denominator 表達:
    gpu_count × denominator ≥ total_count × numerator

【前置知識】
  - 完成 Exercise 1-4
  - 讀過 app/solver.py _build_bm_used_vars() (L337-357)
  - 理解 bm_used[bm_id] 如何表達「BM 上有沒有 VM」

【你的任務】
  1. 繼承 VMPlacementSolver 為 GpuAffinitySolver
  2. 新增 _add_gpu_affinity_constraints() 方法
  3. Override solve() 在 _add_anti_affinity_constraints() 之後呼叫它
  4. 實作 solve_with_gpu_affinity() 便利函式

【提示】（卡住時再展開）

  Hint 1 — 分類 VM:
  > gpu_vm_ids = {vm.id for vm in self.request.vms if vm.demand.gpu_count > 0}

  Hint 2 — 對每台 GPU BM 加約束:
  > for bm in self.request.baremetals:
  >     if bm.total_capacity.gpu_count <= 0:
  >         continue  # 不是 GPU BM，跳過
  >     # 收集這台 BM 上的 assign vars...

  Hint 3 — 整數算術避浮點:
  > # gpu_count_on_bm × denom ≥ total_count_on_bm × numer
  > # 但只在 BM 有 VM 時生效
  > gpu_on_bm = sum(var for vm_id, var in assigned if vm_id in gpu_vm_ids)
  > total_on_bm = sum(var for _, var in assigned)
  > self.model.add(gpu_on_bm * denom >= total_on_bm * numer)

  Hint 4 — 條件式約束（BM 沒用時不生效）:
  > 方法 A: 使用 add().only_enforce_if(bm_used[bm.id])
  >   self._ensure_bm_used_vars()
  >   self.model.add(
  >       gpu_on_bm * denom >= total_on_bm * numer
  >   ).only_enforce_if(self.bm_used[bm.id])
  >
  > 方法 B: total_on_bm == 0 時 gpu_on_bm 也是 0，約束自然成立

【預期效益】
  完成後你會理解:
  - 如何在 CP-SAT 中表達 ratio constraint（整數算術）
  - 條件式約束的兩種實作方式（only_enforce_if vs 自然成立）
  - 如何在現有 solve pipeline 中正確插入新 constraint

【相關閱讀】
  - 本專案: app/solver.py _build_bm_used_vars() (L337-357)
  - 本專案: app/solver.py solve() pipeline (L586-654)
  - OR-Tools: https://developers.google.com/optimization/cp/channeling
═══════════════════════════════════════════════════════════════
"""

from __future__ import annotations
import time
import logging

from ortools.sat.python import cp_model

from app.models import PlacementRequest, PlacementResult, SolverConfig
from app.solver import VMPlacementSolver

logger = logging.getLogger(__name__)


class GpuAffinitySolver(VMPlacementSolver):
    """
    Extended solver enforcing a minimum GPU VM ratio on GPU-capable BMs.
    """

    def __init__(
        self,
        request: PlacementRequest,
        *,
        min_gpu_numerator: int = 1,
        min_gpu_denominator: int = 2,
        **kwargs,
    ):
        super().__init__(request, **kwargs)
        self.min_gpu_numerator = min_gpu_numerator
        self.min_gpu_denominator = min_gpu_denominator

    def _add_gpu_affinity_constraints(self):
        """
        For each GPU-capable BM with VMs placed on it:
          gpu_vm_count × denominator ≥ total_vm_count × numerator
        """
        raise NotImplementedError("YOUR CODE HERE")

    def solve(self) -> PlacementResult:
        """Override to inject GPU affinity constraints into the pipeline."""
        # The base solve() calls:
        #   _build_variables()
        #   _add_one_bm_per_vm_constraint()
        #   _add_capacity_constraints()
        #   _add_anti_affinity_constraints()
        #   _add_objective()
        #   solve
        #
        # You need to add _add_gpu_affinity_constraints() at the right point.
        # Hint: override solve() and call self._add_gpu_affinity_constraints()
        # after building variables but before solving.
        raise NotImplementedError("YOUR CODE HERE")


def solve_with_gpu_affinity(
    vms, bms, min_gpu_numerator=1, min_gpu_denominator=2, **config_overrides,
) -> PlacementResult:
    """Convenience wrapper for testing."""
    cfg = dict(max_solve_time_seconds=10, auto_generate_anti_affinity=False)
    cfg.update(config_overrides)
    request = PlacementRequest(
        vms=vms, baremetals=bms,
        anti_affinity_rules=[],
        config=SolverConfig(**cfg),
    )
    solver = GpuAffinitySolver(
        request,
        min_gpu_numerator=min_gpu_numerator,
        min_gpu_denominator=min_gpu_denominator,
    )
    return solver.solve()
