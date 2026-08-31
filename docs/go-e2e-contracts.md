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
"Resources":  { "cpu_cores": 0, "memory_mib": 0, "storage_gb": 0, "gpu": { "h200": 0 } }
// gpu：逐型號記帳（型號名稱由 scheduler 正規化，格式 ^[\w.-]+$）。
// 舊欄位 gpu_count 已移除；payload 帶 gpu_count → 422（不會被靜默忽略）。
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

依建議順序排列（= roadmap E1→E2→E2.5 影響→E3→E4→E5）。

每項的**驗收條件分三層**，交付時逐條走過：

- **A. 語意** — 表格化的「給定 → 必須」場景，每條都可以寫成一個測試。
  這層是重點：它們對應的都是**做錯了不會報錯、只會靜默給出錯答案**的地方。
- **B. 整合** — 證明它接得上下一站（通常是「輸出不經任何手工轉換直接餵給
  下一個端點且成功」）。這是各項「完成」的硬指標。
- **C. 成果** — 業務層面的成功訊號，可能要一兩個月才驗得到，**不擋交付**。

守門類的項目（G3、G7）額外有一層 **對抗性驗收**：故意弄壞，確認它會叫。
一個從未紅過的守門員與一個壞掉的守門員從外面看是一樣的。

#### 一眼總覽

| # | 一句話「做完了沒」 |
|---|---|
| G1 | `get_book` 的輸出**直接** POST `/v1/capacity/plan` 成功，且同一份 CSV 匯入兩次 demand_id 不變 |
| G2 | canonical run 的 request **零手工欄位**組出來，且 committed 與 in_stock 不重複計算同一批機器 |
| G3 | 兩 profile 的機器集合差集**恰好等於**那三列；忘記宣告 profile 會編譯不過 |
| G4 | 任一 `plan_id` 取回的 response **原封**塞進 reconcile 成功 |
| G5 | 成功與失敗都落帳；placement request 裡**沒有** demand_id |
| G6 | 空分母顯示「—」不是 0%；三個資料源自動組裝、手動與 cron 走同一段程式碼 |
| G7 | 故意製造 filter 漂移**會紅燈**，修好**會恢復綠燈** |

### G1 — 需求帳本（roadmap E1；一切的起點）

持久化帳本（Inventory DB），鍵 = `(fab, cluster_id, node_role, period)`，
值 = DemandEntry 其餘欄位。

#### 三態語意（所有寫入路徑都必須保住）

| 帳本狀態 | 意思 | 報表行為 |
|---|---|---|
| **沒有這一列** | 該月**未規劃** | 該月不出現在報表 |
| **有列、所有需求維度全 0** | 明確「**不成長**」 | 出現，`node_adds = 0` |
| **有列、任一維 > 0** | 有需求 | 正常規劃（0 的那一維 = 不設下限，不是「用 0」） |

**「刪列」與「把列填 0」是兩個不同的意圖**，不可用同一個操作表達，也不可
用軟刪除把兩者混為一談。這是所有寫入 API 的共同約束。

#### 四條寫入路徑（語意各不相同，必須分開實作）

| # | 操作 | 語意 | 範圍 | 觸發來源 |
|---|---|---|---|---|
| W1 | `bulk_replace(entries[], scope)` | **取代**：scope 內既有列全部刪除後寫入新列 | 見下 | CSV / Excel 匯入 |
| W2 | `upsert(entries[])` | **稀疏合併**：同鍵覆蓋（last-write-wins），未提及的列不動 | 只有送進來的鍵 | 表單編輯、網格貼上附加 |
| W3 | `delete(keys[])` | **刪列** → 該月回到「未規劃」 | 只有指定的鍵 | UI 刪除按鈕 |
| W4 | （不是 API）填 0 | 走 W2，值全 0 → 「確定不成長」 | — | 表單編輯 |

