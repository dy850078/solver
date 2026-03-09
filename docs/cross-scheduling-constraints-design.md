# 跨調度約束設計文件（Cross-Scheduling Constraint Design）

> **狀態**：設計確認完成（尚未實作）
> **日期**：2026-03-09

---

## 一、問題背景

### 現況

目前每次 `PlacementRequest` 都是無狀態、獨立的。Solver 只保證**本次調度**的約束被滿足，無法防止未來的調度違反本次所設定的規則。

### 核心問題：「我不犯人，但人可能犯我」

```
Scheduling A（cluster A）：max_vm_per_bm = 3
  → 調度完成後，BM1 有 3 台 VM，符合規則

Scheduling B（cluster B）：max_vm_per_bm = 10
  → 調度後，BM1 變成 12 台 VM
  → Cluster A 原本的規則被違反了，但 solver 對此一無所知
```

**根本原因**：約束的生命週期只存在於單次調度中，不會持久化。

---

## 二、約束類型分類

### 2.1 BM 層級：VM 數量上限

| 屬性 | 說明 |
|------|------|
| 作用對象 | 單台 Baremetal |
| 約束語意 | 這台 BM 上總共不能超過 N 台 VM |
| 規則來源 | BM Role 的永久政策（Inventory API） |
| 影響方向 | 所有使用這個 BM 的調度都需遵守 |
| 生命週期 | 永久，與 VM 的存在無關 |
| 衝突處理 | 同一 BM 有多條限制時取最嚴格（min） |

**BM Role 決定上限**：同一個 BM Group 內，不同 Role 的 BM 可能有不同的上限。

```
BM01  role=control-plane  → 可放 master/infra/l4lb  → max_vm_count = 5
BM02  role=worker         → 可放 worker             → max_vm_count = 20
```

Go Scheduler 從 Inventory API 取得各 BM 的 `max_vm_count`，直接填進 `Baremetal` 模型，**不使用獨立的 `BmVmCountLimit` 結構**。

---

### 2.2 拓撲層級：Cross-Cluster Anti-Affinity（不能共處）

| 屬性 | 說明 |
|------|------|
| 作用對象 | 兩個（或多個）cluster 的 VM 群組 |
| 約束語意 | 這些 cluster 的 VM 不能出現在同一個拓撲範圍內 |
| 規則來源 | 由聲明方（cluster A）在 Inventory API 設定 |
| 影響方向 | **不對稱** — A 在意，B 不一定在意 |
| Enforcement | `hard`（預設）或 `soft` |

**拓撲層級（由低至高）**：
```
rack < datacenter < phase < site
```

**重要隱含規則**：`same(lower)` → `same(higher)`

| 約束 | 意義 |
|------|------|
| 不同 DC | 同 site/phase 可以，但同 DC 不行 |
| 不同 Phase | 同 site 可以，但同 phase/DC/rack 不行 |
| 不同 Site | 完全隔離，最嚴格 |

**Soft Anti-Affinity 的用途**：使用者明確選擇「我知道這條規則，但接受在資源緊張時可以違反」，系統不會自作主張降級。

---

### 2.3 拓撲層級：Cross-Cluster Affinity（希望共處）

| 屬性 | 說明 |
|------|------|
| 作用對象 | 兩個（或多個）cluster 的 VM 群組 |
| 約束語意 | 希望這些 cluster 的 VM 盡量在同一個拓撲範圍內 |
| 規則來源 | 由聲明方設定 |
| 影響方向 | 不對稱 |
| Enforcement | `soft` 只（violating → 不理想，但仍合法） |

**為什麼 Affinity 不設為 Hard？**

目前業務場景中沒有「必須跟另一個 Cluster 同拓撲否則不可調度」的需求。使用者若需要指定特定拓撲範圍，由 Go Scheduler 預先過濾 `baremetals` 即可（不需要 Solver 處理）。

---

## 三、設計決策

### 3.1 Solver 維持無狀態

**Solver 不持久化任何狀態。** Go Scheduler 負責：
1. 從 Inventory API 收集所有相關規則
2. 查詢所有相關 cluster 的現有 VM 位置
3. 將「當前世界狀態 + 所有約束」打包進 `PlacementRequest`
4. Solver 只做一次性計算並回傳結果

---

### 3.2 Hard vs Soft 矩陣（確認版）

| 規則類型 | Enforcement | 實作方式 |
|----------|-------------|----------|
| Anti-Affinity | `hard`（預設） | CP-SAT `model.Add(...)` — 違反則 INFEASIBLE |
| Anti-Affinity | `soft` | CP-SAT objective penalty — 盡量避免 |
| Affinity | `soft`（唯一選項） | CP-SAT objective score — 最大化共處 |
| BM VM Count Limit | `hard` | CP-SAT capacity constraint |

