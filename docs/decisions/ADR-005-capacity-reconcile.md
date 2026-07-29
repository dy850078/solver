# ADR-005: S3 `/v1/capacity/reconcile` —— plan vs actual 對帳純函式與漂移四分類

- **日期**: 2026-07-29
- **作者**: Claude (mentor mode)
- **相關 PR / commit**: branch `claude/provision-e2e-automation-vision-yyzen0`
- **影響範圍**: `app/reconcile.py`(新), `app/models.py`, `app/capacity_planner.py`,
  `app/server.py`, `tests/test_reconcile.py`, `examples/capacity/reconcile_basic.json`

> 寫作對象:一位正在學習 CP-SAT 與排程系統設計的工程師。

## 1. 背景與問題

規劃迴路缺最後一塊:每月 canonical run 的預測與真實世界的落差**從未被量測**,
計畫品質好壞只有體感。設計主題 2 已定骨架——Go 存檔上期 plan(G4),隨時把
「plan + 此刻快照 + 執行帳」丟給 solver 算漂移報告(四指標 + 四分類)。solver
端要補的就是這個純函式。

## 2. 考慮過的方案

**A. solver 端純函式對帳(採用)** —— 對帳進 solver 的**唯一硬理由**是
「可落地可用量」重算:與規劃同一把尺(per-BM bin-pack `reference_vm_spec`),
名目加總在碎片下會說謊(決議 #5 同理)。其餘皆 diff,順便住在同一個模組。

**B. Go 端 diff** —— Go 只能加總名目資源,64 顆散在 8 台的空核裝不下任何一台
8c VM,名目誤差=0 的假象正是要抓的漂移。被排除:退回假指標。

**C. UI 端計算** —— 違反 canonical JSON 優先(#24),且 UI 拿不到 bin-pack。

**實作期定案的 v1 語意**(設計稿留白處):
- **單月對帳**:目標月 = `as_of` 所在月。多月一次對帳需定義「跨月累計誤差」
  語意,v1 不需要——月中跑=進度、月底跑=正式成績,週節奏=每週各跑一次。
- **分母為空 → 指標 = null**:沒有計畫承諾就沒有兌現率,回 0% 或 100% 都是
  假話。headline 同時吐分子分母(`fulfilled_vms/planned_vms`),UI 能顯示
  「17/20」而非只有 85%。
- **無 demand_id 的計畫行 → `unjoinable_planned_vms`**:join 不到就不能計入
  兌現率(冒充 miss 會冤枉計畫),但必須回報——它是帳本衛生訊號。
- **供給實到台數由 Go 落帳**(`machine_adds`):庫存 count diff 不需 solver,
  硬要從快照反推需要 plan 的機器 id 基線,契約反而變重。

## 3. 最終決策

新增 `app/reconcile.py::reconcile`,POST `/v1/capacity/reconcile`。輸入 =
`ReconcilePlan`(Go 存檔的 `CapacityReport` + 當時帳本)+ `ActualSnapshot`
(`as_of`/`in_stock`/`executions`/`machine_adds`)。輸出 = 四頭條
(`ReconcileHeadline`)+ 每格 predicted vs actual(`ReconcileCell`,槽數為主、
名目為輔)+ 規則式漂移(`DriftDetail`,比照 `ShortfallDetail` 可多筆)。
為了讓「預測期末可落地量」有出處,planner 的 `BucketMonthCell` 新增
`in_stock_slots`(凍結機計 0,與 ADR-004 的儀表立場一致)。

## 4. 實作走讀

- `app/capacity_planner.py:1047` —— 規劃側每格槽數:
  `Σ_bm min_f ⌊available_f / spec_f⌋`(f 跑 RESOURCE_FIELDS)。這是 per-BM
  的貪婪裝箱下界,不解 CP-SAT——對帳要的是「同一把尺兩邊量」,尺本身夠準即可,
  真正的可行性仍由 plan run 的完整模型回答。屬 Step D(報表萃取)階段。
- `app/reconcile.py:97` —— `_diff_cells`:actual 快照按
  `(fab_dim, spread_dim, network, pool)` 聚格重算槽數,與預測格取聯集。
  `plan_has_slots`(`:143`)決定「預測缺格」讀作誠實的 0 還是「沒量測」的
  null——舊 plan(無 `reference_vm_spec`)缺格若補 0 會毒化 forecast_error。
- `app/reconcile.py:175` —— 兌現率 `Σ_d min(planned_d, executed_ok_d) /
  Σ_d planned_d`:per-demand 取 min 再加總,超額執行不能沖抵別單的缺口
  (兩單各 4 台、一單做 8 一單做 0,是 50% 不是 100%)。
- `app/reconcile.py:248` —— 歸因防重複:供給不符的格子由 supply row 一筆帶過
  (容量差寫進 message),**只有無法用供給解釋的容量位移**才標 fleet;
  `:323` placement 只在 fab 總量差 ≤ 5%(`PLACEMENT_FAB_TOLERANCE`)時歸因
  ——fab 總量都沒到位時,格子層的落點差是需求/供給故事,怪 placement 是噪音。

## 5. 取捨與風險

- **單月視角**:跨月漂移(1 月缺的 2 月補上)v1 看不到;E4 後若月結報告常
  出現「上月 under、本月 over」成對,再加跨月視角。
- **placement 的 5% 容差是拍的**:太緊會把正常抖動標成漂移,太鬆會漏。等
  真實月結數據出來再校,屆時應改進 config。
- **fleet 歸因只看 cpu_cores 位移**:mem/storage 單獨變動(如換 DIMM)不觸發。
  訊號出現(容量變了但 cpu 沒變)再擴成逐維檢查。
- plan 與 reconcile 的 config 指紋不同時**不擋**,兩個指紋都回吐由 UI 標注
  ——擋掉會讓「換過 config 就永遠不能對帳」,但指紋不同時 forecast_error 的
  可比性下降,讀者要自行判斷。

## 6. 你應該帶走的知識

- **量測要跟預測用同一把尺**:預測用可落地量、量測用名目量,誤差裡就混進了
  尺差。`in_stock_slots` 存在的意義是把尺固定下來。
- **不可量測 ≠ 0**:分母為空的比率回 null;假指標比沒指標危險,因為它會被
  當真。
- **規則式歸因寧可謙虛**:能被 A 解釋的異常不再標 B(supply 吃掉 fleet)、
  上層對不上就不做下層歸因(fab 差抑制 placement)。多筆誠實標注勝過假裝
  單一成因。

## 7. 驗證方式

- `tests/test_reconcile.py` —— 19 條,策略是**用真 planner 產 plan** 再扭曲
  actual:四指標各自反應、隕石、超額 cap、unjoinable、碎片假象
  (`test_fragmentation_hits_slots_not_nominal` 是方案 B 被否決的可執行證據)、
  supply 吃掉 fleet、fab 差抑制 placement、endpoint round-trip。
- 親手跑:`curl -X POST :50051/v1/capacity/reconcile -d
  @examples/capacity/reconcile_basic.json` —— 預期:fulfillment 1.0(4/4)、
  forecast_error 0.5、unplanned_ratio 1/3、drifts = 一筆 demand(隕石)+
  一筆 fleet(-32c 換修縮水)。
