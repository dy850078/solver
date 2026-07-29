# Go 端 E2E 實作說明（G1–G7）— 給實作 agent 的交接文件

> **用途**：把這份文件餵給在 Go Scheduler repo 工作的 agent。**solver repo
> 在本地可讀**——本文內嵌的 JSON 契約只是速查，**與 code 有出入時一律以
> solver repo 的 `app/models.py` 為準**（Pydantic 模型即合約）。跑起來的
> solver 另有 `GET /openapi.json` 機器可讀 schema。
> **日期**：2026-07-29。Solver 端 S1–S6 全數落地，本文所列契約皆已上線。

## Solver repo 閱讀地圖（按需查證，不必通讀）

| 要查什麼 | 讀哪裡 |
|---|---|
| 任何欄位的權威定義、預設值、驗證規則 | `app/models.py`（每個模型的 docstring 含語意） |
| 可直接 POST 的請求樣本 | `examples/capacity/plan_dedicated_pool.json`、`plan_fleet_release.json`、`reconcile_basic.json`；其餘 `examples/` |
| 端點清單與 handler | `app/server.py`（很短） |
| 規劃/roll-forward 的行為語意 | `app/capacity_planner.py::solve_capacity_horizon` docstring |
| reconcile 的計算與歸因規則 | `app/reconcile.py`（模組與各函式 docstring） |
| G7 回放判定邏輯（可直接翻譯成 Go） | `tests/test_consistency_replay.py::replay` + 檔頭差集表 |
| 契約的行為範例（每條規則都有對應測試） | `tests/test_capacity_planner.py`、`tests/test_reconcile.py` |
| 設計依據與決議編號 | `docs/e2e-vision.md`（#1–#25）、`docs/capacity-planning.md`（#1–#43）、`docs/decisions/ADR-001..006` |
| 本地把 solver 跑起來 | `make install && make run`（:50051）；單發 CLI：`make cli INPUT=examples/...` |

驗證手段優先序：**跑測試/實 POST 範例 > 讀 models.py > 讀本文**。

---

## 0. 邊界原則（不可違反）

1. **Solver 是無狀態純函式**：POST 進什麼算什麼，不存任何東西。所有持久化
   （需求帳本、plan 存檔、執行落帳、排程）都在 Go 端。
2. **HTTP 錯誤分兩層**：請求層驗證失敗（欄位格式、引用完整性）→ **422**
   （Pydantic）；業務層失敗 → **200 + `success:false`**，狀態字串以
   `INPUT_ERROR:` / `INFEASIBLE` / `BLOCKED:` 開頭。Go 要 branch 這些前綴，
   **不要 parse 訊息內文**（內文是給人看的，會改）。
3. **每個 response 都帶 `config_fingerprint`**（sha256 前 12 碼，涵蓋生效
   config + 引擎/or-tools 版本）。Go 對每次呼叫落帳這個值——它是 reconcile
   歸因 config/引擎漂移的依據。
4. 金額、採購單號、資產對應、人員權限：全部 Go/上游的事，solver 契約裡沒有。

## 1. Solver 端點總表

| 端點 | 用途 | Go 呼叫時機 |
|---|---|---|
| `POST /v1/capacity/plan` | 多月規劃：需求帳本 → 每 fab 每月 roll-forward 報表 | 每月 canonical run（G6）、手動重跑 |
| `POST /v1/capacity/procure` | 單次採買 sizing（無月概念） | 臨時 what-if；正式流程走 /plan |
| `POST /v1/capacity/reconcile` | plan vs actual 對帳（純函式） | 週 cron / 月底正式 / 手動（G6） |
| `POST /v1/placement/solve`、`/v1/placement/split-and-solve` | 執行期放置（既有，契約未變） | Cluster 建置 / add-node（S7） |
| `GET /openapi.json`、`GET /health` | schema / 健康檢查 | 部署與 codegen |

**placement 契約沒有 demand_id 欄位**——demand_id 是 Go 內部 metadata
（UI 選單 → 執行落帳），不進 solver、不出 solver。

---

## 2. 共用資料模型（JSON 契約速查）

以下是 Go 端需要組裝/解讀的模型（權威版本在 `app/models.py`，出入以彼為準）。
省略的欄位（anti_affinity_rules 等）沿用既有 placement 契約。所有 `period`
一律 `"YYYY-MM"`（格式錯 → 422）。

### Resources / Topology / Baremetal