INFEASIBLE 的處理：調度失敗，由使用者重新調度。系統不做自動降級。

---

### 3.3 Affinity Score 計分方式

Score 從**被放置的 VM** 的角度計算，而非從目標 cluster 的 VM 數量計算。

```
對本次每一台被放置的 VM v：
  score(v, rule) = 1  if v 最終所在的 topology zone 有目標 cluster 的 VM
                = 0  otherwise

Rule 的總分 = Σ score(v, rule) for v in 本次被放置的 VM
```

**為什麼這樣做**：

| 計分方式 | weight=1 的實際語意 | 使用者直覺 |
|---------|------------------|-----------|
| 乘以目標 cluster VM 數 | 目標 cluster 越大，rule 隱性越重要 | ❌ 違反直覺 |
| 只看被放置的 VM 是否達成 | weight 純粹代表「相對重要性」 | ✅ 符合直覺 |

同一 scope 下，無論目標 cluster 有多少台既有 VM，兩條 weight=1 的 affinity rule 對 objective 的最大貢獻相同。

---

### 3.4 `allow_partial_placement` 與 Soft Rule 的優先序

**VM 放置數量的優先權絕對凌駕於 Soft Rule 之上。**

實作方式：Two-Phase Solving

```
Phase 1：最大化放置 VM 數量
  Objective: maximize Σ placed[vm_id]
  → 得到最優放置數量 N

Phase 2：固定放置數量為 N，最大化 soft rule 分數
  Add constraint: Σ placed[vm_id] == N
  Objective: maximize Σ soft_rule_score
  → 在不犧牲任何 VM 的前提下，盡量滿足 affinity
```

| 情況 | 行為 |
|------|------|
| `allow_partial_placement=False`，無 soft rules | 單次 solve |
| `allow_partial_placement=False`，有 soft rules | 單次 solve，objective 只有 soft rule score |
| `allow_partial_placement=True`，無 soft rules | 單次 solve |
| `allow_partial_placement=True`，有 soft rules | **兩階段 solve** |

---

### 3.5 拓撲衝突偵測與冗餘警告（Validation Phase）

**衝突偵測**：同一 cluster pair，affinity 與 anti_affinity 的 scope 層級矛盾。

```
衝突條件：affinity.scope 的層級 ≤ anti_affinity.scope 的層級
  → same(lower) 隱含 same(higher)，與 different(higher) 矛盾
  → 回傳 MODEL_INVALID

合法範例：affinity @ site + anti_affinity @ DC
  → 可以同 site 但不同 DC，不矛盾
```

**冗餘警告**：同一 cluster pair + 同方向（affinity 或 anti_affinity），有多條不同 scope 的規則。

```
規則：只保留最細顆粒度（hierarchy 最低）的規則
警告：對較粗的規則產生 WARNING，標記為冗餘，不納入 Solver 計算

層級（由細至粗）：rack < datacenter < phase < site
```

---

## 四、資料結構（確認版）

### Baremetal 模型更新

```python
class Baremetal:
    id: str
    total_capacity: Resources
    used_capacity: Resources
    topology: Topology

    # 新增
    bm_role: str              # e.g. "control-plane", "worker"
    max_vm_count: int | None  # None = 無限制；由 Go Scheduler 從 Inventory API 填入
    current_vm_count: int = 0 # 目前 BM 上的 VM 總數；由 Go Scheduler 填入
```

### 新增模型

```python
class ExistingVM:
    """已存在於 BM 上的 VM（來自之前的調度，用於 cross-cluster 拓撲規則）"""
    vm_id: str
    cluster_id: str    # 屬於哪個 cluster
    baremetal_id: str  # 目前住在哪台 BM（Solver 結合 Baremetal.topology 推導出 DC/Rack）

class TopologyRule:
    """Cross-cluster 拓撲親和性規則"""
    rule_id: str
    cluster_ids: list[str]  # 哪些 cluster 之間的關係
    scope: Literal["rack", "datacenter", "phase", "site"]
    type: Literal["affinity", "anti_affinity"]
    enforcement: Literal["hard", "soft"] = "hard"  # anti_affinity 預設 hard；affinity 只能 soft
    weight: int = 1         # soft constraint 的相對重要性（僅 soft 有效）

# enforcement 預設值規則：
#   type="anti_affinity" → enforcement 預設 "hard"
#   type="affinity"      → enforcement 固定為 "soft"（傳入 "hard" 視為無效，validation 階段警告）
```