**W1 與 W2 不可互相取代**：拿 W2 模擬匯入，CSV 裡被拿掉的列不會消失（使用
者以為「我刪掉那個月了」但帳本還在）；拿 W1 模擬單列編輯，會把整個 scope
的其他列洗掉。

#### W1（CSV 匯入）的完整規則

沿用決議 #42，solver repo 的 what-if UI 已依此實作（
`app/web_static/js/demand-csv.js`、`report-form.js`，可直接對照）：

- **欄位**：`fab, cluster, role, period, cpu_cores, memory_gib|memory_mib
  擇一, storage_gb, pods, spec(空=Any), network`。`cluster`/`period` 必填；
  **欄序不拘**（按表頭名對映）；**未知欄警告後忽略**。
- **重複鍵報錯**：同一次匯入內出現重複 `(fab, cluster, role, period)` →
  整批拒絕，**不做 last-wins 合併**（合併會讓使用者不知道自己覆蓋了什麼）。
- **全有或全無**：任一列有錯 → 整批不寫入。**必須在單一交易內完成**
  「刪除 scope 內既有列 + 寫入新列 + 重複鍵檢查」。分成 delete 再 insert
  兩步而中途失敗，會留下半新半舊的帳本——而三態語意讓這種狀態**看起來像
  合法資料**（缺的列被讀成「未規劃」），規劃會照跑且不報錯。
- **scope 建議 = CSV 裡出現的 fab 集合**，不是整本。理由：負責人實際上一次
  只處理一兩個 fab 的 Excel，若匯入單一 fab 的檔案卻清掉其他 19 個 fab 的
  資料是災難級誤刪。「整本取代」若真的需要，做成獨立操作並要求額外確認。
- **前端可以先解析**：讀檔、編碼、欄位對映、錯誤標紅放前端體驗最好，
  Go 不需要碰 CSV bytes。但**重複鍵檢查與交易性必須在伺服器端再做一次**
  ——前端預檢是 UX，擋不住兩個人同時匯入。
- **回應必須回吐異動計數**（第一階段就要做，成本近乎為零——交易內本來就
  算得出來）：

  ```jsonc
  { "added": 12, "updated": 30, "deleted": 3,
    "deleted_keys": [{"fab":"fab-a","cluster_id":"c1","node_role":"worker","period":"2026-11"}] }
  ```

  刪除的那幾列是使用者「從 Excel 拿掉」而**可能沒意識到會消失**的月份，
  必須在匯入後明確顯示。沒有這個回吐，誤刪會完全無聲——因為三態語意讓
  「缺列」被讀成「未規劃」，規劃照跑、不報錯。

#### CSV 的 `spec` 欄位

CSV 存的是 **spec 名稱**（如 `standard`），`DemandEntry.vm_specs` 要的是
**Resources 物件**，所以中間必須查目錄：

```
CSV "spec" = "standard" → 查 VM spec 目錄 → vm_specs: [{cpu_cores,memory_mib,storage_gb}]
CSV "spec" = 空          → Any            → vm_specs: null
                                             （solver 從 config.vm_specs 整份目錄自己挑）
```

- **目錄的 SSOT = Inventory API**，所有 spec 都保證有定義。因此 CSV 出現
  查不到的 spec 名稱＝**打字錯誤**，直接報錯並在訊息中列出目錄內容（比照
  solver repo `demand-csv.js:160-162` 的作法），不要嘗試容錯或自動建立。
- **同一份目錄必須同時餵兩個地方**：CSV 的名稱解析（釘死用），以及
  `config.vm_specs`（`spec` 留空時 solver 的候選池）。兩邊來源不同的話，會
  出現「釘 standard 的列用一組定義、Any 的列從另一組挑」這種難查的不一致。
- **帳本存名稱，不存解析後的值**；解析發生在**組 request 時**。
  理由：`plan_run` 存整包 request（G4），所以「那次規劃當時 standard 是幾核」
  自動被凍結在存檔裡——帳本保持人類可讀可編輯，目錄更新自然套用到未來的
  run，而歷史語意仍可追。若改成存 Resources，帳本會與目錄脫節且不好讀。
