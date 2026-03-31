"""
═══════════════════════════════════════════════════════════════
  Exercise 6: Spec Preference Soft Constraint
  Tier 3 — Splitter & Joint Model | 預計時間: 30 分鐘
═══════════════════════════════════════════════════════════════

【學習目標】
  1. 學會在 splitter 的 objective 中加入新的 soft term
  2. 理解 hard constraint（保正確性）vs soft term（導方向）的設計哲學
  3. 學會多 objective term 之間的 weight 平衡

【背景故事】
  目前 splitter 只有 waste minimization 一個 objective term。
  但運維團隊有規格偏好: 例如偏好用 12c48g 而不是 8c32g，
  因為大規格 = 少開機器 = 少管 OS patch = 管理成本低。

  你需要在 splitter 中加入 preference term:
    -w_preference × Σ (preference_weight[spec] × count_var[spec])

  關鍵: preference 是 soft 的。如果高偏好 spec 塞不進 BM，
  solver 仍會選能放進去的 spec。waste 和 preference 共存，
  weight 設定決定誰優先。

【前置知識】
  - 完成 Exercise 1-5
  - 讀過 app/splitter.py build_waste_objective_terms() (L192-213)
  - 理解 count_vars dict 的結構: (req_idx, spec_idx) → IntVar

【你的任務】
  1. 繼承 ResourceSplitter 為 PreferenceSplitter
  2. 接受 spec_preferences 和 w_preference 參數
  3. 實作 build_preference_terms() 回傳 objective terms

【函式簽名】
  spec_preferences: dict[tuple[int,int,int,int], int]
    key = (cpu_cores, memory_mib, storage_gb, gpu_count)
    value = preference weight (越大越偏好)

  build_preference_terms() -> list[cp_model.LinearExprT]
    回傳一組 expression，加進 model.minimize() 中
    (因為是 minimize，所以偏好要取負: -weight × count)

【提示】（卡住時再展開）

  Hint 1 — Spec 的 key:
  > 用 (spec.cpu_cores, spec.memory_mib, spec.storage_gb, spec.gpu_count)
  > 作為 dict key 來查 preference weight

  Hint 2 — 遍歷 count_vars:
  > for (req_idx, spec_idx), count_var in self.count_vars.items():
  >     spec = self._req_specs[req_idx][spec_idx]
  >     key = (spec.cpu_cores, spec.memory_mib, spec.storage_gb, spec.gpu_count)
  >     weight = self.spec_preferences.get(key, 0)

  Hint 3 — Objective term 方向:
  > minimize 中: -weight × count → weight 越大，count 越大越好（reward）
  > terms.append(-self.w_preference * weight * count_var)

  Hint 4 — 平衡設計:
  > waste_weight × waste_amount vs preference_weight × preference_score
  > 如果 waste_weight=10, w_preference=1 → waste 壓倒 preference
  > 如果 waste_weight=1, w_preference=10 → preference 壓倒 waste

【預期效益】
  完成後你會理解:
  - 在 CP-SAT 中 soft constraint = objective term 的設計模式
  - 多個 objective term 如何用 weight 控制優先級
  - 如何在不動原始 splitter code 的情況下擴展 objective

【相關閱讀】
  - 本專案: app/splitter.py build_waste_objective_terms() (L192-213)
  - 本專案: app/split_solver.py objective injection (L70)
  - 本專案: app/solver.py _add_objective() (L543-580)
  - 本專案: docs/objective-function.md
═══════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from ortools.sat.python import cp_model

from app.models import (
    Baremetal, ResourceRequirement, SolverConfig, Resources,
)
from app.splitter import ResourceSplitter


class PreferenceSplitter(ResourceSplitter):
    """
    Extended splitter with spec preference support.

    Adds a preference bonus term to the objective:
      -w_preference × Σ (preference_weight[spec] × count_var[spec])
    """

    def __init__(
        self,
        *,
        spec_preferences: dict[tuple[int, int, int, int], int] | None = None,
        w_preference: int = 3,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.spec_preferences = spec_preferences or {}
        self.w_preference = w_preference

    def build_preference_terms(self) -> list:
        """
        Returns CP-SAT expressions for spec preference.

        Each term: -w_preference × preference_weight × count_var
        (negative because we minimize; higher preference = more reward)

        Returns empty list if no preferences are set.
        """
        raise NotImplementedError("YOUR CODE HERE")
