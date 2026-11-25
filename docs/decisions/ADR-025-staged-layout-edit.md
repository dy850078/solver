# ADR-025: Edit layout 改成「暫存＋整體驗證」——放下先成立，整個 pool 一次驗，Done 才要求合法

- **日期**: 2026-11-25
- **作者**: Claude（與工程師共同決策）
- **相關 PR / commit**: branch `claude/layout-edit-staged`
- **影響範圍**: `app/web_static/js/layout-edit.js`（狀態機重寫）、`demand.js`、`rackdiagram.js`（超載視覺）、`demand.html`、`demand.css`、`styles.css`、`tests/test_bm_demand.py`（新增批次測試）。**後端 `edit_layout` 一行不改。**

> 寫作對象：一位正在學習 CP-SAT 與排程系統設計的工程師。
> 寫作口吻：資深工程師帶 code review。全文繁體中文，程式碼與術語保留英文。

## 1. 背景與問題

ADR-022 的 Edit layout 每一次放下都把整個 pool 鎖定重解，不合法就彈回。這在實務上卡死了最常見的操作：
「先把 A 搬進 X、再把 B 從 X 搬出去」——第一步暫時超載就被拒，除非剛好有空機當暫態。
工程師的觀察正確：問題不在規則，而在**每一步都要求合法**。要改的是驗證的**時機**與呈現，不是驗證器。

硬約束不變：Done 時的擺放一定合法（「違規不能強制接受」）。

## 2. 考慮過的方案

### 方案 A：每步驗證＋「暫存區」格子
- VM 可以先拖進一個沒有機器的格子再拖出來。
- **排除**：報表、矩陣、blueprint 要處理「沒有機器的 VM」；三角換位仍要多拖兩次；暫存區本身就是「允許不合法」的變形，只是把不合法藏在一個格子裡。

### 方案 B：放下不驗，按 Validate 才驗
- 少打 server。
- **排除**：拖了五步才知道第二步錯了。鎖定重解本來就是毫秒級（C1 鎖死後 presolve 直接判定），省這幾次呼叫不值得失去即時回饋。

### 方案 C：後端支援同一顆 VM 多條 move 依序套用
- **排除**：沒必要——server 只看終局，中間狀態對判定沒有意義；客戶端摺疊成淨位移更簡單，`manual_moves` 也不會堆「搬去又搬回」的垃圾。

### 方案 D（採用）：客戶端暫存淨位移，每次放下驗整批
- `pending: Map<vm_id, 最終機器>` 相對進入時的基線 `baseline`；搬回原機就刪 key。
- 每次放下送 `{request, result: baseline, moves: pending 全部}`。通過 → 回傳即 `lastValid`（報表與 `manual_moves` 由 server 算），成為頁面結果；
  不通過 → rack 顯示暫存擺放、報表停在 `lastValid` 並標 pending、編輯列說明原因。
- Done 只在 valid 可按，且寫回的是 `lastValid`，永遠不是暫存 Map。

## 3. 最終決策

採用方案 D。後端 `edit_layout` 本來就是「對 `result` 套一批 moves 後整體重解」（swap 已是兩條 move 一次送），所以這是純前端改動：
把「每次送 1–2 條、相對目前結果」改成「每次送全部 pending、相對基線」。能離開編輯模式的擺放集合與以前**完全相同**——同一個驗證器、同一個終局。

## 4. 實作走讀

**`layout-edit.js` 的三份狀態**——`baseline`（進入時的合法結果，從不被改）、`lastValid`（最近一次 server 接受的結果，初值＝baseline）、`pending`（淨位移）。
`viewResult()` 把 `lastValid` 深複製後用 `pending` 覆寫選定 pool 的 `new_assignments`，rack 就畫這份；`state.result` 只在 `ctx.setResult(lastValid)` 時改變。
這個切分讓「畫面上看到的」與「頁面相信的」可以不同，而且不同的那段永遠只在記憶體裡。

**`validate()` 的序號**——每次放下 `seq++`；回應回來時 `seq` 不符就丟掉。沒有這個，快速連拖兩次時第一次的回應可能晚到，把第二次的 pending 標成 valid。
pending 為空時不打 server：`lastValid = baseline`，因為零淨位移的擺放就是基線本身（基線是合法的，ADR-022 R1）。

**`exit({ force })`**——Done 走 `force=false`，不合法就 return false（按鈕本來就 disabled，這是第二道）；Run 前走 `force=true`，因為結果馬上會被新的解取代，但寫回的仍是 `lastValid`。

**超載視覺（`rackdiagram.js` `capacityRows.over`、`.bm--overfull`）**——只是把既有利用率顯示延伸到 >100%；任何判定都不讀它。
已解出的結果永遠不會出現超載，所以這個樣式在非編輯模式下不會亮。

**`manual_moves` 的語義變化**——以前是逐步紀錄，現在是相對進入編輯那一刻的淨位移。可追溯性仍成立（每顆 VM 從哪到哪），只是不再記錄中途的反覆。

## 5. 取捨與風險

- 與 ADR-022 相比，被接受的最終擺放集合不變；多出來的只有「畫面上允許暫時不合法」。這點靠四個入口把關：Done（gated）、Discard（回基線）、Run（force，寫回 lastValid）、報表／下載（只讀 `state.result`）。
- 驗證請求失敗（網路）→ status `error`、Done disabled、提供 Retry；沒有「預設通過」。
- `structuredClone` 整份結果供 rack 畫圖，pool 很大時每次放下多幾毫秒；可接受。
- 若未來 `edit_layout` 開始對「同一 VM 多次搬移」有語義（例如記錄路徑），客戶端摺疊就要改；目前 server 不看中間狀態，摺疊是安全的。

## 6. 你應該帶走的知識

1. **把「驗證什麼」和「何時驗證」分開**：驗證器不動，只改送驗的時機與失敗時的呈現，就能解掉 UX 問題，而且安全性論證很短——終局集合不變。
2. **非同步驗證一定要有序號**：任何「放下就送、回來就套」的互動，沒有序號就會被晚到的回應打亂。
3. **讓不可信的狀態沒有出口**：暫存擺放只存在 `pending` 與畫面，所有會持久化或輸出的路徑都只讀 `lastValid`；列舉每個出口比「小心一點」可靠。

## 7. 驗證方式

- `tests/test_bm_demand.py::TestLayoutEdit::test_batch_of_moves_valid_only_as_a_whole`：三台滿機三角輪換——每條單獨送都因 capacity 被拒，整批送通過；整批裡混一條壞的 → 整批被拒且輸入不變（原子）。
- 既有 11 條 `TestLayoutEdit` 不動（後端行為不變）。
- Playwright（scratchpad，12 台固定 fleet）：拖到滿機 → 留在原地、`✗ Not valid`、紅框 + 120%、Done disabled、三張報表卡 pending；把一顆搬出去 → `✓ valid`、報表更新；Undo 兩次回基線；三角輪換前兩步 invalid 第三步 valid → Done → `manual_moves` 三筆；Discard 從不合法回基線；搬出又搬回 → 0 pending、不打 server。
