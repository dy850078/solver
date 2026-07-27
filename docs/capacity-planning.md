# Enhancement Proposal — K8s 資源擴充需求計算 (Capacity Planning)

> **作者**: Claude (claude-code)
> **日期**: 2026-06-30（2026-07-27 審閱勘誤，見〈實作對照勘誤〉）
> **狀態**: Phase 1–3 已實作（`app/capacity_planner.py`、`/v1/capacity/procure`、
> `/v1/capacity/plan`）；**缺口 3e/3f 僅完成設計、尚未實作**；backlog 見文末。
> **相關元件**: `app/capacity_planner.py`、`app/splitter.py`、`app/split_solver.py`、
> `app/solver.py`、`app/models.py`、`app/diagnostics.py`
> **實作走讀**: 本文記錄「為什麼這樣設計」；程式碼從頭到尾怎麼運作
> （變數/約束/目標/滾動流程的教學走讀）見 `docs/reading-guide-capacity-planner.md`。

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

### 前史：Excel 容量帳，與它賴以成立的三個假設

在 solver 介入之前，容量規劃靠一份**人工維護的龐大 Excel**：登錄各 fab 的 in-stock
清單與各 cluster 的需求，逐月推算出「每個 fab × month × network 要採買幾台 BM」。
這套做法過去行得通，不是因為 Excel 擅長裝箱，而是因為三個**隱含假設**讓 bin-packing
退化成簡單除法：

| 隱含假設 | 讓計算簡化成什麼 | 現況 |
|---|---|---|
| 一台 BM **一次性擺滿** VM | 「幾台 VM」÷ 固定裝箱率 = 「幾台 BM」；不存在殘餘容量再利用的問題 | ❌ VM 分批長、每台 BM 碎片程度不同，「剩餘價值」台台不同 |
| BM **單一機型** | 容量換算只有一個除數 | ❌ 多機型並存，per-type 容量不同，還要選型 |
| cluster **不共用 BM** | 各 cluster 各算各的帳、最後相加 | ❌ 多 cluster 搶同一實體池，獨立加總會 double-count（見缺口 3c 的正確性陷阱） |

三個假設**同時失效**後，「需求 ÷ 單機容量」這條 Excel 算式就系統性失真 —— 這正是
下文「BM 台數這個舊指標會失真」的流程面根源。除了算不準，Excel 流程還有四個
結構性痛點：

1. **正確性靠人工**：in-stock 快照、逐月滾動、機位扣減全是手動；fab × cluster ×
   month 規模成長後，一格錯全盤錯，且無從稽核是哪一格。
2. **給不出「為什麼缺」**：算式只能回台數，無法區分 capacity / anti_affinity /
   space 成因；採買論述靠人腦補，對長官與財務缺乏舉證力。
3. **約束根本不在帳裡**：拓撲打散、BGP 隔離、master 1/BM 這些真實限制 Excel
   表達不了 —— 於是出現「帳面夠、實際擺不下」的矛盾。
4. **無法驗證可行性**：solver 的解本身就是「真的放得進去」的證明（每台 VM 都有
   落點）；Excel 是估算，無法舉證，也無法回答 what-if。

> 本提案之後，Excel 的角色從「計算引擎」降級為「輸入/交換格式」：需求仍可從
> Excel 貼上匯入（決議 #42 的長表 CSV/TSV），但計算與舉證交給 solver。
> 這也是〈替代方案 C：純試算表〉落選的完整理由。

### 為什麼非做不可：規劃腦與執行腦脫鉤的死局

比「Excel 算不準」更根本的問題是**結構性的**：目前「該不該買、買幾台、放哪」由
人腦＋Excel 決定（**規劃腦**），而「VM 實際落到哪台 BM」由 solver＋Go scheduler
決定（**執行腦**）。兩顆腦用的規則不同——人腦看名目容量，引擎執行 C1–C5 全部
約束。凡是「規劃認為可以、執行認為不行」的差集，都會在**最糟的時點**爆開：
需求已經到門口，而補救手段（採買、擴機位、開 AG）的 lead time 以月計。

**失誤成本的不對稱**是這個問題致命的原因：

- 執行期的錯誤以**分鐘**為單位補救（換一台 BM 重試）。
- 規劃期的錯誤以**季**為單位補救（採買 lead time、機房工程）。
- 因此規劃腦的約束意識必須 **≥** 執行腦；否則整條鏈上最嚴格的一關（執行）
  永遠**最後**才發現不可行——而那正是修正成本最大的時刻。

三種具體死局，根因相同（規劃時沒有執行約束）：

1. **假充足 → 到期才 INFEASIBLE**：帳面總容量夠 → 判定不採買。三個月後需求
   真的來了，執行 solver 回 INFEASIBLE——master 要跨 3 個 AG 打散，ag-2 卻沒
   空間。容量帳看不見拓撲。此刻採買才啟動，需求斷供一整個 lead time；若 fab
   根本只有 2 個 AG，這甚至不是買機器能解的（要開新 AG），死局更深。
2. **買對數量、買錯形狀**：人腦算出「缺 6 台」沒錯，但 6 台全進了有空位的
   ag-0、或全買了 CPU 型、或進了錯的 BGP 域。錢花了，執行時這批機器對
   **觸發採買的那個需求**不可用（打散/機型/網路域擋住），第二次採買再排一個
   lead time。「數量對、形狀錯」從報表上看不出來——台數帳是平的。
3. **想像中的挪移**：人腦的盤算裡「把 A 挪去那台就擠得下」，但執行語意是
   **節點黏住不重排**（替代方案 B 落選的同一現實）、master 1/BM 擋住合併。
   規劃裡合法的步數在執行裡是非法的——這份計畫**從未真正可執行過**，
   只是一直沒人驗證。

共同解法即本提案的核心策略：**讓規劃與執行用同一顆腦**。規劃期的採買
bin-pack 就是執行期的 placement solver（同一套 C1–C5、同一個 splitter、
joint solve），於是：

- 規劃說「夠」＝ 存在一份真實的 assignment（**可行性證明**，不是估算）；
- 規劃說「缺」＝ 附帶成因與位置（capacity / anti_affinity / space），在
  lead time 還來得及的時點，把「採買／擴機位／開 AG」的行動指派給正確的人；
- 買進來的機器**保證**對觸發它的需求可用——因為落點本身就是在打散、機型、
  BGP 約束下解出來的。

一句話總結：**把「執行時才會撞到的牆」搬到規劃時就撞——撞牆成本從一季，
變成一次 solve。**

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

### 缺口總覽（design review 首場用：先需求、後實作）

