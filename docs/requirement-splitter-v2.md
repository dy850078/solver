# Enhancement Proposal: Resource Splitter (Joint Split + Placement Optimization)

> **作者**: Claude Opus 4.6
> **日期**: 2026-04-13
> **基底分支**: `zs/implement-splitter`（基底 commit `251bbe3`）

---

## Summary

Resource Splitter 將原本由 Go scheduler 負責的 VM 規格拆分決策整合進 CP-SAT solver，與 placement 決策共用同一個數學模型一次求解。Go scheduler 只需送出「我要 32 CPU 的 worker」，solver 同時決定「用幾台什麼規格的 VM」以及「每台 VM 放在哪台 BM 上」，消除 split/placement 二階段反覆重試的問題。

---

## Goals / Non-Goals

### Goals

- **一次求解**：split（VM 數量 × 規格）與 placement（VM → BM）在同一個 CP-SAT model 中聯合最佳化
- **向後相容**：不傳 `model` / `active_vars` 時，`VMPlacementSolver` 行為不變
- **最小 waste**：在滿足資源需求的前提下，減少 over-allocation（soft penalty）
- **可配置 spec pool**：支援 requirement 層級與 config 層級的 spec fallback
- **新增 API endpoint**：`POST /v1/placement/split-and-solve`

### Non-Goals

- **不處理 topology affinity**：與 splitter 功能獨立，避免 review 範疇混淆
- **不修改 Go scheduler 的 split 邏輯**：scheduler 端的遷移屬後續工作
- **不支援異質 spec 混合在同一台 VM**：每台 VM 對應單一 spec

---

## Current State & Problem

### 現況

原有的 `/v1/placement/solve` 要求 Go scheduler **在送出請求前自行決定每台 VM 的規格與數量**，solver 只負責 placement。

```
Go scheduler                          Python solver
     │                                      │
     │  「4 台 × 8 CPU VM，幫我放」          │
     │──── POST /v1/placement/solve ──────▶│
     │                                      │  嘗試放置 4 台 VM
     │◀── INFEASIBLE ──────────────────────│  (anti-affinity 限制只有 3 個 AG)
     │                                      │
     │  「那改 2 台 × 16 CPU？」              │  ← scheduler 不知道最佳解
     │──── POST /v1/placement/solve ──────▶│
     │◀── success ─────────────────────────│
```

### 痛點

| 問題 | 影響 |
|------|------|
| Split 與 placement 是相互依賴的決策，卻被強迫序列執行 | Scheduler 猜錯規格就要重試 |
| Scheduler 沒有 BM capacity / AG 分布的全貌 | 無法系統性地找到最優分法 |
| 即使資源上合法的 split，也可能因 anti-affinity 而 placement 失敗 | 重試邏輯複雜且脆弱 |

### 具體失敗情境

假設 master role 需要 12 CPU，scheduler 送出明確的 VM 規格給 `/v1/placement/solve`，且設定了 explicit anti-affinity rule `max_per_ag=1`：

| 情境 | Scheduler 決策 | 結果 |
|------|----------------|------|
| BM 分布：2 個 AG | 送 3 台 × 4 CPU | INFEASIBLE — 3 台要放 3 個 AG，但只有 2 個 |
| BM 分布：2 個 AG | 改送 2 台 × 6 CPU | 成功 — 2 台各放 1 個 AG |

Scheduler 必須自己知道「只有 2 個 AG 所以最多放 2 台 VM」，但它缺乏 BM topology 的完整資訊，只能盲目重試。

---

## Proposed Design

### High-level 架構

核心思想：將 split 決策變數和 placement 決策變數放入**同一個 CpModel**，一次 solve。