### PlacementRequest 更新

```python
class PlacementRequest:
    # --- 現有欄位 ---
    vms: list[VM]
    baremetals: list[Baremetal]          # 已含 bm_role / max_vm_count / current_vm_count
    anti_affinity_rules: list[AntiAffinityRule]  # 現有的 AG-based 規則（不變）
    config: SolverConfig

    # --- 新增欄位 ---
    existing_vms: list[ExistingVM]       # 與本次調度有規則關聯的 cluster 的 VM 位置
    topology_rules: list[TopologyRule]   # Cross-cluster 拓撲規則
```

> **注意**：`BmVmCountLimit` 獨立結構已移除，改為直接掛在 `Baremetal.max_vm_count` 上。

---

## 五、Go Scheduler 的規則收集策略

### 規則收集流程

```
輸入：本次調度的 cluster_id = A，使用的 bm_group = group-1

Step 1：查詢 BM 資訊（含 role 與 VM count limit）
  GET /baremetals?bm_group=group-1
  → [{ baremetal_id, bm_role, max_vm_count, current_vm_count, ... }, ...]

Step 2：查詢 Cross-Cluster 拓撲規則（雙向）
  GET /rules/topology?cluster_id=A
  → A 自己聲明的規則（A → B, A → C）
  → 其他 cluster 聲明的涉及 A 的規則（X → A, Y → A）

Step 3：收集相關 cluster 的現有 VM 位置
  referenced clusters = B, C, X, Y（Step 2 中被提及的所有 cluster）
  GET /vms/existing?cluster_ids=B,C,X,Y
  → [{ vm_id, cluster_id, baremetal_id }, ...]

Step 4：組裝 PlacementRequest 送給 Solver
```

### 為何只需要「一跳」，不需要遞迴？

規則是「A 限制自己與 B 的關係」，而非「A 繼承 B 的所有限制」。

```
A → B（A 不想跟 B 同 DC）
B → C（B 不想跟 C 同 Phase）

調度 A 時：只需要知道 B 在哪裡
不需要知道 C 在哪裡（A 與 C 沒有直接規則）
```

---

## 六、Solver 計算流程（新版）

```
Phase 1：規則驗證
  a. 拓撲衝突偵測（affinity vs anti_affinity scope 矛盾）
     → 有衝突：回傳 MODEL_INVALID + 診斷訊息，停止
  b. 冗餘規則過濾（同 pair 同方向多個 scope）
     → 保留最細顆粒度，其餘加入 diagnostics.warnings

Phase 2：Hard Constraints → CP-SAT model.Add(...)
  - BM VM 數量限制：current_vm_count + 本次分配數 ≤ max_vm_count
  - Anti-affinity hard：topology zone 排除（結合 existing_vms + Baremetal.topology）
  - 資源容量限制：CPU / Memory / Disk / GPU
  - 一台 VM 只能放一台 BM

Phase 3：Soft Constraints → Objective Function
  - Affinity soft：maximize Σ score(vm, rule)
      score(vm, rule) = 1 if vm 所在 zone 有目標 cluster 的 VM，否則 0
  - Anti-affinity soft：minimize Σ violation(rule)
  - 依 weight 加權

Phase 4a（allow_partial_placement=False 或無 soft rules）：
  單次 Solve

Phase 4b（allow_partial_placement=True 且有 soft rules）：
  Two-Phase Solve
  → Phase 1：maximize 放置 VM 數，得 N
  → Phase 2：固定放置數 == N，maximize soft rule score

Phase 5：回傳 PlacementResult
  - assignments（VM → BM 映射）
  - solver_status
  - diagnostics：
      - 每條 soft rule 的達成分數
      - warnings（冗餘規則列表、affinity 目標 cluster 無 existing_vms 等）
```

---

## 七、實作細節補充

### 7.1 `current_vm_count` 的範圍

`Baremetal.current_vm_count` 代表**這台 BM 上所有 VM 的總數**，不限 cluster 或 ip_type。

理由：`max_vm_count` 是 BM Role 的物理政策，限制的是 BM 的總承載量，與 VM 屬於哪個 cluster 無關。若只計算「有規則關聯的 cluster」的 VM，會低估目前的使用量，導致 count limit 計算錯誤。

Go Scheduler 從 Inventory API 取得該 BM 上的全域 VM 數量後填入此欄位。

---

### 7.2 Two-Phase Solving 的 Timeout 分配

