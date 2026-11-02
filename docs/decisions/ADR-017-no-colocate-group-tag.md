# ADR-017: NodeGroup 加 `no_colocate_group` 標籤——讓 mock generator 與 UI 表單產出 ADR-016 的聯集 C4 rule

- **日期**: 2026-09-29
- **作者**: Claude (Fable)
- **相關 PR / commit**: branch `claude/busy-pasteur-vmxewn`
- **影響範圍**: `app/mockgen.py`, `app/web_static/js/mockform.js`,
  `app/web_static/js/rollout-form.js`, `app/web_static/styles.css`,
  `examples/mock/ctrl_plane_learner_apart.json`, `docs/mock-request-generator.md`,
  `tests/test_mockgen.py`, `CLAUDE.md`

> 寫作對象:一位正在學習 CP-SAT 與排程系統設計的工程師。

## 1. 背景與問題

ADR-016 把「control-plane 與 control-plane-learner 不可同 BM」定為一條 explicit
C4 rule:selector 的 `node_role` 是兩個 role 的 list、`max_per_bm=1`。solver 與
UI 的 Request JSON 編輯器都能吃這條 rule(實測 `examples/control_plane_learner_separate.json`
→ 6 VMs 落在 6 台不同 BM)。

但 UI 的兩個**表單**做不到。mock generator(`app/mockgen.py` + `mockform.js`)與
rollout builder(`rollout-form.js`)的資料模型都是「一個 node group 一條 rule」,
rule 的 selector 固定是該 group 自己的 `(cluster, ip_type, role)`。使用者在兩個
group 各填 `max/BM=1`,得到的是兩條 per-role rule——這正是 ADR-016 §5 描述的
情境 2:`w_consolidation` 會主動把 learner 疊到 master 的 BM 上(實測 6 VMs 被
壓到 3 台)。表單沒有任何欄位能說「這兩個 group 的 VM 要一起算 cap」。

更隱蔽的是 mockgen 的 **fleet sizing** 也是 per-role 算:`_headcount_bounds`
對每個 role 算 `⌈n/cap⌉`、取 max,3 master + 3 learner cap 1 得到 3 台。即使
使用者事後手改 JSON 加上聯集 rule,fleet 也只有 3 台,直接 INFEASIBLE。

## 2. 考慮過的方案

1. **兩個 group 填同一個 role 名稱**(scheduler 端合併身分)——ADR-016 §1 已排除:
   auto C3 的分組 key 含 role,合併會把 AG cap 從 ⌈3/3⌉=1 稀釋成 ⌈6/3⌉=2,
   HA 退化且沒有 advisory。**排除。**
2. **表單下方加一塊「extra rules (JSON)」直接 append 到 `PlacementRequest`**——
   實作最快、最通用。但等於把 ADR-016 的知識推回給使用者手寫 selector,而且
   mockgen 的 sizing / ground truth 看不到這條 rule,fleet 仍會少算。**排除:
   解了表達問題,沒解 sizing 問題。**
3. **在 `NodeGroup` 加一個政策標籤 `no_colocate_group`**(採用)——同 scope、同標籤
   的 group 合成一條 rule,selector 的 `node_role` 是成員 role 的 list。表單維持
   group 級思維(使用者只是幫兩個 row 貼同一個標籤),mockgen 內部則以「cap unit」
   為單位算 sizing、ground truth、rule。缺點:只能表達「聯集共用一個 cap」,
   不能表達成員各自不同的 cap——但那本來就不是一條 C4 rule 能表達的東西。
   - 變形:cap 不等時取 min。**排除**——使用者填 2 卻得到 1 是 silent fix,違反
     本專案「契約錯誤回 400、不默默修正」的慣例。

## 3. 最終決策

**身分歸 role,政策歸標籤。** `NodeGroup.no_colocate_group: str | None` 是與
`role` 正交的政策欄位;同 scope、同標籤的 group 合成一條
`MaxPerBaremetalRule`,`selector.node_role = sorted(成員 roles)`、cap 為成員
共同的 `max_per_bm`,`group_id = maxbm/{cid}/{ip|*}/{role1+role2}`(對齊
solver 的 fallback id 慣例)。成員 ip_type 不同時 selector 省略 `ip_type`。

四條驗證,違反一律 400:有標籤必須有 `max_per_bm`;同標籤 cap 必須相等;同標籤
scope 必須一致;一個 role 最多屬於一個標籤,且 role 一旦被標,同 scope 內該 role 的
**所有** group 都要帶同一標籤(selector 以 role 選人,漏標的兄弟 group 會被默默
掃進聯集)。單成員標籤退化成今天的字串 selector,未標籤的請求輸出 bit 級相同。

## 4. 實作走讀

