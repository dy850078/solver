# ADR-008: elastic sizing 加入張數下限與 escalate-until-feasible,讓「count 留空 = 最少幾台」成立

- **日期**: 2026-08-12
- **作者**: Claude (Fable)
- **相關 PR / commit**: branch `claude/topology-infeasible-analysis-ytig4x`(接續 ADR-007)
- **影響範圍**: `app/mockgen.py`, `tests/test_mockgen.py`, `docs/mock-request-generator.md`

> 寫作對象:一位正在學習 CP-SAT 與排程系統設計的工程師。

## 1. 背景與問題

使用者用 mockgen 的實際目的常是回答「**這些需求最少需要幾台 BM?**」——
BM profile 的 `count` 留空(elastic),期待生成器算出最小可行機隊。
但 elastic sizing 只有**容量界**(`Σ capacity ≥ Σ demand / tightness`)加
spread 下限。實案:5 master + 5 learner(各 `max_per_bm=1`)配 192 核大機,
容量除下來 2 台就夠、被 spread 撐到 3 台——但 5 台 master 每台 BM 只准放
1 個,**數學上非 5 台不可**。生成的 request 必然 INFEASIBLE,而且機器越大
容量界越鬆、越容易踩中。使用者被迫手動猜 count,工具的核心語意失效。

## 2. 考慮過的方案

1. **把最小台數做成 CP-SAT 決策變數**(每台 BM 一個 `open` BoolVar,
   `minimize Σ open`,VM 只能放在 open 的 BM 上)——理論最優雅,一次 solve
   得到精確最小值。**排除**:要在 solver 本體加一種新的模型型態(BM 集合
   從輸入變成決策),而 mockgen 只是 solver 的 client;mock 規模下
   「解析下限 + 幾輪重試」以 ms 級成本得到同樣答案,不值得動核心。
2. **二分搜尋台數**——比線性 +1 少幾次 solve?**排除**:可行性對台數
   **不保證單調**(多 pool + anti-affinity 交互時,更多 BM 改變 AG 分佈,
   cap `⌈N/buckets⌉` 跟著變),二分的前提不成立;且解析下限通常只差
   0–1 台,線性遞增實際更少輪。
3. **只加張數下限、不做驗證迴圈**——便宜,但 bin-packing 碎片(例:32c VM
   放 48c BM,容量界說 4 台、實際每台只裝得下 1 個)與規則交互仍會漏。
   **排除**:單獨採用會讓「最少幾台」在邊角情境變回猜測。
4. **下限取 max + escalate-until-feasible**(採用)——解析界把起點推到
   幾乎貼真值,真實 solver 驗證補掉解析看不到的部分。
5. **escalation 無腦對所有 elastic pool +1**——實作最簡。**排除**:
   兩個 pool 只有一個不夠時會過度配置,違背「求最小」;改為從 solver
   diagnostics 歸因到 role → pool,歸因不到才 fallback 全加。

## 3. 最終決策

elastic sizing 的 floor 改為三個必要條件取 max(容量界、spread 界、
**張數界** `max over groups of ceil(n/m)`);`verify=true` 時外層加
escalate-until-feasible:INFEASIBLE 就對被牽連的 pool +1 台重建重驗,
上限 `_MAX_ESCALATIONS = 10` 輪。固定 `count` 的 profile 永不加碼——
那是使用者的明確指定,INFEASIBLE 照實回報。

## 4. 實作走讀

- `app/mockgen.py:413` `_headcount_bounds()`:每個 role 的張數下限
  `ceil(n/m)`。這是 C4(max-per-BM)在「機隊要多大」問題上的投影:
  C4 建模時是 per-BM 的 `Σ assign[vm∈group, bm] ≤ m`(Step C),把它
  對所有 BM 加總得 `|VMs| ≤ m × |BMs|`,移項即 `|BMs| ≥ ceil(n/m)`——
  一個與容量完全無關的 counting bound。兩個刻意的「不乘」:同 role 跨
  ip_type 取 max 不取 sum(規則 key 是 (cluster, ip, role),不同群組可
  共用 BM);跨 cluster 不乘(各 cluster 的規則獨立計數,可重用同批 BM)。
