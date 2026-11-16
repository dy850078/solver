# ADR-021: VM labels 成為可設定的 auto-grouping 維度——`dedicated=AAA` 自成一群分散,而不是編進 role 名

- **日期**: 2026-11-16
- **作者**: Claude (Fable)
- **相關 PR / commit**: branch `claude/label-auto-grouping`
- **影響範圍**: `app/models.py`, `app/solver.py`, `app/splitter.py`, `app/sizing_floors.py`, `app/capacity_planner.py`, `app/mockgen.py`, `app/bm_demand.py`, `app/placement_report.py`, `app/web_static/js/{util,demand-form,demand,rollout,filter,rackdiagram}.js`, `examples/labels_dedicated_spread.json`, `examples/bm_demand/dedicated_labels.json`, `CLAUDE.md`, `README.md`, `docs/constraints.md`

> 寫作對象:一位正在學習 CP-SAT 與排程系統設計的工程師。
> 寫作口吻:資深工程師帶 code review。全文使用繁體中文,程式碼與術語保留英文。

## 1. 背景與問題

某些 VM 帶 k8s node label(`dedicated=AAA`、`dedicated=BBB`),這些 VM 要**自己一群**
做 AG 分散,不能和同 cluster / role / ip_type 的一般 worker 混成一群。今天 auto C3 / C4
的分組 key 寫死是 `(cluster_id, ip_type, node_role)`(`solver.py` 兩處 + `sizing_floors.py`
一處,三份複本),label 無處可放。後果是具體的:9 顆 worker 一群、3 AG,cap = ⌈9/3⌉ = 3,
三顆 AAA 可以合法擠在同一個 AG——`examples/labels_dedicated_spread.json` 關掉
`auto_group_labels` 就能重現(每個 label 值整組落在同一 AG)。

## 2. 考慮過的方案

1. **把 label 編進 role 名**(scheduler 送 `worker-AAA`,solver 零改動)——ADR-016 §1
   已經排除過這條路的鏡像版本:role 是**身分**,被 C3 分組、C5 selector、pinned 分組、
   報表與 hostname 全部依賴;把政策揉進去,`node_role="worker"` 的 explicit rule 和
   C5 selector 會漏掉 `worker-AAA`,legacy pipeline 看到的 role 也變了。**排除:違反
   身分/政策分層。**
2. **只加 `VM.labels`,分組 key 固定納入全部 label**——不用新 config。但 `env=prod`
   這種純標記也會切群,而且 key 會隨 scheduler 多送一個 label 就漂移,無法「預設不變」。
   **排除:沒有開關等於沒有相容性。**
3. **`VM.labels` + `SolverConfig.auto_group_labels` 指定參與分組的 key**(採用):
   key = 三元組 + `((k, labels.get(k, "")) for k in auto_group_labels)`。預設 `[]` →
   bit 級相同;以 `(key, value)` 對分群,`dedicated=AAA` 與 `other=AAA` 不撞群。
4. selector 對「缺 key」的語意,兩個候選:(A) 缺 key 一律不匹配;(B) 缺 key ≡ `""`,
   與分組 key 同一條規則。(A) 無法表達「沒有這個 label 的 VM」,PM Demand 路徑的
   C7 會因此失真(見 §4)。**採用 (B)**(使用者定案)。
5. group_id 編碼:只編 value(`.../AAA`)會讓不同 key 同值撞名;hash 不可讀。
   **採用 `/{k}={v}[,{k2}={v2}]` 後綴**,無 label 參與時 id 完全不變。

## 3. 最終決策

label 是身分(`VM.labels`、`ResourceRequirement.labels`,splitter 合成 VM 繼承),
`auto_group_labels` 是政策。分組 key 收斂成單一函數 `SolverConfig.auto_group_key`,
auto C3、auto C4、`headcount_floor` 共用;selector 加 `labels`,比對規則與分組相同
(`vm.labels.get(k, "") == v`)。不新增約束、不改 Step C/D、不改 objective、status 不變;
`/v1/placement/solve` 契約為嚴格超集。PM Demand 頁的需求列加 `labels`,Policy 區勾選
哪些 key 參與分散分組,每列 C7 rule 以 label 釘死自己的子群。

## 4. 實作走讀

- `app/models.py:661-695` `auto_group_key` / `auto_group_id`——**唯一定義**。放在
  `SolverConfig` 而非 solver,因為 `sizing_floors` 只 import models;若放 solver 會反向
  依賴。`auto_group_id` 前四段固定位置(role 字元集不含 `/`),後綴依 `auto_group_labels`
  順序 → 決定性;`auto/` 前綴**不可改**:`solver.py:1072` 的動態 ceil(split 路徑合成 VM
  的 Σ active 上界)靠 `startswith("auto/")` 判斷。
- `app/solver.py:619-631`、`:809-817`(Step B)——兩處 auto-gen 改呼叫上面兩個函數,
  其餘邏輯(covered 集合、≥2 才產 rule、跳過空 cluster/ip)原封不動。數學上只是把
  Σ 的索引集合切細:原本一條 `Σ_{v∈G} assign[v, b] ≤ ⌈|G|/|AG|⌉`,現在 G 依 label 值
  分成 G₁…Gₙ 各自一條,cap 變小(⌈3/3⌉=1 而非 ⌈9/3⌉=3)。
