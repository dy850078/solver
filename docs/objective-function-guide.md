# Objective Function 實作導覽

> 適用版本：solver (CP-SAT sidecar)
> 目標：能獨立實作 Consolidation + Headroom 目標函數，並具備自行做 Enhancement 的能力

這份導覽分為三個部分：

- **Part 1**：CP-SAT 核心 API（針對本 project 用到的部分）
- **Part 2**：Python 語言模式（本 project 中常見的寫法）
- **Part 3**：實作步驟（7 個 Step，每個有練習題）

執行環境：

```bash
source .venv/bin/activate
python -m pytest tests/ -v
```

---

## Part 1：CP-SAT 核心 API

### 1.1 CP-SAT 是什麼

CP-SAT（Constraint Programming - Satisfiability）是 Google OR-Tools 提供的整數規劃求解器。

你給它三樣東西：
1. **變數（Variables）**：可以取哪些整數值
2. **約束（Constraints）**：變數之間的規則（必須滿足）
3. **目標（Objective）**：要最小化或最大化的表達式（可選）

它回傳一個「讓所有約束都成立，且目標值最好的」變數賦值。

本 project 的使用方式：
```
變數：assign[(vm_id, bm_id)] = 0 或 1
約束：容量限制、每台 VM 最多放一台 BM、anti-affinity
目標：（待實作）minimize 使用的 BM 數量 + 超過安全利用率的程度
```

---

### 1.2 變數類型

#### BoolVar — 布林變數（0 或 1）

```python
from ortools.sat.python import cp_model
model = cp_model.CpModel()

x = model.NewBoolVar("x")
# x 只能是 0 或 1
# 等同於 model.NewIntVar(0, 1, "x")，但 CP-SAT 對 BoolVar 有額外優化
```

用途：`assign[(vm_id, bm_id)]` 就是 BoolVar。

#### IntVar — 整數變數（有上下界）

```python
# 宣告方式：NewIntVar(lower_bound, upper_bound, name)
util_pct = model.NewIntVar(0, 100, "util_pct")
# util_pct 只能是 0 到 100 之間的整數

after_usage = model.NewIntVar(0, 640_000, "after_usage_cpu")
# 注意：上界要根據實際可能的最大值設定
```

**重要**：上界設太小會讓 model 變 invalid（MODEL_INVALID 錯誤）。
上界設太大不影響正確性，只是搜尋空間稍大。

#### NewConstant — 常數（包裝成變數）

```python
zero = model.NewConstant(0)
# 值永遠是 0，可以當作變數傳入需要變數列表的 API
```

用途：`AddMaxEquality` 需要一個列表，常數 0 可以搭配 ReLU。

---

### 1.3 約束 API

#### Add — 線性等式 / 不等式

```python
model.Add(x == 5)       # 等式
model.Add(x + y <= 10)  # 不等式
model.Add(x == y + z)   # 兩邊都是表達式
```

CP-SAT 只支援**線性**表達式（加減乘以常數）。不支援 `x * y`（兩個變數相乘）。

```python
# OK：變數 × 常數
model.Add(3 * x + 2 * y <= 100)

# OK：sum 表達式
model.Add(sum(demand[i] * assign_var[i] for i in range(n)) <= capacity)

# 不行：兩個變數相乘（非線性）
model.Add(x * y == z)  # 會報錯
```

#### AddMaxEquality — 最大值等式

```python
model.AddMaxEquality(target, [v1, v2, v3])
# 效果：target = max(v1, v2, v3)
```

用途一：**BM 是否被使用**
```python
# bm_used = max(assign[vm1,bm], assign[vm2,bm], assign[vm3,bm])
# 只要有任何一個 assign = 1，bm_used 就是 1
model.AddMaxEquality(bm_used, [assign[(vm1, bm)], assign[(vm2, bm)], assign[(vm3, bm)]])
```

用途二：**ReLU（截斷負值）**
```python
# over = max(0, raw)   ← 如果 raw < 0，over = 0（沒有超過上限）
model.AddMaxEquality(over, [model.NewConstant(0), raw])
```

用途三：**跨維度取最壞情況**
```python
# bm_penalty = max(over_cpu, over_mem, over_disk)
model.AddMaxEquality(bm_penalty, [over_cpu, over_mem, over_disk])
```

#### AddDivisionEquality — 整數除法（floor）

```python
model.AddDivisionEquality(result, dividend, divisor)
# 效果：result = floor(dividend / divisor)
```

注意：CP-SAT 不支援浮點數，所有計算必須是整數。

**計算利用率百分比的標準做法**：
```python
# 錯誤：after_usage // total 只能得到 0 或 1（精度太低）
# 正確：先乘 100 再整除
after_times_100 = model.NewIntVar(0, total * 100, "a100")
model.Add(after_times_100 == after_usage * 100)

util_pct = model.NewIntVar(0, 100, "util_pct")
model.AddDivisionEquality(util_pct, after_times_100, total)
# 等價於：util_pct = floor(after_usage / total * 100)
```

驗證：after=9, total=10 → after_times_100=900 → util_pct = 900//10 = 90

---

### 1.4 目標函數

```python
model.Minimize(expr)   # 最小化
model.Maximize(expr)   # 最大化（等價於 Minimize 負值）
```

`expr` 必須是線性表達式：
```python
# 正確
model.Minimize(sum(bm_used.values()))
model.Minimize(10 * bm_count + 8 * total_penalty)

# 不行：不能傳變數列表，只能傳一個表達式
model.Minimize([v1, v2, v3])  # 錯誤
```

**目標 vs 約束的差別**：
- 約束：「必須滿足，否則無解」
- 目標：「在所有合法解中，偏好讓這個值更小的解」

