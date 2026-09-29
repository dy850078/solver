# Compare sets — 多份 mock 情境輪流 sizing，彙整成一張表

`app/compare.py` · `POST /api/compare/run` · `python -m app.compare` · UI `/ui/compare.html`

## 1. 解決什麼問題

採購前常要回答：「A 機型 + (master×5 / infra×5 / l4lb-storage×3) + 3 個 cluster 共住，
要幾台？換 B 機型呢？套餐縮小呢？」以前是在 Topology 頁一次填一份 mock 設定、求解、
抄數字進表格；機型新增或套餐改了就全部重來。

Compare set 把「設定」變成資產、「結果」變成產物：一份 JSON 裝著目錄（VM specs、BM
機型、套餐）與一個情境清單，每個情境**用名字引用**目錄；改一個機型的容量，所有用到
它的情境重跑一次就更新。每個情境交給 mock generator（`docs/mock-request-generator.md`）
做 greenfield sizing：解析下限 → solver 驗證 → escalate 到可行，再把 solver 的實際擺放
縮成比較用的數字。

## 2. 檔案格式（`CompareSet`）

```json
{
  "name": "control-plane sizing · A-64c vs B-96c",
  "vm_specs":  { "cp": { "cpu_cores": 8, "memory_mib": 32000, "storage_gb": 200 } },
  "bm_models": { "A-64c": { "capacity": { "cpu_cores": 64, "memory_mib": 256000, "storage_gb": 2000 } },
                 "B-96c": { "capacity": { "cpu_cores": 96, "memory_mib": 512000, "storage_gb": 4000 }, "roles": [] } },
  "bundles":   { "cp-large": [ { "role": "master", "count": 5, "ip_type": "non-routable", "spec": "cp", "max_per_bm": 1 }, "…" ],
                 "cp-small": [ "…" ] },
  "defaults":  { "seed": 1, "racks": 6, "ags": 3, "anti_affinity": true, "target_spread": { "ag": 3 },
                 "tightness": 1.0, "config_overrides": { "max_solve_time_seconds": 10 } },
  "scenarios": [
    { "name": "A-64c · cp-large · c3", "bm_model": "A-64c", "bundle": "cp-large", "clusters": 3 },
    { "name": "B-96c · cp-large · c3 · loose", "bm_model": "B-96c", "bundle": "cp-large", "clusters": 3,
      "overrides": { "tightness": 0.7 } }
  ]
}
```

| 區塊 | 內容 | 規則 |
|---|---|---|
| `vm_specs` | 具名 VM 規格目錄（同 GenerateRequest） | key 字元集 `^[\w.-]+$` |
| `bm_models` | `{name: {capacity, roles?}}`。永遠彈性 sizing（沒有 `count`）；`roles` 同 `BmProfile.roles`（dedicated pool） | 至少一個 |
| `bundles` | `{name: [NodeGroup, …]}`，即一個 cluster 的 node groups（含 `max_per_bm`、`no_colocate_group`、`scope`、`exclusive`） | 至少一個、不可為空；`spec` 必須存在於 `vm_specs` |
| `defaults` | 其餘 `GenerateRequest` knobs，套用到每個情境 | 不可含保留欄位（見下） |
| `scenarios` | 明確清單。`bm_model` 為字串或 list（多機型混用，標籤顯示 `A+B`）；`clusters ≥ 1`；`overrides` 疊在 `defaults` 上；`enabled:false` 的情境 Run all 時跳過 | 名稱唯一、引用必須存在 |

**保留欄位**：`vm_specs` / `bm_profiles` / `node_groups` / `clusters` 由引用決定，出現在
`defaults` 或 `overrides` 裡 → 422。**未知 knob** 也是 422：pydantic 預設會默默忽略未知
key，打錯字的 override 會靜默失效，所以這裡用 `GenerateRequest.model_fields` 白名單擋。
dict 型的 knob（`config_overrides`、`target_spread`、`max_per_bm_by_role`）在 `overrides`
裡**一層深合併**，可以只加一個 key。

情境是明確清單，不是笛卡兒積；UI 的「Generate grid…」只是批次新增的便利。

## 3. 解析：情境 → `GenerateRequest`

```
vm_specs     = set.vm_specs
bm_profiles  = [BmProfile(name=m, capacity=bm_models[m].capacity, roles=bm_models[m].roles) for m in bm_model]   # count=None
node_groups  = bundles[bundle]
clusters     = scenario.clusters
其餘 knobs    = defaults ⊕ overrides
```

`POST /api/compare/resolve` 回每個情境解析後的 request；`run` 的每一列也附
`resolved`，UI 的「Save as mock preset」就是把它存成 Topology 頁可載入的 preset。

## 4. 每個情境的指標（`ScenarioResult`）

