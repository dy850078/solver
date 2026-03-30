# 閱讀指南：如何從零理解 Requirement Splitter 並延伸開發

## 核心閱讀原則

這個 feature 的核心是「把兩個決策（split + placement）合進同一個 CP-SAT model」。
理解這個之前，先要知道「原本的 solver 長什麼樣子」，再看「splitter 加了什麼」，
最後看「兩者怎麼被串在一起」。

閱讀路徑分三層：**Why → What → How**

---

## 第一層：Why（動機與背景）—— 10 分鐘

### 1. `docs/requirement-splitter.md` §1–§2

先讀這兩節，建立直覺：
- §1 的「具體失敗情境」圖解說明了 sequential split → 放置失敗的問題
- §2 的資訊流圖說明了 joint optimization 的概念

**目標**：能用一句話解釋「為什麼要把 split 和 placement 放進同一個 model」

---

## 第二層：What（資料結構）—— 20 分鐘

### 2. `app/models.py`（全部，約 200 行）

**閱讀順序**：
```
Resources → Baremetal → VM → AntiAffinityRule → SolverConfig
→ PlacementRequest → PlacementResult
→ ResourceRequirement → SplitPlacementRequest → SplitDecision → SplitPlacementResult
```

**第一次閱讀時重點關注**：
- `Resources.fits_in()` ── 這是最底層的「VM 能不能放進這台 BM」判斷
- `SolverConfig.vm_specs` ── 雙重用途：slot score 的 t-shirt sizes + splitter 的候選規格
- `ResourceRequirement` ── caller 送進來的「我需要多少資源」描述，`vm_specs=None` 時 fallback 到 config
- `SplitPlacementResult.split_decisions` ── solver 輸出「要建幾台哪種 VM」的決策

**可以先跳過**：`Topology`、`AntiAffinityRule` 的細節（只要知道它存在就好）

---

## 第三層：How（實作細節）—— 50 分鐘，分四個子步驟

### 3. `app/solver.py`：先讀「原始路徑」（不含 splitter 的部分）

**建議閱讀順序與重點**：

```
__init__()
  → 注意 self.model = cp_model.CpModel()（稍後會看到這行被改成可接收外部 model）
  → 注意 self.assign: dict[(vm_id, bm_id), BoolVar]（這是核心決策變數）

_build_variables()
  → 了解「只為 eligible pairs 建 BoolVar」的設計

_add_one_bm_per_vm_constraint()
  → 理解 sum(vm_vars) == 1 的語意（每台 VM 只能放一台 BM）

_add_capacity_constraints()
  → 理解 Σ demand × assign_var <= capacity 的語意

_add_anti_affinity_constraints()
  → 快速瀏覽即可，重點是「同一 AG 裡的 VM 數量有上限」

_add_objective()
  → 重點看 w_consolidation（少用 BM）和 w_headroom（不超載）的 terms
  → 注意最後的 getattr(self, "_splitter_waste_terms", [])  ← 這是 splitter 注入的鉤子

solve()
  → 看整個 pipeline：build vars → add constraints → add objective → solve → extract
  → 注意 self._last_cp_solver = solver  ← 這是 splitter 讀取 count_var 值的管道

_extract_solution()
  → 注意 active_var == 0 時 continue 的那段  ← 這是 splitter 新增的邏輯
```

**此階段理解目標**：能畫出「一次 solve 呼叫的執行流程圖」

---

### 4. `app/splitter.py`：splitter 加了什麼

這個檔案的 module docstring 就是最好的地圖，**先讀 docstring 再讀 code**。

**閱讀順序**：

```
__init__()
  → 注意 count_vars / active_vars / synthetic_vms 三個容器的用途

build()
  → 入口點，呼叫 _resolve_specs 和 _build_requirement

_resolve_specs()
  → 兩層 fallback 邏輯：requirement.vm_specs → config.vm_specs
  → 過濾掉「放不進任何 BM」的 spec（提前剪枝）

_build_requirement()  ← 核心，花最多時間在這裡
  → 理解 count_var 和 active_var 的關係
  → 理解 coverage constraint: Σ count[s] × spec[s].cpu ≥ total.cpu
  → 理解 link constraint: Σ active[k] == count[s]
  → 理解 symmetry breaking: active[k] ≥ active[k+1]（為什麼這能加速搜尋）

_compute_upper_bound()
  → 理解「max across dimensions」的邏輯（每個維度算一個 upper，取最大值）

build_waste_objective_terms()
  → waste = allocated - total_demand（可以是非零正整數，objective 會 minimize 它）

get_split_decisions()
  → solve 完後讀 count_var 的值，轉成 SplitDecision list
```