- 對照實作：`report-form.js:445-456`（`vm_specs: spec ? [toResources(spec)] : null`）。

#### demand_id 發號

- 建立時產生**穩定**的 `demand_id`；**一旦發出不可變、不可重用**。
- **W1 匯入的列必須用鍵值決定性生成**（例如 `fab-cluster-role-period` 的
  串接或雜湊）。若每次匯入重發新 id，同一行需求在重灌 Excel 後會變成新
  id，先前所有 `plan_execution_record` 的 join 全部斷開，兌現率歸零且查不
  出原因。
- W1 覆蓋既有列時**沿用原 id**（同鍵 → 同 id，決定性生成天然成立）。
- 這隱含一個限制：目前**一個鍵只能有一列需求**。若未來要允許同一
  `(fab, cluster, role, period)` 拆成多筆，決定性發號方案必須重新設計。

#### 其他

- `pool` / `network` 的來源：`pool` 由 Go 從 cluster→tenant 對映自動填，
  **不讓使用者手填**；`network`（BGP）目前 cluster 對不回去，**需要人填**，
  UI 應做成「該 fab 有哪些 BGP 域」的下拉選單而非自由文字——打錯字會讓
  solver 回 `INPUT_ERROR: no (bucket, network) cell exists in network 'xxx'`。
- 讀取端只需要 `get_book(from=當月)`，輸出可直接塞
  `CapacityPlanRequest.demand_book`。

#### 驗收條件

**A. 語意**（每條都是一個可寫成測試的場景；這些是最容易做錯的地方）

| # | 給定 | 操作 | 必須 |
|---|---|---|---|
| A1 | 帳本有 `(fab-a, c1, worker, 2026-09)` | `delete` 該鍵 | `get_book` 不再回該列 |
| A2 | 同上 | 改成需求全 0（W2） | `get_book` **仍回該列**，各維度為 0 |
| A3 | 帳本有 fab-a 三列 | 匯入只含其中兩列的 CSV（W1） | 第三列**消失**；`deleted=1` 且 `deleted_keys` 指出是誰 |
| A4 | 帳本有 fab-a、fab-b 各若干列 | 匯入只含 fab-a 的 CSV | **fab-b 完全不動** |
| A5 | 任意帳本 | 匯入含重複鍵的 CSV | 整批拒絕，**帳本零改變**（不是部分寫入） |
| A6 | 任意帳本 | 匯入時在寫入中途注入失敗 | **帳本零改變**（交易性） |
| A7 | 空帳本 | 同一份 CSV **連續匯入兩次** | 兩次產生的 `demand_id` **完全相同** |
| A8 | 任意帳本 | 匯入含未定義 spec 名的 CSV | 報錯，訊息**列出目錄內容**；帳本零改變 |
| A9 | 任意帳本 | 任一次 W1/W2/W3 | 回應的 `added/updated/deleted` 與實際差異**逐筆相符** |

A7 特別重要:它是 demand_id 決定性發號的唯一驗證方式,而發號一旦錯了,
兌現率會在 E4 才爆炸,那時已經很難回頭修。

**B. 整合**（證明它接得上下一站）

- `get_book(from=當月)` 的輸出**不經任何手工轉換**直接放進
  `CapacityPlanRequest.demand_book` → POST `/v1/capacity/plan` 回 200 且
  `success=true`。這是 G1 完成的硬指標——若還需要中間轉換層，欄位對映就
  還沒對齊。
- 三態的端到端效果:A1 刪掉的月份**不出現在報表**;A2 填 0 的月份
  **出現且 `node_adds_total=0`**。

**C. 成果**（可能要等一兩個月才驗得到，不擋交付）

- Capacity 負責人**一輪完整月度規劃全走帳本**，Excel 只作為匯入來源。
- 帳本列數與實際規劃中的 cluster×role×月 數相符（沒有「還有一半在某人的
  試算表裡」）。

