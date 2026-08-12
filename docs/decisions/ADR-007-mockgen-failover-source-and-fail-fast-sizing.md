# ADR-007: mockgen 修正 failover 讀錯需求來源、elastic sizing 改為 fail fast

- **日期**: 2026-08-12
- **作者**: Claude (Fable)
- **相關 PR / commit**: branch `claude/topology-infeasible-analysis-ytig4x`
- **影響範圍**: `app/mockgen.py`, `tests/test_mockgen.py`

> 寫作對象:一位正在學習 CP-SAT 與排程系統設計的工程師。

## 1. 背景與問題

Topology Visualizer UI 走 `node_groups` 模式產生 mock request 時發現兩個缺陷:

**(a) failover 打勾靜默失效。** `_build_failover_rules()` 用
`req.roles.get("learner", 0)` 判斷 learner 是否存在,但 UI 只送
`node_groups`、不送 `roles`,於是 `roles` 停在預設值
`{"master":3, "worker":3, "infra":2}`——沒有 learner,規則永遠被 skip。
使用者勾了 failover,C5 約束卻從未進入模型,而且回應看起來完全正常
(diagnostics 裡有一行 `failover_skipped`,但 UI 不會攔下來)。

**(b) elastic sizing 可以失控到 100,000 台 BM。** BM profile 的 count 留空時,
`_build_baremetals()` 用 while 迴圈加機器直到總容量 cover 總需求。
迴圈條件是 `not _covers(have, need)`,但若 profile 某個資源欄位是 0
(UI 表單留白即為 0)而 VM 在該欄位有需求,`have` 在該維度永遠追不上
`need`——迴圈只會被 `guard < 100_000` 保險絲停下,然後**靜默回傳**一個
10 萬台 BM、180 萬個 candidate pair、54 MB 的 request。實測 23.6 秒,
在 istio-ingressgateway 後面直接變成 `504 upstream request timeout`。

## 2. 考慮過的方案

**(a) failover 來源:**

1. **就地判斷 `any(g.role == "learner" for g in node_groups)`** —— 改動最小,
   但「某 role 每 cluster 有幾台」這個問題在 `_cap_for()`、
   `_build_max_per_bm_rules()` 已各自處理過一次,再散一處就是第三份邏輯。
   **排除:重複邏輯只會繼續發散。**
2. **在 node_groups 模式下自動回填 `req.roles`** —— 讓舊判斷式不用改,
   但 `roles` 會同時是使用者輸入與衍生值,而 `GenerateRequest` 是 API 契約,
   generator 不該改寫它。**排除:輸入被靜默改寫,違反本專案
   INPUT_ERROR-over-silent-fix 原則。**
3. **抽 `_role_counts()` helper,雙路徑聚合**(採用)—— node_groups 時聚合
   各 group 的 count,否則回傳 legacy dict。與既有雙路徑寫法一致。

**(b) runaway sizing:**

1. **把容量 0 視為「不限制」自動修補** —— 使用者少填一格就得到暗中放寬的
   模型;且 0 是合法輸入(diskless 節點 + storage=0 的 VM 是真實情境)。
   **排除:靜默修補,同上原則。**
2. **只調低 guard 上限** —— 錯誤訊息只能說「超過上限」,使用者還是要自己
   猜出是哪個欄位是 0。**排除:診斷責任被丟回給人。**
3. **前置檢查不可收斂條件 + guard 改為報錯**(採用)—— 迴圈發散的充要條件
   是「存在資源欄位 f 使 `p.capacity[f] == 0` 且 `need[f] > have[f]`」,
   直接在進迴圈前檢測並回 400 指名欄位;guard 降為 5,000 並在觸頂時
   raise 而非靜默回傳,作為第二道防線。

## 3. 最終決策

failover 判斷改經 `_role_counts()` 讀取當前生效的需求來源;elastic sizing
在迴圈前以「零容量欄位 × 殘餘需求」檢測數學上不可收斂的組合並回 400,
guard 上限降至 `_MAX_ELASTIC_BMS = 5_000` 且觸頂即報錯。兩者共同原則:
**寧可把契約violations 變成明確錯誤,也不回傳一個看似成功的垃圾結果。**

## 4. 實作走讀

