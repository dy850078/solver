# Topology Visualizer Web UI

> **作者**: Claude Opus 4.7
> **日期**: 2026-05-20
> **狀態**: Implemented
> **相關分支**: `claude/solver-topology-visualization-YPHgt`
> **PR**: [#11](https://github.com/dy850078/solver/pull/11)

---

## 目錄

1. [Summary](#1-summary)
2. [Goals / Non-Goals](#2-goals--non-goals)
3. [Current State & Problem](#3-current-state--problem)
4. [Architecture](#4-architecture)
5. [Backend Design](#5-backend-design)
6. [Frontend Design](#6-frontend-design)
7. [Visualization Strategy](#7-visualization-strategy)
8. [Tech Stack Rationale](#8-tech-stack-rationale)
9. [File Layout](#9-file-layout)
10. [Operational Notes](#10-operational-notes)
11. [Future Work](#11-future-work)

---

## 1. Summary

新增一個輕量化的單頁 Web UI,讓使用者能直接在瀏覽器內:

1. 透過上傳 JSON、貼到編輯器、或從 `examples/` 載入,組出 PlacementRequest
2. 呼叫 `/v1/placement/solve` 或 `/v1/placement/split-and-solve`
3. 以**Rack 機架圖**檢視 placement 結果,並可依任一拓樸維度 (Site / Phase / DataCenter / Room / Rack / AG) 重新分組
4. 透過 **AG × Rack 反親和矩陣**一眼驗證 anti-affinity 是否如預期分散

技術棧採用**純 HTML + 原生 ES Modules + D3 / CodeMirror via CDN**,**無 npm、無 build step**,由 FastAPI 直接以 StaticFiles 掛載在 `/ui`。

---

## 2. Goals / Non-Goals

### Goals

- 提供視覺化工具讓使用者**快速驗證 placement 結果是否符合預期** (例如「同 AG 的 master 是否真的散到不同 rack?」)
- 支援 `solve` 與 `split-and-solve` 兩個端點
- 處理現實資料的非齊整性:有些 BM 只有 `ag` 維度、有些有完整拓樸,UI 都要 degrade gracefully
- 維運成本最小化:單一 FastAPI process、無前端 build pipeline、修改即生效

### Non-Goals

- **不**支援多 user / 多 session、認證、權限管理 (此 UI 是 dev/ops 內部工具)
- **不**支援即時 (real-time) 重算或編輯 placement;每次都是「貼 input → 跑 → 看結果」
- **不**做 placement 的編輯介面 (表單建構 VM/BM),只接受 JSON
- **不**做後端持久化 (沒有歷史紀錄、沒有 DB);所有狀態存在瀏覽器 memory
- **不**支援離線部署 (依賴 jsdelivr CDN 載入 D3);未來若需要再 vendor 化

---

## 3. Current State & Problem

在此 PR 之前,solver 只有 JSON API 介面。驗證 placement 結果的唯一方法是:

```bash
curl -X POST :50051/v1/placement/solve -d @request.json | jq
```

然後肉眼比對 JSON,例如:

```json
{
  "assignments": [
    {"vm_id": "m-1", "baremetal_id": "bm-r0-a", "ag": "ag-1"},
    {"vm_id": "m-2", "baremetal_id": "bm-r1-a", "ag": "ag-1"},
    ...
  ]
}
```

要回答「m-1, m-2, m-3 是否真的散到不同 AG?」必須:
1. 把 `bm_id` 對到 baremetals[].topology 找 rack/room/AG
2. 心算 cross-check
3. 還要記得 anti-affinity rule 的 `spread_on` 與 `cap_per_bucket`

實務上很容易看走眼,尤其是 `target_spread` 違反但 solver 仍回 OPTIMAL (因為是 advisory) 這類 corner case。

---

## 4. Architecture

```
┌─────────────────────┐         ┌──────────────────────────────┐
│  Browser (Chrome /  │         │  FastAPI process (port 50051)│
│  Safari / Firefox)  │  HTTP   │                              │
│                     │ ◄─────► │  /v1/placement/solve         │
│  /ui (single-page)  │         │  /v1/placement/split-and-solve│
│  - vanilla JS ESM   │         │  /api/examples               │
│  - D3 @7 from CDN   │         │  /api/examples/{name}        │
│  - dark tooltip,    │         │  /ui  ← StaticFiles mount    │
│    rack cards,      │         │  /docs (Swagger)             │
│    AG×Rack matrix   │         │  /health                     │
└─────────────────────┘         └──────────────────────────────┘
```

* **Same-origin**: 前端與 API 都在 `:50051`,無 CORS 設定需求
* **無中介 BFF**: 瀏覽器直接打 solver 的端點;solver 是 sidecar 設計,Go scheduler 本來就是直接呼叫,UI 只是再一個 client
* **無 build artifact 入 repo**: 前端就是 source = served files,改 `.js`/`.css`/`.html` 直接生效

---

## 5. Backend Design

### 5.1 Static mount

`app/server.py` 在現有 `/swagger-static` 之後新增:

```python
_WEB_STATIC_DIR = Path(__file__).parent / "web_static"
if _WEB_STATIC_DIR.is_dir():
    api.mount("/ui", StaticFiles(directory=str(_WEB_STATIC_DIR), html=True), name="ui")

    @api.get("/", include_in_schema=False)
    def root_redirect() -> RedirectResponse:
        return RedirectResponse("/ui/")
```

* 條件式掛載: 若 `web_static/` 不存在 (例如純 CLI 部署) 就不掛
* 根路徑 `/` 自動轉到 `/ui/`,方便 bookmark

### 5.2 Examples API

`app/examples_api.py` 提供兩個端點,讓前端載入 `examples/*.json`:

| Endpoint | 用途 |
|---|---|
| `GET /api/examples` | 列出 `examples/*.json`,並猜測 endpoint hint (檔名以 `split_` 開頭 → `split-and-solve`,否則 `solve`) |
| `GET /api/examples/{name}` | 回傳檔案 JSON 內容 |

**Path traversal 防護**:

```python
_NAME_PATTERN = re.compile(r"^[\w\-.]+\.json$")

def get_example(name: str) -> dict:
    if not _NAME_PATTERN.match(name):
        raise HTTPException(status_code=400, detail="invalid example name")
    target = (_EXAMPLES_DIR / name).resolve()
    target.relative_to(_EXAMPLES_DIR)  # raises if escapes
    ...
```

雙重防護:regex 拒絕 `/`、`..`、空白等;`relative_to()` 在 resolved path 跨出 examples 目錄時拋 `ValueError`。

---

## 6. Frontend Design

### 6.1 模組分工

```
app/web_static/
├── index.html                # 主框架 + import map
├── styles.css                # design tokens + 元件樣式
└── js/
    ├── main.js               # 應用入口、事件綁定、orchestration
    ├── api.js                # fetch 包裝 (listExamples/getExample/solve/splitAndSolve)
    ├── colors.js             # AG → 色票 (Tailwind 500 系列 10/16 色)
    ├── rackdiagram.js        # 核心:buildPanels + renderRackDiagram
    ├── matrix.js             # AG × Rack 矩陣 builder + renderer
    ├── tooltip.js            # 共用浮動 tooltip
    └── summary.js            # 狀態 stat cards、assignments 表、unplaced banner
```

### 6.2 State 模型

`main.js` 持有一個極簡 state:

```js
const state = {
  request: null,    // 最後一次成功送出的 PlacementRequest
  result: null,     // solver 回的結果
  groupBy: "rack",  // 視圖分組維度 (site/phase/datacenter/room/rack/ag)
};
```

UI 操作流程:

```
[Load example/upload/paste]
       ↓
[Run] → POST → state.{request, result}
       ↓
rerenderViz() ──┬─ buildPanels(req, res, groupBy) → rack diagram
                ├─ buildAgRackMatrix(req, res)    → matrix card
                └─ rebuildColorScale + legend     → AG 色票

[Group by ▼]   → state.groupBy = e.value
                → rerenderViz()
```

### 6.3 Import map (CDN)

```html
<script type="importmap">
{ "imports": { "d3": "https://cdn.jsdelivr.net/npm/d3@7.9.0/+esm" } }
</script>
```

D3 鎖具體版本 `7.9.0`,避免上游飄移。若未來改成 vendored 版本,只需在 `web_static/vendor/` 放一份 D3 並改 import map,前端程式碼不動。

---

## 7. Visualization Strategy

設計過程歷經三輪迭代,記錄如下供未來參考。

### 7.1 v0: Treemap (廢棄)

首版用 D3 treemap 呈現 `site > phase > dc > room > rack > BM > VM` 的 6 層嵌套矩形。

**問題**:
- 深層結構下,中間層 (room/rack) 的 label 容易被擠壓掉
- 看不出「同一 AG 是否真的散到不同 rack」這種**驗證型問題** — Treemap 表達面積/占比,但 placement 驗證主要是**集合關係**
- 標籤密度高,「看起來偏老舊、有點擠」(使用者回饋)

### 7.2 v1: Rack diagram (採用)

每個拓樸群組是一張卡片,卡內 BM 像實際機架的 1U server 一行行排列,VM 是 BM 內的色塊 pill (色 = AG)。

```
┌─ site-a › p1 › dc-1 / room-0 ─── 3 BM · 3 VM ─┐
│  rack-1                                        │
│  ┌────────────────────────────┐                │
│  │ bm-r0-a              [ag-1]│                │
│  │ [m-1]                      │                │
│  └────────────────────────────┘                │
│  rack-2                                        │
│  ...                                           │
└────────────────────────────────────────────────┘
```

**關鍵設計**:

* **Card 內 sub-group**: 當分組維度比 rack 淺 (Site/Phase/DC/Room) 時,panel 內以 mono 小標題分出每個 rack,避免「展平成扁平 BM 列表」失去結構感
* **AG 標籤在 BM header**: 不用點開、不用 hover,直接看到該 BM 的 AG 隸屬 (用 `color-mix` 配 18% 不透明度)
* **空 BM 用斜紋背景**: 視覺上既存在又區隔,提示「有可用容量但 solver 沒放東西」
* **VM chip 帶 hover state**: filter brightness + 浮起,接觸目標清楚

### 7.3 v2: AG × Rack matrix (補充)

Rack diagram 是 lens,但驗證反親和散落仍需要橫切視角。新增一張矩陣卡:

| AG \ Rack | rack-1 | rack-2 | rack-3 | rack-4 | rack-5 | rack-6 | Σ | spread |
|---|---|---|---|---|---|---|---|---|
| **ag-1** | 1 | | | 1 | | | 2 | × 2 |
| **ag-2** | | 1 | | | 1 | | 2 | × 2 |
| **ag-3** | | | 1 | | | 1 | 2 | × 2 |

* 行 = AG (含 swatch),列 = rack (full path on hover),cell = VM 數
* Cell 透明度: `--cell-alpha = min(0.95, 0.22 + count × 0.18)`,以 AG 顏色疊在白底
* Sticky 左欄與表頭,寬資料水平捲動
* 每行末標 `× N` 表示該 AG 跨多少 rack — **反親和驗證一秒看完**
* 無 rack 維度時自動 fallback 為 AG × BM,corner cell 寫 `AG \ BM`

### 7.4 Group-by 維度

下拉選單 (`<select id="group-by">`) 提供 6 種視角:

| 選項 | Panel 是什麼 | Sub-group |
|---|---|---|
| Site | 每個 site 一張 panel | rack 層 (`p › dc › room › rack`) |
| Phase | (site/phase) | `dc › room › rack` |
| DataCenter | (site/phase/dc) | `room › rack` |
| Room | (site/phase/dc/room) | `rack` |
| Rack (預設) | 每個 rack | — (panel 已是最細) |
| AG | 每個 AG,帶 accent border | — |

**核心邏輯** (`rackdiagram.js`):

```js
function panelKeyFor(bm, groupBy) {
  if (groupBy === "ag") return bm.topology?.ag || "(no ag)";
  const idx = PHYSICAL_DIMS.indexOf(groupBy);  // 0..4
  const segs = PHYSICAL_DIMS.slice(0, idx + 1)
                            .map(d => bm.topology?.[d] || "");
  return segs.every(s => !s) ? "(no topology)" : segs.join("|");
}

function subGroupKey(bm, groupBy) {
  if (groupBy === "ag" || groupBy === "rack") return "";
  const idx = PHYSICAL_DIMS.indexOf(groupBy);
  const segs = PHYSICAL_DIMS.slice(idx + 1)
                            .map(d => bm.topology?.[d] || "");
  while (segs.length && !segs[segs.length - 1]) segs.pop();
  return segs.join("|");
}
```

設計上 panel key 是 prefix,subgroup key 是 suffix,合起來就是 BM 的完整 physical path,**保證資訊不遺失**。

### 7.5 缺維度的 degradation

- `sample_2vm_9bm.json` 只有 `ag` 維度:
  - Group by Site / Phase / DC / Room / Rack → 一張 `(no topology)` 卡,內含全部 9 個 BM
  - Group by AG → 3 個 AG panel,每個 3 個 BM
  - Matrix fallback 為 AG × BM
- `master_learner_2room.json` 有完整 6 層:
  - 各 group by 都產出對應深度的 panel + sub-group

---

## 8. Tech Stack Rationale

| 評估項 | 純 HTML + ES Modules + CDN | Vite + React + D3 | Streamlit |
|---|---|---|---|
| Build step | 無 | npm build | 無 |
| Node.js 依賴 | 無 | 必要 | 無 |
| 部署 artifact | 6 個 .js 檔 | dist/ (多 chunks) | Python script |
| 修改成本 | 改檔即生效 | rebuild + 重起 | 重起 |
| 客製能力 | 高 | 高 | 中 (受限於元件) |
| 適合範圍大小 | 小到中型 (~10 元件) | 中到大型 | 小型 demo |

**決策依據**: 此 UI 只有 ~7 個元件、單一狀態流、無路由、無 form 重用,React 帶來的 component 模型與 state 管理收益有限,卻必須換來 Node toolchain 與 build pipeline 的維運成本。**核心原則:不過度複雜化**。

未來若 UI 範圍擴大 (例如要加多頁、複雜表單、即時編輯),遷移到 Vite + React 並非難事:D3 邏輯 (`rackdiagram.js`/`matrix.js`) 可直接保留,只需把 DOM 操作換成 JSX。

---

## 9. File Layout

新增:

```
app/
├── examples_api.py                  # GET /api/examples, /api/examples/{name}
└── web_static/
    ├── index.html
    ├── styles.css
    └── js/
        ├── main.js                  # 入口
        ├── api.js                   # API client
        ├── colors.js                # AG → color
        ├── rackdiagram.js           # buildPanels + render
        ├── matrix.js                # AG×Rack matrix
        ├── tooltip.js               # 浮動 tooltip
        └── summary.js               # stat cards + tables
```

修改:

* `app/server.py`: 掛載 `/ui`、`/` redirect、註冊 `examples_api.router`

---

## 10. Operational Notes

### 10.1 啟動

```bash
python -m app.server --port 50051
# → http://localhost:50051/  (自動轉到 /ui/)
```

無需額外 setup;FastAPI 自動偵測 `app/web_static/` 存在並掛載。

### 10.2 修改前端

直接編輯 `app/web_static/**`,瀏覽器強制重新整理 (Cmd-Shift-R / Ctrl-Shift-F5) 即可。沒有 hot reload,但因為是純檔案,reload 成本低。

### 10.3 CDN 依賴

前端 runtime 需要連到 `cdn.jsdelivr.net` 載入 D3 與 Inter 字型。在純內網或 air-gapped 環境會無法載入。緩解方式:

1. 下載 D3 ESM bundle 到 `web_static/vendor/d3.js`
2. 改 `index.html` 的 import map: `"d3": "/ui/vendor/d3.js"`

字型 fallback 到 system font stack (`-apple-system`, `Segoe UI`, etc.),不致破版。

### 10.4 安全考量

* 後端 examples API 嚴格驗 filename,只允許 `[\w\-.]+\.json` 格式並 enforce 路徑落在 `examples/` 內
* 前端對所有 user-controlled 字串 (VM ID、hostname、AG 名稱等) 在 render 時呼叫 `escapeHtml()`,避免 XSS
* 沒有 cookie / session / token 概念,本工具假設網路邊界可信 (例如部署在 dev cluster 內網)

---

## 11. Future Work

以下不在本 PR 範圍但已知值得考慮:

* **Solver 重跑與差異對比**: 比較兩次 solve 結果 (例如改 anti-affinity 規則前後)
* **Spread target 視覺化**: `config.target_spread` 若違反 (但 solver 仍回 OPTIMAL),在 matrix 上高亮對應 row
* **Capacity heat overlay**: BM 卡片內加 CPU/Mem/Storage 使用率 bar,直接看出哪台 BM 接近滿載
* **離線打包**: 把 D3 / Inter 字型 vendor 化進 `web_static/vendor/`,設置選項切換 CDN / vendor
* **大規模資料 perf**: 1000+ BM 時 DOM 節點會多,考慮虛擬捲動或 canvas-based BM 列表

---

## Decision Log

| Decision | Reason | Follow-ups |
|---|---|---|
| 採用純 HTML + ES Modules,放棄 React | 範圍小、維運成本最低化、改檔即生效 | 若 UI 範圍擴大,評估遷移 |
| 主視圖選 Rack diagram 而非 Treemap | Treemap 無法表達「集合關係」驗證,且深層 label 易遺失 | — |
| 補一張 AG × Rack 矩陣 | 反親和驗證是 placement 工具最核心的用途 | 可考慮加 `target_spread` 違反高亮 |
| Group by 用 dropdown 而非多 segmented | 6 個選項;segmented 視覺壓力大 | — |
| Matrix 不跟著 Group by 動 | Matrix 是驗證工具,panel 才是 lens | 未來可考慮獨立的 matrix 維度切換 |
| 後端 examples API 用 regex + `relative_to` 雙重防護 | 兩道防線降低 path traversal 風險 | — |
| 暫不 vendor CDN | 內網部署為次要場景;CDN 啟動成本零 | 若有離線需求再加 vendor 機制 |
