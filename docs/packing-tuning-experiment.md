# BM Packing 調校實驗報告

> **日期**: 2026-05-19
> **狀態**: 待測試 / 結果待填寫
> **相關檔案**: `app/solver.py` (objective function), `app/models.py:150-171` (SolverConfig)

---

## 1. 背景

我們以 Excel 寫了一份簡化的 LP,目標函數為:

```
minimize  Σ bm_used[i]      # 使用的 BM 數
subject to                  # 容量限制
          Σ vm_demand[v] * x[v,i] ≤ capacity[i]
          Σ x[v,i] = 1       (每個 VM 一定要被擺)
```

實驗發現,**Excel LP 的解所使用的 BM 數量比目前 solver (`app/solver.py`) 還少**。
這份報告先說明此現象背後的數學原因,再提供一組標準測試場景,
用以量化在不同 (objective weight, headroom threshold) 設定下,
solver 與 Excel LP 在多個維度上的差距。

---

## 2. 為什麼 solver 用較多 BM (假設)

目前 solver 的目標函數 (`app/solver.py:662-699`) 是多目標加權:

```
minimize  w_consolidation × Σ bm_used
        + w_headroom      × Σ over_headroom_pct
        + w_slot_score    × (- Σ remaining_slots)
        + w_resource_waste× Σ splitter_waste
```

`SolverConfig` (`app/models.py:163-171`) 預設值:

| 參數 | 預設值 |
|---|---|
| `w_consolidation` | 10 |
| `w_headroom` | 8 |
| `headroom_upper_bound_pct` | 90 |
| `w_slot_score` | 0 |
| `w_resource_waste` | 5 |

### 2.1 Crossover 推導

在「再塞一個 VM 到既有 BM」與「開一台新 BM」之間,solver 是用「成本」比較的:

- 開新 BM 的成本: `w_consolidation = 10`
- 把某 BM 從 `threshold%` 推到 `threshold+k%` 的成本: `w_headroom × k = 8k`

當 `k ≥ 2` (即 90% 推到 92% 以上) 時,**新開一台 BM 反而比較便宜**,
因此 solver 會選擇開新 BM,造成 BM 用量比「純 packing」高。

通用 crossover 公式:

```
w_headroom × (100 - threshold) ≈ w_consolidation
```

### 2.2 其他可能的原因

1. **Anti-affinity 是硬約束** (`app/solver.py:382-442`):
   同 group 的 VM 強制散布到不同 AG,等於下限就需要多台 BM。Excel LP 沒有這條。
2. **Solver time limit**: 大規模案例若提早停在 `FEASIBLE`,
   可能還沒收斂到最少 BM 的解。
3. **Splitter 注入的 waste 項**: 對 BM 用量間接影響。

---

## 3. 測試目標

1. 量化「現況 default」vs「Excel LP」的 BM 用量差距,作為基準上界 (理論下界由 Excel LP 提供)。
2. 找出在不同調校下,**最接近 Excel BM 數**且**不顯著惡化容錯/headroom** 的配置。
3. 觀察 anti-affinity 是否是主要原因 (對照無 anti-affinity 的場景)。

---

## 4. 評估指標 (KPI)

| 指標 | 定義 | 目的 |
|---|---|---|
| `bm_used_count` | 至少有一個 VM 的 BM 數 | 直接對標 Excel LP |
| `max_utilization_pct` | 所有 BM 各維度利用率最大值 | 觀察是否過度集中 |
| `headroom_violation_total` | `Σ max(util% − threshold, 0)` | 越界程度 |
| `worst_ag_load` | 單一 AG 上 VM 數最大值 | 模擬 single-AG 失效衝擊 |
| `remaining_slots` | 剩餘空間還可裝多少 t-shirt VM | 未來擴充性 |
| `solve_time_sec` | Solver 運行時間 | 是否在 time limit 內 |
| `solver_status` | `OPTIMAL` / `FEASIBLE` / `INFEASIBLE` | 解的品質 |
| `unplaced_vms` | 無法擺放的 VM 數 | 可行性 |

---

## 5. 測試輸入場景

每個輸入場景至少跑一次 Excel LP (理論下界) + 下面 §6 所有配置。
建議輸入 JSON 放在 `examples/` 下,以便重現。

