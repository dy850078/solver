# ADR-018: Compare sets——目錄 + 引用的情境清單，包在 mock generator 外面做多情境 sizing 比較

- **日期**: 2026-09-29
- **作者**: Claude (Fable)
- **相關 PR / commit**: branch `claude/busy-pasteur-vmxewn`
- **影響範圍**: `app/compare.py`（新）, `app/mockgen.py`（`GenerateResponse.verified`）,
  `app/server.py`, `app/examples_api.py`, `app/web_static/compare.html` + `compare.css` +
  `js/compare.js` + `js/compare-form.js` + `js/compare-set.js`, `js/mockform.js`（匯出卡片）,
  `js/main.js` + `index.html`（Add to compare set）, `examples/compare/`,
  `docs/compare-sets.md`, `tests/test_compare.py`

> 寫作對象:一位正在學習 CP-SAT 與排程系統設計的工程師。

## 1. 背景與問題

使用者要比較「A 機型 + (master×5 / infra×5 / l4lb-storage×3) + 3 cluster 共住」這類
組合在 BM 機型、套餐、cluster 數（以及 tightness、failover 等其他 knobs）上的變化。
現況是在 Topology 頁一次填一份 mock 設定、求解、抄數字進表格；機型新增或套餐改了
就全部重來。

repo 內沒有任何批次或多情境機制；`docs/capacity-planning.md` 早把「what-if 多情境
比較報表」列為未來項目。三個現成引擎各有不合：`rollout_sizing` 單機型、只變台數、
每個 probe 跑完整 rollout；capacity planner 吃 demand book + in_stock，多機型是讓 solver
混選，與「逐一比較機型」相反；`mockgen` 反而最接近——它已經參數化了機型
（`bm_profiles`，`count=None` 即彈性 sizing）、套餐（`node_groups`）、cluster 數與所有
knobs，`generate()` 會用真 solver 驗證並 escalate 到可行。缺的只有兩件事：它把驗證用
的 `PlacementResult` 丟掉（只留 `solver_status` / `unplaced`），而且一次只做一份。

## 2. 考慮過的方案

1. **共用 knobs + 三個軸的笛卡兒積**（`bm_models × bundles × cluster_counts`）——最
   像「矩陣」，但使用者明確要求其他 knobs（tightness、failover…）也能逐情境不同；
   而且笛卡兒積會產生一堆沒人要看的格子。**排除。**
2. **自包含情境**：set = `[{name, params: 完整 GenerateRequest}]`，重用靠 duplicate——
   最簡單、與 Topology 表單一對一，但改一個機型要逐一改 N 份，正是使用者的痛點：
   「重跑」自動化了，「改設定」沒有。**排除。**
3. **目錄 + 引用**（採用）：set 內有 `vm_specs` / `bm_models` / `bundles` 三個目錄與
   `defaults`，`scenarios` 是**明確清單**，每筆用名字引用目錄、可 `overrides` 任何
   knob。改一個機型的容量，所有引用它的情境重跑即更新；「Generate grid…」只是批次
   新增的便利，不是資料模型。缺點是 UI 多一層（先建目錄再組情境）；但目錄的每一塊
   都是現成元件。
4. **UI 端迴圈呼叫 `/api/mock/generate`**，不加後端——省一個模組，但指標（density、
   utilization、per-BM 擺放）得在前端從 `request` + `ground_truth` 拼，而 `ground_truth`
   是 greedy 佈局不是 solver 結果；curl / script 也用不到。**排除：表格是產物，要能
   離線產生。**

儲存位置也考慮過伺服器端（`output/compare-sets/` + CRUD API）。**排除**：sidecar 目前
完全無狀態，`ENABLE_UI` 是唯一的環境開關；為儲存功能引入可寫目錄與檔名衝突處理，
價值要等真有多人共用需求才成立。格式相同，日後只需加 API。採用：檔案下載 / 上傳 +
瀏覽器草稿（localStorage）+ `examples/compare/` 範本。

## 3. 最終決策

一份 JSON（`CompareSet`）同時餵 UI、`POST /api/compare/run` 與 `python -m app.compare`。
每個情境解析成一份 `GenerateRequest` 交給 mockgen；`GenerateResponse` 新增 optional
`verified: PlacementResult`，讓比較不用重解就拿到 solver 的實際擺放。每格回兩個台數：
`bm_fleet`（generator 配置的採購數）與 `bm_used`（solver 實際擺上的），加上單 cluster
平均、`bm_by_cluster` 精確值、VM density、各維度利用率、per-BM 擺放。循序執行；單格
的 mockgen 400 或不合法 override 是 `error` 列，不拖垮整組；結構性錯誤才 422。

## 4. 實作走讀

- `app/compare.py:_check_knob_keys` —— `defaults` / `overrides` 的 key 用
  `GenerateRequest.model_fields` 白名單檢查，並排除四個保留欄位。理由：pydantic 預設
  **忽略**未知 key，`{"tightnes": 0.5}` 會靜默跑成預設值，比較表就會騙人。這是
  「契約錯誤回 422，不默默修正」慣例在這裡的具體形狀。
