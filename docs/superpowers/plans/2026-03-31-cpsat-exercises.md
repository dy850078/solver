# CP-SAT Modeling Exercises — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create 7 progressive CP-SAT modeling exercises on a dedicated branch, with pytest auto-verification and reference solutions.

**Architecture:** All files live in `exercises/` on branch `zs/cpsat-exercises` (from `main`). Tier 1 exercises are fully isolated. Tier 2-3 exercises import and extend existing solver/splitter classes via inheritance. Each exercise has a skeleton file (with docstring, hints, `NotImplementedError`), a test file, and a reference solution.

**Tech Stack:** Python 3.13, ortools (cp_model), pytest, pydantic v2

---

### Task 0: Branch & Directory Setup

**Files:**
- Create: `exercises/__init__.py`
- Create: `exercises/tier1_foundations/__init__.py`
- Create: `exercises/tier2_solver_ext/__init__.py`
- Create: `exercises/tier3_splitter/__init__.py`
- Create: `exercises/solutions/__init__.py`
- Create: `exercises/conftest.py`
- Create: `exercises/README.md`

- [ ] **Step 1: Create branch from main**

```bash
git checkout main
git checkout -b zs/cpsat-exercises
```

- [ ] **Step 2: Create directory structure**

```bash
mkdir -p exercises/tier1_foundations exercises/tier2_solver_ext exercises/tier3_splitter exercises/solutions
```

- [ ] **Step 3: Create `__init__.py` files**

Create empty `__init__.py` in each directory:
- `exercises/__init__.py`
- `exercises/tier1_foundations/__init__.py`
- `exercises/tier2_solver_ext/__init__.py`
- `exercises/tier3_splitter/__init__.py`
- `exercises/solutions/__init__.py`

- [ ] **Step 4: Create `exercises/conftest.py`**

```python
"""
Shared test helpers for CP-SAT exercises.

These are independent copies of the project's test helpers,
so exercises can run without modifying existing test infrastructure.
"""

import sys
from pathlib import Path

# Ensure project root is on sys.path so `from app.xxx import ...` works
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from app.models import (
    Resources, Topology, Baremetal, VM, NodeRole,
    SolverConfig, PlacementRequest, AntiAffinityRule,
    ResourceRequirement, SplitPlacementRequest,
)
from app.solver import VMPlacementSolver
from app.splitter import ResourceSplitter
from app.split_solver import solve_split_placement


def make_bm(bm_id, cpu=64, mem=256_000, disk=2000, gpu=0,
            used_cpu=0, used_mem=0, used_disk=0,
            ag="ag-1", dc="dc-1", rack="rack-1"):
    return Baremetal(
        id=bm_id,
        total_capacity=Resources(cpu_cores=cpu, memory_mib=mem, storage_gb=disk, gpu_count=gpu),
        used_capacity=Resources(cpu_cores=used_cpu, memory_mib=used_mem, storage_gb=used_disk),
        topology=Topology(site="site-a", phase="p1", datacenter=dc, rack=rack, ag=ag),
    )


def make_vm(vm_id, cpu=4, mem=16_000, disk=100, gpu=0,
            role=NodeRole.WORKER, cluster="cluster-1",
            ip_type="routable", candidates=None):
    return VM(
        id=vm_id,
        demand=Resources(cpu_cores=cpu, memory_mib=mem, storage_gb=disk, gpu_count=gpu),
        node_role=role,
        ip_type=ip_type,
        cluster_id=cluster,
        candidate_baremetals=candidates or [],
    )


def solve(vms, bms, rules=None, **config_overrides):
    """Solve with default config, overridable."""
    cfg = dict(max_solve_time_seconds=10, auto_generate_anti_affinity=False)
    cfg.update(config_overrides)
    request = PlacementRequest(
        vms=vms, baremetals=bms,
        anti_affinity_rules=rules or [],
        config=SolverConfig(**cfg),
    )
    return VMPlacementSolver(request).solve()


def amap(result):
    """Shorthand: vm_id -> bm_id dict."""
    return result.to_assignment_map()


def make_req(cpu=0, mem=0, disk=0, gpu=0,
             role=NodeRole.WORKER, cluster="cluster-1", ip_type="routable",
             vm_specs=None, min_vms=None, max_vms=None):
    """Shorthand for building a ResourceRequirement."""
    return ResourceRequirement(
        total_resources=Resources(cpu_cores=cpu, memory_mib=mem, storage_gb=disk, gpu_count=gpu),
        node_role=role,
        cluster_id=cluster,
        ip_type=ip_type,
        vm_specs=vm_specs,
        min_total_vms=min_vms,
        max_total_vms=max_vms,
    )


def split_solve(requirements, bms, explicit_vms=None, rules=None, **config_overrides):
    """Shorthand for split-and-solve."""
    cfg = dict(max_solve_time_seconds=10, auto_generate_anti_affinity=False)
    cfg.update(config_overrides)
    request = SplitPlacementRequest(
        requirements=requirements if isinstance(requirements, list) else [requirements],
        vms=explicit_vms or [],
        baremetals=bms,
        anti_affinity_rules=rules or [],
        config=SolverConfig(**cfg),
    )
    return solve_split_placement(request)
```

- [ ] **Step 5: Create `exercises/README.md`**

```markdown
# CP-SAT Modeling Exercises

## 如何開始

```bash
# 確保在 zs/cpsat-exercises branch
git checkout zs/cpsat-exercises

# 啟動虛擬環境
source .venv/bin/activate

# 跑單題測試（紅燈 → 你寫 code → 綠燈）
pytest exercises/tier1_foundations/test_ex1.py -v

# 跑某個 tier 的所有測試
pytest exercises/tier1_foundations/ -v

# 跑全部練習測試
pytest exercises/ -v
```

## 練習順序

| # | 題目 | 路徑 | 預計時間 |
|---|------|------|---------|
| 1 | 單機 Bin Packing | `tier1_foundations/ex1_bin_packing.py` | 10 min |
| 2 | 多機 VM Assignment | `tier1_foundations/ex2_multi_assignment.py` | 15 min |
| 3 | Activation Variable | `tier1_foundations/ex3_activation_var.py` | 15 min |
| 4 | Rack Anti-Affinity | `tier2_solver_ext/ex4_rack_anti_affinity.py` | 20 min |
| 5 | GPU Affinity | `tier2_solver_ext/ex5_gpu_affinity.py` | 25 min |
| 6 | Spec Preference | `tier3_splitter/ex6_spec_preference.py` | 30 min |
| 7 | Global VM Limit | `tier3_splitter/ex7_global_vm_limit.py` | 40 min |

## 驗證方式

1. **pytest 自動驗證**：跑測試看紅燈→綠燈
2. **參考解對照**：`solutions/sol{N}_*.py` 對照你的建模思路差異

## 建議

- 按順序做，每題都建立在前面的基礎上
- 先自己想，卡住了再展開 skeleton 裡的 hints
- 完成後跟參考解比較，看建模方式有什麼不同
```

- [ ] **Step 6: Verify structure**

Run: `ls -R exercises/`

Expected: all directories and files exist.

