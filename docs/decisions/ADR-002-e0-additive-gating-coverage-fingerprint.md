# ADR-002: E0 批次——committed 生效月閘門、per-demand 覆蓋標註、config 指紋

- **日期**: 2026-07-28
- **作者**: Claude (claude-code)
- **相關 PR / commit**: `2ff9998`(S1)、`d77552b`(S2)、`2cda45e`(S4)
- **影響範圍**: `app/models.py`、`app/capacity_planner.py`、`app/splitter.py`、
  `app/solver.py`、`app/split_solver.py`、對應測試、`examples/capacity/plan_two_fabs.json`

> 寫作對象:一位正在學習 CP-SAT 與排程系統設計的工程師。

## 1. 背景與問題

e2e-vision.md 的 roadmap E0。三個缺口:(a) committed stock(已購未到貨)在
多月規劃裡被當成「期初就可用」,系統性高估前期容量——而到貨時程受供貨與人為
因素浮動,無法自動推 ETA;(b) 引擎知道每台規劃 VM 落在 in-stock、committed
還是新採買上,但報表聚合後就丟了,需求單追蹤 UI 與承諾兌現率都需要這個
per-demand 分解;(c) 回應無法對應到「產生它的 config + 引擎版本」,規劃/執行
一致性(D2/D4 漂移)無從量測。三者都不動 CP-SAT 模型——是資料面預過濾、
結果萃取、回應 metadata。

## 2. 考慮過的方案

**S1 生效月表示法**
- 全域固定 lead-time 參數——假精準,實際到貨月因單而異。**排除**。
- 維持現狀(當期可用)——正是要修的高估。**排除**。
- ✅ `available_from: "YYYY-MM" | None` 人工維護,None=當月可用(向後相容)。
  另設 format validator:`period` 欄位雖無驗證先例,但格式錯的月份(如
  `"2026/07"`,`/` 字典序在 `-` 後)會排在所有合法月之後、**沉默地永久封鎖
  該筆庫存**——沉默失效比 422 危險得多。

**S2 assignment → requirement 的歸屬鏈**
- 解析合成 id `split-r{idx}-s{s}-{k}` ——可行,但讓 id 格式變成隱性契約,
  改字串就斷。**排除**。
- ✅ splitter 在鑄造 VM 時記顯式映射 `vm_req_of[vm_id] = req_idx`;
  splitter 實例本來就掛在 `_Pass.splitter` 上,拿得到。

**S4 蓋章位置**
- 每個 response literal 各自帶欄位(~10 處)——未來新增 early-return 會漏蓋。**排除**。
- server.py endpoint 層蓋——CLI 路徑與函式庫呼叫直呼 `solve()`,會漏。**排除**。
- ✅ 公開入口 thin wrapper(`solve` → `_solve`),「每條 return 都蓋章」變成
  結構保證;單一 literal 的 `solve_capacity_horizon` 直接加 kwarg 即可。

## 3. 最終決策

三個 additive 改動一批交付:`CommittedStock.available_from` 在多月規劃的
唯一月度過濾點閘控(單發 procure 無期別概念,欄位忽略);
`ProcurementResult.requirement_coverage` 由 `_to_result` 以顯式映射計算,
horizon 依索引 join 回 `DemandEntry`(新透傳欄位 `demand_id`)產出
`PeriodFabReport.demand_coverage`;四個回應模型統一回吐
`config_fingerprint` = sha256[:12](canonical config + engine + ortools 版本)。

## 4. 實作走讀

- `capacity_planner.py:735-738` — 月度閘門。這裡是全 codebase 唯一一處把
  committed 池轉成當月請求的地方,而且**同一個 list 物件**同時是
  `committed_entry_used` 的索引空間與 `_roll_forward` 的 drain 目標,在此
  過濾故索引永不偏移;list 持有跨月狀態物件的參照,drain 後餘量自然帶到下月。
  ISO "YYYY-MM" 的字典序即時間序,`<=` 使閘門含當月。
- `splitter.py:244` — 在 active BoolVar 誕生的同一行旁記 `vm_req_of`。這屬
  Step C(建模)階段的 bookkeeping:每個合成 VM slot 的 `active_var` 決定它
  存不存在,而歸屬關係在鑄造時就已確定,不必等解出來再反推。
- `capacity_planner.py:575-597` — 覆蓋分類在 `_to_result`(Step D 萃取後),
  天然位於 roll-forward 改名(`acq-{period}-`)**之前**;分類邏輯是集合
  membership:`buyable_type_of` → new_buy、`committed_type_of` → committed、
  其餘必為 in-stock(候選只來自 in_stock ∪ virtual,窮盡且互斥)。
  上月買的機器本月已 materialize 成 in-stock,故算 in_stock——與
  `in_stock_bm_used` 語意一致,文件化於 `RequirementCoverage` docstring。
- `models.py:404-422` — 指紋:`json.dumps(sort_keys=True)` 遞迴排序即
  canonical form(蓋掉 `target_spread` 的插入序),list 順序(`vm_specs`)
  刻意保留語意;版本經 `importlib.metadata` + `lru_cache`(行程常數)。

## 5. 取捨與風險

`available_from` 靠人工維護,過期失真只能靠(未來的)供給命中率量測,不會
自動修正。覆蓋分類只涵蓋 requirement-driven 的合成 VM,顯式 `request.vms`
刻意不計入——若未來顯式 VM 也要歸屬,需另立 key。指紋把 ortools 升版也算
「config 變了」,這是刻意的(packing 形狀可能變),但代表例行升版會讓所有
指紋翻新——reconcile 端要把「指紋不同」當訊號而非錯誤。BM 台數仍無法精確
歸屬到單一 demand(joint solve 合買),coverage 是 node 級精確、BM 級概量。

## 6. 你應該帶走的知識

- **沉默失效比爆炸危險**:字典序比較的欄位,格式錯不會丟例外、只會永遠排
  在最後——這種欄位必須在入口驗證,即使鄰近欄位沒有驗證先例。
- **索引當外鍵時,過濾點要唯一**:`committed_entry_used` 以 list 索引為鍵,
  能安全過濾的前提是「建索引空間」與「用索引空間」共用同一個 list 物件。
- **蓋章類欄位用 wrapper 不用散裝**:回應 metadata 要「每條 return 必有」,
  唯一結構性的做法是把多 return 函式包一層,而不是逐 literal 補欄位。

## 7. 驗證方式

- `tests/test_capacity_planner.py::TestCommittedAvailableFrom`(7 條:閘門、
  含當月、餘量跨月、索引不偏移、超 horizon、格式 422、procure 忽略)、
  `::TestRequirementCoverage`(6 條:總和不變量、三來源、排除顯式 VM、
  失敗全零、demand_id 回聲、跨月 in-stock 語意)。
- `tests/test_splitter.py::TestVmReqOfMap`、
  `tests/test_solver.py::TestConfigFingerprint`(格式/決定性/dict 序不敏感/
  權重敏感/跨行程/INPUT_ERROR 也蓋章)。
- 手驗:POST `examples/capacity/plan_two_fabs.json` 到 `/v1/capacity/plan`
  ——fab-a 2026-01 `committed_bm_used == 0`(閘門)、2026-02 三來源
  coverage `(8, 8, 8)`、回應帶 12-hex `config_fingerprint`。