目標函數不能讓無解的問題變有解，也不會讓本來有解的問題變無解。

#### 多目標的優先級設計

CP-SAT 只能有一個目標函數，但可以把多個目標合併：

```python
# 優先級：A > B > C
# 設計：讓 A 的係數遠大於 B 和 C 加起來的最大值

MAX_B_C = 100 * n_bms * 10  # B + C 的理論上限

model.Minimize(
    -1_000_000 * a    # 最高優先（負號 = 越大越好）
    + 10 * b          # 次優先
    + 8 * c           # 最低優先
)
```

只要 `1_000_000 >> MAX_B_C`，「多放一個 VM（讓 a+1）」對目標值的影響，
永遠大於「b 和 c 如何變化」的影響。

---

### 1.5 CP-SAT 的執行流程

```
model = CpModel()
    ↓ 宣告變數 (NewBoolVar, NewIntVar)
    ↓ 加入約束 (Add, AddMaxEquality, AddDivisionEquality)
    ↓ 設定目標 (Minimize / Maximize)

solver = CpSolver()
solver.parameters.max_time_in_seconds = 30
status = solver.Solve(model)

if status in (OPTIMAL, FEASIBLE):
    value = solver.Value(some_var)  # 讀取解
```

Status 含義：
| Status | 意義 |
|--------|------|
| OPTIMAL | 找到最優解（在時限內搜尋完畢）|
| FEASIBLE | 找到合法解（但不確定是最優）|
| INFEASIBLE | 確定無解 |
| MODEL_INVALID | 模型本身有錯誤（如上下界反轉）|
| UNKNOWN | 時間到了，什麼都沒找到 |

---

## Part 2：Python 語言模式

### 2.1 Pydantic v2 BaseModel

本 project 用 Pydantic v2 取代 dataclass。宣告欄位的方式：

```python
from pydantic import BaseModel, Field

class Config(BaseModel):
    # 簡單欄位：型別 = 預設值
    timeout: float = 30.0
    max_workers: int = 8
    enabled: bool = True

    # 需要 Field 的情況（如 default_factory）
    items: list[str] = Field(default_factory=list)
```

建立物件：
```python
cfg = Config()                        # 全用預設值
cfg = Config(timeout=10.0)            # 部分覆蓋
cfg = Config(**{"timeout": 10.0})     # 從 dict 建立
```

從 JSON 建立（本 project 的主要用途）：
```python
cfg = Config.model_validate_json('{"timeout": 10.0}')
cfg = Config.model_validate({"timeout": 10.0})
```

---

### 2.2 型別提示

```python
# dict 型別提示
bm_used: dict[str, cp_model.IntVar] = {}
# key 是字串（bm_id），value 是 CP-SAT 的 IntVar

# list 型別提示
penalties: list[cp_model.IntVar] = []

# tuple 作為 dict key（本 project 的 assign）
assign: dict[tuple[str, str], cp_model.IntVar] = {}
# key 是 (vm_id, bm_id) tuple
```

---

### 2.3 Generator Expression 與 sum()

Python 的 `sum()` 可以接受 generator expression（不需要先建 list）：

```python
# 基本用法
total = sum(x for x in [1, 2, 3])           # 6

# 帶條件的篩選
total = sum(x for x in items if x > 0)

# 兩層迴圈
total = sum(a * b for a, b in pairs)

# 搭配 CP-SAT（核心用法）
new_usage = sum(
    getattr(self.vm_map[vm_id].demand, field) * var
    for vm_id, var in assigned_vars
)
# 注意：這回傳的是 CP-SAT 的 LinearExpr，不是 Python int
```

---

### 2.4 getattr() 動態存取屬性

```python
class Resources:
    cpu_cores: int = 0
    memory_mb: int = 0

res = Resources(cpu_cores=4, memory_mb=16000)

# 靜態存取
val = res.cpu_cores   # 4

# 動態存取（適合迴圈多個維度）
for field in ["cpu_cores", "memory_mb", "disk_gb", "gpu_count"]:
    val = getattr(res, field)   # 等同於 res.cpu_cores, res.memory_mb, ...
    print(f"{field} = {val}")
```

本 project 中，`RESOURCE_FIELDS = ["cpu_cores", "memory_mb", "disk_gb", "gpu_count"]` 就是用這個模式讓一段程式同時處理四個資源維度。

---

### 2.5 List Comprehension 與 in 運算子

```python
# dict 的 key lookup：O(1)
if (vm_id, bm_id) in self.assign:
    var = self.assign[(vm_id, bm_id)]

# list comprehension 收集符合條件的項目
vm_vars_on_bm = [
    self.assign[(vm_id, bm.id)]
    for vm_id in self.vm_map           # 遍歷所有 VM
    if (vm_id, bm.id) in self.assign   # 只保留有建立 assign 變數的 pair
]
```

---

## Part 3：實作步驟

每個 Step 包含：「做什麼 → 核心概念 → 你的任務 → 驗證方式」。

---

### Step 1：擴充 SolverConfig

**做什麼**：在 `app/models.py` 的 `SolverConfig` 加入三個新欄位。

**核心概念**：Pydantic v2 欄位宣告（參見 Part 2.1）。

**你的任務**：找到 `SolverConfig` 類別（約 162 行），在 `auto_generate_anti_affinity` 後面加入：
- `w_consolidation: int` — consolidation 目標的權重（預設讓現有測試不需要改動）
- `w_headroom: int` — headroom 目標的權重
- `headroom_upper_bound_pct: int` — 超過此百分比才懲罰（預設 90）

思考問題：
1. 預設值應該設多少？（提示：現有 23 個測試都用預設 config，目標函數只是「偏好」，不影響哪些解合法）
2. 如果 `w_consolidation=0` 代表什麼？

