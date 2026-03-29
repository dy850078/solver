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

| 路徑 | 目前回傳內容 | 缺口 |
|------|------------|------|
| **成功** | `assignments`, `solver_status` (OPTIMAL/FEASIBLE), `solve_time_seconds` | 無 objective 拆解、無利用率指標、無放置理由 |
| **失敗** | `constraint_check.failed_at`, `vms_with_no_eligible_bm`, `infeasible_anti_affinity_rules`, `counts` | 無 capacity gap 分析（VM 差多少放不下？） |

---

### 設計：成功路徑 — Objective Breakdown + 利用率指標

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

### 設計：失敗路徑 — Capacity Gap 分析

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

## Part 3: 總結

### 要做的事

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

### 需要修改的檔案

| 檔案 | 變更 |
|------|------|
| `app/solver.py` | 儲存 `_headroom_penalty_vars` 和 `_slot_score_vars`；新增 `_build_success_diagnostics()`；在 `_extract_solution()` 中呼叫 |
| `app/diagnostics.py` | 在 `DiagnosticsBuilder` 新增 `_compute_capacity_gaps()` |
| `app/models.py` | 不需要修改 schema — `diagnostics: dict[str, Any]` 已足夠彈性 |
| `tests/test_solver.py` | 新增成功路徑 diagnostics 測試（objective breakdown、per-BM 利用率） |
| `tests/test_diagnostics.py` | 新增 capacity gap 分析測試 |

### 驗證方式

```bash
# 所有既有測試通過
python -m pytest tests/ -x -q

# 成功路徑包含 diagnostics
python -m app.server --cli --input examples/success_basic.json
# → diagnostics.objective.total, diagnostics.per_bm.*.utilization

# 失敗路徑包含 capacity gaps
python -m app.server --cli --input examples/error_infeasible.json
# → diagnostics.capacity_gaps（如適用）

# Duplicate BM 錯誤不受影響
python -m app.server --cli --input examples/error_duplicate_bm.json
# → INPUT_ERROR 如前
```