| 缺口 | 需求（一句話） | 為什麼需要（沒有它會怎樣） | 處理方式（設計摘要） | 狀態 |
|---|---|---|---|---|
| **1** Pod 維度 | 一台 node 的 Pod 有上限，sizing 要保證節點數容得下總 Pod 需求 | K8s 有 max-pods-per-node 實限；只按 CPU/Mem 算出的台數可能資源夠、Pod 塞不下 | 全域 `max_pods_per_node` → 節點數下限 `⌈pods/max⌉` 併入 splitter count 下限；placement 端零改動 | ✅ |
| **2** 規劃模式（採買）| 還沒買機器時也要算得出「該買幾台」 | 既有引擎只能放進固定庫存（候選 BM 必填），回答不了採買量 | `BaremetalType` 機型模板 + `ProcurementCap` 桶機位上限；虛擬可採買 BM 進 joint solve，被用到的就是採買數 | ✅ |
| **3a** 多期 roll-forward | 這個月的放置與採買要影響下個月的帳 | 單發 solve 無時間維；Q1 買的機器 Q2 才是庫存，不滾動會把同一批容量/機位賣兩次 | 每 (fab, 月) 一次 solve；庫存消耗、買機 materialize 成下月 in-stock、機位遞減、已購池 drain | ✅ |
| **3b** 採買量算法 | 「買幾台、什麼型、放哪個桶」要可證明，不是估算 | 平均裝箱率忽略碎片與拓撲打散，系統性算錯採買量（替代方案 C 落選的原因） | 殘量 + 虛擬 BM 一次 CP-SAT 聯合解（機型選擇 + 落點 + 數量）；分層目標：台數最少 → 桶間平衡 | ✅ |
| **3c** 報表與解釋性 | 頭條要「可落地可用量」；缺口要講得出成因 | 名目加總系統性高估 →「帳面夠卻擺不下」；只回台數說服不了長官與財務 | solve 結果本身舉證 + 四儀表（nominal / slots / stranded / balance）+ 結構化 `ShortfallDetail`（capacity / anti_affinity / space）| ✅（graceful partial、`space` 的桶標註待補）|
| **3d** 需求單與 Provenance | User 只填意圖；現況一律由系統帶入 | User 填不了也不該填 cluster 現況；固定 12 期不符實務；「沒填」被誤讀成 0 會做錯決策 | `DemandEntry` 稀疏帳本、upsert 修訂、月份三態、horizon = 帳本月份；現況由 Go Scheduler 整合 Inventory 帶入 | ✅ |
| **3e** 新舊節點一起打散 | anti-affinity 的作用範圍是整個 cluster，不是這批新節點 | 只平衡新批會長出全域傾斜的 cluster，報表還是綠燈（假陰性） | `ExistingDistribution` 每桶聚合數當基線；master 硬約束（容忍既有違規）、worker 軟約束 | ⚠️ 設計已定、未實作 |
| **3f** 現有 VM 的 per-BM 佔用 | max_per_bm 要把既有 VM 算進去 | 否則規劃會把新 master 排到「資源夠但 1/BM 已滿」的機器上，與真實放置打架 | `ExistingBmOccupancy`（count-only，免 double count）；約束左邊加常數即可 | ⚠️ 設計已定、未實作 |
| **3g** BGP 網路域隔離 | 整個 cluster 統一住某個 BGP 的機器 | 一個 AG 混多個 BGP，「往 ag-0 買」有歧義；cluster 只看得到自己 BGP 那部分容量 | 放置靠既有 `candidate_baremetals` 過濾（零改動）；採買與計量以 `(bucket, network)` 配對為單位 | ✅ |
| **3h** 已採購庫存 | 已買未上架的機器要先用完，才建議買新的 | 回答「已買各 100 台夠不夠、各型還缺幾台」；否則採買建議會重複花錢 | `CommittedStock` 低權重層；三層順序 in-stock → committed → 新買 | ✅ |

> 另有兩項**設計已定、列 backlog**：機隊事件簿（建新拆舊，決議 #40）與
> no-buy what-if 情境（決議 #43 第二層）——review 時可作為「下一步」收尾。

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
     remaining_node_slots { 還能再長幾台參考 VM }
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

**決議：機位上限 `max_bm` 掛在「桶（AG/DC）」層級**，因為你們就是 by AG / by DC 看分散、
不在意 ag-0 裡的哪個 rack（rack 級落點由 DC Hardware Team 張羅）。落點 = 打散桶本身，
不需 zone→bucket 映射。

```python
class BaremetalType(BaseModel):
    """可採買的 BM 機型 (無固定 id)。"""
    type_id: str
    capacity: Resources
    fab: str                          # 歸屬廠區 (= topology.site 或 phase)

class ProcurementCap(BaseModel):
    """某桶的採買機位上限；缺此桶 → 理想化無上限。（缺口 3g 另補 network 欄位）"""
    fab: str
    bucket: str                       # AG 或 DC 的值，如 "ag-0"
    max_bm: int                       # 該桶還能加幾台 BM（總數，不分 rack、不分機型）
```

- **in-stock**：沿用既有 `Baremetal`（有 id / used_capacity / topology）。
- **procurement**：規劃層先盡量放進 in-stock，殘量才在桶內生成新 BM，受 `max_bm[bucket]` 上限。
- **理想化為 per-bucket fallback**：有給 `max_bm` 的桶受限、沒給的桶無限。故可混用
  —— 假定機位夠的 fab 不給、盤點過的 fab 給精準值。
- `candidate_baremetals` 由規劃層**依 fab/桶自動推導**，維持 splitter 契約不破壞。

> **兩個 caveat**：
> 1. `max_bm` 限的是**桶內容量**；**桶集合仍是現實 AG/DC** —— 不能憑空生不存在的 AG。故某 fab
>    只有 2 AG 卻要 3 副本打散時，採買**仍會卡**（`ShortfallDetail.cause=anti_affinity`）。
> 2. 桶買滿 `max_bm` 仍放不下 → `cause=space`，partial + advisory（見缺口 3c），
>    交由 DC Hardware Team 擴充機位；不硬性 INFEASIBLE。

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
added_resources[b] = Σ_t buy[t,b] × cap_t          # 採買替桶 b 新增的「資源量」(非台數)
resulting_available[b] = in_stock_available[b] + added_resources[b] − demand_placed[b]

