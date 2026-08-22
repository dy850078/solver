# ADR-014: Rollout sizing——拓撲模板產生機隊,並以「解析下界 + 上行掃描」回答最少台數

- **日期**: 2026-08-22
- **作者**: Claude(與 dy850078 討論定案)
- **相關 PR / commit**: branch `claude/solver-ui-requirements-4sme83`(e899e5f..)
- **影響範圍**: `app/models.py`, `app/sizing_floors.py`(新), `app/rollout_sizing.py`(新),
  `app/server.py`, `app/web_static/*`, `tests/test_rollout_sizing.py`

> 寫作對象:一位正在學習 CP-SAT 與排程系統設計的工程師。

## 1. 背景與問題

rollout 模擬(ADR-013)要求先給定機隊。建廠情境下問題是反的:**「照這個 cluster
rollout 順序建,最少要買幾台 BM?」**——台數未知,但「散到幾個 AG」是明確的規劃
輸入。另外原本 UI 只有一個 AG 字串,site/phase/dc/room/rack 完全無法表達。

**與既有功能的關係(三者答案不同,必須講清楚)**:

| 功能 | 回答 | 為何不能直接沿用 |
|---|---|---|
| mockgen elastic(`count` 留空,ADR-008) | 單一批次最少台數 | 產出單一 `PlacementRequest`,所有 cluster 一次聯合擺放,無順序、無 pinned 折疊 |
| capacity planner `/v1/capacity/plan` | 採購尺度要買幾台 | 近似模型;明確拒收 pinned VM(`capacity_planner.py:369-386`) |
| 本 ADR | **循序建置**最少台數 | — |

偏序:`capacity planner ≤ mockgen elastic ≤ rollout sizing`。循序建置會碎片化,
聯合擺放永遠較緊;把所有步驟合併求解得到的是**下界**,不是答案。

## 2. 考慮過的方案

**方案 A:把台數變成 CP-SAT 決策變數**(每台一個 `open` BoolVar、minimize Σ open)。
理論最優雅,一次 solve 得精確最小。**排除**:rollout 的語意是「逐步求解、逐步釘死」,
不是單一模型;要把 S 個步驟的順序與 pin 展開成一個模型等於重寫 solver 核心,
而循序性正是我們要量測的東西。

**方案 B:二分搜尋台數**。**排除——這是本 ADR 最重要的決定**。
`ADR-008 §2 方案 2` 已為 mockgen 否決過(可行性對台數不保證單調,bucket 數會隨
機隊變),rollout 還多兩個更強的成因:
1. **貪婪路徑依賴**:每步只提交**一個**最佳解並釘死。加機器改變模型,solver 可能
   挑到不同的等價最佳解,讓後續步驟反而失敗。序列可能是 `F,F,S,F,S,S`,
   二分會回傳垃圾,加倍也可能跳過真正的最小值。
2. **UNKNOWN 與 INFEASIBLE 在 API 表面同為 `success=False`**:大 N 的模型最慢最易
   逾時,會被誤讀成「不可行」而一路加碼到上限。
**方案 C(採用):解析下界 → 逐台 +1 上行掃描。** 在非單調地形下**反而比二分更
正確**:`[floor, N*)` 全部實測失敗、`< floor` 由下界排除,故 `N*` 是精確最小值。

**下界高估 vs 低估**:第一版 `capacity_floor` 把 VM 依規格分組、各自
`ceil(n / fits_per_bm)` 再**相加**——**被自己的下界測試抓到是錯的**:它假設不同
規格的 VM 不能共機(8c 明明能和 40c 共用 64c 的機器),高估成 4,搜尋從 4 起跳,
永遠看不到真正可行的 3。改為嚴格的**逐欄位體積界** `⌈Σdemand_f / cap_f⌉`。

## 3. 最終決策

`FleetTemplate` 用拓撲計數(sites/phases/dc/rooms/racks/ags)描述機隊:每個 rack
一組 topology、上層維度以 rack 序號取模輪替(與 `mockgen._build_racks` 同規則),
機器再 round-robin 灑到 racks——per-AG 因此恆平衡到差 ≤1。台數給定就是固定庫存;
**台數留空 = 估算**,走 `POST /v1/placement/rollout/size`:算解析下界 → 逐台上行
掃描,每次探測跑真正的 `solve_rollout`,回傳台數、per-AG 分佈、下界拆解與探測足跡。