**驗證**：
```bash
python -m pytest tests/ -v -k "not Objective"
# 應全部通過（23 個測試）
```

---

### Step 2：在 `__init__` 加入 `bm_used` dict

**做什麼**：在 `app/solver.py` 的 `VMPlacementSolver.__init__` 末尾加入一個空 dict，之後的方法會填充它。

**核心概念**：型別提示（Part 2.2）。

**你的任務**：在 `self.assign: dict[...] = {}` 後面，加入：
```python
self.bm_used: dict[str, cp_model.IntVar] = {}
```

**為什麼要這樣設計**：`_build_bm_used_vars()` 會填充它，`_add_objective()` 會讀取它。
把它放在 `__init__` 而不是區域變數，讓兩個方法都能存取。

**驗證**：
```bash
python -c "from app.solver import VMPlacementSolver; print('OK')"
```

---

### Step 3：實作 `_build_bm_used_vars()`

**做什麼**：對每台 BM，建立一個布林變數，表示「這台 BM 是否被使用」。

**核心概念**：`NewBoolVar` + `AddMaxEquality`（Part 1.2、1.3）。

**數學定義**：
```
bm_used[bm] = max(assign[vm_1, bm], assign[vm_2, bm], ...)
            = 1  如果任何 VM 放在這台 BM
            = 0  如果沒有 VM 放在這台 BM
```

**你的任務**：在 `_add_anti_affinity_constraints` 方法後面，加入新方法 `_build_bm_used_vars(self)`：

```python
def _build_bm_used_vars(self):
    for bm in self.request.baremetals:
        bm_used = self.model.NewBoolVar(f"bm_used_{bm.id}")

        # 1. 收集所有「可能被指派到這台 BM 的 assign 變數」
        vm_vars_on_bm = [
            ???
            for vm_id in self.vm_map
            if (vm_id, bm.id) in self.assign
        ]

        # 2. 用 AddMaxEquality 連結
        if vm_vars_on_bm:
            self.model.AddMaxEquality(???, ???)
        else:
            # 邊界：沒有任何 VM 考慮過這台 BM → 永遠不被使用
            self.model.Add(bm_used == 0)

        # 3. 存到 self.bm_used
        self.bm_used[???] = bm_used
```

思考問題：
1. list comprehension 的每一個元素是什麼型別？（`IntVar`，是 assign 的 value）
2. `AddMaxEquality(target, variables)` 的兩個參數各填什麼？
3. 為什麼要處理 `vm_vars_on_bm` 為空的情況？（提示：`AddMaxEquality` 需要至少一個變數）

**驗證**：
```bash
# 先不跑全部測試，只確認方法可以被呼叫不報錯
python -c "
from app.models import *
from app.solver import VMPlacementSolver
bm = Baremetal(id='bm-1', total_capacity=Resources(cpu_cores=10, memory_mb=1000, disk_gb=100))
vm = VM(id='vm-1', demand=Resources(cpu_cores=2, memory_mb=100, disk_gb=10))
req = PlacementRequest(vms=[vm], baremetals=[bm], config=SolverConfig(w_headroom=0))
solver = VMPlacementSolver(req)
solver._build_variables()
solver._build_bm_used_vars()
print('bm_used keys:', list(solver.bm_used.keys()))
"
```

---

### Step 4：實作 `_compute_headroom_penalties()`

**做什麼**：對每台 BM，計算「最壞資源維度的利用率超過安全上限幾個百分比」。

**核心概念**：`AddDivisionEquality` + ReLU + 跨維度取 max（Part 1.3）。

**計算流程（對每台 BM 的每個資源維度）**：

```
A. after_usage = used_capacity + Σ(demand × assign_var)
   → 這台 BM 放入所有 VM 後的總使用量

B. util_pct = floor(after_usage × 100 / total_capacity)
   → 利用率百分比（0–100）

C. raw = util_pct - headroom_upper_bound_pct
   → 超過上限的量（可能為負）

D. over = max(0, raw)
   → 負數截斷為 0（沒超過就是 0）

E. bm_penalty = max(over_cpu, over_mem, over_disk, over_gpu)
   → 最壞維度決定這台 BM 的 penalty
```

**你的任務**：實作以下框架：

```python
def _compute_headroom_penalties(self) -> list[cp_model.IntVar]:
    penalties = []
    for bm in self.request.baremetals:
        dim_overs = []
        for field in RESOURCE_FIELDS:
            total_d = getattr(bm.total_capacity, field)
            if total_d == 0:
                continue   # 為什麼要跳過？

            used_d = getattr(bm.used_capacity, field)

            # 收集這台 BM 上所有候選的 (vm_id, assign_var) pair
            assigned_vars = [
                (vm_id, self.assign[(vm_id, bm.id)])
                for vm_id in self.vm_map
                if (vm_id, bm.id) in self.assign
            ]
            if not assigned_vars:
                continue

            # 新放入的 VM 消耗（每個 VM 的 demand × assign 變數，加總）
            new_usage = sum(
                getattr(self.vm_map[vm_id].demand, field) * var
                for vm_id, var in assigned_vars
            )

            # Step A: after_times_100
            after_times_100 = self.model.NewIntVar(
                0, ???, f"a100_{bm.id}_{field}"   # 上界是多少？
            )
            self.model.Add(after_times_100 == (used_d + new_usage) * 100)

            # Step B: util_pct（0–100）
            util_pct = self.model.NewIntVar(0, 100, f"util_{bm.id}_{field}")
            self.model.AddDivisionEquality(???, ???, ???)

            # Step C: raw（可能為負！）
            raw = self.model.NewIntVar(???, ???, f"raw_{bm.id}_{field}")
            self.model.Add(raw == util_pct - self.config.headroom_upper_bound_pct)

            # Step D: over = max(0, raw)
            over = self.model.NewIntVar(0, 100, f"over_{bm.id}_{field}")
            self.model.AddMaxEquality(???, [???, ???])
            dim_overs.append(over)

        if dim_overs:
            # Step E: bm_penalty = 跨維度最大值
            bm_penalty = self.model.NewIntVar(0, 100, f"hp_{bm.id}")
            self.model.AddMaxEquality(???, dim_overs)
            penalties.append(bm_penalty)

    return penalties
```