軟目標: minimize (max_b resulting_available[b] − min_b resulting_available[b])
```

> **`× cap_t` 的意義**：`buy[t,b]` 是**台數**，`cap_t` 是一台 type-t 的**資源**（如 64 vCore），
> 相乘＝這些機器帶來的**資源**。平均比的是資源可用量（5 台大機 ≠ 5 台小機），故要換算成資源。
> 單一機型時台數與資源成正比可省略。
>
> **同一個 `buy` 有兩種加總、勿混**：
> - **max_bm 機位約束**比**台數**：`Σ_t buy[t,b] ≤ max_bm[b]`（一格一台）。
> - **balance 目標**比**資源**：`Σ_t buy[t,b] × cap_t`（可用量看資源多寡）。

這讓採買「考慮 in-stock 現況」自動補在最短的桶，達成真正的平均，而非帳面平均。

### 缺口 3a — 多期 roll-forward

- **月粒度**；horizon = **`demand_book` 中出現的月份集合**（排序），不咬死 12 期（見缺口 3d）。
- 多 fab × 多月一次算：因 per-fab 自給自足，各 (fab, period) 為獨立求解、天然可平行。
- 每期帶**增量需求**（該期新增的 cluster 需求），不是累計值。
- **庫存狀態逐期滾動**：第 N 期 placement 把 BM 的 `used_capacity` 推進，
  第 N+1 期在這個基礎上繼續放（節點黏住、不重排）。採買進來的 BM 變成下一期的 in-stock。
- **機位也逐期滾動**：第 N 期採買消耗 `max_bm[bucket]`，第 N+1 期看到的是遞減後的值
  （避免多月超賣同一批機位）。
- 實作上每期就是一次既有 `solve_split_placement(allow_partial_placement=True)`，
  期間用回傳的 assignments 更新 `used_capacity` 與 `max_bm` 後傳入下一期。

### 缺口 3b — 採買量算法

殘量（這期 in-stock 放不下的 VM）連同「候選落點內的虛擬可採買 BM」一起丟進一次 CP-SAT solve：

- **變數**：`bm_buy_used[t, b, k]` BoolVar — type `t` 在桶 `b` 的第 k 台是否採買。
  k 上限 = `min(max_bm[b], 殘量最多需要幾台)`；桶無 `max_bm` 時只由後者封頂（理想化）。
  展開成每台虛擬 BM 讓 `max_per_bm=1`（master 1/BM）自然成立。沿用既有 `assign[vm, bm]`。
- **機位約束**：`Σ_{t,k} bm_buy_used[t,b,k] ≤ max_bm[b]`（有給的桶才加此約束）。
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
>
> **實作勘誤（2026-07-27）**：`placeable_available` / `fragmentation_loss` /
> `shortfall_vms` 三個草案欄位**未做成獨立報表欄位**。實作的分工是：
> - 「可落地」的舉證由 **joint solve 結果本身**承擔（success / `shortfalls` /
>   `procurement` —— 需求真的放進去了沒、缺在哪），不另跑一次「bin-pack 參考 VM 至
>   INFEASIBLE」的探測 solve。
> - 儀表欄位為 `nominal_available` / `remaining_node_slots` / `stranded_available` /
>   `balance_after`（`models.py:600-604`）。注意 `remaining_node_slots` 是
>   **per-BM 貪婪 `_fits_count` 加總**（`capacity_planner.py:582`），**不含 anti-affinity
>   與跨 BM 交互** —— 它是尺標值（本節「純參考」的定位），不是表格中定義的那個
>   「帶 anti-affinity bin-pack」的可落地量，兩者勿混。

#### 報表對象與粒度（決議）

**規劃 vs 執行是兩個階段** —— 規劃報表的最細粒度**停在 `(fab, AG/DC, month)` 的台數**，
**不下到某台 BM / 某 rack**。個別 VM 落哪台 BM 是**執行階段** `solve` / splitter 的事，不是
規劃報表的產物。這正好對齊「3 AG 平均」的思考語言，也砍掉大量複雜度。

**兩個對象**（財務＝Capacity 大臣同一角色；採購不納入；工程師 rack 級 drill-down 移出規劃範圍）：

| 對象 | 要看的 | 粒度 | 台數類型 |
|---|---|---|---|
| **Capacity 大臣**（主要消費者，含編預算）| 完整規劃：各 fab 各 AG/DC 各月 → 加幾台 node ＋ 買幾台 BM ＋ 成因；其中「各 fab 各 DC 各月買幾台 BM」即編預算視圖 | fab × AG/DC × 月 | node ＋ BM |
| **長官** | 缺口 + 採買 + 成因（彙總）| fab × 季/年 | 主要 BM |

**成因要結構化 + 指到哪個桶/維度 + 一句人話**（只給三個字使用者會無所適從）：

```python
class ShortfallDetail(BaseModel):
    cause: str                 # "capacity" | "anti_affinity" | "space"
    bucket: str | None = None  # 哪個桶（AG/DC），如 "ag-0"
    dimension: str | None = None   # capacity→"cpu_cores"/…；anti_affinity→"ag"/"datacenter"
    needed: int | None = None
    available: int | None = None
    message: str               # 一句可讀說明