**此階段理解目標**：能解釋「為什麼需要 active_var，count_var 單獨不夠」
（答案：count_var 說「要幾台」，但 solver 需要知道「哪幾個 slot 是有效的」才能加 assign constraint）

---

### 5. `app/split_solver.py`：兩者如何被串在一起

這個檔案只有 89 行，是整個 feature 的「裝配廠」。

**逐行閱讀，特別注意**：

```python
model = cp_model.CpModel()                    # 建立共用 model

splitter = ResourceSplitter(model=model, ...)  # splitter 用這個 model
synthetic_vms = splitter.build()               # splitter 在 model 上加 split constraints

solver_instance = VMPlacementSolver(
    placement_request,
    model=model,              # ← 同一個 model！這是 joint optimization 的關鍵
    active_vars=splitter.active_vars,  # ← 把 active_var 傳進去
)

solver_instance._splitter_waste_terms = ...   # 注入 waste penalty
result = solver_instance.solve()              # 一次 solve，兩組 constraints 同時在 model 裡
```

**此階段理解目標**：能解釋 `model=model` 這一行的重要性，以及拿掉它會發生什麼事

---

### 6. `tests/test_splitter.py`：用測試驗證理解

不需要全部讀完，**選這幾個測試讀**：

| 測試 | 理解目標 |
|------|---------|
| `TestBasicSplit::test_exact_division` | 最簡單的 happy path |
| `TestBasicSplit::test_max_vm_count_makes_infeasible` | max_vms constraint 如何讓它 infeasible |
| `TestMultiSpecSplit::test_prefers_less_waste` | waste objective 如何影響 spec 選擇 |
| `TestSplitWithAntiAffinity::test_auto_anti_affinity_spreads_synthetic_vms` | synthetic VM 和 anti-affinity 的互動 |
| `TestMixedMode::test_explicit_and_split_coexist` | explicit + synthetic VM 共存 |

**建議方式**：讀完一個測試後，在腦中追蹤「這個 test 的 constraint 是在哪個函數加進去的」

---

## 快速參考地圖

```
Request 進來
    │
    ├── SplitPlacementRequest (models.py)
    │       └── ResourceRequirement × N
    │
    ▼
solve_split_placement() (split_solver.py)
    │
    ├─► ResourceSplitter.build() (splitter.py)
    │       ├── count_var[(req, spec)]   IntVar
    │       ├── active_var[vm_id]        BoolVar
    │       ├── coverage constraints
    │       └── synthetic VMs list
    │
    ├─► VMPlacementSolver(model=shared, active_vars=...) (solver.py)
    │       ├── assign[(vm_id, bm_id)]  BoolVar
    │       ├── capacity constraints
    │       ├── anti-affinity constraints
    │       └── objective (consolidation + headroom + waste)
    │
    ├─► solver.solve()  ← 一次搜尋，兩套 constraints 同時作用
    │
    └─► extract: PlacementAssignment + SplitDecision
```

---

## 如果要延伸新功能，入口點在哪

| 想加的功能 | 先讀 | 改動入口 |
|-----------|------|---------|
| 新的 split constraint（例如某個 spec 至少 2 台）| `splitter.py::_build_requirement` | 在 `_build_requirement` 末尾加 `self.model.add(...)` |
| 新的 split objective term | `splitter.py::build_waste_objective_terms` | 回傳更多 expression |
| 讓 solver 知道 anti-affinity 的 AG 分布再 split | `splitter.py::_build_requirement` + `solver.py::_resolve_anti_affinity_rules` | splitter 目前不感知 AG，需要傳入 ag_to_bms |
| 為不同 requirement 設定不同 anti-affinity group | `split_solver.py` | 在組合 PlacementRequest 時自動生成 AntiAffinityRule，vm_ids 設為同 role 的 synthetic VM ids |
| 回傳每個 requirement 的 waste 數量 | `models.py::SplitPlacementResult` + `splitter.py::get_split_decisions` | 在 SplitDecision 加 `waste` 欄位 |

---

## 建議的實際操作練習

1. **跑測試，邊看 log**：`pytest tests/test_splitter.py -v -s` 觀察 INFO log 輸出
2. **改 w_resource_waste=0 跑 test_prefers_less_waste**：看 solver 是否不再偏好 zero-waste spec
3. **在 `_build_requirement` 加一行 `print(f"upper_bound for spec {spec}: {upper}")`**：直觀理解 upper bound 計算
4. **讀 `_add_one_bm_per_vm_constraint` 的 active_var 分支**，再對應到 `splitter.py` 的 link constraint，理解「兩邊如何呼應」
