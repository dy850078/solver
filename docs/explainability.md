# Explainability 設計文件

## 背景

Solver 目前在**失敗時**提供豐富的診斷資訊（constraint layer check、infeasible anti-affinity 偵測、無 eligible BM 的 VM 列表），但在**成功時**幾乎不回傳任何解釋 — 只有 assignments 列表和 OPTIMAL/FEASIBLE 狀態。Go scheduler 無法判斷放置品質或決策原因。

本文件評估 CP-SAT assumptions 的適用性，說明為何選擇替代方案，並設計 post-solve 可解釋性系統。

---

## Part 1: CP-SAT Assumptions — 評估

### 什麼是 Assumptions？

CP-SAT 提供 `AddAssumption(literal)` API，可以為約束標記布林變數。求解後若結果為 INFEASIBLE，`SufficientAssumptionsForInfeasibility()` 會回傳導致衝突的 assumption 子集 — 即「最小不可滿足子集」(MUS) 的近似值。

```python
# 範例：為每個約束標記一個布林 assumption
b = model.new_bool_var("capacity_bm1_cpu")
model.add(usage <= capacity).only_enforce_if(b)
model.add_assumptions([b])

# 求解後
if status == INFEASIBLE:
    core = solver.SufficientAssumptionsForInfeasibility()
    # core 告訴你哪些約束衝突
```

### 為什麼不使用 Assumptions

| 限制 | 對我們 Solver 的影響 | 嚴重度 |
|------|---------------------|--------|
| **強制 `num_workers=1`** | Solver 預設使用 `num_workers=8`。Assumptions 禁用多執行緒，典型工作負載效能下降 3-5 倍。 | **Critical** |
| **與 optimization objectives 不相容** | Solver 使用 `model.minimize()` 包含 4 個加權項。CP-SAT 在 assumptions + objective 下行為不可靠 — 可能 hang 住或產生錯誤結果。 | **Critical** |
| **MUS 不保證最小** | 回傳的 assumption 集合經過啟發式最小化，但可能包含冗餘項目，使解釋帶有雜訊。 | Medium |
| **無法重用 solve state** | 每次使用不同 assumptions 都需要完整前處理，無法增量測試約束子集。 | Medium |
| **僅支援布林變數** | 無法直接標記整數約束（capacity、headroom）。需要為每個約束額外包裝 enforcement literal，增加模型複雜度。 | Low |

### Assumptions 的理想使用場景（如果沒有上述限制）

- 精確定位 INFEASIBLE 的約束衝突（例如：「bm-3 的 CPU capacity 與 masters-ha anti-affinity rule 衝突」）
- 不需要重建模型即可找到最小不可滿足約束子集
- 互動式除錯工作流程

### 我們的替代方案：現有做法 + Post-Solve 指標

| 面向 | Assumptions 方案 | 我們的方案 |
|------|-----------------|-----------|
| **失敗診斷** | `SufficientAssumptionsForInfeasibility()` — 自動 MUS | `_constraint_layer_check()` — 重建 3 個小模型，逐層測試 |
| **效能** | 單執行緒，無法使用 objective | 多執行緒，與主求解獨立 |
| **成功解釋** | 無法幫助（僅適用於 INFEASIBLE） | Post-solve 指標擷取，使用 `solver.value()` |
| **相容性** | 與 `minimize()` 衝突 | 完全相容 |

**結論**：Assumptions 不適合帶有 optimization objectives 的 production solver。現有的 layer check 對失敗診斷更穩健。成功路徑的可解釋性改用 post-solve value extraction。

---

## Part 2: Explainability 設計

### 現狀分析

> 實作狀態：✅ 已實作 ｜ ⏳ 設計待實作

| 路徑 | 目前回傳內容 | 缺口 |
|------|------------|------|
| **INPUT_ERROR** | ✅ `input_errors: [...]`（重複 BM 偵測，`solver.py:99-116`） + ✅ `advisories`（若有） | 目前僅偵測重複 BM 與重複 candidate，未涵蓋其他靜態驗證 |
| **成功** | ✅ `advisories`（若有） + `assignments`, `solver_status`, `solve_time_seconds` | ⏳ 無 objective 拆解、⏳ 無 per-BM 利用率指標 |
| **失敗** | ✅ `constraint_check.failed_at`, `vms_with_no_eligible_bm`, `infeasible_anti_affinity_rules`, `counts` + ✅ `advisories`（若有） | ⏳ 無 capacity gap 分析（VM 差多少放不下？） |
| **Exception** | ✅ `advisories`（若有），`solver_status="ERROR: ..."` | 例外路徑刻意保持極簡 |

`advisories` 透過 `_with_advisories()`（`solver.py:777-782`）注入**所有**回傳路徑，是橫切關注點，與其他 diagnostics 區段獨立。

---

### Diagnostic 系統架構

#### 四條回傳路徑與決定點

`VMPlacementSolver.solve()`（`solver.py:701-775`）依序檢查以下 gate，第一個命中決定路徑：

