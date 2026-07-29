# ADR-006: S5 一致性回放測試 —— 守護「plan 可行 ⇒ execution 可行」

- **日期**: 2026-07-29
- **作者**: Claude (mentor mode)
- **相關 PR / commit**: branch `claude/provision-e2e-automation-vision-yyzen0`
- **影響範圍**: `tests/test_consistency_replay.py`(新;app/ 無變更)

> 寫作對象:一位正在學習 CP-SAT 與排程系統設計的工程師。

## 1. 背景與問題

核心價值主張是「規劃承諾的容量,執行時真的放得進去」,但這句話至今是願望不
是性質:規劃與執行的候選推導可以各自漂移(D1)、config 可以只改一邊(D2),
而且**壞掉時不會有任何測試變紅**。M3 已定守門方式:同一快照雙跑
(planning view → `/v1/capacity/plan`、execution view → `/split-and-solve`),
①可行而②INFEASIBLE 且原因不在顯式差集 → 紅燈。S5 是它的 solver 端單元層。

## 2. 考慮過的方案

**A. 合成快照 + replay harness(採用)** —— 測試檔內建 M1 已定差集表
(os_not_ready / in_repair 規劃納入、reserved 兩邊排除),從「機器+狀態」
推導兩個 view 雙跑,判定分四級。優點:不依賴 Go、快、可精準製造每種漂移。

**B. 只寫「同 view 雙跑必須同可行」的性質測試** —— 少了差集語意,等於假設
兩個 profile 相同;真正要守的恰是「差集之外不准有差」。被排除:守錯性質。

**C. 等 G7 直接上真實快照 E2E** —— 真快照抓得到現實但無法窮舉漂移型態,且
紅燈歸因困難(是 filter?config?引擎?)。兩層本就互補,solver 層先行。

**實作期兩個裁決**:
- **未申報的 view 差在 solve 之前用集合比對抓**。若靠「execution view 重解
  也不可行」推斷,申報差集與未申報漂移會得到同一種失敗訊號,無法區分。
- **判定優先序:filter 先於 divergence**。view 都不對時再報引擎分歧,會把
  修理方向誤導到 config/引擎,而真兇是 filter 清單。

## 3. 最終決策

`tests/test_consistency_replay.py::replay`:①集合比對抓未申報 view 差 →
②planning view 跑 `solve_capacity_plan`(無採買型錄,純「放得下嗎」)→
③execution view 跑 `solve_split_placement`(candidates=該 view,即 Go step-3
會交付的清單)→ ④若③敗,以**同一 config** 對 execution view 重問規劃問題:
仍可行=兩個 view 都有容量、只有第二條管線拒絕 → `broken`(引擎/config 分歧);
不可行 → `explained_by_profile_delta`(承諾壓在申報差集機器上,設計內容忍)。

## 4. 實作走讀

- `tests/test_consistency_replay.py:44-46` —— 差集表就是 M1 的三列,直接寫成
  兩個狀態集合。它是這組測試的**規格常數**:Go 端若改差集,這裡必須同步改,
  改動本身就會經過 review——這正是 M1「強制決策」的單元層體現。
- `:96-99` —— 集合比對在任何 solve 前:`execution_ids != declared` 即紅。
  數學上這是把「filter 漂移」定義成 view 的集合性質,與「容量夠不夠」的
  可行性性質分離——兩個正交維度分開檢查,歸因才不含糊。
- `:108-116` —— ④的重問用 ① 的 config(不是 exec_config):控制變因,讓
  「view 相同、config 不同」的失敗能被歸到 divergence 而非容量。
- `test_config_drift_is_red` 順帶示範一個陷阱:auto max_per_bm 的分組鍵是
  `(cluster, ip_type, role)` 且空 `ip_type` 會跳過——合成需求不填 ip_type,
  漂移 config 會靜默不生效,測試假綠。

## 5. 取捨與風險

- 合成快照窮舉的是**漂移型態**不是**真實形狀**;真快照的長尾(奇形拓撲、
  巨量規模)要靠 G7 層。兩層缺一不可。
- 差集表在測試裡是手抄本;Go 端差集若改而這裡忘了改,測試會用舊規格審新
  世界。G7 落地時應讓兩邊共用同一份機器可讀的差集清單。
- harness 住在 tests/:G7 是 Go 端走 HTTP,不會 import 它;若未來 solver 端
  要提供 replay 端點,再搬進 app/(YAGNI 先不搬)。

## 6. 你應該帶走的知識

- **保證要變成會變紅的測試才算數**:「共用同一套約束模型」是架構事實,
  但架構事實擋不住 filter 清單各自演化——性質要有守門員。
- **先驗檢查與求解檢查分層**:能用集合比對回答的問題(view 差)不要交給
  solver 推斷;混在一起的失敗訊號無法歸因。
- **控制變因是歸因的前提**:④重問刻意鎖 config,才能把 divergence 與
  容量不足分開。

## 7. 驗證方式

- `tests/test_consistency_replay.py` —— 7 條:綠路、os_not_ready/in_repair
  兩種申報差集容忍、reserved 兩邊排除(在 plan 端就擋下)、未申報 filter
  紅燈、config 漂移紅燈、雙漂移時 filter 判定優先。
- 全套:`make test`(258 條)。此變更不動 app/,無 example 需求。
