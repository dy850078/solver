# ADR-002: 升級工作流支援——pinned VM(C6)、過渡態容量與最終態約束

- **日期**: 2026-07-24
- **作者**: Claude(與 dy850078 討論定案)
- **相關 PR / commit**: branch `claude/baremetal-k8s-upgrade-73ll3z`
- **影響範圍**: `app/models.py`, `app/solver.py`, `app/diagnostics.py`, `tests/`, `examples/upgrade_*.json`, `CLAUDE.md`

> 寫作對象:一位正在學習 CP-SAT 與排程系統設計的工程師。

## 1. 背景與問題

BM(OS 升版)與 K8s cluster 升級會定期發生且可能撞期,採「先加後減」
(surge-then-drain):先長出替代 VM、再 drain 舊 VM。改動前的 solver 是
「從零擺放」器——只看得到每台 BM 的 aggregate `used_capacity`,不知道
「VM X 在 BM Y 上」。這在升級情境是致命盲點:為升級中的 cluster 長新
master 時,solver 可能把三台新 master 放進與存活舊 master 相同的 AG 而
不自知。需要的新概念:既有 VM 的位置(pinned)、生命週期(誰要留、誰
要走)、BM 的 cordon 與版本標籤,以及「新舊並存」的過渡期語意。設計上
以情境 3(BM+K8s 合併升級)為骨架,情境 1/2 必須是同一套程式的退化特例。

## 2. 考慮過的方案

**A. Pinned VM = 固定值的 assign 變數(採用)**
為 `(vm, pinned_bm)` 建真實 BoolVar 並加 `var == 1`。
優點:C2–C5、`bm_used`、headroom、extraction 全部迭代 `self.assign`,
固定變數自動流進所有計算,零特例;CP-SAT presolve 會把固定布林代換掉,
無效能代價。缺點:`assignments` 回傳會包含本來就沒動的 VM(契約新增,
Go 端需知悉)。

**B. Pinned VM = 常數折疊(否決)**
不建變數,把 pinned 需求以常數加進 C2 的 RHS、C3/C4/C5 的 bucket 計數。
否決原因:每個 builder 都得認得 pinned(正是要避免的 special-casing),
且 `⌈N/buckets⌉` 的 N 與 per-bucket sum 活在兩種表示法裡,極易漏改。

**C. 容量契約方向:`used_capacity` 由 scheduler 先扣除 pinned(否決)**
否決原因:scheduler 漏扣時 pinned 需求被計兩次 → 假 INFEASIBLE,而且
solver 無從偵測。採用的方向(`used_capacity` 含 pinned、solver 扣除)
讓 solver 能驗證漂移:`Σ pinned demand > used_capacity` ⇒ INPUT_ERROR。

**D. 既有佈局違反 C3/C4/C5 時硬 fail(否決)**
否決原因:solver 動不了 pinned VM,對它無法改變的現實報 INFEASIBLE 會
讓升級功能在真實遺留環境完全不可用。採用 cap 放寬 + advisory 回報。

## 3. 最終決策

新增契約欄位(VM: `lifecycle`/`pinned_bm`/`replaces`/`eviction_blocked`/
`prefer_bm_labels`;BM: `schedulable`/`labels`;config: `w_label_preference`),
以 **C6(固定指派)** 把既有 VM 帶進模型;**C2 驗證過渡態**(新舊並存),
**C3/C4/C5 驗證最終態**(排除 `to_be_removed`);既有違規以「放寬到
pinned 數量 + advisory」處理;避免二次搬遷用軟性 label 偏好,不做硬過濾。

## 4. 實作走讀

- `app/solver.py:752` — `_add_pinned_assignment_constraints`(Step C):
  C6 的全部數學就是 `assign[vm, pinned_bm] == 1`。關鍵在它「不做」什麼:
  因為 Step A 的 eligibility 對 pinned VM 只回傳 `[pinned_bm]`(`solver.py:88`),
  這一個變數存在且唯一,固定後 C1 的 `Σ = 1` 自動滿足,C2–C5 的每個
  bucket sum 都得到正確的常數貢獻。