```
solve() 進入點
  │
  ├─ self._input_errors 非空？ ──── YES ──→ INPUT_ERROR
  │   （duplicate BM/candidate            ├─ diagnostics: input_errors + advisories
  │    在 __init__ 階段就已偵測）          └─ unplaced_vms = 全部 VM
  │   NO
  │
  ├─ try: _build_variables()
  │       _add_*_constraints()
  │       _add_objective()
  │       solver.solve(self.model)
  │
  ├─ status == OPTIMAL/FEASIBLE？ ── YES ──→ 成功
  │                                        ├─ _extract_solution()
  │                                        ├─ diagnostics: advisories（未來：objective + per_bm）
  │                                        └─ unplaced_vms 通常為空（partial mode 例外）
  │   NO
  │
  ├─ status == INFEASIBLE/UNKNOWN？ ─ YES ──→ 失敗
  │                                        ├─ DiagnosticsBuilder.build()
  │                                        ├─ diagnostics: constraint_check + 4 區段 + advisories
  │                                        └─ unplaced_vms = 全部 VM
  │
  └─ except Exception: ─────────────────→ Exception
                                           ├─ solver_status = "ERROR: {e}"
                                           ├─ diagnostics: advisories（其他刻意留空）
                                           └─ unplaced_vms = 全部 VM
```

**設計重點**：
1. **路徑互斥**：四條路徑不會同時走到，但 `advisories` 會出現在任何一條（透過 `_with_advisories()`）
2. **Fail-fast**：INPUT_ERROR 在 `__init__` 階段就偵測，連 model 都不建（節省成本、且這類錯誤是 scheduler bug，build model 也沒意義）
3. **Exception 路徑刻意極簡**：因為 exception 表示 solver 內部 bug，能塞 diagnostics 的前提是 model state 已正確，但 exception 時這假設不成立

#### 模組分工

| 模組 | 職責 | 何時執行 |
|------|------|---------|
| `solver.py` | Model 建構、約束加入、objective 設定、`solver.solve()`、advisory 收集（model build 階段）、成功時解抽取 | 每次 `solve()` |
| `diagnostics.py` | INFEASIBLE/UNKNOWN 後重建獨立小模型逐層測試、計算純資料分析（如未來的 capacity gap） | 僅在失敗路徑觸發 |
| `models.py` | `PlacementResult.diagnostics: dict[str, Any]` — 故意 untyped，避免 schema 演進綁死 Pydantic | （schema 定義） |

**為什麼診斷分散在 2 個模組？**
- `solver.py` 持有 model、變數、`solver.value()` 結果 — 「成功 + advisory」相關 diagnostic 必須在這裡
- `diagnostics.py` 重建獨立小模型且用 5 秒 timeout — 與主求解隔離，避免診斷邏輯影響主路徑效能與正確性
- 兩者透過 `get_eligible_baremetals()` 與 `RESOURCE_FIELDS`（`solver.py:51, 54-76`）共享原子邏輯，避免不一致

---

### Diagnostic 核心 Helper 的設計原理

要修改或擴充 diagnostic 邏輯前，必須理解三個現有 helper **為何這樣設計**。

#### Helper A：`_constraint_layer_check()` — 為什麼是 layered？

位置：`diagnostics.py:113-189`

**問題**：CP-SAT 回 INFEASIBLE 時不會告訴你「哪個約束導致衝突」。MUS（minimal unsatisfiable subset）需要 assumptions，但前面 Part 1 已說明那條路不通。

**解法**：拆成 3 個獨立小模型，依約束複雜度由淺入深逐層加入，找出**第一個**讓問題變 INFEASIBLE 的層級：

| 層級 | 加入的約束 | 若這層 fail 表示 |
|------|-----------|----------------|
| `one_bm_per_vm` | 每個 VM 必須放在恰好 1 台 BM 上 | 至少有一個 VM 沒有 eligible BM（最常見根因） |
| `capacity` | + 每台 BM 各維度容量上限 | VMs 個別可放，但總和裝不下 |
| `anti_affinity` | + AG 散佈規則 | 容量夠，但 anti-affinity policy 衝突 |

**設計選擇與理由**：

1. **由淺入深的順序**：對應「結構必要 → 資源限制 → policy 限制」三層。第一層失敗不可能解，第二層失敗可能加機器解決，第三層失敗通常需要調 policy。錯一層只能在那層之後修，所以第一個 fail 點就是根因。

2. **獨立小模型而非重用 self.model**：
   - 主 model 帶 objective 與 splitter waste terms — 加入這些反而干擾診斷
   - 重用會強制把 build 邏輯切細，破壞 `_add_*_constraints` 的可讀性
   - 獨立模型可以在診斷階段任意加減層而不影響主路徑

3. **5 秒 timeout（`diagnostics.py:167`）**：診斷模型比主模型簡單，正常 < 1 秒。設 5 秒是保險上限 — 若超過就回 UNKNOWN，避免診斷本身變成新的延遲源。

4. **`failed_at` 取「第一個」非 OK 的層**：後層可能因前層仍 fail 而 fail，但根因就在第一層。

**讀者如果想動這個 helper，必須回答**：
- 加新約束類型 → 要不要新增第 4 層？放在哪個位置？
- 新層的 timeout 是否需要不同（例如更複雜的 scheduling constraint）？

#### Helper B：`_check_anti_affinity_feasibility()` — Pigeonhole 的必要不充分性

位置：`diagnostics.py:93-111`

**程式邏輯**：
```python
min_ags_needed = ceil(vm_count / max_per_ag)
reachable_ags = 該 group 的 VM 至少能放上的 BM 所在的 AG 集合
if len(reachable_ags) < min_ags_needed: → infeasible
```

**為什麼這是必要不充分（重要！）**：

