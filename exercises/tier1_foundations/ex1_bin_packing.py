"""
═══════════════════════════════════════════════════════════════
  Exercise 1: 單機 Bin Packing
  Tier 1 — Foundations | 預計時間: 10 分鐘
═══════════════════════════════════════════════════════════════

【學習目標】
  1. 學會建立 BoolVar 決策變數
  2. 學會用 Σ demand × var ≤ capacity 建立多維度容量約束
  3. 學會用 Maximize 設定目標函數

【背景故事】
  你有一台 baremetal 伺服器，有固定的 CPU 和 Memory 容量。
  現在有 N 台 VM 要放上去，但不一定全部放得下。
  你需要決定「哪些 VM 要放進去」，使得放入的 VM 數量最大化。

  這是你 solver 中 _build_variables() + _add_capacity_constraints()
  的最小化版本 — 只有一台 BM，沒有 assignment matrix，只有 0/1 選取。

【你的任務】
  實作 bin_pack() 函式。

【函式簽名】
  def bin_pack(bm_cpu: int, bm_mem: int, vms: list[tuple[int, int]]) -> list[bool]

  參數:
    bm_cpu — BM 的 CPU 核心數
    bm_mem — BM 的記憶體 (MiB)
    vms    — 每台 VM 的 (cpu, mem) 需求

  回傳:
    list[bool] — 第 i 個元素為 True 表示第 i 台 VM 被選入

  範例:
    bin_pack(16, 64000, [(8, 32000), (8, 32000), (8, 32000)])
    → [True, True, False]  (只能放 2 台，第 3 台放不下)

【提示】（卡住時再展開）

  Hint 1 — 變數設計:
  > 為每台 VM 建一個 BoolVar：selected[i] = model.new_bool_var(f"vm_{i}")
  > selected[i] == 1 表示這台 VM 被選入

  Hint 2 — 容量約束:
  > CPU: sum(vms[i][0] * selected[i] for i in range(n)) <= bm_cpu
  > Mem: sum(vms[i][1] * selected[i] for i in range(n)) <= bm_mem

  Hint 3 — 目標函數:
  > model.maximize(sum(selected))  # 放入最多台 VM

  Hint 4 — 解提取:
  > solver.solve(model) 後用 solver.value(selected[i]) 讀取 0 或 1
  > 轉成 bool: [solver.value(selected[i]) == 1 for i in range(n)]

【預期效益】
  完成後你會理解:
  - CP-SAT 的基本 workflow: 建 model → 加變數 → 加約束 → 設目標 → solve → 讀解
  - BoolVar 如何用來表達「選或不選」的決策
  - 多維度容量約束的標準寫法 (你的 solver 中有 4 個維度: cpu/mem/disk/gpu)

【相關閱讀】
  - 本專案: app/solver.py _build_variables() (L211-226)
  - 本專案: app/solver.py _add_capacity_constraints() (L269-305)
  - OR-Tools: https://developers.google.com/optimization/cp/cp_solver
═══════════════════════════════════════════════════════════════
"""

from ortools.sat.python import cp_model


def bin_pack(bm_cpu: int, bm_mem: int, vms: list[tuple[int, int]]) -> list[bool]:
    """
    決定哪些 VM 可以放入一台 BM，使放入的 VM 數量最大化。

    Args:
        bm_cpu: BM 的 CPU 核心數
        bm_mem: BM 的記憶體 (MiB)
        vms: 每台 VM 的 (cpu_cores, memory_mib) 需求

    Returns:
        list[bool]: 第 i 個元素為 True 表示第 i 台 VM 被選入
    """
    if not vms:
        return []
    result = []
    model = cp_model.CpModel()

    # Variables
    assign_vars = [model.new_bool_var(f"assign_{vm_idx}") for vm_idx in range(len(vms))]

    # Constriants
    cpu_usage = sum(v * vms[i][0] for i, v in enumerate(assign_vars))
    model.add(cpu_usage <= bm_cpu)
    mem_usage = sum(v * vms[i][1] for i, v in enumerate(assign_vars))
    model.add(mem_usage <= bm_mem)

    # Objective
    model.maximize(sum(assign_vars))

    # Solve
    solver = cp_model.CpSolver()
    solver.solve(model)
    # Extract result
    result = [solver.value(v) == 1 for v in assign_vars]

    return result
