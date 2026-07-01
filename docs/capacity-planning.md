# Enhancement Proposal — K8s 資源擴充需求計算 (Capacity Planning)

> **作者**: Claude (claude-code)
> **日期**: 2026-06-30
> **狀態**: Design — 待 review，尚未實作
> **相關元件**: `app/splitter.py`、`app/split_solver.py`、`app/solver.py`、`app/models.py`、`app/diagnostics.py`

---

## Summary

K8s Capacity 大臣需要把「User 對各 Cluster 提出的 CPU/Mem/Storage/Pod 需求」轉換成
**Add Node Plan**（要加幾台、什麼規格的 VM as K8s Node），並向上彙整成
**跨廠區 × 跨季度的容量報表與採買建議**（in-stock BM 數、未來各季缺口、建議採買量）。

現有引擎的 `split-and-solve` 已能解「需求 → VM spec × count」這一段；本提案在不重寫
solver 的前提下，補上三塊缺口：

1. **Pod 維度** — 一台 VM as K8s Node 的 Pod 容納上限。
2. **規劃模式** — 在「還沒有指定候選 BM」時也能算出**需要多少供給**（採買量），而不只是放進固定庫存。
3. **多廠區 × 多季度聚合報表** — 庫存逐期 roll-forward、缺口轉採買、碎片化與有效剩餘量報表。

核心策略：新增一層 **Capacity Planning 編排層**，復用既有 joint-optimization 引擎
（splitter + solver），而非另起爐灶。

---

## Goals / Non-Goals

### Goals
- 從 per-cluster 的 vCore / Mem / Storage / **Pod** 需求，產出明確的「加 N 台 ⟨spec⟩ VM」計畫。
- 支援「最緊長幾台」與「固定機型長到滿足需求」兩種 sizing 策略（既有 `w_resource_waste` / `min_total_vms` 即可）。
- 把 Pod 上限納入 sizing：保證節點數足以容納總 Pod 需求。
- 在多 VM 共住、spec 不一、碎片不均的現實下，產出**可讀的容量報表**：
  - 以「資源貨幣」(vCore/Mem/Storage/Pods) 而非單純 BM 台數呈現。
  - 「有效剩餘量」：每個 fab 還能再長幾台標準 VM。
  - 「碎片率」：裝不下最小標準 VM 的擱淺 (stranded) 容量。
- 產出**採買建議**：每個 fab、每季缺幾台標準機型 BM，並標註缺口成因（容量不足 vs 拓撲打散不足）。

### Non-Goals
- 不做即時排程（即時 placement 仍走既有 `/v1/placement/solve`）。
- 不做成本最佳化 / 採購議價 / 機型 TCO 比較（只回「台數」，不回「金額」）。
- 不重排既有已上線節點（多期模擬中 placement 是黏住的，roll-forward 不 reshuffle）。
- 本提案**只交付設計**，不含實作。

---

## Current State & Problem

### 現況：引擎是「單發放置」

```
PlacementRequest / SplitPlacementRequest
   (一批 VM 或需求 + 一份固定 BM 庫存)
            │
            ▼
   solve_split_placement()  ── joint CP-SAT ──▶  success / INFEASIBLE / partial
            │
            ▼
   SplitDecision (spec × count) + PlacementAssignment (vm → bm)
```

`split-and-solve` 已能回答「User 給我 vCore，我要回他加幾台什麼規格」：
輸入 `ResourceRequirement.total_resources`，輸出 `SplitDecision{vm_spec, count}`。
「最緊長幾台」= 單一 spec + 高 `w_resource_waste`，或設 `min_total_vms`。

### 痛點對照表

| 需求 | 現況 | 缺口 |
|---|---|---|
| 需求 → 加幾台什麼 VM | ✅ `split-and-solve` | 無（已支援） |
| 一台 VM 限制多少 Pod | ❌ `Resources` 無 pod 欄位 (`models.py:27`) | **缺口 1** |
| 還沒買機器，要買幾台 BM | ❌ splitter 強制 `candidate_baremetals` 必填 (`splitter.py:102`)，只能放進既有庫存 | **缺口 2** |
| 多 fab × 多季度的 in-stock / 缺口報表 | ❌ 單發、無時間維、無跨 fab 聚合，只回 `bm_used_count/bm_total_count` | **缺口 3** |
| 碎片化呈現 | ⚠️ `slot_score` 只在 objective 內被優化 (`solver.py:864`)，未外露成報表 | **缺口 3** |
| 採買量評估 | ❌ 無「缺口 → 採買台數」輸出 | **缺口 3** |

### 為什麼「BM 台數」這個舊指標會失真

以前用 BM 台數看概量，前提是「一台 BM ≈ 一個工作負載單位」。但現在：
- 多 VM 共住一台 BM、VM spec 不一 → 一台 BM 的「剩餘價值」差異極大。
- 每台 BM 碎片程度不同 → 帳面 available 加總起來很多，實際卻長不出任何一台標準 VM。

因此「還剩幾台 BM」無法回答「我還能不能滿足下一批需求」。需要換成**資源貨幣 + 有效剩餘量**。

---

## Proposed Design

### High-level 架構：新增 Capacity Planning 編排層