填空提示：
- `after_times_100` 的上界：`used_d` 最大是 `total_d`，再加上所有 VM 的 demand（但都在 `avail` 以內），所以最大就是 `total_d`；乘以 100 後是 `total_d * 100`
- `raw` 的範圍：`util_pct` 是 0–100，上限是 90，所以 raw 最小是 `0-90=-90`，最大是 `100-0=100`；保守設 `(-100, 100)`
- `AddDivisionEquality(result, dividend, divisor)`：參數順序是「結果、被除數、除數」
- `AddMaxEquality` 的第二個參數需要一個「列表」，常數 0 要用 `self.model.NewConstant(0)` 包裝

**驗證**：
```bash
python -c "
from app.models import *
from app.solver import VMPlacementSolver
bm = Baremetal(id='bm-1', total_capacity=Resources(cpu_cores=10, memory_mb=1000, disk_gb=100))
vm = VM(id='vm-1', demand=Resources(cpu_cores=8, memory_mb=100, disk_gb=10))
req = PlacementRequest(vms=[vm], baremetals=[bm], config=SolverConfig(w_consolidation=0))
solver = VMPlacementSolver(req)
solver._build_variables()
penalties = solver._compute_headroom_penalties()
print('penalties count:', len(penalties))
# 預期：1（只有 bm-1 有候選 VM）
"
```

---

### Step 5：實作 `_add_objective()`

**做什麼**：把所有目標項組合成一個 `Minimize` 呼叫。

**核心概念**：優先級設計（Part 1.4）。

**你的任務**：在 `_compute_headroom_penalties` 後面，加入 `_add_objective(self)`：

```python
def _add_objective(self):
    terms = []

    # 最高優先：partial placement 模式下，多放 VM 永遠比任何 penalty 重要
    if self.config.allow_partial_placement:
        total_placed = sum(self.assign.values())
        # 為什麼用 -1_000_000？（提示：Minimize，負號讓「越多越好」）
        terms.append(??? * total_placed)

    # Consolidation：minimize 被使用的 BM 數量
    if self.config.w_consolidation > 0:
        self._build_bm_used_vars()
        terms.append(??? * sum(self.bm_used.values()))

    # Headroom：minimize 超過安全利用率的程度
    if self.config.w_headroom > 0:
        penalties = self._compute_headroom_penalties()
        if penalties:
            terms.append(??? * sum(penalties))

    if terms:
        self.model.Minimize(sum(terms))
```

思考問題：
1. 為什麼要先判斷 `if self.config.w_consolidation > 0` 才呼叫 `_build_bm_used_vars()`？（提示：如果不需要，就不要建立多餘的變數）
2. `sum(self.assign.values())` 回傳什麼型別？（CP-SAT LinearExpr，可以被 Minimize）
3. `-1_000_000` 是怎麼估算出來的？（提示：w_headroom × 100 × N_BMs + w_consolidation × N_BMs，假設最多 1000 台 BM）

**驗證**：
```bash
python -m pytest tests/ -v -k "not Objective"
# 還是 23 個測試，應全部通過
```

---

### Step 6：修改 `solve()` 方法

**做什麼**：把 `solve()` 裡舊的 Maximize 邏輯換成 `self._add_objective()`。

**你的任務**：找到這段舊代碼（約 300–306 行）：

```python
# 舊代碼
if self.config.allow_partial_placement:
    total_placed = sum(self.assign[key] for key in self.assign)
    self.model.Maximize(total_placed)
```

把它換成：

```python
self._add_objective()
```

**注意**：把整個 if 區塊（包含 Maximize 那行）都換掉，只留這一行。

**驗證**：
```bash
python -m pytest tests/ -v -k "not Objective"
# 應仍全部通過（partial placement 測試也要過）
```

---

### Step 7：寫 TestObjective 測試類別

**做什麼**：在 `tests/test_solver.py` 末尾加入三個測試，驗證目標函數的行為。

**核心概念**：測試設計原則：
- 一次只測一個元件（一個 weight 設 0，只測另一個）
- 製造「好選項」和「壞選項」，驗證 solver 選了好的
- 驗證**行為**（用幾台 BM），不驗證**具體 ID**（除非必要）

現有 helper 可以直接使用：
```python
def solve(vms, bms, rules=None, **config_overrides):
    # 支援任意 SolverConfig 欄位，直接 pass 進去即可
    ...

def make_bm(bm_id, cpu=64, mem=256_000, disk=2000, gpu=0,
            used_cpu=0, used_mem=0, used_disk=0, ag="ag-1", ...):
    ...

def make_vm(vm_id, cpu=4, mem=16_000, disk=100, ...):
    ...
```

**你的任務**：在最後一個測試類別（`TestSerialization`）後面加入：