- `app/solver.py:236-242` — `effective_used = used_capacity − Σ(pinned demand)`
  (Step A 前的預計算):這是防重複計算的核心。C2 寫成
  `Σ(所有列出 VM 的 demand×var) ≤ total − effective_used`(`solver.py:793`),
  對新 VM 移項後恰好等於舊語意 `≤ total − used`,所以 eligibility 的
  fits 檢查不必改——這個推導值得記住。負值檢查即漂移 INPUT_ERROR。
- `app/solver.py:937` — dynamic-ceil 路徑的放寬常數(Step C, C3):
  `relax_b = max(0, pinned_in_b·|B| − explicit_count − (|B|−1))`。
  推導:約束是 `count·|B| ≤ total_active + (|B|−1) + relax_b`,最壞情況
  `total_active = explicit_count`(synthetic 全不啟用)時必須容納固定的
  `pinned_in_b`,解出 relax_b。靜態路徑則簡單得多:`max(cap, pinned_in_b)`。
- `app/diagnostics.py:62,280` — layer check 鏡射(Step D):診斷的
  throwaway model 給 pinned VM 的唯一變數會被 one_bm_per_vm 層固定,
  等效 C6,故不需新增 pinned 層;但 capacity 層的 RHS 必須同步改用
  `effective_available`、C3/C4/C5 層必須套同樣的放寬,否則 `failed_at`
  會指錯層。共用 `pinned_count_in_bucket`(`solver.py:96`)避免兩處漂移。

## 5. 取捨與風險

- `assignments` 現在回傳 pinned 與 `to_be_removed` VM(完整過渡態圖像),
  Go scheduler 須以自己已知的 VM id/lifecycle 區分——這是契約新增。
- 放寬 + advisory 意味「既有違規」不會擋下 solve;若 scheduler 忽略
  advisory,違規會持續存在。訊號:advisory 頻繁出現同一 group 時應人工介入。
- `capacity_planner` 的 post-solve 記帳(roll-forward)未考慮 pinned VM
  ——規劃請求混入 pinned VM 時 `used_capacity` 會重複累加。目前規劃路徑
  不使用 pinned;若未來要混用,先修 `_roll_forward`。
- `replaces` 目前只驗證不影響擺放;若需要「新 VM 避開被取代者的 BM」,
  加一個 `w_replace_apart * assign[new, old.pinned_bm]` 罰項即可(已預留)。

## 6. 你應該帶走的知識

- **固定變數優於常數折疊**:CP-SAT presolve 會消掉固定布林,代價為零,
  卻讓所有既有約束「免費」看見既有狀態——建模時先想「能不能用固定變數
  表達已成事實」,再考慮改寫約束。
- **同一組變數可以對不同約束呈現不同世界**:C2 看過渡態、C3/C4/C5 看
  最終態,差別只在「誰是成員」(membership),不在變數本身——把時間語意
  編碼在成員資格而非變數,是避免 time-indexed 模型爆炸的關鍵。
- **對 solver 動不了的現實,放寬 + 回報,不要 INFEASIBLE**:約束的職責
  是「不讓事情變得更糟」;把「現況已壞」變成硬失敗,功能就無法上線。

## 7. 驗證方式

- `tests/test_solver.py::TestPinnedAssignment`(C6)、
  `TestTransitionalCapacity::test_no_double_count_of_pinned_demand`
  (重複計算防護,漏改任一 `used_capacity` 讀取即 fail)、
  `TestPinnedAntiAffinity`/`TestPinnedMaxPerBm`/`TestPinnedFailover`
  (最終態成員 + 放寬)、`TestUpgradeDegenerate`(三情境同路徑退化)。
- `make cli INPUT=examples/upgrade_bm_cordon_drain.json`(情境 1)、
  `upgrade_k8s_surge_drain.json`(情境 2)、
  `upgrade_combined_surge_drain.json`(情境 3,含 `bm_not_evictable`
  advisory)。三者已用 scratchpad 腳本逐條驗證 C1–C6 數學成立。
