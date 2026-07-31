---
name: contract-check
description: Checklist of integration anti-patterns between go-scheduler, the solver sidecar, and Inventory DB. Use when writing or reviewing contract assembly (internal/solverclient, internal/planning), candidate derivation, demand-book persistence, or metric computation — and before finalizing any change that touches what gets sent to or read from the solver.
---

# 契約整合 Anti-Patterns Checklist

這裡列的錯誤**編譯得過、測試會綠、上線後靜默說謊**。Solver 端的陷阱是
CP-SAT 建模;Go 端的陷阱是**契約漂移**與**假資料**。動到契約組裝、候選推導、
帳本持久化、指標計算前先過一遍;完成前用文末的快速表複查。

## 一致性(最危險的一類)

### 1. 規劃與執行各自演化 filter

```go
// BAD: 規劃期自己寫一套候選推導
func planningCandidates(bms []BM) []BM {
    return filter(bms, func(b BM) bool { return b.State == "ready" })  // 少了 os_not_ready
}
```

規劃比執行**窄**只是低估(安全);比執行**寬**就是承諾破裂——規劃說放得下、
執行放不下。而且新增一個只掛執行期的 filter,規劃從此永遠高估卻**沒有任何
測試會紅**。

**Fix**:兩個具名 profile(`planning` / `execution`)+ 明文差集清單;
新 filter 必須宣告歸屬。差集之外的任何差異都是 bug。

### 2. 單向 pool 過濾

```go
// BAD: 只鎖獨佔那一邊
if cluster.Pool != "" {
    bms = filterByPool(bms, cluster.Pool)
}
// 共用 cluster 沒過濾 → 吃得到獨佔池的機器
```

隔離是**雙向**的:`pool=""` 的共用需求也必須排除所有池機器。漏掉這半邊,
獨佔租戶的機器會被共用負載佔用,而規劃是按隔離算的——下期會建議再買一批
同樣的機器。

**Fix**:過濾條件永遠是精確相等 `bm.Pool == demand.Pool`,`""` 當成一個
正常的值,不要當萬用字元。或維持「bmg 成員同 pool」的不變式,讓隔離結構成立。

### 3. 沒落帳 config_fingerprint

每個 solver response 都帶指紋。不存的話,日後對帳看到誤差變大,分不清是
模型退步還是有人改了權重/升了引擎版本。

**Fix**:每次呼叫(規劃與執行)都把指紋寫進該次紀錄。

## 資料誠實

### 4. 名目加總冒充可落地容量

```go
// BAD: 「這格還有多少空間」
var free int
for _, bm := range cells[c] { free += bm.Total.CPU - bm.Used.CPU }
```

64 核散在 8 台各 8 核,裝不下任何一台需要 8 核的 node——名目說「還有 64 核」,
真相是 0。這是規劃系統最典型的說謊方式。

**Fix**:可落地量只有 solver 算(per-BM bin-pack `reference_vm_spec`),
讀 `BucketMonthCell.in_stock_slots` 或走 reconcile。名目量只能當輔助欄。

### 5. 分母為空時回 0% 或 100%

```go
// BAD
rate := float64(fulfilled) / float64(planned)   // planned == 0 → NaN 或被當 0
```

沒有計畫承諾就沒有兌現率。回 0% 會讓看板顯示「這個月全部沒做到」,回 100%
會讓人以為完美——兩個都是編出來的。

**Fix**:`*float64` 或 `sql.NullFloat64`,分母為 0 時是 nil;UI 顯示「—」。
同時把分子分母都吐出來,讓人看得到「17/20」。

### 6. 失敗月的 what-if 數字被聚合

規劃失敗的月份,solver 仍會吐出「如果能買的話要買幾台」——那是 what-if,
不是計畫。Solver 的 `totals` / `budget_view` 已排除,**Go 端二次聚合時
必須同樣排除**,否則採買預算會憑空多一截。

**Fix**:聚合前先 `if !report.Success { continue }`。

### 7. 把採購台數攤到 demand_id

多張需求單 joint solve 合買一台機器,「這台算誰的」沒有非任意的答案。
`DemandCoverage` 的 `in_stock/committed/new_buy` 是 **node 數不是機器數**。

**Fix**:node 級覆蓋照用(精確);帳務歸屬走 `pool` 座標(獨佔池整批屬於
該租戶,無爭議)。不要發明分攤公式。