```

| 成因 | message 例 | 使用者行動 |
|---|---|---|
| `space` | 「ag-0 機位用罄：需 8 台、上限 5」| 找 DC Hardware Team 在 ag-0 擴機位 |
| `anti_affinity` | 「需 3 個 AG 打散，此 fab 只有 2 個」| 開新 AG / 放寬打散 |
| `capacity` | 「記憶體不足：缺 512 GiB（CPU/Storage 夠）」| 挑高記憶體機型 |

- `anti_affinity` / `space` 的 bucket 與數字直接來自既有 `DiagnosticsBuilder`
  （`diagnostics.py` 的 `_check_anti_affinity_feasibility` 已回傳 `reachable_buckets` /
  `min_buckets_needed`）；`space` 另由「理想化 vs 有 max_bm 兩解比對」判定。
- `capacity` 的綁死維度 = 哪個資源維度總需求超過總供給最多。
- 一個缺口可能**同時多個成因**（沒機位 + 記憶體也不夠）→ 報表帶 `list[ShortfallDetail]`。

編預算視圖（fab × DC × 月 → BM 台數）是 Capacity 大臣的一個投影，不是獨立角色。

#### 兩種「台數」必須分開

- **Node/VM 台數**：某 AG 某月要「加幾台 K8s node」→ Capacity 大臣轉述 owner/工程師、
  Cluster Owner 拿去建 VM（對映 `SplitDecision`）。
- **BM 台數（採買）**：某 DC 某月要「買幾台實體機」→ 財務大臣編預算。

兩者不同（N 台 node 可能共住、只需 K≤N 台 BM）。報表**兩欄分開**，避免財務/長官看錯。

#### 形式：canonical JSON 優先，UI 與 Excel 都是薄投影

先產出結構化的 `CapacityReport` JSON，**Web UI 和 Excel 都只是它的投影**，不各寫一套邏輯：

- **Web UI**（先做）：複用既有 `app/web_static/`；規劃報表本質是 fab × AG/DC × 月 的表格
  + 篩選 + 長官頭條。表格中等工作量，趨勢圖可後補。
  ✅ **已實作**（`/ui/report.html`，深色 shadcn-minimal 風格，與 Topology Visualizer 互相導覽）：
  頭條 stat tiles（狀態/node 台數/BM 買數/committed/求解時間）、Fab × Month 表格（狀態 chips：
  OK/SPACE/CAPACITY/ANTI-AFFINITY/BLOCKED，點列下鑽）、月明細（採買 tags、split decisions、
  結構化 shortfalls、健康指標、(bucket, network) cells + CPU 利用率 meter）、Budget view
  （逐月 bar strip + 明細表）。範例 `examples/capacity/plan_two_fabs.json`。
- **Excel**（後補）：資料本為表格狀，從同一份 JSON 匯出 xlsx/csv 很容易，加個 endpoint 即可。

#### 「可落地可用量」為什麼不能 by-cluster 加總（正確性陷阱）

不同 cluster 有不同 spec、不同候選 BM、不同 anti-affinity，且**搶同一個實體 BM 池**。
各算各的 `remaining_node_slots` 再相加會 double-count（A 說還能放 5 台、B 也說 5 台，指的是同一塊空間）。

因此兩種用途分開：

1. **算採買缺口** → **不要先算 `remaining_node_slots` 再相減**。把該期**所有 cluster 的需求一起丟進
   該 fab 共用庫存做一次 joint placement**，殘量就是缺口。一次解，contention 自然處理。
2. **健康度儀表 `remaining_node_slots`（純參考）** → 用「fab/桶層級 + 單一全域 `reference_vm_spec`」
   算粗略的「若全拿來長參考 VM 可長 N 台」，並**明講這是尺標值、不是各 cluster 加總**。

by-cluster 的可見度，改用**結果導向**呈現（這個 cluster 需求滿足了沒／缺幾台），
而不是發明一個能相加的 slot 數。

**參考 spec 選 A（單一全域），不 per-role（決議 #34）**：因為 spec 不只 role 不同、連同 role 跨
cluster 都不同，per-role 尺標也不「真」。既然都不可能真，就用一個**清楚一致的共同尺標**。精準度
落在該精準處 —— **實際 sizing/placement/採買用需求自帶的真實 per-role spec**（既有能力，見下），
`reference_vm_spec` 只是儀表的尺，刻意不追求 per-cluster 精準。

> **各 role 不同 spec 的支援性（設計檢視）**：**現有即支援**。`ResourceRequirement.vm_specs`
> （`models.py:400`）每個 role 可帶自己的 spec pool，splitter `_resolve_specs`（`splitter.py:99`）
> 「有就用、沒有 fallback config.vm_specs」（splitter 決策 C）。`SplitDecision` 本就 by role 分開。
> `DemandEntry.vm_specs` 沿用此機制。故 master 用一組、worker 用另一組是既有能力，不需新增設計。

#### 碎片率（健康度副指標）

`stranded = Σ_bm (裝不下 min_useful_spec 的剩餘空間)`，即「名目 − 可落地」的細分，
正是現有 `slot_score` 概念（`solver.py:864`），把它從 objective 內部指標**外露成報表欄位**。

> **用 `min_useful_spec`（最小可用 spec）而非 `reference_vm_spec`（決議 #34）**：碎片是「連最小的
> 都塞不下」才算真浪費；若用代表性 spec 會把「塞不下 32c 但塞得下 8c」的可用空間誤判為浪費。
>
> **命名切開撞名**：現有 `w_headroom`/`headroom_upper_bound_pct`（`models.py:311`）是「單台 BM 別
> 塞超過 90%」的利用率餘裕，與此處「還能長幾台」無關。故本儀表用 `remaining_node_slots` /
> `reference_vm_spec`，不沿用 `headroom` 字樣。

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
    allowed_bm_types: list[str] | None = None  # 決議 #38：per-cluster 限採買機型；None=fab 內任何機型
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

> ⚠️ **實作狀態（2026-07-27）：設計已定、尚未實作。** `ExistingDistribution` 不存在於
> `models.py`，`CapacityPlanRequest` 也沒有 `existing_distributions` 欄位。目前規劃 solve
> 的 anti-affinity 只作用在「本次新增的節點」上，現有節點分佈不參與 —— 使用時要知道
> 這個限制（歷史傾斜的 cluster 可能被規劃出「新批平衡、全域仍傾斜」的結果）。

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

> ⚠️ **實作狀態（2026-07-27）：設計已定、尚未實作。** `ExistingBmOccupancy` 不存在於
> `models.py`。目前 max_per_bm 約束只計「本次新增的 VM」，不含既有 VM 的 per-BM 佔用。

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

### 缺口 3g — 網路域 (BGP) 隔離

「整個 cluster 統一住某個 BGP 的 rack」= **每個 cluster 的 VM 只能落在該 BGP 網路域的 BM 上**。
這是**放置資格過濾（filter / affinity）**，且因「全 cluster 統一」是 per-cluster 單純過濾。

> **BGP 是 filter，不是 spread 維度** —— cluster 固定在一個 BGP 內、不跨 BGP 打散。故
> **不進 `SPREAD_DIMENSIONS`**，走「候選過濾」那條路。

**sizing / placement / 打散：現有機制已滿足。** 靠既有 `candidate_baremetals`
（`models.py:135`、`ResourceRequirement.candidate_baremetals:403`）：

- Go Scheduler 填 cluster A 的候選時只放 **BGP1 的 BM** → solver 自然只放 BGP1，零改動。
- 打散自動正確：`reachable_buckets` 由候選 BM 算（`diagnostics.py:136`），只在「有 BGP1 rack
  的 AG」間展開。
- 與 provenance 一致：BGP 是系統已知屬性，由 Go Scheduler 填候選，**不需 user 填**。

**採買：需補 BGP-aware。** 採買往 AG/DC 桶生成虛擬 BM；若一個 AG 底下混 BGP1/BGP2，「往 ag-0
買」有歧義。故 `ProcurementCap`（與 BM）加 `network` 標籤，cluster A 的殘量只買進符合其 BGP 的桶：

```python
class ProcurementCap(BaseModel):
    fab: str
    bucket: str
    network: str = ""     # BGP 域，如 "bgp1"；採買只在符合 cluster network 的桶生成
    max_bm: int
