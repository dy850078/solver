# ADR-004: E2.5 機隊事件簿 `release` —— 排定除役整台還池，事件前凍結不接新節點

- **日期**: 2026-07-29
- **作者**: Claude (mentor mode)
- **相關 PR / commit**: branch `claude/provision-e2e-automation-vision-yyzen0`
- **影響範圍**: `app/models.py`, `app/capacity_planner.py`,
  `tests/test_capacity_planner.py`, `examples/capacity/plan_fleet_release.json`

> 寫作對象:一位正在學習 CP-SAT 與排程系統設計的工程師。

## 1. 背景與問題

多期規劃的 roll-forward 是**單向消耗**的:placement 累加 `used_capacity`、
slot cap 遞減、買機落地。它表達不了現實中常見的反向事件——「三月把 bm-7、bm-8
從舊 cluster 除役,容量還池供四月使用」。今日的 workaround 是斷成兩次 solve、
手動改 in-stock,報表從中斷開。決議 #40 早已定案設計(只做 `release`、整台
釋放、事前擋新節點),roadmap E2.5 輪到它落地。

## 2. 考慮過的方案

**A. 事件簿(採用)** —— 與 demand book 平行的 `fleet_events` 清單,規劃器在
月迴圈裡套用:事件月前凍結、事件月 `used_capacity` 歸零。
優點:狀態機仍是單一 roll-forward,事件只是每月迴圈開頭的一次狀態修正;
缺點:只支援整台粒度。

**B. 把除役建成 CP-SAT 決策變數**(讓 solver 自己選「何時清空哪台」)——
優點:理論上可以聯合最佳化除役時點;缺點:除役時點是**外部排程事實**
(舊 cluster 的搬遷計畫),不是 solver 的自由度,建成變數是把輸入偽裝成決策,
模型變大還會「最佳化」出與現實不符的除役月。被排除:語意錯誤,不只是成本問題。

**C. 部分釋放**(per-resource 釋放量)—— 表達力更強,但輸入從「機器清單」
變成「每台每資源的釋放帳」,而實際操作就是整台下架。表達力換輸入複雜度,
不划算(#40 已裁定)。留待真實需求出現再擴充。

**實作期新決策——凍結機與健康儀表**:設計文件只說「仍計入報表快照」。凍結機
不接新節點,它的空位**不是可用餘裕**;若照舊計入 `nominal_available` /
`remaining_node_slots`,儀表就高估容量(D1 型說謊)。最終:儀表排除凍結機
剩餘容量,cells 快照仍完整呈現(快照是狀態帳、儀表是可用度,座標不同)。
替代方案「照舊計入」實作最省,輸在違反「規劃不高估」的核心原則。

## 3. 最終決策

新增 `CapacityPlanRequest.fleet_events`(`FleetEvent{period, action="release",
bm_ids, fab}`)。規劃器每月迴圈開頭:release 月已到且未套用 → 該機
`used_capacity = Resources()` 還池;release 月未到 → 該機凍結(進
`ProcurementRequest.frozen_bm_ids`,退出候選、儀表排除)。報表以
`released_bms` / `frozen_bms` 標注。事件引用錯誤在 Pydantic 驗證期即拒絕。

## 4. 實作走讀

- `app/capacity_planner.py:787-822` —— 每 fab 建 `release_month: {bm_id →
  period}`,月迴圈開頭套用「首個 ≥ 事件月的規劃月」一次性歸零(`released_applied`
  防重複——之後月份的 placement 會重新累加 used,再歸零就是抹帳)。釋放月落在
  兩個規劃月之間時順延到下一個規劃月:中間沒有任何 solve,兩個狀態不可區分,
  這是無損的簡化。
- `app/capacity_planner.py:337` —— 凍結過濾放在 `candidates = (explicit or
  in_stock) + virtual` **之後**,所以呼叫端顯式給的 candidate list 也到不了
  凍結機。這是 Step A(eligibility)層級的過濾:candidate 不存在,CP-SAT 模型
  裡連 `assign[vm, bm]` 變數都不會生成——比建變數再加 `= 0` 約束便宜,也讓
  INFEASIBLE 診斷不會誤指向凍結機。
- `app/capacity_planner.py:674` —— 儀表迴圈 `if bm.id in frozen: continue`:
  數學上是把 `Σ remaining` 的求和域縮小到「事實上可接新節點的機器」,讓
  nominal / slots / stranded / balance_after 回答的仍是「還能塞多少」。
- `app/models.py:878` —— 引用完整性(dangling `bm_id`、一機多事件、具名 fab
  不符)全部在請求驗證期擋下,不進 solver。事件簿是 plan of record,打錯字
  默默不釋放比報錯貴得多。

## 5. 取捨與風險

- **凍結是無條件的**:即使該月不凍結就 INFEASIBLE、凍結機其實有空位,計畫也
  不會用它——這是刻意的(「二月建上去、三月清掉」是自相矛盾的計畫),但失敗
  訊息若讓人誤以為容量不存在會難查;`_no_candidates_reason` 已補明確措辭。
- 釋放月不在規劃月集合時順延套用:若未來報表要顯示「事件月當月」的精確時點
  (而非下一個規劃月),需要把 `released_bms` 標注與套用月脫鉤。
- `retire` / `add` 尚未支援;需求出現時在同一 `FleetEvent.action` Literal 上
  擴充,不要另開清單。

## 6. 你應該帶走的知識

- **外部事實不要建成決策變數**:除役時點是輸入不是自由度;把輸入偽裝成變數,
  solver 會「最佳化」出與現實矛盾的答案。
- **最便宜的約束是不生成變數**:在 Step A 的候選推導層過濾,比在模型裡加
  `assign = 0` 約束省變數、省傳播,診斷也更乾淨。
- **狀態帳與可用度是兩個座標**:快照(cells)如實記錄機器存在與負載;儀表
  (nominal/slots)只計「真的能用」的部分。混用兩者就是規劃高估的來源。

## 7. 驗證方式

- `tests/test_capacity_planner.py::TestFleetEvents` —— 12 條:釋放還池、
  事前凍結、儀表排除、跨規劃月順延、單發端點 `frozen_bm_ids`、全凍結錯誤
  訊息、五類驗證拒絕。
- 親手跑:`make cli` 不適用(plan 端點),用
  `curl -X POST :50051/v1/capacity/plan -d @examples/capacity/plan_fleet_release.json`
  —— 預期:2026-01 `frozen_bms=[bm-legacy-1]`、buy 0;2026-02
  `released_bms=[bm-legacy-1]`、buy 0(不釋放的話該月得買 1 台)。
