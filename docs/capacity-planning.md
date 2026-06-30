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

### 缺口 2 — 規劃模式：用「BM 機型 template」描述供給

新增供給的兩種來源：

```python
class BaremetalType(BaseModel):
    """可採買的 BM 機型 (無固定 id，數量無上限)。"""
    type_id: str
    capacity: Resources
    fab: str                       # 歸屬廠區 (= topology.site 或 phase)
    topology_template: Topology    # 採買後落在哪個 room/rack/ag 的策略
```

- **in-stock**：沿用既有 `Baremetal`（有 id / used_capacity / topology）。
- **procurement template**：`BaremetalType`，規劃層先盡量放進 in-stock，殘量才用 template 算採買台數。
- 規劃模式下 `candidate_baremetals` 由規劃層**依 fab/topology 自動推導**（不再要求呼叫方填），
  維持 splitter 既有的「候選必填」契約不破壞 —— 編排層在呼叫 splitter 前補齊它。

**決議：每個 fab 可有多個採買機型可選。** 採買 bin-pack 因此是「機型選擇 + 數量」的聯合問題
（見缺口 3b）。CP-SAT 天然能解：對每個 type 各開一組虛擬可採買 BM，由目標函數挑出總台數
（或未來加權成本）最小的組合。搜尋空間變大但每次規劃的規模仍小（單 fab × 單月）。

### 缺口 3a — 多期 roll-forward

- `CapacityPlanRequest.periods`：有序的期別（**決議：月粒度，12 期**）。
- 多 fab × 多月一次算：因 per-fab 自給自足，各 (fab, period) 為獨立求解、天然可平行。
- 每期帶**增量需求**（該期新增的 cluster 需求），不是累計值。
- **庫存狀態逐期滾動**：第 N 期 placement 把 BM 的 `used_capacity` 推進，
  第 N+1 期在這個基礎上繼續放（節點黏住、不重排）。採買進來的 BM 變成下一期的 in-stock。
- 實作上每期就是一次既有 `solve_split_placement(allow_partial_placement=True)`，
  期間用回傳的 assignments 更新 `used_capacity` 後傳入下一期。

### 缺口 3b — 採買量算法

殘量（這期 in-stock 放不下的 VM）丟進一個小 bin-packing：
以標準採買機型為箱子，**minimize 啟用箱數** → 即「這個 fab 這季要買幾台 BM」。

這等同把 solver 反過來用：建立 `bm_buy_used[t, k]` BoolVar（第 k 台 type-t 機型是否採買），
demand 固定、目標 `minimize Σ bm_buy_used`，並沿用既有 capacity / anti-affinity 約束。
可直接在 `solver.py` 的 CP-SAT 模型上加一組「虛擬可採買 BM」實現，復用 `bm_used` 機制
（`solver.py:770`）。

缺口成因標註複用 `DiagnosticsBuilder._constraint_layer_check()`（`diagnostics.py:308`）：
能區分缺口是 `capacity`（純資源不足）還是 `anti_affinity`（拓撲/AG 打散不足）造成。

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

#### 分層報表：頭條收斂成「結果 + 成因」，多維度下沉成 drill-down

解釋性的原則是**不論底下有幾個拓撲維度，講故事的人手上永遠只有 3 個數字 + 1 個原因**：

- **頭條（給長官 / sponsor）** — per (fab, period)：
  「能再長 N 台 node ／ 本期需求 M 台 ／ 缺 M−N 台」＋ **成因標籤**
  （`capacity` 容量不足 vs `anti_affinity` 拓撲打散不足）。成因標籤直接複用
  `DiagnosticsBuilder._constraint_layer_check()`（`diagnostics.py:308`），它本來就能分這兩層。
- **Drill-down（給工程師 / 採購落點）** — 才展開 by DC / Room / Rack 的資源貨幣、碎片分布。
  採買最終要落到某個 room/rack，這層有用，但它是附錄、不是頭條。

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

### 缺口 3d — User 需求單 (Demand Form)

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
class DemandForm(BaseModel):              # user 填寫，編排層轉成 ResourceRequirement
    cluster_id: str
    fab: str
    period: str                           # "2026-07"
    # 填 0 / 省略 = 該維度不約束（但不代表 VM 該維度用量為 0）
    cpu_cores: int = 0
    memory_mib: int = 0
    storage_gb: int = 0
    pod_count: int = 0
    node_role: NodeRole = NodeRole.WORKER
    # 顯式指定希望的 VM 規格；省略則用 config.vm_specs 由 solver 選
    vm_specs: list[Resources] | None = None
    min_total_vms: int | None = None
    max_total_vms: int | None = None
```

### 核心資料模型 (草案)

```python
# models.py 新增
class BaremetalType(BaseModel):
    type_id: str
    capacity: Resources
    fab: str
    topology_template: Topology = Topology()

class PeriodDemand(BaseModel):
    period: str                       # 月粒度，如 "2026-07"
    fab: str
    requirements: list[ResourceRequirement]   # 含 total_pods

class CapacityPlanRequest(BaseModel):
    periods: list[str]                # 有序期別（12 個月）
    demands: list[PeriodDemand]       # 多 fab × 多月，逐 (fab, period) 獨立求解
    in_stock: list[Baremetal]
    procurement_types: list[BaremetalType]   # 每 fab 可多機型
    config: SolverConfig              # 含 max_pods_per_node, vm_specs, headroom_reference_spec

class PeriodFabReport(BaseModel):
    fab: str
    period: str
    # 頭條：結果 + 成因
    placeable_headroom_vms: int       # 可落地：還能再長幾台參考 VM
    demand_vms: int                   # 本期需求換算台數
    shortfall_vms: int                # 缺幾台
    shortfall_cause: str              # "capacity" | "anti_affinity" | "none"
    procurement: dict                 # {type_id: count}，可多機型
    # 證據 / 健康度
    nominal_available: Resources      # 名目可用量（會高估）
    placeable_available: Resources    # 可落地可用量（真實）
    fragmentation_loss: Resources     # 名目 − 可落地
    # drill-down（附錄）：by DC/Room/Rack 的資源貨幣與碎片
    breakdown: list[dict] = []

class CapacityReport(BaseModel):
    by_fab_period: list[PeriodFabReport]
    totals: dict                      # 跨 fab/period 彙總
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
2. **drill-down 粒度**：頭條收斂成「結果 + 成因」，drill-down 要展到 DC、Room 還是 Rack？
   愈細愈精準但報表愈長。建議預設展到 Room，Rack 視需要。
3. **成因標籤的細緻度**：目前只分 `capacity` / `anti_affinity` 兩類。是否需要再細分
   （例如哪個拓撲維度、哪個 AG 卡住）？這會增加 drill-down 的解釋負擔。
4. **採買 topology 落點**：多機型採買後落在哪個 room/rack/ag，由 `topology_template` 指定。
   是否需要讓 solver 也參與「買來放哪裡最不破碎」的決策，還是由規劃方固定指定？

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

### 未來展望：跨 fab 調撥（Phase 4，超出本提案範圍）
保留升級路徑。屆時把「每 fab 一個獨立庫存池」放寬成「跨 fab 候選池 + 調撥成本」，
編排層在 joint placement 時允許需求落到他 fab，並對跨 fab placement 加權懲罰
（避免無謂搬遷）。本提案的 per-fab 迴圈結構不需重寫，只需在候選 BM 推導與目標函數擴充。
