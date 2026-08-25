# ADR-013: Rollout 模擬——以 pinned VM 逐步重放建置順序,預演死局

- **日期**: 2026-08-18
- **作者**: Claude(與 dy850078 討論定案)
- **相關 PR / commit**: branch `claude/solver-ui-requirements-4sme83`(5f48484..97113f0)
- **影響範圍**: `app/rollout.py`(新)、`app/split_solver.py`、`app/models.py`,
  `app/solver.py`(duplicate VM id 防護)、`app/server.py`、`tests/test_rollout.py`

> 寫作對象:一位正在學習 CP-SAT 與排程系統設計的工程師。

## 1. 背景與問題

規劃時所有 cluster 聯合最佳化,實際建置卻是分批進行——「聯合可行」不保證「逐批
可行」:先建的 cluster 佔走容量、切碎拓撲後,後建的可能面對「總量夠但符合
C3/C5/C6 的擺法不存在」的死局,而發現時已無採購 lead time。需要在建置前,照
使用者指定的順序(由需求時程決定,未必是最優順序)把整條路預演一遍,指出
第幾步會斷。ADR-012 的 pinned 原語正是為此鋪路。

實作前的探查還挖出一個既有 bug:split 路徑的 `failover_rules` 欄位存在但從未
傳入 `PlacementRequest`——**C5 在 split-and-solve 上從未生效**;
`exclusive_bm_rules` 更是連欄位都沒有。模擬的忠實性依賴規則直通,必須先修。

## 2. 考慮過的方案

**方案 A(採用):pin-based 折疊。** 每步跑真實 split-and-solve(pure 為精確
退化),放置結果轉為 pinned VM 帶進下一步,並把 demand 加進宿主 used(pinned
合約要求 used 含 pinned 消耗;下一步的正規化再扣回,帳面 net-zero)。
完整 C1–C6 保真度,死局歸因即 solver 的 INFEASIBLE 診斷。

**方案 B:capacity-based 折疊(capacity planner 的 `_roll_forward` 模式)。**
只把放置量加進 used、不留 VM 身分。被排除:與 ADR-012 §2 同一個理由——
C3 計數、C5 census、C6 佔用者全部失明,模擬會對跨步互動說謊;它適合採購尺度
(planner 的正當用法),不適合擺位尺度。

**方案 C:一次聯合 solve 假裝分批。** 把所有步丟進一個模型解一次。被排除:
它回答的是「聯合可行嗎」,不是「照這個順序逐批建置可行嗎」——貪婪時序正是
要模擬的對象。

**改名範圍之爭(方案 A 內部)**:synthetic id(`split-r0-s0-0`)每步重複,
必須 namespace。最初設計是「全部 carried VM 改名 `{step}/{id}`」——被自己的
驗證打回:vm_ids 形式的 C6 規則引用的是原名,改名後 member 集合為空,
`_add_exclusive_constraints` 對空 member 直接 `continue`,**排他保護無聲蒸發**,
恰好打敗規則聯集存在的目的。定案:**只改名 synthetic**,explicit id 保持原字面
(跨步的 appliance 群組因此可用 vm_ids 表達),配 rollout 層的引用驗證補洞。

## 3. 最終決策

新模組 `app/rollout.py`(純 orchestration,solver 核心零修改):維護 rolling
baremetals 與 carried pins,第 k 步在 **rules(1..k) 聯集**下求解;成功則折疊
(pin + `candidate=[host]` + 宿主 `used += demand`),失敗則 latch
`failed_step`、後續步出 BLOCKED stub(仿 capacity planner 的 blocked-period)。
`existing_vms` 表達 brownfield 起點,永不折疊(demand 已在起始 used)。
前置修正:split 路徑規則直通(C5/C6)、solver 拒收重複 VM id、split_solver
增 `solve_split_placement_with_synthetics` 回傳 synthetic VM 物件(
`SplitDecision` 無 per-VM 歸屬,demand 無法從結果復原)。

## 4. 實作走讀

