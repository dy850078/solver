# 需求拆分系統設計文件 (Requirement Splitting System)

> **版本**: 1.0
> **日期**: 2026-03-30
> **狀態**: 已實作

---

## 1. 背景與動機

### 1.1 現有系統

目前 VM Placement Solver 接收 Go scheduler 傳入的**明確 VM 清單**（每台 VM 有具體的 cpu/mem/storage 規格），透過 CP-SAT 求解器找到最佳的 VM → BM 放置方案。

```
Go Scheduler → [VM specs × count + BM 清單] → Solver → [VM→BM assignments]
```

### 1.2 新需求

未來使用者將改為提供**總資源需求量**（如「我需要 256 CPU、1TB RAM、10TB storage」），而非明確的 VM 規格與數量。系統需要自動將總需求拆分為 VM spec × count。

**核心挑戰**：不同 BM 有不同的 cpu:mem:storage 比例，不同的 VM 拆分方式會直接影響 BM 資源浪費程度。

例如：
- BM Type A: 64 CPU, 256GB RAM, 2TB storage（比例 1:4:32）
- BM Type B: 128 CPU, 512GB RAM, 4TB storage（比例 1:4:32）
- 使用者需求：128 CPU + 512GB RAM + 8TB storage

若拆成 2 × (64 CPU, 256GB, 4TB) → 完美利用 Type B
若拆成 4 × (32 CPU, 128GB, 2TB) → 可能造成 Type A 上的碎片浪費

### 1.3 設計目標

1. **全局最優**：拆分結果必須考慮 BM 的放置約束（anti-affinity、topology、容量限制）
2. **彈性**：支援按 role 分別指定需求、可混合明確 VM 與待拆分需求
3. **向後相容**：既有 `/v1/placement/solve` 完全不變
4. **無狀態**：所有資料由 Go scheduler 傳入

---

## 2. 設計決策

### 2.1 方案比較

| 方案 | 說明 | 優點 | 缺點 |
|------|------|------|------|
| **A. 獨立前處理** | 先拆分，再放置 | 開發簡單 | 拆分不考慮放置約束，可能無法放下 |
| **B. 直接改 solver** | 在 solver 內部同時做拆分 | 全局最優 | 破壞現有架構 |
| **C. 聯合模型 + 模組分離** ✅ | 新模組建立拆分變數，與 solver 共用 CpModel | 全局最優 + 程式碼分離 | 模型較大 |

### 2.2 選擇方案 C 的理由

方案 A 的致命缺陷：**拆分與放置是耦合的決策問題**。

假設有 3 個 AG，anti-affinity 要求 master 分散到不同 AG。若前處理先拆出 3 台 master，但每台 master 的 spec 剛好不適合某些 AG 的 BM 比例，就會導致放置失敗。而聯合優化可以同時考慮「用什麼 spec」和「放在哪」。

方案 B 會將拆分邏輯直接嵌入 `VMPlacementSolver`，導致原本乾淨的 solver 變得複雜。

方案 C 透過**共用 `CpModel`** 實現聯合優化，但拆分邏輯獨立在 `ResourceSplitter` 模組中，保持程式碼分離。

---

## 3. 系統架構

### 3.1 整體流程

```
Go Scheduler (從 inventory API 取得所有資料)
  │
  │  提供：vm_specs (全部可用的 VM 規格)
  │        baremetals (每台 BM 個別的 total/used capacity + topology)
  │        requirements (按 role 的總需求量)
  │        anti_affinity_rules, existing_vms, topology_rules
  │
  ├─ [既有路徑] POST /v1/placement/solve
  │     PlacementRequest (明確 VM list)
  │     → VMPlacementSolver → PlacementResult
  │
  └─ [新路徑] POST /v1/placement/split-and-solve
        SplitPlacementRequest
        → ResourceSplitter (建立 split 決策變數)
        → VMPlacementSolver (加入 placement 約束 + 目標函數)
        → SplitPlacementResult (拆分決策 + 放置結果)
```

### 3.2 模組職責

| 模組 | 檔案 | 職責 |
|------|------|------|
| **ResourceSplitter** | `app/splitter.py` | 建立拆分的 CP-SAT 決策變數和約束 |
| **Split Orchestrator** | `app/split_solver.py` | 組合 splitter + solver，執行聯合求解 |
| **VMPlacementSolver** | `app/solver.py` | 建立放置的 CP-SAT 約束和目標函數（已擴展支援 active vars） |
| **Data Models** | `app/models.py` | `ResourceRequirement`, `SplitPlacementRequest`, `SplitDecision`, `SplitPlacementResult` |
| **HTTP API** | `app/server.py` | `POST /v1/placement/split-and-solve` endpoint |