- [ ] **Step 7: Commit**

```bash
git add exercises/
git commit -m "chore: scaffold exercises directory and shared helpers"
```

---

### Task 1: Ex1 — 單機 Bin Packing

**Files:**
- Create: `exercises/tier1_foundations/ex1_bin_packing.py`
- Create: `exercises/tier1_foundations/test_ex1.py`
- Create: `exercises/solutions/sol1_bin_packing.py`

- [ ] **Step 1: Create test file `exercises/tier1_foundations/test_ex1.py`**

```python
"""Tests for Exercise 1: Single-Machine Bin Packing."""

from exercises.tier1_foundations.ex1_bin_packing import bin_pack


class TestBinPackBasic:
    def test_all_fit(self):
        """All VMs fit → all True."""
        result = bin_pack(
            bm_cpu=64, bm_mem=256_000,
            vms=[(4, 16_000), (8, 32_000), (4, 16_000)],
        )
        assert result == [True, True, True]

    def test_none_fit(self):
        """BM too small for any VM."""
        result = bin_pack(
            bm_cpu=2, bm_mem=4_000,
            vms=[(4, 16_000), (8, 32_000)],
        )
        assert result == [False, False]

    def test_empty_vms(self):
        """No VMs → empty list."""
        result = bin_pack(bm_cpu=64, bm_mem=256_000, vms=[])
        assert result == []


class TestBinPackCapacity:
    def test_cpu_bottleneck(self):
        """Memory fits all, but CPU doesn't → must choose subset."""
        # 64 CPU, each VM needs 32 → max 2 VMs
        result = bin_pack(
            bm_cpu=64, bm_mem=256_000,
            vms=[(32, 16_000), (32, 16_000), (32, 16_000)],
        )
        assert sum(result) == 2
        assert len(result) == 3

    def test_mem_bottleneck(self):
        """CPU fits all, but memory doesn't → must choose subset."""
        # 256k mem, each VM needs 128k → max 2
        result = bin_pack(
            bm_cpu=64, bm_mem=256_000,
            vms=[(4, 128_000), (4, 128_000), (4, 128_000)],
        )
        assert sum(result) == 2
        assert len(result) == 3

    def test_maximize_count(self):
        """Packs the maximum number of VMs, not just any subset."""
        # 64 CPU: one big (48) + one small (8) = 56, OR three small (24)
        # Maximize count → prefer 3 small
        result = bin_pack(
            bm_cpu=64, bm_mem=256_000,
            vms=[(48, 16_000), (8, 16_000), (8, 16_000), (8, 16_000)],
        )
        # Must fit at least 3 (the three 8-CPU VMs)
        assert sum(result) >= 3

    def test_exact_fit(self):
        """VMs exactly fill the BM."""
        result = bin_pack(
            bm_cpu=16, bm_mem=64_000,
            vms=[(8, 32_000), (8, 32_000)],
        )
        assert result == [True, True]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest exercises/tier1_foundations/test_ex1.py -v`

Expected: ImportError or NotImplementedError.

- [ ] **Step 3: Create skeleton `exercises/tier1_foundations/ex1_bin_packing.py`**

```python
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

    raise NotImplementedError("YOUR CODE HERE")
```

- [ ] **Step 4: Create solution `exercises/solutions/sol1_bin_packing.py`**

```python
"""Reference solution for Exercise 1: Single-Machine Bin Packing."""

from ortools.sat.python import cp_model


def bin_pack(bm_cpu: int, bm_mem: int, vms: list[tuple[int, int]]) -> list[bool]:
    if not vms:
        return []

    model = cp_model.CpModel()
    n = len(vms)

    # 決策變數: 每台 VM 選或不選
    selected = [model.new_bool_var(f"vm_{i}") for i in range(n)]

    # 容量約束: CPU
    model.add(sum(vms[i][0] * selected[i] for i in range(n)) <= bm_cpu)
    # 容量約束: Memory
    model.add(sum(vms[i][1] * selected[i] for i in range(n)) <= bm_mem)

    # 目標: 放入最多台 VM
    model.maximize(sum(selected))

    # 求解
    solver = cp_model.CpSolver()
    status = solver.solve(model)

    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return [solver.value(selected[i]) == 1 for i in range(n)]
    else:
        return [False] * n
```

- [ ] **Step 5: Verify solution passes all tests**

Run: `pytest exercises/tier1_foundations/test_ex1.py -v`

Temporarily swap import in test to point to solution, verify PASS, then revert.

- [ ] **Step 6: Commit**

```bash
git add exercises/tier1_foundations/ex1_bin_packing.py exercises/tier1_foundations/test_ex1.py exercises/solutions/sol1_bin_packing.py
git commit -m "feat: add exercise 1 — single-machine bin packing"
```

---

### Task 2: Ex2 — 多機 VM Assignment

**Files:**
- Create: `exercises/tier1_foundations/ex2_multi_assignment.py`
- Create: `exercises/tier1_foundations/test_ex2.py`
- Create: `exercises/solutions/sol2_multi_assignment.py`

- [ ] **Step 1: Create test file `exercises/tier1_foundations/test_ex2.py`**

```python
"""Tests for Exercise 2: Multi-Machine VM Assignment."""

from exercises.tier1_foundations.ex2_multi_assignment import assign_vms


class TestAssignBasic:
    def test_one_vm_one_bm(self):
        """Simplest case: 1 VM fits on 1 BM."""
        bms = [{"id": "bm-1", "cpu": 64, "mem": 256_000}]
        vms = [{"id": "vm-1", "cpu": 4, "mem": 16_000}]
        result = assign_vms(bms, vms)
        assert result == {"vm-1": "bm-1"}

    def test_three_vms_three_bms(self):
        """3 VMs, 3 BMs, each BM can hold exactly 1 VM."""
        bms = [
            {"id": "bm-1", "cpu": 8, "mem": 32_000},
            {"id": "bm-2", "cpu": 8, "mem": 32_000},
            {"id": "bm-3", "cpu": 8, "mem": 32_000},
        ]
        vms = [
            {"id": "vm-1", "cpu": 8, "mem": 32_000},
            {"id": "vm-2", "cpu": 8, "mem": 32_000},
            {"id": "vm-3", "cpu": 8, "mem": 32_000},
        ]
        result = assign_vms(bms, vms)
        assert result is not None
        assert set(result.keys()) == {"vm-1", "vm-2", "vm-3"}
        assert set(result.values()) == {"bm-1", "bm-2", "bm-3"}

    def test_empty_vms(self):
        """No VMs → empty map."""
        bms = [{"id": "bm-1", "cpu": 64, "mem": 256_000}]
        result = assign_vms(bms, [])
        assert result == {}


class TestAssignCapacity:
    def test_two_big_vms_must_split(self):
        """2 VMs too big for one BM → must go to different BMs."""
        bms = [
            {"id": "bm-1", "cpu": 32, "mem": 128_000},
            {"id": "bm-2", "cpu": 32, "mem": 128_000},
        ]
        vms = [
            {"id": "vm-1", "cpu": 24, "mem": 96_000},
            {"id": "vm-2", "cpu": 24, "mem": 96_000},
        ]
        result = assign_vms(bms, vms)
        assert result is not None
        assert result["vm-1"] != result["vm-2"]

    def test_uneven_bms(self):
        """Big VM must go to big BM."""
        bms = [
            {"id": "bm-small", "cpu": 8, "mem": 32_000},
            {"id": "bm-big", "cpu": 64, "mem": 256_000},
        ]
        vms = [
            {"id": "vm-big", "cpu": 32, "mem": 128_000},
            {"id": "vm-small", "cpu": 4, "mem": 16_000},
        ]
        result = assign_vms(bms, vms)
        assert result is not None
        assert result["vm-big"] == "bm-big"

    def test_insufficient_capacity(self):
        """Total capacity not enough → None."""
        bms = [{"id": "bm-1", "cpu": 8, "mem": 32_000}]
        vms = [
            {"id": "vm-1", "cpu": 8, "mem": 32_000},
            {"id": "vm-2", "cpu": 8, "mem": 32_000},
        ]
        result = assign_vms(bms, vms)
        assert result is None

    def test_packing_multiple_on_one_bm(self):
        """Multiple small VMs fit on one BM."""
        bms = [
            {"id": "bm-1", "cpu": 64, "mem": 256_000},
            {"id": "bm-2", "cpu": 64, "mem": 256_000},
        ]
        vms = [
            {"id": f"vm-{i}", "cpu": 4, "mem": 16_000}
            for i in range(4)
        ]
        result = assign_vms(bms, vms)
        assert result is not None
        assert len(result) == 4
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest exercises/tier1_foundations/test_ex2.py -v`