- `app/mockgen.py:501-509`:`head_gap` 把已存在的 fixed BM 扣掉——bound
  是「服務該 role 的 BM 總數」,不是 elastic 自己的;固定 3 台時 elastic
  只需補 2。此 profile 每加一台就同時服務其所有 role,gap 與 copies 1:1
  遞減,所以進迴圈前算一次即可。`floor = max(spread, head_gap, min_copies)`
  中的 `min_copies` 是 escalation 的注入點:傳「上一輪 copies + 1」的
  **絕對值**而非「+1」的相對值,避免容量界已大於 floor 時 escalation
  變 no-op(單調遞增的保證來自這裡)。
- `app/mockgen.py:779-797` escalation 迴圈:build → ground truth →
  真實 `VMPlacementSolver.solve()`(即 Step A–D 全跑)→ 失敗則
  `_escalation_targets()`(`:742`)從 `infeasible_max_per_bm_rules` 的
  `group_id` 尾段與 `vms_with_no_eligible_bm` 的 vm→role 對映解析出
  受害 role,對映到服務它的 elastic profile。`round_no == _MAX_ESCALATIONS`
  時 break 不再記 trail,確保 `rounds` 語意 =「實際執行的加碼次數」。
- 診斷輸出 `app/mockgen.py:804`:`auto_escalated = {rounds, trail}`,
  trail 逐輪記 `{bms, status}` 加最終狀態——生成器替你加了幾台、每輪
  結果如何,UI 與 curl 都看得到,不是黑箱。

## 5. 取捨與風險

- Escalation 每輪完整重建 + solve;mock 規模(數十 BM)ms 級,但若拿
  mockgen 生千台場景會放大 11 倍最壞成本。`rounds` 在 diagnostics 可見,
  慢了有跡可循。
- 歸因依賴 `group_id` 格式 `maxbm/{cid}/{ip}/{role}`(rsplit 取尾段);
  格式若改,fallback(全 elastic +1)仍收斂,只是可能多 1–2 台。
- 可行性對台數不單調的極端情境理論上存在(AA bucket cap 隨 BM 分佈變化),
  escalation 只朝上走,可能錯過「較少台反而可行」的怪解——但那違反
  「最少幾台」的直覺用途,視為可接受;訊號:escalation 收斂的台數明顯
  高於手動試出的值時回頭檢查。

## 6. 你應該帶走的知識

- **容量界與張數界是兩種本質不同的下限**:前者把 VM 當可切分的液體
  (LP relaxation 視角),後者是純 counting(`ceil(n/m)`,與機器多大無關)。
  求最小機隊必須所有必要條件取 max,漏一個就會生出結構性 INFEASIBLE。
- **解析下限 + 驗證迴圈**是處理 NP-hard sizing 的實用模式:下限負責
  「起點幾乎正確」,真實 solver 負責「碎片與交互也算數」;二分搜尋在
  可行性不單調時是陷阱。
- **escalation 要傳絕對 floor 不是相對增量**:當另一個下限(容量)已高於
  你要加的值,「+1」會被 max 吃掉變 no-op;單調前進的保證必須顯式建構
  (`prev_copies + 1`),不能依賴「大概會變多」。

## 7. 驗證方式

- `tests/test_mockgen.py::test_elastic_floor_covers_max_per_bm`(張數界勝出,零 escalation)
- `tests/test_mockgen.py::test_elastic_floor_counts_fixed_bms`(fixed BM 抵扣 gap)
- `tests/test_mockgen.py::test_escalation_resolves_fragmentation`(碎片靠 escalation 收斂)
- `tests/test_mockgen.py::test_escalation_caps_and_reports`(不可救的輸入 10 輪封頂並回報 trail)
- 回歸:`test_elastic_profile_sizes_fleet` 等既有 sizing 測試全綠(共 266)。
- 手動:實案兩情境(64 核與 192 核 ctrl pool、count 留空)POST
  `/api/mock/generate` → 均自動得 5 台、OPTIMAL、`auto_escalated` 缺席
  (下限一步到位);將 tightness=1.0 + 48c BM + 5×32c VM 重送可看到
  `auto_escalated.rounds=1`。