```

> **決議：AG 與 BGP 交錯（一 AG 混多 BGP）→ `network` 必需。** 推論：**容量規劃的有效計量
> 單位是 `(AG/DC × BGP)` 配對**，不是單純 AG —— cluster A（BGP1）只看得到 ag-0 的 BGP1 那部分。
> 打散仍在 AG（cluster 在自己 BGP 內跨 AG 打散），但**容量帳 / 採買 / 報表以 `(bucket, network)`
> 為單位**（見缺口 3c 報表加 network 維度）。

### 缺口 3h — 已採購庫存 (pre-committed stock)｜已納入（effort 低）

情境：某廠已買好兩種機型各 100 台，想確認「夠不夠、不夠各再買多少」。這是既有採買模型的
自然延伸 —— **把已購庫存當成「近零成本的採買層」**：

- 已購池：每型上限 = 已購台數，成本權重**低但非 0**；新買 = 一般採買，權重高。
- 目標 `minimize`：**先用 in-stock、再用已購、最後才買新的**。
- **實作勘誤（vs 原設計「權重 0」）**：實際是三層權重 in-stock (0) →
  committed (`w_committed_stock=100`) → buy (`w_procurement=10_000`)（`models.py:339-343`）。
  已購權重若真設 0，solver 會把已購機和 in-stock 視為無差別，可能留著 in-stock 空間
  不用、先開已購機；設一個遠小於 `w_procurement` 的小權重才能保住
  「先填滿現有、再拆封已購、最後才花錢」的順序。
- 報表直接回答：**「用掉 100 台裡的 X 台、還缺 → 各機型再買 Y 台」**（`bm_procurement.from_committed`）。

```python
class CommittedStock(BaseModel):      # 選填輸入（空=不啟用）；已採購但待分配的庫存
    fab: str
    type_id: str
    count: int                        # 已買幾台
    bucket: str | None = None         # 已上架則填（→ 等同 in_stock）；浮動則 None（solver 決定落點）
    network: str | None = None
```

兩種擺法：已上架（知道 AG/BGP）→ 直接當 `in_stock`；未上架（浮動）→ 當已購池，solver 決定落點
（受 cluster BGP 限制）。**effort 低，已納入範圍**（`committed_stock` 為空時等同不啟用）。

### 核心資料模型 (草案)

> ⚠️ **本節是設計時的草案，與最終實作有出入；以 `app/models.py` 為準。** 主要差異：
> `CapacityPlanRequest` 無 `existing_distributions` / `existing_bm_occupancy`（3e/3f 未實作）；
> `BucketMonthCell` 的 `node_adds` / `bm_bought` 是**計數**而非 list（spec 細分在 period 層
> `split_decisions`）；`ShortfallDetail.cause` 實際有六值（另有 `unknown` / `input_error` /
> `blocked`）；`budget_view` 是 `list[BudgetRow]` 且**含 `type_id`**（財務看得到買什麼機型）。
> 完整落差清單見〈實作對照勘誤〉。

```python
# models.py 新增
class BaremetalType(BaseModel):
    type_id: str
    capacity: Resources
    fab: str                          # 1U 假設；未來 GPU 多 U 用 rack_units 擴充

class ProcurementCap(BaseModel):      # 缺口 2：桶機位上限（缺此桶 → 理想化無上限）
    fab: str
    bucket: str                       # AG 或 DC 的值
    network: str = ""                 # 缺口 3g：BGP 域；採買只在符合 cluster network 的桶生成
    max_bm: int

class CapacityPlanRequest(BaseModel):
    demand_book: list[DemandEntry]    # 稀疏帳本；horizon = 其中出現的月份集合（排序）
    # periods 不再是輸入 —— 由 demand_book 的 distinct period 推導
    in_stock: list[Baremetal]
    procurement_types: list[BaremetalType]   # 每 fab 可多機型
    procurement_caps: list[ProcurementCap] = []  # 桶機位上限；缺 → 該桶理想化無上限（缺口 2）
    committed_stock: list[CommittedStock] = []   # 已採購待分配庫存（缺口 3h，選填）
    existing_distributions: list[ExistingDistribution] = []  # 現有節點每桶聚合數（缺口 3e）
    existing_bm_occupancy: list[ExistingBmOccupancy] = []     # 現有 VM per-BM 佔用（缺口 3f）
    config: SolverConfig              # 含 max_pods_per_node, vm_specs, reference_vm_spec,
                                      #    min_useful_spec, procurement_spread_dimension,
                                      #    w_procurement_balance

# 規劃報表最細粒度 = (fab, bucket=AG/DC, network=BGP, month)；不下到個別 BM/rack
class BucketMonthCell(BaseModel):
    fab: str
    bucket: str                       # AG 或 DC（依 procurement_spread_dimension）
    network: str                      # BGP 域（缺口 3g：計量單位是 (bucket, network)）
    period: str                       # 月
    in_stock: dict                    # {total, used, available} Resources（by AG/BGP 現況）
    node_adds: list[SplitDecision]    # 加幾台什麼 spec 的 node（給 owner/工程師）
    bm_procurement: list[dict]        # [{type_id, count, from_committed}] 買幾台（含已購庫存 3h）
    shortfalls: list[ShortfallDetail] = []   # 結構化成因（可多個），見缺口 3c

class PeriodFabReport(BaseModel):     # fab × month 彙總（給長官頭條）
    fab: str
    period: str
    # 頭條：結果 + 成因（兩種台數分開）
    node_adds_total: int              # 本月本 fab 要加的 node 台數
    bm_procurement_total: int         # 本月本 fab 要買的 BM 台數
    shortfall_vms: int                # 缺幾台（需求 − 可落地）
    shortfalls: list[ShortfallDetail] = []   # 彙總各 bucket 的結構化成因
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
| 採買機位假設不準（理想化桶或 `max_bm` 過時）| 營運 | 有 `max_bm` 的桶用精準值、缺的桶理想化；買滿仍缺標 `space` 成因交 DC Hardware Team；機位逐月 roll-forward。 |
| roll-forward 與真實上線順序不符 | 營運 | 設計為「規劃輔助」非「執行真相」；輸出標明假設前提。 |
| Pod 全域值未來變 per-spec | 技術 | 已預留升級路徑（替代方案 A），現有欄位語意不衝突。 |
| 既有 `split-and-solve` 契約變動 | 技術/組織 | 新功能走新 endpoint `/v1/capacity/plan`，舊 endpoint 與行為不動；`max_pods_per_node` 預設 0 = 停用。 |

---

## Rollout Plan

分階段、各自可獨立交付：