Expected: ImportError or NotImplementedError.

- [ ] **Step 3: Create skeleton `exercises/tier1_foundations/ex2_multi_assignment.py`**

```python
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
```

- [ ] **Step 4: Create solution `exercises/solutions/sol2_multi_assignment.py`**

```python
"""Reference solution for Exercise 2: Multi-Machine VM Assignment."""

from ortools.sat.python import cp_model


def assign_vms(
    bms: list[dict],
    vms: list[dict],
) -> dict[str, str] | None:
    if not vms:
        return {}

    model = cp_model.CpModel()

    # 決策變數: assign[(vm_id, bm_id)] = BoolVar
    assign = {}
    for vm in vms:
        for bm in bms:
            assign[(vm["id"], bm["id"])] = model.new_bool_var(
                f"assign_{vm['id']}_{bm['id']}"
            )

    # 約束 1: 每台 VM 恰好放到一台 BM
    for vm in vms:
        model.add(
            sum(assign[(vm["id"], bm["id"])] for bm in bms) == 1
        )

    # 約束 2: 每台 BM 的容量不能被超過
    for bm in bms:
        # CPU
        model.add(
            sum(vm["cpu"] * assign[(vm["id"], bm["id"])] for vm in vms)
            <= bm["cpu"]
        )
        # Memory
        model.add(
            sum(vm["mem"] * assign[(vm["id"], bm["id"])] for vm in vms)
            <= bm["mem"]
        )

    # 求解
    solver = cp_model.CpSolver()
    status = solver.solve(model)

    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        result = {}
        for vm in vms:
            for bm in bms:
                if solver.value(assign[(vm["id"], bm["id"])]) == 1:
                    result[vm["id"]] = bm["id"]
                    break
        return result
    else:
        return None
```

- [ ] **Step 5: Commit**

```bash
git add exercises/tier1_foundations/ex2_multi_assignment.py exercises/tier1_foundations/test_ex2.py exercises/solutions/sol2_multi_assignment.py
git commit -m "feat: add exercise 2 — multi-machine VM assignment"
```

---

### Task 3: Ex3 — Activation Variable Pattern

**Files:**
- Create: `exercises/tier1_foundations/ex3_activation_var.py`
- Create: `exercises/tier1_foundations/test_ex3.py`
- Create: `exercises/solutions/sol3_activation_var.py`

- [ ] **Step 1: Create test file `exercises/tier1_foundations/test_ex3.py`**

```python
"""Tests for Exercise 3: Activation Variable Pattern."""

import re
from exercises.tier1_foundations.ex3_activation_var import optimal_split


class TestBasicSplit:
    def test_exact_division(self):
        """16 / 8 = 2, zero waste."""
        result = optimal_split(target=16, specs=[8], max_per_spec=10)
        assert result == {8: 2}

    def test_non_exact(self):
        """30 / 8 = 3.75 → need 4 (waste=2) or use 12s."""
        result = optimal_split(target=30, specs=[8, 12], max_per_spec=10)
        total = sum(spec * count for spec, count in result.items())
        assert total >= 30
        # Optimal: 12×2 + 8×1 = 32 (waste=2) or 8×4 = 32 (waste=2)
        waste = total - 30
        assert waste <= 2

    def test_zero_target(self):
        """Target 0 → all counts zero (or empty dict)."""
        result = optimal_split(target=0, specs=[8, 12], max_per_spec=10)
        total = sum(spec * count for spec, count in result.items())
        assert total == 0

    def test_single_spec_large(self):
        """100 / 12 = 8.33 → 9 × 12 = 108 (waste=8)."""
        result = optimal_split(target=100, specs=[12], max_per_spec=20)
        assert result[12] == 9
        assert 12 * 9 == 108  # waste = 8


class TestWasteMinimization:
    def test_prefers_less_waste(self):
        """Between two valid splits, picks the one with less waste."""
        # target=24: spec 8 → 3×8=24 (waste=0), spec 12 → 2×12=24 (waste=0)
        # Both are zero-waste, either is fine
        result = optimal_split(target=24, specs=[8, 12], max_per_spec=10)
        total = sum(spec * count for spec, count in result.items())
        assert total == 24  # zero waste

    def test_mixed_specs_less_waste(self):
        """40: 8×5=40 (waste=0), or 12×3+8×0=36 (not enough) → 12×3+8×1=44 (waste=4).
        Should prefer 8×5."""
        result = optimal_split(target=40, specs=[8, 12], max_per_spec=10)
        total = sum(spec * count for spec, count in result.items())
        waste = total - 40
        assert waste == 0  # 8×5 is achievable


class TestActivationPattern:
    def test_uses_bool_vars(self):
        """Verify the implementation uses BoolVar (activation pattern)."""
        import inspect
        from exercises.tier1_foundations.ex3_activation_var import optimal_split
        source = inspect.getsource(optimal_split)
        assert "new_bool_var" in source, (
            "Must use new_bool_var (activation variable pattern), not just IntVar for counts"
        )

    def test_symmetry_breaking(self):
        """Verify symmetry breaking is implemented (active[k] >= active[k+1])."""
        import inspect
        from exercises.tier1_foundations.ex3_activation_var import optimal_split
        source = inspect.getsource(optimal_split)
        # Check for the >= pattern between consecutive active vars
        assert ">=" in source or "active" in source.lower(), (
            "Should implement symmetry breaking: active[k] >= active[k+1]"
        )


class TestMaxPerSpec:
    def test_respects_max(self):
        """max_per_spec limits the upper bound of slots."""
        # target=100, spec=8 → needs 13, but max_per_spec=5
        # With only spec 8 and max 5: 5×8=40 < 100, can't satisfy
        # With specs [8, 12] and max 5: 12×5+8×5=100, exact!
        result = optimal_split(target=100, specs=[8, 12], max_per_spec=5)
        total = sum(spec * count for spec, count in result.items())
        assert total >= 100
        for count in result.values():
            assert count <= 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest exercises/tier1_foundations/test_ex3.py -v`

