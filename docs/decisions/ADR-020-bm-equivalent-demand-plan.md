# ADR-020: PM 的「等效 BM 數」需求——編譯成 rollout/sizing 請求,產出 cluster × role × AG 矩陣與 per-BM 版面

- **日期**: 2026-11-09
- **作者**: Claude (Fable)
- **相關 PR / commit**: branch `claude/trusting-gates-ys3ofp`
- **影響範圍**: `app/bm_demand.py`(新), `app/placement_report.py`(新), `app/models.py`(`FleetTemplate.id_prefix`, `SplitDecision.cluster_id/ip_type`, `RolloutStepReport.new_vms`), `app/rollout.py`, `app/rollout_sizing.py`, `app/server.py`, `app/web_static/rollout.html`, `app/web_static/js/rollout.js`, `examples/bm_demand/pm_three_clusters.json`

> 寫作對象:一位正在學習 CP-SAT 與排程系統設計的工程師。
> 寫作口吻:資深工程師帶 code review。全文使用繁體中文,程式碼與術語保留英文。

## 1. 背景與問題

採買階段的輸入長這樣:

| Cluster | 需求 | 形狀 | 政策 |
|---|---|---|---|
| A1 | 11 台 90vC/720GiB 的量 | VM 用「切三」 | 每種 node type ≥ 6 台 BM |
| B1 | 5 台 | VM 固定 90vC(= 切一) | 同上 |
| C1 | 6 台 | — | **獨佔** BM |
| 共住 | A1 與 B1 可以共機;C1 不與任何人共機 | | |

目標是回答:(a) 需要幾台機器 / 給定機器數擺不擺得下;(b) 每個 cluster 的每種
node type 在 AG-1/2/3 各落幾顆;(c) **每台 BM 上擺哪些 VM**,並輸出給尚未接上
scheduler 的 legacy 系統用的 BM-VM mapping template。

既有工具沒有任何一個吃這種輸入:splitter 吃絕對資源總量 + 離散 spec;
mockgen 把同一組 bundle 複製到每個 cluster;rollout/sizing 吃 `RolloutStep`;
沒有 cluster × role × AG 的彙整,也沒有 per-BM 版面。但引擎都在——缺的是一層
**把 PM 語言翻成引擎語言的編譯器**,和一層**把引擎輸出翻回 PM 語言的報表**。

## 2. 考慮過的方案

### 2a. 等效 BM 的語意

1. **資源總量語意**:`total_resources = N × bm_cap`,顆數交給 splitter 的
   coverage `Σ count×spec ≥ total`。最省事——完全不需要換算。但 spec 不整除
   BM 時會多切一顆,而那顆在 PM 預算的 N 台裡沒位置:BM 90vC、spec 40vC、
   N=5 → ⌈450/40⌉=12 顆、需 6 台。固定 fleet 模式直接 INFEASIBLE,sizing
   模式回答「買 6 台」,兩者都推翻了 PM「5 台」的前提。**排除。**
2. **機器數(slot)語意**(採用,D1):`k = fits_per_bm(spec, cap)`,
   `count = N × k`,`min_total_vms = max_total_vms = count`。N 台就是 N 台;
   資源少給的部分(400 vs 450 vC)以 `spec_does_not_tile_bm` advisory 回報,
   決定權留給 PM(換 spec 或加一台)。兩種語意在 spec 是 BM 整除分數
   (切一/二/三)時結果相同。

### 2b. 「不指定 spec」怎麼辦

曾設計「悲觀形狀」(挑能滿足分散下限的最大 spec)。使用者釐清 PM 一定會給
spec(一種 BM 只有切一/二/三幾個固定 spec,獨佔的 user 直接給切一),所以
**不做**。保留兩種輸入:`bm_equivalents + vm_spec`,或 `vm_count + vm_spec`
(等效 BM 反推 `⌈count/k⌉` 回報);兩者都給且不一致 → INPUT_ERROR。

### 2c. 共住政策

1. **cluster 層級的 exclusive 約束**——C6 是 VM 層級「一台一顆」,語意不同,
   要再新增一個約束。**排除**(本輪只新增 C7)。