- **必要條件成立**：根據 pigeonhole，`vm_count` 個 VM 分到 `len(reachable_ags)` 個籃子，每籃最多 `max_per_ag`，必須滿足 `len(reachable_ags) * max_per_ag >= vm_count`，即 `len(reachable_ags) >= ceil(vm_count / max_per_ag)`。違反就絕對 INFEASIBLE。
- **不充分**：通過此檢查不代表 feasible — 可能因為**容量**讓某個 AG 內的 BM 都裝不下這個 group 的 VM，或與其他 group 的 anti-affinity 互相擠壓。
- **診斷意義**：能 list 出來的 rule 是「絕對的根因」（高信心），沒列出來的不代表 OK（低信心）。**必要不充分的診斷只能指出有問題的東西，不能保證列出全部問題**。

**讀者如果想加新的可行性檢查**：必須先區分「絕對必要」與「啟發式」 — 兩者放在不同欄位，避免誤導 scheduler。

#### Helper C：`get_eligible_baremetals()` — 為什麼是 module-level

位置：`solver.py:54-76`，`diagnostics.py:62-63` 透過薄 wrapper 呼叫

**設計原則：Single source of truth**。

eligibility 邏輯（candidate list 過濾 + capacity fits_in 檢查）若在 solver 與 diagnostics 各寫一份，會發生：
- Solver 認為 VM 無法放（因某個邊界條件）→ 排除該 (vm, bm) 變數
- Diagnostics 認為 VM 可放 → 不報 `vms_with_no_eligible_bm`
- → INFEASIBLE 但找不到根因，scheduler 完全無線索

`RESOURCE_FIELDS = ["cpu_cores", "memory_mib", "storage_gb", "gpu_count"]` 同理 — 任何 per-resource 計算（capacity、headroom、slot score、未來 capacity_gap）都必須走這個常數，避免遺漏新增的維度。

**讀者規則**：`solver.py` 與 `diagnostics.py` 共用的邏輯，**必須**抽到 module-level 函式，由 diagnostics 透過 import 或薄 wrapper 呼叫，不可複製。

---

### 開發新 Diagnostic 的工作流程

#### Step 1：決策樹 — 屬於哪條路徑？

```
我要加的 diagnostic 是…
│
├─ 不需要 solve 就能判斷的靜態錯誤（如 schema 違反、互斥欄位、重複 ID）？
│   → INPUT_ERROR 路徑
│   → 在 __init__ 加偵測 → self._input_errors.append(...)
│   → 範例：duplicate BM 偵測（solver.py:99-116）
│
├─ Solve 成功但與 policy/最佳實務有落差？
│   → Advisory 路徑
│   → 在 model build 階段（rule resolution 或 constraint add）self.advisories.append({...})
│   → 必須非阻斷（不可改變 success）
│   → 範例：ag_spread_below_target（solver.py:248-274）
│
├─ Solve 失敗的根因解釋？
│   ├─ 純資料分析（不需要 solve）→ 加在 DiagnosticsBuilder.build()，新增方法
│   │   範例：vms_with_no_eligible_bm（diagnostics.py:70-72）
│   │   未來範例：capacity_gaps（已設計，待實作）
│   │
│   └─ 需要重建小模型（如新約束類型的根因定位）→ 加新 layer 到 _constraint_layer_check
│       注意：保持每個 layer 獨立、timeout 上限、由淺入深排序
│
└─ Solve 成功的品質指標（utilization、objective 拆解）？
    → 在 _build_success_diagnostics() 中加（待實作）
    → 規則：只用 solver.value(var)，不再呼叫 solve
    → 若需要 model variable，採用 Two-phase pattern（見下節）
```

#### Step 2：Two-phase Pattern — 需要 model variable 的 diagnostic

某些 diagnostic（如 objective 拆解）需要讀取**模型內部變數**的解值。CP-SAT 變數是建構 model 時建立的局部物件，post-solve 階段拿不到 — 必須先 stash。

**Phase 1 — Build time（`_compute_*` 系列方法內）**：
```python
def _compute_headroom_penalties(self) -> list[cp_model.IntVar]:
    penalties = []
    # ... 建立變數與約束 ...
    self._headroom_penalty_vars = penalties   # ← STASH
    return penalties
```

**Phase 2 — Post-solve（`_build_success_diagnostics` 內）**：
```python
def _build_success_diagnostics(self, solver):
    if self._headroom_penalty_vars:
        raw = sum(solver.value(p) for p in self._headroom_penalty_vars)
        diag["headroom_penalty"] = self.config.w_headroom * raw
```

**規則**：
1. Stash 在 `__init__` 預先初始化為空 list（避免 `AttributeError`）
2. Stash 變數命名前綴 `_`（私有，diagnostic 才用）
3. Phase 2 必須容忍空 stash（若該 objective term 未啟用，list 就是空）
4. 不要 stash「可從 stash 重算」的衍生值 — 只 stash 最小集合

#### Step 3：反模式（必須避免）