1. **Phase 1 — Pod 維度**：`SolverConfig.max_pods_per_node` + `ResourceRequirement.total_pods`
   + splitter 節點數下限。小、獨立、向後相容（預設停用）。✅ **已實作**
   （`models.py` 兩欄位、`splitter.py::_pod_node_floor` + 併入 count 下限、
   `tests/test_splitter.py::TestPodDimension` 5 測試；placement 端零改動）。
2. **Phase 2 — 採買量（單 fab、單期）**：殘量 bin-pack 成採買台數（**多機型選擇**）
   + 可落地可用量 / 破碎損耗 / 碎片率報表外露。先解決「該買幾台、怎麼證明」。
   ✅ **已實作**（`app/capacity_planner.py::solve_capacity_plan`、`/v1/capacity/procure`、
   `tests/test_capacity_planner.py` 20 測試）：
   多機型 + 每桶 `max_bm`（跨機型與 committed 以 `solver.bm_group_caps` 共同計數）+
   優先順位 in-stock → committed（`w_committed_stock`）→ 新買（`w_procurement`）+
   `space` 成因（capped vs uncapped 比對）+ **BGP network 過濾**（`(bucket, network)` 格，
   `Baremetal.network` / `ResourceRequirement.network`）+ **committed stock**（3h，bucketed 或
   floating pool）+ **平衡目標** `w_procurement_balance`（結果可用 CPU 的 max−min）+
   **健康指標**（`nominal_available` / `remaining_node_slots` / `stranded_available` /
   `balance_after`，用 `reference_vm_spec` / `min_useful_spec`）。
   **仍延後**：graceful partial（現以兩解比對報 `space`，非 soft coverage）。
3. **Phase 3 — 多月 roll-forward + 多 fab 聚合**：完整 `CapacityPlanRequest/Report`、月級時間滾動、
   分層報表（頭條 + drill-down），新增 `/v1/capacity/plan`。
   ✅ **已實作**（`capacity_planner.py::solve_capacity_horizon`、10 個 horizon 測試）：
   稀疏帳本 `DemandEntry`（三態月份、horizon=帳本月份）、per-fab 獨立滾動
   （`fab_topology_dimension`，預設 site）、逐月 roll-forward（庫存消耗、買/領機器
   materialize 成下月 in-stock 並改 id 防碰撞、機位 `max_bm` 遞減、已購池 drain）、
   `PeriodFabReport`（頭條 + `ShortfallDetail` + 健康指標）+ `(bucket, network)` cell
   drill-down + `budget_view`（fab × DC × 月 → 台數）+ totals。
   順帶補上決議 #38 `allowed_bm_types`（DemandEntry / ResourceRequirement）。
   **註**：失敗月份的需求**不會**帶入下月（修輸入重算，文件化於 docstring）；
   graceful partial 仍為未來項。cell 的 node_adds 為總數（spec 細分在 period 層
   `split_decisions`），較設計草案簡化。
4. **Phase 4（未來，超出本提案）— 跨 fab 調撥**：見 Decision Log 末。

**回滾策略**：各 Phase 都是 additive（新欄位預設停用 / 新 endpoint）。
回滾 = 不呼叫新 endpoint、`max_pods_per_node=0`，既有行為完全不受影響。

---

## 實作對照勘誤（2026-07-27 審閱）

逐節核對本文件與 `app/capacity_planner.py` / `app/models.py` / `app/solver.py` 的結果。
分三類：**(A) 文件說了、code 沒做**；**(B) 文件與 code 不一致（以 code 為準）**；
**(C) code 做了、文件沒寫（補充）**。

### A. 設計已定、尚未實作

| 項目 | 現況 | 影響 |
|---|---|---|
| 缺口 3e `ExistingDistribution` | 類別不存在；`CapacityPlanRequest` 無此欄位 | 規劃 solve 的打散只作用在新增節點；歷史傾斜 cluster 可能得到「新批平衡、全域仍斜」的假綠燈 |
| 缺口 3f `ExistingBmOccupancy` | 類別不存在 | max_per_bm 只計新 VM；規劃結果可能與 Go scheduler 真實 max_per_bm 打架 |
| 3c graceful partial | 失敗月無部分放置結果（Phase 2 註已載明） | 缺口量化靠 what-if 數字，非逐 VM partial |

### B. 文件與程式碼不一致（以程式碼為準）

| 文件敘述 | 程式碼現況 |
|---|---|
| 3h：已購庫存「成本權重 0」 | 三層權重 in-stock (0) → committed (`w_committed_stock=100`) → buy (`w_procurement=10_000`)；權重 0 會讓已購與 in-stock 無差別（`models.py:339-343`、`solver.py:1049-1068`）|
| 3c：報表欄位 `placeable_available` / `fragmentation_loss` / `shortfall_vms` | 未實作；改由 solve 結果本身舉證 + `nominal_available` / `remaining_node_slots` / `stranded_available` / `balance_after` 四個儀表（`models.py:600-604`）|
| `ShortfallDetail.cause` 三值 | 六值：另有 `unknown`（時限未證明 INFEASIBLE）、`input_error`、`blocked`（`models.py:726-727`）|
| `ShortfallDetail.bucket`「指到哪個桶」 | 欄位存在但**目前從未被填**（`capacity_planner.py::_shortfall_details`）——`space` 成因還說不出「哪個桶機位用罄」，是已知待補項 |
| 草案 `w_procurement_balance: int = 3` | 預設 **0**（opt-in；`models.py:349`），要平衡目標需顯式開啟 |
| 草案 `budget_view: [{fab, dc, month, bm_count}]` | `BudgetRow{fab, bucket, network, period, type_id, bm_count}` —— 按機型細分（`models.py:736`）|
| Phase 3 註「失敗月需求不帶入下月」 | 更強：失敗月**之後該 fab 的所有已規劃月份不再求解**，回 `BLOCKED` stub（`capacity_planner.py::_blocked_report`），理由是結構性污染 —— 缺了失敗月需求的後續解會過度樂觀 |
| `models.py:522` `ProcurementCap.network` docstring「reserved; unused」 | **已過時**：network 實際參與 cell 推導、機位計數、roll-forward 遞減。待 follow-up 修正（動 `models.py` 需走 ADR 流程，故本次僅記錄）|

### C. 程式碼有、文件未載的實作細節（補充）

- **`space` 判定的兩道守則**（`capacity_planner.py:117-137`）：只有**證明 INFEASIBLE** 才
  進入成因分類；UNKNOWN（時限到）回 `unknown`，不亂扣帽子。Pass-2（拿掉 caps 重解）只在
  請求真的帶 `procurement_caps` 時才跑。