```
SplitPlacementRequest
    │
    ▼
solve_split_placement()         ← 協調者 (split_solver.py)
    │
    ├─► ResourceSplitter.build()     ← 建立 split 變數 + 約束
    │       回傳 synthetic VMs
    │
    ├─► 合併 explicit VMs + synthetic VMs → PlacementRequest
    │
    ├─► VMPlacementSolver(shared_model, active_vars)
    │       建立 placement 變數 + 約束
    │
    ├─► model.solve()               ← 一次求解所有決策
    │
    └─► 讀取解 → SplitDecision[] + PlacementAssignment[]
```

### 檔案職責

| 檔案 | 職責 | 不負責 |
|------|------|--------|
| `app/splitter.py` | 在共用 CpModel 上建立 split variables + constraints | 不知道 placement 細節 |
| `app/split_solver.py` | 協調 splitter/solver 初始化、組合 request、讀取結果 | 不含任何 CP-SAT 約束邏輯 |
| `app/solver.py` | 建立 placement variables + constraints，支援 active_var | 不知道 split 邏輯 |

### 新增 / 修改檔案

| 檔案 | 動作 | 改動方向 |
|------|------|---------|
| `app/splitter.py` | 新增 | ResourceSplitter class |
| `app/split_solver.py` | 新增 | solve_split_placement() orchestrator |
| `app/models.py` | 修改 | +4 model；`slot_tshirt_sizes` → `vm_specs`；+`w_resource_waste` |
| `app/solver.py` | 修改 | `__init__` 接受 shared model/active_vars；constraint/objective/extract 支援 active_var |
| `app/server.py` | 修改 | +endpoint `/v1/placement/split-and-solve` |

---

### 核心資料流 / API

#### `POST /v1/placement/split-and-solve`

**Request** — `SplitPlacementRequest`

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
  "config": { "vm_specs": [], "w_resource_waste": 5 }
}
```

**Response** — `SplitPlacementResult`

```json
{
  "success": true,
  "split_decisions": [
    { "node_role": "worker", "vm_spec": { "cpu_cores": 8, ... }, "count": 4 }
  ],
  "assignments": [
    { "vm_id": "split-r0-s0-0", "baremetal_id": "bm-0", "ag": "ag-0" },
    { "vm_id": "split-r0-s0-1", "baremetal_id": "bm-0", "ag": "ag-0" },
    ...
  ],
  "solver_status": "OPTIMAL",
  "solve_time_seconds": 0.12
}
```

**語意**：`split_decisions` 告訴 scheduler 要建幾台什麼規格的 VM；`assignments` 告訴 scheduler 每台 VM 放哪台 BM。

#### 資料模型

```python
class ResourceRequirement(BaseModel):
    total_resources: Resources          # 這個 role 的總資源需求
    node_role: NodeRole = WORKER
    cluster_id: str = ""
    ip_type: str = ""
    vm_specs: list[Resources] | None    # None → fallback 到 config.vm_specs
    min_total_vms: int | None = None
    max_total_vms: int | None = None

class SplitPlacementRequest(BaseModel):
    requirements: list[ResourceRequirement]
    vms: list[VM] = []                  # 可混入 explicit VMs
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

`SolverConfig` 欄位變更：
- `slot_tshirt_sizes` → `vm_specs`（**breaking change**，Go scheduler 需同步更新）
- 新增 `w_resource_waste: int = 5`（waste penalty 權重）

---

### CP-SAT 模型：變數、約束、目標函數

以下透過一個**貫穿全文的範例**來說明模型的每一個環節。

#### 範例情境

```
Requirement: worker role 需要 32 CPU
可用 VM Specs:
  spec-0: 8 CPU
  spec-1: 16 CPU
Baremetals:
  bm-0 (ag-0): 可用 64 CPU
  bm-1 (ag-1): 可用 64 CPU
Anti-affinity: explicit rule, max_per_ag = 1
```

---

#### Step 1：計算 Upper Bound

每個 (requirement, spec) 組合的 slot 數上限，取**各資源維度 ceil 的最大值**：

```python
upper = max(ceil(total.field / spec.field) for field in [cpu, mem, disk, gpu] if spec.field > 0)
```

套用範例（簡化只看 CPU 維度）：