```
CapacityPlanRequest
  (多 cluster × 多 period × 多 fab 的增量需求，含 Pod；
   in-stock BM 清單 + 可採買 BM 機型 template)
        │
        ▼
 ┌────────────────────────────────────────────────────────────┐
 │  Capacity Planning Layer  (新增: app/capacity_planner.py)     │
 │                                                              │
 │  for fab in fabs:                                            │
 │    state = in-stock BM 庫存(該 fab)                           │
 │    for period in periods (時間順序):                          │
 │      1) splitter:  該期該 fab 的需求 → VM spec×count           │  復用 splitter (+Pod floor)
 │      2) solver(partial): 把 VM 放進 state                     │  復用 solver
 │      3) 殘量(unplaced) → bin-pack 成「採買 BM 單位」            │  新增採買求解
 │      4) state += 採買的 BM；量測碎片 / 有效剩餘 slot           │  外露 slot_score
 │  聚合 → CapacityReport (per fab × per period)                 │
 └────────────────────────────────────────────────────────────┘
        │
        ▼
 CapacityReport:
   per (fab, period):
     resource_currency  { vCore/Mem/Storage/Pods: total/used/available }
     effective_headroom { 還能再長幾台標準 VM }
     fragmentation      { stranded 容量, 碎片率 }
     demand             { 本期需求 }
     shortfall          { 缺口資源量 }
     procurement        { 建議採買 BM 台數, 成因標註 }
```

### 缺口 1 — Pod 維度（決議：全域同一 max-pods）

因為 Pod 上限是**全域同一值**且與 VM spec 無關，建模可大幅簡化：Pod 需求不必成為
placement 端的資源欄位，而是退化成 **節點數下限**。

```
min_nodes(req) = ⌈ req.total_pods / global_max_pods_per_node ⌉
```

- 在 `SolverConfig` 新增 `max_pods_per_node: int = 0`（0 = 停用 Pod 約束）。
- 在 `ResourceRequirement` 新增 `total_pods: int = 0`。
- splitter 的 `_build_requirement()` 把 `min_nodes` 併入既有的 count 下限：
  `Σ_s count[req, s] ≥ max(min_total_vms or 0, min_nodes)`。
- **placement 端不變**：BM 沒有 Pod 容量概念，capacity constraint 不需要動。

> 好處：不需在 `Resources` 加非對稱欄位，不需區分 `COVERAGE_FIELDS` / `PLACEMENT_FIELDS`，
> 對既有 solver 零侵入。若未來 Pod 上限改為 per-spec，再升級成資源欄位（見 Alternatives）。

### 缺口 2 — 規劃模式：用「BM 機型 template + 候選落點」描述供給

**採買單位 = 單台 BM（決議 Q1）**，落點由 solver 在 fab 的 AG/DC 桶內分配台數（**L1，決議 Q2**）。

**決議：現階段採「理想化落點」—— 假定每個桶都有位置、機位無上限。** 這把「誰維護落點資料」
的問題整個消掉：落點集合直接**推導**為「該 fab 現有的 AG/DC 桶」，不需外部維護。

```python
class BaremetalType(BaseModel):
    """可採買的 BM 機型 (無固定 id)。"""
    type_id: str
    capacity: Resources
    fab: str                          # 歸屬廠區 (= topology.site 或 phase)
    # 理想化：桶內台數無上限；目標桶集合 = fab 現有 AG/DC（None → 自動推導）
    target_buckets: list[str] | None = None
```

- **in-stock**：沿用既有 `Baremetal`（有 id / used_capacity / topology）。
- **procurement**：規劃層先盡量放進 in-stock，殘量才在 fab 的桶內生成新 BM 算採買台數。
- 規劃模式下 `candidate_baremetals` 由規劃層**依 fab/桶自動推導**（不要求呼叫方填），
  維持 splitter 既有的「候選必填」契約不破壞 —— 編排層在呼叫 splitter 前補齊它。

> **理想化的兩個 caveat**：
> 1. 理想化的是**桶內容量（無限加機器）**，但**桶集合仍是現實 AG/DC** —— 憑空生一個不存在的
>    AG 不允許。故某 fab 只有 2 AG 卻要 3 副本打散時，採買**仍會卡**（`shortfall_cause=
>    anti_affinity`），這是正確且有用的訊號。
> 2. 規劃出的採買數可能超出實體機位（假定無限位置）。規劃/編預算階段可接受 —— 它告訴你
>    「要買 N 台、並得喬空間」。**真實機位上限（`max_bm` + landing-zone 維護）列為未來 Phase**。

**多機型可選（決議 Q3-機型）**：每個 fab 可有多個 `BaremetalType`；採買 bin-pack 是
「機型選擇 + 落點 + 數量」的聯合問題，CP-SAT 天然能解（見缺口 3b）。

#### 平均擺放策略：跨 AG / 實體 DC，且考慮 in-stock 現況

實務採買是**跨 3 個桶平均擺放**（目前是 3 AG，正轉向 3 實體 DC）。兩者引擎都天生支援：
`ag` 與 `datacenter` **都已在 `SPREAD_DIMENSIONS`**（`models.py:18`）。所以只需一個參數
指定要平衡哪個維度，數學完全一致 —— 這正好覆蓋「先用 3 AG 算、之後 1 AG 對應 1 實體 DC」
的轉換：

```python
# SolverConfig 新增
procurement_spread_dimension: str = "ag"     # "ag" | "datacenter"（或其他 SPREAD_DIMENSIONS）
w_procurement_balance: int = 3               # 平衡「結果可用量」的軟目標權重
```

**關鍵：平均的是「採買後的結果可用量」，不是「採買台數」。** 若 ag-0 現有 in-stock 還很多、
ag-1 很少，正確做法是**多買到 ag-1 去補平**，而不是每桶各買一樣多。形式化：

