# ADR-012: 引入 pinned VM 原語——既有 VM 以固定變數帶入,C3/C4/C5 cap 既往不咎

- **日期**: 2026-08-18
- **作者**: Claude(與 dy850078 討論定案)
- **相關 PR / commit**: branch `claude/solver-ui-requirements-4sme83`(2e96b85..)
- **影響範圍**: `app/models.py`, `app/solver.py`, `app/diagnostics.py`,
  `app/capacity_planner.py`, `tests/*`, `examples/pinned_add_node.json`

> 寫作對象:一位正在學習 CP-SAT 與排程系統設計的工程師。

## 1. 背景與問題

生產調度是「一次一個 cluster」加上「對既有 cluster add-node」。solver 只看得到
本次請求中的 VM:第二次調度不知道第一次的擺放,C3 anti-affinity 的 per-bucket
計數每次從零開始——master 的 AG 分布第一次 2/2/1、下一次又 2/2/1,偏移逐批累積,
而且沒有任何約束能阻止它。`docs/capacity-planning.md` 決策表早已把「per-VM 身分
帶入」列為延後的 Option B;本次 add-node 需求正是兌現它的時機,同時它也是未來
rollout 模擬(逐 cluster 預演建置)的基礎原語。

核心難題有二:(a) 既有 VM 的資源消耗已記在 BM 的 `used_capacity`(DB 真相),
再以 VM 形式帶入會被 C2 重複扣款;(b) 既有擺放可能已違反今日的 rule(歷史偏移、
rule 改版),全局視野一開就會把整個 solve 炸成 INFEASIBLE。

## 2. 考慮過的方案

**方案 A:把既有 VM 折進 `used_capacity`(只調容量,不建變數)。**
優點:零模型成本。缺點:C3/C4/C5 的群組計數與 C6 的 outsider 判斷都讀
`assign` 變數,folded 的 VM 對它們完全隱形——C6 會把 appliance 放上已有住戶的
BM,**無聲違反 ADR-011 立約要守的保證**。被排除:正確性缺口不可補救。

**方案 B(採用):既有 VM 以「固定 assign 變數」帶入(`pinned_to` 顯式欄位)。**
單一候選 → 變數在 presolve 就化為常數,求解成本近零;C2–C6 自動看到完整人口。
配套:used 正規化(見 §3)與 grandfathered caps 處理歷史違規。

**方案 C:用 `candidate_baremetals` 單元素隱式表達 pinned。**
優點:不加欄位。被排除:「使用者指定新 VM 落點」也是單元素 candidate,兩者
語意不同——前者是對未來的限制(不豁免、可 INFEASIBLE),後者是對過去的陳述
(需正規化、需既往不咎)。隱式推斷會把兩者混為一談。

**Grandfather 的替代:軟約束(違規進目標函數罰分)。** 被排除:行為不可預測,
無法回答「為什麼這次它願意違規」;`max(cap, 既有數)` 是硬性的「不惡化」語意,
可解釋、可測試。

## 3. 最終決策

`VM.pinned_to: str | None` 為顯式事實欄位;`used_capacity` 維持 DB 真相(含
pinned 消耗),solver 於建模前一次性正規化 `effective_used = used − Σ(pinned
demand)`,固定變數再經 C2 把消耗加回——帳面淨額不變,這是合約定義的確定性轉換,
不是 silent fix。C3/C4/C5 的 per-bucket cap 改為 `max(base_cap, bucket 內
pinned 數)`:存量違規凍結該 bucket(新 VM 進不去)而非 INFEASIBLE。C6 不
grandfather(排他是二元 appliance 語意),pinned 造成的排他違規為 INPUT_ERROR。
結果回傳全量並以 `PlacementAssignment.pinned` 標記;capacity planner 路徑拒收
pinned(其健康度與 roll-forward 以 raw used 記帳,會雙算)。

## 4. 實作走讀

- `app/solver.py:110` + `:235`(Step A 之前):`_normalize_pinned_capacity` 在
  `bm_map`/`dim_to_bms` 建立**之前**以 `model_copy` 重建 baremetals。位置是關鍵:
  eligibility 的 `fits_in`、C2、headroom、slot score、procurement balance 全部
  透過 `bm_map`/`available_capacity` 讀容量,正規化一次、下游零修改。守門:任一
  維度 `effective_used < 0` 或宿主 `used > total` → INPUT_ERROR(否則會憑空鑄造
  容量,或讓固定變數把 C2 逼成無解)。