| Requirement | Spec | 計算 | Upper Bound |
|-------------|------|------|-------------|
| 32 CPU | spec-0 (8 CPU) | ceil(32/8) = 4 | **4** |
| 32 CPU | spec-1 (16 CPU) | ceil(32/16) = 2 | **2** |

若有設定 `min_total_vms` / `max_total_vms`，會進一步調整 upper bound：
- `upper = max(upper, min_total_vms)` — 確保 min 可達
- `upper = min(upper, max_total_vms)` — 尊重 max 上限

---

#### Step 2：建立變數 (Variables)

**count_var** — 每種 spec 用幾台（IntVar）

| 變數名稱 | 型別 | 值域 | 語意 |
|----------|------|------|------|
| `count_r0_s0` | IntVar | [0, 4] | requirement-0 選 spec-0 (8CPU) 幾台？ |
| `count_r0_s1` | IntVar | [0, 2] | requirement-0 選 spec-1 (16CPU) 幾台？ |

**active_var** — 每個 synthetic slot 是否啟用（BoolVar）

根據 upper bound，spec-0 產生 4 個 slot，spec-1 產生 2 個 slot，共 6 個 synthetic VM：

| VM ID | active_var | 對應 spec | 語意 |
|-------|-----------|----------|------|
| `split-r0-s0-0` | BoolVar | spec-0 (8CPU) | slot 0 是否啟用？ |
| `split-r0-s0-1` | BoolVar | spec-0 (8CPU) | slot 1 是否啟用？ |
| `split-r0-s0-2` | BoolVar | spec-0 (8CPU) | slot 2 是否啟用？ |
| `split-r0-s0-3` | BoolVar | spec-0 (8CPU) | slot 3 是否啟用？ |
| `split-r0-s1-0` | BoolVar | spec-1 (16CPU) | slot 0 是否啟用？ |
| `split-r0-s1-1` | BoolVar | spec-1 (16CPU) | slot 1 是否啟用？ |

**assign** — 每台 VM 放哪台 BM（BoolVar，由 VMPlacementSolver 建立）

每個 synthetic VM × 每台 eligible BM 一個變數：

| | bm-0 | bm-1 |
|--|------|------|
| `split-r0-s0-0` | BoolVar | BoolVar |
| `split-r0-s0-1` | BoolVar | BoolVar |
| `split-r0-s0-2` | BoolVar | BoolVar |
| `split-r0-s0-3` | BoolVar | BoolVar |
| `split-r0-s1-0` | BoolVar | BoolVar |
| `split-r0-s1-1` | BoolVar | BoolVar |

> 這三層變數構成了模型的核心：`count_var` 控制「選幾台」，`active_var` 決定「哪些 slot 啟用」，`assign` 決定「啟用的 VM 放哪裡」。

---

#### 為什麼需要 `count_var` 和 `active_var` 兩層？

**問題**：解題前不知道 `count_var` 的值。`count_var = IntVar(0..4)` 代表「可能用 0 到 4 台」，但我們必須在建模時就把所有可能的 VM 交給 placement solver。

**解法**：預建 upper bound 數量的 VM slot（每個帶一個 BoolVar），然後用約束把它們與 count_var 綁定。solver 解完後，未啟用的 slot 自動不被放置。

以 spec-0 為例（upper=4），假設 solver 解出 `count_r0_s0 = 2`：

| slot | active_var 解值 | 行為 |
|------|---------------|------|
| `split-r0-s0-0` | **1** (active) | 必須放到某台 BM |
| `split-r0-s0-1` | **1** (active) | 必須放到某台 BM |
| `split-r0-s0-2` | 0 (inactive) | 強制不放置 |
| `split-r0-s0-3` | 0 (inactive) | 強制不放置 |
| **sum** | **2** | == count_r0_s0 ✓ |

**兩層變數的分工**：