```
對每個桶 b（procurement_spread_dimension 的 bucket）:
  resulting_available[b] = in_stock_available[b] + bought[b]×cap − demand_placed[b]

軟目標: minimize (max_b resulting_available[b] − min_b resulting_available[b])
```

這讓採買「考慮 in-stock 現況」自動補在最短的桶，達成真正的平均，而非帳面平均。

### 缺口 3a — 多期 roll-forward

- **月粒度**；horizon = **`demand_book` 中出現的月份集合**（排序），不咬死 12 期（見缺口 3d）。
- 多 fab × 多月一次算：因 per-fab 自給自足，各 (fab, period) 為獨立求解、天然可平行。
- 每期帶**增量需求**（該期新增的 cluster 需求），不是累計值。
- **庫存狀態逐期滾動**：第 N 期 placement 把 BM 的 `used_capacity` 推進，
  第 N+1 期在這個基礎上繼續放（節點黏住、不重排）。採買進來的 BM 變成下一期的 in-stock。
- 實作上每期就是一次既有 `solve_split_placement(allow_partial_placement=True)`，
  期間用回傳的 assignments 更新 `used_capacity` 後傳入下一期。

### 缺口 3b — 採買量算法

殘量（這期 in-stock 放不下的 VM）連同「候選落點內的虛擬可採買 BM」一起丟進一次 CP-SAT solve：

- **變數**：`bm_buy_used[t, b, k]` BoolVar — type `t` 在桶 `b` 的第 k 台是否採買
  （理想化：k 上限由「殘量最多需要幾台」推導，非機位上限）；沿用既有 `assign[vm, bm]`。
- **約束**：殘量 VM 必須放進（in-stock ∪ 已啟用的採買 BM）；沿用既有 capacity /
  anti-affinity / failover（此時 spread 會作用在採買 BM 的落點拓撲上，逼採買去補足打散缺口）。
- **目標（分層）**：
  1. 主：`minimize Σ bm_buy_used`（總採買台數最少；未來可換成加權成本）。
  2. 軟：`minimize (max−min) resulting_available[b]`（平衡結果可用量，權重 `w_procurement_balance`）。

實作上直接在 `solver.py` 的 CP-SAT 模型加一組「虛擬可採買 BM」，復用 `bm_used` 機制
（`solver.py:770`）—— 採買 BM 就是「可選啟用、啟用要計數」的 BM。

缺口成因標註複用 `DiagnosticsBuilder._constraint_layer_check()`（`diagnostics.py:308`）：
能區分缺口是 `capacity`（純資源不足）還是 `anti_affinity`（拓撲/AG 打散不足）造成 ——
後者正是「單台自由落點」若沒有跨桶候選就會卡住的情形，也是本設計要 L1 的原因。

### 缺口 3c — 報表維度與解釋性（回答「該用什麼維度看資源長相」「怎麼說服採買」）

#### 核心反框：頭條是「可落地可用量」，不是「名目資源貨幣」

純加總 `available_capacity` 會**系統性高估**容量，因為它假設資源連續、且忽略拓撲打散。
這正是「帳面 100 vCore、需求 99 vCore 卻擺不下」的根因。解法不是換單位，而是換一個
**由 solver 在真實約束（capacity + anti-affinity + 拓撲）下算出來的數字**當頭條：

| 指標 | 算法 | 角色 |
|---|---|---|
| **名目可用量** (nominal) | Σ 各 BM `available_capacity` | 帳面數字，會高估 |
| **可落地可用量** (placeable) | 帶 anti-affinity 實際 bin-pack 參考 VM 至 INFEASIBLE | 真正裝得下的量 |
| **破碎損耗** (loss) | 名目 − 可落地 | 碎片 + 拓撲擋住吃掉的量 |

> **關鍵**：碎片化「和」拓撲擋不下，兩者都被吸進「可落地可用量」這一個數字。
> 不需為了拓撲再開一堆維度去解釋 —— solver 跑出 INFEASIBLE / partial 就是舉證。
> 採買理由從「需求 vs 名目」改成 **「需求 vs 可落地」**，數字才誠實。

#### 報表對象與粒度（決議）

**規劃 vs 執行是兩個階段** —— 規劃報表的最細粒度**停在 `(fab, AG/DC, month)` 的台數**，
**不下到某台 BM / 某 rack**。個別 VM 落哪台 BM 是**執行階段** `solve` / splitter 的事，不是
規劃報表的產物。這正好對齊「3 AG 平均」的思考語言，也砍掉大量複雜度。

**兩個對象**（財務＝Capacity 大臣同一角色；採購不納入；工程師 rack 級 drill-down 移出規劃範圍）：

| 對象 | 要看的 | 粒度 | 台數類型 |
|---|---|---|---|
| **Capacity 大臣**（主要消費者，含編預算）| 完整規劃：各 fab 各 AG/DC 各月 → 加幾台 node ＋ 買幾台 BM ＋ 成因；其中「各 fab 各 DC 各月買幾台 BM」即編預算視圖 | fab × AG/DC × 月 | node ＋ BM |
| **長官** | 缺口 + 採買 + 成因（彙總）| fab × 季/年 | 主要 BM |

成因標籤（`capacity` vs `anti_affinity`）直接複用
`DiagnosticsBuilder._constraint_layer_check()`（`diagnostics.py:308`）。編預算視圖（fab × DC ×
月 → BM 台數）是 Capacity 大臣的一個投影，不是獨立角色。

