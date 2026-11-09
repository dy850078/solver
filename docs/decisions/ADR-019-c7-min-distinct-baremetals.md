# ADR-019: C7 最小佔用機器數——「這群 VM 至少要分散在 N 台 BM」做成精確約束

- **日期**: 2026-11-09
- **作者**: Claude (Fable)
- **相關 PR / commit**: branch `claude/trusting-gates-ys3ofp`
- **影響範圍**: `app/models.py`, `app/solver.py`, `app/diagnostics.py`, `app/splitter.py`, `app/split_solver.py`, `app/rollout.py`, `app/rollout_sizing.py`, `app/sizing_floors.py`, `examples/min_distinct_bms.json`, `CLAUDE.md`

> 寫作對象:一位正在學習 CP-SAT 與排程系統設計的工程師。
> 寫作口吻:資深工程師帶 code review——不只說「做了什麼」,要說「為什麼、
> 還考慮過什麼、你應該從中學到什麼」。全文使用繁體中文,程式碼與術語保留英文。

## 1. 背景與問題

採買規劃的需求裡有一條政策:「每個 cluster 的每種 node type,VM 至少要分散在
6 台 BM 上」。這是 HA 的機器層級下限——不是「每個 AG 最多幾顆」(C3),也不是
「每台 BM 最多幾顆」(C4),而是「**至少**幾台 BM 有這群的人」。

盤點既有約束後發現沒有一個能精確表達它:

- **C3** 的維度只到 `rack`(`SPREAD_DIMENSIONS`,`app/models.py:24`),沒有
  BM 層級;而且 C3 是每個 bucket 的**上限**,方向相反。
- **`target_spread`** 只產出 `spread_below_target` advisory
  (`app/solver.py:624-653`),不是約束。
- **C4** `max_per_bm = ⌈count/N⌉` 可以**近似**——11 顆、N=6 時 cap=2,
  11 顆每台最多 2 顆至少要 6 台。但 (a) splitter 路徑上 `count` 是決策變數,
  編譯期算不出 cap;(b) 它禁止不均勻排法:3+2+2+2+1+1 跨了 6 台、完全合法,
  C4 卻不准某台放 3 顆;(c) N 不整除 count 時會過度分散——10 顆、N=6,
  cap=2 只保證 5 台,得退到 cap=1 → 10 台。

PM 的採購數字是硬的,多買 4 台不是「保守」,是算錯。所以需要一個精確的下界約束。

## 2. 考慮過的方案

1. **C4 近似 + 事後檢查**——不改 solver。缺陷如上:split 路徑算不出、
   過度分散、而且「事後檢查」在 sizing 迴圈裡等於沒檢查(solver 說可行,
   報表說不符合,誰對?)。**排除。**
2. **只做 advisory**(像 `target_spread`)——不保證,違背「一定擺得下」。**排除。**
3. **新約束 C7,完整 reification**:每台 BM 一個布林 `host[g,b]`,
   `host == max(assign[v,b] : v∈G)`(`add_max_equality`,C6 的寫法),
   `Σ_b host ≥ N`。正確,但多貼了一個方向的約束而沒有擋住任何作弊(見 §4)。
   **保留為「若未來有人讀 host 就改回來」的 fallback。**
4. **新約束 C7,單邊連結**(採用):`host[g,b] ≤ Σ_{v∈G} assign[v,b]`、
   `Σ_b host ≥ N`。

## 3. 最終決策

新增 **C7 `MinDistinctBaremetalRule`**(`group_id` / `vm_ids` 或 `selector` /
`min_distinct_bms`),走和 C4、C6 一樣的 selector 解析路徑,用一個輔助布林
加單邊連結建模。它是**下界**,所以 pinned VM 不需要 grandfather;靜態 group
成員數 < N 在 Step B 直接回 INPUT_ERROR;可達 BM 數 < N 由 model 回
INFEASIBLE 並在 diagnostics 點名。split-and-solve、rollout、rollout sizing
三條路徑全部貫通(ADR-013 的教訓:新 rule 欄位最容易在 `split_solver.py`
被默默丟掉),`sizing_floors` 新增 `distinct_floor`。

## 4. 實作走讀

**為什麼需要 `host`(`app/solver.py` `_add_min_distinct_constraints`)。**
我們要的量是「有成員的 BM 數」= `Σ_b [Σ_v assign[v,b] ≥ 1]`。中括號是指示函數,
不是線性式,CP-SAT 不能直接對它寫 `≥ N`。標準手法是**引入輔助變數代表指示值**,
再用線性約束把它和原變數綁住。這是 CP-SAT 建模裡最常用的技巧之一
(reification / indicator),C6 的 `z_b` 也是同一招。

**為什麼只綁一個方向。** 完整 reification 有兩個方向:

- (a) `host ≤ Σ assign`:host=1 ⇒ 那台至少有一個成員。**禁止灌水。**
- (b) `host ≥ assign[v,b]` ∀v:有成員 ⇒ host 必為 1。**禁止漏算。**

我們的目標約束 `Σ_b host ≥ N` 是**下界**。solver 能作弊的唯一方式是把沒人的
BM 標成 host=1 來湊數——(a) 擋住這件事。(b) 只會把 Σ host 推**大**,對
「≥ N」只有幫助、沒有限制;solver 本來就可以自由把有成員的台設 1。所以兩種
模型在 `assign` 上的解集合完全相同(投影相等),這和 ADR-011 §4 對 C6 的
論證是同一套。C6 最後選完整 reification,是因為它的 `z_b` 還被
「outsiders barred」約束**讀取**(`assign[u,b] + z_b ≤ 1`);C7 的 `host`
沒有別人讀、也不進 objective。方法 docstring 寫明了:哪天有人要讀 host、
或約束反過來變成「最多 N 台」(上界),就要補 (b)。