Expected: ImportError or NotImplementedError.

- [ ] **Step 3: Create skeleton `exercises/tier1_foundations/ex3_activation_var.py`**

```python
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
    if target <= 0:
        return {spec: 0 for spec in specs}

    raise NotImplementedError("YOUR CODE HERE")
```

- [ ] **Step 4: Create solution `exercises/solutions/sol3_activation_var.py`**

```python
"""Reference solution for Exercise 3: Activation Variable Pattern."""

from ortools.sat.python import cp_model


def optimal_split(target: int, specs: list[int], max_per_spec: int) -> dict[int, int]:
    if target <= 0:
        return {spec: 0 for spec in specs}

    model = cp_model.CpModel()

    # Pre-allocate activation slots
    active = {}
    count_var = {}
    for spec in specs:
        for k in range(max_per_spec):
            active[(spec, k)] = model.new_bool_var(f"active_{spec}_{k}")

        # Count variable linked to active slots
        count_var[spec] = model.new_int_var(0, max_per_spec, f"count_{spec}")
        model.add(
            sum(active[(spec, k)] for k in range(max_per_spec)) == count_var[spec]
        )

        # Symmetry breaking: active[k] >= active[k+1]
        for k in range(max_per_spec - 1):
            model.add(active[(spec, k)] >= active[(spec, k + 1)])

    # Coverage constraint
    total_produced = sum(spec * count_var[spec] for spec in specs)
    model.add(total_produced >= target)

    # Objective: minimize waste
    model.minimize(total_produced - target)

    # Solve
    solver = cp_model.CpSolver()
    status = solver.solve(model)

    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {spec: solver.value(count_var[spec]) for spec in specs}
    else:
        return {spec: 0 for spec in specs}
```

- [ ] **Step 5: Commit**

```bash
git add exercises/tier1_foundations/ex3_activation_var.py exercises/tier1_foundations/test_ex3.py exercises/solutions/sol3_activation_var.py
git commit -m "feat: add exercise 3 — activation variable pattern"
```

---

### Task 4: Ex4 — Rack-Level Anti-Affinity

**Files:**
- Create: `exercises/tier2_solver_ext/ex4_rack_anti_affinity.py`
- Create: `exercises/tier2_solver_ext/test_ex4.py`
- Create: `exercises/solutions/sol4_rack_anti_affinity.py`

- [ ] **Step 1: Create test file `exercises/tier2_solver_ext/test_ex4.py`**

```python
"""Tests for Exercise 4: Rack-Level Anti-Affinity."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from exercises.conftest import make_bm, make_vm, amap
from exercises.tier2_solver_ext.ex4_rack_anti_affinity import (
    RackAntiAffinityRule,
    solve_with_rack_anti_affinity,
)
from app.models import NodeRole, AntiAffinityRule


class TestRackAntiAffinity:
    def test_same_rack_different_ag(self):
        """3 BMs in same rack, different AGs → rack rule max_per_rack=1 spreads VMs."""
        bms = [
            make_bm("bm-1", ag="ag-1", rack="rack-1"),
            make_bm("bm-2", ag="ag-2", rack="rack-1"),
            make_bm("bm-3", ag="ag-3", rack="rack-2"),
        ]
        vms = [
            make_vm("vm-1", role=NodeRole.MASTER),
            make_vm("vm-2", role=NodeRole.MASTER),
            make_vm("vm-3", role=NodeRole.MASTER),
        ]
        rack_rules = [
            RackAntiAffinityRule(
                group_id="masters",
                vm_ids=["vm-1", "vm-2", "vm-3"],
                max_per_rack=1,
            )
        ]
        r = solve_with_rack_anti_affinity(vms, bms, rack_rules=rack_rules)
        assert r.success
        m = amap(r)
        # rack-1 has bm-1 and bm-2; rack-2 has bm-3
        # max_per_rack=1 → at most 1 VM on rack-1 BMs, 1 on rack-2 BMs
        rack1_vms = [v for v, b in m.items() if b in ("bm-1", "bm-2")]
        rack2_vms = [v for v, b in m.items() if b == "bm-3"]
        assert len(rack1_vms) <= 1
        assert len(rack2_vms) <= 1

    def test_two_racks_max_per_rack_2(self):
        """6 BMs / 2 racks / 3 AGs → max_per_rack=2 distributes VMs."""
        bms = [
            make_bm("bm-1", ag="ag-1", rack="rack-1"),
            make_bm("bm-2", ag="ag-2", rack="rack-1"),
            make_bm("bm-3", ag="ag-3", rack="rack-1"),
            make_bm("bm-4", ag="ag-1", rack="rack-2"),
            make_bm("bm-5", ag="ag-2", rack="rack-2"),
            make_bm("bm-6", ag="ag-3", rack="rack-2"),
        ]
        vms = [make_vm(f"vm-{i}") for i in range(4)]
        rack_rules = [
            RackAntiAffinityRule(
                group_id="workers",
                vm_ids=[f"vm-{i}" for i in range(4)],
                max_per_rack=2,
            )
        ]
        r = solve_with_rack_anti_affinity(vms, bms, rack_rules=rack_rules)
        assert r.success
        m = amap(r)
        rack1_bms = {"bm-1", "bm-2", "bm-3"}
        rack1_count = sum(1 for b in m.values() if b in rack1_bms)
        rack2_count = sum(1 for b in m.values() if b not in rack1_bms)
        assert rack1_count <= 2
        assert rack2_count <= 2

    def test_no_rack_rule_same_as_original(self):
        """Without rack rules, behaves like the original solver."""
        bms = [make_bm("bm-1"), make_bm("bm-2")]
        vms = [make_vm("vm-1"), make_vm("vm-2")]
        r = solve_with_rack_anti_affinity(vms, bms, rack_rules=[])
        assert r.success
        assert len(amap(r)) == 2

    def test_rack_and_ag_rules_together(self):
        """Rack rules and AG rules both apply simultaneously."""
        bms = [
            make_bm("bm-1", ag="ag-1", rack="rack-1"),
            make_bm("bm-2", ag="ag-2", rack="rack-1"),
            make_bm("bm-3", ag="ag-1", rack="rack-2"),
            make_bm("bm-4", ag="ag-2", rack="rack-2"),
        ]
        vms = [make_vm(f"vm-{i}", role=NodeRole.MASTER) for i in range(4)]
        vm_ids = [f"vm-{i}" for i in range(4)]
        ag_rules = [AntiAffinityRule(group_id="ag-spread", vm_ids=vm_ids, max_per_ag=2)]
        rack_rules = [RackAntiAffinityRule(group_id="rack-spread", vm_ids=vm_ids, max_per_rack=2)]
        r = solve_with_rack_anti_affinity(
            vms, bms, ag_rules=ag_rules, rack_rules=rack_rules,
        )
        assert r.success
        m = amap(r)
        # Check AG constraint
        ag1_count = sum(1 for b in m.values() if b in ("bm-1", "bm-3"))
        ag2_count = sum(1 for b in m.values() if b in ("bm-2", "bm-4"))
        assert ag1_count <= 2
        assert ag2_count <= 2
        # Check rack constraint
        rack1_count = sum(1 for b in m.values() if b in ("bm-1", "bm-2"))
        rack2_count = sum(1 for b in m.values() if b in ("bm-3", "bm-4"))
        assert rack1_count <= 2
        assert rack2_count <= 2

    def test_infeasible_rack_constraint(self):
        """Not enough racks → infeasible."""
        # 2 BMs in same rack, max_per_rack=1, 2 VMs → can only place 1
        bms = [
            make_bm("bm-1", ag="ag-1", rack="rack-1"),
            make_bm("bm-2", ag="ag-2", rack="rack-1"),
        ]
        vms = [make_vm("vm-1"), make_vm("vm-2")]
        rack_rules = [
            RackAntiAffinityRule(
                group_id="test",
                vm_ids=["vm-1", "vm-2"],
                max_per_rack=1,
            )
        ]
        r = solve_with_rack_anti_affinity(vms, bms, rack_rules=rack_rules)
        assert not r.success
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest exercises/tier2_solver_ext/test_ex4.py -v`