#### 後續增強（第一階段不做）

- **匯入前預覽（dry-run）**：在 W1 加 `dry_run` 旗標，回傳與實際寫入
  **同樣的異動計數結構**但不提交，讓使用者按下確認前先看到「將刪除 K 列」。
  用同一支 API 的旗標而非另開預覽 endpoint——這樣預覽走的是與寫入**完全
  相同的程式碼路徑**，不會出現「預覽說刪 3 列、實際刪 12 列」的兩套邏輯
  不同步問題。第一階段的事後計數回吐已經讓誤刪可見，dry-run 是把它從
  「事後知道」升級成「事前攔截」，成本很小（同一個結構加個旗標）。
- **樂觀鎖**：dry-run 回應帶 scope 的版本 token，正式提交時帶回比對，不符
  就拒絕並要求重新預覽。防的是預覽與提交之間別人改了帳本。等到有多人同時
  編輯帳本的實際場景再做。
- **需求網格（demand lines grid）**：決議 #42 列為後補。

### G2 — 現況快照聚合（roadmap E2）

一鍵從 Inventory/BM Service 聚合出 plan 輸入的系統側五件套：
`in_stock`（含 used_capacity、topology、network、pool 標籤）、
`procurement_caps`、`committed_stock`（用畢業規則推導剩餘台數 +
人工 available_from）、`existing_*`（placement 用）、以及機型型錄。

- **規劃與執行共用同一快照來源**——這是一致性保證的地基，不要為規劃另抄一份。
- 快照要能以 (fab, 時間點) 匯出存檔（G7 回放要用）。

#### 驗收條件

**A. 語意**

| # | 給定 | 必須 |
|---|---|---|
| A1 | 一台機器,其 rack 有 `available_group=ag-3`、`bgp_number=bgp-x` | 產出的 `Baremetal.topology.ag == "ag-3"`、`network == "bgp-x"` |
| A2 | 一台 `pool=pool-ml` 的機器 | 產出的 `Baremetal.pool == "pool-ml"`;共用機器為 `""` |
| A3 | 一台 OS 未 ready、一台維修中、一台保留機 | 前兩台**在** `in_stock`,保留機**不在**（planning profile） |
| A4 | 一張 PO 20 台、其中 5 台已到貨進 Inventory | `committed_stock` 該筆 `count == 15`,且那 5 台**同時出現在** `in_stock`——總數 20 不是 25 |
| A5 | 一張 PO 標了 `available_from=2026-10` | 產出的 committed 條目帶該值 |
| A6 | 同一時刻連續呼叫兩次 | 產出**逐欄位相同**（快照要可重現，否則 G7 回放沒有意義） |

A4 是最容易錯的一條:漏扣就把同一批機器的容量算兩次,規劃會以為容量充足
而少買,且完全不會報錯。

**B. 整合**

- 產出的 request **零手工欄位**直接 POST `/v1/capacity/plan` → 200 且
  `success=true`。
- 與人工組裝的同期 request 做欄位級 diff → **差異為 0**（第一次上線時做一次
  對照即可，之後靠 A1–A6）。
- 匯出的快照可存檔、可重新載入產生**位元相同**的 request（G7 前置）。

**C. 成果**

- 一次 canonical run 從按下按鈕到拿到報表**不需要任何人工填欄位**。

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

#### 驗收條件

**A. 語意**

| # | 給定 | 必須 |
|---|---|---|
| A1 | 一批含四種狀態的機器 | 兩個 profile 各自輸出的機器集合，**差集恰好等於表上那三列**，多一台少一台都算失敗 |
| A2 | 全部機器都是 ready | 兩個 profile 輸出**完全相同** |
| A3 | 程式碼裡新增一個未宣告 profile 的 filter | **CI 或編譯期擋下**（例如 filter 註冊必須帶 profile 參數，缺少就編譯不過） |