#### 兩種「台數」必須分開

- **Node/VM 台數**：某 AG 某月要「加幾台 K8s node」→ Capacity 大臣轉述 owner/工程師、
  Cluster Owner 拿去建 VM（對映 `SplitDecision`）。
- **BM 台數（採買）**：某 DC 某月要「買幾台實體機」→ 財務大臣編預算。

兩者不同（N 台 node 可能共住、只需 K≤N 台 BM）。報表**兩欄分開**，避免財務/長官看錯。

#### 形式：canonical JSON 優先，UI 與 Excel 都是薄投影

先產出結構化的 `CapacityReport` JSON，**Web UI 和 Excel 都只是它的投影**，不各寫一套邏輯：

- **Web UI**（先做）：複用既有 `app/web_static/`；規劃報表本質是 fab × AG/DC × 月 的表格
  + 篩選 + 長官頭條。表格中等工作量，趨勢圖可後補。
- **Excel**（後補）：資料本為表格狀，從同一份 JSON 匯出 xlsx/csv 很容易，加個 endpoint 即可。

#### 「可落地可用量」為什麼不能 by-cluster 加總（正確性陷阱）

不同 cluster 有不同 spec、不同候選 BM、不同 anti-affinity，且**搶同一個實體 BM 池**。
各算各的 headroom 再相加會 double-count（A 說還能放 5 台、B 也說 5 台，指的是同一塊空間）。

因此兩種用途分開：

1. **算採買缺口** → **不要先算 headroom 再相減**。把該期**所有 cluster 的需求一起丟進
   該 fab 共用庫存做一次 joint placement**，殘量就是缺口。一次解，contention 自然處理。
2. **健康度儀表（純參考）** → 才用「fab 層級、單一參考 spec」算粗略的「若全拿來長標準 VM
   可長 N 台」，並**明講這是參考值、不是各 cluster 加總**。

by-cluster 的可見度，改用**結果導向**呈現（這個 cluster 需求滿足了沒／缺幾台），
而不是發明一個能相加的 headroom。

#### 碎片率（健康度副指標）

`stranded = Σ_bm (裝不下參考 VM 的剩餘空間)`，即「名目 − 可落地」的細分，
正是現有 `slot_score` 概念（`solver.py:864`），把它從 objective 內部指標**外露成報表欄位**。
碎片率高 → 該整併或調 spec；碎片率低且可落地也低 → 該採買。

### 缺口 3d — User 需求單 (Demand Form) 與資料來源分工 (Provenance)

**核心原則：需求單只裝「User 意圖」，「現況」一律走系統。** User 不該、也難以從 UI 填
cluster 現況。分工如下：

| 誰提供 | 內容 | 來源 |
|---|---|---|
| **User（需求單）** | 哪個 cluster / 哪個月 / 什麼 role / 要多少 CPU/Mem/Storage/Pod / (可選) 指定 spec / min-max 台數 | UI 表單 |
| **系統（不讓 User 填）** | in-stock BM 庫存、候選 BM、cluster 現有分佈（每 AG 聚合數）、HA policy | **中介 Go Scheduler Service**（整合 Inventory）|

> **決議（岔路 A）：需求是「增量」語意** —— 「這個月幫我加 32 vCore」。因此 **sizing 不需要
> cluster 現況**（32 vCore 就切 32 vCore），只有 placement / 採買需要 BM 現況。
> 需求單因而不需 `demand_mode` 欄位。cluster 現況（Node 層聚合）只在**新舊一起打散**時才要
> （見缺口 3e）。
>
> **資料流**：所有系統側資料由中介的 **Go Scheduler Service** 整合 Inventory 後填入請求契約，
> Python solver 維持純函式（與既有 `candidate_baremetals` 由 scheduler 帶入的設計一致）。

提供 user 一張需求單填寫資源量，大部分**現有資料模型已支援**：

- **「只在乎 CPU 就只填 CPU」** → splitter 對每個資源維度檢查 `if total_demand <= 0: continue`
  （`splitter.py:171`），需求填 0 的維度**不會產生 coverage 約束**。忽略某些值天生可用。
- **顯式指定 VM spec** → `ResourceRequirement.vm_specs` 已存在；填了就只用這些 spec。
- **Pod Count** → Phase 1 新增 `total_pods`。

> ⚠️ **語意澄清（要對 user 與長官講清楚）**：「忽略 Mem」= 不對 Mem 設下限約束，
> **不代表 VM 的 Mem 是 0**。選到的 spec 仍有記憶體，placement 仍吃掉 BM 的實體記憶體。
> 所以「只填 CPU」算出的台數，背後仍佔用真實 Mem/Storage —— 報表的資源消耗欄位照實反映，
> 不因 user 沒填就當 0。

需求單是 `ResourceRequirement` 的薄包裝，欄位一對一對映：

```python
class DemandEntry(BaseModel):             # 需求帳本的一列；每列 = 一個目標月
    cluster_id: str
    node_role: NodeRole = NodeRole.WORKER
    period: str                           # 目標月份 "2026-07"
    # 增量需求；維度層級填 0 = 該維度不約束（不代表 VM 該維度用量為 0）
    cpu_cores: int = 0
    memory_mib: int = 0
    storage_gb: int = 0
    pod_count: int = 0
    # 顯式指定希望的 VM 規格；省略則用 config.vm_specs 由 solver 選
    vm_specs: list[Resources] | None = None
    min_total_vms: int | None = None
    max_total_vms: int | None = None
    fab: str | None = None                # 預設由 cluster 現有 footprint 推導（系統帶入）
    # 註：無 demand_mode（一律增量）；無 cluster 現況欄位（系統經 Go Scheduler 帶入）
```

