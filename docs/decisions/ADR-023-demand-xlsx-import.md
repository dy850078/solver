# ADR-023: PM Demand 從 Excel 匯入／匯出——瀏覽器端手刻 .xlsx 讀取器，工作簿是 request JSON 的另一種拼法

- **日期**: 2026-11-19
- **作者**: Claude（與工程師共同決策）
- **相關 PR / commit**: branch `claude/demand-xlsx`
- **影響範圍**: `app/web_static/js/xlsx-read.js`（新）、`app/web_static/js/demand-xlsx.js`（新）、`demand.js`、`demand-form.js`（`toRequest` 拆出、`demandKey` 改用共用 `demandIdentity`）、`util.js`、`demand.html`、`styles.css`（預覽對話框樣式；`report.css` 是 Report 頁獨立的樣式表，保留自己那份）。後端無變更。

> 寫作對象：一位正在學習 CP-SAT 與排程系統設計的工程師。
> 寫作口吻：資深工程師帶 code review。全文繁體中文，程式碼與術語保留英文。

## 1. 背景與問題

PM 的需求一多，在 PM Demand 頁一列一列填很耗時；PM 本來就在 Excel 整理需求。需求是「頁面直接吃 `.xlsx`」，
而且 Excel 要能涵蓋 UI 能填的**一切**（specs、groups、exclusive、policy、topology、solver 設定），不是只有 demand 列；
匯入前要能預覽、有錯不能半套用。

兩個環境事實決定了做法：UI 慣例是零第三方 JS、零 CDN（`xlsx.js` 寫入器就是手刻的），後端沒有 `openpyxl` / `python-multipart`，
而開發即將移到內部網路、PyPI mirror 有什麼不確定。

## 2. 考慮過的方案

### 方案 A：後端 openpyxl + multipart 上傳 endpoint
- 優點：解析最穩、Python 好測。
- 缺點：兩個新相依（openpyxl、python-multipart）；內部 mirror 若沒有就裝不起來；這會是 repo 第一個 multipart endpoint。
- **排除**：為了一個 UI 便利功能引入部署風險不划算；而且解析本來就不需要 server——資料在使用者手上。

### 方案 B：引入 SheetJS（CDN 或 vendored）
- **排除**：違反 UI 零第三方 JS 的慣例；vendored 版本更新與授權都是負擔。

### 方案 C（採用）：瀏覽器端手刻讀取器，以 request JSON 為介面
- xlsx = zip + 幾個 XML。瀏覽器原生有 `DecompressionStream("deflate-raw")` 與 `DOMParser`，讀取器約 200 行、零相依，與 `xlsx.js` 寫入器成對。
- 匯入 / 匯出都以 **BmDemandPlanRequest JSON** 為介面，而不是 form state：匯入 = `sheetsToRequest()` → 既有 `loadTemplate()`；
  匯出 = `requestToSheets(buildRequest())`。模組不碰 DOM、不碰 form state，可以在 node 直接測 round-trip。
- 缺點：需要較新的瀏覽器（Chrome/Edge 103+、Firefox 113+、Safari 16.4+），不支援時對話框明講。

### 兩個 schema 層的取捨
- **`exclusive` 放哪**：原本想在 Demands 每列加 yes/no 欄；但它是 cluster 層級旗標，同 cluster 兩列 yes/no 會歧義。改放 Settings `exclusive_clusters`，與 JSON 一對一。
- **選填工作表語義**：**不存在** → 沿用表單現況；**存在但只有表頭** → 清空該區塊。沒有這條，「沒有 groups 的表單」Export → Import 就不是 round-trip。

## 3. 最終決策

採用方案 C。五張工作表（Demands 必填；VM specs、Machine models、Groups、Settings 選填），工作表名與欄名大小寫無關、可別名、順序無關，
表頭列自動偵測（PM 常在上面加標題列）。匯入走預覽對話框，任何錯誤都擋 Import（all-or-nothing）；Export 把目前表單寫成同一格式，
所以 Export 出來的檔就是範本，Settings 表還附每個 key 的說明欄。

## 4. 實作走讀