A3 是這一項的**真正價值所在**。差集清單寫在文件上只是紀錄；讓「忘記宣告」
變成**不可能編譯**，才是制度化。若做不到編譯期，至少要有一條測試遍歷所有
已註冊 filter 並斷言每個都宣告了 profile。

**B. 對抗性驗收（守門員自己要被驗證會叫）**

- 故意在 `execution` profile 加一個排除某台 ready 機器的 filter 而不宣告
  → **G7 的回放必須紅燈**。
- 故意把「保留機」從兩邊排除改成只排除 execution → **回放必須紅燈**。

一個從來不會紅的守門員，和一個壞掉的守門員，從外面看是一樣的。這兩條要
真的跑過一次，不是寫在文件上。

**C. 成果**

- 差集清單有版本、有 owner；solver repo 的
  `tests/test_consistency_replay.py`（`PLANNING_STATES` / `EXECUTION_STATES`）
  與它一致——**改表時兩邊同步改**是 code review checklist 的一項。

### G4 — Plan 存檔（roadmap E3）

每次 canonical run 把 **request + response 整包**存檔，發 `plan_id`。

- 存檔的 response 之後**原封不動**作為 `ReconcileRequest.plan.report` 塞回；
  request 裡的 demand_book 作為 `plan.demand_snapshot`。不要另造格式。
- response 的 `config_fingerprint` 一起落帳（M2）。

#### 驗收條件

**A. 語意**

| # | 給定 | 必須 |
|---|---|---|
| A1 | 任一 `plan_id` | 取回的 request + response **與當時送出/收到的位元相同**（不是「欄位差不多」——存的若是重新序列化過的物件，欄位順序或 null/預設值處理的差異會在 reconcile 時變成假漂移） |
| A2 | 同上 | `config_fingerprint` 存得到、取得回 |
| A3 | 帳本在存檔後被修改 | 取回的 `demand_snapshot` **仍是當時那份**（存檔是快照不是參照） |

**B. 整合**

- 取回的 response **不經任何轉換**塞進 `ReconcileRequest.plan.report`、
  取回的 demand_book 塞進 `plan.demand_snapshot` → POST
  `/v1/capacity/reconcile` 回 200 且 `success=true`。這是 G4 完成的硬指標。
- 若 JSON 外放 object storage：指標列與 blob **不可能不同步**（同一交易寫入，
  或 blob 先寫、指標後寫且指標帶 checksum）。

**C. 成果**

- 每次 canonical run 都有存檔，沒有「那次的計畫找不到了」的月份。

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

#### 驗收條件

**A. 語意**

| # | 給定 | 必須 |
|---|---|---|
| A1 | 建置時選了需求單 | 送給 solver 的 placement request **不含 demand_id**（契約沒這欄位；有的話代表你動了不該動的地方） |
| A2 | 建置成功 | 落一筆 `ExecutionRecord`，`status=success`、`demand_id` 正確 |
| A3 | 建置**失敗** | **一樣落帳**，`status=failed` + `infeasible_cause`。漏掉失敗案例會讓兌現率虛高 |
| A4 | 建置時**沒選**需求單 | `demand_id=null` 落帳，且該筆會被 reconcile 計入計畫外比率 |
| A5 | 同一張需求單分兩次建置 | 兩筆紀錄，reconcile 加總後與計畫數比對 |
| A6 | 需求單狀態機 | 是**衍生查詢**，不是欄位——改了 plan 或執行紀錄，狀態自動變；DB 裡沒有一個會過期的 `status` 欄 |

**B. 整合**

- 把 `ExecutionRecord` 餵進 `ReconcileRequest.actual.executions` →
  reconcile 算得出非 null 的 `fulfillment_rate`。
- 刻意讓某張單只執行一半 → `fulfillment_rate` 確實是 50%，且 `drifts` 有一
  筆 `category=demand`、`delta=-N`。

**C. 成果**

- 新 build/add-node 的**帶單率**可量測（`1 − unplanned_ratio`）且達到目標值。
- `unjoinable_planned_vms` 趨近 0（帳本列都有 demand_id）。

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