```jsonc
"Resources":  { "cpu_cores": 0, "memory_mib": 0, "storage_gb": 0, "gpu_count": 0 }
"Topology":   { "site": "", "phase": "", "datacenter": "", "room": "", "rack": "", "ag": "" }
"Baremetal": {
  "id": "bm-1", "hostname": "",
  "total_capacity": Resources, "used_capacity": Resources,
  "topology": Topology,
  "network": "",   // BGP 網域；filter 屬性非打散維度
  "pool": ""       // 獨佔池標籤；"" = 共用池（獨立 domain，不是萬用字元）
}
```

**pool 嚴格隔離**（決議 #22）：pool=X 的需求只能用 pool=X 的機器；共用需求
只能用 pool="" 的機器。溢出處理是**人工在 Inventory 改機器 pool 標籤**，
下次 canonical run 自然生效——契約上沒有 spill，不要在 Go 端發明。

### DemandEntry（帳本列 = G1 的儲存單位）

```jsonc
{
  "cluster_id": "cluster-a",        // 必填
  "node_role": "worker",            // master|learner|worker|infra|l4lb-storage|bastion
  "period": "2026-09",              // 必填
  "cpu_cores": 0, "memory_mib": 0, "storage_gb": 0, "pod_count": 0,  // 增量需求；0=該維度不設下限
  "vm_specs": [Resources] | null,   // null = 用 config.vm_specs 目錄
  "min_total_vms": null, "max_total_vms": null,
  "fab": "",                        // "" = 單 fab 模式（見 §3 fab 規則）
  "network": "",                    // BGP 過濾
  "allowed_bm_types": null,         // 此 cluster 允許的採買機型清單（決議 #38）
  "pool": "",                       // 獨佔池；由 Go 從 cluster 註冊表系統填入
  "demand_id": "DMD-2609-014"       // G1 發號；solver 純透傳
}
```

**月份三態**（決議 #26）：帳本**沒有**該月的列＝未規劃（報表不出現）；
有列但全 0＝明確「不成長」；任一維 >0＝需求。Go 的 upsert/delete 必須保留
這個區分——刪列與留全 0 列語意不同。

### 供給側模型

```jsonc
"BaremetalType": { "type_id": "big-64c", "capacity": Resources, "fab": "" }
"ProcurementCap": { "bucket": "ag-1", "max_bm": 10, "fab": "", "network": "" }
"CommittedStock": {                  // 已購未到（錢已花、機器在路上）
  "type_id": "big-64c",              // 必須存在於 procurement_types
  "count": 100,
  "bucket": null,                    // null = 浮動，solver 決定落點
  "network": "", "fab": "", "pool": "",
  "available_from": "2026-10"        // null = 當月可用；人工維護，不做自動 ETA
}
"FleetEvent": {                      // 建新拆舊（E2.5）
  "period": "2026-03",               // 生效月
  "action": "release",               // 目前只有 release（整台除役還池）
  "bm_ids": ["bm-7", "bm-8"],        // 必須是 in_stock 裡的 id；一台只能出現一次
  "fab": ""                          // 具名 fab 模式必填，且需與機器實際 fab 一致
}
```

### CapacityPlanRequest（canonical run 的輸入 = G2 的產出）

```jsonc
{
  "demand_book": [DemandEntry],
  "in_stock": [Baremetal],
  "procurement_types": [BaremetalType],
  "procurement_caps": [ProcurementCap],
  "committed_stock": [CommittedStock],
  "fleet_events": [FleetEvent],
  "anti_affinity_rules": [], "max_per_bm_rules": [], "failover_rules": [],
  "config": SolverConfig
}
```

### CapacityReport（canonical run 的輸出 = G4 的存檔單位）

