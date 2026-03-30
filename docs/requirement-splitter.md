# Requirement Splitter 設計文件

> **適用版本**: branch `zs/implement-splitter`（基底 commit `251bbe3`）
> **作者**: Claude Sonnet 4.6
> **日期**: 2026-03-30

---

## 目錄

1. [背景與問題](#1-背景與問題)
2. [解法概念：Joint Optimization](#2-解法概念joint-optimization)
3. [新增 API Endpoint](#3-新增-api-endpoint)
4. [檔案職責](#4-檔案職責)
5. [資料模型](#5-資料模型)
6. [CP-SAT 模型結構](#6-cp-sat-模型結構)
7. [重要設計決策與取捨](#7-重要設計決策與取捨)
8. [對既有程式的改動](#8-對既有程式的改動)
9. [後續開發注意事項](#9-後續開發注意事項)
10. [測試覆蓋](#10-測試覆蓋)
11. [使用範例](#11-使用範例)

---

## 1. 背景與問題

### 原有流程的缺陷

原本的 `/v1/placement/solve` 要求呼叫方（Go scheduler）在送出請求前就**自行決定每台 VM 的規格與數量**，solver 只負責把這些 VM 放到 BM 上。

```
Go scheduler                        Python solver
     │                                    │
     │  自行決定：4 台 × 8 CPU VM          │
     │─────── POST /v1/placement/solve ──▶│
     │                                    │  嘗試放置 4 台 VM
     │◀──── INFEASIBLE ───────────────────│  (anti-affinity 限制只有 3 個 AG)
     │                                    │
     │  換一種方式：2 台 × 16 CPU VM        │  ← 但 scheduler 不知道要換幾台
     │─────── POST /v1/placement/solve ──▶│
     │                                    │  這次成功放置
     │◀──── success ──────────────────────│
```

**核心問題**：split（決定 VM 數量與規格）和 placement（決定放哪個 BM）是**相互依賴**的決策，但原有設計強迫它們**序列執行**，導致：

1. Go scheduler 要猜 VM 規格，猜錯要反覆重試
2. 即使 scheduler 猜出了一個「資源上合法」的分法，placement 仍可能因 anti-affinity、BM 容量等限制而失敗
3. 沒有系統性的方式找到「既滿足資源需求、又能成功放置」的最優分法

### 具體失敗情境

```
總需求：master × 3 個 AG，需要 12 CPU
BM 配置：bm-0(ag-0), bm-1(ag-1)  ← 只有 2 個 AG

若 scheduler 送來「3 台 × 4 CPU VM」：
  anti-affinity 要求 max_per_ag=1 但只有 2 個 AG → INFEASIBLE

若改送「2 台 × 6 CPU VM」且 min_vms=2：
  資源夠，2 個 AG 各一台 → 成功
```

Scheduler 無從得知要送哪種分法，只能盲目嘗試或在上層加複雜的重試邏輯。

---

## 2. 解法概念：Joint Optimization

### 核心思想

把「split 決策」和「placement 決策」放進**同一個 CP-SAT model**，一次 solve 同時決定兩件事：

```
CpModel (shared)
├── Split variables  (ResourceSplitter 建立)
│   ├── count_var[spec]    — 每種 spec 建幾台
│   └── active_var[vm_id]  — 每個 synthetic slot 是否啟用
│
└── Placement variables  (VMPlacementSolver 建立)
    └── assign[(vm_id, bm_id)]  — 每台 VM 放哪個 BM
```

CP-SAT solver 在搜尋時會**同時考慮** split feasibility 和 placement feasibility，自動找到兩者都滿足的解。

### 資訊流

```
SplitPlacementRequest
    │
    ▼
solve_split_placement()
    │
    ├─► ResourceSplitter.build()
    │       建立 count_var / active_var
    │       加入 coverage constraints
    │       回傳 synthetic VMs
    │
    ├─► 組合 explicit VMs + synthetic VMs
    │       → PlacementRequest
    │
    ├─► VMPlacementSolver(request, model=shared_model, active_vars=...)
    │       建立 assign variables
    │       加入 capacity / anti-affinity constraints
    │       active_var 連結 split 與 placement
    │
    ├─► solver.solve()  ← 一次搜尋完成所有決策
    │
    └─► 從 cp_solver 讀取 count_var 值
            → SplitDecision list
            → PlacementAssignment list
```

---

## 3. 新增 API Endpoint

### `POST /v1/placement/split-and-solve`

**Request**：`SplitPlacementRequest`

```json
{
  "requirements": [
    {
      "total_resources": { "cpu_cores": 32, "memory_mib": 128000, "storage_gb": 800, "gpu_count": 0 },
      "node_role": "worker",
      "cluster_id": "cluster-1",
      "ip_type": "routable",
      "vm_specs": [
        { "cpu_cores": 8, "memory_mib": 32000, "storage_gb": 200, "gpu_count": 0 }
      ],
      "min_total_vms": null,
      "max_total_vms": null
    }
  ],
  "vms": [],
  "baremetals": [
    {
      "id": "bm-0",
      "total_capacity": { "cpu_cores": 64, "memory_mib": 256000, "storage_gb": 2000, "gpu_count": 0 },
      "topology": { "ag": "ag-0" }
    }
  ],
  "anti_affinity_rules": [],
  "config": {
    "vm_specs": [],
    "w_resource_waste": 5
  }
}
```

**Response**：`SplitPlacementResult`

```json
{
  "success": true,
  "split_decisions": [
    { "node_role": "worker", "vm_spec": { "cpu_cores": 8, "memory_mib": 32000, "storage_gb": 200, "gpu_count": 0 }, "count": 4 }
  ],
  "assignments": [
    { "vm_id": "split-r0-s0-0", "baremetal_id": "bm-0", "ag": "ag-0" },
    { "vm_id": "split-r0-s0-1", "baremetal_id": "bm-0", "ag": "ag-0" },
    { "vm_id": "split-r0-s0-2", "baremetal_id": "bm-0", "ag": "ag-0" },
    { "vm_id": "split-r0-s0-3", "baremetal_id": "bm-0", "ag": "ag-0" }
  ],
  "solver_status": "OPTIMAL",
  "solve_time_seconds": 0.12,
  "unplaced_vms": [],
  "diagnostics": {}
}
```

**`split_decisions` 的語意**：告訴 Go scheduler「要建幾台哪種規格的 VM」，scheduler 再根據這個資訊去 Kubernetes 建立真實的 VM，然後使用 `assignments` 裡的 `vm_id → baremetal_id` 對應執行 placement。

---

## 4. 檔案職責

### 新增檔案

#### `app/splitter.py` — `ResourceSplitter`

唯一職責：**在共用 CpModel 上建立 split decision variables 和約束**。

不知道 placement 的細節，只關心：
- 哪些 spec 能放進至少一台 BM
- coverage constraint（Σ count × spec ≥ total）
- active_var 如何與 count_var 連結
- waste objective terms

#### `app/split_solver.py` — `solve_split_placement()`

唯一職責：**協調 splitter 和 solver 的初始化順序**，組合 PlacementRequest，注入 waste terms，呼叫 `solver.solve()`，最後從解中讀出 split decisions。

不含任何 CP-SAT 約束邏輯。

#### `tests/test_splitter.py`

18 個測試，涵蓋 8 個場景類別：Basic Split、Multi-Spec、BM Capacity Constraint、Per-Role Requirements、Anti-Affinity、Mixed Mode、Config Fallback、HTTP Endpoint。

### 修改的既有檔案

| 檔案 | 改動方向 |
|------|---------|
| `app/models.py` | 新增 4 個 model；`slot_tshirt_sizes` → `vm_specs`；新增 `w_resource_waste` |
| `app/solver.py` | 擴充 `__init__`；修改 constraint / objective / extract 方法以支援 active_vars |
| `app/server.py` | 新增 endpoint；加入本機 Swagger UI（不依賴 CDN）|
| `tests/test_solver.py` | `slot_tshirt_sizes` → `vm_specs` |

---

## 5. 資料模型

### 新增模型（`app/models.py`）

```python
class ResourceRequirement(BaseModel):
    total_resources: Resources          # 這個 role 的總資源需求
    node_role: NodeRole = WORKER
    cluster_id: str = ""
    ip_type: str = ""
    vm_specs: list[Resources] | None = None   # None → 使用 config.vm_specs
    min_total_vms: int | None = None
    max_total_vms: int | None = None

class SplitPlacementRequest(BaseModel):
    requirements: list[ResourceRequirement]
    vms: list[VM] = []              # 可同時混入既有 explicit VMs
    baremetals: list[Baremetal]
    anti_affinity_rules: list[AntiAffinityRule] = []
    config: SolverConfig = SolverConfig()

class SplitDecision(BaseModel):
    node_role: NodeRole
    vm_spec: Resources
    count: int

class SplitPlacementResult(BaseModel):
    success: bool
    assignments: list[PlacementAssignment] = []
    split_decisions: list[SplitDecision] = []
    solver_status: str = ""
    solve_time_seconds: float = 0.0
    unplaced_vms: list[str] = []
    diagnostics: dict[str, Any] = {}
```

### `SolverConfig` 欄位變更

```python
# 舊
slot_tshirt_sizes: list[Resources] = []

# 新
vm_specs: list[Resources] = []        # 雙用途：slot score + splitter spec pool
w_resource_waste: int = 5             # 新增：懲罰 over-allocation
```

> ⚠️ **Breaking change**：Go scheduler 若在 `config` 帶 `slot_tshirt_sizes`，需改為 `vm_specs`。

---

## 6. CP-SAT 模型結構

### Variables

```
count_var[req_idx, spec_idx]   IntVar(0..upper)
  意義：requirement req_idx 選 spec spec_idx 的數量

active_var["split-r{i}-s{j}-{k}"]   BoolVar
  意義：第 req_idx 個 requirement 的 spec j 的第 k 個 slot 是否啟用

assign[(vm_id, bm_id)]   BoolVar  (由 VMPlacementSolver 建立)
  意義：vm_id 是否放在 bm_id 上
```

### Constraints（splitter 建立）

**1. Coverage constraint**（每個資源維度）
```
∀ field ∈ {cpu, mem, disk, gpu}:
  Σ_s  count[req, s] × spec[s].field  ≥  total_resources.field
```

**2. Link count → active slots**
```
∀ spec s:
  Σ_k  active["split-r{req}-s{spec}-{k}"]  ==  count[req, spec]
```

**3. Symmetry breaking**
```
∀ k < upper-1:
  active[k] ≥ active[k+1]
```

**4. VM count bounds**（可選）
```
Σ_s count[req, s] ≥ min_total_vms   (if set)
Σ_s count[req, s] ≤ max_total_vms   (if set)
```

### Constraints（solver 建立，擴充支援 active_var）

**原有邏輯**（explicit VM）：
```
sum(assign[vm, *]) == 1          (或 ≤ 1 for partial placement)
```

**新增邏輯**（synthetic VM，有 active_var）：
```
sum(assign[vm, *]) == active_var[vm]
```
當 `active_var=0`，等式右側為 0，即強制 VM 不被放置；當 `active_var=1`，必須恰好放在一台 BM 上。這個約束是 split 決策與 placement 決策的**橋接點**。

### Objective（Minimize）

```
w_consolidation × Σ bm_used
+ w_headroom    × Σ headroom_penalties
- w_slot_score  × Σ slot_scores
+ w_resource_waste × Σ waste_terms
```

其中 `waste_terms` 由 splitter 提供：
```
waste = (Σ_s count[s] × spec[s].field) − total_demand.field
```

### Upper bound 計算

每個 (requirement, spec) 組合的 slot 數上限：
```python
upper = max(
    ceil(total.cpu  / spec.cpu),
    ceil(total.mem  / spec.mem),
    ceil(total.disk / spec.disk),
    ceil(total.gpu  / spec.gpu),
)
upper = max(upper, min_total_vms)   # 確保 min_vms 可達
upper = min(upper, max_total_vms)   # 尊重 max_vms（如有）
```

---

## 7. 重要設計決策與取捨

### 決策 A：Symmetry breaking 必要性

**問題**：若某個 spec 的 upper bound 是 5，但解只需要 3 台，則 `active[0..2]=1, active[3..4]=0` 和任意其他組合（如 `active[0,1,3]=1`）在資源上等價，但 CP-SAT 會把它們當作不同解去搜尋。

**解法**：強制 `active[k] ≥ active[k+1]`，等同規定「有效的 slot 一定從 index 0 開始連續排列」，將等價解的數量從 $\binom{5}{3}=10$ 降為 1。

**影響**：synthetic VM 的 id 帶有 slot index（`split-r0-s0-2`），但這是 internal 識別符，Go scheduler 不應依賴其格式。

---

### 決策 B：Waste 是 soft penalty 而非 hard constraint

**問題**：若把 waste == 0 設為 hard constraint，任何不能整除的情境都會變 INFEASIBLE（例如需要 70 CPU，spec 是 8 CPU，最少需 9 台共 72 CPU，有 2 CPU waste）。

**解法**：waste 進入 objective 的 minimize 項（soft），允許 over-allocation 但懲罰它。`w_resource_waste` 預設值 5，介於 `w_headroom`（8）和 0 之間，可依需求調整。

**調整建議**：
- 想讓 solver 更積極減少 waste → 提高 `w_resource_waste`
- 資源緊張環境下想更優先放置成功率 → 降低 `w_resource_waste`

---

### 決策 C：Spec pool 的兩層 fallback

```
ResourceRequirement.vm_specs != None  →  使用 requirement 層的 specs
                              == None  →  使用 config.vm_specs
```

**設計理由**：多數情況下 cluster 裡所有 role 使用同一組 t-shirt sizes，放在 `config.vm_specs` 統一管理。但若 master 和 worker 需要不同規格，可在各自的 `ResourceRequirement.vm_specs` 個別指定。

**注意**：`config.vm_specs` 同時也是 slot score objective 的參考規格（原 `slot_tshirt_sizes` 的用途），兩個功能共用同一個欄位。若日後需要區分，建議拆成獨立欄位。

---

### 決策 D：`_last_cp_solver` 的傳遞方式

**問題**：`split_solver.py` 在 solve 完成後需要讀取 `count_var` 的值，但 `CpSolver` 物件只存在於 `VMPlacementSolver.solve()` 的 local scope。

**解法**：在 `_extract_solution` 被呼叫前，將 solver 存到 `self._last_cp_solver`，orchestrator 再用 `getattr(solver_instance, "_last_cp_solver", None)` 取得。

**取捨**：這是一個輕量的 side-channel，替代方案是修改 `PlacementResult` 帶出 solver 物件，但會讓所有 caller 的 return type 變複雜，影響面太大。

---

### 決策 E：不支援 topology affinity

這個 branch 刻意以 commit `251bbe3`（不含 cross-cluster constraints）為基底。Topology affinity 與 splitter 是**獨立**的功能，放在同一個 branch 會使 code review 困難、測試範疇模糊。

若日後需要合併，只需：
1. 在 `SplitPlacementRequest` 加入 `existing_vms` 和 `topology_rules` 欄位
2. 在 `split_solver.py` 的 `PlacementRequest(...)` 建構時帶入這兩個欄位

---

## 8. 對既有程式的改動

### `app/solver.py`

所有改動都**向後相容**，不傳 `model` / `active_vars` 時行為與原來完全一致。

```python
# __init__ 新增 keyword-only 參數
def __init__(self, request, *, model=None, active_vars=None):
    ...
    self.active_vars = active_vars or {}
    self.model = model if model is not None else cp_model.CpModel()

# _add_one_bm_per_vm_constraint 新增 active_var 分支
active_var = self.active_vars.get(vm.id)
if active_var is not None:
    self.model.add(sum(vm_vars) == active_var)  # ← 新增
elif self.config.allow_partial_placement:
    self.model.add(sum(vm_vars) <= 1)           # ← 原有
else:
    self.model.add(sum(vm_vars) == 1)           # ← 原有

# _add_objective 新增 waste penalty（透過 getattr 隔離）
waste_terms = getattr(self, "_splitter_waste_terms", [])
if waste_terms and self.config.w_resource_waste > 0:
    terms.append(self.config.w_resource_waste * sum(waste_terms))

# _extract_solution 新增跳過 inactive slots
active_var = self.active_vars.get(vm.id)
if active_var is not None and solver.value(active_var) == 0:
    continue  # 這個 slot 未被 splitter 啟用，不計入 unplaced
```

### `app/server.py`

新增本機 Swagger UI（避免無網路環境 `/docs` 空白）：

```python
_SWAGGER_STATIC_DIR = Path(swagger_ui_bundle.__file__).parent
api.mount("/swagger-static", StaticFiles(directory=str(_SWAGGER_STATIC_DIR)), ...)
```

---

## 9. 後續開發注意事項

### Go Scheduler 端需更新

1. **`config.slot_tshirt_sizes` → `config.vm_specs`**（breaking change）
2. Response 的 `assignments` 中，vm_id 以 `split-` 開頭的是 splitter 產生的 synthetic VM，scheduler 需根據 `split_decisions` 中的 spec 和 count 去 Kubernetes 建立真實 VM，再套用 assignments 做 placement
3. `split_decisions` 是 **scheduler 的行動指令**，`assignments` 是 **placement mapping**，兩者需要對應使用

### Synthetic VM ID 格式

```
split-r{req_idx}-s{spec_idx}-{k}
```
- `req_idx`：`requirements` 陣列的 index（0-based）
- `spec_idx`：該 requirement 的 `vm_specs` 陣列 index（0-based）
- `k`：同一 spec 的第幾個 slot（0-based）

**不要在 scheduler 端 parse 這個格式**，它是 internal identifier，未來版本可能變更。

### 效能考量

| 規模 | 預期表現 |
|------|---------|
| 2–3 requirements × 1–2 specs × 5–10 BMs | < 1 秒 |
| 5 requirements × 3 specs × 50 BMs | < 5 秒（`max_solve_time_seconds` 預設 30 秒） |
| 大量 specs（10+）或超大 upper bound | 搜尋空間膨脹，考慮增加 `w_resource_waste` 促使 solver 收斂 |

Symmetry breaking 對同 spec 多台的情境有顯著幫助；若仍太慢，可考慮降低 `max_solve_time_seconds` 並接受 FEASIBLE 而非 OPTIMAL 解。

### 擴充 split 功能時的入口點

| 想擴充的功能 | 入口檔案 |
|-------------|---------|
| 新增 split constraint（例如每個 spec 至少 2 台）| `splitter.py::_build_requirement()` |
| 新增 split objective term | `splitter.py::build_waste_objective_terms()` |
| 讓 splitter 知道 topology | `split_solver.py` + `SplitPlacementRequest` 加欄位 |
| 限制 spec pool 來源（例如只允許 config 中的 spec）| `splitter.py::_resolve_specs()` |

---

## 10. 測試覆蓋

`tests/test_splitter.py` 共 18 個測試：

| 類別 | 測試 | 驗證重點 |
|------|------|---------|
| `TestBasicSplit` | 4 個 | 整除、非整除、min_vms、max_vms infeasible |
| `TestMultiSpecSplit` | 2 個 | waste minimization、混合 spec |
| `TestBMCapacityConstraint` | 2 個 | 過大 spec 被過濾、容量不足 infeasible |
| `TestPerRoleRequirements` | 1 個 | worker + master 分開 split |
| `TestSplitWithAntiAffinity` | 1 個 | synthetic VM 參與 auto anti-affinity |
| `TestMixedMode` | 1 個 | explicit VM + synthetic VM 共存 |
| `TestConfigSpecsFallback` | 2 個 | config.vm_specs fallback、無 spec → infeasible |
| `TestSplitEndpoint` | 1 個 | HTTP endpoint smoke test |

執行：
```bash
pytest tests/test_splitter.py -v
pytest tests/ -v  # 含既有 40 個測試，共 54 個
```

---

## 11. 使用範例

### curl 範例：32 CPU worker 需求，自動選 8 CPU VM

```bash
curl -s -X POST http://localhost:50051/v1/placement/split-and-solve \
  -H 'Content-Type: application/json' \
  -d '{
    "requirements": [{
      "total_resources": {"cpu_cores": 32, "memory_mib": 128000, "storage_gb": 800, "gpu_count": 0},
      "node_role": "worker",
      "cluster_id": "cluster-1",
      "ip_type": "routable",
      "vm_specs": [
        {"cpu_cores": 8, "memory_mib": 32000, "storage_gb": 200, "gpu_count": 0}
      ]
    }],
    "baremetals": [
      {"id": "bm-0", "total_capacity": {"cpu_cores": 64, "memory_mib": 256000, "storage_gb": 2000, "gpu_count": 0}, "topology": {"ag": "ag-0"}},
      {"id": "bm-1", "total_capacity": {"cpu_cores": 64, "memory_mib": 256000, "storage_gb": 2000, "gpu_count": 0}, "topology": {"ag": "ag-1"}}
    ],
    "config": {"auto_generate_anti_affinity": false}
  }' | python -m json.tool
```

預期回應：`split_decisions: [{count: 4, vm_spec: {cpu_cores: 8, ...}}]`，`assignments` 4 筆。

### curl 範例：使用 config.vm_specs 讓 solver 自選規格

```bash
curl -s -X POST http://localhost:50051/v1/placement/split-and-solve \
  -H 'Content-Type: application/json' \
  -d '{
    "requirements": [{
      "total_resources": {"cpu_cores": 32, "memory_mib": 128000, "storage_gb": 800, "gpu_count": 0},
      "node_role": "worker"
    }],
    "baremetals": [
      {"id": "bm-0", "total_capacity": {"cpu_cores": 64, "memory_mib": 256000, "storage_gb": 2000, "gpu_count": 0}, "topology": {"ag": "ag-0"}}
    ],
    "config": {
      "vm_specs": [
        {"cpu_cores": 4,  "memory_mib": 16000, "storage_gb": 100, "gpu_count": 0},
        {"cpu_cores": 8,  "memory_mib": 32000, "storage_gb": 200, "gpu_count": 0},
        {"cpu_cores": 16, "memory_mib": 64000, "storage_gb": 400, "gpu_count": 0}
      ],
      "w_resource_waste": 10,
      "auto_generate_anti_affinity": false
    }
  }' | python -m json.tool
```

Solver 會在 3 種規格中選出 waste 最小的組合。