| 編號 | 場景名稱 | VM 數 | BM 數 | AG 數 | Anti-affinity | 容量寬鬆度 | 目的 |
|---|---|---|---|---|---|---|---|
| S1 | small-loose | ~10 | ~8 | 3 | 1 group, 3 VMs | 寬鬆 (利用率 < 30%) | 基本可行性 |
| S2 | small-tight | ~10 | ~4 | 2 | 1 group, 3 VMs | 緊湊 (利用率 > 80%) | 觀察 headroom 衝突 |
| S3 | medium-mixed | ~50 | ~20 | 3 | 多組,包含 master/worker | 中等 (~60%) | 接近真實 |
| S4 | large-prod-like | ~200 | ~80 | 3-5 | 完整 master/worker spread | 中等 | 大規模 + 計時 |
| S5 | anti-affinity-heavy | ~30 | ~15 | 3 | 5+ groups 全部觸發 | 中等 | 隔離 anti-affinity 影響 |
| S6 | single-ag | ~30 | ~10 | **1** | (自動關閉) | 中等 | 排除 anti-affinity 變因 |
| S7 | mixed-tshirt | ~50 | ~20 | 3 | 1-2 group | 中等 | 觀察 fragmentation |

---

## 6. 測試配置 (Objective Weight Matrix)

| 配置 ID | `w_consolidation` | `w_headroom` | `headroom_upper_bound_pct` | 預期行為 |
|---|---|---|---|---|
| **REF** | — | — | — | Excel LP 純 min(BM) 結果,作為理論下界 |
| **DEFAULT** | 10 | 8 | 90 | 目前線上設定 (baseline) |
| **C1** | 10 | 8 | **95** | 最小改動:只放寬 threshold |
| **B1** | **20** | **2** | **85** | 中度 packing (crossover ≈ 95%) |
| **A1** | **100** | **1** | **95** | 接近 Excel (crossover ≈ 95%,packing 優先) |
| **NO-HR** | 10 | **0** | — | 完全關掉 headroom,純 consolidation |

> 註: 每組配置其他參數維持預設 (`w_slot_score=0`, `w_resource_waste=5`,
> `max_solve_time_seconds=30`)。

---

## 7. 結果回填表

> 每個輸入場景複製一份下表,把對應數字填入。
> Excel LP (REF) 那一列可只填 `bm_used_count` 與 `max_utilization_pct`,其他可留白。

### 場景 S1 — small-loose

| 配置 | `bm_used` | `max_util%` | `headroom_violation` | `worst_ag_load` | `remaining_slots` | `solve_time` | `status` | `unplaced` |
|---|---|---|---|---|---|---|---|---|
| REF (Excel) |  |  | n/a | n/a | n/a | n/a | n/a | n/a |
| DEFAULT |  |  |  |  |  |  |  |  |
| C1 |  |  |  |  |  |  |  |  |
| B1 |  |  |  |  |  |  |  |  |
| A1 |  |  |  |  |  |  |  |  |
| NO-HR |  |  |  |  |  |  |  |  |

### 場景 S2 — small-tight

| 配置 | `bm_used` | `max_util%` | `headroom_violation` | `worst_ag_load` | `remaining_slots` | `solve_time` | `status` | `unplaced` |
|---|---|---|---|---|---|---|---|---|
| REF (Excel) |  |  | n/a | n/a | n/a | n/a | n/a | n/a |
| DEFAULT |  |  |  |  |  |  |  |  |
| C1 |  |  |  |  |  |  |  |  |
| B1 |  |  |  |  |  |  |  |  |
| A1 |  |  |  |  |  |  |  |  |
| NO-HR |  |  |  |  |  |  |  |  |

### 場景 S3 — medium-mixed

| 配置 | `bm_used` | `max_util%` | `headroom_violation` | `worst_ag_load` | `remaining_slots` | `solve_time` | `status` | `unplaced` |
|---|---|---|---|---|---|---|---|---|
| REF (Excel) |  |  | n/a | n/a | n/a | n/a | n/a | n/a |
| DEFAULT |  |  |  |  |  |  |  |  |
| C1 |  |  |  |  |  |  |  |  |
| B1 |  |  |  |  |  |  |  |  |
| A1 |  |  |  |  |  |  |  |  |
| NO-HR |  |  |  |  |  |  |  |  |

### 場景 S4 — large-prod-like