## 4. 實作走讀

- `app/sizing_floors.py:71` `capacity_floor`:體積界 `max_f ⌈Σdemand_f / cap_f⌉`。
  註解寫明為何**不能**用「分組相除再相加」。requirement 直接用 `total_resources`,
  體積與 spec 選擇無關,所以這一項對 splitter 的決策免疫。
- `app/sizing_floors.py` `fleet_floor`:`max(ags, solo + max(capacity, headcount, pack))`。
  **solo 是加法**:C6 成員獨占整機、不服務任何其他 VM,不能與其他需求取 max;
  其餘三項都是同一批非獨占 VM 的下界,取 max 才不重複計算。
  `headcount` 是 C4 對機隊大小的投影(ADR-008 的張數界):把 per-BM 的
  `Σ assign[vm∈group, bm] ≤ m` 對所有機器加總得 `|BMs| ≥ ⌈n/m⌉`,與容量無關。
- `app/rollout_sizing.py` `build_fleet`:id 依序號固定,所以同一個 n 永遠產生同一個
  機隊——探測可重現、可比較。
- `app/rollout_sizing.py` `_validate`:pre-flight 攔下「任何 N 都不可行」的輸入
  (VM 大於機型、requirement 的 network 與模板不符、failover/AA 落在被塌縮成單一
  bucket 的維度、預設 candidates、pin、existing_vms)。沒有它,一個建模錯誤會偽裝
  成「機器不夠」,把整個探測預算燒完才放棄。
- `app/rollout_sizing.py` `_probe_status`:探測狀態取自
  `rollout.reports[failed].solver_status`——`RolloutResult.solver_status` 依合約
  只在 request 層 INPUT_ERROR 時有值。UNKNOWN 直接中止而非繼續加碼。

## 5. 取捨與風險

- 答案是「**在你指定的 K 個 AG / R 個 rack 形狀下**的最小值」,不是全域最小;
  改變拓撲計數就是不同的問題。`target_spread` 不自動上調(K 是使用者的提問本身),
  只發 advisory——與 mockgen 的 auto-bump 慣例刻意不同。
- 下界離真值遠時,線性掃描會多跑幾輪;預算(`max_probes`/`deadline_seconds`/
  `max_baremetals`)用盡時回報 `lower_bound`/`upper_bound` 而非裸失敗。
- v1 單一機型、greenfield;混合機型的採購最佳化仍是 capacity planner 的職責。
- 重新審視訊號:探測輪數經常 >3(該補強下界)、或需要「已有 N 台再增購」。

## 6. 你應該帶走的知識

- **可行性不單調時,上行掃描比二分更正確**,不只是更保守:掃描過的每個較小值都
  是實測失敗,加上合法下界,答案就是精確最小值;二分的前提根本不成立。
- **下界必須嚴格低估,否則「最小」是謊言**——搜尋永遠不會探測下界以下。
  「分組相除再相加」是很自然的寫法,卻悄悄假設了不同規格的 VM 不能共機。
  這種錯誤沒有測試會自己浮現,所以下界的性質測試(floor ≤ 實際答案)是必需品。
- **把「任何規模都無解」的輸入擋在搜尋之前**:否則建模錯誤會偽裝成資源不足,
  使用者拿到的建議是「多買機器」,而真正的問題是規則寫錯了。

## 7. 驗證方式

- `tests/test_rollout_sizing.py::TestSizingFloors`(逐項手算 +
  `test_floor_never_exceeds_the_real_answer` 性質測試)、
  `TestSizingSearch::test_answer_is_minimal`(N-1 台必須失敗)、
  `TestSizingNonMonotonic`(暴力核對:每個更小的機隊都真的失敗)、
  `TestSizingPreflight`(六種 INPUT_ERROR)、`TestFleetGeneration`、
  `TestSizingMultiCluster`、`TestRolloutSizingEndpoint`。
- 親手驗證:UI 的 Baremetal fleet 把 **How many 留空** 再 Simulate,或
  `examples/rollout_sizing/greenfield_three_clusters.json` 直接打端點——
  三個 cluster、各自 spec 組合,回報 5 台(ag-1:2 / ag-2:2 / ag-3:1),
  以 4 台重跑會失敗。
