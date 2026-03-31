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

---

## 建模思維養成：從 count_var 到 active_var 的思考過程

### 為什麼最直覺的 count_var 設計不夠用

初學者面對 splitter 問題時，最自然的想法是：

```python
count_var["4c8g"] = IntVar(0..10)  # 讓 solver 決定要幾台 4c8g
```

解出數量後，再建立 VM instance 跑 placement solver。這是 **兩階段求解**：

```
階段一：splitter 解出數量 → 階段二：拿數量建 VM → 再跑 placement solver
```

致命問題：**階段一認為合法的 split，階段二可能放不下。** 例如 anti-affinity 約束可能導致 3 台 4c8g 在 placement 階段根本擺不進去，但 splitter 在階段一無從得知。兩個決策之間存在強耦合，拆開求解會丟失可行性保證。

### active_var 的本質：讓兩個問題共享同一個模型

核心 insight：**如果不知道最終要幾台，就把上界數量的 slot 全部先建出來，讓 solver 自己決定哪些 slot 要啟用。**

```python
# 假設最多需要 5 台 4c8g
for i in range(5):
    active_var[f"syn_4c8g_{i}"] = BoolVar()       # solver 決定開或關
    assign[f"syn_4c8g_{i}", bm] = BoolVar()       # 同時建好 placement 變數

# VM 被放到某台 BM 上 ⟺ 這個 slot 是 active 的
sum(assign[vm, all_bms]) == active_var[vm]

# count_var 就是 active 的加總（給人類看的）
sum(active_vars_for_spec) == count_var
```

這樣 **split 和 placement 在同一個 CpModel 裡被同時求解**，solver 不會選出一個「擺不下的 split」。

### 兩個變數各自的角色

| 面向 | count_var | active_var |
|------|-----------|-----------|
| **層級** | Requirement 層級 | 個別 VM slot 層級 |
| **型別** | IntVar(0..upper) | BoolVar |
| **誰建立** | Splitter | Splitter（建 upper 個 slot） |
| **誰使用** | Splitter 的 coverage constraints | Placement solver 的 assignment constraint |
| **語意** | 「這個 spec 要幾台」 | 「這個 slot 是否啟用」 |

---

## 這個設計模式的來歷

active_var 不是這個專案發明的，而是 CP/MIP 建模中的 **標準模式**：

### Pre-allocation with Activation Variables

在組合優化中，當「要用幾個」某種東西是未知的，標準做法是：

1. **建出上界數量的 slot**
2. **給每個 slot 一個 binary activation variable**
3. **讓 solver 自己決定啟用哪些**

這個模式在許多經典問題裡反覆出現：

| 經典問題 | 未知數量 | Pre-allocate 什麼 |
|---------|---------|------------------|
| Vehicle Routing (VRP) | 要用幾台車 | 上界數量的車，每台有 `used[v]` BoolVar |
| Facility Location | 要開幾個倉庫 | 所有候選位置，每個有 `open[f]` BoolVar |
| Bin Packing | 要用幾個箱子 | 上界數量的箱子，每個有 `active[b]` BoolVar |
| **Splitter（本專案）** | 要幾台某 spec 的 VM | 上界數量的 synthetic VM，每台有 `active_var` |

> 建模的 sense 不是「想出新招」，而是「認出舊招」。
> 模式庫越大，遇到新問題時能匹配到正確模式的速度就越快。

---

## CP-SAT 建模能力學習路徑

### 第一階段：學會基本模式（1–3 個月）

**目標**：建立常用建模模式的直覺

1. **精讀 OR-Tools CP-SAT 官方範例**，特別是：
   - [Bin Packing](https://developers.google.com/optimization/bin/bin_packing) — 正好是 activation variable 模式
   - [VRP](https://developers.google.com/optimization/routing) — 多種變體，activation + routing
   - [Job Shop Scheduling](https://developers.google.com/optimization/scheduling/job_shop) — interval variable 模式

2. **每個範例問自己三個問題**：
   - 決策變數是什麼？（什麼是 solver 需要決定的）
   - 約束是什麼？（什麼是不能違反的）
   - 目標是什麼？（什麼是要最大/最小化的）

3. **建立個人模式庫** — 常用的核心模式：

   | 模式 | 用途 | 本專案對應 |
   |------|------|-----------|
   | Binary activation | 未知數量的物件啟停 | `active_var` |
   | Big-M linearization | 把 if-then 邏輯線性化 | — |
   | Channeling constraints | `x == 3 ⟺ indicator[3] == 1` | — |
   | Interval + NoOverlap | 排程問題的時間區間 | — |
   | Element constraint | 用變數當 index 查表 | — |
   | Symmetry breaking | 消除等價解加速搜尋 | `active[k] >= active[k+1]` |

### 第二階段：學會問「這跟什麼問題同構？」（3–6 個月）

**目標**：遇到新問題時，能快速辨識出對應的經典問題結構

遇到新問題時，不要直接想「怎麼建模」，而是先問：

> 「這個問題的本質結構，跟哪個經典問題最像？」

以本專案的 splitter 為例：
- 有一堆「需求」要分配到「容器」裡 → **Bin Packing**
- 容器的數量不固定 → **Variable-size Bin Packing**
- 分配的同時要考慮放到哪台機器 → **Joint Bin Packing + Assignment**

認出是 bin packing 之後，activation variable 模式就是自然的選擇。

**推薦資源**：
- Coursera: [Modeling Discrete Optimization](https://www.coursera.org/learn/discrete-optimization)（University of Melbourne）— 建模思維最完整的課程
- MiniZinc 教材 — MiniZinc 是高階建模語言，專注在「如何把問題表達成約束」而非底層 API

### 第三階段：學會判斷 Joint Model vs Decomposition（6 個月以上）

**目標**：判斷何時該把所有約束放進同一個模型，何時該拆分

不是所有問題都適合塞進同一個模型：

| 策略 | 適用場景 | 本專案例子 |
|------|---------|-----------|
| **Joint model** | 兩個決策之間有強耦合（一個影響另一個的可行性） | split + placement 合併求解 |
| **Decomposition** | 問題太大 solver 跑不動，或子問題之間耦合弱 | 未來若 BM 數量達數千台，可能需要拆分 |

這個階段需要實戰經驗：嘗試 joint model → 觀察求解時間 → 判斷是否需要拆分。

### 學習時程建議

```
Week 1-2:   讀完 OR-Tools CP-SAT 所有官方教程，跑通每個範例
Week 3-4:   自己從零實作 bin packing + job shop（不看答案）
Week 5-8:   找 3-5 個真實問題建模（排班、配送、資源分配）
Week 9-12:  修 Coursera Discrete Optimization 課程，學習 MiniZinc
持續:        遇到問題先想「這跟什麼經典問題同構」再動手建模
```