2. **單一大 fleet 共解**——政策無從表達。**排除。**
3. **tenancy group 分 pool**(採用,D3):可互相共住的 cluster 組成一組,每組
   一個 pool、一種 BM 型號、一批自己的機器。sizing 模式**每組獨立**跑
   `size_rollout`,**採購總數 = 各組相加**(組間不共機,A 組的碎片給不了 B 組);
   固定 fleet 模式每組各自 `solve_rollout`。`Baremetal.pool`、
   `FleetTemplate.pool`、`ResourceRequirement.candidate_baremetals` 都早就存在。
   代價:組間碎片不互補,總數會比共解高——這正是政策本意,結果裡並列各組
   數字讓 PM 看得到。
   限制:非對稱關係(A 可跟 B、B 可跟 C、A 不可跟 C)分 pool 做不到,需要
   新約束;v1 每組一種 BM 型號(`FleetTemplate` 限制)。

### 2d. 編譯器放哪

1. **擴充 mockgen `NodeGroup`**——mockgen 把同 bundle 複製到每個 cluster,
   表達不了「各 cluster 需求不同」。**排除。**
2. **splitter 內加「BM 分數 coverage」**——把 splitter 綁死在同質 BM 模型。**排除。**
3. **獨立純模組 `app/bm_demand.py`**(採用,D4),輸出現有的 `RolloutStep`。
   CP-SAT 一行都不碰;唯一需要 solver 端新原語的「至少 N 台」已由 C7(ADR-019)
   提供。

### 2e. 合成 VM 的身分怎麼帶回報表

1. **解析 synthetic id** `split-r{req}-s{spec}-{k}`——rollout UI 今天就這麼做
   (`rollout.js` `SYNTH_RE`),但 `splitter.py:118-121` 明言 id 格式是實作細節、
   spec index 是過濾後的位置,demand 也拿不到。**排除。**
2. **`RolloutStepReport.new_vms: list[VM]`**(採用,D5):rollout 在 fold 時
   本來就握有這些物件,順序與 `new_assignments` 一一對應。UI 也改成優先讀它
   (synthetic 因此有 demand,capacity micro-bar 不再退化)。

### 2f. headroom 目標(意外發現,D6)

跑參考情境時 shared 組算出 **24 台**,floor 卻是 17。probe 紀錄顯示 step 1
(A1,36 顆切三)每台只放 **2** 顆:`w_headroom=8` 在懲罰 100% 使用率。留下的
30vC 碎片放不進 B1 的切二(45vC),sequential rollout 一路爬到 24。
`w_headroom=0` → 18 台(floor 17,多的 1 台是 B1「≥ 6 台」硬要求,正確)。

headroom 目標是為生產環境的**單次**排程設計的(留成長空間);PM 的 spec 是
刻意切成 BM 分數**要填滿機器**的。兩者目的相反。
**決定:`BmDemandPlanRequest.config` 預設 `SolverConfig(w_headroom=0)`**,
caller 傳完整 `SolverConfig` 即可覆寫。權重仍住在 `SolverConfig`,沒有 hardcode。
落選方案:(i) 保留預設 → 多買 33%;(ii) 把所有 cluster 放同一個 step 共解
→ 也得 18,但慢 3–5 倍,且不再模擬 sequential build。

### 2g. UI:獨立頁面 vs rollout 側欄(D7–D9)

PM 流程的輸入(目錄、需求、共住組、政策)與輸出(多個 pool 各有 fleet 與 rollout、
總台數、advisories、template 下載)和 rollout 頁「單一 `RolloutResult` + 單一
fleet」的假設差太多。

1. **rollout 側欄加 segmented「Build order | PM demand」**——`state.request /
   result / sizing` 全部要變成陣列、每個 render 函數要 group-aware。**排除。**
2. **獨立頁 `demand.html`**(採用,D7):`demand-form.js` 比照 `rollout-form.js`
   的 template-string + `data-s/i/f` 委派模式;結果區重用 `rackdiagram.js` 的
   `buildPanels/renderRackDiagram`、`.table`、`.segmented`、`.step-strip`;
   「Open in Rollout」把選中 pool 的 fleet + compiled steps 以 localStorage
   交接給 rollout 頁(rollout 頁啟動時消費一次)。兩頁各自單一職責。