- **虛擬 BM 的合成 rack（vrack）**：每台可採買/已購 BM 掛獨立 `vrack-<id>`（新買機器總能
  分開上架，rack 落點是 DC Hardware Team 的事，決議 #29），避免 rack 級 anti-affinity 把
  同 cell 的採買機誤判為同 rack。roll-forward materialize 時 id 加 `acq-<period>-` 前綴、
  vrack 同步改名，防止下月重新生成的虛擬 BM 撞名/撞 rack（`capacity_planner.py:187-204,822-830`）。
- **worst-case 槽位上界是「sound」的**：每 cell 生成幾台虛擬 BM，用
  `spec_count_upper_bound`（已含 pod floor / min-max 台數）÷ per-BM 可裝數（再被
  max_per_bm / rack-spread cap 收緊）逐 spec 加總；naive `ceil(總需求/機型容量)` 會低估
  （64c 機只裝得下一台 40c VM），把可行方案誤判成缺口（`capacity_planner.py::_worst_case_counts`）。
- **機位上限的實作機制**：`max_bm` 不是逐台約束，而是 `solver.bm_group_caps` ——
  對一組 BM 的 `bm_used` 指示變數加 `Σ used ≤ cap`，同一機制同時管「桶機位（跨機型 +
  已購共同計數）」與「浮動已購池總量 ≤ count」（`solver.py:788-797`）。
- **balance 目標的桶播種**：只用**真實 in-stock BM** 播種桶集合；純虛擬桶會貢獻 avail=0
  把 min 釘死在 0，讓 (max−min) 退化成「minimize max」（`solver.py:1091-1106`）。
- **顯式 VM pass-through**：`ProcurementRequest.vms` 的候選清單不動、永不落在虛擬 BM 上
  （scheduler 的過濾是權威）；會驅動採買的需求一律走 `requirements`（`models.py:556-559`）。
- **`fab=""` 單廠模式**：demand book 全空 fab = 整池規劃；混用空/具名 fab 會被
  validator 拒絕（同批機器被兩個獨立滾動狀態賣兩次）；具名模式下 caps / committed
  必須具名 fab，機型目錄可不具名（`models.py:688-721`）。
- **已購池精準 drain**：`committed_entry_used` 按 committed_stock **entry index** 記帳，
  roll-forward 扣的正是 solver 實際抽用的那筆（`models.py:579-582`）。
- **INPUT_ERROR 前置檢查**：committed / allowed_bm_types 引用不存在機型、需求無任何可達
  候選（含 network 沒有任何 cell 宣告過的情形）→ 帶可行動訊息的 INPUT_ERROR，
  不會靜默當成 capacity 缺口（`capacity_planner.py:63-111,372-396`）。

---

## Open Question

**目前無未決 Open Question** —— 三條線 + BGP/庫存均已定案（見 Decision Log）。

> 已消解的 Open Questions：
> - ~~drill-down 粒度到 Room/Rack~~ → 決議 #21：規劃報表最細到 AG/DC，不下 BM/rack。
> - ~~採買 topology 落點誰決定~~ → 決議 #29：`max_bm` 掛桶層級，solver 在桶內分配。
> - ~~成因標籤細緻度~~ → 決議 #33：結構化 `ShortfallDetail`（指到桶/維度 + 人話）。
> - ~~參考 spec 選法~~ → 決議 #34：單一全域 `reference_vm_spec`；碎片用 `min_useful_spec`。
> - ~~AG 是否對齊 BGP~~ → 決議 #37：交錯，`network` 必需，計量單位 `(AG, BGP)`。

---

## Decision Log

