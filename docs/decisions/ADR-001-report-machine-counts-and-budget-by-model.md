# ADR-001: 報表改以「台數」呈現機器消耗，budget 按機型拆分

- **日期**: 2026-07-08
- **作者**: Claude (claude-code)
- **相關 PR / commit**: branch `claude/in-stock-csv-import`
- **影響範圍**: `app/models.py`、`app/capacity_planner.py`、`app/web_static/js/report.js`、`tests/test_capacity_planner.py`

> 寫作對象:一位正在學習 CP-SAT 與排程系統設計的工程師。

## 1. 背景與問題

使用者實測報表後提出兩個缺口:

1. **cell drill-down 的「CPU utilization」看不出想看的東西**。使用者的心智模型
   是台數:「這個月動用了幾台現有機器、幾台已購機、買了幾台新機、長了幾個節點」。
   利用率是資源比例,回答的是「池子多滿」,不是「動了幾台」——而後者才是
   月度增量規劃的語言。且 `in_stock_bm_used` 只存在於單期
   `ProcurementResult`,組多期 `PeriodFabReport` 時被丟掉,UI 完全沒有這個數字。
2. **多個 buyable 機型時,budget view 只有台數沒有機型**。`BudgetRow` 的鍵是
   `(fab, bucket, network, period)`,兩種機型各買一台會被壓成同一列 `bm_count=2`
   ——財務拿到「買 2 台」卻不知道買什麼,無法編預算。(月 detail 的
   Procurement chips 一直有 by-type 資訊,但 budget view 才是財務的主視圖。)

## 2. 考慮過的方案

**缺口 1(in-stock 台數)**:

- **方案 A(採用): 把 `in_stock_bm_used` 接進 `PeriodFabReport` + 每 cell 一個
  `in_stock_bm_used`,UI 拔掉 utilization 欄改放台數欄。**
  優點:單一語言(台數)貫穿四個指標;per-cell 歸因跟既有
  `_cell_attribution` 同一機制,增量最小。缺點:失去資源層級的視角(見取捨)。
- **方案 B: 保留 utilization 欄,旁邊加台數欄。**
  優點:資訊不減。缺點:cell 表變寬、兩種語言並存增加讀表負擔;使用者明說
  「利用率可以先拔掉」——保留它是在違背使用者的優先序。被排除。

**缺口 2(budget 按機型)**:

- **方案 A(採用): budget Counter 的鍵加上 `type_id`,`BudgetRow` 增欄。**
  型別來源是新增的 `ProcurementResult.bought_type_of: dict[bm_id, type_id]`。
  優點:一次到位,粒度 = 財務決策粒度。缺點:同 cell 多機型時列數變多(可接受,
  這正是要的資訊)。
- **方案 B: 從 bought BM 的合成 id(`buy-{type}-{bucket}|{network}-{k}`)parse
  回機型。** 優點:不動 model。缺點:type_id 本身可含 `-`,parse 脆弱;
  把 id 當 API 是反模式。被排除——多一個顯式 dict 欄位便宜得多。

## 3. 最終決策

報表的機器消耗一律以**台數**呈現:`node_adds / bm_bought / committed_used /
in_stock_bm_used` 四個計數貫穿 stat tiles、cell drill-down、list view 與
metric pivots;CPU utilization 欄自 cell 表移除。budget view 的鍵擴為
`(fab, bucket, network, period, type_id)`,一列 = 一個機型的採買量。

## 4. 實作走讀

- `app/capacity_planner.py:_cell_attribution` — 新增第四個 Counter:
  `touched_stock` 先用 set 對 assignment 的 baremetal_id 去重(**一台機器裝五個
  新節點只算一台**——這是台數語意的關鍵,直接數 assignment 會變成節點數),
  再按 `(bucket, network)` cell 分桶。放在 roll-forward **之前**呼叫,因為
  roll-forward 會把買到的機器改名(`acq-` 前綴),之後 id 就對不上了。
- `app/capacity_planner.py:_solve_once` 尾端 — `bought_type_of` 由
  `p.buyable_type_of`(建虛擬 BM 時就有的 id→type 映射)直接投影,零成本。
- `app/capacity_planner.py:solve_capacity_horizon` — budget 累加改為
  `budget[(fab, bucket, network, period, type_id)] += 1`。注意 fallback
  `res.bought_type_of.get(bm.id, "")`:理論上不會缺,但報表層寧可出空字串
  也不要 KeyError 炸掉整份報表。
- `app/models.py` — `BudgetRow.type_id: str = ""` 用預設值保持向後相容:
  舊 JSON(無此欄)仍能解析,Go 端消費者不升級也不會壞。

## 5. 取捨與風險

- 拔掉 utilization 後,「池子快滿了」的訊號只剩 Health gauges(nominal /
  slots / stranded)與 balance chips。若未來發現使用者在 cell 層級仍需要
  資源視角,訊號會是「他們開始手動比對 gauges 和 cells」——屆時再以 tooltip
  或次要欄位補回,而不是現在保留雙語言。
- `in_stock_bm_used` 的跨月語意:前月買的機器 roll-forward 後就是 in-stock,
  後月碰到它會計入。這是刻意的(「動用既有機器」的定義),但讀者可能誤解為
  「動用期初就存在的機器」——欄位 docstring 有明寫。
- budget 列數 = cell 數 × 機型數,20 fab × 多機型時列會多;bar strip 仍按月
  聚合所以視覺不受影響。

## 6. 你應該帶走的知識

1. **報表指標要用讀者的決策語言**:財務決策單位是「哪個機型買幾台」,容量
   決策單位是「動了幾台機器」——資源比例(utilization)對這兩者都是間接證據。
2. **去重層級決定語意**:同一份 assignments,按 VM 數 = node adds、按 distinct
   BM id = machines touched。彙總前先想清楚「一單位是什麼」。
3. **合成 id 不是 API**:需要從結果反查屬性時,寧可多帶一個顯式映射欄位,
   不要 parse id 字串。

## 7. 驗證方式

- `tests/test_capacity_planner.py::TestCapacityHorizon::test_in_stock_bm_used_reported`
  (2 台在庫、需求只碰 1 台 → period/cell/totals 都是 1)
- `tests/test_capacity_planner.py::TestCapacityHorizon::test_budget_view_splits_by_model`
  (small×2 + big×1 → budget 兩列,各帶 type_id)
- 手動:`make cli INPUT=examples/capacity/plan_two_fabs.json` 看 budget_view
  的 `type_id`;UI 跑任一 capacity 範例,detail 的 Cells 表應為四個台數欄、
  無 utilization。