Expected: ImportError or NotImplementedError.

- [ ] **Step 3: Create skeleton `exercises/tier2_solver_ext/ex4_rack_anti_affinity.py`**

```python
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
```

- [ ] **Step 4: Create solution `exercises/solutions/sol4_rack_anti_affinity.py`**

```python
"""Reference solution for Exercise 4: Rack-Level Anti-Affinity."""

from __future__ import annotations
from collections import defaultdict

from pydantic import BaseModel

from app.models import (
    PlacementRequest, PlacementResult, SolverConfig,
    AntiAffinityRule,
)
from app.solver import VMPlacementSolver


class RackAntiAffinityRule(BaseModel):
    group_id: str
    vm_ids: list[str]
    max_per_rack: int = 2


class RackAwareSolver(VMPlacementSolver):

    def __init__(self, request, *, rack_rules=None, **kwargs):
        super().__init__(request, **kwargs)
        self.rack_rules = rack_rules or []

    def _add_anti_affinity_constraints(self):
        # AG-level (inherited)
        super()._add_anti_affinity_constraints()

        # Rack-level: group BMs by rack
        rack_to_bms: dict[str, list[str]] = defaultdict(list)
        for bm in self.request.baremetals:
            rack_to_bms[bm.topology.rack].append(bm.id)

        for rule in self.rack_rules:
            for rack, rack_bm_ids in rack_to_bms.items():
                vars_in_rack = [
                    self.assign[(vm_id, bm_id)]
                    for vm_id in rule.vm_ids
                    for bm_id in rack_bm_ids
                    if (vm_id, bm_id) in self.assign
                ]
                if vars_in_rack:
                    self.model.add(sum(vars_in_rack) <= rule.max_per_rack)


def solve_with_rack_anti_affinity(
    vms, bms, rack_rules=None, ag_rules=None, **config_overrides,
) -> PlacementResult:
    cfg = dict(max_solve_time_seconds=10, auto_generate_anti_affinity=False)
    cfg.update(config_overrides)
    request = PlacementRequest(
        vms=vms, baremetals=bms,
        anti_affinity_rules=ag_rules or [],
        config=SolverConfig(**cfg),
    )
    solver = RackAwareSolver(request, rack_rules=rack_rules or [])
    return solver.solve()
```

- [ ] **Step 5: Commit**

```bash
git add exercises/tier2_solver_ext/ex4_rack_anti_affinity.py exercises/tier2_solver_ext/test_ex4.py exercises/solutions/sol4_rack_anti_affinity.py
git commit -m "feat: add exercise 4 — rack-level anti-affinity"
```

---

### Task 5: Ex5 — GPU Affinity Constraint

**Files:**
- Create: `exercises/tier2_solver_ext/ex5_gpu_affinity.py`
- Create: `exercises/tier2_solver_ext/test_ex5.py`
- Create: `exercises/solutions/sol5_gpu_affinity.py`

- [ ] **Step 1: Create test file `exercises/tier2_solver_ext/test_ex5.py`**

```python
"""Tests for Exercise 5: GPU Affinity Constraint."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from exercises.conftest import make_bm, make_vm, amap
from exercises.tier2_solver_ext.ex5_gpu_affinity import solve_with_gpu_affinity
from app.models import NodeRole


class TestGpuAffinity:
    def test_ratio_half(self):
        """GPU BM must have at least half GPU VMs."""
        bms = [
            make_bm("bm-gpu", cpu=64, mem=256_000, gpu=4),
            make_bm("bm-no-gpu", cpu=64, mem=256_000, gpu=0),
        ]
        vms = [
            make_vm("vm-gpu-1", cpu=8, mem=32_000, gpu=1),
            make_vm("vm-gpu-2", cpu=8, mem=32_000, gpu=1),
            make_vm("vm-nogpu-1", cpu=8, mem=32_000, gpu=0),
            make_vm("vm-nogpu-2", cpu=8, mem=32_000, gpu=0),
        ]
        r = solve_with_gpu_affinity(vms, bms, min_gpu_numerator=1, min_gpu_denominator=2)
        assert r.success
        m = amap(r)
        # Count GPU VMs on the GPU BM
        on_gpu_bm = [v for v, b in m.items() if b == "bm-gpu"]
        gpu_vms_on_gpu_bm = [v for v in on_gpu_bm if "gpu" in v and "nogpu" not in v]
        if on_gpu_bm:
            # gpu_count * 2 >= total_count * 1
            assert len(gpu_vms_on_gpu_bm) * 2 >= len(on_gpu_bm) * 1

    def test_ratio_full(self):
        """ratio=1/1 → GPU BM only accepts GPU VMs."""
        bms = [
            make_bm("bm-gpu", cpu=64, mem=256_000, gpu=4),
            make_bm("bm-no-gpu", cpu=64, mem=256_000, gpu=0),
        ]
        vms = [
            make_vm("vm-gpu-1", cpu=8, mem=32_000, gpu=1),
            make_vm("vm-nogpu-1", cpu=8, mem=32_000, gpu=0),
        ]
        r = solve_with_gpu_affinity(vms, bms, min_gpu_numerator=1, min_gpu_denominator=1)
        assert r.success
        m = amap(r)
        # non-GPU VM must NOT be on GPU BM
        assert m["vm-nogpu-1"] == "bm-no-gpu"

    def test_no_gpu_bm(self):
        """No GPU BMs → constraint is a no-op, behaves like original solver."""
        bms = [make_bm("bm-1"), make_bm("bm-2")]
        vms = [make_vm("vm-1"), make_vm("vm-2")]
        r = solve_with_gpu_affinity(vms, bms, min_gpu_numerator=1, min_gpu_denominator=2)
        assert r.success
        assert len(amap(r)) == 2

    def test_empty_gpu_bm_no_constraint(self):
        """GPU BM with no VMs placed → constraint doesn't trigger."""
        bms = [
            make_bm("bm-gpu", cpu=8, mem=32_000, gpu=4),
            make_bm("bm-big", cpu=128, mem=512_000, gpu=0),
        ]
        # Only non-GPU VMs: they should all go to bm-big
        vms = [make_vm(f"vm-{i}", gpu=0) for i in range(3)]
        r = solve_with_gpu_affinity(vms, bms, min_gpu_numerator=1, min_gpu_denominator=1)
        assert r.success
        m = amap(r)
        # All VMs should be on bm-big (non-GPU BM) since ratio=1/1
        # means GPU BM can ONLY have GPU VMs
        for v, b in m.items():
            assert b == "bm-big"

    def test_gpu_vm_must_go_to_gpu_bm(self):
        """GPU VM needs GPU capacity → must be on GPU BM."""
        bms = [
            make_bm("bm-gpu", cpu=64, mem=256_000, gpu=4),
            make_bm("bm-no-gpu", cpu=64, mem=256_000, gpu=0),
        ]
        vms = [make_vm("vm-gpu", cpu=8, mem=32_000, gpu=2)]
        r = solve_with_gpu_affinity(vms, bms, min_gpu_numerator=1, min_gpu_denominator=2)
        assert r.success
        assert amap(r)["vm-gpu"] == "bm-gpu"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest exercises/tier2_solver_ext/test_ex5.py -v`