- `app/mockgen.py:609` `_role_counts()`:回傳每 cluster 各 role 的 VM 數。
  node_groups 模式下同一 role 可能拆在多個 group(不同 spec/ip_type),
  所以必須聚合而非取第一個。這裡的輸出餵給 `_build_failover_rules()`
  (`app/mockgen.py:620`)——它產生的 `FailoverRule` 最終在 solver 的
  **Step B**(rule validation + selector expansion)展開成 primary/backup
  VM 集合,並於 **Step C** 建成 C5:對 fault_domain 每個 bucket b,
  `Σ(primary∈b) + Σ(backup∈b) ≤ |backup|`,即最壞情況整個 bucket 全滅時,
  倖存的 backup 數仍足以承接該 bucket 內的 primary。修好前這條約束
  根本不存在於模型中——**打勾與否模型完全相同**。
- `app/mockgen.py:443` 不可收斂檢測:`deficient` 收集所有
  `p.capacity[f] == 0 ∧ need[f] > have[f]` 的欄位。數學意義:sizing 迴圈
  每輪讓 `have += p.capacity`,是一條斜率為 `p.capacity` 的單調遞增數列;
  斜率在維度 f 為 0 而目標差距為正,則該維度永遠不可達——這不是
  「要跑很久」,是**發散**,所以屬於 400(輸入錯誤)而非 timeout(算力問題)。
  錯誤訊息帶欄位名與缺口量,使用者一眼知道該補哪一格。
- `app/mockgen.py:460` guard 觸頂改 raise:斜率非零但極小(如 storage=1 GB
  對上數千 GB 需求)時迴圈會收斂但機器數爆炸。5,000 台是「任何合理 mock
  情境的十倍以上」的量級判斷,觸頂即代表輸入有誤,回 400 並附上
  `need`/`have` 快照。舊行為(靜默回傳)讓錯誤在三層之外才爆炸:
  browser parse 54 MB、ingress 504、或 solver 收到 180 萬變數。
- 這些都發生在 mockgen(solver 之前的請求產生層),但設計對齊 solver 的
  **Step A** 精神:eligibility 檢查在建模前把不可能的組合擋掉,
  不讓 CP-SAT 花時間證明一個人眼可判的 INFEASIBLE。

## 5. 取捨與風險

- **行為變更**:node_groups + failover 打勾以前是 no-op,現在會真的建出
  C5。某些過去回 OPTIMAL 的組合可能開始回 INFEASIBLE——這是正確結果,
  但對慣用者是可見的變化。
- 前置檢測只覆蓋「零容量」這一種發散;`min_pool` 條件(copies 下限)
  永遠可收斂,故不在檢測範圍。若未來迴圈條件加入新項,需重新推導
  收斂條件——訊號:再次看到 guard 觸頂的 400 卻找不到零容量欄位。
- 5,000 上限是啟發式。若有天 mock 情境真的需要更大艦隊,
  這個常數要跟著動(它在模組層級,一行即可)。

## 6. 你應該帶走的知識

- **保險絲要會叫。** 只擋住失控(guard)而不報錯,等於把錯誤搬到更遠、
  更貴的地方爆炸(這次是 ingress 的 504)。fail fast 的價值在於
  錯誤訊息出現在因果現場。
- **同一份事實不要有兩個來源。** `roles` 與 `node_groups` 並存時,
  每個讀取點都必須知道哪份生效;抽 `_role_counts()` 把「選來源」collapse
  成一處,是雙路徑 API 演進的標準解法。
- **先分辨發散與收斂慢。** 迴圈的單調遞增量在某維度斜率為 0 且差距為正
  ⇒ 數學上不可達,該回 INPUT_ERROR;斜率極小 ⇒ 收斂但代價爆炸,
  該設上限。兩者的正確錯誤訊息完全不同。

## 7. 驗證方式

- `tests/test_mockgen.py::test_node_groups_failover_emits_per_cluster_rule`
- `tests/test_mockgen.py::test_node_groups_failover_skipped_without_learner`
- `tests/test_mockgen.py::test_elastic_profile_rejects_vm_it_cannot_host`
- `tests/test_mockgen.py::test_elastic_profile_guard_raises_instead_of_runaway`
- legacy 路徑回歸:`test_failover_emits_per_cluster_rule`、
  `test_failover_skipped_without_learner` 維持綠燈。
- 手動驗證:`make run` 後將 UI 情境(master/learner/l4lb-storage/infra ×
  node_groups + failover)POST 到 `/api/mock/generate`——回應應含 1 條
  failover rule;將 BM profile 的 storage 改 0 重送,應在毫秒級收到 400
  而非 54 MB 回應。