### 3.3 聯合求解的實作方式

```python
# 1. 建立共用的 CpModel
model = CpModel()

# 2. ResourceSplitter 在 model 上建立拆分變數和約束
splitter = ResourceSplitter(model, requirements, baremetals, config)
synthetic_vms = splitter.build()  # 返回合成的 VM 物件

# 3. VMPlacementSolver 在同一個 model 上建立放置變數和約束
all_vms = explicit_vms + synthetic_vms
solver = VMPlacementSolver(request, model=model, active_vars=splitter.active_vars)
result = solver.solve()

# 4. 從求解結果提取拆分決策
split_decisions = splitter.get_split_decisions(cp_solver)
```

---

## 4. 資料模型

### 4.1 新增模型

#### ResourceRequirement — 單一 role 的總資源需求

| 欄位 | 型別 | 預設 | 說明 |
|------|------|------|------|
| `total_resources` | `Resources` | — | 該 role 的總 CPU/mem/storage/GPU 需求 |
| `cluster_id` | `string` | `""` | 所屬 cluster |
| `node_role` | `NodeRole` | `worker` | 此需求對應的 role |
| `ip_type` | `string` | `""` | 網路類型（影響 auto anti-affinity 分組） |
| `vm_specs` | `Resources[] \| null` | `null` | 此 role 可用的 spec pool；`null` = 使用 `config.vm_specs` |
| `min_total_vms` | `int \| null` | `null` | 最少 VM 數量 |
| `max_total_vms` | `int \| null` | `null` | 最多 VM 數量 |

#### SplitPlacementRequest — 拆分 + 放置的輸入

| 欄位 | 型別 | 說明 |
|------|------|------|
| `requirements` | `ResourceRequirement[]` | 按 role 分組的需求 |
| `vms` | `VM[]` | 可選：與拆分需求共存的明確 VM |
| `baremetals` | `Baremetal[]` | 每台 BM 的個別資料 |
| `anti_affinity_rules` | `AntiAffinityRule[]` | AG 級 anti-affinity 規則 |
| `config` | `SolverConfig` | solver 設定（含 `vm_specs` 和 `w_resource_waste`） |
| `existing_vms` | `ExistingVM[]` | 其他 cluster 的既有 VM |
| `topology_rules` | `TopologyRule[]` | 跨 cluster 拓撲規則 |

#### SplitDecision — 拆分結果

| 欄位 | 型別 | 說明 |
|------|------|------|
| `node_role` | `NodeRole` | 此決策對應的 role |
| `vm_spec` | `Resources` | 選定的 VM 規格 |
| `count` | `int` | 此規格的 VM 數量 |

#### SplitPlacementResult — 拆分 + 放置的輸出

| 欄位 | 型別 | 說明 |
|------|------|------|
| `success` | `bool` | 是否所有 VM 都已放置 |
| `assignments` | `PlacementAssignment[]` | VM → BM 放置結果 |
| `split_decisions` | `SplitDecision[]` | 每個 (role, spec) 的數量決策 |
| `solver_status` | `string` | `OPTIMAL`, `FEASIBLE`, `INFEASIBLE`, ... |
| `solve_time_seconds` | `float` | 求解時間 |
| `unplaced_vms` | `string[]` | 未放置的 VM ID |
| `diagnostics` | `object` | 診斷資訊 |

### 4.2 SolverConfig 新增欄位

| 欄位 | 型別 | 預設 | 說明 |
|------|------|------|------|
| `vm_specs` | `Resources[]` | `[]` | 所有可用的 VM 規格（原 `slot_tshirt_sizes`，已統一命名） |
| `w_resource_waste` | `int` | `5` | 資源浪費的目標函數權重 |

---

## 5. CP-SAT 模型詳解

### 5.1 決策變數

#### 拆分變數（ResourceSplitter 建立）

| 變數 | 型別 | 範圍 | 說明 |
|------|------|------|------|
| `count[(req_idx, spec_idx)]` | IntVar | `[0, upper_bound]` | 第 req_idx 個需求中，第 spec_idx 個 spec 使用的數量 |
| `active[vm_id]` | BoolVar | `{0, 1}` | 合成 VM 是否啟用（1=放置，0=不放置） |

**upper_bound 計算**：
```
upper = max(ceil(total_resources.field / spec.field) for field in [cpu, mem, disk, gpu])
upper = max(upper, min_total_vms)  # 確保能滿足最低數量
upper = min(upper, max_total_vms)  # 不超過最大數量
```

#### 放置變數（VMPlacementSolver 建立）

| 變數 | 型別 | 範圍 | 說明 |
|------|------|------|------|
| `assign[(vm_id, bm_id)]` | BoolVar | `{0, 1}` | VM 是否放置在此 BM 上 |

