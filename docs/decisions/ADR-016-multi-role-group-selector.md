# ADR-016: GroupSelector 的 node_role 支援多值——用 rule 表達 control-plane / learner 的共站政策

- **日期**: 2026-09-18
- **作者**: Claude (Fable)
- **相關 PR / commit**: branch `claude/relaxed-pascal-scu79m`
- **影響範圍**: `app/models.py`, `app/solver.py`, `app/capacity_planner.py`, `examples/control_plane_learner_separate.json`, `CLAUDE.md`

> 寫作對象:一位正在學習 CP-SAT 與排程系統設計的工程師。

## 1. 背景與問題

上游 DB 將把 master 與 learner 都標成 `control-plane`,只靠 `is_learner`
欄位區分;同時每個 cluster 需要二選一的政策:(1) learner **不可**與
master 同 BM;(2) learner **可以**與 master 同 BM。兩種政策下每種 role
仍各自 1-per-BM。

第一版想法是把政策編碼進 scheduler 送出的 role 字串:情境 1 兩者都叫
`control-plane`、情境 2 叫 `control-plane` + `control-plane-learner`。
細看 solver 對 `node_role` 的依賴後發現兩個缺陷,而且都出現在情境 1:

- **C3 的 cap 被稀釋**。auto C3 與 auto C4 共用分組 key
  `(cluster_id, ip_type, node_role)`(`app/solver.py:604`、`:787`)。
  合併 role 等於把 C3 群也合併:3 master + 3 learner 分到 3 個 AG,
  每 AG cap 從 ⌈3/3⌉=1 變成 ⌈6/3⌉=2,兩個 master 可以合法落在同一個 AG。
  `max_per_bm=1` 只保證不同 BM,不保證不同 AG——這是 HA 退化,且不會有
  任何 advisory 提醒。
- **C5 寫不出來**。`FailoverRule.primary/backup` 用 role selector 選人
  (`app/models.py:358`);role 同名時 primary 集合 = backup 集合。

## 2. 考慮過的方案

1. **政策編碼進 role 名稱**(scheduler 端,solver 零改動)——缺陷如上;
   另外 group_id / diagnostics 看不出誰是 learner,pinned VM(ADR-012)的
   分組 key 也會隨政策漂移。**排除:破壞 solver 以 role 為身分的前提。**
2. **solver 加 `is_learner` 欄位 + config 開關**——solver 對特定 role 有
   分支邏輯,正是 ADR-010 刻意拆掉的耦合。**排除。**
3. **身分固定、政策用 rule,selector 只用 `vm_ids`**——今天就能用,
   但 ID 是 per-request 的,遇到 splitter 的 synthetic VM 只能用 selector
   (`app/models.py:719`)。**保留為 fallback,不作主線。**
4. **身分固定、政策用 rule,`GroupSelector.node_role` 接受 list**(採用)
   ——字串 = 精確比對(不變)、list = role ∈ 集合、None = wildcard。
   JSON 契約是嚴格超集。另兩個變形也被排除:另開 `node_roles` 欄位
   (一個概念兩個欄位、要互斥驗證);glob/前綴比對(role 目錄是開放字串,
   前綴會撈到無關 role)。

## 3. 最終決策

**身分歸 scheduler,政策歸 rule。** scheduler 永遠把 master 送成
`control-plane`、learner 送成 `control-plane-learner`,與政策無關。
情境 2 不需任何 rule(auto C3 + per-role auto C4)。情境 1 加一條 explicit
C4 rule,selector 用 list 選兩種 role 的**聯集**、`max_per_bm=1`:
對每台 BM,`Σ assign[vm ∈ cp ∪ learner, bm] ≤ 1`,一次涵蓋 master 之間、
learner 之間、master 與 learner 之間的不同住。

## 4. 實作走讀

- `app/models.py:293-313` — `node_role: str | list[str] | None` 加
  `field_validator`:字串走既有 `validate_role`;list 必須非空、逐元素驗格式、
  去重保序。Pydantic v2 的 union 是 smart mode:JSON 字串只會落到 `str`、
  陣列只會落到 `list[str]`,沒有歧義。
- `app/models.py:316-340` — `role_set()` 把 str/list 統一成 `frozenset`,
  `matches_attrs()` 接純量三元組做比對,`matches(vm)` 委派給它。這是
  「str-vs-list」唯一的判斷點——呼叫端不再碰 `node_role` 的型別。
- `app/solver.py:816` — Step B 的 fallback group id 把 list 用 `+` 串起
  (`selector/A/*/control-plane+control-plane-learner`);role 字元集
  `^[\w.-]+$` 不含 `+`,不會撞名。`_expand_vm_ids` 本來就走 `sel.matches`,
  不用改。
- `app/capacity_planner.py:575` — `_selector_matches_req` 改為委派
  `matches_attrs`,消掉原本複製一份的比對邏輯,兩條路徑不可能再漂移。
- Step C 完全沒動。情境 1 能成立靠的是既有的一個不對稱:auto C4 會跳過
  已被 explicit C4 rule 覆蓋的 VM(`app/solver.py:777`),所以聯集 rule
  取代兩條 per-role auto C4(cap 1 的聯集本就包含 per-role cap 1);
  而 auto C3 用自己的 covered 集合(`app/solver.py:592`),per-role 的
  AG 分散原封不動。

## 5. 取捨與風險

- 情境 2 下 `w_consolidation` 會**主動**把 learner 疊到 master 的 BM 上
  (端到端跑出 4 台 BM);使用者確認「允許即可」。若日後想「允許但不偏好」,
  需要新的 soft term,不是改 rule。
- 同 BM 共站時 BM 故障會同時帶走一組 master + learner;C5 的 fault_domain
  是 room,room 層級的 N-1 仍成立,但 BM 層級沒有保護——這是政策選擇。
- `+` 串接的 group id 只在 caller 沒給 `group_id` 時出現;UI 與 mockgen
  仍只送字串,前端不需要改。
- 訊號:若出現「role ∈ 集合」之外的需求(例如 role 的**交集**或排除),
  代表 selector 該升級成小型查詢語言,而不是再加特例。

## 6. 你應該帶走的知識

- **身分與政策分層**:VM 欄位描述「它是什麼」,rule 描述「它該怎麼放」。
  把政策塞進身分欄位,會讓所有依賴身分的機制(C3、C5、pin)一起失真。
- auto-gen 的 **covered 集合是 per-constraint 的**:explicit C4 只壓掉
  auto C4,不壓 auto C3。這個不對稱是刻意的,也是本次能不動 Step C 的原因。
- 擴充契約時選 **嚴格超集**(`str | list[str]`)而非新欄位:舊 payload
  bit 級相容,呼叫端只需一個正規化 helper。

## 7. 驗證方式

- `tests/test_solver.py::TestMultiRoleSelector`(8 個測試:兩種形式的
  比對/驗證/去重、fallback id、情境 1 端到端斷言 6 台不同 BM 且每種 role
  跨 3 AG、情境 2 端到端、C5 多值 selector)。
- `tests/test_capacity_planner.py::TestSelectorListForm`。
- `make cli INPUT=examples/control_plane_learner_separate.json` —— 情境 1;
  拿掉 `max_per_bm_rules`、開 `auto_generate_max_per_bm` 即情境 2。
- 回歸:`make cli INPUT=examples/master_learner_2room.json`,全套 428 綠。
