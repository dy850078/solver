# 為什麼引入 CP-SAT Solver？—— Go 排程器能力邊界與整合效益

> **對象**：Kubernetes 叢集排程組的工程師
> **目的**：釐清 Go 排程器的實際能力與邊界，說明哪些問題真正需要 CP-SAT，
> 以及整合 Solver 對開發者帶來的具體效益

---

## 一、背景：Go 排程器的 Snapshot 機制

Go 排程器並非原始的輪詢（Round-Robin）。它在每次排程批次（scheduling batch）開始時，
會建立一份**資源快照（snapshot）**，記錄當下所有 BM 的可用容量與狀態。
排程過程中每分配一台 VM，就即時更新這份快照中的已用資源，確保後續的 VM 不會被排到
同一批次中已超量的 BM。

```
批次開始：建立 Snapshot
           BM-A: CPU 32 可用 / BM-B: CPU 16 可用 / BM-C: CPU 8 可用

分配 VM-1 (8 CPU) → BM-A
  Snapshot 更新：BM-A: CPU 24 可用

分配 VM-2 (12 CPU) → BM-B
  Snapshot 更新：BM-B: CPU 4 可用

分配 VM-3 (6 CPU) → ?  ← Snapshot 知道 BM-B 已不夠，自動略過
  → BM-C (8 可用 ≥ 6 ✓)
```

### Go 排程器（含 Snapshot）能處理的問題

憑藉 Snapshot，Go 排程器在**單一批次**內可以正確處理：

| 能力 | 說明 |
|------|------|
| **資源容量約束** | CPU / Memory / Disk / GPU 不超量 |
| **候選清單過濾** | 尊重 Step 3 篩出的合法 BM 清單（IP、網段、硬體型號） |
| **AG 反親和分散** | 同批次內的 Master VM 分散到不同 AG |
| **BM VM 數量上限** | 同批次內不超過 BM 的 max_vm_count 政策 |
| **部分排程回報** | 資源不足時回報哪些 VM 放不下 |

這些能力已足以應付**單一叢集、單一批次**的排程需求。

---

## 二、真正的問題：Go 排程器做不到的兩件事

Snapshot 機制的關鍵限制是：**它只看得到「這次批次」的資訊**。
現實環境中有兩類問題超出了這個邊界，且在純 Go 實作上「困難到不合理」。

---

### 問題 A：跨批次排程約束（Cross-Scheduling-Batch Constraints）

**Snapshot 是批次內的狀態，它看不到其他批次、其他叢集留下的歷史排程結果。**

#### 情境：跨叢集拓撲反親和性

```
昨天：排程 Cluster-A
  → Master VM 全部排入 DC-1（合法，當時沒有衝突）

今天：排程 Cluster-B，政策要求「Cluster-B 不能與 Cluster-A 在同一 DC」
  → Go 排程器建立新 Snapshot
  → Snapshot 只記錄今天這批 VM 的狀態
  → Snapshot 完全不知道昨天 Cluster-A 已在 DC-1
  → Cluster-B 的 VM 被排進 DC-1 ← 違反跨叢集隔離政策
```

這不是 Bug，而是 Snapshot 設計的必然結果：**Snapshot 的生命週期是一個批次，跨批次的歷史狀態不在它的視野內。**

#### 情境：跨叢集親和性（Co-location）

```
Cluster-A 已在 DC-2 有大量 VM（昨天排程）
今天排程 Cluster-B，希望「靠近 Cluster-A 以降低延遲」
→ Go Snapshot 不知道 Cluster-A 在哪裡
→ 無法偏好 DC-2，只能隨機或輪詢分配
```

#### 情境：跨叢集 BM VM 數量統計

```
BM-X 已有 Cluster-A 的 3 個 VM（歷史批次）
今天排程 Cluster-B，BM-X 的 max_vm_count = 4
→ Go Snapshot 只看到「今天這批」分配了幾個
→ 不知道歷史上已有 3 個 VM，可能再分配 4 個 → 共 7 個，超出上限
```