| | `count_var` | `active_var` |
|---|---|---|
| 層次 | Splitter 決策層 | VM 槽位層 |
| 型別 | IntVar（整數） | BoolVar（0 或 1） |
| 回答 | 用幾台 spec X？ | 第 k 個 slot 要不要放？ |
| 消費者 | Coverage constraint | VMPlacementSolver（assign 約束） |

`active_var` 是 Splitter 與 VMPlacementSolver 之間的**橋接介面**。

---

#### Step 3：建立約束 (Constraints)

##### Constraint 1：Coverage — 資源必須覆蓋需求

```
∀ field ∈ {cpu, mem, disk, gpu}:
  Σ_s count[req, s] × spec[s].field ≥ total_resources.field
```

套用範例（CPU 維度）：

```
count_r0_s0 × 8  +  count_r0_s1 × 16  ≥  32
```

| count_r0_s0 | count_r0_s1 | 總 CPU | 滿足 ≥ 32？ |
|:-----------:|:-----------:|:------:|:----------:|
| 4 | 0 | 32 | ✓ |
| 2 | 1 | 32 | ✓ |
| 0 | 2 | 32 | ✓ |
| 3 | 0 | 24 | ✗ |
| 1 | 1 | 24 | ✗ |

Coverage constraint 保證 solver 只考慮資源充足的組合。

##### Constraint 2：Link — count 與 active slot 的連結

```
∀ spec s:
  Σ_k active["split-r{req}-s{s}-{k}"] == count[req, s]
```

套用範例：

```
active[r0-s0-0] + active[r0-s0-1] + active[r0-s0-2] + active[r0-s0-3] == count_r0_s0
active[r0-s1-0] + active[r0-s1-1]                                      == count_r0_s1
```

##### Constraint 3：Symmetry Breaking — 消除等價解

```
∀ k < upper-1:
  active[k] ≥ active[k+1]
```

若 `count_r0_s0 = 2`，沒有 symmetry breaking 時，以下 $\binom{4}{2}=6$ 種組合都是等價解：

| slot 0 | slot 1 | slot 2 | slot 3 | 等價？ |
|:------:|:------:|:------:|:------:|:-----:|
| 1 | 1 | 0 | 0 | ✓ |
| 1 | 0 | 1 | 0 | ✓ |
| 1 | 0 | 0 | 1 | ✓ |
| 0 | 1 | 1 | 0 | ✓ |
| 0 | 1 | 0 | 1 | ✓ |
| 0 | 0 | 1 | 1 | ✓ |

加入 `active[k] ≥ active[k+1]` 後，唯一合法的排列是 `[1, 1, 0, 0]`。搜尋空間從 6 降為 1，大幅加速求解。

##### Constraint 4：VM Count Bounds（可選）

```
Σ_s count[req, s] ≥ min_total_vms   (if set)
Σ_s count[req, s] ≤ max_total_vms   (if set)
```

##### Constraint 5：Placement — active_var 連結 assign 變數

**原有邏輯**（explicit VM）：
```
sum(assign[vm, *]) == 1    （必須放置）
```

**新增邏輯**（synthetic VM）：
```
sum(assign[vm, *]) == active_var[vm]
```

| active_var | assign 約束 | 效果 |
|:----------:|:----------:|------|
| 1 | `sum(assign) == 1` | 必須恰好放在一台 BM |
| 0 | `sum(assign) == 0` | 強制不放置，跳過 |

這個約束是 split 與 placement 的**橋接點**：splitter 決定啟用哪些 slot，placement solver 決定啟用的 VM 放在哪裡。

##### Constraint 6：Capacity + Anti-Affinity（由 VMPlacementSolver 處理）

Capacity checking 與原有 solver 邏輯相同，synthetic VM 完全參與。

Anti-affinity 在 splitter 場景中有兩種使用方式：

| 方式 | 適用情境 | 說明 |
|------|---------|------|
| **Explicit rules** | 所有情境 | Scheduler 送入固定的 `max_per_ag`，solver 在此約束下選擇 VM 數量 |
| **Auto anti-affinity** | 所有情境（含 VM 數量為決策變數） | Solver 自動依實際啟用數量計算 spreading |

