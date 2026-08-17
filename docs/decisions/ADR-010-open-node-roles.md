# ADR-010: node_role 從封閉 enum 開放為自由字串

- **日期**: 2026-08-16
- **作者**: Claude (Fable)
- **相關 PR / commit**: branch `claude/topology-infeasible-analysis-ytig4x`
- **影響範圍**: `app/models.py`, `app/solver.py`, `app/mockgen.py`, `app/splitter.py`, UI (`mockform.js`)

> 寫作對象:一位正在學習 CP-SAT 與排程系統設計的工程師。

## 1. 背景與問題

`NodeRole` 是寫死六值的 enum(master/learner/worker/infra/l4lb-storage/
bastion)。新 role(ceph-mon、lb、f5、lvslb…)會被 Pydantic 直接 422。
關鍵觀察:**solver 本體從不認識 NodeRole**——它只把 `node_role` 當
auto-rule 分組 key `(cluster_id, ip_type, node_role)` 裡的不透明字串。
耦合只存在於 models 的型別宣告、mockgen 的驗證器與 UI 的下拉清單,
是「型別武斷收窄」而非「邏輯依賴」。

## 2. 考慮過的方案

1. **enum 持續加值**——每個新 role 一次 PR + 部署,而 role 目錄的真正
   擁有者是 Go scheduler / 營運方,不是 solver。**排除:把別人的目錄
   複製一份在自己這裡,注定漂移。**
2. **enum + 動態註冊 API**——多一個端點與持久狀態,換到的只是把 typo
   從 warning 變 error。**排除:成本/收益不成比例。**
3. **開放為 `str` + 格式驗證 + advisory**(採用)——`^[\w.-]+$` 且非空;
   已知 role 清單降級為建議(UI datalist 提示、mockgen diagnostics 記
   `unknown_roles`),不阻擋。

## 3. 最終決策

`node_role` 全面改為 `str`(VM、GroupSelector、capacity planner 各
模型),`NodeRole` enum 保留作為「已知 role」目錄供 baseline/建議清單
引用。JSON 契約是嚴格超集——enum 本來就序列化為字串,Go scheduler
既有 payload 全部原樣可解,狀態字串不變。

## 4. 實作走讀

- `app/models.py`: 欄位型別 `NodeRole` → `str`,`VM.node_role` 加
  pattern 驗證。GroupSelector 的比對(`vm.node_role != self.node_role`)
  從 enum 相等變純字串相等——語意不變,str-mixin enum 的相等本來就是
  字串相等。
- 全 app 掃除 `.value`(solver 的分組 key、mockgen 的生成路徑):
  這批呼叫點正是「solver 把 role 當不透明字串」的證據——改動只是把
  `vm.node_role.value` 縮成 `vm.node_role`,分組行為 bit 級不變。
  對應 Step B(auto-rule 分組)與 selector 展開。
- `app/mockgen.py`: `_valid_role` 系列驗證器放寬為格式檢查;
  `_ROLE_BASELINE` 查無此 role 時沿用既有 worker fallback;失敗回報
  改為 diagnostics `unknown_roles` advisory。failover 的 master→learner
  慣例**保留**——那是領域語意,不是型別限制。
- UI `mockform.js`: role `<select>` → `<input list>` + 共用
  `<datalist>`,已知 role 是建議、任意字串可輸入。

## 5. 取捨與風險

- Typo 不再被 422 擋下(`mastr` 會被當成新 role)。緩解:UI datalist
  降低打錯率、mockgen diagnostics 標示 unknown_roles;auto-AA 分組
  key 打錯的後果是「自成一群」,會在 spread advisory / 結果中可見。
- 若 Go 端也在做 role 驗證,兩邊都開放後「目錄的唯一權威」正式歸
  Go/營運方——這是本來就該在的位置。

## 6. 你應該帶走的知識

- 開放性設計的判準:先問「這個值系統**邏輯上**依賴誰」。當程式只拿它
  做分組 key/顯示,封閉 enum 是武斷的耦合;當程式對特定值有分支邏輯
  (如 failover 的 master/learner),那部分才需要具名慣例。
- 驗證的層級可以拆:**格式**(硬性,擋垃圾)與**目錄**(advisory,
  擋 typo)分開,比全有全無的 enum 更符合演進中的系統。

## 7. 驗證方式

- `tests/test_mockgen.py::test_node_groups_open_roles_accepted_with_advisory`
  (role="ceph-mon" 端到端 verified 且 diagnostics 帶 unknown_roles)
- `tests/test_mockgen.py::test_node_groups_bad_role_format_rejected`、
  `test_max_per_bm_by_role_rejects_bad_format`(格式仍是硬 gate)
- 既有六 role 全數回歸(全套 280 綠)。格式驗證:`app/models.py:141`
  `validate_role()`;advisory:mockgen diagnostics `unknown_roles`;
  UI:`mockform.js` role 欄位 input+datalist。