**本質問題**：排程約束的生命週期跨越了多個獨立的批次（甚至多個叢集），
而 Snapshot 的設計邊界在單一批次內。
**在不引入外部求解器的情況下，Go 需要自行維護全域歷史狀態、手動實作回溯搜尋，複雜度呈指數級增長。**

---

### 問題 B：柔性約束（Soft Constraints）極難實作

硬性約束（Hard Constraint）很好描述：「條件不滿足就失敗」。
但排程中大量的需求是**柔性的**：「盡量滿足，實在不行就退而求其次」。

#### 純 Go 實作柔性約束的困境

**方法一：貪心啟發（Greedy Heuristic）**

```go
// 先試「理想選擇」，失敗就退而求其次
for _, bm := range preferredBMs {
    if canPlace(vm, bm) {
        place(vm, bm)
        break
    }
}
```

問題：
- 貪心決策不可逆，前面 VM 的選擇可能讓後面 VM 陷入困境
- 無法保證「整批 VM 的柔性規則總滿足度最大」
- 新增一個柔性規則 = 重寫選擇邏輯，規則組合爆炸

**方法二：回溯搜尋（Backtracking）**

```go
func tryAllCombinations(vms []VM, bms []BM) Assignment {
    // 嘗試所有可能的分配組合，選出最高分的
    // 時間複雜度：O(|BM|^|VM|) ← 100 BM × 50 VM = 100^50 種組合
}
```

問題：
- 計算複雜度不可接受（指數級）
- 需要自行實作剪枝（pruning）、搜尋策略
- 等同於自己實作一個求解器，且效率遠不如成熟工具

**方法三：多次分配 + 評分**

```go
// 試多種分配方案，選最高分的
for i := 0; i < maxAttempts; i++ {
    attempt := randomAssignment(vms, bms)
    score := evaluate(attempt, softRules)
    if score > bestScore { best = attempt }
}
```

問題：
- 結果非確定性，不可重現
- 無法保證找到最優解
- 柔性規則的優先順序（weight）難以正確反映在最終分數上

**多個柔性規則同時存在時的組合難題**：

```
軟規則 1：Cluster-B 偏好與 Cluster-A 同 DC（weight=10）
軟規則 2：Cluster-B 偏好遠離 Cluster-C 的 rack（weight=5）
軟規則 3：盡量讓 VM 分散到不同 BM（weight=3）

問題：當規則 1 和規則 2 互相衝突時，正確的 trade-off 是什麼？
純 Go 手寫邏輯幾乎無法表達「在不違反規則 1 的前提下，盡量滿足規則 2」。
```

---

## 三、CP-SAT Solver 的解法

本專案以 **Python sidecar 服務**的形式整合 CP-SAT 求解器。
Go 排程器負責收集所有狀態（包括歷史排程、其他叢集現況），
Solver 負責在滿足所有約束的前提下找出最優分配。

### 解法 A：跨批次約束 — 外部狀態注入

Go 排程器在呼叫 Solver 前，主動查詢並組裝「跨批次的全域狀態」，
注入 `PlacementRequest` 中：

```json
{
  "existing_vms": [
    {
      "vm_id": "vm-cluster-a-master-1",
      "cluster_id": "cluster-a",
      "baremetal_id": "bm-42",
      "topology": { "datacenter": "dc-1", "rack": "rack-3", ... }
    }
  ],
  "topology_rules": [
    {
      "rule_id": "a-b-anti-affinity",
      "cluster_ids": ["cluster-a", "cluster-b"],
      "scope": "datacenter",
      "type": "anti_affinity",
      "enforcement": "hard"
    }
  ]
}
```

Solver 從 `existing_vms` 建立拓撲佔用索引，配合 `topology_rules` 加入對應約束，
讓「歷史批次的排程結果」對今天的分配產生約束效果。