Auto anti-affinity 對含 synthetic VM 的群組使用**動態約束**：

```
固定（explicit VM only）:  count_in_ag <= ceil(len(vm_ids) / num_ags)
動態（含 synthetic VM）:   count_in_ag * num_ags <= total_active + (num_ags - 1)
                            ≡ count_in_ag <= ceil(total_active / num_ags)
```

其中 `total_active = Σ active_var[synthetic] + count(explicit)`，是 CP-SAT 運算式。solver 會根據實際啟用的 VM 數量動態調整每個 AG 的容許上限，而非基於 upper bound（所有 synthetic slots）。當群組中全部是 explicit VM 時，動態公式退化為與固定公式等價。

---

#### Step 4：目標函數 (Objective)

```
Minimize:
    w_consolidation × Σ bm_used
  + w_headroom      × Σ headroom_penalties
  - w_slot_score    × Σ slot_scores
  + w_resource_waste × Σ waste_terms       ← 新增
```

**waste_terms** 由 splitter 提供，計算每個維度的 over-allocation：

```
waste[field] = Σ_s count[s] × spec[s].field − total_demand.field
```

套用範例（solver 選了 `count_r0_s0=4, count_r0_s1=0`）：

| 維度 | 已分配 | 需求 | Waste |
|------|--------|------|-------|
| CPU | 4 × 8 = 32 | 32 | **0** |
| Memory | 4 × 32000 = 128000 | 128000 | **0** |
| Storage | 4 × 200 = 800 | 800 | **0** |

完美整除，waste = 0。但若需求是 30 CPU：

| 維度 | 已分配 | 需求 | Waste |
|------|--------|------|-------|
| CPU | 4 × 8 = 32 | 30 | **2** |

Waste 進入 objective 被懲罰。`w_resource_waste` 越高，solver 越積極選低 waste 的組合。

> **設計選擇**：Waste 是 **soft penalty** 而非 hard constraint。若強制 waste == 0，任何不能整除的情境都會 INFEASIBLE（例如需要 30 CPU，spec 8 CPU → 最少 4 台 = 32 CPU，必有 2 CPU waste）。

---

#### 完整求解過程演繹

回到範例情境，加上 explicit anti-affinity rule `max_per_ag=1`（scheduler 明確要求每個 AG 最多放 1 台）：

```
需求: 32 CPU worker
Spec: 8 CPU, 16 CPU
BM: bm-0(ag-0, 64CPU), bm-1(ag-1, 64CPU)
Anti-affinity: explicit rule, max_per_ag = 1
```

**Solver 搜尋空間**（滿足 coverage 的組合）：

| 組合 | count_s0 (8CPU) | count_s1 (16CPU) | 總 VM 數 | Anti-affinity 可行？ | Waste (CPU) |
|------|:---------------:|:----------------:|:--------:|:-------------------:|:-----------:|
| A | 4 | 0 | 4 | ✗ (4 VM, 2 AG) | 0 |
| B | 2 | 1 | 3 | ✗ (3 VM, 2 AG) | 0 |
| C | 0 | 2 | 2 | ✓ (2 VM, 2 AG) | 0 |

- 組合 A/B 因為 VM 數 > AG 數，anti-affinity 約束 infeasible
- 組合 C 恰好 2 台 VM 放 2 個 AG，**feasible 且 waste = 0**

**Solver 選擇組合 C**：

| 變數 | 解值 |
|------|------|
| `count_r0_s0` | 0 |
| `count_r0_s1` | 2 |
| `active[r0-s1-0]` | 1 |
| `active[r0-s1-1]` | 1 |
| `assign[r0-s1-0, bm-0]` | 1 |
| `assign[r0-s1-1, bm-1]` | 1 |

**輸出**：