Expected: ImportError or NotImplementedError.

- [ ] **Step 3: Create skeleton `exercises/tier2_solver_ext/ex5_gpu_affinity.py`**

```python
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

from app.models import PlacementRequest, PlacementResult, SolverConfig
from app.solver import VMPlacementSolver


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
```

- [ ] **Step 4: Create solution `exercises/solutions/sol5_gpu_affinity.py`**

```python
"""Reference solution for Exercise 5: GPU Affinity Constraint."""

from __future__ import annotations
import time
import logging

from ortools.sat.python import cp_model

from app.models import PlacementRequest, PlacementResult, SolverConfig
from app.solver import VMPlacementSolver

logger = logging.getLogger(__name__)


class GpuAffinitySolver(VMPlacementSolver):

    def __init__(self, request, *, min_gpu_numerator=1, min_gpu_denominator=2, **kwargs):
        super().__init__(request, **kwargs)
        self.min_gpu_numerator = min_gpu_numerator
        self.min_gpu_denominator = min_gpu_denominator

    def _add_gpu_affinity_constraints(self):
        gpu_vm_ids = {vm.id for vm in self.request.vms if vm.demand.gpu_count > 0}

        self._ensure_bm_used_vars()

        for bm in self.request.baremetals:
            if bm.total_capacity.gpu_count <= 0:
                continue

            assigned = [
                (vm_id, self.assign[(vm_id, bm.id)])
                for vm_id in self.vm_map
                if (vm_id, bm.id) in self.assign
            ]
            if not assigned:
                continue

            gpu_on_bm = sum(var for vm_id, var in assigned if vm_id in gpu_vm_ids)
            total_on_bm = sum(var for _, var in assigned)

            self.model.add(
                gpu_on_bm * self.min_gpu_denominator
                >= total_on_bm * self.min_gpu_numerator
            ).only_enforce_if(self.bm_used[bm.id])

    def solve(self) -> PlacementResult:
        start = time.time()

        if self._input_errors:
            for err in self._input_errors:
                logger.error("Input validation failed: %s", err)
            return PlacementResult(
                success=False,
                solver_status="INPUT_ERROR: duplicate baremetals detected",
                solve_time_seconds=time.time() - start,
                unplaced_vms=[vm.id for vm in self.request.vms],
                diagnostics={"input_errors": self._input_errors},
            )

        try:
            self._build_variables()
            self._add_one_bm_per_vm_constraint()
            self._add_capacity_constraints()
            self._add_anti_affinity_constraints()
            self._add_gpu_affinity_constraints()
            self._add_objective()

            solver = cp_model.CpSolver()
            solver.parameters.max_time_in_seconds = self.config.max_solve_time_seconds
            solver.parameters.num_workers = self.config.num_workers

            status = solver.solve(self.model)
            status_name = self._status_name(status)

            if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                self._last_cp_solver = solver
                return self._extract_solution(solver, status_name, time.time() - start)
            else:
                return PlacementResult(
                    success=False,
                    solver_status=status_name,
                    solve_time_seconds=time.time() - start,
                    unplaced_vms=[vm.id for vm in self.request.vms],
                )
        except Exception as e:
            return PlacementResult(
                success=False,
                solver_status=f"ERROR: {e}",
                solve_time_seconds=time.time() - start,
                unplaced_vms=[vm.id for vm in self.request.vms],
            )


def solve_with_gpu_affinity(
    vms, bms, min_gpu_numerator=1, min_gpu_denominator=2, **config_overrides,
) -> PlacementResult:
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
```

- [ ] **Step 5: Commit**

```bash
git add exercises/tier2_solver_ext/ex5_gpu_affinity.py exercises/tier2_solver_ext/test_ex5.py exercises/solutions/sol5_gpu_affinity.py
git commit -m "feat: add exercise 5 — GPU affinity constraint"
```

---

### Task 6: Ex6 — Spec Preference Soft Constraint

**Files:**
- Create: `exercises/tier3_splitter/ex6_spec_preference.py`
- Create: `exercises/tier3_splitter/test_ex6.py`
- Create: `exercises/solutions/sol6_spec_preference.py`

- [ ] **Step 1: Create test file `exercises/tier3_splitter/test_ex6.py`**