**架構分工**：跨批次的歷史狀態收集由 Go 排程器負責，Solver 本身保持無狀態（stateless）。

#### 跨叢集硬性反親和：直接封鎖

```
∀ BM-j 所在 zone ∈ 其他叢集已占用 zones:
    assign[vm_i, bm_j] = 0  （該變數直接從模型中移除）
```

#### 跨叢集 BM VM 數統計：全域計數

```
∀ BM-j:
    current_vm_count（所有叢集歷史 VM 數）+ Σ assign[vm_i, bm_j] ≤ max_vm_count
```

`current_vm_count` 由 Go 從 Inventory API 拿到後填入，Solver 直接使用。

---

### 解法 B：柔性約束 — 目標函數建模

CP-SAT 的目標函數天生支援「加權偏好」：

```
Maximize:
  Σ placement_count × 1000           ← 放置數量（最高優先，大權重）
  + Σ affinity_satisfied × weight    ← 親和性滿足（正分獎勵）
  - Σ anti_affinity_violated × weight ← 反親和違反（負分懲罰）
```

多個柔性規則的優先順序透過 `weight` 精確表達，不需要手寫 trade-off 邏輯。

#### 兩階段求解確保硬性優先

當同時有部分排程和柔性規則時：

```
Phase 1: Maximize 放置數 → 得到最優數量 N
         ↓ 固定：已放置數 == N
Phase 2: Maximize 柔性規則總分（在不犧牲放置數的前提下）
```

這確保「多放一台 VM 永遠優先於滿足任何柔性偏好」，不需要人工調整 weight 數值。

---

## 四、約束對照總表

| 約束類型 | Go 排程器（含 Snapshot）| CP-SAT Solver | 關鍵差異 |
|---------|----------------------|--------------|---------|
| BM 資源容量 | ✅ 批次內正確處理 | ✅ 線性不等式約束 | Go 已能做，Solver 同樣支援 |
| 候選清單過濾 | ✅ Step 3 篩選 | ✅ 預篩選變數 | Go 已能做，Solver 同樣支援 |
| AG 反親和（批次內） | ✅ Snapshot 追蹤 | ✅ per-AG 計數約束 | Go 已能做，Solver 同樣支援 |
| BM VM 數上限（批次內） | ✅ Snapshot 追蹤 | ✅ 計數不等式約束 | Go 已能做，Solver 同樣支援 |
| **跨批次拓撲反親和（Hard）** | ❌ Snapshot 看不到歷史 | ✅ `existing_vms` 注入 + 硬約束 | **Solver 解決** |
| **跨批次拓撲反親和（Soft）** | ❌ 指數級複雜度 | ✅ 目標函數懲罰項 | **Solver 解決** |
| **跨批次拓撲親和（Soft）** | ❌ 無法最優化 | ✅ 目標函數獎勵項 | **Solver 解決** |
| **多柔性規則 Trade-off** | ❌ 手寫邏輯爆炸 | ✅ 加權目標函數 | **Solver 解決** |

---

## 五、對開發者的效益：為什麼 Solver 讓實作更簡單？

這一節說明整合 CP-SAT 後，開發者在**新增功能、維護、測試**上獲得的具體好處。

---

### 效益 1：宣告式（Declarative）約束 — 描述「要什麼」而非「怎麼找」

純 Go 實作排程邏輯時，開發者需要同時思考：
- **業務規則**（VM 不能超出容量）
- **搜尋策略**（先試哪台 BM？衝突了怎麼回溯？）
- **狀態追蹤**（Snapshot 怎麼更新？邊界條件？）

CP-SAT 把後兩者交給求解器，開發者只需描述業務規則：

```python
# 新增「BM VM 數量上限」約束 — 3 行完成
for bm in baremetals:
    if bm.max_vm_count:
        model.Add(bm.current_vm_count + sum(assign[vm, bm]) <= bm.max_vm_count)
```