```jsonc
{
  "success": true,                   // 每個規劃月都成功才 true
  "by_fab_period": [{                // 一 fab × 一月
    "fab": "", "period": "2026-01", "success": true,
    "node_adds_total": 12, "bm_procurement_total": 2,
    "committed_bm_used": 1, "in_stock_bm_used": 3,
    "procurement": [{"type_id": "big-64c", "count": 2}],
    "committed_used": [{"type_id": "...", "count": 1}],
    "split_decisions": [...], "shortfalls": [ShortfallDetail],
    "solver_status": "OPTIMAL",
    "nominal_available": Resources, "remaining_node_slots": 40,
    "stranded_available": Resources | null, "balance_after": {"ag-1": 128},
    "cells": [BucketMonthCell],      // 見下
    "demand_coverage": [DemandCoverage],
    "released_bms": [], "frozen_bms": []   // fleet events 標注
  }],
  "budget_view": [                   // 採買投影（只計成功月）
    {"fab": "", "bucket": "ag-1", "network": "", "pool": "",
     "period": "2026-02", "type_id": "big-64c", "bm_count": 1}
  ],
  "totals": {...}, "solve_time_seconds": 0.1,
  "config_fingerprint": "4aca1d66cdd4"
}

"BucketMonthCell": {                 // 規劃粒度 = (fab, bucket, network, pool, month)
  "fab": "", "bucket": "ag-1", "network": "", "pool": "", "period": "2026-01",
  "node_adds": 4, "bm_bought": 1, "committed_used": 0, "in_stock_bm_used": 2,
  "in_stock_total": Resources, "in_stock_used": Resources,
  "in_stock_available": Resources,
  "in_stock_slots": 8                // 可落地槽數；config 無 reference_vm_spec 時為 null
}

"DemandCoverage": {                  // 需求單覆蓋標註（demand_id 一條龍的回程）
  "demand_id": "DMD-2609-014", "cluster_id": "...", "node_role": "worker",
  "period": "2026-01", "fab": "",
  "in_stock": 10, "committed": 4, "new_buy": 2, "total": 16
}
```

失敗月：該月 `success:false` + `shortfalls[]`（cause ∈ capacity / anti_affinity
/ space / unknown / input_error），該 fab 之後的規劃月變 `BLOCKED` stub、不解。
失敗月的數字是 what-if（修好輸入該重跑），**不計入 totals / budget_view**。

### ReconcileRequest / ReconcileReport（G6 呼叫、G4+G5 供料）

```jsonc
// POST /v1/capacity/reconcile
{
  "plan": {
    "plan_id": "plan-2026-01-v1", "created_at": "2026-01-02",
    "report": CapacityReport,        // G4 存檔的 response，原封不動塞回來
    "demand_snapshot": [DemandEntry] // 當時餵進去的帳本（demand_id join 空間）
  },
  "actual": {
    "as_of": "2026-01-31",           // 對帳時間點；其所在月 = 對帳目標月
    "in_stock": [Baremetal],         // 此刻真實快照（B 管道同源）
    "committed_stock": [CommittedStock],
    "executions": [{                 // G5 執行落帳
      "demand_id": "DMD-..." | null, // null = 沒走帳本（隕石）
      "cluster_id": "...", "node_role": "worker",
      "vm_count": 4, "status": "success" | "failed",
      "period": "2026-01", "fab": "", "infeasible_cause": null
    }],
    "machine_adds": [{               // Go 從庫存歷史算的實到機（count diff 即可）
      "fab": "", "bucket": "ag-1", "network": "", "pool": "",
      "period": "2026-01", "count": 1
    }]
  },
  "config": SolverConfig             // 對帳當下的 shared config
}

// Response
{
  "success": true, "status": "OK",   // plan 沒涵蓋目標月 → success:false + INPUT_ERROR
  "period": "2026-01", "as_of": "2026-01-31", "plan_id": "plan-2026-01-v1",
  "headline": {
    "fulfillment_rate": 1.0,  "planned_vms": 4, "fulfilled_vms": 4,
    "unjoinable_planned_vms": 0,     // 帳本列沒 demand_id → 不計入兌現率但要回報
    "forecast_error": 0.5,           // Σ|Δ可落地槽| / Σ預測槽
    "supply_hit_rate": null,         // 分母為空 → null（不是 0%，也不是 100%）
    "planned_machine_adds": 0, "actual_machine_adds": 0,
    "unplanned_ratio": 0.333, "executed_vms": 6, "unplanned_vms": 2
  },
  "cells": [/* 每格 predicted vs actual：slots 為主、名目 Resources 為輔 */],
  "drifts": [{                       // 漂移四分類，可多筆、可同格多類
    "category": "demand" | "supply" | "placement" | "fleet",
    "fab": "", "bucket": "ag-1", "network": null, "pool": null,
    "period": "2026-01", "delta": -32, "demand_ids": [], "message": "人話說明"
  }],
  "config_fingerprint": "...",       // 本次對帳 config 的指紋
  "plan_config_fingerprint": "..."   // plan.report 自帶的指紋；兩者不同 → UI 標注
}
```

---

## 3. 全域語意規則（組 request 時必須遵守）

