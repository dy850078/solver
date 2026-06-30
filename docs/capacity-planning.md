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

### 缺口 3a — 多期 roll-forward

- `CapacityPlanRequest.periods`：有序的期別（1–12 月或 Q1–Q4）。
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

### 缺口 3c — 報表維度（回答「該用什麼維度看資源長相」）

**不要再用 BM 台數當單一指標。** 報表分三層：

1. **資源貨幣（主指標）** — per (fab, period) 的 vCore / Mem / Storage / Pods，分 total / used / available。
   這是需求語言，owner / sponsor 都看得懂。
2. **有效剩餘量（最實用）** — 「這個 fab 還能再長幾台標準 VM（如 32c/256g）」=
   對剩餘空間做 bin-pack。直接回答「還能加幾台 node」，且自動把碎片納入（裝不下就是裝不下）。
3. **碎片率（健康度）** — `stranded = Σ_bm (裝不下最小標準 VM 的剩餘空間)`，
   正是現有 `slot_score` 概念（`solver.py:864`），把它從 objective 內部指標**外露成報表欄位**。
   - 碎片率高 → 該整併或調整 spec 策略。
   - 碎片率低且 available 也低 → 該採買。

### 核心資料模型 (草案)

```python
# models.py 新增
class BaremetalType(BaseModel):
    type_id: str
    capacity: Resources
    fab: str
    topology_template: Topology = Topology()

class PeriodDemand(BaseModel):
    period: str                       # "2026-Q3" / "2026-07"
    fab: str
    requirements: list[ResourceRequirement]   # 含 total_pods

class CapacityPlanRequest(BaseModel):
    periods: list[str]                # 有序期別
    demands: list[PeriodDemand]
    in_stock: list[Baremetal]
    procurement_types: list[BaremetalType]
    config: SolverConfig              # 含 max_pods_per_node, vm_specs

class PeriodFabReport(BaseModel):
    fab: str
    period: str
    resource_currency: dict           # total/used/available × 4 維
    effective_headroom_vms: int       # 還能再長幾台標準 VM
    fragmentation: dict               # stranded 量 + 碎片率
    demand: Resources
    shortfall: Resources
    procurement_bm_count: int
    shortfall_cause: str              # "capacity" | "anti_affinity" | "none"

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
2. **Phase 2 — 採買量（單 fab、單期）**：殘量 bin-pack 成採買台數 + 碎片/有效剩餘 slot 報表外露。
   先解決「該買幾台」。
3. **Phase 3 — 多期 roll-forward + 多 fab 聚合**：完整 `CapacityPlanRequest/Report` 與時間滾動，
   新增 `/v1/capacity/plan`。

**回滾策略**：三個 Phase 都是 additive（新欄位預設停用 / 新 endpoint）。
回滾 = 不呼叫新 endpoint、`max_pods_per_node=0`，既有行為完全不受影響。

---

## Open Question

1. **採買機型策略**：每個 fab 的「標準採買機型」是單一機型還是多機型可選？
   多機型會讓 Phase 2 的 bin-pack 變成機型選擇問題（仍可解，但搜尋空間變大）。
2. **期別粒度**：報表要做到「月」還是「季」？月粒度 12 期 × 多 fab，求解次數較多但仍小規模。
3. **有效剩餘量的「標準 VM」定義**：用哪一個 spec 當量尺？建議用該 cluster 類型最常見的
   spec，或在 config 指定一個 `headroom_reference_spec`。
4. **跨 fab 調撥**：需求能否跨 fab 滿足，還是每個 fab 自給自足？目前設計假設**自給自足**
   （per-fab 獨立），若允許調撥需在編排層加跨 fab 的 placement 選項。
5. **Pod 全域值來源**：`max_pods_per_node` 由 config 帶入即可，是否需要支援「不同 K8s 版本/
   cluster 用不同全域值」？目前設計是單一全域值。

---

## Decision Log (Review 後補)
- **Decision**: （待填）
- **Reason**: （待填）
- **Follow-ups**: （待填）