對比等效的 Go 實作，需要：
- 在 Snapshot 中追蹤 current_vm_count
- 在每次分配時檢查並更新
- 處理批次結束時的邊界清理
- 考慮併發安全

**每條新約束 = 幾行 Python，而非一個子系統。**

---

### 效益 2：柔性約束免費獲得最優性

純 Go 的柔性邏輯只能給出「不錯的解」，無法保證最優。
CP-SAT 在 timeout 內搜尋**全局最優解**，並回報是 `OPTIMAL`（保證最優）或 `FEASIBLE`（時間不夠但可用）。

```python
# 柔性親和規則 — 加一個目標項即可
objective_terms.append((colocation_indicator, rule.weight))
model.Maximize(sum(var * w for var, w in objective_terms))
```

開發者不需要思考「這個啟發式好不好」，求解器負責找最好的。

---

### 效益 3：新增約束不破壞現有邏輯

CP-SAT 的約束是**加法性（additive）**的：

```
現有約束集合 {容量, AG反親和, 候選清單}
    + 新約束：跨叢集拓撲反親和
    = 新約束集合（自動與所有現有約束共同作用）
```

純 Go 的情況：新增跨叢集反親和需要修改 Snapshot 結構、分配主迴圈的判斷邏輯、
回報結構，以及確保不與 AG 反親和的邏輯互相干擾。

**CP-SAT：加一個 `model.Add()` 調用。Go：可能動到多個子系統。**

---

### 效益 4：測試與業務規則一一對應

CP-SAT 約束直接對應業務規則，讓測試案例也能直接對應：

```python
# 測試「BM VM 數量上限」— 業務語義清晰
def test_count_limit_respected():
    bm = make_bm("bm-1", max_vm_count=2, current_vm_count=1)
    vms = [make_vm("vm-1"), make_vm("vm-2")]   # 2 個新 VM
    result = solve(vms, [bm])
    assert not result.success  # 1 + 2 = 3 > 2，必須失敗

# 測試「跨叢集硬性反親和」— 場景直觀
def test_blocks_same_dc():
    # 設定：BM-1 在 DC-1（已被 Cluster-B 佔用），BM-2 在 DC-2
    # 規則：Cluster-A 與 Cluster-B 反親和@datacenter
    result = solve(vms_cluster_a, [bm_dc1, bm_dc2], ...)
    assert assignment["vm-1"] == "bm-2"  # 必須避開 DC-1
```

測試直接描述業務場景，不需要了解排程演算法的內部細節。

---

### 效益 5：清晰的職責分工 — Go 做 Go 擅長的事

整合後的架構分工明確：

```
Go 排程器                          Python Solver Sidecar
─────────────────────              ──────────────────────────
• Kubernetes API 互動              • 約束建模（CP-SAT 模型）
• Inventory API 查詢               • 最優化搜尋
• VM 創建 / 刪除 / 更新             • 柔性規則 Trade-off
• 歷史狀態收集（existing_vms）      • 衝突檢測與診斷
• 候選清單篩選（Step 3）            • 兩階段求解
• 錯誤處理與重試                    • 結果回報
• 排程批次協調
```

Go 用它最擅長的方式處理系統整合、API 呼叫、並發控制。
Solver 用成熟的數學工具處理組合最優化。
兩者透過定義清晰的 JSON API 解耦，可以獨立開發、測試、部署。

---

### 效益 6：自帶診斷能力，降低排程失敗的 Debug 成本

Solver 回傳結構化的診斷資訊，讓排程失敗不再是黑盒：

```json
{
  "success": false,
  "solver_status": "MODEL_INVALID",
  "diagnostics": {
    "error": "Topology rule conflict: affinity rule 'r1' at scope 'rack' conflicts with anti-affinity rule 'r2' at scope 'datacenter'",
    "warnings": [
      { "type": "enforcement_downgraded", "rule_id": "r3", "reason": "affinity rules cannot be hard; downgraded to soft" },
      { "type": "redundant_rule_filtered", "rule_id": "r4", "reason": "coarser than r2 for same cluster pair; filtered out" }
    ]
  }
}
```

