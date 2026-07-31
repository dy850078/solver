# CLAUDE.md — go-scheduler

> **安裝時要改的地方**:所有 `<...>` 佔位符;`## Architecture` 的實際套件結構;
> `## Commands` 的實際指令。其餘內容是設計語意,可直接沿用。

Go 排程服務:接收 VM 建置請求、推導候選機器、呼叫 solver sidecar 取得放置
方案;同時是容量規劃 E2E 的編排者與唯一持久層(Inventory DB)。

本檔只寫**看程式碼推不出來的東西**:領域語意、契約規則、與工程師的協作約定。

## Architecture

```
<repo>/
├── cmd/                  # 進入點
├── internal/
│   ├── scheduler/        # 候選推導 + solver 呼叫(執行期)
│   ├── planning/         # 需求帳本、canonical run、reconcile 編排(規劃期)
│   ├── solverclient/     # solver HTTP 契約的唯一封裝層
│   └── inventory/        # Inventory DB 存取
docs/decisions/           # ADR
```

**Solver 是外部純函式服務**:無狀態、進什麼算什麼。所有持久化都在
Inventory DB;solver 契約的權威定義在 solver repo 的 `app/models.py`
(本機可讀:`<solver repo 路徑>`),契約說明在 `docs/go-e2e-contracts.md`。

## 領域語意(推不出來,務必保持正確)

### 兩期一致性(最重要的性質)

**規劃期承諾的容量,執行期必須真的放得進去。** 破壞它的兩個來源:

- **D1 候選推導漂移**:規劃與執行用不同的 filter 卻沒宣告。差集必須是
  **明文清單**(`planning` / `execution` 兩個具名 profile),新增任何 filter
  都要宣告進哪個。已定案差集:OS 未 ready / 維修中 → 規劃納入、執行排除;
  保留機 → 兩邊都排除。
- **D2 config 漂移**:每個 solver response 都帶 `config_fingerprint`,
  **每次呼叫都要落帳**。規劃與執行用同一份 config。

方向性:規劃比執行**窄**=低估,安全;規劃比執行**寬**=承諾破裂,禁止。

### pool(獨佔池)

- `baremetal.pool` 是**分割**:每台恰好屬於一個,`""` = 共用池,是**獨立
  domain 不是萬用字元**。
- **嚴格隔離,雙向**:pool=X 的需求只能用 pool=X 的機器,**共用需求也絕對
  不能碰 pool 機器**。單向過濾(只鎖獨佔那邊)等於沒隔離。
- 不變式:**一個 bmg 的成員必須同 pool**。成立的話執行期隔離自動達成,
  候選推導不用改。
- 池間搬移是**人工操作**(改 `baremetal.pool`),沒有 spill / 借用機制。
  想做要走正式翻案,不能在執行期悄悄放寬。

### 需求帳本

- 唯一鍵 `(fab, cluster_id, node_role, period)`,`period` 一律 `"YYYY-MM"`。
- **月份三態**:缺列 = 未規劃(報表不出現);留列全 0 = 明確「不成長」;
  任一維 > 0 = 需求。**刪列與留全 0 列語意不同**,不可用同一個操作表達。
- `demand_id` 一旦發出**不可變、不可重用**,revise 走同鍵 upsert。CSV 匯入
  用鍵值決定性生成,重匯 id 不變(否則執行紀錄的 join 全斷)。
- CSV 匯入語意「**取代全部**」必須是**單一交易**;delete + insert 分兩步
  失敗會留下半新半舊的帳本,而三態語意讓這種狀態**看起來像合法資料**。

### 採購與 committed

- 提單即入 `committed_stock`(不入帳的話,下期規劃會再建議買同一批 →
  重複採購,而且沒人會發現)。
- committed 畢業:`剩餘 = PO 台數 − 該 PO 已出現在 Inventory 的台數`。
  到貨進 in_stock 的**同時**必須扣減,否則同一台機器容量被算兩次。
- 機器到貨時從 PO **繼承 pool**——這是 `baremetal.pool` 的寫入時機。
- 不做自動 ETA:`available_from` 是人工欄位,假 ETA 比沒有更糟。

### 指標與報表

- **分母為空 → null,不是 0% 也不是 100%**。假指標比沒指標危險,因為會被
  當真。
- **可落地量 ≠ 名目量**。「還剩幾核」在碎片下說謊,可落地槽數只有 solver
  算得出來(per-BM bin-pack)。Go 端不得自行用加總冒充。
- 失敗月的數字是 what-if,**不進任何聚合**。
- 機器級採購歸屬只有概量(多張需求單合買),**node 級覆蓋才精確**。帳務
  歸屬走 `pool` 座標,不要試圖攤到 demand_id。

### Solver 狀態字串

只 branch **前綴**:`INPUT_ERROR:` / `INFEASIBLE` / `BLOCKED:` / `OPTIMAL` /
`FEASIBLE` / `UNKNOWN`。訊息內文是給人看的,會演化,**禁止 parse**。
`UNKNOWN` ≠ `INFEASIBLE`:前者是逾時(可加時間重試),後者是證明(要改輸入)。

## Commands

```bash
make test          # go test ./...
make lint          # go vet ./... + golangci-lint run
make build
make solver-up     # 起本機 solver sidecar(<solver repo>/make run,:50051)
make e2e           # 對本機 solver 跑契約整合測試
```

## 與工程師的協作約定(mentor mode)

1. **先解釋再使用**:引入新的模式(交易邊界、快照組裝策略、契約欄位語意)
   時,先用 2–3 句說明它解決什麼問題,再讓程式碼出現。
2. **決策要附替代方案**:任何非顯而易見的選擇必須說「不用 Y 是因為 Z」。
   只有「我用了 X」的說明是不完整的。
3. **核心變更先出計畫**:動到 `internal/solverclient/`、`internal/planning/`
   契約組裝、或 Inventory schema 的改動,先進 plan mode 讓人 review。
4. 對話使用**繁體中文**;程式碼、註解、commit message 用英文;ADR 繁體中文。

## Workflow

- **分支**:不推 `main`。功能分支 + `git push -u origin <branch>`,PR 由人 review。
- **每完成一個任務就 commit**,訊息要說清楚做了什麼、為什麼。
- **核心變更必須有 ADR**:契約組裝、候選推導、帳本語意、Inventory schema
  的改動要在 `docs/decisions/` 留紀錄(用 `/adr` skill)。Stop hook 會擋。
- **端到端驗證**:契約相關改動不能只跑單元測試——用 `/verify-e2e` 對真的
  solver 跑一遍,確認 request 組得出來、response 解得開。
- **單一事實來源**:不複製 solver 的模型定義到多處;`internal/solverclient/`
  是契約的唯一封裝層。不開 `*_v2.go` / `enhanced_*.go`。