#### 驗收條件

**A. 語意**

| # | 給定 | 必須 |
|---|---|---|
| A1 | 某指標分母為 0（例如該月沒有任何採買計畫） | API 回 `null`，UI 顯示「—」;**任何地方都不出現 0%** |
| A2 | plan 涵蓋 2026-01、`as_of=2026-03-15` | 收到 `INPUT_ERROR`，UI 給明確訊息（不是空白畫面或 500） |
| A3 | 某 fab-月規劃失敗 | 該月的 what-if 數字**不進**任何 Go 端二次聚合 |
| A4 | 一個 cell 該月計畫到貨 3 台、實到 1 台 | `machine_adds` 算出 1，`supply_hit_rate` 反映短缺，`drifts` 有對應的 supply 列 |
| A5 | 同一份輸入跑兩次 reconcile | 結果相同（純函式，沒有隱藏狀態） |
| A6 | plan 與 reconcile 的 config 指紋不同 | **照跑不擋**，兩個指紋都呈現在 UI 上並標注 |

**B. 整合**

- 月度 canonical run 一鍵完成:G2 快照 + G1 帳本 → plan → G4 存檔,**中間
  無人工步驟**。
- reconcile 三個資料源（G4 存檔、當下快照、G5 落帳）自動組裝,手動觸發與
  cron 走**同一段程式碼**。

**C. 成果**

- 第一份月結漂移報告產出，**且被 Capacity 負責人實際用來做決策**（不是產出
  就算數——沒人看的報表等於沒有）。
- 四指標連續三個月有數字（不是一堆 null），代表資料鏈真的通了。

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

#### 驗收條件

**A. 語意**（四級判定各驗一次）

| # | 給定 | 必須判定為 |
|---|---|---|
| A1 | 全部機器 ready，容量充足 | `consistent` |
| A2 | 需求超過總容量 | `plan_infeasible`（沒有承諾就沒有要守的） |
| A3 | 承諾壓在 OS 未 ready 的機器上 | `explained_by_profile_delta`（容忍） |
| A4 | execution view 少一台 ready 機器 | `broken:undeclared_filter_delta` |
| A5 | 兩 view 相同但 config 指紋不同且②不可行 | `broken:divergence` |
| A6 | A4 與 A5 **同時發生** | 報 **filter**（判定優先序:view 都錯了再談引擎分歧會誤導修方向） |

**B. 對抗性驗收（最重要的一條）**

**故意弄壞，確認它會叫**：

1. 在 execution profile 偷加一個排除某台 ready 機器的 filter → 回放**必須
   紅燈**，且指出是 filter 漂移而非容量不足。
2. 把 execution 的 config 換一份（例如調 `max_per_bm`）→ 回放**必須紅燈**，
   且指出是 config/引擎漂移。
3. 修好之後 → 回放**必須恢復綠燈**。

這三步要真的跑過一次並留下紀錄。**一個從未紅過的守門員，與一個壞掉的
守門員，從外面看完全一樣**——而這一項的全部價值就在它會不會叫。

**C. 整合與成果**

- 快照三份（raw + 兩 profile）可匯出、可重放,重放產生**位元相同**的 request。
- 定期 job 在跑（頻率依快照成本決定，週一次起跳），紅燈有人收到通知。
- 連續三個月無紅燈，或紅燈都能追到具體的 filter/config 變更——代表守門
  真的在守，而不是靜默通過。

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
8. 不要用 upsert 實作 CSV 匯入（W1）——匯入是**取代**，用 upsert 的話
   CSV 裡被拿掉的列不會消失，使用者以為刪掉了、帳本裡還在。
9. 不要把「刪列」和「填 0」做成同一個操作——前者是未規劃、後者是確定不
   成長，規劃結果完全不同。
10. 不要讓 CSV 匯入的 demand_id 隨機發號——重灌一次 Excel 就把所有執行
    紀錄的 join 打斷。