```json
{
  "split_decisions": [{ "node_role": "worker", "vm_spec": { "cpu_cores": 16 }, "count": 2 }],
  "assignments": [
    { "vm_id": "split-r0-s1-0", "baremetal_id": "bm-0", "ag": "ag-0" },
    { "vm_id": "split-r0-s1-1", "baremetal_id": "bm-1", "ag": "ag-1" }
  ]
}
```

> 如果是原有的二階段流程，scheduler 可能先嘗試 4 台 × 8 CPU → INFEASIBLE → 再嘗試 2 台 × 16 CPU → 成功。Joint optimization 一步到位。

---

### 關鍵設計決策

#### 決策 A：Symmetry Breaking 必要性

**問題**：upper bound 為 N 但只需 K 台時，$\binom{N}{K}$ 種 slot 排列方式在資源上等價，CP-SAT 會逐一搜尋。

**解法**：強制 `active[k] ≥ active[k+1]`，等同規定啟用的 slot 從 index 0 連續排列，將等價解壓縮為 1 種。

**影響**：synthetic VM 的 ID 帶 slot index（`split-r0-s0-2`），但這是 internal identifier，Go scheduler 不應依賴其格式。

#### 決策 B：Waste 是 Soft Penalty

**問題**：hard constraint `waste == 0` 導致非整除情境一律 INFEASIBLE。

**解法**：waste 進入 objective minimize 項。

**調整建議**：
- 想減少 waste → 提高 `w_resource_waste`（預設 5）
- 資源緊張、優先放置成功率 → 降低 `w_resource_waste`

#### 決策 C：Spec Pool 兩層 Fallback

```
requirement.vm_specs != None → 使用 requirement 層的 specs
                    == None → 使用 config.vm_specs
```

多數場景 cluster 共用同組 t-shirt sizes，放 `config.vm_specs`。若特定 role 需不同規格，在 `ResourceRequirement.vm_specs` 個別指定。

> `config.vm_specs` 同時是 slot score objective 的參考規格（原 `slot_tshirt_sizes` 用途），兩功能共用同一欄位。日後若需區分建議拆成獨立欄位。

#### 決策 D：`_last_cp_solver` 的傳遞方式

**問題**：`split_solver.py` 解完後需要讀取 `count_var` 值，但 `CpSolver` 物件只存在於 `VMPlacementSolver.solve()` 的 local scope。

**解法**：solve 前將 solver 存到 `self._last_cp_solver`，orchestrator 透過 `getattr` 取得。

**取捨**：輕量 side-channel。替代方案是讓 `PlacementResult` 帶出 solver 物件，但會讓所有 caller 的 return type 變複雜。

#### 決策 E：不支援 Topology Affinity

此分支以 commit `251bbe3`（不含 cross-cluster constraints）為基底。日後合併只需在 `SplitPlacementRequest` 加入 `existing_vms` 和 `topology_rules` 欄位。

---

### Spec 解析邏輯

1. 使用 requirement 層的 `vm_specs`；若為 None，fallback 到 `config.vm_specs`
2. 過濾掉無法 fit 進任何 BM 的 spec（若有 `candidate_baremetals` 則只檢查指定 BM）
3. 回傳可用 specs，不可用的在建模前即移除

---

### Synthetic VM ID 格式

```
split-r{req_idx}-s{spec_idx}-{k}
```

| 欄位 | 意義 |
|------|------|
| `req_idx` | `requirements` 陣列的 index（0-based） |
| `spec_idx` | 該 requirement 的 `vm_specs` 陣列 index（0-based） |
| `k` | 同一 spec 的第幾個 slot（0-based） |

**Go scheduler 不應 parse 此格式**，它是 internal identifier，未來版本可能變更。

---

### 對既有程式的改動

#### `app/solver.py`

所有改動**向後相容**，不傳 `model` / `active_vars` 時行為不變。