- **fab 模式二選一**：`demand_book` 全部 `fab:""`（單 fab 模式）或全部具名。
  混用 → 422。具名模式下 `procurement_caps` / `committed_stock` /
  `fleet_events` **都必須具名 fab**（`procurement_types` 可以留 `""`——型錄
  無狀態）。
- **fab 維度**：機器屬於哪個 fab 由 `config.fab_topology_dimension`（預設
  `site`）決定；`config.procurement_spread_dimension`（預設 `ag`）決定
  bucket。兩者 Go 不要 hardcode，從 shared config 讀。
- **fleet_events 驗證**（422 級）：bm_id 必須存在於 in_stock；一台機器只能
  出現在一個事件；具名 fab 需與機器拓撲一致。語意：事件月**之前**該機凍結
  （不接新節點、舊負載照算、報表 `frozen_bms` 標注、儀表不計其空位）；事件
  月起 `used_capacity` 歸零還池（`released_bms` 標注）。
- **committed 畢業規則**（Go 端帳務，決議 #39/本文 §1 生命週期）：
  `committed 剩餘台數 = PO 台數 − 該 PO 已出現在 Inventory 的台數`。到貨的
  機器進 in_stock 的**同時**必須從 committed_stock 扣掉，否則同一台會被算
  兩次容量。
- **不要發明 ETA**：`available_from` 是人工欄位；到貨時程浮動，假 ETA 比
  沒有更糟，漂移交給 reconcile 量測。

---

## 4. G1–G7 逐項實作說明

依建議順序排列（= roadmap E1→E2→E2.5 影響→E3→E4→E5）。每項附驗收條件。

### G1 — 需求帳本（roadmap E1；一切的起點）

住 Go 端的持久化帳本，鍵 = `(fab, cluster_id, node_role, period)`，值 =
DemandEntry 其餘欄位。

- API：`upsert(entries[])`（同鍵覆蓋 last-write-wins）、`bulk_import(CSV)`
  （欄位見 capacity-planning 決議 #42：`fab, cluster, role, period,
  cpu_cores, memory_gib|memory_mib 擇一, storage_gb, pods, spec, network`；
  **匯入取代全部、重複鍵報錯**）、`delete(key)`、`get_book(from=當月)`。
- **發號**：建立時產生穩定 `demand_id`。過渡期（CSV 匯入）用鍵值決定性生成
  （如 `fab-clusterA-worker-2026-09`），同鍵重匯 id 不變。
- `pool` 欄位由 Go 從 cluster 註冊表填，不讓使用者手填。
- **驗收**：Capacity 負責人一輪月度規劃全走帳本，Excel 只剩匯入來源；
  `get_book` 的輸出可直接塞 `CapacityPlanRequest.demand_book`。

### G2 — 現況快照聚合（roadmap E2）

一鍵從 Inventory/BM Service 聚合出 plan 輸入的系統側五件套：
`in_stock`（含 used_capacity、topology、network、pool 標籤）、
`procurement_caps`、`committed_stock`（用畢業規則推導剩餘台數 +
人工 available_from）、`existing_*`（placement 用）、以及機型型錄。

- **規劃與執行共用同一快照來源**——這是一致性保證的地基，不要為規劃另抄一份。
- 快照要能以 (fab, 時間點) 匯出存檔（G7 回放要用）。
- **驗收**：canonical run 的 request 全自動組裝，零手工欄位。

### G3 — Filter profile 制度化（roadmap E2；一致性關鍵）

Go filter stage 抽成兩個**具名** profile：

| BM 狀態 | `execution` | `planning` | 理由 |
|---|---|---|---|
| OS 未 ready | 排除 | **納入** | 月內會 ready，是規劃期容量 |
| 維修中 | 排除 | **納入** | 維修週期 1–2 週 < 月粒度 |
| 保留機 | 排除 | **排除** | 送人的，不是我方容量 |

- 差集維護成**明文清單**（文件 + 版本），新增任何 filter 必須宣告進哪個
  profile——防「新 filter 只掛 execution，planning 從此高估卻無人發現」。
- G2 的快照聚合用 `planning` profile 出規劃輸入；執行期照舊 `execution`。
- **驗收**：差集清單存在且被 code review 強制；solver repo 的
  `tests/test_consistency_replay.py` 手抄了這張表——改表時兩邊同步改。

### G4 — Plan 存檔（roadmap E3）

每次 canonical run 把 **request + response 整包**存檔，發 `plan_id`。

- 存檔的 response 之後**原封不動**作為 `ReconcileRequest.plan.report` 塞回；
  request 裡的 demand_book 作為 `plan.demand_snapshot`。不要另造格式。
- response 的 `config_fingerprint` 一起落帳（M2）。
- **驗收**：任一 `plan_id` 可完整取回當時的輸入輸出。

### G5 — demand_id 一條龍（roadmap E3）

- UI 建置流程多一步「選需求單」；Go 把 demand_id 作 **pass-through
  metadata** 帶在自己的執行紀錄裡（**不進 placement request**——solver 契約
  沒這欄位）。
- 每次執行（成功或失敗）落一筆 `ExecutionRecord`：demand_id（沒走帳本就
  null）、cluster、role、vm_count、status、period、infeasible_cause。
- 需求單狀態機 = Go/UI 從三份資料 join 衍生（最新 plan 的 demand_coverage、
  執行紀錄、committed 到貨狀態）：
  `已規劃 → 等待採買/到貨 → 可執行 → 建置中 → 已完成 / 卡關`。solver 無狀態，
  不存這個狀態機。
- **驗收**：新 build/add-node 帶單率可量測且 > 目標值。

### G6 — 編排（roadmap E4）

- **月度 canonical run**：G2 快照 + G1 帳本 → `/v1/capacity/plan` → G4 存檔。
  手動觸發優先（需求大改、大批到貨時人工重跑）；cron 後補。
- **reconcile 排程**：週 cron + 月底正式 + 手動。組法：
  `plan` = 上期 canonical run 存檔；`actual.in_stock` = 此刻快照
  （planning profile）；`executions` = G5 落帳（目標月）；`machine_adds` =
  Go 從庫存歷史數的實到機（per cell per month 的 count diff，不需 solver）。
- **單月語意**：一次 reconcile 只對 `as_of` 所在月。plan 沒涵蓋該月會回
  INPUT_ERROR——別拿 12 月的 plan 對 3 月的帳。
- 指標為 null = 不可量測（分母為空），UI 顯示「—」，**不要**畫成 0%。
- `unjoinable_planned_vms > 0` 是帳本衛生警報（有列沒 demand_id），要浮出。
- **驗收**：第一份月結漂移報告產出且被 Capacity 負責人實際使用。

### G7 — 一致性回放（roadmap E5）

守護「plan 說可行 ⇒ execution 放得進去」：

```
同時刻快照 S 匯出三份：raw、S(planning profile)、S(execution profile)
① S(planning)  + 需求 D → POST /v1/capacity/plan      → 應可行且無缺口
② S(execution) + 需求 D → POST /v1/placement/split-and-solve（dry-run，
   requirements 的 candidate_baremetals = execution profile 交付的機器清單）