- `app/compare.py:scenario_metrics` —— 純函式，輸入 `GenerateResponse` + 解析後的
  request。擺放類指標一律讀 `resp.verified`（solver），不讀 `ground_truth`（greedy）：
  greedy 只證明「放得下」，solver 才是「會怎麼放」。`bm_used` 與 `bm_fleet` 刻意分開回
  傳：前者受 `w_consolidation` / `w_headroom` 權重影響，後者有 `num_ags` 下限且
  escalation 只加不減，兩者語意不同，合成一個數字會誤導採購。
- `app/compare.py:run_scenario` —— 兩層 `try`：`resolve_scenario` 的 `ValidationError`
  （knob 值不合法）與 `generate_mock_request` 的 `HTTPException`（mockgen 400，例如
  超過 `_MAX_ELASTIC_BMS`）都變成 `status="error"` 的列。一個太小的機型不該讓另一個
  機型的欄位消失。
- `app/mockgen.py:generate` 末尾 —— 只加一行 `verified=result`。既有消費端（UI、
  測試）只讀舊欄位，payload 多一份 assignments 可接受。
- `app/web_static/js/compare-set.js:addMockParams` —— Topology 頁「Add to compare set」
  把一份 mock 參數拆進 set：specs 進目錄、每個 `bm_profile` 成一個機型、node groups
  成一個 bundle（名稱由 `bundleLabel` 推導，如 `master5-infra5-l4lb-storage3`），其餘
  knobs 第一次進 `defaults`、之後只有差異進 `overrides`。名稱衝突且內容不同時加
  `-2` 後綴，不覆蓋。

## 5. 取捨與風險

- **同 seed 不保證同擺放**：`resolved` 與 `bm_fleet` 可重現，但 CP-SAT 多 worker 在同
  分解之間的選擇會變。測試只比 sizing，不 diff 擺放；文件寫明「比較看數字」。
- 每格最壞 11 次 solve（escalation）× time limit；deadline 只能在格與格之間跳過。
  UI 逐格呼叫繞過了這個問題（每次 request 只有一格），批次呼叫者靠 `max_scenarios`。
- 同步 handler 整組占一個 threadpool worker。目前使用情境是單人工具，可接受；若變成
  服務，應改成 job + polling。
- `bm_per_cluster_avg` 在 cluster 共住時是平均而非分攤：一台 BM 上有三個 cluster 的
  VM，會被三個 cluster 各算一次。`bm_by_cluster` 給精確值，表格顯示平均、detail 顯示
  精確。
- 訊號：若出現「同一個 role 要參與多條 rule」「情境之間要共用一台已存在的機隊」
  （brownfield），代表該把 compare 接到 `/v1/placement/solve` 或 rollout，而不是繼續
  擴 mockgen。

## 6. 你應該帶走的知識

- **設定是資產，結果是產物**：把人手抄的表格變成「可重跑的輸入檔 + 可重產的輸出
  檔」，重點不在 UI 而在檔案格式——目錄 + 引用讓一處修改傳播到所有情境。
- **兩個相似的數字不要合併**：`bm_used`（solver 的選擇）與 `bm_fleet`（generator 的配
  置）語意不同，合成一個「需要幾台」會讓採購者拿到錯的數。寧可多一欄，把差異解釋
  在表頭 tooltip。
- **靜默忽略是契約的敵人**：pydantic 的 extra-ignore 對 API 很友善，對「比較表」是陷
  阱。凡是使用者以為有生效的 key，都要有驗證。
- **失敗隔離的粒度要對齊使用者的心智模型**：使用者看的是「A 機型這一欄」，所以一
  格壞了只有一格壞；整組 422 只留給「檔案本身不合法」。

## 7. 驗證方式

- `tests/test_compare.py`（29 個測試，含 9 個參數化的 422 案例）：解析與合併、九種 422、指標定義（`bm_used ==
  distinct BM`、density、utilization ∈ (0,1]、per-BM util ≤ 1、`bm_by_cluster`）、單格
  error 隔離、infeasible 指標為 None、`only` / `enabled` / deadline / `max_scenarios`、
  CSV 欄位、CLI、兩個 endpoint、examples 可 parse。`tests/test_mockgen.py` 加 `verified`
  兩個測試。全套 474 綠。
- `python -m app.compare --input examples/compare/control_plane_sizing.json --csv
  output/compare.csv`：A-64c × cp-large × c3 = 6 台（每 cluster 2.0）、B-96c = 5 台
  （利用率較低）；cp-small 兩機型皆 3 台。
- Playwright smoke：Topology 頁「Add to compare set」→ Compare 頁草稿還原 → 載入範本
  → Run all 六格 ok → 依機型 / 依套餐分組 → 點列看 per-BM 擺放 → 匯出按鈕啟用。
