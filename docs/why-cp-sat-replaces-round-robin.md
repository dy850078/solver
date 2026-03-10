# 為什麼用 CP-SAT Solver 取代 Go 排程器的輪詢（Round-Robin）？

> **對象**：Kubernetes 叢集排程組的工程師
> **目的**：說明現有輪詢排程的具體缺陷，以及本專案如何用 Google OR-Tools CP-SAT 約束規劃求解器逐一解決這些問題

---

## 一、背景：現有排程器做什麼？

Go 排程器（scheduler）負責把一批「需要被創建的 VM」分配到「可用的裸金屬伺服器（Baremetal，BM）」上。
目前的核心邏輯是**輪詢（Round-Robin）**：依序把 VM 一個個分配給下一台 BM，週而復始。

```
VM-1 → BM-A
VM-2 → BM-B
VM-3 → BM-C
VM-4 → BM-A   ← 回到頭
VM-5 → BM-B
...
```

這個做法簡單易懂，但面對真實環境的各種約束條件，會產生多個難以手動修補的問題。

---

## 二、輪詢排程的七個核心問題

### 問題 1：無法保證不超出 BM 資源容量

輪詢只考慮「下一台是誰」，不考慮這台 BM 剩下多少 CPU / Memory / Disk / GPU。
結果是：**VM 被排到一台根本塞不下它的 BM**，導致建立失敗或系統過載。

> 真實情境：BM-A 已剩 2 CPU cores，VM-5 要求 16 cores → 輪詢照排不誤。

---

### 問題 2：無法尊重候選清單（Candidate List）

排程流程的第三步（Step 3）會依據 IP、網段、硬體型號等條件先篩出每個 VM 的「合法 BM 清單」。
輪詢不讀這份清單，直接按序分配，導致 VM 被放到**不合格的 BM** 上。

> 真實情境：VM 需要 Routable IP，只有 BM-B、BM-C 有這個網段，但輪詢可能把它排到 BM-A。

---

### 問題 3：無法做到 AG（Availability Group）反親和性分散

高可用架構要求同一叢集的 Master VM 分散到不同的 AG（機架群組），避免單一 AG 故障導致控制平面全滅。
輪詢天生不懂拓撲，它只會依序填滿：

```
AG-1: VM-master-1, VM-master-2, VM-master-3   ← 3 個 master 全在同一 AG！
AG-2: 空的
AG-3: 空的
```

> 真實後果：BM-A、BM-B 都在 AG-1 被輪詢填滿，某天 AG-1 機架斷電，整個叢集控制平面消失。

---

### 問題 4：無法處理部分排程（Partial Placement）

某些情境下 BM 資源不足以放下「這批 VM 的全部」，合理的做法是「能放幾個就放幾個，回報哪些放不下」。
輪詢不具備這種判斷，它要嘛全放（可能超量），要嘛整批失敗（無法利用剩餘空間）。

---

### 問題 5：無法限制單台 BM 上的總 VM 數

某些 BM 因角色（Role）或授權限制，有「最多同時跑 N 個 VM」的政策上限，與資源用量無關。
輪詢對這個數字完全無感，會繼續往同一台 BM 疊加 VM 直到資源耗盡或超出政策。

> 真實情境：BM-infra 的策略是 max 4 個 VM，目前已有 3 個，輪詢又分配了 2 個 → 超出政策上限。

---

### 問題 6：無法執行跨叢集拓撲反親和性

多個 Kubernetes 叢集共享同一批 BM 時，「不同叢集的特定角色 VM 不能在同一個機房（Datacenter）」這類跨叢集規則完全超出輪詢的能力範圍。
輪詢只看「現在要排的這批 VM」，完全看不到其他叢集的 VM 已經在哪裡。

> 真實情境：Cluster-A 和 Cluster-B 要求 datacenter 級別反親和，但輪詢把 Cluster-A 的 VM 排到 Cluster-B 已佔用的 DC-1，違反隔離政策。