| 欄位 | 定義 |
|---|---|
| `status` | `ok` / `infeasible` / `error` / `skipped` |
| `labels` | `{bm_model, bundle, clusters}`，由引用推導，表格分組用 |
| `bm_fleet` | generator **配置**的台數 = `len(request.baremetals)`。這是採購數：有 `num_ags` 下限，且 escalation 只加不減 |
| `bm_used` | solver 驗證結果實際擺上的台數（`bm_used_count`）。是 solver 在 `w_consolidation` / `w_headroom` 權重下的選擇，**不是證明過的最小值** |
| `bm_per_cluster_avg` | `bm_used / clusters`（平均；cluster 共住時一台 BM 會被多個 cluster 算到） |
| `bm_by_cluster` | `{cluster_id: 該 cluster 的 VM 落在幾台不同 BM}`（精確值；`shared` 群組另列） |
| `vm_total` | VM 總數 |
| `vm_density_max` / `vm_density_avg` | 每台**有用到的** BM 上的 VM 數之最大 / 平均 |
| `utilization` | 每個資源維度 Σ demand ÷ Σ used BM 容量（`gpu:<model>` 維度亦列；容量 0 的維度略過） |
| `placements` | `[{bm_id, ag, vms:[{vm_id, role, cluster_id}], util:{…}}]`，只列有 VM 的 BM |
| `escalation_rounds` | mockgen 加機輪數（0 = 解析下限一次到位） |
| `solve_time_seconds` / `elapsed_seconds` | 最後一次驗證的 solve 時間 / 整個 generate 的 wall clock（含 escalation） |
| `resolved` | 這一格實際跑的 `GenerateRequest` |
| `error` | `status=error` 時的原因（mockgen 400、knob 值不合法）；`skipped` 時為 deadline 說明 |

`infeasible` 時擺放類指標為 `null` 而非 0（solver 預設不回部分擺放）。

## 5. 執行語意

- `POST /api/compare/run`：body = set + `only?: [names]`、`deadline_seconds=120`、
  `max_scenarios=60`。**循序**執行——CP-SAT 一次 solve 已用 `num_workers=8`，再開
  process 只會互搶核心。逾時後剩餘情境標 `skipped`；選到的情境數超過
  `max_scenarios` → 422。
- **單格失敗不拖垮整組**：某機型太小觸發 mockgen 的 runaway guard（400）、某 override
  值不合法（如 `tightness: 5`），只有那一列是 `error`。結構性錯誤（引用不存在、未知
  knob、重複名稱）才是整個請求 422。
- UI 逐格呼叫（`only=[一個名字]`），表格邊跑邊填、可 Cancel；批次呼叫一次送整組。
- **同 seed 不保證同擺放**：`resolved` request 與 `bm_fleet` 可重現，但 solver 在同分
  解之間的選擇隨多執行緒而異；比較請看數字，不要 diff 擺放。

## 6. CLI

```bash
python -m app.compare --input examples/compare/control_plane_sizing.json            # CSV 到 stdout
python -m app.compare --input set.json --csv output/compare.csv --json output/compare.json
python -m app.compare --input set.json --only "A-64c · cp-large · c3" --deadline 300
```

exit code：0 全部 ok；1 有 infeasible / error / skipped；2 set 本身不合法。CSV 欄位順序
固定（`ROW_COLUMNS`）：`name, bm_model, bundle, clusters, status, bm_used, bm_fleet,
bm_per_cluster_avg, vm_total, vm_density_max, vm_density_avg, util_cpu, util_mem,
util_storage, escalation_rounds, solve_time_seconds, elapsed_seconds, error`。

## 7. UI（`/ui/compare.html`）

- **左側**：Load set / Save set / New；example 下拉（`examples/compare/*.json`）；三個
  目錄（VM specs、BM models、Bundles）沿用 Topology 頁的卡片；Defaults 欄位 +
  Advanced JSON；Scenarios 清單（勾選、名稱、機型 / 套餐下拉、cluster 數、⚙ overrides
  JSON、⧉ 複製、✕）；「Generate grid…」勾機型 × 套餐 × cluster 數批次新增。
- **Topology 頁入口**：Generate mock 卡片的「＋ Add to compare set」把目前表單拆進
  草稿 set（specs / 機型 / node groups 進目錄，其餘 knobs 進 `defaults`，第二份起差異
  進 `overrides`）。
- **草稿**：編輯中的 set 自動存進 `localStorage`（`solver-compare-draft`），重新整理不丟；
  只在該瀏覽器，Save set 才是持久化。伺服器端儲存刻意不做（sidecar 維持無狀態；格式
  相同，日後只需加 API）。
- **右側**：依 BM 機型（預設）或依套餐分組的表格：`used / fleet`、BM / cluster、VM
  density（max，avg 為副標）、cpu / mem / storage 利用率條、status chip、時間；點列展
  開 per-BM 擺放（role / cluster 標籤 + 利用率條）；CSV / xlsx 匯出；Save as mock preset。

## 8. 限制

- 每格最壞 11 次 solve（escalation）× `max_solve_time_seconds`；deadline 只能在格與格之
  間跳過。
- 同步 handler 整組占一個 threadpool worker；批次呼叫者用 `max_scenarios` 與 deadline
  自我限制。
- 表格的 `bm_used` 反映 objective 權重；要「證明最少幾台」請用 rollout sizing
  （`docs/rollout-simulation-and-sizing.md`）。