| 配置 | `bm_used` | `max_util%` | `headroom_violation` | `worst_ag_load` | `remaining_slots` | `solve_time` | `status` | `unplaced` |
|---|---|---|---|---|---|---|---|---|
| REF (Excel) |  |  | n/a | n/a | n/a | n/a | n/a | n/a |
| DEFAULT |  |  |  |  |  |  |  |  |
| C1 |  |  |  |  |  |  |  |  |
| B1 |  |  |  |  |  |  |  |  |
| A1 |  |  |  |  |  |  |  |  |
| NO-HR |  |  |  |  |  |  |  |  |

### 場景 S5 — anti-affinity-heavy

| 配置 | `bm_used` | `max_util%` | `headroom_violation` | `worst_ag_load` | `remaining_slots` | `solve_time` | `status` | `unplaced` |
|---|---|---|---|---|---|---|---|---|
| REF (Excel) |  |  | n/a | n/a | n/a | n/a | n/a | n/a |
| DEFAULT |  |  |  |  |  |  |  |  |
| C1 |  |  |  |  |  |  |  |  |
| B1 |  |  |  |  |  |  |  |  |
| A1 |  |  |  |  |  |  |  |  |
| NO-HR |  |  |  |  |  |  |  |  |

### 場景 S6 — single-ag (排除 anti-affinity)

| 配置 | `bm_used` | `max_util%` | `headroom_violation` | `worst_ag_load` | `remaining_slots` | `solve_time` | `status` | `unplaced` |
|---|---|---|---|---|---|---|---|---|
| REF (Excel) |  |  | n/a | n/a | n/a | n/a | n/a | n/a |
| DEFAULT |  |  |  |  |  |  |  |  |
| C1 |  |  |  |  |  |  |  |  |
| B1 |  |  |  |  |  |  |  |  |
| A1 |  |  |  |  |  |  |  |  |
| NO-HR |  |  |  |  |  |  |  |  |

### 場景 S7 — mixed-tshirt

| 配置 | `bm_used` | `max_util%` | `headroom_violation` | `worst_ag_load` | `remaining_slots` | `solve_time` | `status` | `unplaced` |
|---|---|---|---|---|---|---|---|---|
| REF (Excel) |  |  | n/a | n/a | n/a | n/a | n/a | n/a |
| DEFAULT |  |  |  |  |  |  |  |  |
| C1 |  |  |  |  |  |  |  |  |
| B1 |  |  |  |  |  |  |  |  |
| A1 |  |  |  |  |  |  |  |  |
| NO-HR |  |  |  |  |  |  |  |  |

---

## 8. 解讀指引

填完後,依下列順序判讀:

1. **`DEFAULT` vs `REF` 的 BM gap** — 確認問題大小;若 gap 很小,代表 Excel 贏的幅度被高估。
2. **`NO-HR` vs `REF` 的 BM gap** — 若仍有差距,代表 anti-affinity 是主因 (非 headroom)。
3. **`S6` 結果** — 在無 anti-affinity 場景中,`A1` 或 `NO-HR` 應該逼近 `REF`。
   若仍有 gap → solver time 或其他項作怪。
4. **`worst_ag_load` 變化** — packing 越積極,單一 AG 上 VM 越多,失效衝擊越大,這是換來省 BM 的代價。
5. **`max_util%` 與 `headroom_violation`** — 觀察省下來的 BM 是不是用過熱換來的。

---

## 9. 結論與建議 (待填)

> 完成測試後,於此處填入:
>
> - 哪一組配置在「BM 數逼近 Excel」與「容錯/headroom 不顯著惡化」之間最平衡
> - 是否需要依場景動態調整 (e.g. 緊湊容量用 A1,寬鬆用 DEFAULT)
> - Anti-affinity 是否需要改為軟約束 (若 §8 第 2 點顯示它是主因)
> - 是否建議改用字典序 (lexicographic) 求解取代加權,讓「先 min BM、再 min headroom」的優先順序更可解釋

---

## 10. Open Questions

- 是否需要把 Excel LP 完整移植成 Python (e.g. PuLP) 以便 CI 自動跑 REF?
- 是否需要新增 KPI:`vms_displaced_on_single_bm_failure` (單一 BM 損失衝擊)?
- `w_resource_waste` 在這次實驗中保持預設,是否需要納入 sensitivity sweep?