| 反模式 | 為什麼錯 | 正確做法 |
|--------|---------|---------|
| 在 diagnostic 階段呼叫 `solver.solve(self.model)` | 主 model 已 solve 過、再 solve 是浪費；且 INFEASIBLE 仍是 INFEASIBLE | 用 `_constraint_layer_check` 的拆層獨立小模型 |
| Diagnostic 邏輯 throw exception 沒 catch | 會把成功路徑變 ERROR，破壞主功能 | `_extract_solution` 內任何 diagnostic 計算須能容忍部分資料缺失 |
| Advisory 在 `self.advisories.append` 後又 `model.add(0 == 1)` 強制失敗 | Advisory 定義就是「成功但警示」，阻斷會讓 advisory 與 INFEASIBLE 重疊不可分 | 阻斷情境改寫成 INPUT_ERROR 或讓 solver 自然 INFEASIBLE 後填 failure diagnostic |
| 在 `solver.py` 與 `diagnostics.py` 各寫一份 eligibility / capacity 計算 | 兩處不同步 → 診斷與解不一致 | 抽到 module-level（如 `get_eligible_baremetals`），雙方共用 |
| 把「絕對必要」與「啟發式」診斷混在同一欄位 | Scheduler 無法判斷信心度 | 分欄位：`infeasible_anti_affinity_rules`（必要）vs 未來可能加的 `likely_*`（啟發式） |
| Advisory `details` 結構每次觸發都不同 | Scheduler 無法 parse | 同一個 `type` 的 `details` 欄位集合必須穩定，新欄位只能 additive |
| 在 `_extract_solution` 中重新走 `self.request.vms` 計算昂貴指標 | 大規模請求下會拖慢成功路徑 | 預先 stash 必要中間值，post-solve 只做 O(n) 讀取 |

---

### 已實作：Advisory 機制 — 成功但需要警示的情境

Advisory 用於回報「solver 求解成功（OPTIMAL/FEASIBLE），但與 policy 或最佳實務有落差」的情境。Go scheduler 收到後可選擇通報、忽略或拒絕該結果，但 placement 本身仍是有效的。

#### 2.0 資料模型

```json
{
  "diagnostics": {
    "advisories": [
      {
        "type": "ag_spread_below_target",
        "severity": "warning",
        "group_id": "auto/routable/master",
        "message": "Anti-affinity for routable/master below policy target: actual spread=2, target=3 (2 AG(s), 5 VMs).",
        "details": {
          "vm_count": 5,
          "num_ags": 2,
          "effective_spread": 2,
          "target_ag_spread": 3,
          "max_per_ag": 3,
          "ag_names": ["ag-a", "ag-b"]
        }
      }
    ]
  }
}
```

| 欄位 | 說明 |
|------|------|
| `type` | Advisory 類型字串（machine-readable，目前只有 `ag_spread_below_target`） |
| `severity` | 嚴重度（目前固定 `warning`；保留 `info` / `error` 給未來類型） |
| `group_id` | 觸發來源（例如 anti-affinity rule id） |
| `message` | 人類可讀的單行訊息（已含關鍵數字） |
| `details` | 結構化細節，欄位由 `type` 決定 |

#### 2.0.1 Advisory type 清單

**`ag_spread_below_target`** — 由 `_resolve_anti_affinity_rules()`（`solver.py:248-274`）觸發。

當 auto-generated anti-affinity rule 的「最大可能 AG 散佈」低於 `SolverConfig.target_ag_spread`（預設 3）時觸發。`effective_spread = min(num_ags, len(vm_ids))` — 即 infra AG 數與 group VM 數的較小值。

兩種典型成因：
- **Infra 不足**：群組有 5 個 VM 但只有 2 個 AG → effective_spread=2 < target=3
- **Group 太小**：有 5 個 AG 但群組只有 2 個 VM → effective_spread=2 < target=3

對於含 synthetic VM（splitter slot）的 rule，`details.max_per_ag` 為字串 `"dynamic"`（因實際 VM 數是決策變數）。

#### 2.0.2 注入路徑

`_with_advisories()` 統一將 `self.advisories` 合併入 diagnostics dict：

| 路徑 | 注入點 |
|------|--------|
| INPUT_ERROR | `solver.py:719` |
| 失敗（INFEASIBLE/UNKNOWN） | `solver.py:755` |
| Exception | `solver.py:774` |
| 成功（OPTIMAL/FEASIBLE） | `solver.py:833`（在 `_extract_solution()` 末端） |

設計意圖：advisory 與「成功/失敗」正交，scheduler 不需要在不同回傳結構間 switch。

#### 2.0.3 新增 Advisory type 的擴充流程

1. 在會偵測該情境的方法中（通常是 rule resolution 或 model build 階段）`self.advisories.append({...})`
2. 確保 `type` 字串獨特、`details` 結構穩定（scheduler 端會 parse）
3. 保持非阻斷 — advisory 不應改變 solve 是否成功的判定
4. 加上對應的單元測試（參考 `tests/test_solver.py` 的 `TestAGSpreadAdvisory`）

---

### 設計：成功路徑 — Objective Breakdown + 利用率指標 ⏳ 尚未實作

> **狀態**：設計完成，尚未進入程式碼。`_extract_solution()`（`solver.py:798-834`）目前回傳 `diagnostics=self._with_advisories({})`，僅包含 advisory（若有）。

OPTIMAL/FEASIBLE 時，透過 `solver.value()` 讀取 CP-SAT 變數值計算 post-solve 指標。不需要修改模型 — 純粹讀取已有的變數。

#### 2.1 新增回應欄位

加入 `PlacementResult.diagnostics`（成功時）：

