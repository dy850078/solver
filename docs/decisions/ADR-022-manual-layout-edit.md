# ADR-022: PM Demand 的擺放可以手動拖拉調整——用「鎖定重解」驗證每一步，而不是另寫規則檢查器

- **日期**: 2026-11-19
- **作者**: Claude（與工程師共同決策）
- **相關 PR / commit**: branch `claude/layout-edit`
- **影響範圍**: `app/bm_demand.py`（`ManualMove`、`LayoutEditRequest/Result`、`edit_layout`、`_validate_group_layout`、`_accumulate_rules`、`_assemble_reports`）、`app/server.py`（`POST /v1/placement/bm-demand/edit-layout`）、`app/web_static/js/layout-edit.js`（新）、`demand.js`、`rackdiagram.js`、`api.js`、`demand.html`、`demand.css`、`tests/test_bm_demand.py::TestLayoutEdit*`

> 寫作對象：一位正在學習 CP-SAT 與排程系統設計的工程師。
> 寫作口吻：資深工程師帶 code review。全文繁體中文，程式碼與術語保留英文。

## 1. 背景與問題

PM Demand 跑出的 placement（ADR-020）只能看、不能改。實務上 PM 看到 rack diagram 之後常會想「這兩顆對調」「這顆挪到那台空機」，
而 legacy pipeline export 是施工文件，手改之後必須仍然是**合法**的擺放：容量（C2）、AG 分散（auto C3，含 ADR-021 的 label 分群）、
獨佔（C6）、最小機器數（C7）一個都不能破。矩陣、layouts、legacy template 也要跟著變，否則 UI 在說謊。

工程師已定案的邊界：(1) 嚴格鎖定——solver 不得「順便」搬別的 VM；(2) 違規不能強制接受；(3) 目標函數不參與（這是驗證，不是最佳化）；
(4) 只能在同一個 pool 內搬；(5) 結果要可追溯（`manual_moves`）；(6) 先按 Edit layout 再拖，避免誤拖。

核心問題只有一個：**怎麼判斷「搬完之後」還合法，而且判斷邏輯和產生計畫的邏輯是同一份？**

## 2. 考慮過的方案

### 方案 A：前端自己檢查 C2 / C3 / C4
- 做法：JS 讀 chip 的 demand 與機器容量，算 AG 計數。
- 優點：零延遲、不打 server。
- 缺點：第二份約束實作。ADR-017 §6 已經踩過一次——UI 與 solver 對「分群」的理解漂移，bug 很難被發現。
  C3 的 cap `⌈n/|AG|⌉` 還牽涉 `auto_group_labels`；C7 要數 distinct machines；GPU 是每型號一維。這些都會再寫錯一次。
- **排除**：規則只能有一個 source of truth，就是 `VMPlacementSolver`。

### 方案 B：把現況全部 `pinned_to`，再解一次
- 做法：每顆 VM `pinned_to=目前機器`，丟給 solver。
- 缺點：ADR-012 明確規定 pin 是**事實**，C3/C4/C5 的 cap 會 grandfather 成 `max(cap, pinned count)`，
  也就是說三顆 AAA 擠進同一個 AG 也「合法」——驗證永遠通過，等於沒驗。
- **排除**：拿「既往不咎」的機制來驗證新狀態，語義相反。

### 方案 C：只鎖被搬的那顆，其餘交給 solver 重排
- 做法：被搬的 VM `candidate_baremetals=[目標]`，其餘自由。
- 缺點：違反「嚴格鎖定」。solver 為了讓這一步可行，會把別的 VM 搬走，使用者看到的圖和自己拖的不一樣，legacy export 跟著變動。
- **排除**：PM 要的是「我的擺放合不合法」，不是「有沒有某個擺放合法」。

### 方案 D（採用）：全員 `candidate_baremetals=[目前機器]` 的鎖定重解
- 做法：pool 內每顆 VM 的候選機器只有一台（被搬的 = 新機器），`pinned_to=None`，機器 `used_capacity` 歸零，
  rules 從該 pool 的 compiled steps 累積，config 用原請求的。C1 被鎖死後模型只剩一個候選解，solve 就是 feasibility check。
- 優點：驗證與產生計畫是同一份模型；INFEASIBLE 時 `diagnostics.constraint_check.failed_at` 直接點名哪一層；
  通過後報表是 `(assignments, vms, bms)` 的純函數，重算即可。
- 缺點：每次放下都打一次 server（鎖死後 presolve 幾乎即時；只有失敗時分層診斷才會慢）。

## 3. 最終決策

採用方案 D。`POST /v1/placement/bm-demand/edit-layout` 收 `{request, result, moves}`，對每個被動到的 pool 做鎖定重解；
可行 → 回傳改寫後的 `BmDemandPlanResult`（`edited=True`、`manual_moves` 追加、stock / `bm_used_count` / 四份報表重算）；
不可行 → `ok=false` + solver 的 `solver_status` / `diagnostics`，原 result 不動。永遠 HTTP 200（與 `/plan` 的慣例一致）。
UI 的每一次放下、每一次「Move to…」都走這條路；Undo 回放前端歷史（每一筆都曾被接受過，不必再驗）。

## 4. 實作走讀