```python
# __init__ 新增 keyword-only 參數
def __init__(self, request, *, model=None, active_vars=None):
    self.active_vars = active_vars or {}
    self.model = model if model is not None else cp_model.CpModel()

# _add_one_bm_per_vm_constraint：active_var 分支
active_var = self.active_vars.get(vm.id)
if active_var is not None:
    self.model.add(sum(vm_vars) == active_var)   # 新增
elif self.config.allow_partial_placement:
    self.model.add(sum(vm_vars) <= 1)            # 原有
else:
    self.model.add(sum(vm_vars) == 1)            # 原有

# _add_objective：waste penalty
waste_terms = getattr(self, "_splitter_waste_terms", [])
if waste_terms and self.config.w_resource_waste > 0:
    terms.append(self.config.w_resource_waste * sum(waste_terms))

# _extract_solution：跳過 inactive slots
active_var = self.active_vars.get(vm.id)
if active_var is not None and solver.value(active_var) == 0:
    continue

# _resolve_anti_affinity_rules：synthetic VM 群組標記為動態
has_synthetic = any(vid in self.active_vars for vid in vm_ids)
if has_synthetic:
    rules.append(AntiAffinityRule(..., max_per_ag=0))  # sentinel
else:
    max_per_ag = math.ceil(len(vm_ids) / num_ags)
    rules.append(AntiAffinityRule(..., max_per_ag=max_per_ag))

# _add_anti_affinity_constraints：動態約束取代固定 max_per_ag
if use_dynamic:  # auto rule 含 synthetic VM
    total_active = sum(active_vars[synthetic]) + count(explicit)
    model.add(sum(vars_in_ag) * num_ags <= total_active + (num_ags - 1))
else:
    model.add(sum(vars_in_ag) <= rule.max_per_ag)
```

---

## Alternative & Trade-offs

### 替代方案：二階段 Split-then-Place

```
Phase 1: ResourceSplitter 獨立 solve → 輸出 split decisions
Phase 2: VMPlacementSolver 接收 concrete VMs → solve placement
```

**為何不選**：
- Phase 1 不知道 BM capacity / AG 分布 → 可能選出 placement infeasible 的分法
- 需要外部重試迴圈（Phase 2 失敗 → 調整 Phase 1 參數 → 重跑）
- 無法保證全域最優

### 替代方案：Go Scheduler 端自行窮舉 Split

Go scheduler 嘗試所有 (spec, count) 組合，逐一呼叫 `/v1/placement/solve`。

**為何不選**：
- 組合數指數成長（多 spec × 多維度）
- 每個組合一次 HTTP round-trip
- scheduler 缺乏 BM 容量資訊，無法有效剪枝

### 權衡總結

| | Joint (本方案) | 二階段 | Scheduler 窮舉 |
|--|:-:|:-:|:-:|
| 保證 feasible split | ✓ | ✗ | ✗ |
| 全域最優 | ✓ | ✗ | ✗（近似） |
| 實作複雜度 | 中 | 低 | 高（scheduler 端） |
| Solver 複雜度 | 較高 | 低 | 低 |
| HTTP round-trips | 1 | 2 | N |

---

## Risk & Mitigations

| 風險 | 嚴重度 | 機率 | 緩解策略 |
|------|:------:|:----:|---------|
| 大量 specs × 高 upper bound 導致搜尋空間膨脹 | 中 | 低 | Symmetry breaking + `w_resource_waste` 促進收斂；可降低 `max_solve_time_seconds` 接受 FEASIBLE 解 |
| `config.vm_specs` breaking change 導致 Go scheduler 錯誤 | 高 | 中 | 明確標記 breaking change；Go 端同步更新 `slot_tshirt_sizes` → `vm_specs` |
| `_last_cp_solver` side-channel 在未來重構時被遺忘 | 低 | 低 | 有明確 docstring + 測試覆蓋 |
| Synthetic VM ID 格式被 scheduler parse 導致耦合 | 中 | 低 | 文件明確標注不可依賴；日後可加 opaque prefix |

### 效能預期