```json
{
  "success": true,
  "assignments": [...],
  "solver_status": "OPTIMAL",
  "diagnostics": {
    "objective": {
      "total": 18,
      "bms_used": 2,
      "consolidation_cost": 20,
      "headroom_penalty": 8,
      "slot_score_bonus": -10
    },
    "per_bm": {
      "bm-01": {
        "vms_placed": ["vm-1", "vm-3", "vm-5"],
        "utilization": {
          "cpu_cores": { "used": 40, "total": 64, "pct": 62 },
          "memory_mib": { "used": 192000, "total": 256000, "pct": 75 },
          "storage_gb": { "used": 1200, "total": 2000, "pct": 60 },
          "gpu_count": { "used": 0, "total": 0, "pct": 0 }
        }
      },
      "bm-02": { "..." : "..." }
    }
  }
}
```

#### 2.2 資料來源（全部可在 Post-Solve 取得）

| 指標 | 來源 | 計算方式 |
|------|------|---------|
| `bms_used` | `self.bm_used` | `sum(solver.value(v) for v in self.bm_used.values())` |
| `consolidation_cost` | `self.bm_used` + config | `w_consolidation * bms_used` |
| `headroom_penalty` | 在 `_compute_headroom_penalties` 中儲存 penalty 變數 | `w_headroom * sum(solver.value(p) for p in penalty_vars)` |
| `slot_score_bonus` | 在 `_compute_slot_score_bonus` 中儲存 score 變數 | `w_slot_score * sum(solver.value(s) for s in score_vars)` |
| `total` | `solver.objective_value` | 直接從 CP-SAT 取得 |
| Per-BM 利用率 | `self.assign` + `self.vm_map` + `self.bm_map` | 加總已放置 VM demand + 既有 used，除以 total |
| Per-BM VM 列表 | `self.assign` | 篩選 `solver.value() == 1` per BM |

#### 2.3 實作方式

**Step 1**：在模型建構時儲存中間 objective 變數。

```python
# __init__ 中初始化
self._headroom_penalty_vars: list[cp_model.IntVar] = []
self._slot_score_vars: list[cp_model.IntVar] = []

# _compute_headroom_penalties 中 — return 前儲存
self._headroom_penalty_vars = penalties
return penalties

# _compute_slot_score_bonus 中 — return 前儲存
self._slot_score_vars = scores
return scores
```

**Step 2**：新增 `_build_success_diagnostics` method。

```python
def _build_success_diagnostics(self, solver: cp_model.CpSolver) -> dict:
    diag = {}

    # Objective breakdown
    obj = {"total": solver.objective_value}
    if self.bm_used:
        obj["bms_used"] = sum(solver.value(v) for v in self.bm_used.values())
        obj["consolidation_cost"] = self.config.w_consolidation * obj["bms_used"]
    if self._headroom_penalty_vars:
        raw = sum(solver.value(p) for p in self._headroom_penalty_vars)
        obj["headroom_penalty"] = self.config.w_headroom * raw
    if self._slot_score_vars:
        raw = sum(solver.value(s) for s in self._slot_score_vars)
        obj["slot_score_bonus"] = -self.config.w_slot_score * raw
    diag["objective"] = obj

    # Per-BM utilization
    per_bm = {}
    for bm in self.request.baremetals:
        placed = [
            vm_id for vm_id in self.vm_map
            if (vm_id, bm.id) in self.assign
            and solver.value(self.assign[(vm_id, bm.id)]) == 1
        ]
        if not placed:
            continue
        new_demand = sum(
            (self.vm_map[vid].demand for vid in placed),
            start=Resources(),
        )
        after = bm.used_capacity + new_demand
        util = {}
        for field in RESOURCE_FIELDS:
            total = getattr(bm.total_capacity, field)
            used = getattr(after, field)
            util[field] = {
                "used": used,
                "total": total,
                "pct": round(used * 100 / total) if total > 0 else 0,
            }
        per_bm[bm.id] = {"vms_placed": placed, "utilization": util}
    diag["per_bm"] = per_bm

    return diag
```

**Step 3**：在 `_extract_solution` 中呼叫。

```python
# _extract_solution 回傳前：
diagnostics = self._build_success_diagnostics(solver)
return PlacementResult(
    success=...,
    assignments=...,
    diagnostics=diagnostics,
    ...
)
```

---

### 設計：失敗路徑 — Capacity Gap 分析 ⏳ 尚未實作

> **狀態**：設計完成，尚未進入程式碼。`DiagnosticsBuilder.build()`（`diagnostics.py:65-91`）目前在偵測到 `vms_with_no_eligible_bm` 時只列出 VM id 清單，未計算 shortfall。

INFEASIBLE 時，對無 eligible BM 的 VM 顯示差距有多大。

#### 2.4 新增診斷區段

加入 `DiagnosticsBuilder.build()`：

```json
{
  "capacity_gaps": {
    "vm-big": {
      "demand": { "cpu_cores": 128, "memory_mib": 512000 },
      "best_available": { "cpu_cores": 64, "memory_mib": 256000 },
      "shortfall": { "cpu_cores": 64, "memory_mib": 256000 }
    }
  }
}
```

#### 2.5 實作方式

在 `DiagnosticsBuilder.build()` 中，偵測到 `vms_with_no_eligible_bm` 後：

```python
if no_eligible:
    diag["vms_with_no_eligible_bm"] = no_eligible
    diag["capacity_gaps"] = self._compute_capacity_gaps(no_eligible)
```