**`xlsx-read.js` 的 ZIP 解析**——以 central directory 的 method / sizes / offset 為準，local header 只用來算資料起點。
原因：Excel 與許多串流寫入器會設 data-descriptor flag，local header 的 size 欄位是 0；信任它就讀不到東西。
開頭 `D0 CF 11 E0` 是 OLE 容器（加密工作簿或舊版 .xls），直接給明確訊息而不是「not a zip」。

**`xlsx-read.js` 的 XML 解析**——全部以 `localName` 比對、不看 namespace 前綴，因為 Excel 的「Strict Open XML」存檔用的是 `purl.oclc.org` 的 namespace。
共享字串要串接 `<si>` 下所有 `<t>`（rich-text run 會拆成多段），但要跳過 `<rPh>`（日文 Excel 的注音），否則 cluster 名會多出假名。
`<c>` 缺 `r` 屬性時用游標遞推（有些寫入器省略）。

**`demand-xlsx.js` 的 `sheetsToRequest`**——先解析目錄表（specs / models），再解 groups、demands，因為後者的名稱要對前者驗證；
目錄在「匯入的表，否則 base request」裡找。labels / gpu 先自己 tokenize 再交給 `parseLabels` / `parseGpu`——這兩個 util 會**靜默丟掉**壞 token，
匯入不能靜默。label 的 charset 用 `\p{L}\p{N}_.-` 加 `u` flag，對齊 Python `\w` 的 Unicode 語義（JS 的 `\w` 只有 ASCII）。
重複 demand 用 `util.demandIdentity`（表單的 `demandKey` 也改用它），與 `app/bm_demand.py` 的 dup 規則同一份。

**`demand-form.js` 的 `toRequest`**——從 `buildRequest` 拆出純映射、不 throw。匯入的 base 要用它：表單可能正處於不完整狀態（例如剛 New），
但它仍然是「沒給的工作表沿用什麼」的來源。

## 5. 取捨與風險

- 讀取器刻意不支援：ZIP64、deflate64、加密工作簿、UTF-16 XML、日期序號轉換（schema 沒有日期欄）。遇到就是明確錯誤。
- 瀏覽器版本門檻：`DecompressionStream("deflate-raw")`。內部環境若是舊版瀏覽器，退路是 `.json` 範本。
- 表頭自動偵測用「命中 ≥ 2 個已知欄名」；單欄的工作表（理論上）偵測不到，目前每張表至少兩個必填欄，不成問題。
- Settings 的空白儲存格對數字／布林代表「沿用」，對文字／清單代表「清空」——這個不對稱是為了 round-trip（表單的 `exclusive_clusters: []` 要能匯出再匯回）。
- 未知欄位、未知 step、沒人用的 `auto_group_labels` 只警告不擋；真正的契約錯誤（名稱不存在、重複、格式）才擋。

## 6. 你應該帶走的知識

1. **檔案格式往往比想像中薄**：xlsx 只是 zip + XML；平台原生 API（`DecompressionStream`、`DOMParser`）就夠，先看有沒有函式庫以外的路。
2. **以契約（request JSON）為介面，而不是 UI 狀態**：模組與 DOM 解耦，round-trip 可以在 node 測，而且「Excel 能表達的」自動等於「API 能接受的」。
3. **匯入要 all-or-nothing、靜默修正是 bug**：util 裡「容錯」的 parser 在匯入情境要先用嚴格的 tokenizer 包一層。

## 7. 驗證方式

- Node（scratchpad，`node --test` 風格）：四個 `examples/bm_demand/*.json` 各做 `requestToSheets → sheetsToRequest(base={})`，與原 JSON 等價
  （缺鍵與 pydantic 預設視為相同）；錯誤案例：缺 Demands、未知 vm_spec、重複列、壞 label、非整數 count、表頭而已的 Groups → `[]`、缺表 → 沿用 base、未知 setting。
- Playwright（scratchpad）：New → Export → Import 同檔 → JSON 編輯區完全相同；手刻「像 Excel 存的」fixture（deflate、rich-text + `<rPh>`、inlineStr、公式快取值、布林、
  缺 `r`、絕對路徑 rel、標題列）與 Strict OOXML 版都讀得出來；含錯 fixture 標紅且 Import 停用；只有 Demands 表時其餘區塊沿用；非 zip 檔明確錯誤。
- 手動：`make dev` → PM Demand → Export Excel → 在 Excel 填 → Import Excel… → 預覽 → Import → Run。