```python
"""Tests for Exercise 6: Spec Preference Soft Constraint."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from ortools.sat.python import cp_model

from exercises.conftest import make_bm, make_req, Resources
from exercises.tier3_splitter.ex6_spec_preference import (
    PreferenceSplitter,
)
from app.models import SolverConfig, NodeRole


def _solve_preference_split(
    requirements, bms, spec_preferences=None, w_resource_waste=5, w_preference=3,
):
    """Helper: build a model with preference splitter and solve it."""
    config = SolverConfig(
        vm_specs=[],
        w_resource_waste=w_resource_waste,
        max_solve_time_seconds=10,
    )
    model = cp_model.CpModel()
    splitter = PreferenceSplitter(
        model=model,
        requirements=requirements if isinstance(requirements, list) else [requirements],
        baremetals=bms,
        config=config,
        spec_preferences=spec_preferences or {},
        w_preference=w_preference,
    )
    synthetic_vms = splitter.build()
    waste_terms = splitter.build_waste_objective_terms()
    pref_terms = splitter.build_preference_terms()

    # Combine objective: waste penalty + preference bonus
    obj_terms = []
    if waste_terms:
        obj_terms.append(config.w_resource_waste * sum(waste_terms))
    if pref_terms:
        obj_terms.extend(pref_terms)
    if obj_terms:
        model.minimize(sum(obj_terms))

    solver = cp_model.CpSolver()
    status = solver.solve(model)

    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return splitter.get_split_decisions(solver)
    return []


class TestSpecPreference:
    def test_both_zero_waste_pick_preferred(self):
        """Two specs both achieve zero waste → pick the one with higher preference."""
        bms = [make_bm("bm-1", cpu=64, mem=256_000)]
        # Need 24 CPU: 8×3=24 (waste=0) or 12×2=24 (waste=0)
        spec_8 = Resources(cpu_cores=8, memory_mib=32_000)
        spec_12 = Resources(cpu_cores=12, memory_mib=48_000)
        req = make_req(
            cpu=24, mem=96_000,
            vm_specs=[spec_8, spec_12],
        )
        prefs = {
            (12, 48_000, 0, 0): 10,   # prefer 12-core
            (8, 32_000, 0, 0): 1,
        }
        decisions = _solve_preference_split([req], bms, spec_preferences=prefs)
        # Should prefer spec_12 (weight=10)
        assert any(d.vm_spec.cpu_cores == 12 and d.count == 2 for d in decisions)

    def test_waste_overrides_preference(self):
        """High-preference spec has more waste → waste weight wins."""
        bms = [make_bm("bm-1", cpu=64, mem=256_000)]
        # Need 40 CPU:
        #   8×5 = 40 (waste=0)
        #   12×4 = 48 (waste=8)
        spec_8 = Resources(cpu_cores=8, memory_mib=32_000)
        spec_12 = Resources(cpu_cores=12, memory_mib=48_000)
        req = make_req(cpu=40, mem=160_000, vm_specs=[spec_8, spec_12])
        prefs = {
            (12, 48_000, 0, 0): 5,   # prefer 12-core
            (8, 32_000, 0, 0): 1,
        }
        # With high waste weight, should pick 8×5 despite lower preference
        decisions = _solve_preference_split(
            [req], bms, spec_preferences=prefs, w_resource_waste=10, w_preference=1,
        )
        total_waste = sum(
            d.vm_spec.cpu_cores * d.count for d in decisions
        ) - 40
        assert total_waste == 0

    def test_no_preference_uses_waste_only(self):
        """Without preferences, behaves like original splitter."""
        bms = [make_bm("bm-1", cpu=64, mem=256_000)]
        spec_8 = Resources(cpu_cores=8, memory_mib=32_000)
        req = make_req(cpu=32, mem=128_000, vm_specs=[spec_8])
        decisions = _solve_preference_split([req], bms, spec_preferences={})
        assert any(d.vm_spec.cpu_cores == 8 and d.count == 4 for d in decisions)

    def test_preference_terms_returned(self):
        """build_preference_terms() returns non-empty list when preferences exist."""
        bms = [make_bm("bm-1", cpu=64, mem=256_000)]
        spec_8 = Resources(cpu_cores=8, memory_mib=32_000)
        req = make_req(cpu=32, mem=128_000, vm_specs=[spec_8])
        config = SolverConfig(vm_specs=[], max_solve_time_seconds=10)
        model = cp_model.CpModel()
        splitter = PreferenceSplitter(
            model=model,
            requirements=[req],
            baremetals=bms,
            config=config,
            spec_preferences={(8, 32_000, 0, 0): 5},
            w_preference=3,
        )
        splitter.build()
        terms = splitter.build_preference_terms()
        assert len(terms) > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest exercises/tier3_splitter/test_ex6.py -v`

Expected: ImportError or NotImplementedError.

- [ ] **Step 3: Create skeleton `exercises/tier3_splitter/ex6_spec_preference.py`**

```python
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
```

- [ ] **Step 4: Create solution `exercises/solutions/sol6_spec_preference.py`**

```python
"""Reference solution for Exercise 6: Spec Preference Soft Constraint."""

from __future__ import annotations

from ortools.sat.python import cp_model

from app.models import Baremetal, ResourceRequirement, SolverConfig, Resources
from app.splitter import ResourceSplitter


class PreferenceSplitter(ResourceSplitter):

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
        if not self.spec_preferences:
            return []

        terms = []
        for (req_idx, spec_idx), count_var in self.count_vars.items():
            specs = self._req_specs.get(req_idx, [])
            if spec_idx >= len(specs):
                continue
            spec = specs[spec_idx]
            key = (spec.cpu_cores, spec.memory_mib, spec.storage_gb, spec.gpu_count)
            weight = self.spec_preferences.get(key, 0)
            if weight > 0:
                # Negative: in minimize, this rewards using preferred specs
                terms.append(-self.w_preference * weight * count_var)
        return terms
```

- [ ] **Step 5: Commit**

```bash
git add exercises/tier3_splitter/ex6_spec_preference.py exercises/tier3_splitter/test_ex6.py exercises/solutions/sol6_spec_preference.py
git commit -m "feat: add exercise 6 — spec preference soft constraint"
```

---

### Task 7: Ex7 — 跨 Requirement 全域 VM 數量上限

**Files:**
- Create: `exercises/tier3_splitter/ex7_global_vm_limit.py`
- Create: `exercises/tier3_splitter/test_ex7.py`
- Create: `exercises/solutions/sol7_global_vm_limit.py`

- [ ] **Step 1: Create test file `exercises/tier3_splitter/test_ex7.py`**