### 5.2 約束

#### 拆分約束

| # | 約束 | 數學表達 | 說明 |
|---|------|----------|------|
| S1 | 資源覆蓋 | `Σ(count[s] × spec[s].field) >= total_resources.field` | 每個資源維度，拆分後總量 >= 需求 |
| S2 | VM 數量下限 | `Σ count[s] >= min_total_vms` | 可選 |
| S3 | VM 數量上限 | `Σ count[s] <= max_total_vms` | 可選 |
| S4 | Active-count link | `Σ active[spec][0..N] == count[spec]` | 啟用的 VM 數量 = count 變數值 |
| S5 | Symmetry breaking | `active[spec][k] >= active[spec][k+1]` | 排除等價解，加速求解 |

#### 放置約束（與 active var 整合）

| # | 約束 | 數學表達 | 說明 |
|---|------|----------|------|
| P1 | 一對一（普通 VM） | `Σ assign[vm, :] == 1` | 每台 VM 放在恰好一台 BM |
| P1' | 一對一（合成 VM） | `Σ assign[vm, :] == active[vm]` | **新增**：放置 iff 啟用 |
| P2 | BM 容量 | `Σ(demand.field × assign) <= available.field` | 不超過 BM 可用容量 |
| P3 | AG anti-affinity | `Σ assign[vm, bm∈AG] <= max_per_ag` | 分散到不同 AG |
| P4 | BM VM 數量限制 | `current + Σ assign[:, bm] <= max_vm_count` | 不超過 BM 最大 VM 數 |

### 5.3 目標函數

```
Minimize:
    w_consolidation × Σ bm_used[bm]                    # 使用最少的 BM
  + w_headroom      × Σ headroom_penalty[bm]            # 避免高使用率
  - w_slot_score    × Σ slot_score[bm]                  # 剩餘空間可用性
  + w_resource_waste × Σ (allocated[field] - demand[field])  # 最小化資源浪費 (新增)
```

**資源浪費項**的作用：當多個 spec 組合都能滿足 `>= 總需求` 時，選擇浪費最少的組合。

例如：需求 32 CPU
- Spec A (8 CPU) × 4 = 32 CPU → waste = 0
- Spec B (16 CPU) × 3 = 48 CPU → waste = 16
- 目標函數偏好 Spec A 的組合

### 5.4 Spec 自動篩選邏輯

當 `ResourceRequirement.vm_specs` 為 `null` 時，系統使用 `SolverConfig.vm_specs`（全部可用 spec）並自動過濾：

```python
def _resolve_specs(req, baremetals, config):
    candidates = req.vm_specs or config.vm_specs
    return [
        spec for spec in candidates
        if any(spec.fits_in(bm.available_capacity) for bm in baremetals)
    ]
```

過濾條件：spec 的每個資源維度都必須 <= 至少一台 BM 的可用容量。這避免了建立永遠無法放置的 VM。

---

## 6. Go Scheduler 整合

### 6.1 Solver 無狀態原則

Solver 不維護任何狀態，所有資料由 Go scheduler 從 inventory API 取得後傳入。

### 6.2 Go Scheduler 需提供的資料

| 資料 | 來源 | 說明 |
|------|------|------|
| `requirements[].total_resources` | 使用者申請 | 按 role 的總資源需求量 |
| `requirements[].vm_specs` | VM Inventory API（可選） | 此 role 可用的 VM spec；`null` 時使用 `config.vm_specs` |
| `config.vm_specs` | VM Inventory API | 所有可用的 VM 規格定義 |
| `baremetals` | BM Inventory API | 每台 BM 個別的 total/used capacity + topology |
| `existing_vms` | VM Inventory API | 其他 cluster 的既有 VM 放置資訊 |
| `anti_affinity_rules` | 使用者 / Policy | AG 級 anti-affinity 規則 |
| `topology_rules` | 使用者 / Policy | 跨 cluster topology 規則 |

### 6.3 為什麼需要每台 BM 個別資料？

必須提供每台 BM 個別的剩餘資源而非聚合的總量，原因：

1. **放置約束是 per-BM 的**：anti-affinity 要求 VM 分散到不同 AG 的 BM 上
2. **容量是離散不可合併的**：3 台各剩 10 CPU 的 BM ≠ 1 台剩 30 CPU 的 BM
3. **聯合優化的前提**：solver 要同時決定拆分和放置，必須知道每台 BM 各自能放什麼

---

## 7. 案例演示

### 7.1 基本拆分：單一 Spec

**輸入**：
- 需求：worker 總共 32 CPU, 128GB RAM, 800GB disk
- 可用 spec：8 CPU, 32GB RAM, 200GB disk
- BM 庫存：2 台，各 64 CPU, 256GB RAM, 2TB disk