```python
def _compute_capacity_gaps(self, vm_ids: list[str]) -> dict:
    gaps = {}
    for vm_id in vm_ids:
        vm = self.vm_map[vm_id]
        # 找剩餘容量最大的 BM（最佳候選）
        candidates = vm.candidate_baremetals or [bm.id for bm in self.request.baremetals]
        best = None
        for bm_id in candidates:
            if bm_id not in self.bm_map:
                continue
            avail = self.bm_map[bm_id].available_capacity
            if best is None or self._capacity_score(avail) > self._capacity_score(best):
                best = avail
        if best is None:
            continue
        shortfall = {}
        for field in RESOURCE_FIELDS:
            d = getattr(vm.demand, field)
            a = getattr(best, field)
            if d > a:
                shortfall[field] = d - a
        if shortfall:
            gaps[vm_id] = {
                "demand": {f: getattr(vm.demand, f) for f in RESOURCE_FIELDS
                           if getattr(vm.demand, f) > 0},
                "best_available": {f: getattr(best, f) for f in RESOURCE_FIELDS
                                   if f in shortfall},
                "shortfall": shortfall,
            }
    return gaps
```

---

### 測試模式

#### 共用 Fixture（`tests/conftest.py`）

| Helper | 用途 |
|--------|------|
| `make_bm(bm_id, cpu, mem, disk, gpu, used_*, ag, dc, rack)` | 建一台 BM；省略參數有合理預設（cpu=64 等） |
| `make_vm(vm_id, cpu, mem, disk, role, cluster, ip_type, candidates)` | 建一個 VM；預設 routable worker |
| `solve(vms, bms, rules=None, **config_overrides)` | 用預設 config（auto_anti_affinity=False, max_solve_time=10s）跑求解 |
| `amap(result)` | `vm_id -> bm_id` 速查 dict |
| `client` (pytest fixture) | FastAPI TestClient，用於整合測試 |

**慣例**：寫新 diagnostic 測試前**先看 conftest** — 多數需求已有 helper，自己 build `Baremetal()`/`VM()` 通常代表沒讀過。

#### Diagnostic 測試的三類斷言

依 diagnostic 所屬路徑，斷言模式不同：

**1. Advisory（成功 + 警示）**：`success=True` 與 advisory 內容**必須分開斷言**
```python
def test_my_advisory(self):
    r = solve(vms, bms, auto_generate_anti_affinity=True)
    assert r.success                                    # 不可被打斷
    assert "advisories" in r.diagnostics                # 有觸發
    a = r.diagnostics["advisories"][0]
    assert a["type"] == "my_new_type"
    assert a["details"]["my_field"] == expected_value   # 結構穩定
```
反例請看 `tests/test_solver.py:230-321` 的 `TestAGSpreadAdvisory`。

**2. INPUT_ERROR**：要驗證**完全不嘗試 solve**
```python
def test_my_input_error(self):
    r = solve(vms_with_bad_input, bms)
    assert not r.success
    assert r.solver_status.startswith("INPUT_ERROR:")
    assert "input_errors" in r.diagnostics
    assert any("expected fragment" in e for e in r.diagnostics["input_errors"])
    assert r.unplaced_vms == [vm.id for vm in vms_with_bad_input]  # 全部 unplaced
```

**3. 失敗路徑（INFEASIBLE）**：要構造**確定無解**的場景，然後驗證 `constraint_check.failed_at` 與對應區段
```python
def test_my_failure_diagnostic(self):
    # 構造保證 INFEASIBLE 的場景（如 demand > 所有 BM capacity）
    r = solve(vms_too_big, bms_too_small)
    assert not r.success
    assert r.solver_status == "INFEASIBLE"
    assert r.diagnostics["constraint_check"]["failed_at"] == "one_bm_per_vm"
    assert "vms_with_no_eligible_bm" in r.diagnostics
```

#### 「不該觸發」也要測

Diagnostic 最容易出的 bug 是**過度敏感**（誤報）。每個 advisory / 失敗區段都應有對應的「正常情境不觸發」測試。看 `TestAGSpreadAdvisory` 中 `test_no_advisory_when_*` 系列 — 4 個觸發測試對 3 個不觸發測試。

---

### Schema 契約與整合

#### 與 Go Scheduler 的契約面

`PlacementResult.diagnostics: dict[str, Any]`（`models.py:201`）對 Go 端是 free-form JSON。但**雖然 untyped，仍是契約**。

| 契約等級 | 包含 | 修改規則 |
|---------|------|---------|
| **強契約**（Go scheduler 會 parse） | `success`, `solver_status`, `assignments[]`, `unplaced_vms`, `solve_time_seconds` | 改 schema 必須與 scheduler 同步發版 |
| **半契約**（Go scheduler 可能讀） | `diagnostics.input_errors`, `diagnostics.constraint_check.failed_at`, `diagnostics.advisories[].type`, `diagnostics.advisories[].details.<已穩定欄位>` | 只能 additive（加新 key、加新 advisory type）；不可改既有欄位語意 |
| **弱契約**（內部診斷，不保證） | 其他 `diagnostics.*` 區段、log 訊息、`details.message` 文字 | 可自由演進；但若 Go 端開始依賴某欄位，立即升等為半契約並寫入此表 |

#### Advisory `details` 的演進規則