#### 需求帳本：稀疏、可修訂、月份驅動 horizon（決議）

需求存成一本**帳本 = `list[DemandEntry]`**，每列 `(cluster, role, month)`：

- **稀疏**：user 一次可只送某幾個月；沒送的月份就沒那列。
- **修訂 = upsert**：再送同一 `(cluster, role, month)` 覆蓋舊列（last-write-wins），非疊加
  （每列是「那個月加多少」，改就是改那個月）。
- **狀態存呼叫端**：帳本持久化 / upsert 由 **Go Scheduler Service** 負責；solver 無狀態，
  每次拿**當前完整帳本快照**重算。
- **horizon = 帳本裡實際出現的月份集合**（排序後 roll-forward），**不咬死 12 期 / 日曆年**。
  中間沒填的月份無需求、狀態原封帶過，不影響結果。

**月份三態語意（避免沉默被誤讀為 0）**：

| 帳本狀態 | 意義 | 報表呈現 |
|---|---|---|
| 沒有該列 | 未規劃（還不知道）| 標「未規劃」，不當 0 |
| 有列、需求全 0 | 確定不長 | 標「無成長」|
| 有列、某維度 > 0 | 有需求（0 的維度不約束）| 正常規劃 |

> 兩層 0 不同源：**列層級**全 0＝該月不長；**維度層級**單一維度 0＝該維度不設下限。

**重算永遠「從當下往未來」**：過去月份的 add 已執行、已 baked 進 in-stock（`used_capacity`），
故重算時 horizon 只含**當下及未來**的已填月份，過去自然退出模擬，乾淨處理「年中更新後半年」。

### 缺口 3e — 新舊節點一起打散（整個 cluster 的 anti-affinity）

**決議（岔路 B）：anti-affinity 作用範圍是「整個 cluster」，不是「這批新節點」。** 新長出來的
節點要與**現有節點一起**滿足打散，否則會長出全域傾斜的 cluster（且報表假綠燈）。

需要的資料：cluster 現有節點在每個 spread bucket 的**聚合數**（不需逐節點落點），
由 Go Scheduler Service 從 Inventory 聚合後帶入：

```python
class ExistingDistribution(BaseModel):
    cluster_id: str
    node_role: NodeRole
    spread_dimension: str                 # "ag" | "datacenter"
    counts_per_bucket: dict[str, int]     # {"ag-0": 3, "ag-1": 1, "ag-2": 1}
```

**Role-aware 強度（決議）**：

- **Master — 硬約束**：5 台 master 天然 `⌈5/3⌉=2` → **2/2/1**，正是既有 `AntiAffinityRule`
  auto-cap 公式 `ceil(N/buckets)`（`models.py:180`），N 用「現有 + 新增」總數。add-master
  時把現有 master 分佈當基線。
- **Worker — 軟約束**：傾斜就靠 add-node 慢慢 balance，不擋 solve。沿用既有 `target_spread`
  advisory 機制（`models.py:308`，達不到目標發 `spread_below_target` 而非失敗）+ 平衡目標
  （把新 worker 推向較空的桶）。

**優雅處理既有違規（master 硬約束的邊界）**：若現有分佈本身已違規（歷史遺留，如 ag-0 已 3 台
> cap=2），不因此讓整個 solve INFEASIBLE。約束改為「不准把桶推得更爆，容忍既有的爆」：

```
新增在桶 b 的數量 ≤ max(0, cap − 現有數[b])
  現有 ag-0=3、cap=2 → 新增 ≤ 0（不往 ag-0 加，但不 INFEASIBLE）
  現有 ag-1=1、cap=2 → 新增 ≤ 1
同時發 advisory：「ag-0 現有 master 超標，建議重排（超出本工具範圍）」
```

> ⚠️ 這是既有 splitter **刻意未做**的 topology-affinity-with-existing（`requirement-splitter.md`
> 決策 E）。本設計以「聚合基線數」的輕量形式補上，避免引入完整 `existing_vms` 模型。

### 缺口 3f — 現有 BM 上的 VM 佔用（per-BM，給 max_per_bm 用）

缺口 3e 的「每 AG 聚合數」只夠**跨 AG 打散**。但「**同一台 BM 上同 type VM 的數量上限**」
（`MaxPerBaremetalRule` / `auto_generate_max_per_bm`，`models.py:271`）是 **per-BM 粒度**，
聚合到 AG 就不夠 —— 必須知道「具體哪台 BM 已有幾台某 group 的現有 VM」。否則 solver 會把新
節點排到「資源夠、但 max_per_bm 已滿」的 BM 上，與 Go scheduler 真實放置打架。

不同約束對「現有 VM」的資料粒度需求：

| 約束 | 需要粒度 | 是否已涵蓋 |
|---|---|---|
| BM 容量 | 資源量 | ✅ `Baremetal.used_capacity` 已反映 |
| 跨 AG/DC 打散（3e）| 每桶聚合數 | 需 `ExistingDistribution` |
| 同 BM 同 type 上限（max_per_bm）| 每台 BM 的 group 佔用數 | ❌ 需 `ExistingBmOccupancy` |

