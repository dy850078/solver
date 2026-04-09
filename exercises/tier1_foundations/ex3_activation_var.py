"""
═══════════════════════════════════════════════════════════════
  Exercise 3: Activation Variable Pattern
  Tier 1 — Foundations | 預計時間: 15 分鐘
═══════════════════════════════════════════════════════════════

【學習目標】
  1. 學會 pre-allocate slots + BoolVar activation 的建模模式
  2. 學會用 Σ active[k] == count 連結 activation 和 count
  3. 學會 symmetry breaking: active[k] >= active[k+1]
  4. 學會用 Minimize(waste) 設定最小浪費目標

【背景故事】
  一個工廠要生產「至少 target 個 CPU 核心」的伺服器。
  可選規格有 8 核和 12 核等。要決定各生產幾台，使總浪費最小。

  直覺做法是用 IntVar 表示 count — 但這題要求你用
  「pre-allocate slots + BoolVar activation」模式。
  這正是你 splitter 中 active_var 的核心模式。

  為什麼要這樣做？因為在真實 solver 中，每個 slot 需要對應一台
  synthetic VM，才能跟 placement solver 的 assign 變數連結。
  如果只有 count_var，你無法建立「第 k 台 VM 放到哪台 BM」的約束。

【你的任務】
  實作 optimal_split() 函式。
  關鍵要求: 必須用 new_bool_var 建 activation slots，不能只用 IntVar。

【函式簽名】
  def optimal_split(target: int, specs: list[int], max_per_spec: int) -> dict[int, int]

  參數:
    target       — 需要的最小 CPU 總核心數
    specs        — 可用的規格清單，例如 [8, 12]
    max_per_spec — 每種規格最多用幾台

  回傳:
    {spec: count} — 例如 {8: 2, 12: 1} 表示 2 台 8 核 + 1 台 12 核

  範例:
    optimal_split(30, [8, 12], 10)
    → {12: 2, 8: 1}  (24+8=32, waste=2, 這是最小浪費的方案之一)

【提示】（卡住時再展開）

  Hint 1 — Slot 設計:
  > 為每種 spec 建 max_per_spec 個 BoolVar slot:
  > active = {}
  > for spec in specs:
  >     for k in range(max_per_spec):
  >         active[(spec, k)] = model.new_bool_var(f"active_{spec}_{k}")

  Hint 2 — Count 與 Active 的連結:
  > count_var[spec] = model.new_int_var(0, max_per_spec, f"count_{spec}")
  > model.add(sum(active[(spec, k)] for k in range(max_per_spec)) == count_var[spec])

  Hint 3 — Symmetry Breaking:
  > for k in range(max_per_spec - 1):
  >     model.add(active[(spec, k)] >= active[(spec, k + 1)])
  > 這確保 active slots 從 index 0 開始連續啟用，避免等價解爆炸

  Hint 4 — Coverage + Waste:
  > total_produced = sum(spec * count_var[spec] for spec in specs)
  > model.add(total_produced >= target)
  > waste = total_produced - target  # 這是一個 LinearExpr
  > model.minimize(waste)

【預期效益】
  完成後你會理解:
  - 為什麼不能只用 count_var（因為 placement 需要知道「哪幾個 slot 是 active 的」）
  - activation variable 模式在 bin packing / VRP / facility location 中的通用性
  - symmetry breaking 如何大幅減少等價解，加速求解

【相關閱讀】
  - 本專案: app/splitter.py _build_requirement() (L105-170)
  - 本專案: app/splitter.py — module docstring 的 constraint structure 說明
  - 本專案: docs/reading-guide-splitter.md §3「Activation Variable Pattern」
  - OR-Tools: https://developers.google.com/optimization/bin/bin_packing
═══════════════════════════════════════════════════════════════
"""

from ortools.sat.python import cp_model


def optimal_split(target: int, specs: list[int], max_per_spec: int) -> dict[int, int]:
    """
    用 activation variable 模式決定各規格要幾台，使浪費最小。

    Args:
        target: 需要的最小 CPU 總核心數
        specs: 可用的規格清單 (e.g. [8, 12])
        max_per_spec: 每種規格最多用幾台

    Returns:
        {spec: count} 映射
    """
    if target <= 0 or len(specs) == 0 or max_per_spec < target:
        return {spec: 0 for spec in specs}

    model = cp_model.CpModel()

    # Variables
    assign_var: dict[tuple[int, int], cp_model.IntVar] = {}
    for spec in specs:
        for idx in range(max_per_spec):
            assign_var[(spec, idx)] = model.new_bool_var(f"assign_{spec}_{idx}")

    # Constraints
    for spec in specs:
        var_list = [
            var for (spec_model, idx), var in assign_var.items() if spec_model == spec
        ]
        for idx in range(len(var_list) - 1):
            model.add(var_list[idx] >= var_list[idx + 1])

    model.add(sum(var * pair[0] for pair, var in assign_var.items()) >= target)

    # Objective
    model.minimize(sum(var * pair[0] for pair, var in assign_var.items()) - target)

    # Solve
    solver = cp_model.CpSolver()
    solver.solve(model)

    # Extract solution
    result: dict[int, int] = {}
    for spec in specs:
        result[spec] = sum(
            [
                solver.value(var)
                for (spec_model, idx), var in assign_var.items()
                if spec_model == spec
            ]
        )

    return result