| 規模 | 預期表現 |
|------|---------|
| 2–3 requirements × 1–2 specs × 5–10 BMs | < 1 秒 |
| 5 requirements × 3 specs × 50 BMs | < 5 秒 |
| 大量 specs (10+) 或超大 upper bound | 搜尋空間膨脹，考慮增加 `w_resource_waste` |

---

## Rollout Plan

### 上線方式

1. **Phase 1 — 部署 solver 側**
   - 合併 `zs/implement-splitter` 分支
   - `/v1/placement/solve` 行為不變（向後相容）
   - 新 endpoint `/v1/placement/split-and-solve` 就緒但 scheduler 尚未呼叫

2. **Phase 2 — Go scheduler 遷移**
   - `config.slot_tshirt_sizes` → `config.vm_specs`
   - 新流程呼叫 `/v1/placement/split-and-solve`，舊流程保留為 fallback
   - 逐步切換 cluster

3. **Phase 3 — 清理**
   - 移除 scheduler 端舊 split 邏輯
   - 驗證生產環境效能

### 回滾策略

- solver 側：revert 到合併前的 commit，`/v1/placement/solve` 不受影響
- scheduler 側：切回舊流程（呼叫 `/v1/placement/solve` + scheduler 端 split 邏輯）

---

## Testing

`tests/test_splitter.py` 共 21 個測試，涵蓋 8 個場景類別：

| 類別 | 測試數 | 驗證重點 |
|------|:------:|---------|
| `TestBasicSplit` | 4 | 整除、非整除、min_vms、max_vms infeasible |
| `TestMultiSpecSplit` | 2 | Waste minimization、混合 spec |
| `TestBMCapacityConstraint` | 2 | 過大 spec 被過濾、容量不足 infeasible |
| `TestPerRoleRequirements` | 1 | Worker + master 分開 split |
| `TestSplitWithAntiAffinity` | 3 | Fixed count spreading、動態 count spreading、mixed explicit+synthetic group |
| `TestMixedMode` | 1 | Explicit VM + synthetic VM 共存 |
| `TestConfigSpecsFallback` | 2 | config.vm_specs fallback、無 spec → infeasible |
| `TestSplitEndpoint` | 1 | HTTP endpoint smoke test |

執行：
```bash
pytest tests/test_splitter.py -v         # splitter 測試
pytest tests/ -v                         # 全部測試（含既有 47 個，共 68 個）
```

---

## Extending the Splitter

| 想擴充的功能 | 入口檔案 |
|-------------|---------|
| 新增 split constraint（例如每個 spec 至少 2 台） | `splitter.py::_build_requirement()` |
| 新增 split objective term | `splitter.py::build_waste_objective_terms()` |
| 讓 splitter 知道 topology | `split_solver.py` + `SplitPlacementRequest` 加欄位 |
| 限制 spec pool 來源 | `splitter.py::_resolve_specs()` |

---

## Open Questions

1. **`config.vm_specs` 雙用途是否需拆分？** — 目前同時做 slot score 參考 + splitter spec pool，日後是否需要獨立欄位？
2. **Waste penalty 權重如何調校？** — `w_resource_waste=5` 是初始值，生產環境可能需要依 workload 特性微調
3. **是否需要 per-spec count bounds？** — 目前只支援全 requirement 的 `min/max_total_vms`，是否需要 per-spec `min/max_count`？
4. **[已解決] Auto anti-affinity 在 VM 數量為決策變數時的精確度？** — 見 Decision Log。`_add_anti_affinity_constraints` 對含 synthetic VM 的 auto rule 使用動態約束，不再基於 upper bound

---

## Decision Log (Review 後補)

| Decision | Reason | Follow-ups |
|----------|--------|------------|
| Auto anti-affinity 對含 synthetic VM 的群組改用動態約束 `count_in_ag * N <= total_active + (N-1)` | 原本 `max_per_ag = ceil(upper_bound / num_ags)` 基於 slot 上限計算，VM 數量為決策變數時約束過度寬鬆 | 無 — explicit rules 不受影響，純 explicit VM 群組行為不變 |