- `app/mockgen.py:80` `_CapUnit` —— 這次的核心抽象。一個 unit = 一個 cap 與共用它的
  role 集合,同時是「產 rule 的單位」、「sizing 的單位」、「ground truth 計數的單位」。
  未標籤 group 是單 role unit(key `(scope, role, ip)`),標籤 group 合成多 role unit。
  把三個消費端統一到一個抽象,是為了避免三處各自解讀 rule 語意而漂移。
- `app/mockgen.py:320` `_build_cap_units` —— 兩趟:先驗證(400),再建 unit。注意
  「role ↔ 標籤是函數」的檢查是 per `(scope, role)`,因為 shared 與 cluster scope
  的 selector 選的是不同 `cluster_id`,不會互相掃到。
- `app/mockgen.py:706` head_gap 迴圈 —— **最容易寫錯的地方**。多 role unit 的
  bound 不是直接 `⌈total/cap⌉`,而是對**該 elastic profile 服務的子集**
  `sub = roles ∩ served` 算。反例:master、learner 各一個 pool,若對 master pool
  套聯集 bound ⌈10/1⌉=10,它會被撐到 10 台,learner pool 再看到 `existing=10`
  反而拿到 0 台 → 走 escalation。單 role unit 下 `sub = roles`,退化成原本的程式。
  測試 `test_no_colocate_headcount_floor_split_pools` 專門守這條。
- `app/mockgen.py:867` `_place` —— 原本 `group_key` 是手工拼的
  `f"{cluster}/{ip}/{role}"`,若只改 `_cap_for` 不改這裡,ground truth 的計數器仍是
  per-role,聯集 cap 形同未執行(solver 驗證還是會過,但 `unplaced_ground_truth`
  的形狀會錯)。現在 key 來自 `unit.key`,master 與 learner 共用同一個 per-BM 計數器。
  AG spread 仍維持 per `(cluster, ip, role)`,對應 auto-C3 的 per-role 語意。
- `app/web_static/js/rollout-form.js:563-579` —— 前端鏡射同一套驗證(`throw Error`,
  表單本來就會顯示),並在 `loadIntoForm`(`:748`)讓 list selector 能配對回 group:
  請求裡沒有標籤名,所以用 `roles.join("+")` 代替,讓 ADR-016 形式的 JSON 載回來
  時留在 form mode。

## 5. 取捨與風險

- 標籤只表達「同一個 cap」。若有「master 之間 cap 1、learner 之間 cap 2、跨 role
  cap 1」這類需求,需要多條 rule,超出一個標籤欄位的表達力——那時應該回到方案 2
  的 extra rules,而不是給標籤加參數。
- head_gap 的子集 bound 在「pool 部分重疊」(某 profile 服務 master 與 worker,
  另一個服務 learner)下仍是**下界**而非精確值;escalation 迴圈(每輪 +1 台)負責
  補差,不會 overshoot。
- `_escalation_targets` 靠 group_id 最後一段以 `+` 切 role;role 字元集
  `^[\w.-]+$` 不含 `+`,不會誤切。若日後 group_id 格式改動,這裡要同步。
- 前端沒有測試;兩個表單的驗證邏輯是後端的鏡射,漂移時後端 400 是最後防線。

## 6. 你應該帶走的知識

- **一個語意、一個抽象**:rule 的語意(誰跟誰共用 cap)被三個地方消費——產 rule、
  算 fleet、算 ground truth。與其在三處各自「記得」聯集語意,不如造一個 `_CapUnit`
  讓三處都只看 unit。這是 ADR-016 「身分與政策分層」在 generator 內部的延伸。
- **下界要對「能吃到這些 VM 的資源」算**:headcount bound 是 `⌈n/cap⌉`,但 n 只能
  數該 pool 服務得到的 VM。把全域的 bound 套到局部的 pool,會把下界變成錯誤的
  上界,逼另一個 pool 進 escalation。
- **驗證比 silent fix 便宜**:cap 取 min 看起來友善,但使用者得不到任何訊號。
  400 帶清楚的 detail,是把問題丟回填表的人,而不是藏進結果裡。

## 7. 驗證方式

- `tests/test_mockgen.py` 新增 15 個測試(`test_no_colocate_*`、
  `test_escalation_targets_split_union_id`):合成單條 list rule、per-cluster 展開、
  shared scope 單條、ip 不同省略、單成員退化、sizing 加總(單 pool 10 台)、
  sizing 子集(雙 pool 5+5)、ground truth 不共站、五種 400。全套 443 綠。
- UI:`make dev` → `/ui` 載入 preset `mock/ctrl_plane_learner_apart.json` →
  Generate & Run → 6 VMs 在 6 台不同 BM,產出的 JSON 只有一條
  `max_per_bm_rules`(2-role list)。清掉一列的標籤 → 兩條 per-role rule、3 台
  (情境 2)。
- `curl /api/mock/generate` 送 cap 不等的請求 → 400,detail 說明原因。