---

### 問題 7：無法優化跨叢集拓撲親和性（Co-location）

某些叢集希望「跟另一個叢集的 VM 排在相同的機房」，以降低延遲或滿足合規要求（軟性需求）。
輪詢更不可能為了這種偏好去調整分配順序。

---

## 三、解決方案：CP-SAT 約束規劃求解器

本專案以 **Python sidecar 服務**的形式運行在 Go 排程器旁，接收 PlacementRequest、回傳 PlacementResult。
核心是 Google OR-Tools 的 **CP-SAT 求解器**，它的工作原理是：

```
定義「決策變數」→ 加入「約束條件」→ 設定「目標函數」→ 搜尋滿足所有條件的最佳解
```

每個 (VM, BM) 的組合對應一個布林變數 `assign[vm_i, bm_j]`，值為 1 代表「把 VM-i 放到 BM-j」。

---

## 四、七個問題的對應解法

### 解法 1：容量約束（Capacity Constraint）

**對應問題：1**

對每台 BM 的每個資源維度加入線性不等式約束：

```
∀ BM-j, ∀ resource r:
  Σ (demand[vm_i][r] × assign[vm_i, bm_j]) ≤ available[bm_j][r]
```

資源維度：`cpu_cores`、`memory_mb`、`disk_gb`、`gpu_count`

求解器在搜尋過程中絕對不會產生超出容量的解。

---

### 解法 2：候選清單約束（Candidate List）

**對應問題：2**

在建立變數前做**預篩選**：若 VM-i 的候選清單存在，則只為 `(vm_i, bm_j in candidates)` 的組合建立變數，其他組合的變數根本不存在。

```python
if vm.candidate_baremetals:
    eligible = [bm for bm in vm.candidate_baremetals if capacity_fits]
else:
    eligible = [bm for bm in all_bms if capacity_fits]

for bm_id in eligible:
    assign[(vm.id, bm_id)] = model.NewBoolVar(...)
```

由於變數不存在，求解器物理上無法把 VM 排到不合格的 BM。
此舉同時縮小了問題規模，加快求解速度。

---

### 解法 3：AG 反親和性約束（Anti-Affinity）

**對應問題：3**

對每條反親和規則，對每個 AG 加入計數上限約束：

```
∀ rule r, ∀ AG a:
  Σ (assign[vm_i, bm_j]) ≤ max_per_ag(r)
    vm_i ∈ rule.vm_ids
    bm_j ∈ AG a
```

`max_per_ag` 動態計算為 `ceil(VM 數 / AG 數)`，允許在 AG 數不足時做均衡分散：

| 情境 | VM 數 | AG 數 | max_per_ag | 分布 |
|------|-------|-------|-----------|------|
| 3 Master，3 AG | 3 | 3 | 1 | 1/1/1 ✓ |
| 5 Master，3 AG | 5 | 3 | 2 | 2/2/1 ✓ |
| 6 Worker，3 AG | 6 | 3 | 2 | 2/2/2 ✓ |

不需要顯式指定規則，支援**自動生成**：依 `(ip_type, node_role)` 分組，自動產生對應的反親和規則。

---

### 解法 4：部分排程（Partial Placement）

**對應問題：4**

開啟 `allow_partial_placement=True` 後：

1. 每個 VM 的分配約束從「恰好放到一台」改為「最多放到一台」（`== 1` → `<= 1`）
2. 目標函數設為**最大化已放置的 VM 總數**：

```
Maximize: Σ assign[vm_i, bm_j]
```

求解器會自動找出能放最多 VM 的最佳組合，回報中的 `unplaced_vms` 欄位說明哪些 VM 無法排入。

---

### 解法 5：BM VM 數量上限（VM Count Limit）

**對應問題：5**

在 `Baremetal` 模型中加入 `max_vm_count` 和 `current_vm_count` 兩個欄位，並加入約束：

