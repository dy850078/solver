# ADR-024: Blueprint 多兩個 meta 鍵 `_topology` / `_binding`——用 `_` 前綴把「機器以外的資訊」放進同一份文件

- **日期**: 2026-11-24
- **作者**: Claude（與工程師共同決策）
- **相關 PR / commit**: branch `claude/blueprint-meta`
- **影響範圍**: `app/placement_report.py`（`BmLayout.topology`、`topology_numbers`、`legacy_template`、`LEGACY_META_KEYS`）、`app/bm_demand.py`（型別）、`app/web_static/js/demand.js` / `demand.html` / `demand.css`（勾選器）、tests、README、CLAUDE.md。solver 邏輯不動。

> 寫作對象：一位正在學習 CP-SAT 與排程系統設計的工程師。
> 寫作口吻：資深工程師帶 code review。全文繁體中文，程式碼與術語保留英文。

## 1. 背景與問題

Legacy pipeline export（內部稱 **blueprint**）每個 pool 一份文件，只有 `BM1…BMn → [VM rows]`（ADR-020 D10）。下游現在要兩樣東西：
每台機器的拓撲（放哪個 dc），以及一個日後由 inventory-generator 填入真實 hostname 的位置。ADR-020 的風險段早就寫了
「格式確定後可能要加欄位，屆時只動 `placement_report.py`」——這次就是那一步。

限制：`BM1…BMn` 鍵本身不能變（下游照它讀機器）；報表是 `(assignments, vms, bms)` 的純函數，edit-layout 之後要自動一致。

## 2. 考慮過的方案

### 方案 A：另開結果欄位 `legacy_topology` / `legacy_binding`
- 優點：`legacy_template` 一個位元組都不變。
- **排除**：下游要的是**一份** blueprint 內含這些資訊；分開放，pipeline 得多做一次對齊，而且 UI 的 Copy / Download 要拼兩份。

### 方案 B：拓撲塞進每一列 VM row
- **排除**：拓撲是機器的屬性不是 VM 的；一台三顆 VM 就重複三次，而且 `_binding` 根本沒有對應的 VM 列。

### 方案 C（採用）：同一份文件、`_` 前綴的 meta 鍵
- `_topology: {BMn: {...}}`、`_binding: {BMn: ""}`，鍵集合與 BM 鍵完全相同（同一個 `_number_layouts` 來源）。
- 讀者的契約只多一條：**以 `_` 開頭的鍵不是機器**。這與請求端 `_name` / `_description` 的慣例一致。

### 三個次要取捨
- **值轉數字、鍵用簡寫**（`dc` 而非 `datacenter`）：下游要的形狀。數字取字串尾端的數字（`dc-1` → 1）；沒有尾數的字串**原樣保留**而不是變 0——`placement_report` 是純函數，
  也可能餵真實 inventory 的拓撲，靜默變 0 比留字串危險。空維度略過。
- **維度在 UI 端選、不在請求端**：API 永遠六維全出，UI 勾選器決定複製／下載時留哪些（預設只留 `dc`）。這跟既有「欄位勾選只影響匯出文件」同一套，改勾不用重跑；
  請求端加參數會讓 `BmDemandPlanRequest` 多一個只為輸出格式服務的欄位。
- **`_binding` 用空字串**而不是 `null` 或機器 id：「有鍵、值空」就是待填的訊號；機器 id 已在 `legacy_bm_map`，重複放會讓人誤以為那是 hostname。

## 3. 最終決策

採用方案 C。`legacy_template()` 在每份 pool 文件的 `BM1…BMn` 之後追加 `_topology` 與 `_binding`；`BmLayout` 多帶完整 `topology`（原本只有 `ag` / `rack`）；
`topology_numbers()` 負責數字化。UI 的勾選器拆成 Columns / Blueprint keys / Topology dims 三組，`_topology` 未勾時維度整組停用。

## 4. 實作走讀

**`app/placement_report.py` `topology_numbers`** — `re.search(r"(\d+)$")` 取尾數；回傳型別是 `dict[str, int | str]`，`str` 就是「沒尾數、原樣保留」那條路，型別本身把這個決定講明白。

**`legacy_template`** — meta 鍵用同一個 `numbered` 列表產生，所以不可能跟 BM 鍵對不上；放在 BM 鍵之後，照順序讀的舊讀者仍先看到機器。
回傳型別從 `dict[str, dict[str, list[...]]]` 改成 `dict[str, dict[str, Any]]`——型別標註本來就跟實際內容（labels 列）不符，這次順手修正。

**`demand.js` `legacyDocFor`** — 原本 `rows.map(...)` 假設每個值都是陣列，meta 鍵一進來就會炸；改成逐鍵分流：`_topology` 依勾選過濾維度、`_binding` 原樣、其餘走列過濾。
`renderLegacy` 的「n BM」只數非 `_` 鍵。localStorage 鍵升為 `-v2`，因為舊存檔缺新 id 會把它們全部當成未勾。

## 5. 取捨與風險

- 下游若用「文件的所有鍵都是機器」的寫法讀 blueprint，會把 `_topology` 當成一台機器——這是契約變更，要通知 pipeline 端改成略過 `_` 鍵（README 已寫明）。
- 預設只匯出 `dc`：PM 若需要 rack 等維度要自己勾，但 Result JSON 的 `legacy_template` 永遠六維齊全，不會丟資料。
- 「blueprint」與「legacy template」目前是同一份文件的兩個稱呼；JSON 欄位名 `legacy_template` 不改，等內部兩者真的合併再處理命名。

## 6. 你應該帶走的知識

1. **加欄位時先想讀者的迭代方式**：一個 `for key in doc` 的讀者會把新鍵當成舊語義的成員；用命名約定（`_` 前綴）把「不是同類」講在資料本身上。
2. **純函數的輸入域比你眼前的呼叫者大**：數字化時「沒尾數就變 0」對 fleet 機器無害，對真實拓撲是靜默錯誤——選擇保留原值並讓型別說出來。
3. **輸出形狀的選擇放在最靠近輸出的那一層**（UI 勾選），不要為了輸出格式往請求契約加欄位。

## 7. 驗證方式

- `tests/test_placement_report.py::TestBlueprintMetaKeys`：尾數／原樣／空維度；`BmLayout.topology` 完整；meta 鍵集合＝BM 鍵集合、值對應正確機器；閒置機器沒有 meta 條目。
- `tests/test_bm_demand.py::TestPlanEndToEnd::test_blueprint_meta_keys_describe_the_planned_machines`：plan 結果每個 pool 六維皆為 int、對回 fleet 機器；
  `TestLayoutEdit::test_move_into_same_ag_is_accepted_and_reports_follow`：搬移後新 BMn 的 `_topology` 是目標機器的。
- Playwright（scratchpad）：Run → Blueprint 卡 JSON 預設 `_topology` 只有 `dc`、`_binding` 全空 → 勾 `rack` 出現、取消 `_binding` 消失 → BM 數不含 meta 鍵 → Edit layout 後同步。