**host 只在有成員 assign var 的 BM 上建**(`cpsat-anti-patterns` #6)。一台
成員全不可達的 BM 貢獻必為 0,建變數只是浪費。注意即使可達 BM 數 < N,
`Σ host ≥ N` 仍然會貼進 model——讓失敗留在 model 裡,diagnostics 才有東西
可以點名,而不是在 Python 端提前 return 一個沒人解釋的 INFEASIBLE。

**合成 slot 不需特殊處理。** splitter 未啟用的 slot 其 `assign` 全被 C1
壓成 0,(a) 自動讓那台的 host 為 0——和 C4 的 docstring 說的同一件事。

**Pinned 不 grandfather(對比 ADR-012)。** C3/C4/C5 是上界,既有 pinned
佈局可能**已經超過** cap,不放寬 model 就直接 INFEASIBLE,所以才有
`max(cap, pinned)`。C7 是下界,pinned VM 固定在某台只會讓 host 多一個 1、
只會幫忙達到 N,不可能違反。沒有要放寬的東西。

**Pre-flight 放在 Step B(`_resolve_min_distinct_rules`)。** group 只有
明確 VM 且不到 N 顆時 `Σ host ≤ |M| < N`,任何 fleet 都無解。這是 contract
問題不是 fleet 問題——鏡像 C5 的 `|primary| > |backup|` 檢查回 INPUT_ERROR。
合成 slot **不**計入這個檢查:它們的 active 數是 splitter 的決策,slot 上界
不是人口普查(`tests/test_splitter.py::TestSplitWithMinDistinctBm::test_synthetic_group_is_not_preflight_rejected`)。
放進 sizing 的話會燒光 probe 預算後怪機器不夠,這正是 ADR-014 §_validate
要攔的那類「任何大小都不可行」。

**`distinct_floor`(`app/sizing_floors.py`)。** 一條 C7 rule 對 fleet size
的投影就是 N 本身。rules 之間取 **max 不取 sum**:不同 group 可以共用機器,
6 + 6 不需要 12。只有當 rule 真的命中成員(明確 VM 或 selector 命中一個有
需求的 requirement)才算,否則貢獻 0——`sizing_floors` 的鐵律是寧可低估不可高估。

## 5. 取捨與風險

- **C7 × 自動 C3 的交互**:N=6、3 AG、動態 cap ⌈n/3⌉ ⇒ 每個 AG 至少要有
  2 台可用 BM。先前的 cluster 塞滿某個 AG 時,總容量夠也會死路。這是
  rollout 模擬該揭露的事實,不是 bug;但 diagnostics 的層序把 `anti_affinity`
  排在 `min_distinct_bm` 之前,兩者同時綁住時 `failed_at` 會點名 C3。
- **Sizing 非單調性多一個來源**:C7 會改變 solver 在 step k 的任意選擇,
  影響 step k+1。線性掃描(ADR-014)本來就處理這件事;`distinct_floor` 抬高
  起點減少 probe 數。
- **沒有 auto-generation**:和 C6 一樣,footprint 是 caller 明示的政策,
  不從 `(cluster, ip_type, role)` 分組推斷。若未來 default 想自動套用,
  應該像 `default_max_per_bm` 一樣走 config 開關 + INPUT_ERROR 守門。
- **Procurement planner 沒接 C7**(和 C6 一樣),ADR-003 的 pool 路徑若要
  用再補。

## 6. 你應該帶走的知識

1. **指示函數要靠輔助布林線性化**,而綁幾個方向取決於目標約束的方向:下界
   只需要「禁止灌水」那一邊,上界只需要「禁止漏算」那一邊。先問「solver 會
   怎麼作弊」,再決定貼哪個約束。
2. **上界才需要 grandfather,下界不用。** 放寬模型是為了容忍既有違規;
   一個既有狀態不可能違反的約束,沒有東西要放寬。
3. **近似約束的代價在不整除的地方。** C4 當 C7 用,在 N | count 時剛好精確,
   其他時候要嘛不夠要嘛過度——而「過度」在採購場景就是多花錢。

## 7. 驗證方式

- `tests/test_solver.py::TestMinDistinctBaremetal`:≥N、允許不均勻(6+1+1+1+1+1)、
  INPUT_ERROR、INFEASIBLE + `failed_at == "min_distinct_bm"`、pinned 計入、
  role list selector、空 group advisory。
- `tests/test_splitter.py::TestSplitWithMinDistinctBm`:split 路徑貫通(先驗
  baseline 會 consolidate,再驗 rule 推到 ≥6)。
- `tests/test_rollout.py::TestRolloutMinDistinct`:跨 step 累積、pin 計入。
- `tests/test_rollout_sizing.py::TestSizingMinDistinct`:`distinct_floor`、
  「10 顆半機、N=6 → 恰好 6 台」(C4 近似會給 10)。
- 親手驗證:`make cli INPUT=examples/min_distinct_bms.json OUTPUT=output/verify-c7.json`,
  11 顆 1/3 機 VM 落在 6 台(1+2+2+2+2+2),AG 4/4/3。