```
∀ BM-j with max_vm_count set:
  current_vm_count[bm_j] + Σ assign[vm_i, bm_j] ≤ max_vm_count[bm_j]
```

`current_vm_count` 由 Go 排程器從 Inventory API 查詢後填入，代表目前該 BM 上**所有叢集**的 VM 總數。
`max_vm_count = None` 表示無限制，不加約束。

---

### 解法 6：跨叢集拓撲反親和性（Cross-Cluster Anti-Affinity）

**對應問題：6**

透過 `existing_vms`（其他叢集已存在的 VM 及其拓撲位置）和 `topology_rules`（跨叢集規則）來實現。

**拓撲層級**（從細到粗）：

```
rack  <  datacenter  <  phase  <  site
```

同層級及以下相同，則上層也相同（`same(rack) → same(datacenter) → same(phase) → same(site)`）。

**強制（Hard）反親和性**：直接禁止相關組合

```python
# 如果 BM-j 所在的 datacenter 已被其他叢集的 VM 占用
# 則直接設 assign[vm_i, bm_j] = 0
if bm_zone in occupied_zones:
    model.Add(assign[(vm_i, bm_j)] == 0)
```

**柔性（Soft）反親和性**：加入懲罰項（目標函數負分）

```
Minimize: Σ weight × violation_indicator[vm_i, rule_r]
```

若唯一可用的 BM 都在被占用的區域，柔性規則不阻止排程，只記錄違反情況。

**規則驗證**（自動在求解前執行）：

| 檢查項目 | 處理方式 |
|---------|---------|
| 親和性 + 強制（hard）| 自動降為柔性（soft）+ 警告 |
| 親和與反親和 scope 衝突 | 回傳 `MODEL_INVALID` + 錯誤訊息 |
| 同一對叢集有多個同方向規則 | 保留最細粒度，過濾較粗的 + 警告 |

---

### 解法 7：跨叢集拓撲親和性（Cross-Cluster Affinity）

**對應問題：7**

**永遠是柔性（Soft）**，在目標函數中加入獎勵項：

```
Maximize: Σ weight × colocation_indicator[vm_i, rule_r]
```

若 VM-i 被放到與目標叢集 VM 相同的拓撲區域，`colocation_indicator = 1`，獲得正分。

若目標叢集目前沒有任何 VM，親和規則自動無效化（只記錄警告），不影響排程正確性。

---

## 五、兩階段求解（Two-Phase Solving）

當同時啟用「部分排程」和「柔性拓撲規則」時，單純合併目標函數會有**優先順序問題**：
求解器可能為了多拿幾個柔性分數，放棄多放一台 VM。

本專案採用**兩階段求解**確保優先級正確：

```
第一階段：Maximize 已放置 VM 數 → 得到最大數量 N
          ↓
第二階段：固定已放置數 == N，Maximize 柔性規則分數
```

兩個階段平分總 timeout（各佔 50%）。

```
┌─────────────────────────────────────────────────────────┐
│  Phase 1: 最大化 VM 放置數                                │
│  約束：容量 + 候選清單 + AG 反親和 + VM 數上限 + 強制拓撲  │
│  目標：Maximize Σ assign[vm_i, bm_j]                     │
│  結果：N = 最多能放 N 台                                   │
└────────────────────┬────────────────────────────────────┘
                     │ 固定：Σ assign == N
┌────────────────────▼────────────────────────────────────┐
│  Phase 2: 最大化柔性規則分數                              │
│  約束：全部硬約束 + Σ assign == N                         │
│  目標：Maximize Σ weight × soft_indicator                 │
│  結果：在不犧牲 VM 放置數的前提下，最佳化拓撲偏好           │
└─────────────────────────────────────────────────────────┘
```

---

## 六、完整求解流程圖