```python
class TestObjective:

    def test_consolidation_prefers_fewer_bms(self):
        """
        場景：2 台 BM，各有 64 cpu / 256GB，都夠放下全部 VM。
        3 台 VM，各需 4 cpu / 16GB。
        開啟 consolidation，關閉 headroom。
        期望：全部 VM 放在同一台 BM（minimize BM 數量）。
        """
        bms = [
            make_bm("bm-1", cpu=???, mem=???),
            make_bm("bm-2", cpu=???, mem=???),
        ]
        vms = [make_vm(f"vm-{i}", cpu=???, mem=???) for i in range(3)]

        r = solve(vms, bms, w_consolidation=10, w_headroom=0)

        assert r.success
        assert len(r.assignments) == 3

        bm_ids_used = {a.baremetal_id for a in r.assignments}
        assert len(bm_ids_used) == ???, f"Expected ? BM used, got: {bm_ids_used}"

    def test_headroom_avoids_high_utilization(self):
        """
        場景：BM-A 已使用 0 cpu，BM-B 已使用 2 cpu（total 都是 10）。
        VM 需要 8 cpu。
        關閉 consolidation，開啟 headroom（上限 90%）。
        BM-A：(0+8)/10 = 80% → over = 0
        BM-B：(2+8)/10 = 100% → over = 10
        期望：VM 放在 BM-A。
        """
        bms = [
            make_bm("bm-a", cpu=10, mem=256_000, used_cpu=0),
            make_bm("bm-b", cpu=10, mem=256_000, used_cpu=???),
        ]
        vms = [make_vm("vm-1", cpu=8, mem=16_000)]

        r = solve(vms, bms, w_consolidation=0, w_headroom=8)

        assert r.success
        assert amap(r)["vm-1"] == "???", f"Expected bm-a, got: {amap(r)}"

    def test_partial_placement_priority_over_consolidation(self):
        """
        場景：1 台 BM（cpu=8），3 台 VM（各需 4 cpu）。
        容量只夠放 2 台。
        allow_partial_placement=True，consolidation 開啟。
        期望：放 2 台（不能因為 consolidation 而只放 1 台）。
        """
        bms = [make_bm("bm-1", cpu=???, mem=32_000, disk=200)]
        vms = [make_vm(f"vm-{i}") for i in range(3)]

        r = solve(vms, bms, allow_partial_placement=True, w_consolidation=10)

        assert len(r.assignments) == ???, f"Expected ? placed, got {len(r.assignments)}"
        assert len(r.unplaced_vms) == ???
```

**驗證**：
```bash
python -m pytest tests/ -v -k "Objective"
# 預期：3 個新測試全部通過
```

---

### 最終驗證

```bash
python -m pytest tests/ -v
# 預期：全部 29 個測試通過（23 原有 + 6 新增）
# 注意：加入目標函數後，某些測試會稍慢（solver 需要搜尋更多才能找到最優解）
```

---

## Enhancement 指南

完成基本實作後，這裡列出幾個可能的 Enhancement 方向，以及各自需要的 CP-SAT/Python 知識。

### Enhancement A：新增 Disk/GPU 維度的差異化權重

**現在**：所有資源維度用同一個 `headroom_upper_bound_pct`（90%）。
**Enhancement**：CPU 上限 85%，Memory 上限 80%，Disk 上限 95%。

**需要修改**：
1. `SolverConfig` 加入 per-field 的上限（或一個 dict）
2. `_compute_headroom_penalties` 改為每個 field 讀取對應上限

**Python 技巧**：用 `dict` 型別的 Pydantic 欄位：
```python
headroom_pct_by_field: dict[str, int] = Field(
    default_factory=lambda: {
        "cpu_cores": 85, "memory_mb": 80,
        "disk_gb": 95, "gpu_count": 90,
    }
)
```

---

### Enhancement B：Soft Anti-Affinity（盡量分散，但不強制）

**現在**：Anti-affinity 是硬約束（`Add(sum <= max_per_ag)`），違反就無解。
**Enhancement**：允許違反，但給予 penalty。

**需要修改**：把 `Add(sum <= max_per_ag)` 改為建立 penalty 變數加入目標函數。

**CP-SAT 技巧**：
```python
# violation = max(0, count_in_ag - max_per_ag)
violation = model.NewIntVar(0, n_vms, f"aa_violation_{rule_id}_{ag}")
count_in_ag = sum(assign_vars_in_ag)
raw = model.NewIntVar(-n_vms, n_vms, f"aa_raw_{rule_id}_{ag}")
model.Add(raw == count_in_ag - rule.max_per_ag)
model.AddMaxEquality(violation, [model.NewConstant(0), raw])
# 加入目標
terms.append(w_soft_aa * violation)
```

---

### Enhancement C：BM 使用率的上下界（避免過輕負載）

**現在**：headroom 只懲罰「過高」利用率。
**Enhancement**：也懲罰「過低」利用率（e.g. 低於 20% 也算浪費）。

**CP-SAT 技巧**：用 `AddMinEquality` 和另一個 ReLU：
```python
# under = max(0, lower_bound - util_pct)
under = model.NewIntVar(0, 100, f"under_{bm.id}_{field}")
raw_under = model.NewIntVar(-100, 100, f"raw_under_{bm.id}_{field}")
model.Add(raw_under == self.config.headroom_lower_bound_pct - util_pct)
model.AddMaxEquality(under, [model.NewConstant(0), raw_under])
```

---

### Enhancement D：多叢集隔離（不同 cluster_id 的 VM 不放同一台 BM）

**現在**：沒有 cluster 隔離約束。
**Enhancement**：同一台 BM 上，只能放同一個 `cluster_id` 的 VM。

**CP-SAT 技巧**：這是「互斥」約束。可以用 `AddForbiddenAssignments`，或逐對加 `BoolOr`：
```python
# 對每對 (vm_a from cluster_1, vm_b from cluster_2)，不能同在同一台 BM
for bm_id in bm_ids:
    for vm_a in cluster_1_vms:
        for vm_b in cluster_2_vms:
            if (vm_a.id, bm_id) in assign and (vm_b.id, bm_id) in assign:
                # 不能同時為 1
                model.Add(
                    assign[(vm_a.id, bm_id)] + assign[(vm_b.id, bm_id)] <= 1
                )
```