- `app/solver.py:84`(Step A):pinned 的 eligibility 特判寫在**模組函數**
  `get_eligible_baremetals` 裡——`_add_one_bm_per_vm_constraint` 會重算 eligibility,
  diagnostics 也 import 同一函數;特判放實例方法會讓兩處視角分裂。
- `app/solver.py:843`(Step C, C1):`model.add(assign == 1)` 置於
  `allow_partial_placement` 的 `<= 1` 分支之上——pin 是事實,partial 模式也不得
  丟棄。數學上這使該變數成為常數,presolve 直接代入。
- `app/solver.py:1040-1055`(Step C, C3 靜態分支):cap 移入 bucket 迴圈取
  `max(static_cap, pinned_b)`。整維早跳過(`static_cap >= N`)仍以 base cap 判斷
  ——grandfather 只會放鬆,跳過依然 sound。動態分支(`:999-1026`,splitter
  synthetic 與 pinned 同群時)沒有標量 cap 可抬,改用 reified disjunction:
  `z → K·load_b ≤ total_active + K−1`、`¬z → load_b ≤ pinned_b`。因 pinned 變數
  固定為 1,`load_b ≥ pinned_b` 恆真,`¬z` 即「此 bucket 不收新成員」——容忍歷史
  超額但不惡化。僅 `pinned_b > 0` 的 bucket 付這兩條約束的成本。
- `app/solver.py:664-676`(Step B, C5):`|P| > |L|` 預檢在 rule 含 pinned 成員時
  跳過——bucket 可凍結後,計數論證不再是不可行證明,交給模型判定。
- `app/diagnostics.py`:layer check 是**平行重建的小模型**,必須同步鏡射
  pin 固定與 grandfathered caps,否則主模型靠 grandfather 存活的場景,層歸因
  會反過來指控 anti_affinity(測試
  `test_grandfathered_skew_does_not_blame_anti_affinity` 守著這件事)。

## 5. 取捨與風險

- pinned 的 `demand` 必須是 inventory 記帳值;若與 DB 入帳口徑不一致且未扣到
  負值,防護欄抓不到,容量帳會靜默偏差——這是 scheduler 端的合約責任。
- 凍結語意讓歷史違規**永遠不會被主動修復**;需要修復時是另一個顯式 rebalance
  模式(解除 pin + 最小化搬動數),不是 add-node 的副作用。
- Legacy 宿主可能為 C3 預設 cap 分母帶進自由成員到不了的 bucket(分母灌水是
  `dim_to_bms` 既有特性,非本次引入);已加 advisory
  `pinned_legacy_bucket_in_spread_denominator` 提示,`cap_per_bucket` 是逃生門。
  若實務頻繁咬人,屆時獨立 ADR 修正分母語意。
- 重新審視訊號:pinned 數量大到建模時間有感(改為只 pin 規則相關 VM)、或
  capacity planner 需要 brownfield 支援(解除 v1 的拒收)。

## 6. 你應該帶走的知識

- **固定變數是帶入「既成事實」的正規手法**:單一候選 + `== 1` 的 BoolVar 在
  presolve 化為常數,近乎免費,卻讓所有讀 `assign` 的約束自動獲得全局視野;
  folding 進容量只對 C2 等價,對計數類約束(C3–C6)是資訊丟失。
- **既往不咎 = `max(cap, 既有數)`**:對存量違規不追溯、對新決策不惡化,而且
  自動把新 VM 導向不足的 bucket。動態 cap 沒有標量可抬時,用 reified
  disjunction 表達「守 cap 或凍結」。
- **平行模型必須同步語意**:diagnostics 的 layer check 自建模型,主模型的任何
  語意變更(pin、grandfather)都要鏡射過去,否則診斷與事實矛盾——比沒有診斷
  更糟。

## 7. 驗證方式

- `tests/test_solver.py::TestPinnedNormalization`(雙算迴歸、兩道守門)、
  `TestPinnedAntiAffinityGrandfather`(2/2/1 自我矯正、3/1/1 容忍不惡化)、
  `TestPinnedFailover`(預檢跳過、凍結導流)、`TestPinnedExclusive` 與
  `TestPinnedInputValidation`(C6 兩款違規)。
- `tests/test_splitter.py::TestPinnedWithSplitter`(動態分支 disjunction)、
  `tests/test_diagnostics.py::TestPinnedDiagnostics`(層歸因一致)。
- 親手驗證:`make cli INPUT=examples/pinned_add_node.json` — 兩台 pinned master
  原地不動(`pinned=true`),新 master 被導向空的 ag-3。