純 Go 要達到同等診斷品質，需要在每個分支加入人工的錯誤分析邏輯。

---

## 六、完整求解流程

```
Go Scheduler
  │  1. 查 Inventory API：BM 容量、角色、歷史 VM 數
  │  2. 執行 Step 3：取得候選清單
  │  3. 查詢其他叢集的 existing_vms 及 topology_rules
  │
  │  POST /v1/placement/solve
  ▼  { vms, baremetals, existing_vms, topology_rules, config }
┌────────────────────────────────────────────────────────┐
│  Python Solver Sidecar                                 │
│                                                        │
│  Phase 0：規則驗證                                      │
│    ├─ Hard 親和 → 降為 Soft（+ 警告）                   │
│    ├─ 衝突檢測 → MODEL_INVALID（親和 scope ≤ 反親和）   │
│    └─ 冗餘過濾 → 保留最細粒度（+ 警告）                 │
│                                                        │
│  Phase 1：建立 CP-SAT 硬約束                            │
│    ├─ 每個 VM 恰好分配一台 BM（or ≤ 1 if 部分排程）     │
│    ├─ BM 資源容量不超量（CPU/Mem/Disk/GPU）              │
│    ├─ AG 反親和分散（per-AG 計數上限）                  │
│    ├─ BM VM 數量上限（歷史 + 新增 ≤ max_vm_count）      │
│    └─ 跨批次拓撲反親和 Hard（直接封鎖對應變數）          │
│                                                        │
│  Phase 2：建立目標函數                                   │
│    ├─ Soft 反親和懲罰項（-weight per 違反）             │
│    └─ Soft 親和獎勵項（+weight per 滿足）               │
│                                                        │
│  Phase 3：求解                                          │
│    ├─ 部分排程 + Soft 規則 → 兩階段求解                 │
│    └─ 其他 → 單階段求解                                 │
│                                                        │
│  Phase 4：回傳                                          │
│    ├─ assignments: [{vm_id, bm_id, ag}]               │
│    ├─ unplaced_vms: [vm_id, ...]                      │
│    ├─ solver_status: OPTIMAL / FEASIBLE / INFEASIBLE  │
│    └─ diagnostics: { warnings, errors }               │
└────────────────────────────────────────────────────────┘
  │
  ▼
Go Scheduler 依據 assignments 執行 VM 創建
```

---

## 七、總結

| 面向 | 純 Go 排程器（含 Snapshot）| 整合 CP-SAT Solver 後 |
|------|--------------------------|----------------------|
| **批次內容量 / 候選清單** | ✅ 正確處理 | ✅ 同樣正確 |
| **跨批次 / 跨叢集約束** | ❌ Snapshot 看不到歷史 | ✅ 外部狀態注入 + 硬約束 |
| **柔性規則** | ❌ 啟發式，非最優 | ✅ 全域最優加權目標函數 |
| **多規則 Trade-off** | ❌ 手寫邏輯複雜度爆炸 | ✅ 自動 weight 平衡 |
| **新增約束成本** | 高（可能動多個子系統） | 低（新增 `model.Add()` 呼叫） |
| **測試可讀性** | 需了解演算法細節 | 直接描述業務場景 |
| **失敗診斷** | 需手工埋點 | 內建結構化診斷 |
| **職責清晰度** | 排程邏輯與系統整合混雜 | Go 做整合，Solver 做最優化 |

**一句話總結**：
Go 排程器已能在批次內正確處理大多數約束，
真正需要 CP-SAT 的是**跨批次的歷史狀態約束**和**多目標柔性最優化**。
而整合 Solver 的附加效益是：讓每條業務規則直接對應一段程式碼，
新需求不再是演算法挑戰，而只是新的 `model.Add()` 呼叫。

---

*文件版本：1.1 | 更新日期：2026-03-10*