每個 advisory `type` 視為**獨立 schema**：
- ✅ 加新欄位（additive，舊版 Go scheduler 自動 ignore）
- ✅ 加新的 `type`
- ❌ 改既有欄位的語意（例如 `effective_spread` 從「最大可能散佈」改成「實際散佈」）
- ❌ 移除既有欄位
- ⚠️ 拆/合欄位：等同移除 + 新增，需要兩階段發版

**版本演進策略**：若需要破壞性變更，新增 `type=ag_spread_below_target_v2` 並讓兩者並存一個 release，再移除舊版。

#### Splitter 整合 Checklist

`active_vars`（splitter 注入的 BoolVar，標示「這個 synthetic VM 是否啟用」）對 diagnostic 的影響涵蓋多個面向。實作任何 diagnostic 前，依此 checklist 檢查：

| Diagnostic 類型 | 要對 active_vars 做的事 | 為什麼 |
|---------------|-----------------------|-------|
| Eligibility 計算 | **不變** — `get_eligible_baremetals` 不看 active_var | Eligibility 是靜態事實，與 slot 是否啟用無關 |
| Anti-affinity rule（含 synthetic） | `max_per_ag` 要動態化（`solver.py:411-438`），advisory `details.max_per_ag` 字串為 `"dynamic"` | VM 數量是決策變數，固定 max 算錯 |
| `_extract_solution` | 跳過 `solver.value(active_var) == 0` 的 slot | 那是 splitter 決定不用的 slot |
| 未來 `_build_success_diagnostics` | `per_bm.vms_placed` 只列 active_var==1 的 VM；utilization 也只算這些 | 否則回報的「已放置」與 assignments 不一致 |
| 未來 `capacity_gaps` | 排除 `active_vars` 中尚未啟用的 slot | 「solver 自願放棄」不算 shortfall |
| Advisory 偵測 | 用 active_vars 上限數估算 `effective_spread` 是保守作法 | 求 spread 上限即可，下限會誤觸發 |

**規則**：寫新 diagnostic 時，先問「這個 diagnostic 的輸入有沒有 synthetic VM 參與？」如果有，必須在文件 + 測試明確說明 splitter 情境的行為。

#### INPUT_ERROR vs INFEASIBLE 的判定原則

兩者都會回 `success=False`，但語意完全不同：

| 維度 | INPUT_ERROR | INFEASIBLE |
|------|------------|-----------|
| 根本原因 | 呼叫端送錯（schema 違反、邏輯衝突如重複 ID） | Infra 真的不夠（資源、AG 數、policy 過嚴） |
| 修復責任 | Scheduler / 上游 caller | Infra 增加機器、放寬 policy、或減少 VM |
| 偵測時機 | `__init__` 階段，model 未建 | Solve 完成後 |
| 是否該重試 | 否（同樣 input 必再失敗） | 視情況（infra 變動可能解開） |
| Solve 成本 | 0（fail-fast） | 完整 solve 時間 |

**判定原則**：
- 「只看 input 就能判斷必錯」→ INPUT_ERROR（如 duplicate ID、互斥欄位、schema 違反）
- 「需要組合所有 input 才知道能不能解」→ INFEASIBLE（如總 demand > 總 capacity）
- **不可把 INFEASIBLE 偽裝成 INPUT_ERROR** — 因為 infra 一變動同樣 input 可能就能解，提早 reject 會讓 scheduler 永遠不敢再試
- **不可把 INPUT_ERROR 拖到 solve 後才回** — 浪費 solve 成本，且 scheduler 拿到 INFEASIBLE 會誤以為是 infra 問題

---

### 觀察性

#### Logger 使用慣例

`solver.py` 與 `diagnostics.py` 共用 `logger = logging.getLogger(__name__)`：

| Level | 使用時機 | 範例 |
|-------|---------|------|
| `error` | INPUT_ERROR / Exception / VM 無 eligible BM 強制 INFEASIBLE | `solver.py:712, 768, 328` |
| `warning` | Advisory 觸發、INFEASIBLE 帶 diagnostics | `solver.py:274, 756` |
| `info` | Auto rule 解析、solve 開始/結束、status | `solver.py:215-224, 234-241, 737-748` |

**原則**：
1. **Diagnostic 內容會雙寫** — log 給 ops、diagnostics 給 scheduler，是不同消費者
2. Log 訊息要含**足以辨識**的 context（VM id、BM id、rule group_id），不可只說 "failed"
3. Advisory 觸發要**同時** `self.advisories.append` 與 `logger.warning` — log 用於 ops 即時告警，advisory 用於 scheduler 程式化反應
4. 不要把 diagnostic 全文 dump 進 log（太吵） — log 摘要，diagnostics 給細節

#### 除錯流程

碰到「diagnostic 結果不如預期」時：
1. 先看 `constraint_check.failed_at`（若是失敗路徑）— 這定位到層級
2. 用 CLI 模式重跑：`python -m app.server --cli --input <case>.json` 看完整 diagnostics
3. 開 `logging.basicConfig(level=logging.DEBUG)`，重跑看完整 model 建構過程
4. 拆掉 objective（`w_*=0`）跑一次，分離「constraint 問題」與「objective 影響」

---

## Part 3: 總結

### 已實作