**實際用途（決議）**：max_per_bm 主要用在 **master** —— group `(cluster_id, node_role=master)`、
`max_per_bm=1`（一台 BM 最多 1 個同 cluster master）。故每台 BM 對此 group 的現有數只會是
0/1，count-only 剛好且唯一必要。

```python
class ExistingBmOccupancy(BaseModel):
    baremetal_id: str
    # group key = (cluster_id, ip_type, node_role) → 該 BM 上現有同組 VM 數
    group_counts: dict[str, int]     # 例: {"clusterA||master": 1}
```

> **避免 double count**：資源面已由 `used_capacity` 蓋掉，故此資料**只帶 count、不帶資源**。
> 它專供「計數型約束」（max_per_bm、打散），與容量無關。
>
> **subsume 3e**：per-BM count 依 BM 的 AG 加總即得 3e 的每 AG 聚合數 —— 一份資料同時餵飽
> 打散與 max_per_bm。

**決議：solver 契約用「聚合 count」（Option A），不送完整 existing VM（Option B）。** 理由：

1. **夠用** —— master 1/BM、跨 AG 打散等所有現有約束，count-only 都足夠。
2. **與現有契約一致** —— Go scheduler 本來就送聚合 `used_capacity`（非每台 VM）；這份現況比照
   送聚合 count。詳細 VM 資料**留在 Go Scheduler Service 那層**，在邊界 aggregate 成 count 再送，
   solver 維持純函式、拿最小必要資訊。
3. **無 double count** —— 完整 existing VM 會與 `used_capacity` 重複算資源，得改寫容量模型
   （改用 `total_capacity` + pinned VM 消耗）；聚合 count 完全迴避。

> Option B（完整 `existing_vms`，即 splitter 決策 E 的 deferred 能力）列為未來，僅當出現
> 「需區分特定 VM 身分」的約束（指定 VM 的 failover 配對、逐 VM affinity）時才值得付代價。

**solver 改動極小**：max_per_bm 約束（`diagnostics.py:283` / solver 對應處）由
`Σ(新 VM) ≤ cap` 擴成 `existing_count[bm][group] + Σ(新 VM) ≤ cap`，加一個常數而已。

### 核心資料模型 (草案)

```python
# models.py 新增
class BaremetalType(BaseModel):
    type_id: str
    capacity: Resources
    fab: str
    # 理想化落點（決議 28）：桶內無上限；None → 自動推導 fab 現有 AG/DC
    target_buckets: list[str] | None = None
    # 未來：真實機位上限 ProcurementLandingZone(topology, max_bm) 於此擴充

class CapacityPlanRequest(BaseModel):
    demand_book: list[DemandEntry]    # 稀疏帳本；horizon = 其中出現的月份集合（排序）
    # periods 不再是輸入 —— 由 demand_book 的 distinct period 推導
    in_stock: list[Baremetal]
    procurement_types: list[BaremetalType]   # 每 fab 可多機型
    existing_distributions: list[ExistingDistribution] = []  # 現有節點每桶聚合數（缺口 3e）
    existing_bm_occupancy: list[ExistingBmOccupancy] = []     # 現有 VM per-BM 佔用（缺口 3f）
    config: SolverConfig              # 含 max_pods_per_node, vm_specs, headroom_reference_spec,
                                      #    procurement_spread_dimension, w_procurement_balance

# 規劃報表最細粒度 = (fab, bucket=AG/DC, month)；不下到個別 BM/rack
class BucketMonthCell(BaseModel):
    fab: str
    bucket: str                       # AG 或 DC（依 procurement_spread_dimension）
    period: str                       # 月
    node_adds: list[SplitDecision]    # 加幾台什麼 spec 的 node（給 owner/工程師）
    bm_procurement: list[dict]        # [{type_id, count}] 要買幾台 BM（給財務）
    shortfall_cause: str              # "capacity" | "anti_affinity" | "none"

class PeriodFabReport(BaseModel):     # fab × month 彙總（給長官頭條）
    fab: str
    period: str
    # 頭條：結果 + 成因（兩種台數分開）
    node_adds_total: int              # 本月本 fab 要加的 node 台數
    bm_procurement_total: int         # 本月本 fab 要買的 BM 台數
    shortfall_vms: int                # 缺幾台（需求 − 可落地）
    shortfall_cause: str              # "capacity" | "anti_affinity" | "none"
    cells: list[BucketMonthCell]      # 下鑽到 AG/DC × month
    # 證據 / 健康度（支撐採買論述）
    nominal_available: Resources      # 名目可用量（會高估）
    placeable_available: Resources    # 可落地可用量（真實）
    fragmentation_loss: Resources     # 名目 − 可落地
    balance_after: dict               # 採買後各 bucket 的 resulting_available

class CapacityReport(BaseModel):      # canonical JSON；Web UI 與 Excel 皆為其投影
    by_fab_period: list[PeriodFabReport]
    totals: dict                      # 跨 fab/period 彙總
    # 編預算視圖（Capacity 大臣用）：fab × DC × month → BM 台數（由 cells 投影）
    budget_view: list[dict]           # [{fab, dc, month, bm_count}]
```

### 新增 API

```
POST /v1/capacity/plan
  Request:  CapacityPlanRequest
  Response: CapacityReport
```

---

## Alternative & Trade-offs