- `app/models.py:391-412` `matches_attrs(..., labels)`——`labels` 是**必填位置參數**,
  不給預設 None。原因:三個呼叫端(`matches`、`capacity_planner.py:577`、
  `sizing_floors.py:215`)若漏傳會靜默永不匹配,在 capacity planner 會漏掉 cap 偵測;
  必填讓漏改在第一次呼叫就炸。`labels={}` 正規化成 None(`:347` 的 validator),
  `is_empty()` (`:375`) 納入 labels,否則「只用 label 選人」的 rule 會被當成空 selector 拒收。
- `app/bm_demand.py:292-311` `_selector(d, label_keys)`——每列的 C7 selector 把**請求內
  所有** demand 用到的 key 都釘死,缺的釘 `""`。這就是 (B) 語意的用處:無 label 那列的
  selector `{"dedicated": ""}` 排除 AAA/BBB 的 VM,否則它的「≥ N 台」下界會被 AAA
  已佔的機器灌水(selector 萬用 → Σ host 數到別人的機器)。只釘 `auto_group_labels`
  內的 key 不夠:使用者不勾 `tier` 時,`tier=gold` / `tier=silver` 兩列的 C7 仍會互灌。
  沒有任何 label 時 `labels=None`,輸出與 ADR-020 bit 級相同。`:360-372` 對「有用到但
  沒勾」的 key 發 `label_not_in_auto_group` advisory。
- `app/models.py:231-252` label 格式:key `^([\w.-]+/)?[\w.-]+$`、value `^[\w.-]*$`
  (k8s 子集),排除 `=`、`,`、`+`、空白,所以 UI 的 `k=v, k2=v2` 解析、group_id 後綴、
  selector id 的 `+` join 都不會歧義。`app/mockgen.py:1049` 的 group_id 解析改讀第 3 段
  (原本 `rsplit("/")` 取尾段,會把 `dedicated=AAA` 當成 role)。

## 5. 取捨與風險

- **fingerprint 全變**:`config_fingerprint` dump 整個 config,新欄位讓所有舊 plan 的
  reconcile 報一次 config drift。與 ADR-003 / ADR-015 的先例相同,屬設計使然。
- **pinned 的上線順序**:若 scheduler 先在新 VM 送 labels、inventory 的 pinned VM 還沒帶,
  帶 label 的新 VM 自成一群,grandfather(`max(cap, pinned)`)對它失效——過度寬鬆、
  不會 INFEASIBLE。契約上要求兩邊同步上線。
- **缺 key ≡ ""** 讓「有 key 但空值」與「沒 key」不可區分;k8s 本身允許空值 label,
  分組與 selector 一致採用,代價可接受。
- **已知缺口**:`DemandEntry`(多期 capacity planner)無 labels;`rollout-form.js` 匯入器
  以 `role|ip|spec` 摺疊,匯入帶 label 的請求會併群;mockgen 不產生 labels;
  legacy pipeline 文件四欄不變,看不到 dedicated 分群(hostname 帶值是唯一線索)。
- 訊號:若出現「label 要跨 role 合併」或「label 的否定/集合比對」需求,代表 selector
  該升級成查詢語言,而不是再加欄位(與 ADR-016 §5 同一條警戒線)。

## 6. 你應該帶走的知識

1. **身分與政策分層,第二次。** ADR-016 把政策從 role 名拿出來放進 rule;這次把
   「哪些身分維度參與分組」放進 config。身分欄位可以一直加,分組 key 只認被點名的。
2. **一個語意一個函數。** 三處各自拼 tuple 的 key 在加維度時必然漂移;收斂到
   `auto_group_key` 後,fleet 下界與 solver 分組不可能再對「誰共用 cap」持不同意見。
3. **下界約束的 selector 要「釘死補集」。** C7 是 `Σ host ≥ N`,selector 多選到別人的
   VM 只會讓它更容易滿足——這種失真不會 INFEASIBLE、也不會有 advisory,只能在
   語意設計時就避免(缺 key ≡ "" 讓補集可表達)。

## 7. 驗證方式

- `tests/test_solver.py::TestLabelAutoGrouping`(三群各自跨 AG 與 BM、預設不變、
  未勾 key 不切、`(key,value)` 不撞、helper 函數、格式驗證、explicit label rule 只覆蓋
  自己那群)、`::TestLabelSelector`(缺 key ≡ ""、多 key、`{}` 萬用、labels-only rule、
  fallback id)。
- `tests/test_splitter.py::TestSplitWithLabels`、`tests/test_rollout_sizing.py::TestLabelHeadcountFloor`
  (floor 3 vs 6)、`tests/test_bm_demand.py::TestLabels`、`tests/test_placement_report.py::TestLabelsInReports`、
  `tests/test_capacity_planner.py::test_selector_matches_req_labels`、
  `tests/test_mockgen.py::test_escalation_targets_read_role_as_segment_three`。全套 590 綠。
- 親手驗證:`make cli INPUT=examples/labels_dedicated_spread.json OUTPUT=output/verify-labels.json`
  → 三條 `auto/A/routable/worker/dedicated=…` rule,每群 AG 1/1/1、每 BM 1 顆;
  把 `auto_group_labels` 清空再跑,每個 label 值整組落同一 AG。
  `curl -d @examples/bm_demand/dedicated_labels.json …/bm-demand/plan` → 矩陣三列、
  hostname `worker-R-AAA-01`。UI(scratchpad Playwright):載入範例 → Policy 已勾
  `dedicated` → 新 key 不勾 → Save 帶 labels 與 `auto_group_labels` → 重複列被擋 →
  Run 後矩陣有 Labels 欄且三列各跨 3 AG → 重新整理草稿還原。