`SolverConfig.max_solve_time_seconds` 在 Two-Phase Solving 時，**由兩個 Phase 平均分配**。

```
phase_1_timeout = max_solve_time_seconds / 2
phase_2_timeout = max_solve_time_seconds / 2
```

若 Phase 1 提前結束，剩餘時間**不轉移**給 Phase 2，維持各自獨立的 budget。

**理由**：VM 放置數量優先於 soft rule，Phase 1 若在 budget 內找到最優解，Phase 2 仍需足夠時間做 affinity 最佳化。若未來發現分配比例需要調整，可在 `SolverConfig` 加入 `phase_split_ratio` 欄位。

---

### 7.3 現有 `AntiAffinityRule`（AG-based）與 `TopologyRule` 的關係

兩者是**互補、不重疊**的機制，各自獨立運作：

| | `AntiAffinityRule` | `TopologyRule` |
|---|---|---|
| **作用範圍** | 本次調度內部 | 跨 cluster（含歷史調度） |
| **參照對象** | 本次請求中的 VM | `existing_vms`（其他 cluster 的 VM） |
| **隔離單位** | AG（Availability Group） | rack / DC / phase / site |
| **規則來源** | `PlacementRequest` 內明確傳入 | Inventory API（Go Scheduler 收集） |
| **未來計畫** | 維持現有邏輯，不合併 | 新增功能，獨立處理 |

兩套機制在 Solver 內分開處理，Solver 計算流程中 Phase 2 的 Anti-affinity hard constraint 同時套用兩者。

---

### 7.4 `existing_vms` 的地理範圍

`existing_vms` 包含**目標 cluster 在全域的所有 VM**，不限於當前 bm_group 內的 BM。

**理由**：

```
Cluster B 的 VM 在 dc1（dc1 的 BM 不在本次的 bm_group 內）
Cluster A 宣告：anti_affinity 與 B @ datacenter

若 existing_vms 只包含 bm_group 內的 VM：
  → Solver 不知道 dc1 已有 Cluster B 的 VM
  → 可能把 Cluster A 的 VM 放到 dc1 → 規則被違反
```

Solver 拿到全域的 `existing_vms` 後，結合 `baremetals` 列表中的 `topology` 資訊推導出各 cluster 的拓撲分佈，進而正確套用 anti_affinity 規則。

> **注意**：`existing_vms` 中的 `baremetal_id` 若不在本次 `baremetals` 列表內，Solver 需從 `existing_vms` 配合一份**完整的 BM topology 查找表**來取得其 topology 資訊。Go Scheduler 應確保此查找表隨 request 一併傳入，或預先將 topology 資訊展開到 `ExistingVM` 中。

建議將 `ExistingVM` 擴充為：

```python
class ExistingVM:
    vm_id: str
    cluster_id: str
    baremetal_id: str
    topology: Topology  # 新增：直接帶入，避免 Solver 需要額外查找
```

---

### 7.5 Affinity 目標 Cluster 無 `existing_vms` 時的行為

若 `topology_rules` 中某條 affinity rule 的目標 cluster 在 `existing_vms` 中**沒有任何 VM**（例如該 cluster 尚未調度或 VM 已全部刪除）：

- 該 rule 的 affinity score 對所有 BM 均為 0
- 等同於這條 rule 在本次調度中**無任何作用**，不影響 placement 決策
- Solver **不報錯**，但在 `diagnostics.warnings` 中加入警告：

```json
{
  "type": "affinity_rule_no_effect",
  "rule_id": "rule-123",
  "reason": "target cluster 'B' has no existing VMs; affinity rule has no effect"
}
```

Go Scheduler 在收集 `existing_vms` 後若發現某個 referenced cluster 完全沒有 VM，可以選擇提前過濾掉該條 rule（不送進 request），以減少 Solver 的無效計算。

---

### 7.6 `TopologyRule.enforcement` 預設值與合法性

| `type` | `enforcement` 合法值 | 預設值 | 備註 |
|--------|---------------------|--------|------|
| `anti_affinity` | `"hard"`, `"soft"` | `"hard"` | 兩者皆合法 |
| `affinity` | `"soft"` | `"soft"` | 傳入 `"hard"` → validation 警告，強制改為 `"soft"` |

Validation 階段（Phase 1）若收到 `affinity + enforcement="hard"`，不視為 `MODEL_INVALID`，而是：
1. 自動降級為 `"soft"`
2. 在 `diagnostics.warnings` 中說明：

```json
{
  "type": "enforcement_downgraded",
  "rule_id": "rule-456",
  "reason": "affinity rules cannot be hard; downgraded to soft"
}
```