注意：這種方式的變數數量是 O(VM² × BM)，對大規模 case 要考慮效能。

---

### Enhancement E：Slot Score（剩餘容量可用性）

**問題**：Consolidation 把 VM 塞進最少 BM，但不關心「放完之後剩下的資源能不能被後續的 VM 使用」。Headroom 只看百分比，不看剩餘空間是否匹配真實的 VM 規格。

**例子**：一台 BM 放完 VM 後剩 3 CPU，但最小的 VM t-shirt size 需要 4 CPU → 這 3 CPU 就是浪費（碎片化）。

**Slot Score 定義**：
對每台**被使用的** BM，計算放完 VM 後，剩餘容量還能裝幾個各種 t-shirt size 的 VM。
加總所有 t-shirt size 的可容納數量 = 該 BM 的 slot score。

```
slot_score(bm) = Σ_tshirt min_dim(remaining_dim // tshirt_dim)
```

Slot score 越高 = 剩餘空間越「有用」→ 在目標函數中獎勵（取負號）。

**重要設計考量**：
1. **只計算被使用的 BM**。否則 solver 會把 VM 往小 BM 塞，保留大 BM 的高 slot score → 與 consolidation 矛盾。
2. **預設權重 = 0**（opt-in）。Slot score 應作為 consolidation 的 tiebreaker，不能反過來壓過 consolidation。
3. **需要 `AddMinEquality`**（之前只用過 `AddMaxEquality`），因為瓶頸維度決定可容納數量。
4. **需要 `AddMultiplicationEquality`**（新 API），用來實現 `effective = bm_used × raw_score`。

**CP-SAT 新 API**：

```python
# AddMinEquality — 最小值等式（和 AddMaxEquality 對稱）
model.AddMinEquality(target, [v1, v2, v3])
# 效果：target = min(v1, v2, v3)

# AddMultiplicationEquality — 變數乘法
model.AddMultiplicationEquality(target, [var_a, var_b])
# 效果：target = var_a × var_b
# 注意：參數是列表，不是兩個獨立參數
```

---

#### Step E1：擴充 SolverConfig

**做什麼**：在 `app/models.py` 的 `SolverConfig` 加入 slot score 相關欄位。

**你的任務**：在 `headroom_upper_bound_pct` 後面加入：
- `w_slot_score: int` — slot score 的權重（預設 0，opt-in）
- `slot_tshirt_sizes: list[Resources]` — VM t-shirt size 定義列表

思考問題：
1. 為什麼預設 `w_slot_score=0` 而不是像 `w_consolidation=10` 那樣有預設值？
   （提示：slot score 的效果可能與 consolidation 衝突，需要使用者明確啟用）
2. `list[Resources]` 需要用 `Field(default_factory=...)` 嗎？為什麼？
   （提示：mutable default 問題，同 `list[str]`）

**驗證**：
```bash
python -m pytest tests/ -v -k "not SlotScore"
# 應全部通過（32 - 3 = 29 個測試）
```

---

#### Step E2：實作 `_compute_slot_score_bonus()`

**做什麼**：對每台被使用的 BM，計算放完 VM 後各 t-shirt size 的可容納數量。

**計算流程（對每台 BM）**：

```
A. 收集所有候選 (vm_id, assign_var) pair

B. 對每個 t-shirt size，對每個資源維度：
   remaining_d = total_d - used_d - Σ(demand × assign_var)
   slots_d = floor(remaining_d / tshirt_d)

C. slots_for_tshirt = min(slots_d 跨維度)  ← 瓶頸維度決定

D. bm_score = Σ slots_for_tshirt 跨所有 t-shirt size

E. effective_score = bm_used × bm_score  ← 只有被使用的 BM 才計入
```

**你的任務**：在 `_compute_headroom_penalties` 後面加入 `_compute_slot_score_bonus(self)`：

```python
def _compute_slot_score_bonus(self) -> list[cp_model.IntVar]:
    tshirt_sizes = self.config.slot_tshirt_sizes
    if not tshirt_sizes:
        return []

    scores = []
    for bm in self.request.baremetals:
        # A: 收集候選 assign var
        assigned_vars = [
            (vm_id, self.assign[(vm_id, bm.id)])
            for vm_id in self.vm_map
            if (vm_id, bm.id) in self.assign
        ]
        if not assigned_vars:
            continue

        tshirt_slots = []
        for t_idx, tshirt in enumerate(tshirt_sizes):
            dim_slots = []
            for field in RESOURCE_FIELDS:
                tshirt_d = getattr(tshirt, field)
                if tshirt_d == 0:
                    continue  # 為什麼跳過？（提示：除以零 + 此維度不是瓶頸）

                total_d = getattr(bm.total_capacity, field)
                used_d = getattr(bm.used_capacity, field)

                # 新放入的 VM 消耗
                new_usage = sum(
                    getattr(self.vm_map[vm_id].demand, field) * var
                    for vm_id, var in assigned_vars
                )

                # B: 剩餘容量
                remaining = self.model.NewIntVar(
                    0, ???, f"rem_{bm.id}_{field}_t{t_idx}"
                    # 上界是多少？（提示：最大是 total_d）
                )
                self.model.Add(remaining == ??? - ??? - ???)

                # B: 此維度可容納幾個
                slots_d = self.model.NewIntVar(
                    0, ???, f"slotd_{bm.id}_{field}_t{t_idx}"
                    # 上界：total_d // tshirt_d
                )
                self.model.AddDivisionEquality(???, ???, ???)
                dim_slots.append(slots_d)

            if dim_slots:
                # C: 跨維度取最小值（瓶頸維度決定）
                max_possible = min(
                    getattr(bm.total_capacity, f) // getattr(tshirt, f)
                    for f in RESOURCE_FIELDS
                    if getattr(tshirt, f) > 0
                )
                slots_for_tshirt = self.model.NewIntVar(
                    0, max_possible, f"slot_{bm.id}_t{t_idx}"
                )
                self.model.AddMinEquality(???, ???)
                tshirt_slots.append(slots_for_tshirt)

        if tshirt_slots:
            # D: 加總所有 t-shirt size 的可容納數量
            bm_score = self.model.NewIntVar(0, ???, f"sscore_{bm.id}")
            self.model.Add(bm_score == sum(tshirt_slots))

            # E: 只計算被使用的 BM（乘以 bm_used）
            effective = self.model.NewIntVar(0, ???, f"eff_sscore_{bm.id}")
            self.model.AddMultiplicationEquality(
                ???, [???, ???]
            )
            scores.append(effective)

    return scores
```