3. **範本格式**(D8):範本 = `BmDemandPlanRequest` JSON 本身,加 `_name` /
   `_description`(pydantic 預設忽略未知欄位,前端 POST 前 strip)。匯出的檔案
   可直接 `curl -d @`,`examples/bm_demand/*.json` 同時就是內建範本。草稿存
   localStorage `solver-demand-draft`(compare 頁的慣例)。落選:自訂精簡格式
   ——多一層轉換、多一種 schema。
4. **表單單位 GiB**(D9),存檔時 ×1024;與 rollout-form 一致、是 PM 的語言。
5. 表單列尾即時顯示編譯預覽(`fitsPerBm` 與後端同公式,含 footprint / AG
   spread 警示),讓「11 台 → 22 顆」在按 Run 前就看得到;真值仍以回傳的
   `compiled` 為準。

### 2h. 「某幾個 role 只能住有標籤的 BM」:需求層級的 group 覆寫,與 Go scheduler 的分工

需求:同一個 Baremetal group、同一個 cluster,**某幾個 role** 只能落在有特定標籤
(例如 GPU、大儲存)的機器上。

1. **可借住語意**(標籤機器有空位時其他 role 也能用)——同一個 fleet 內的異質子集,
   `FleetTemplate` 要支援多個子 fleet、`rollout_sizing` 的遞增搜尋要變成二維
   (標籤台數 × 一般台數)。**排除(本輪)**:屬於核心改動,等真的有需求再開一輪。
2. **專屬語意**(採用):貼標籤的機器**就是一個獨立 pool**,別人不借住。這在
   模型上等於「拆 Baremetal group」,只是 group 成員要從 per-cluster 鬆成
   per-(cluster, role):`ClusterDemand.group` 覆寫該列的所屬 group,
   `TenancyGroup.clusters` 允許空(純靠覆寫成員)。編譯器 `owner_of(d) = d.group
   or owner[d.cluster_id]`;驗證「每列需求恰解析到一個 group」;報表的
   `clusters` 改為「該組實際有需求的 cluster 聯集」,同一個 cluster 因此可以
   出現在兩個 group 的矩陣列與 BM 前綴裡。實務上特殊硬體本來就不想被普通
   worker 佔掉,專屬語意通常就是對的語意。

**與 Go scheduler 的分工**(使用者的疑問:調度端已用 candidate baremetal 做
「control-plane BM 只住 control-plane VM」,規劃端這樣做會不會互相影響?):

- 規劃端把 group / 覆寫編譯成 `ResourceRequirement.candidate_baremetals`(只含該
  pool 的機器)。這和 scheduler 的 step-3 篩選是**同一個機制的兩端**:規劃端在
  **模仿**調度端的分割,讓模擬出的計畫能在真實調度時重現;身分(哪台 BM 是
  control-plane BM)永遠是 scheduler 的事,規劃端不定義它。
- `/v1/placement/solve`、`/split-and-solve` 契約不變;新增的 `min_distinct_bm_rules`
  選填、預設空,scheduler 不送就沒有 C7。PM Demand 路徑(`/bm-demand/*`)完全不碰
  生產路徑。
- 一致性風險:規劃端假設標籤 pool 專屬;若調度端允許其他 role 借住標籤機器,
  實際使用率會與計畫漂移。這交給 `reconcile.py` 與 consistency replay(ADR-006),
  不是規劃端的 bug。要嘛兩邊一致,要嘛接受計畫偏保守。
- 需要動 scheduler 的只有兩種情況:想讓生產調度也強制「至少分散 N 台」(帶
  `min_distinct_bm_rules`),或想讓 scheduler 直接吃規劃端的 BM 版面(legacy
  template)當施工圖——那是輸入資料,不是程式改動。

UI 上 group 顯示為「Baremetal group」(使用者用語);JSON 欄位 `groups` 與類別名
`TenancyGroup` 不改,避免動 API 契約。

## 3. 最終決策

