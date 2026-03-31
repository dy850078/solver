"""
═══════════════════════════════════════════════════════════════
  Exercise 2: 多機 VM Assignment
  Tier 1 — Foundations | 預計時間: 15 分鐘
═══════════════════════════════════════════════════════════════

【學習目標】
  1. 學會建立 assign[(vm, bm)] 二維 BoolVar 矩陣
  2. 學會用 Σ assign[vm, *] == 1 表達「每台 VM 恰好放到一台 BM」
  3. 學會為每台 BM 的每個資源維度加容量約束

【背景故事】
  上一題只有一台 BM，這題擴展到多台 BM。
  每台 VM 必須「恰好」放到一台 BM 上（不能不放，也不能放兩台）。
  每台 BM 的 CPU 和 Memory 不能被超用。
  如果放不下，回傳 None。

  這是你 solver 中 _build_variables() + _add_one_bm_per_vm_constraint()
  + _add_capacity_constraints() 的簡化版。

【你的任務】
  實作 assign_vms() 函式。

【函式簽名】
  def assign_vms(
      bms: list[dict],    # [{"id": str, "cpu": int, "mem": int}, ...]
      vms: list[dict],    # [{"id": str, "cpu": int, "mem": int}, ...]
  ) -> dict[str, str] | None

  回傳:
    成功: {vm_id: bm_id} assignment map
    失敗: None (INFEASIBLE)

  範例:
    assign_vms(
      [{"id": "bm-1", "cpu": 32, "mem": 128000}],
      [{"id": "vm-1", "cpu": 8, "mem": 32000}, {"id": "vm-2", "cpu": 8, "mem": 32000}]
    )
    → {"vm-1": "bm-1", "vm-2": "bm-1"}

【提示】（卡住時再展開）

  Hint 1 — 變數設計:
  > 建一個二維 BoolVar 字典:
  > assign = {}
  > for vm in vms:
  >     for bm in bms:
  >         assign[(vm["id"], bm["id"])] = model.new_bool_var(...)

  Hint 2 — One-BM-per-VM 約束:
  > for vm in vms:
  >     model.add(sum(assign[(vm["id"], bm["id"])] for bm in bms) == 1)

  Hint 3 — 容量約束:
  > for bm in bms:
  >     cpu_usage = sum(vm["cpu"] * assign[(vm["id"], bm["id"])] for vm in vms)
  >     model.add(cpu_usage <= bm["cpu"])
  >     # 同理 mem

  Hint 4 — 解提取:
  > result = {}
  > for vm in vms:
  >     for bm in bms:
  >         if solver.value(assign[(vm["id"], bm["id"])]) == 1:
  >             result[vm["id"]] = bm["id"]

【預期效益】
  完成後你會理解:
  - 二維 assign 矩陣是 VM placement 問題的標準建模方式
  - 「每台 VM 恰好放一台 BM」是怎麼用 sum == 1 表達的
  - 當 BM 不只一台時，容量約束要「對每台 BM 分別加」

【相關閱讀】
  - 本專案: app/solver.py _build_variables() (L211-226)
  - 本專案: app/solver.py _add_one_bm_per_vm_constraint() (L228-267)
  - 本專案: app/solver.py _add_capacity_constraints() (L269-305)
═══════════════════════════════════════════════════════════════
"""

from ortools.sat.python import cp_model


def assign_vms(
    bms: list[dict],
    vms: list[dict],
) -> dict[str, str] | None:
    """
    將每台 VM 分配到恰好一台 BM 上，不超過容量。

    Args:
        bms: [{"id": str, "cpu": int, "mem": int}, ...]
        vms: [{"id": str, "cpu": int, "mem": int}, ...]

    Returns:
        {vm_id: bm_id} 或 None (INFEASIBLE)
    """
    if not vms:
        return {}

    raise NotImplementedError("YOUR CODE HERE")