### 替代方案 A：把 Pod 做成 `Resources` 的第五個資源欄位
- 在 `Resources` 加 `max_pods`，VM 視角=容量、需求視角=總量，比照 cpu/mem 走 coverage。
- **為何不選（現階段）**：因決議「全域同一 max-pods」，Pod 上限與 spec 無關，
  退化成節點數下限即可，加資源欄位會引入「placement 端要不要累加 pod」的非對稱複雜度
  （BM 沒有 pod 容量）。保留為未來 per-spec 需求出現時的升級路徑。

### 替代方案 B：每期獨立求解（不 roll-forward）
- 每期各自把「累計需求」對「全量庫存」重解一次。
- **為何不選**：會重排既有節點（不符「節點黏住」現實），且無法表達採買的時間遞延
  （Q1 買的機器 Q2 才生效）。roll-forward 雖然較複雜，但貼近實際運維。

### 替代方案 C：純試算表 / 離線報表，不進 solver
- 用平均裝箱率估算缺口。
- **為何不選**：忽略碎片與拓撲打散，正是現況「BM 台數失真」的根因；會系統性低估採買量。

### 替代方案 D：頭條用「名目資源貨幣」而非「可落地可用量」
- 直接加總 `available_capacity` 當頭條。
- **為何不選**：忽略碎片與拓撲，會出現「帳面夠卻擺不下」的矛盾，無法支撐採買論述。
  名目量保留為報表的「證據欄」（對照可落地量凸顯破碎損耗），但**不當頭條**。

---

## Risk & Mitigations

| 風險 | 類型 | 緩解 |
|---|---|---|
| 多期 × 多 fab × bin-pack 求解時間膨脹 | 技術 | 每期每 fab 獨立求解（天然可平行），單次規模小；採買 bin-pack 設 `max_solve_time` 並接受 FEASIBLE。 |
| 採買 template 的 topology 落點假設不準 | 營運 | 報表標註成因；採買的 topology_template 由規劃方明確指定，不由 solver 臆測。 |
| roll-forward 與真實上線順序不符 | 營運 | 設計為「規劃輔助」非「執行真相」；輸出標明假設前提。 |
| Pod 全域值未來變 per-spec | 技術 | 已預留升級路徑（替代方案 A），現有欄位語意不衝突。 |
| 既有 `split-and-solve` 契約變動 | 技術/組織 | 新功能走新 endpoint `/v1/capacity/plan`，舊 endpoint 與行為不動；`max_pods_per_node` 預設 0 = 停用。 |

---

## Rollout Plan

分階段、各自可獨立交付：

1. **Phase 1 — Pod 維度**：`SolverConfig.max_pods_per_node` + `ResourceRequirement.total_pods`
   + splitter 節點數下限。小、獨立、向後相容（預設停用）。
2. **Phase 2 — 採買量（單 fab、單期）**：殘量 bin-pack 成採買台數（**多機型選擇**）
   + 可落地可用量 / 破碎損耗 / 碎片率報表外露。先解決「該買幾台、怎麼證明」。
3. **Phase 3 — 多月 roll-forward + 多 fab 聚合**：完整 `CapacityPlanRequest/Report`、月級時間滾動、
   分層報表（頭條 + drill-down），新增 `/v1/capacity/plan`。
4. **Phase 4（未來，超出本提案）— 跨 fab 調撥**：見 Decision Log 末。

**回滾策略**：各 Phase 都是 additive（新欄位預設停用 / 新 endpoint）。
回滾 = 不呼叫新 endpoint、`max_pods_per_node=0`，既有行為完全不受影響。

---

## Open Question

仍待 reviewer 給意見的點（已決議者見 Decision Log）：

1. **參考 VM (`headroom_reference_spec`) 的選法**：可落地可用量要用哪個 spec 當量尺？
   建議在 config 指定一個全域 `headroom_reference_spec`（如 32c/256g）當「健康度儀表」的量尺；
   而採買缺口則一律用「該期實際需求」做 joint placement，不依賴參考 spec。reviewer 同意否？
2. **成因標籤的細緻度**：目前只分 `capacity` / `anti_affinity` 兩類。是否需要再細分
   （例如哪個拓撲維度、哪個 AG 卡住）？這會增加解釋負擔。

> 已消解的 Open Questions：
> - ~~drill-down 粒度到 Room/Rack~~ → 決議 #21：規劃報表最細到 AG/DC，不下 BM/rack。
> - ~~採買 topology 落點誰決定~~ → 決議 #28：理想化，solver 在 fab 的 AG/DC 桶內分配。

---

## Decision Log