填空提示：
- `remaining` 的上界：`total_d`（最大剩餘 = 沒放任何 VM）
- `remaining` 的等式：`total_d - used_d - new_usage`
- `AddDivisionEquality(slots_d, remaining, tshirt_d)`：結果、被除數、除數
- `AddMinEquality(slots_for_tshirt, dim_slots)`：target、變數列表
- `AddMultiplicationEquality(effective, [self.bm_used[bm.id], bm_score])`
- `bm_score` 和 `effective` 的上界：加總所有 t-shirt size 的最大可能 slot 數

思考問題：
1. 為什麼 `remaining` 的下界是 0 而不是負數？
   （提示：capacity constraint 已經保證 total - used - placement >= 0）
2. `AddMinEquality` 和 `AddMaxEquality` 的用法有什麼差異？
   （提示：完全一樣，只是取 min vs max）
3. 為什麼要 `AddMultiplicationEquality` 而不是直接 `bm_used * bm_score`？
   （提示：CP-SAT 不支援兩個變數相乘的線性表達式，需要用專門的約束）

**驗證**：
```bash
python -c "
from app.models import *
from app.solver import VMPlacementSolver
bm = Baremetal(id='bm-1', total_capacity=Resources(cpu_cores=32, memory_mb=256000, disk_gb=2000))
vm = VM(id='vm-1', demand=Resources(cpu_cores=8, memory_mb=16000, disk_gb=100))
tshirts = [Resources(cpu_cores=4, memory_mb=16000, disk_gb=100)]
req = PlacementRequest(vms=[vm], baremetals=[bm], config=SolverConfig(w_consolidation=0, w_headroom=0, w_slot_score=5, slot_tshirt_sizes=tshirts))
solver = VMPlacementSolver(req)
solver._build_variables()
solver._build_bm_used_vars()
scores = solver._compute_slot_score_bonus()
print('scores count:', len(scores))
# 預期：1（bm-1 有候選 VM）
"
```

---

#### Step E3：連接 `_add_objective()`

**做什麼**：在 `_add_objective()` 中加入 slot score 項。

**你的任務**：在 headroom 項之後加入：

```python
if self.config.w_slot_score > 0:
    self._ensure_bm_used_vars()   # 為什麼需要這行？
    slot_scores = self._compute_slot_score_bonus()
    if slot_scores:
        terms.append(??? * sum(slot_scores))
        # 填入什麼？（提示：Minimize 中用負值 = 獎勵。slot score 越高越好）
```

思考問題：
1. 為什麼要 `_ensure_bm_used_vars()` 而不是直接 `_build_bm_used_vars()`？
   （提示：如果 consolidation 已經建了 `bm_used`，再建一次會報錯）
2. `-self.config.w_slot_score` 的負號和 Minimize 的關係是什麼？
   （提示：`Minimize(-5 * score)` 等價於 `Maximize(5 * score)`）
3. `w_slot_score` 應該設多少才不會壓過 consolidation？
   （提示：consolidation 每多用 1 台 BM = +10，slot score 差異通常 < 20，
   所以 `w_slot_score=1` 就足夠當 tiebreaker）

**驗證**：
```bash
python -m pytest tests/ -v -k "not SlotScore"
# 應全部通過（現有測試不受影響，因為 w_slot_score 預設 0）
```

---

#### Step E4：寫 TestSlotScore 測試類別

**做什麼**：在 `tests/test_solver.py` 末尾加入三個測試。

**測試設計原則**：
- 只測 slot score 時，關閉 consolidation 和 headroom（設為 0）
- 測 slot score + consolidation 時，確認 slot score 作為 tiebreaker 而非主導
- 測 w_slot_score=0 時確認不影響結果

**你的任務**：