## 持久化語意

### 8. 「取代全部」不是交易

```go
// BAD
db.Delete("demand_book", scope)
for _, e := range entries { db.Insert(e) }   // 中途失敗 = 半新半舊
```

帳本的三態語意讓半完成狀態**看起來像合法資料**(缺列被解讀成「未規劃」),
規劃會照著跑出錯誤答案而且不會報錯。

**Fix**:單一交易內完成清空 + 寫入 + 重複鍵檢查。前端預檢只是 UX,擋不住
併發匯入。

### 9. 三態被壓成兩態

刪列(未規劃)與留列全 0(確定不成長)是**不同語意**。用軟刪除、或把全 0 列
當成「空值可以清掉」,就把兩者混成一個。

**Fix**:刪除與清零是兩個不同的 API 操作,UI 也要分開。

### 10. demand_id 重發或可變

CSV 重匯時重發新 id,之前所有執行紀錄的 join 全斷,兌現率直接歸零且查不出
原因。

**Fix**:鍵值決定性生成;一旦發出不可變、不可重用。

### 11. committed 沒扣就進 in_stock

到貨機器進 `in_stock` 但 committed 池沒扣減 → 同一台機器被算兩次容量 →
規劃以為容量充足 → 少買。

**Fix**:畢業是**一個動作的兩面**,同一交易內完成。

### 12. 到貨後才回溯匹配 PO

同型多張單 + 分批交貨,靠「機型+數量+時間窗」猜是哪張單一定會錯,而且錯了
沒人發現。

**Fix**:連結在**提單那一刻**建立(記 plan_id + 格座標),PO 編號後補;
最好讓 PO 帶回我方 request id,自動 join。

## 呼叫 solver

### 13. Parse 狀態字串內文

```go
// BAD
if strings.Contains(resp.SolverStatus, "no candidate baremetals") { ... }
```

訊息內文是給人看的,會演化。

**Fix**:只 branch 前綴(`INPUT_ERROR:` / `INFEASIBLE` / `BLOCKED:` /
`OPTIMAL` / `FEASIBLE` / `UNKNOWN`)。

### 14. UNKNOWN 當 INFEASIBLE 處理

`UNKNOWN` = 逾時沒結論(可以加時間重試);`INFEASIBLE` = 證明放不下(要改
輸入)。混為一談會讓使用者在該重試時去改需求,或在該改需求時空等。

**Fix**:兩者分開處理,UNKNOWN 提示調高 `max_solve_time_seconds`。

### 15. 在 Go 端重造 solver 的模型定義

契約散在多個 struct、多處手刻 JSON,升版時漏改一處就靜默錯。

**Fix**:`internal/solverclient/` 是唯一封裝層,其他套件只用它的型別。
契約權威是 solver repo 的 `app/models.py`,有疑問去讀它或 `GET /openapi.json`。

## 快速複查表

| # | 檢查 | 違反時的症狀 |
|---|---|---|
| 1 | 兩個 profile + 明文差集 | 規劃高估,執行放不下,無測試會紅 |
| 2 | pool 雙向精確相等 | 獨佔池被共用負載吃掉,重複採購 |
| 3 | config 指紋每次落帳 | 誤差變大但歸因不明 |
| 4 | 可落地量來自 solver | 碎片下的容量假象 |
| 5 | 空分母 → null | 看板顯示編造的 0% / 100% |
| 6 | 失敗月不進聚合 | 預算憑空多一截 |
| 7 | 不攤採購到 demand_id | 帳務數字任意且無法對質 |
| 8 | 取代全部是單一交易 | 半新半舊帳本被當合法資料 |
| 9 | 刪列 ≠ 清零 | 「未規劃」與「不成長」混淆 |
| 10 | demand_id 決定性且不可變 | 兌現率歸零,原因難查 |
| 11 | 畢業同交易扣 committed | 容量算兩次,少買 |
| 12 | PO 連結提單時建立 | 分批到貨對不上 |
| 13 | 只 branch 狀態前綴 | 訊息一改就壞 |
| 14 | UNKNOWN ≠ INFEASIBLE | 使用者做錯的補救動作 |
| 15 | 契約單一封裝層 | 升版漏改處靜默錯誤 |
