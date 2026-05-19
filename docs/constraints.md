# Constraints 設計文件

## 總覽

Solver 使用 CP-SAT 的硬約束（constraints）保證每個解都是**正確的**。與目標函數（soft objective）不同，硬約束不可違反 — 違反任何一條，solver 回傳 INFEASIBLE。

### 決策變數

```
assign[(vm_id, bm_id)] ∈ {0, 1}
```

- `1` = 此 VM 放在此 BM 上
- `0` = 不放
- 只對 **eligible pairs** 建立變數（見下節），不符資格的 (VM, BM) 組合連變數都不存在

### 約束總表

| 代號 | 名稱 | 類型 | 說明 |
|------|------|------|------|
| — | Eligible Pairs | 前置篩選 | 決定哪些 (VM, BM) 對建立變數 |
| C1 | One-BM-per-VM | 指派約束 | 每個 VM 恰好放在一台 BM |
| C2 | Capacity | 容量約束 | 每台 BM 各維度不超量 |
| C3 | Anti-Affinity | 分散約束 | 同 `(cluster_id, ip_type, role)` 群組 VM 跨 AG 分散 |
| C4 | Max per Baremetal | 上限約束 | 同 `(cluster_id, ip_type, role)` 群組 VM 單台 BM 上限 |

---

## 前置篩選：Eligible Pairs

> 來源：`solver.py` — `_get_eligible_baremetals()`

在建立 CP-SAT 變數之前，先篩選出合理的 (VM, BM) 配對。這不是約束，而是**搜尋空間縮減** — 減少變數數量讓 solver 更快。

### 篩選邏輯

```
if vm.candidate_baremetals 不為空:
    eligible = [bm for bm in candidate_baremetals
                if bm 存在 且 vm.demand fits_in bm.available_capacity]
else:
    eligible = [bm for bm in all_baremetals
                if vm.demand fits_in bm.available_capacity]
```

兩條路徑：

| 情境 | 行為 |
|------|------|
| Go scheduler 提供了 `candidate_baremetals` | 只考慮候選清單中容量夠的 BM |
| 未提供 `candidate_baremetals` | 考慮所有容量夠的 BM |

### 為什麼有效

```
100 VMs × 50 BMs = 5,000 種組合（全建立變數）

經過篩選後：
  - candidate list 限制每個 VM 平均 10 台候選
  - capacity check 再淘汰部分
→ 可能只剩 ~500 個 eligible pairs（減少 90%）
```

變數越少，solver 搜尋空間越小，求解越快。

### 與 Capacity Constraint (C2) 的關係

前置篩選檢查的是 `available_capacity`（靜態，不考慮本次新放入的 VM）。C2 約束檢查的是**累計**放入後的總使用量。兩者是互補的兩層防線：

```
前置篩選：VM-A 需 16 CPU，BM-1 有 20 CPU available → eligible ✓
C2 約束：  VM-A(16) + VM-B(8) = 24 > 20 → 不可同時放在 BM-1 ✗
```

---

## C1: One-BM-per-VM（唯一指派）

> 來源：`solver.py` — `_add_one_bm_per_vm_constraint()`

### 公式

**標準模式**（`allow_partial_placement = false`）：

```
∀ vm_i:  Σ assign[vm_i, bm_j] = 1    (j ∈ eligible BMs of vm_i)
```

每個 VM **必須**恰好放在一台 BM 上。

**Partial placement 模式**（`allow_partial_placement = true`）：

```
∀ vm_i:  Σ assign[vm_i, bm_j] ≤ 1    (j ∈ eligible BMs of vm_i)
```

每個 VM **最多**放在一台 BM 上，允許不放（搭配 objective P0 的 -1,000,000 獎勵確保盡量多放）。

### 邊界情況：無 eligible BM

| 模式 | 行為 |
|------|------|
| 標準模式 | `model.add(0 == 1)` 強制 INFEASIBLE，因為此 VM 無處可放 |
| Partial placement | `continue` 跳過，此 VM 自然進入 unplaced_vms |

### 計算範例

VM-1 的 eligible BMs = [BM-A, BM-B, BM-C]：

```
assign[VM-1, BM-A] + assign[VM-1, BM-B] + assign[VM-1, BM-C] == 1
```

| 方案 | A | B | C | 合計 | 合法？ |
|------|---|---|---|------|--------|
| 放在 BM-A | 1 | 0 | 0 | 1 | ✓ |
| 放在 BM-B | 0 | 1 | 0 | 1 | ✓ |
| 放在兩台 | 1 | 1 | 0 | 2 | ✗ |
| 都不放 | 0 | 0 | 0 | 0 | ✗（標準）/ ✓（partial） |

---

## C2: Capacity（容量約束）

> 來源：`solver.py` — `_add_capacity_constraints()`