| 功能 | 路徑 | 程式位置 |
|------|------|---------|
| `vms_with_no_eligible_bm`（無候選 BM 的 VM 清單） | 失敗 | `diagnostics.py:70-72` |
| `infeasible_anti_affinity_rules`（spread 不可能達成的規則） | 失敗 | `diagnostics.py:75-77`, `_check_anti_affinity_feasibility` |
| `constraint_check`（逐層測試找出第一個 infeasible 的約束層） | 失敗 | `diagnostics.py:79-80`, `_constraint_layer_check` |
| `counts`（vms / bms / ags / variables / rules 摘要） | 失敗 | `diagnostics.py:82-89` |
| `input_errors`（重複 BM、重複 candidate） | INPUT_ERROR | `solver.py:99-116`, `solver.py:719` |
| `advisories`（橫切：注入所有路徑） | 全部 | `solver.py:777-782` |
| `advisories[].type=ag_spread_below_target`（policy 散佈不足） | 成功/失敗皆可能 | `solver.py:248-274` |

### 未實作（設計已定）

| 功能 | 路徑 | 優先級 | 工作量 |
|------|------|--------|--------|
| Objective 拆解（total, consolidation, headroom, slot score） | 成功 | High | Low — 讀取已有變數 |
| Per-BM 放置後利用率 | 成功 | High | Low — 從 assignments 計算 |
| Per-BM VM 列表 | 成功 | High | Low — 已在 extract 迴圈中 |
| Capacity gap 分析 | 失敗 | Medium | Low — 比較 demand vs 最佳可用 |

### 不做的事（以及原因）

| 功能 | 原因 |
|------|------|
| CP-SAT assumptions | 強制單執行緒、破壞 optimizer、與 objectives 不相容 |
| Per-VM 放置理由（「為什麼選這台 BM？」） | 需要解 N 個替代模型 — 昂貴且對 scheduler 自動化價值低 |
| 約束鬆緊度分析 | CP-SAT 不暴露 dual values；需要 shadow-price 近似，複雜度高 |

### 需要修改的檔案（剩餘工作）

| 檔案 | 變更 |
|------|------|
| `app/solver.py` | 儲存 `_headroom_penalty_vars` 和 `_slot_score_vars`；新增 `_build_success_diagnostics()`；在 `_extract_solution()` 中以 `self._with_advisories(self._build_success_diagnostics(solver))` 取代目前的空 dict |
| `app/diagnostics.py` | 在 `DiagnosticsBuilder` 新增 `_compute_capacity_gaps()`，於偵測到 `vms_with_no_eligible_bm` 後呼叫 |
| `app/models.py` | 不需要修改 schema — `diagnostics: dict[str, Any]` 已足夠彈性 |
| `tests/test_solver.py` | 新增成功路徑 diagnostics 測試（objective breakdown、per-BM 利用率） |
| `tests/test_diagnostics.py` | 新增 capacity gap 分析測試 |

### Splitter 互動

詳見前節「Splitter 整合 Checklist」。摘要：synthetic VM（`active_vars`）會影響 anti-affinity advisory 的 `max_per_ag`（顯示為 `"dynamic"`），並要求未來的 `capacity_gaps` / `_build_success_diagnostics` 在計算時排除 `active_var == 0` 的 slot。

### 驗證方式

```bash
# 所有既有測試通過
python -m pytest tests/ -x -q

# Advisory 已實作 — 觸發後在 diagnostics 中可見
python -m pytest tests/test_solver.py::TestAGSpreadAdvisory -v
# → 6 個測試覆蓋 advisory 觸發 / 不觸發條件

# 成功路徑包含 diagnostics（待實作後）
python -m app.server --cli --input examples/success_basic.json
# → diagnostics.objective.total, diagnostics.per_bm.*.utilization

# 失敗路徑包含 capacity gaps（待實作後）
python -m app.server --cli --input examples/error_infeasible.json
# → diagnostics.capacity_gaps（如適用）

# Duplicate BM 錯誤不受影響
python -m app.server --cli --input examples/error_duplicate_bm.json
# → INPUT_ERROR + diagnostics.input_errors
```

---

## Part 4: 相關文件

| 主題 | 文件 | 與本文的關係 |
|------|------|-------------|
| 為什麼選 CP-SAT | [`why-cp-sat.md`](./why-cp-sat.md) | 補充 Part 1 為何不用 assumptions 的上層決策 |
| Objective function 細節 | [`objective-function.md`](./objective-function.md), [`objective-function-guide.md`](./objective-function-guide.md) | 解釋 `_compute_headroom_penalties` / `_compute_slot_score_bonus` 的數學模型 — 開發 success diagnostic（objective breakdown）時必讀 |
| Constraint 完整列表 | [`constraints.md`](./constraints.md) | 列出所有 hard/soft constraint — 新增 `_constraint_layer_check` 層級時需對齊 |
| Splitter 設計 | [`requirement-splitter.md`](./requirement-splitter.md), [`requirement-splitter-v2.md`](./requirement-splitter-v2.md), [`reading-guide-splitter.md`](./reading-guide-splitter.md) | `active_vars` / synthetic VM 的來源與語意 — 寫對 splitter 互動的 diagnostic 時必讀 |
| Go scheduler 整合 | [`go-scheduler-guide.md`](./go-scheduler-guide.md) | Schema 契約的另一端（消費者視角） |
| 提案新 diagnostic 功能 | [`enhancement-proposal-template.md`](./enhancement-proposal-template.md) | 大型 diagnostic 變更應走 enhancement proposal 流程 |