判定：
  ②成功 → 綠
  ②INFEASIBLE 且缺的容量坐在差集機器上（對 execution view 重問①仍不可行）→ 容忍
  execution view 少了差集外的機器 → 紅（filter 漂移，先於一切用集合比對抓）
  兩個 view 相同、config 指紋不同、②不可行 → 紅（config/引擎漂移）
```

- 定期 job 跑真實快照；②是純函式呼叫，天然 dry-run。
- 判定邏輯直接翻譯 `tests/test_consistency_replay.py::replay`（約 40 行，
  含判定優先序：filter 先於 divergence；差集表在同檔 `PLANNING_STATES` /
  `EXECUTION_STATES` 常數）。
- **驗收**：CI 綠燈 + 定期真快照回放在跑。

---

## 5. 常見誤區（給 agent 的負面清單）

1. 不要在 Go 端快取/複製 solver 的求解邏輯（哪怕「只是算算剩多少台」）——
   名目加總在碎片下說謊，可落地量只有 solver 能算，這正是 reconcile 存在的
   理由。
2. 不要把失敗月的 what-if 數字加進任何聚合——solver 的 totals/budget_view
   已排除，Go 端二次聚合時同樣要排除。
3. 不要用 `(cluster, month)` 匹配代替 demand_id join——同月多筆會歧義。
4. 不要把 reconcile 的 `machine_adds` 留白然後期待 solver 從快照反推——
   契約就是 Go 提供實到數（count diff），solver 只 diff。
5. 不要因為 config 指紋不同就擋 reconcile——照跑，兩個指紋都在 response，
   標注即可。
6. 狀態字串只 branch 前綴（`INPUT_ERROR:` / `INFEASIBLE` / `BLOCKED:`），
   訊息內文會演化。
7. pool 沒有 spill——別在 Go 端加「池滿借共用」的 fallback，那會繞過決議 #22。