### 公式

```
∀ bm_j, ∀ resource_d:
    Σ (demand_d[vm_i] × assign[vm_i, bm_j]) ≤ available_d[bm_j]
    (i ∈ all VMs where (vm_i, bm_j) is eligible)
```

其中 `available_d = total_d - used_d`。

### 資源維度

| 欄位 | 說明 | 單位 |
|------|------|------|
| `cpu_cores` | CPU 核心數 | 個 |
| `memory_mib` | 記憶體 | MiB |
| `storage_gb` | 儲存空間 | GB |
| `gpu_count` | GPU 數量 | 個 |

每個維度獨立建立一條約束。任何一個維度超量就違反約束。

### 計算範例

BM-1：total 64 CPU，used 8 CPU → available 56 CPU

3 個 VM eligible on BM-1：

```
VM-A: 16 CPU    VM-B: 8 CPU    VM-C: 32 CPU

約束: 16 × assign[A, BM-1] + 8 × assign[B, BM-1] + 32 × assign[C, BM-1] ≤ 56
```

| 方案 | A | B | C | 總需求 | ≤ 56？ |
|------|---|---|---|--------|--------|
| 放 A+B+C | 1 | 1 | 1 | 56 | ✓（剛好） |
| 放 A+C | 1 | 0 | 1 | 48 | ✓ |
| 再加 VM-D(16) | 1 | 1 | 1+D | 72 | ✗ |

多維度情境 — 即使 CPU 夠，memory 不夠也不行：

```
BM-1: available = 56 CPU / 64,000 MiB
VM-A: demand = 16 CPU / 32,000 MiB
VM-B: demand = 16 CPU / 48,000 MiB

CPU:    16 + 16 = 32 ≤ 56    ✓
Memory: 32,000 + 48,000 = 80,000 > 64,000  ✗  → 不可同時放
```

---

## C3: Anti-Affinity（AG 分散）

> 來源：`solver.py` — `_add_anti_affinity_constraints()`、`_resolve_anti_affinity_rules()`

### 公式

```
∀ rule_r, ∀ ag_k:
    Σ assign[vm_i, bm_j] ≤ max_per_ag[r]
    (i ∈ rule_r.vm_ids,  j ∈ BMs in ag_k,  where (vm_i, bm_j) is eligible)
```

對每條 anti-affinity rule 的每個 AG，限制該 rule 中的 VM 在此 AG 內的數量不超過 `max_per_ag`。

### 規則來源

Anti-affinity rules 有兩種來源：

#### (a) 顯式規則

Go scheduler 在 request 中傳入的 `anti_affinity_rules`：

```json
{
  "group_id": "masters-ha",
  "vm_ids": ["vm-master-1", "vm-master-2", "vm-master-3"],
  "max_per_ag": 1
}
```

#### (b) 自動生成規則

當 `config.auto_generate_anti_affinity = true`（預設）時，solver 自動為具有相同 `(cluster_id, ip_type, node_role)` 的 VM 群組產生規則。

**Grouping key**：`(cluster_id, ip_type, node_role)`

```
cluster-A / routable / master    → 一組
cluster-A / routable / worker    → 一組
cluster-B / non-routable / infra → 一組
```

> **為什麼包含 `cluster_id`?**
> 同一台 BM 環境若同時部署多個 cluster，舊版只用 `(ip_type, node_role)` 會把
> 不同 cluster 的相同角色併進同一個 spread 預算。結果可能讓某個 cluster 的
> masters 集中在少數 AG，違反該 cluster 的 HA 要求。把 `cluster_id` 納入分組
> 鍵，每個 cluster 的 HA 就能獨立計算。

**max_per_ag 計算**：

```
max_per_ag = ceil(vm_count / ag_count)
```

**排除條件**：

| 條件 | 行為 |
|------|------|
| VM 已被顯式規則覆蓋 | 不重複產生 |
| VM 的 `ip_type` 為空 | 跳過（無法分組） |
| VM 的 `cluster_id` 為空 | 跳過（無法分組） |
| 群組只有 1 個 VM | 不產生（無需分散） |

#### (c) Selector 形式（簡化寫法）

不論顯式或 (透過 API 傳入的) 規則，都可改用 `selector` 替代逐一列舉 `vm_ids`。
`selector` 與 `vm_ids` 必須恰好提供一個。

```json
{
  "group_id": "A-masters",
  "selector": {
    "cluster_id": "cluster-A",
    "ip_type": "routable",
    "node_role": "master"
  },
  "max_per_ag": 1
}
```

`selector` 任何欄位設為 `null` 代表 wildcard：