| # | Decision | Reason | Follow-ups |
|---|---|---|---|
| 1 | Pod 上限用**單一全域值** `max_pods_per_node` | 與 spec 無關，退化成節點數下限即可，對 placement 端零侵入 | 未來若 per-spec 再走替代方案 A |
| 2 | 期別**月粒度** | 規劃需要月級可見度；規模仍小 | 期數不固定 12，見 #27（帳本驅動 horizon）|
| 3 | 每 fab **可多採買機型** | 反映真實採購選項 | Phase 2 bin-pack 做機型選擇 |
| 4 | 現階段 **per-fab 自給自足**，不跨 fab 調撥 | 簡化、可平行；符合現況 | 未來跨 fab 調撥：編排層加跨 fab placement 選項（多 fab 候選池 + 調撥成本權重），列為 Phase 4 |
| 5 | 報表頭條用**可落地可用量**，名目量降為證據欄 | 名目量會因碎片/拓撲高估，無法支撐採買論述 | — |
| 6 | 缺口用**全 cluster joint placement** 算，不 by-cluster 加總 `remaining_node_slots` | 各 cluster 搶同一 BM 池，加總會 double-count | — |
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
| 28 | ~~一律理想化無上限~~ → **改為：理想化是 per-bucket fallback，`max_bm` 是精準模式** | 理想化會產出虛胖/不符現況的採買數（user 修正）| 見 #29–#32 |
| 29 | `max_bm` 掛在**桶（AG/DC）層級**，非 rack | 你們 by AG/DC 看分散，不在意桶內哪個 rack；rack 級由 DC Hardware Team 張羅 | `ProcurementCap{fab, bucket, max_bm}` |
| 30 | 機位**逐月 roll-forward**（採買消耗、遞減）| 避免多月超賣同一批機位 | — |
| 31 | 新增第三種成因 **`space`**（桶機位用罄）| 讓報表能講「空位買滿仍缺 → 要擴機房」；partial+advisory 不硬 fail | `space` 由「理想化 vs 有 max_bm 兩解比對」判定 |
| 32 | 機型佔位先當 **1U（1 台=1 格）** | 先簡化 | 未來 GPU 多 U 用 `rack_units` + 每型 U 數，結構不變 |
| 33 | 成因**結構化** `ShortfallDetail`（cause + 桶 + 維度 + needed/available + 人話），可多筆 | 只給三個字使用者無所適從；需指到哪個桶/維度才知道要幹嘛 | 多數欄位既有 diagnostics 已算，只需外露 |
| 34 | 健康儀表用**單一全域 `reference_vm_spec`**（非 per-role）；碎片用**獨立 `min_useful_spec`** | spec 連同 role 跨 cluster 都不同，per-role 也不真；實際數字已用需求真實 spec，儀表只是共同尺 | 各 role 不同 spec 為既有能力（`ResourceRequirement.vm_specs`）|
| 35 | 儀表**改名切開撞名**：`remaining_node_slots` / `reference_vm_spec` / `min_useful_spec` | 與既有 `w_headroom`（單台 BM 利用率餘裕）語意不同，避免混淆 | `w_headroom` 維持原名 |
| 36 | **BGP 網路域隔離**：sizing/placement/打散靠既有 `candidate_baremetals` 過濾（現有滿足）；採買加 `ProcurementCap.network` 標籤 | cluster 統一住一 BGP＝per-cluster filter；BGP 是 filter 非 spread 維度 | 見 #37 |
| 37 | AG 與 BGP **交錯** → `network` **必需**；容量/採買/報表計量單位＝**`(AG/DC, BGP)`** | 一 AG 混多 BGP，cluster 只看得到自己 BGP 那部分 | 報表 cell 加 `network` 維度 + `(AG,BGP)` in-stock/used/available |
| 38 | **per-cluster 限採買機型** `DemandEntry.allowed_bm_types` | 不同 cluster 可買不同機型；in-stock 端已由候選過濾，採買端需機型允許清單 | 小改動：殘量只生成 allowed 機型的虛擬 BM |
| 39 | **已採購庫存**（缺口 3h，effort 低已納入）：當「零成本採買層」，先用完再買新 | 回答「已買各 100 台夠不夠、各再買多少」 | `CommittedStock`；已上架→in_stock、浮動→已購池；空＝不啟用 |
| — | 命名修正：`bought[b]` → `added_resources[b]`；釐清 `buy` 的兩種加總（台數 for max_bm、資源 for balance）| 避免 reviewer 卡在 `×cap_t` 的語意 | — |
| 40 | **建新拆舊（fleet events）**：先只做 `release`（整台除役還池）、事件月前**擋新節點**；設計已定、**列 backlog 暫不實作** | roll-forward 目前單向消耗，表達不了「某月機器從舊 cluster 除役變回可用」 | 見下方〈Backlog：機隊事件簿〉 |
| 41 | **UI 定位 = B：檢視 + 微調 + 模擬工具**；正式需求帳本住 Go Scheduler 端 | 20 fab × 20 cluster 規模需要持久化/權限/稽核/並行編輯，在無狀態 sidecar 長儲存層違反 #25；UI 保留 what-if 沙盒、報表視覺化、除錯 demo 三角色 | 演進：UI 加 localStorage 草稿 → Go 端帳本 API 好後加 Load/Save book 按鈕，UI 始終無狀態 |
| 42 | **需求規模化輸入 = 長表 CSV/TSV 匯入**（Excel 貼上），**匯入取代全部**、**重複鍵報錯**；格子語意維持資源總量（GiB）；需求網格（demand lines grid）**後補** | 20 fab 的需求已活在 Excel；長表與 `DemandEntry` 一比一，同時是未來與 Go 端帳本的交換格式 | 欄位：`fab, cluster, role, period, cpu_cores, memory_gib(或 memory_mib 擇一), storage_gb, pods, spec(目錄名，空=Any), network`；cluster/period 必填、欄序不拘、未知欄警告忽略、全有或全無 |
| 43 | **「不採買累計缺口」= 每月採買量的前綴和**（純 UI 變換，不另跑情境） | 每月採買量本身就是該月的邊際缺口（solver 買的是滿足需求的最少台數），per-fab running sum 即「都不買的話到 M 月累計缺幾台」；回答「說服採買」的緊迫性論述 | 未規劃月攜帶前值（淡色標注）；**失敗月繼續累計其 what-if 採買量並從此標 `≥`（下界）**——先前的缺口不因該月失敗而消失，且該月真實缺口可能大於 what-if（capacity/AA 成因時無法量化）。此表本身即 what-if 情境，納入失敗月 what-if 與「totals 只計成功月」不衝突。節點級 no-buy what-if 見下方 Backlog |

### 未來展望：跨 fab 調撥（Phase 4，超出本提案範圍）
保留升級路徑。屆時把「每 fab 一個獨立庫存池」放寬成「跨 fab 候選池 + 調撥成本」，
編排層在 joint placement 時允許需求落到他 fab，並對跨 fab placement 加權懲罰
（避免無謂搬遷）。本提案的 per-fab 迴圈結構不需重寫，只需在候選 BM 推導與目標函數擴充。

### Backlog：no-buy 情境（what-if 第一例，決議 #43 的第二層）
「累計缺口 = 採買前綴和」給的是**機器台數**語言。若要「都不買的話**哪些 cluster 的
幾個節點**放不下」的節點級衝擊，需要真正的 no-buy 情境跑法：關閉採買（committed
仍可用——已付費）、失敗月不 BLOCK 後續而是逐月量化未滿足需求。牽動兩個核心語意
（partial placement、stop-after-failure 的情境豁免），即決議 #12 延後的 what-if
多情境的第一個具體實例；實作前走 plan-first 設計流程。

### Backlog：機隊事件簿（fleet events）— 建新拆舊（決議 #40，設計已定、暫不實作）

**情境**：某月把幾台 BM 從舊 cluster 除役，容量釋放回 in-stock 供後續月份使用。
現況做不到：`in_stock.used_capacity` 是期初快照，roll-forward 只會消耗容量
（placement 累加、cap 遞減、買機落地），沒有任何「某月釋放/移除機器」的事件。
今日唯一 workaround 是斷成兩次 solve、手動改 in-stock，報表會斷開。

**設計**（與 demand book 平行的事件簿）：

```json
"fleet_events": [
  { "period": "2026-03", "fab": "fab-a", "action": "release", "bm_ids": ["bm-7", "bm-8"] }
]
```

已定的三個語意選擇：

1. **事件範圍：只做 `release`**（除役還池）。`retire`（整台汰除離隊）與
   `add`（非採買到貨）留到有需求再擴充——同一事件簿結構直接加 action 即可。
2. **粒度：整台釋放**。事件月起該機 `used_capacity` 歸零；不做部分釋放
   （表達力換輸入簡單，「整台從舊 cluster 除役」符合實際操作）。
3. **事前隔離：擋住**。排定 release 的機器在事件月**之前**退出候選池
   （舊負載照算佔用、但不接新節點），事件月起以乾淨姿態回歸。
   避免「二月建上去、三月被清掉」的自相矛盾計畫，也天然免除
   「先放置後除役」的衝突驗證。

**實作要點**（動 `solve_capacity_horizon` 的 per-fab 迴圈）：
- 每月 solve 前套用該月事件：release → `used_capacity = Resources()`。
- 事前隔離：對 `period < 事件月` 的 solve，把待除役機從候選推導剔除
  （仍計入 in-stock 報表快照，gauges 如實反映其舊負載）。
- 驗證：`bm_ids` 引用不存在的機器、或與具名 fab 範圍不符 → `INPUT_ERROR`。
- 報表：事件月的 month detail 標註「released N machines」；釋放後容量
  自然流入 nominal / slots / balance_after / cells。
- UI：表單加「Fleet events」區（月份 + 機器群組挑選）。