```python
class TestSlotScore:

    def test_slot_score_prefers_usable_remainder(self):
        """
        場景：2 台 BM 都能放 VM(cpu=8)。
        BM-A: total=32 → 放完剩 24 cpu → small=6, medium=3, large=1 → score=10
        BM-B: total=10 → 放完剩 2 cpu  → 全部 0 → score=0
        只開 slot score，solver 應選 BM-A。
        """
        from app.models import Resources

        bms = [
            make_bm("bm-a", cpu=32, mem=256_000, disk=2000),
            make_bm("bm-b", cpu=10, mem=256_000, disk=2000),
        ]
        vms = [make_vm("vm-1", cpu=8, mem=16_000)]

        tshirts = [
            Resources(cpu_cores=???, memory_mb=???, disk_gb=???),   # small
            Resources(cpu_cores=???, memory_mb=???, disk_gb=???),   # medium
            Resources(cpu_cores=???, memory_mb=???, disk_gb=???),   # large
        ]

        r = solve(
            vms, bms,
            w_consolidation=0, w_headroom=0, w_slot_score=5,
            slot_tshirt_sizes=tshirts,
        )

        assert r.success
        assert amap(r)["vm-1"] == "???", f"Expected ???, got: {amap(r)}"

    def test_slot_score_breaks_consolidation_tie(self):
        """
        場景：2 台 BM 都夠放全部 VM，consolidation 會塞進 1 台（tie）。
        Slot score 打破 tie：偏好放完後剩餘 slot 更多的 BM。

        BM-A: total=32 cpu → 放 2 VM(4cpu) 後剩 24 → score 高
        BM-B: total=12 cpu → 放 2 VM(4cpu) 後剩 4  → score 低
        """
        # ...省略，參考 test_slot_score_prefers_usable_remainder 的寫法...
        # 注意：w_slot_score 設為 1（作為 tiebreaker，不壓過 w_consolidation=10）

    def test_slot_score_zero_weight_disables(self):
        """w_slot_score=0 時不影響結果（等同不開啟）。"""
        # ...驗證 w_slot_score=0 時，行為和沒有 slot score 一樣...
```

**驗證**：
```bash
python -m pytest tests/ -v -k "SlotScore"
# 預期：3 個新測試全部通過

python -m pytest tests/ -v
# 預期：全部 35 個測試通過
```

---

#### 目標函數全貌（加入 Slot Score 後）

```
Minimize(
    -1,000,000 × placed_count          ← P0：放越多越好（僅 partial mode）
    + w_consolidation × Σ bm_used       ← P1：用越少 BM 越好
    + w_headroom × Σ headroom_penalty   ← P2：避免超過利用率上限
    - w_slot_score × Σ effective_score  ← P3：獎勵剩餘空間可用性
)
```

權重建議：`w_consolidation=10, w_headroom=8, w_slot_score=1`。
Slot score 的量級（通常 0–30）× 1 = 0–30，遠小於 consolidation 的 10 × N_BMs，
確保 slot score 只在 consolidation 打平時才發揮作用。

---

## 常見錯誤排查

| 錯誤 | 可能原因 | 解法 |
|------|----------|------|
| `MODEL_INVALID` | `NewIntVar` 上下界設錯（lower > upper） | 檢查 `raw = NewIntVar(-100, 100, ...)` 不能寫成 `(0, 100)` |
| `MODEL_INVALID` | `AddDivisionEquality` 的除數可能是 0 | 加上 `if total_d == 0: continue` |
| consolidation 測試失敗，VM 仍分散 | `w_headroom` 沒有設為 0，headroom penalty 影響了選擇 | 測試時明確傳 `w_headroom=0` |
| headroom 測試失敗，solver 選錯 BM | `util_pct` 計算錯誤 | 手算：BM-B: `(2+8)*100 // 10 = 100`，over = `100-90 = 10` |
| partial placement 放的數量錯誤 | 舊 `Maximize` 沒有完全移除 | 確認 `solve()` 裡只有 `self._add_objective()` |
| 測試變慢很多 | 目標函數讓 solver 需要搜尋最優解 | 屬正常現象；可把 `max_solve_time_seconds` 加大 |
| slot score 反而讓 VM 分散 | `w_slot_score` 太大壓過了 consolidation | 確保 `w_slot_score` ≪ `w_consolidation`（建議 1 vs 10）|
| slot score 沒有效果 | 沒有乘以 `bm_used`，未使用的 BM 有高 score | 確認 `_ensure_bm_used_vars()` 有被呼叫，且用了 `AddMultiplicationEquality` |

---

## Quick Reference

### CP-SAT API 速查

```python
# 變數
model.NewBoolVar("name")                      # 0 or 1
model.NewIntVar(lb, ub, "name")               # 整數 [lb, ub]
model.NewConstant(value)                      # 固定值

# 約束
model.Add(expr == value)                      # 等式
model.Add(expr <= value)                      # 不等式
model.AddMaxEquality(target, [v1, v2, ...])   # target = max(v1, v2, ...)
model.AddMinEquality(target, [v1, v2, ...])   # target = min(v1, v2, ...)
model.AddDivisionEquality(r, dividend, div)   # r = floor(dividend / div)
model.AddMultiplicationEquality(t, [a, b])    # t = a × b（變數乘法）

# 目標
model.Minimize(expr)
model.Maximize(expr)

# 求解
solver = cp_model.CpSolver()
solver.parameters.max_time_in_seconds = 30
status = solver.Solve(model)
value = solver.Value(var)                     # 讀取解

# Status 常數
cp_model.OPTIMAL     # 最優解
cp_model.FEASIBLE    # 合法解（非最優）
cp_model.INFEASIBLE  # 無解
cp_model.MODEL_INVALID
cp_model.UNKNOWN
```

### 本 project 的資料結構速查

```python
# solver.py 的 instance variables
self.request      # PlacementRequest
self.config       # SolverConfig
self.vm_map       # dict[str, VM]           vm_id → VM
self.bm_map       # dict[str, Baremetal]    bm_id → Baremetal
self.ag_to_bms    # dict[str, list[str]]    ag → [bm_id, ...]
self.assign       # dict[(vm_id, bm_id), IntVar]
self.bm_used      # dict[str, IntVar]       bm_id → BoolVar（Step 2 後加入）

# RESOURCE_FIELDS = ["cpu_cores", "memory_mb", "disk_gb", "gpu_count"]
# 對每個 field 用 getattr(obj, field) 取值

# 判斷 (vm_id, bm_id) 是否有 assign 變數
if (vm_id, bm_id) in self.assign:
    var = self.assign[(vm_id, bm_id)]
```