```python
"""Tests for Exercise 7: Global VM Limit."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from exercises.conftest import make_bm, make_req, make_vm, Resources
from exercises.tier3_splitter.ex7_global_vm_limit import (
    solve_split_with_global_limit,
)
from app.models import NodeRole, AntiAffinityRule


class TestGlobalMaxVms:
    def test_limits_total_across_requirements(self):
        """2 reqs each needing 4 VMs, but global max=6 → total ≤ 6."""
        bms = [make_bm(f"bm-{i}", cpu=128, mem=512_000) for i in range(6)]
        spec = Resources(cpu_cores=8, memory_mib=32_000)
        req_w = make_req(cpu=32, mem=128_000, role=NodeRole.WORKER, vm_specs=[spec])
        req_m = make_req(cpu=32, mem=128_000, role=NodeRole.MASTER, vm_specs=[spec])
        r = solve_split_with_global_limit(
            requirements=[req_w, req_m], bms=bms, global_max_vms=6,
        )
        assert r.success
        total = sum(d.count for d in r.split_decisions)
        assert total <= 6

    def test_no_limit_places_all(self):
        """Without global limit, each req gets what it needs."""
        bms = [make_bm(f"bm-{i}", cpu=128, mem=512_000) for i in range(10)]
        spec = Resources(cpu_cores=8, memory_mib=32_000)
        req_w = make_req(cpu=32, mem=128_000, role=NodeRole.WORKER, vm_specs=[spec])
        req_m = make_req(cpu=32, mem=128_000, role=NodeRole.MASTER, vm_specs=[spec])
        r = solve_split_with_global_limit(
            requirements=[req_w, req_m], bms=bms, global_max_vms=None,
        )
        assert r.success
        total = sum(d.count for d in r.split_decisions)
        assert total == 8  # 4 workers + 4 masters


class TestGlobalMinVms:
    def test_min_vms_enforced(self):
        """Global min forces more VMs than coverage alone would need."""
        bms = [make_bm(f"bm-{i}", cpu=128, mem=512_000) for i in range(10)]
        spec = Resources(cpu_cores=8, memory_mib=32_000)
        # Coverage needs 2 VMs (16 CPU / 8 = 2), but global min = 5
        req = make_req(cpu=16, mem=64_000, vm_specs=[spec])
        r = solve_split_with_global_limit(
            requirements=[req], bms=bms, global_min_vms=5,
        )
        assert r.success
        total = sum(d.count for d in r.split_decisions)
        assert total >= 5

    def test_infeasible_min_exceeds_capacity(self):
        """Global min too high for BM capacity → INFEASIBLE."""
        bms = [make_bm("bm-1", cpu=32, mem=128_000)]
        spec = Resources(cpu_cores=8, memory_mib=32_000)
        req = make_req(cpu=8, mem=32_000, vm_specs=[spec])
        r = solve_split_with_global_limit(
            requirements=[req], bms=bms, global_min_vms=10,
        )
        assert not r.success


class TestGlobalLimitWithExplicitVms:
    def test_explicit_vms_count_toward_limit(self):
        """Explicit VMs + synthetic VMs together respect global max."""
        bms = [make_bm(f"bm-{i}", cpu=128, mem=512_000) for i in range(6)]
        spec = Resources(cpu_cores=8, memory_mib=32_000)
        # 2 explicit VMs + requirement needs 4 → total 6
        # global max = 4 → synthetic must be ≤ 2
        explicit = [make_vm(f"explicit-{i}") for i in range(2)]
        req = make_req(cpu=32, mem=128_000, vm_specs=[spec])
        r = solve_split_with_global_limit(
            requirements=[req], bms=bms,
            explicit_vms=explicit,
            global_max_vms=4,
        )
        assert r.success
        synthetic_count = sum(d.count for d in r.split_decisions)
        total = synthetic_count + len(explicit)
        assert total <= 4


class TestGlobalLimitWithAntiAffinity:
    def test_anti_affinity_and_global_limit(self):
        """Anti-affinity + global limit both apply."""
        bms = [
            make_bm("bm-1", ag="ag-1", cpu=128, mem=512_000),
            make_bm("bm-2", ag="ag-2", cpu=128, mem=512_000),
            make_bm("bm-3", ag="ag-3", cpu=128, mem=512_000),
        ]
        spec = Resources(cpu_cores=8, memory_mib=32_000)
        req = make_req(cpu=24, mem=96_000, vm_specs=[spec])
        r = solve_split_with_global_limit(
            requirements=[req], bms=bms, global_max_vms=3,
            auto_generate_anti_affinity=True,
        )
        assert r.success
        total = sum(d.count for d in r.split_decisions)
        assert total <= 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest exercises/tier3_splitter/test_ex7.py -v`

Expected: ImportError or NotImplementedError.

- [ ] **Step 3: Create skeleton `exercises/tier3_splitter/ex7_global_vm_limit.py`**

```python
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
```

- [ ] **Step 4: Create solution `exercises/solutions/sol7_global_vm_limit.py`**

```python
"""Reference solution for Exercise 7: Global VM Limit."""

from __future__ import annotations
import time

from ortools.sat.python import cp_model

from app.models import (
    PlacementRequest, SplitPlacementResult,
    SolverConfig, ResourceRequirement, Baremetal, VM, AntiAffinityRule,
    Resources,
)
from app.splitter import ResourceSplitter
from app.solver import VMPlacementSolver


def solve_split_with_global_limit(
    requirements,
    bms,
    global_max_vms=None,
    global_min_vms=None,
    explicit_vms=None,
    rules=None,
    **config_overrides,
) -> SplitPlacementResult:
    start = time.time()
    explicit_vms = explicit_vms or []
    rules = rules or []

    cfg = dict(max_solve_time_seconds=10, auto_generate_anti_affinity=False)
    cfg.update(config_overrides)
    config = SolverConfig(**cfg)

    reqs = requirements if isinstance(requirements, list) else [requirements]

    # 1. Shared model
    model = cp_model.CpModel()

    # 2. Splitter builds split variables + coverage constraints
    splitter = ResourceSplitter(
        model=model,
        requirements=reqs,
        baremetals=bms,
        config=config,
    )
    synthetic_vms = splitter.build()

    if not synthetic_vms and not explicit_vms:
        return SplitPlacementResult(
            success=False,
            solver_status="NO_VMS",
            solve_time_seconds=time.time() - start,
        )

    # 3. Global VM count constraints (AFTER splitter.build(), BEFORE solver)
    all_counts = list(splitter.count_vars.values())
    n_explicit = len(explicit_vms)

    if global_max_vms is not None and all_counts:
        synthetic_budget = global_max_vms - n_explicit
        model.add(sum(all_counts) <= max(0, synthetic_budget))

    if global_min_vms is not None and all_counts:
        synthetic_min = global_min_vms - n_explicit
        model.add(sum(all_counts) >= max(0, synthetic_min))

    # 4. Combine explicit + synthetic VMs
    placement_request = PlacementRequest(
        vms=list(explicit_vms) + synthetic_vms,
        baremetals=bms,
        anti_affinity_rules=rules,
        config=config,
    )

    # 5. Solver on the same shared model
    solver_instance = VMPlacementSolver(
        placement_request,
        model=model,
        active_vars=splitter.active_vars,
    )
    solver_instance.splitter_waste_terms = splitter.build_waste_objective_terms()

    result = solver_instance.solve()

    # 6. Extract split decisions
    if result.success or result.solver_status in ("OPTIMAL", "FEASIBLE"):
        cp_solver = getattr(solver_instance, "_last_cp_solver", None)
        split_decisions = splitter.get_split_decisions(cp_solver) if cp_solver else []
    else:
        split_decisions = []

    return SplitPlacementResult(
        success=result.success,
        assignments=result.assignments,
        split_decisions=split_decisions,
        solver_status=result.solver_status,
        solve_time_seconds=time.time() - start,
        unplaced_vms=result.unplaced_vms,
        diagnostics=result.diagnostics,
    )
```

- [ ] **Step 5: Commit**

```bash
git add exercises/tier3_splitter/ex7_global_vm_limit.py exercises/tier3_splitter/test_ex7.py exercises/solutions/sol7_global_vm_limit.py
git commit -m "feat: add exercise 7 — global VM limit across requirements"
```

---

### Task 8: Verify All Solutions Pass

- [ ] **Step 1: Temporarily wire solutions into tests and run**

For each exercise, temporarily modify the test import to point to the solution file and verify all tests pass. Then revert.

Run: `pytest exercises/ -v --tb=short`

Verify: All solution tests pass. All skeleton tests fail with NotImplementedError.

- [ ] **Step 2: Final commit**

```bash
git add -A exercises/
git commit -m "chore: verify all exercise solutions pass tests"
```