| Selector | 命中範圍 |
|---|---|
| `{cluster_id="A", ip_type="non-routable", node_role=MASTER}` | cluster A 的 non-routable masters |
| `{cluster_id="A", node_role=MASTER}` | cluster A 的所有 masters |
| `{node_role=MASTER}` | 所有 cluster 的所有 masters |
| `{cluster_id="A"}` | cluster A 的全部 VMs |
| 全欄位為 `null` | **拒絕**（會 INPUT_ERROR） |

### 計算範例

#### 範例 1：3 master / 3 AG / max_per_ag=1

```
AG-1 的 BMs: [BM-1, BM-2]
AG-2 的 BMs: [BM-3]
AG-3 的 BMs: [BM-4]

約束（AG-1）: assign[m1, BM-1] + assign[m1, BM-2]
             + assign[m2, BM-1] + assign[m2, BM-2]
             + assign[m3, BM-1] + assign[m3, BM-2] ≤ 1

約束（AG-2）: assign[m1, BM-3] + assign[m2, BM-3] + assign[m3, BM-3] ≤ 1

約束（AG-3）: assign[m1, BM-4] + assign[m2, BM-4] + assign[m3, BM-4] ≤ 1
```

→ 每個 AG 最多 1 個 master → 3 個 master 分散在 3 個不同的 AG。

#### 範例 2：自動生成 — 5 routable worker / 3 AG

```
max_per_ag = ceil(5 / 3) = 2
```

| AG | 上限 | 可能放置 |
|----|------|----------|
| AG-1 | ≤ 2 | worker-1, worker-2 |
| AG-2 | ≤ 2 | worker-3, worker-4 |
| AG-3 | ≤ 2 | worker-5 |

→ 允許 2/2/1 分佈，保證不會出現 3/2/0 或 5/0/0。

#### 範例 3：候選不足導致 INFEASIBLE

```
3 master VMs, max_per_ag=1, 3 AGs
但 master VMs 的 candidate_baremetals 全都指向 AG-1 的 BMs
→ AG-1 最多放 1 個 master，其他 2 個無處可去
→ INFEASIBLE
```

---

## C4: Max per Baremetal（單台 BM 上限）

> 來源：`solver.py` — `_add_max_per_bm_constraints()`、`_resolve_max_per_bm_rules()`

### 公式

```
∀ rule_r, ∀ bm_j:
    Σ assign[vm_i, bm_j] ≤ max_per_bm[r]
    (i ∈ rule_r.vm_ids,  where (vm_i, bm_j) is eligible)
```

對每條 max-per-bm rule 的每台 BM，限制該 rule 中的 VM 在此 BM 內的數量不超過 `max_per_bm`。

### 與 C3 的差別

| | C3（Anti-Affinity） | C4（Max per BM） |
|---|---|---|
| 分散單位 | AG（多台 BM 組成的虛擬群組） | 單台 BM |
| 用途 | 跨 AG HA：避免整個 AG 故障時 cluster 全掛 | 跨主機 HA：避免單台 BM 故障時影響過多 node |
| 預設行為 | 自動生成（`auto_generate_anti_affinity = true`） | **opt-in**（`auto_generate_max_per_bm = false`） |
| 預設 cap | 動態計算 `ceil(n / num_ags)` | 由 `default_max_per_bm` 指定，無內建預設 |

兩者可同時生效，**互不取代**。

### 規則來源

#### (a) 顯式規則

```json
{
  "group_id": "A-nonrt-master",
  "selector": {
    "cluster_id": "cluster-A",
    "ip_type": "non-routable",
    "node_role": "master"
  },
  "max_per_bm": 1
}
```

或用 `vm_ids` 形式：

```json
{
  "group_id": "custom",
  "vm_ids": ["vm-1", "vm-2", "vm-3"],
  "max_per_bm": 2
}
```

#### (b) 自動生成規則

開啟 `config.auto_generate_max_per_bm = true` 時，solver 對具有相同
`(cluster_id, ip_type, node_role)` 的 VM 群組（大小 ≥ 2）產生規則，每條規則
套用 `config.default_max_per_bm`。

**必要條件**：`auto_generate_max_per_bm=true` 時 `default_max_per_bm` 必須為正整數，
否則 solver 回傳 `INPUT_ERROR`。

**排除條件**：與 C3 相同——已被顯式規則涵蓋的 VM、`cluster_id` / `ip_type` 為空的 VM、
單一 VM 群組均不參與 auto-gen。

### 計算範例

#### 範例 1：3 masters / 1 AG / 3 BMs / max_per_bm=1

```
所有 3 BM 在 ag-1
顯式規則: vm_ids=[m1,m2,m3], max_per_bm=1

約束（bm-1）: assign[m1,bm-1] + assign[m2,bm-1] + assign[m3,bm-1] ≤ 1
約束（bm-2）: 同上 ≤ 1
約束（bm-3）: 同上 ≤ 1
```