`app/bm_demand.py`:`BmSpec` / `VmSpec` 目錄、`ClusterDemand`、`TenancyGroup`、
`FleetTopology`、`BmDemandPlanRequest` → `compile_bm_demands()`(純函數,
輸出每組的 `RolloutStep[]` + `FleetTemplate`,pre-flight INPUT_ERROR 與 advisory)
→ `plan_bm_demands()`(每組 `size_rollout` 或 `solve_rollout`,合併)。
`app/placement_report.py`:`placement_matrix` / `bm_layouts` / `legacy_template`
三個純函數。endpoint `POST /v1/placement/bm-demand/compile`(只編譯,可貼進
rollout 頁 JSON editor)與 `/plan`。rollout 頁新增「Placement matrix」卡片
(client-side 從 `new_vms` + `new_assignments` 彙整,隨選到的 step 累積)。

## 4. 實作走讀

**`fits_per_bm`(`app/bm_demand.py`)** = `min_d ⌊cap_d / spec_d⌋`,只算
spec_d > 0 的維度。**⌊⌋** 因為顆數是整數且每顆要完整放入:90vC 放 40vC 是
2 顆不是 2.25、更不是 3。**跨維度取 min** 因為 VM 要在 CPU / 記憶體 / 儲存 /
每種 GPU **同時**放得下,最緊的維度決定答案(90vC/720GiB 放 45vC/480GiB:
CPU 允許 2、記憶體允許 1 → k=1;取 max 會讓記憶體爆)。這其實就是 C2 在
「單一 spec 填滿一台」時的解析解。獨佔 cluster 強制 k=1(一台一顆,不論 spec)。

**`compile_bm_demands` 的 pre-flight 順序**:先驗目錄/分組/build_order 這類
結構錯誤,有錯就 return(後面的計算會建立在錯誤前提上);再逐 demand 算
k / count / N,攔 `count < min_distinct_bms`(B1 切一 ×5 vs 6 → 直接告訴 PM
「用更小的 spec 或更多機器」)。這沿用 ADR-014 的原則:**任何大小都不可行的
事,在搜尋前攔下**,否則 sizing 會燒光 probe 後怪機器不夠。

**`ag_spread_exceeds_equivalents` advisory**。寫性質測試「ΣN 台一定擺得下」
時失敗,原因是 auto C3:1 台等效 BM 切三 = 3 顆,cap ⌈3/3⌉=1 per AG,必須跨
3 個 AG → **至少 3 台**。精確的說法是:機器數語意的「每個 cluster 各拿自己的
N 台」對 **C1 / C2 / C6** 永遠合法(C2 由 `fits_per_bm` 的定義保證,C6 由 k=1
保證),但有兩個約束可以要求比 N 台更多:**C3** 在 `N < min(count, ags)` 時
(上例),**C7** 在 `N < min_distinct_bms` 時(B1:5 台切二 = 10 顆、政策 ≥ 6 台
→ 6 台,這就是參考情境 shared 組 18 vs floor 17 的那 1 台)。編譯器對兩者各發
一條 advisory——`ag_spread_exceeds_equivalents` 與 `footprint_exceeds_equivalents`
(`tests/test_bm_demand.py::TestAgSpreadAdvisory` / `::TestFootprintAdvisory`)——
性質測試則同時關掉 auto AA 與分散下限才成立(`TestSumOfMachinesAlwaysFits`)。
獨佔 cluster 的 count = N,`count < min_distinct_bms` 已是 INPUT_ERROR,不會再發
footprint advisory。

**rules 的發放**:每個 demand 一條 `MinDistinctBaremetalRule`(selector
`{cluster_id, ip_type, node_role}`),每個獨佔 cluster **一條** `ExclusiveBaremetalRule`
(selector 只有 `cluster_id`,涵蓋它所有 role),掛在該 cluster 第一次出現的
step。rollout 的 rules 是跨 step **累積**的(`rollout.py` `acc_*`),所以同一
key 只發一次,否則後面的 step 會收到重複約束(無害但浪費)。

**`FleetTemplate.id_prefix`**:各組獨立 sizing 產生的 BM 都叫 `bm-001`…,合併
報表會撞名。加一個 additive 欄位讓每組用 `"<group>/bm-"` 前綴,比事後在報表層
改名(要同步改 assignments / new_vms / pinned_to)乾淨得多。