```
Go Scheduler
  │
  │  POST /v1/placement/solve
  │  {vms, baremetals, anti_affinity_rules,
  │   existing_vms, topology_rules, config}
  ▼
┌──────────────────────────────────────────┐
│  Python Solver Sidecar                   │
│                                          │
│  1. 規則驗證                              │
│     ├─ 衝突檢測（→ MODEL_INVALID）        │
│     ├─ 冗餘過濾（→ 保留最細粒度）          │
│     └─ Hard 親和降級（→ Soft + 警告）     │
│                                          │
│  2. 建立 CP-SAT 模型                      │
│     ├─ 決策變數（只建合法的 (VM, BM) 對）  │
│     ├─ 每個 VM 只能在一台 BM              │
│     ├─ BM 資源容量限制                    │
│     ├─ AG 反親和分散約束                  │
│     ├─ BM VM 數量上限                     │
│     └─ 跨叢集 Hard 反親和（直接封鎖）      │
│                                          │
│  3. 建立目標函數                          │
│     ├─ Soft 反親和懲罰項（-weight）       │
│     └─ Soft 親和獎勵項（+weight）         │
│                                          │
│  4. 求解                                  │
│     ├─ 有部分排程 + Soft 規則 → 兩階段     │
│     └─ 其他 → 單階段                      │
│                                          │
│  5. 回傳結果                              │
│     ├─ success / 失敗原因                 │
│     ├─ assignments: [{vm_id, bm_id, ag}] │
│     ├─ unplaced_vms: [vm_id, ...]        │
│     ├─ solver_status: OPTIMAL/FEASIBLE/… │
│     └─ diagnostics: {warnings, errors}  │
└──────────────────────────────────────────┘
  │
  ▼
Go Scheduler 依據 assignments 執行 VM 創建
```

---

## 七、問題與解法對照總表

| # | 輪詢的問題 | CP-SAT 解法 | 實作位置 |
|---|-----------|------------|---------|
| 1 | 不檢查 BM 資源容量 | 容量線性不等式約束 | `_add_capacity_constraints()` |
| 2 | 忽略候選清單 | 預篩選，只建合法變數 | `_get_eligible_baremetals()` |
| 3 | 無 AG 反親和分散 | per-AG 計數上限約束 + 自動規則生成 | `_add_ag_anti_affinity_constraints()` |
| 4 | 不支援部分排程 | 最大化放置數 + `unplaced_vms` 回報 | `_solve_single()` + `allow_partial_placement` |
| 5 | 忽略 BM VM 數量政策 | `current + new ≤ max_vm_count` 約束 | `_add_vm_count_constraints()` |
| 6 | 看不到跨叢集現況 | Hard/Soft 跨叢集拓撲反親和約束 | `_add_hard_topology_constraints()` |
| 7 | 無法優化跨叢集共置 | Soft 親和目標函數獎勵項 | `_build_soft_objective_terms()` |

---

## 八、與 Go 排程器的整合方式

CP-SAT Solver 以 **HTTP Sidecar** 的形式運行，Go 排程器保持 stateless 狀態：

```
Go Scheduler（現有）           Python Solver Sidecar（新增）
      │                                    │
      │  1. 查 Inventory API               │
      │     取得 BM 容量、角色、VM 數統計   │
      │                                    │
      │  2. 查 Step 3 候選清單             │
      │                                    │
      │  3. 查詢其他叢集 existing_vms      │
      │     及 topology_rules              │
      │                                    │
      │  ──── POST /v1/placement/solve ──► │
      │       PlacementRequest             │
      │                                    │
      │  ◄─── PlacementResult ────────────│
      │       assignments                  │
      │                                    │
      │  4. 依據 assignments 建立 VM       │
```

Go 排程器負責**收集所有狀態和規則**，Solver 只負責**計算最優分配**。
Solver 本身完全無狀態（stateless），每次呼叫獨立計算。

---

*文件版本：1.0 | 更新日期：2026-03-10*