→ 每台 BM 最多 1 個 master，加上 C1（每 VM 必須放）→ 3 master 必在 3 台不同 BM。

#### 範例 2：跨 cluster 不互相影響

```
cluster-A：1 個 non-routable master
cluster-B：1 個 non-routable master
1 台 BM、auto_generate_max_per_bm=true、default_max_per_bm=1
```

→ Auto-gen 分組：
- `auto-bm/A/non-routable/master`：只有 1 個 VM → **不產生規則**
- `auto-bm/B/non-routable/master`：只有 1 個 VM → **不產生規則**

→ 兩個 master 都可放在同一台 BM ✓

#### 範例 3：與 C3 共存

```
cluster-A：3 個 routable masters
3 AG，每 AG 2 台 BM
auto_generate_anti_affinity=true、auto_generate_max_per_bm=true、default_max_per_bm=1

C3 自動生成：max_per_ag = ceil(3/3) = 1 → 3 master 各占一個 AG
C4 自動生成：max_per_bm = 1            → 同 AG 內不會擠同一台 BM
```

→ 兩個約束同時滿足：每個 AG 一個 master、每台 BM 至多一個 master。

### 失敗診斷

當 INFEASIBLE 時，`diagnostics.infeasible_max_per_bm_rules` 會列出結構性無解的規則：

```json
{
  "group_id": "strict-masters",
  "vm_count": 3,
  "max_per_bm": 1,
  "reachable_bms": 2,
  "slots_available": 2
}
```

`slots_available = max_per_bm × reachable_bms < vm_count` 表示即使最佳放置，BM 數量也容不下這個群組。

---

## 約束間的交互作用

### Candidate List × Anti-Affinity

Candidate list 縮小搜尋空間，可能讓 anti-affinity 無法滿足：

```
candidate_baremetals 只包含 AG-1 的 BM
+ max_per_ag = 1
+ 3 個 VM
→ AG-1 只能放 1 個，其他 2 個的 eligible list 為空
→ INFEASIBLE
```

**建議**：Go scheduler 產生 candidate list 時，確保涵蓋足夠多的 AG。

### Capacity × Anti-Affinity

容量約束可能讓 anti-affinity 的分佈方案不可行：

```
3 master VMs (each 32 CPU), max_per_ag=1, 3 AGs
AG-1: BM-1 (available 64 CPU)    → 放得下 ✓
AG-2: BM-2 (available 16 CPU)    → 放不下 ✗
AG-3: BM-3 (available 64 CPU)    → 放得下 ✓
→ AG-2 無法放任何 master → 只有 2 個 AG 可用
→ 但 3 master / max_per_ag=1 需要 3 個 AG → INFEASIBLE
```

### C3 × C4

C3 限制 AG 層級的分佈；C4 限制單 BM 層級的分佈。兩者**獨立**且**可疊加**：

```
3 routable masters / 3 AGs (每 AG 2 BM) / max_per_ag=1 / max_per_bm=1
→ C3 強制 3 master 各在不同 AG
→ C4 在每個 AG 內部也禁止疊放在同一台 BM（本例每 AG 只放 1 個，本就成立）
```

若兩者同時 auto-gen，分組鍵都是 `(cluster_id, ip_type, node_role)`，所以同一群 VM
會被兩條規則同時涵蓋——這是預期行為，不會衝突。

### Partial Placement × C1

`allow_partial_placement` 將 C1 從 `== 1` 放寬為 `<= 1`，讓 solver 在容量不足時仍能回傳部分解（而非 INFEASIBLE）。但放寬約束後需要 objective P0 的 -1,000,000 權重確保 solver 不會為了優化其他目標而主動放棄 VM。

```
標準模式:   必須放所有 VM，放不下 → INFEASIBLE
Partial 模式: 盡量放，放不下的進 unplaced_vms
```

---

## Config 參數速查

| 參數 | 影響的約束 | 說明 |
|------|-----------|------|
| `allow_partial_placement` | C1 | `true`: `≤ 1`（允許不放），`false`: `== 1`（必須放） |
| `auto_generate_anti_affinity` | C3 | `true`: 自動按 `(cluster_id, ip_type, node_role)` 產生 AG 分散規則 |
| `auto_generate_max_per_bm` | C4 | `true`: 自動按 `(cluster_id, ip_type, node_role)` 產生單 BM 上限規則（需設 `default_max_per_bm`） |
| `default_max_per_bm` | C4 | C4 auto-gen 時的預設上限；必為正整數 |
| `candidate_baremetals`（per VM） | Eligible Pairs | 限定此 VM 只考慮指定的 BM |
| `anti_affinity_rules`（per request） | C3 | 顯式指定的 AG 分散規則 |
| `max_per_bm_rules`（per request） | C4 | 顯式指定的單 BM 上限規則 |