**報表(`app/placement_report.py`)全是純函數**,輸入是 `(assignments, vms, bms)`
三個 list,不碰 solver。spec 名靠 `Resources` 精確相等反查目錄,查不到給
`"<cpu>c-<mem>g"` 退路——報表永遠不會因為一個沒命名的 spec 掛掉。
`legacy_template` 的欄位順序與 ip_type 標籤(`routable→R`, `non-routable→NR`)
可調,正式格式到了只改參數。

## 5. 取捨與風險

- **分 pool 不互補**:各組碎片不能互填,`total_baremetals` 比共解高。是政策本意。
- **單一 BM 型號/組**:混搭留後續(候選路線:mockgen 多 profile 彈性 sizing)。
- **sequential 預設**:每個 cluster 一個 step(`step` 欄位可合併成一步共解)。
  sequential 是保守答案(≥ 共解),符合「一定擺得下」;要最佳打包就把 `step`
  設成同一個名字。
- **deadline 是每組各自的**:`deadline_seconds` 不在組間分攤,總耗時上限是
  組數 × deadline。
- **C7 × C3** 的交互與 sizing 非單調性見 ADR-019 §5。
- **legacy template 格式未定**:formatter 可調,但真正的格式確定後可能要加欄位
  (例如 hostname),屆時只動 `placement_report.py`。

## 6. 你應該帶走的知識

1. **先問「哪個數字是硬的」再選語意。** 採購台數是 PM 簽的,資源量是估的;
   資源總量語意會為了湊量多買一台,機器數語意把差額變成 advisory。
2. **目標函數有情境。** headroom 在生產排程是美德,在「刻意填滿」的採購模擬
   是 33% 的多買。換用途時要重新審視每一個 objective 權重,不是只看約束。
3. **「一定擺得下」要說清楚對哪些約束成立。** ΣN 的保證來自 C1/C2/C6 的
   trivial 解;任何「至少要跨幾個 bucket / 幾台機器」的下界(C3、C7)都可能把
   需求推到 N 以上,而且應該在求解前就告訴需求方。
4. **分解(decomposition)是免費的約束。** 「組間不共機」用分 pool 各自求解
   就精確成立,完全不用進 CP-SAT——能用資料切開的,不要用約束綁住。

## 7. 驗證方式

- `tests/test_bm_demand.py::TestDemandGroupOverride`:A1 worker 在 shared、A1
  gpu-worker 覆寫到 gpu-nodes → 兩組、矩陣兩列 A1、BM 前綴分開、GPU pool 只有
  gpu-worker;未知 group → INPUT_ERROR;純覆寫成員的 group 合法;獨佔 cluster
  跨兩組各一條 C6。範例 `examples/bm_demand/tagged_gpu_roles.json`。
- `tests/test_bm_demand.py`:`fits_per_bm`(整除、向下取整、跨維度 min、GPU、
  不合)、編譯(slot 語意、`vm_count` 反推、不一致、`count < N`、不整除 advisory、
  獨佔、分組、build_order)、端到端三 cluster 情境(shared 18 / c1-own 6 /
  total 24、矩陣每列 ≥6 台、版面容量不超、獨佔 pool 無外人、template 形狀、
  `fleet_size = required − 1` 失敗)、ΣN 性質測試、AG advisory、endpoint。
- `tests/test_placement_report.py`:矩陣、版面、template、spec 名退路。
- `tests/test_rollout.py::TestRolloutNewVms`:`new_vms` 與 `new_assignments` 對齊。
- UI(無 JS 測試框架,以 scratchpad 的 Playwright 腳本驗證):載入
  `pm_three_clusters` 範例 → Save 的 JSON 與範例語意等價 → Run → 24 台、矩陣 4 列、
  版面 24 列、template 24 個 key → 切換 pool → Open in Rollout 後 rollout 頁矩陣
  卡片出現 → 重新整理後草稿還原。
- 親手驗證:`make dev` 後
  `curl -s -X POST localhost:50051/v1/placement/bm-demand/plan -H 'content-type: application/json' -d @examples/bm_demand/pm_three_clusters.json | jq '.total_baremetals, .placement_matrix'`;
  UI:rollout 頁載入 `/compile` 的輸出,看「Placement matrix」卡片。