- `app/rollout.py:155`(折疊核心):`host.used_capacity += src.demand` 與
  轉 pin 成對出現。數學:第 k+1 步 solver 的 `_normalize_pinned_capacity` 會
  對同一台宿主扣回 Σ(pinned demand),所以 C2 看到的 available 恆等於
  「總量 − 第三方真實用量 − 已模擬放置量」;`Resources` 加減是逐欄位整數,
  多步零漂移。synthetic 用 splitter 回傳物件的 demand 折疊,不重算——重算
  一旦與正規化的扣減不一致,solver.py:300-308 的守門就會炸出 INPUT_ERROR。
- `app/rollout.py:92-99`(規則聯集,對應 solver Step B 的輸入):累積列表直接
  往下一步的 `SplitPlacementRequest` 帶。第 1 步的 exclusive 規則因此在第 2 步
  依然生效——pinned appliance 的固定變數流進 outsider 約束,z 被逼 0。
- `app/rollout.py:177-249`(`_validate`):蓋 solver 看不見的跨步合約——
  step name 唯一、explicit id 全域唯一、`vm_ids` 只可引用「已出場」的 explicit
  id。最後一項是關鍵:`_expand_vm_ids` 對 unknown id 是**靜默丟棄**,C6 規則
  成員集為空時整條約束消失,不會有任何錯誤——這層驗證是把無聲失效變成具名
  INPUT_ERROR。
- `app/split_solver.py:27-44`(前置重構):tuple 變體回傳 `(result,
  synthetic_vms)`,fingerprint 蓋章隨之移入;公開函數取 `[0]`,HTTP 合約不變。
  規則直通修正在 :71-80(`exclusive_bm_rules` + `failover_rules` 一起補)。

## 5. 取捨與風險

- 模擬成本隨步數線性增長,且第 k 步的模型含前面所有 VM(釘死變數 presolve
  便宜,但建模時間仍在);步數 × VM 數大時要量測。
- 規則聯集是「只增不減」:現實中 rule 若在兩批之間被撤銷,模擬表達不了——
  屆時再議步級 override 語意(v1 刻意不做)。
- explicit id 若刻意取名 `{step}/split-...` 形狀可與改名後的 synthetic 撞名,
  未特別防禦(全域唯一檢查涵蓋 explicit 對 explicit,不涵蓋這種構造攻擊);
  出現時 solver 的 duplicate VM id 防護是最後一道網。
- 重新審視訊號:需要「模擬中途改 rule / 改 config」、需要 rebalance 建議、
  或 UI 需要逐步 landable 向量時。

## 6. 你應該帶走的知識

- **時序模擬的折疊必須保留身分**:把放置折成容量(used)只對 C2 等價;凡有
  計數、census、佔用語意的約束(C3/C5/C6),折疊就要以「固定變數 + 帳面搬移」
  成對進行,缺一邊就會雙算或失明。
- **靜默丟棄是比報錯更危險的合約行為**:`_expand_vm_ids` 對 unknown id 不吭聲,
  在單次請求裡是寬容,在跨步組裝裡就成為保護蒸發的通道——上層組裝者必須
  自己驗證引用完整性。
- **修 bug 先寫紅測試**:split 路徑丟 C5/C6 的修正,先寫「修正前必紅」的
  回歸測試再動手——證明 bug 存在,也證明修正生效,兩份證據一次取得。

## 7. 驗證方式

- `tests/test_rollout.py::TestRolloutFoldForward`(帳面精確、無雙算、死局攔截)、
  `TestRolloutRulesUnion`(§2 改名之爭的回歸測試在
  `test_step1_exclusive_vm_ids_bars_step2_outsider`)、`TestRolloutBrownfield`、
  `TestRolloutDeadEnd`、`TestRolloutRenaming`、`TestRolloutEndpoint`。
- `tests/test_splitter.py::TestSplitWithFailover / TestSplitWithExclusiveBm`
  (修正前確認為紅)。
- 親手驗證:`examples/rollout/basic_two_step.json` 經
  `TestRolloutExample` in-process 跑——cluster-b 的 workers 避開 cluster-a 的
  F5 專屬機,final_baremetals 的 used 帳與兩步放置量吻合。