| # | Decision | Reason | Follow-ups |
|---|---|---|---|
| 1 | Pod 上限用**單一全域值** `max_pods_per_node` | 與 spec 無關，退化成節點數下限即可，對 placement 端零侵入 | 未來若 per-spec 再走替代方案 A |
| 2 | 期別**月粒度**（12 期） | 規劃需要月級可見度；規模仍小 | — |
| 3 | 每 fab **可多採買機型** | 反映真實採購選項 | Phase 2 bin-pack 做機型選擇 |
| 4 | 現階段 **per-fab 自給自足**，不跨 fab 調撥 | 簡化、可平行；符合現況 | 未來跨 fab 調撥：編排層加跨 fab placement 選項（多 fab 候選池 + 調撥成本權重），列為 Phase 4 |
| 5 | 報表頭條用**可落地可用量**，名目量降為證據欄 | 名目量會因碎片/拓撲高估，無法支撐採買論述 | — |
| 6 | 缺口用**全 cluster joint placement** 算，不 by-cluster 加總 headroom | 各 cluster 搶同一 BM 池，加總會 double-count | — |
| 7 | 需求單可**忽略部分維度**（填 0 不約束） | splitter 既有行為（`splitter.py:171`） | 報表仍照實反映被忽略維度的真實佔用 |
| 8 | 採買單位 = **單台 BM**（非整櫃） | 符合實際採購顆粒度 | 落點理想化後由 solver 在桶內分配（Q1；見 #28）|
| 9 | 落點 = **L1：solver 在 fab 的 AG/DC 桶內分配** | 平衡「規劃可控」與「自動最佳化」 | 現階段理想化無機位上限（Q2；見 #28）|
| 10 | 平均維度**可選 `ag` 或 `datacenter`**，用 `procurement_spread_dimension` | 兩者皆在 `SPREAD_DIMENSIONS`；覆蓋 AG→實體 DC 轉換（3AG 結果可 1:1 對應實體 DC）| — |
| 11 | 「平均」平衡的是**採買後結果可用量**，非採買台數 | 要考慮 in-stock 現況才精準，補在最短的桶 | 軟目標 `w_procurement_balance` |
| 12 | 現階段**單一最佳建議**，不做 what-if 多情境 | 先求可用 | 未來 what-if：外層迴圈疊多情境比較報表（L2）|
| 13 | 需求是**增量語意**（加多少），無 `demand_mode` | sizing 不需 cluster 現況，僅 placement/採買需 BM 現況 | — |
| 14 | 需求單只裝 **User 意圖**；現況一律走系統（**Go Scheduler Service** 整合 Inventory）| provenance 分工；user 無法從 UI 填現況 | Inventory 只需回「每 AG 聚合數」即可，不需逐節點 |
| 15 | anti-affinity 作用範圍 = **整個 cluster**（新舊一起打散）| 對齊實際 HA 心智；只平衡新批次會長出全域傾斜 | 補 splitter 未做的 existing-baseline（缺口 3e）|
| 16 | **Role-aware**：master 硬（2/2/1）、worker 軟（advisory + balance）| master 是 quorum、worker 是容量；worker 靠 add-node 慢慢平衡 | — |
| 17 | master 硬約束**容忍既有違規**（`new[b] ≤ max(0, cap−existing[b])` + advisory）| 歷史傾斜不該擋住加機器，但也不弄更糟 | **已定：用容忍版 (a)** |
| 18 | 帶入**現有 VM 的 per-BM 佔用**（count-only）給 max_per_bm 用（缺口 3f）| 聚合到 AG 不夠；否則規劃 placement 會與真實 max_per_bm 打架 | **已定** |
| 19 | max_per_bm 主要用在 **master**：`(cluster, master)` cap=1 | 一台 BM 最多 1 個同 cluster master；故 count 只 0/1，count-only 足夠 | — |
| 20 | solver 契約用**聚合 count**（Option A），非完整 existing VM（Option B）| 夠用 + 與現有 `used_capacity` 聚合契約一致 + 迴避 double count；詳細留在 Go Scheduler 層邊界聚合 | Option B（完整 `existing_vms`）列未來，僅特定-VM 身分約束才需 |
| 21 | 報表最細粒度 = **`(fab, AG/DC, month)`**，不下到個別 BM/rack | 規劃 vs 執行分階段；BM 級擺放是執行時 `solve` 的事 | 大幅減複雜度；對齊「3 AG 平均」語言 |
| 22 | 報表對象收斂成 **2 個**：Capacity 大臣（主，含編預算視圖）、長官 | 財務＝Capacity 大臣同一角色；採購不用管；工程師 rack 級 drill-down 移出規劃範圍 | — |
| 23 | 分開 **node 台數** 與 **BM 台數** 兩欄 | node 給 owner 建 VM；BM 給財務編預算；N node 可能只需 K≤N BM | — |
| 24 | 形式 **canonical JSON 優先**，Web UI 先做、Excel 後補 | UI/Excel 皆為 JSON 薄投影，不各寫一套；複用 `app/web_static/` | 趨勢圖可後補 |
| 25 | 需求存成**稀疏帳本** `list[DemandEntry]`，每列 `(cluster,role,月)`，修訂=upsert | user 可任意送某幾月、可年中修訂 | 帳本持久化由 Go Scheduler 負責，solver 無狀態 |
| 26 | 月份**三態**：無列=未規劃、全 0=不長、某維度>0=有需求 | 讓報表誠實區分「沉默」與「明確不長」 | 兩層 0（列層級 vs 維度層級）分開 |
| 27 | horizon = **帳本出現的月份**，不咬死 12 期/日曆年 | user 填幾月就規劃幾月；重算永遠從當下往未來 | 過去月份已 baked 進 in-stock，自動退出模擬 |
| 28 | 採買落點採**理想化**：桶內機位無上限、桶集合=fab 現有 AG/DC | 消掉「誰維護落點資料」；規劃/編預算階段夠用 | 真實機位上限（`max_bm`＋landing-zone 維護）列未來；桶集合仍受現實限制（不能憑空生 AG）|

### 未來展望：跨 fab 調撥（Phase 4，超出本提案範圍）
保留升級路徑。屆時把「每 fab 一個獨立庫存池」放寬成「跨 fab 候選池 + 調撥成本」，
編排層在 joint placement 時允許需求落到他 fab，並對跨 fab placement 加權懲罰
（避免無謂搬遷）。本提案的 per-fab 迴圈結構不需重寫，只需在候選 BM 推導與目標函數擴充。