**`app/bm_demand.py:830` `_validate_group_layout`** — 鎖定的建構。重點在兩個「不要」：不要用 `pinned_to`（方案 B 的理由），
不要保留 `used_capacity`。後者比較隱晦：`gr.baremetals` 本來就是 `build_fleet` 的乾淨 fleet，但契約應該是「explicit VM 就是全部人口」，
不依賴上游給哪份清單；而且歸零之後 Step A（`get_eligible_baremetals`）不會把放不下的目標先濾掉，容量不足會以 C2 的
`failed_at="capacity"` 回報，而不是 `one_bm_per_vm` + `vms_with_no_eligible_bm` 這種對 UI 不友善的形狀。

**`app/bm_demand.py:811` `_accumulate_rules`** — rule 的來源是 `result.compiled[g].steps`，照 `app/rollout.py` 的 `acc_*` 邏輯逐 step 累積，
不是另抄一份 C6/C7。這保證未來編譯器若多發一種 rule（例如 C5），鎖定重解會自動一起查。

**flat check 與 sequential rollout 的關係（`_validate_group_layout` docstring、`test_zero_moves_revalidates_the_plan_as_is`）** —
原計畫是逐 step 解、pin 前面的結果；驗證卻是一次攤平。兩者等價嗎？bm_demand 只發 C6/C7，都與 step 無關（C7 是下界，不 grandfather）。
auto C3 在 step k 的 cap 是 `max(⌈N_k/|B|⌉, 該 bucket 的 pinned 數)`，pinned 數又被更早 step 的 cap 限住，歸納得每個 bucket 最終
≤ `⌈N_total/|B|⌉`，恰等於 flat cap。所以 rollout 接受過的擺放，零 moves 的 flat check 必過；零 moves 測試就是這個論證的回歸守門。

**`app/bm_demand.py:865` `edit_layout`** — 守門（fingerprint、pool 存在且成功、VM 在 pool 內、`to_bm` 在 pool 內、不重複）全部回
`INPUT_ERROR`，不丟給 solver。特別是 `to_bm` 不在 pool：solver 對未知 candidate 只會回 INFEASIBLE + `vms_with_no_eligible_bm`，
UI 會翻譯成「放不下」，誤導；所以要自己擋。寫回時以 **vm_id** 對應（flat request 是各 step 串接，順序無意義）；
`gr.sizing.rollout = gr.rollout` 這行是因為 JSON 來回後兩者不再是同一個物件，而 UI 兩邊都可能讀。

**`app/web_static/js/layout-edit.js:28` `explainRejection`** — D5 翻譯表以 `constraint_check.failed_at` 為準；`infeasible_*` 清單只是
計數下界（可能是空的），有才附 group_id。`UNKNOWN`（逾時）顯示「請再試」而不是當作通過。

## 5. 取捨與風險

> 2026-11-25 修訂：逐步驗證改為「放下先成立、整個 pool 一次驗、Done 才要求合法」，見 ADR-025。驗證器本身不變。

- 每次放下一次 solve。鎖死後幾乎即時；只有被拒時分層診斷最多 7×5s。可接受，因為失敗是少數路徑。
- 驗證的「盲點」只剩本來就不是硬規則的東西：advisories（`spread_below_target`、`label_not_in_auto_group`、`ag_spread_exceeds_equivalents`）
  維持 Run 當時的內容、不重算（banner 已標示）；Go scheduler 自己的政策（taint / affinity）本來就不在 solver 契約內；
  C5 / auto C4 / explicit C3 cap 編譯器沒發就不查——但「沒發」的規則計畫本身也沒強制，不是漏洞。
- 若未來編譯器開始發**與 step 相依**的 rule（例如 C3 explicit cap 隨 step 變），§4 的等價論證會破，屆時應改成「進入編輯模式先驗基線」。
- `sizing` 的 floor / probes 不再描述編輯後的擺放；UI 標示 edited、不重跑 sizing。再 Run 會整個覆蓋（UI 提示一次）。
- Open in Rollout 在 edited 時改送 explicit VM（每顆 `candidate_baremetals=[其機器]`），rollout 頁因此看到編輯後的擺放、也會用同樣方式重驗。

## 6. 你應該帶走的知識

1. **同一個「VM 在 BM b」有兩種語義**：`pinned_to` 是事實（既往不咎），單一 `candidate_baremetals` 是要求（不豁免）。要驗證就用後者。
2. **驗證器不該是第二份規則實作**。把狀態餵回產生它的模型做 feasibility check，diagnostics 自然就是錯誤訊息。
3. **攤平檢查 vs 逐步檢查**不是永遠等價，等價與否取決於 cap 是否 step 相依；把論證寫在 docstring、用零 moves 測試當守門。

## 7. 驗證方式

- `tests/test_bm_demand.py::TestLayoutEdit`：零 moves 必過（flat check 回歸）、同 AG 空機接受且矩陣 / layouts / legacy / stock 跟著變、
  跨 AG 第四顆 AAA 被 `anti_affinity` 擋、滿機被 `capacity` 擋、swap 原子、連續編輯累積、守門全部 INPUT_ERROR、失敗的 pool 不可編、
  獨佔 pool 同機被 `exclusive` 擋、sized 路徑 `sizing.rollout` 別名同步；`TestLayoutEditEndpoint`：endpoint 永遠 200。
- 手動：`make dev` → PM Demand → Run → Edit layout → 拖 chip 到空機 / 滿機 / 另一顆 chip，觀察編輯列訊息與矩陣、legacy export 的變化；
  Open in Rollout 應看到編輯後的擺放。