**Solver 求解**：
- `count[worker_spec] = 4`（32/8 = 4）
- 4 台合成 VM，全部啟用
- 放置到 2 台 BM（各 2 台）

**輸出**：
```json
{
  "success": true,
  "split_decisions": [
    {"node_role": "worker", "vm_spec": {"cpu_cores": 8, "memory_mib": 32000, "storage_gb": 200}, "count": 4}
  ],
  "assignments": [
    {"vm_id": "split-r0-s0-0", "baremetal_id": "bm-1", "ag": "ag-1"},
    {"vm_id": "split-r0-s0-1", "baremetal_id": "bm-1", "ag": "ag-1"},
    {"vm_id": "split-r0-s0-2", "baremetal_id": "bm-2", "ag": "ag-2"},
    {"vm_id": "split-r0-s0-3", "baremetal_id": "bm-2", "ag": "ag-2"}
  ]
}
```

### 7.2 多 Spec 混合拆分

**輸入**：
- 需求：worker 總共 48 CPU, 192GB RAM, 1200GB disk
- 可用 spec：
  - Small: 4 CPU, 16GB RAM, 100GB disk
  - Large: 16 CPU, 64GB RAM, 400GB disk
- BM 庫存：3 台，各 64 CPU, 256GB RAM, 2TB disk

**Solver 求解**（waste minimization）：
- `count[large] = 3`（48/16 = 3，完美覆蓋）
- `count[small] = 0`（不需要）
- waste = 0（每個維度都精確匹配）

### 7.3 Per-role 拆分 + Anti-affinity

**輸入**：
- 需求 1：master 總共 12 CPU, 48GB RAM，固定 3 台
- 需求 2：worker 總共 32 CPU, 128GB RAM
- master spec: 4 CPU, 16GB RAM
- worker spec: 8 CPU, 32GB RAM
- BM 庫存：4 台，分佈在 3 個 AG
- auto_generate_anti_affinity = true

**Solver 求解**：
- master: 3 台 × (4 CPU, 16GB)，各分配到不同 AG
- worker: 4 台 × (8 CPU, 32GB)，分散放置
- anti-affinity 約束在**聯合模型**中確保 master 分散，不會出現拆分合理但放不下的情況

### 7.4 混合模式：明確 VM + 拆分需求

**輸入**：
- 明確 VM: 1 台 infra VM (8 CPU, 32GB)
- 拆分需求：worker 總共 16 CPU, 64GB
- worker spec: 4 CPU, 16GB

**Solver 求解**：
- infra VM 直接放置（普通 placement 約束）
- 4 台 worker 合成 VM（16/4 = 4）
- 共 5 台 VM 在同一個 CpModel 中聯合放置

---

## 8. 達成效益

### 8.1 自動化需求拆分

| 項目 | 改動前 | 改動後 |
|------|--------|--------|
| 使用者輸入 | 必須指定每台 VM 的精確規格和數量 | 只需指定按 role 的總資源量 |
| 拆分方式 | 人工決定（可能不最優） | solver 自動找最優組合 |
| BM 利用率 | 取決於人工選擇的 VM spec | 自動匹配 BM 資源比例 |

### 8.2 全局最優

透過聯合 CP-SAT 模型，拆分和放置在**同一次求解**中完成：
- 不會出現「拆分看似合理但放不下」的問題
- anti-affinity、topology、容量等約束全部被考慮
- 資源浪費最小化

### 8.3 向後相容

- 既有 `POST /v1/placement/solve` 完全不變
- `SolverConfig.vm_specs` 是 `slot_tshirt_sizes` 的重命名，JSON 欄位名變更但功能相同
- 不影響 Go scheduler 的既有呼叫流程

---

## 9. 後續工作

### 9.1 短期

- **Go Scheduler 整合**：在 Go 端新增 `/v1/placement/split-and-solve` 的呼叫路徑
- **VM Spec 管理**：在 inventory API 中定義和維護 VM spec 清單
- **效能測試**：驗證大規模場景（100+ BMs, 多 spec 組合）的求解時間

### 9.2 中期

- **GPU 支援**：驗證 GPU 資源維度的拆分行為
- **Cost-aware 拆分**：在目標函數中加入成本權重（不同 spec 可能有不同單價）
- **拆分結果解釋**：在 diagnostics 中加入「為什麼選這個組合」的解釋

### 9.3 長期

- **Multi-cluster 拆分**：一次拆分需求分配到多個 cluster
- **動態 Spec 生成**：不限於預定義 spec，由 solver 直接決定 VM 的資源規格（需額外約束確保規格合理）
- **歷史數據回饋**：根據過去的放置結果調整 spec pool 或目標函數權重
