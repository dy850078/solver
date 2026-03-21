# Objective Function 設計文件

## 總覽

Solver 使用 CP-SAT 的 `Minimize` 目標函數，將多個優化目標加權組合成單一純量值。所有項目加總後越小越好。

```
Minimize(
    -1,000,000 × placed_count          # (僅 partial placement 模式)
  + w_consolidation × Σ bm_used[j]
  + w_headroom      × Σ headroom_penalty[j]
  - w_slot_score    × Σ slot_score[j]
)
```

## 優先級

各項目透過權重量級分離，確保高優先級永遠優先：

| 優先級 | 項目 | 權重量級 | 方向 | 說明 |
|--------|------|----------|------|------|
| P0 | Placed count | -1,000,000 | 越多越好 | 僅 `allow_partial_placement=true` 時啟用 |
| P1 | Consolidation | `w_consolidation` (預設 10) | 越少越好 | 最小化使用的 BM 數量 |
| P2 | Headroom | `w_headroom` (預設 8) | penalty 越小越好 | 避免單台 BM 利用率過高 |
| P3 | Slot score | `w_slot_score` (預設 0) | 越高越好 | 偏好剩餘空間仍可用的 BM |

> P0 的權重 1,000,000 遠大於其他項，確保「多放一個 VM」永遠優先於「少用一台 BM」。

---

## 各項目詳解

### 1. Partial Placement Priority (P0)

```python
if allow_partial_placement:
    terms.append(-1_000_000 * total_placed)
```

- **觸發條件**：`config.allow_partial_placement = true`
- **語義**：容量不夠放所有 VM 時，盡量放越多越好
- **機制**：每多放一個 VM，目標函數減少 1,000,000，遠超其他項的影響
- **搭配約束**：此模式下每個 VM 的約束從 `== 1` 放寬為 `<= 1`

### 2. Consolidation (P1)

```python
terms.append(w_consolidation * Σ bm_used[j])
```

- **觸發條件**：`w_consolidation > 0`
- **語義**：盡量把 VM 塞進少數幾台 BM，減少使用的機器數量
- **變數**：`bm_used[j]` 是 boolean — 只要有任何 VM 放在 BM-j 上就為 1

```
bm_used[j] = max(assign[vm_0, j], assign[vm_1, j], ...)
```

- **效果**：solver 會把 VM 集中放在已使用的 BM，而非分散到新的 BM

#### 計算範例

3 個 VM，2 台 BM 都夠放全部：

| 方案 | bm_used 數 | consolidation 成本 |
|------|-----------|-------------------|
| 全放 BM-1 | 1 | 10 × 1 = **10** |
| 分散兩台 | 2 | 10 × 2 = **20** |

→ solver 選方案 A（成本較低）

### 3. Headroom Penalty (P2)

```python
terms.append(w_headroom * Σ headroom_penalty[j])
```

- **觸發條件**：`w_headroom > 0`
- **語義**：避免任何一台 BM 的利用率超過安全上限（`headroom_upper_bound_pct`，預設 90%）
- **注意**：這是軟目標（penalty），不是硬約束。超過 90% 仍允許，但會被懲罰

#### 計算流程（每台 BM × 每個資源維度）

```
Step A: after_usage = (used + new_placement) × 100
Step B: util_pct = after_usage ÷ total              # 整數百分比 0–100
Step C: raw = util_pct - headroom_upper_bound_pct    # 可能為負
Step D: over = max(0, raw)                           # ReLU: 只計超過的部分
Step E: bm_penalty = max(over_cpu, over_mem, ...)    # 跨維度取最大值
```

> 使用整數乘 100 再除法，避免浮點運算（CP-SAT 只支援整數）。

#### 計算範例

BM 總量 64 CPU，已用 16，新放入 VM 需 40 CPU：

```
util_pct = (16 + 40) × 100 ÷ 64 = 87%
raw = 87 - 90 = -3
over = max(0, -3) = 0     → 無 penalty
```

若新放入 VM 需 48 CPU：

```
util_pct = (16 + 48) × 100 ÷ 64 = 100%
raw = 100 - 90 = 10
over = max(0, 10) = 10    → penalty = 10
```

### 4. Slot Score Bonus (P3)

```python
terms.append(-w_slot_score * Σ effective_slot_score[j])
```

- **觸發條件**：`w_slot_score > 0` 且 `slot_tshirt_sizes` 非空
- **語義**：偏好放置後剩餘空間仍能容納標準 VM（t-shirt size）的 BM
- **為什麼需要**：consolidation 只管「少用 BM」，但可能把 VM 塞進大機器，導致剩餘空間碎片化（例如每台都剩 3 CPU，無法再放任何 VM）

#### T-shirt Size 定義

由 Go scheduler 在 request 中提供，例如：

```json
"slot_tshirt_sizes": [
  {"cpu_cores": 4,  "memory_mib": 16000, "storage_gb": 100},
  {"cpu_cores": 8,  "memory_mib": 32000, "storage_gb": 200},
  {"cpu_cores": 16, "memory_mib": 64000, "storage_gb": 400}
]
```

#### 計算流程（每台 BM）

```
對每個 t-shirt size:
  對每個資源維度:
    remaining = total - used - new_placement
    slots_d = remaining ÷ tshirt_demand         # 此維度能放幾個
  slots_for_tshirt = min(slots_d across dims)    # 瓶頸維度決定
slot_score = Σ slots_for_tshirt across t-shirts  # 加總所有 size
```

#### 為什麼只計算被使用的 BM

```python
effective = bm_used[j] × bm_score[j]
```

如果不乘 `bm_used`，solver 會故意把 VM 全塞進小 BM，讓大 BM 完全空閒以獲得最高 slot score。乘上 `bm_used` 後，空閒的 BM 不貢獻分數，消除此偏差。

#### 計算範例

BM 總量 64 CPU / 256,000 MiB，已用 0，放入 1 個 VM (8 CPU / 32,000 MiB) 後：

```
剩餘: 56 CPU / 224,000 MiB

small  (4 CPU / 16,000 MiB):  min(56÷4, 224000÷16000) = min(14, 14) = 14
medium (8 CPU / 32,000 MiB):  min(56÷8, 224000÷32000) = min(7, 7)   = 7
large  (16 CPU / 64,000 MiB): min(56÷16, 224000÷64000) = min(3, 3)  = 3

slot_score = 14 + 7 + 3 = 24
```

---

## 項目交互作用

### Consolidation vs Headroom

兩者天然拉鋸：

- **Consolidation**：把 VM 塞滿少數 BM → 利用率拉高
- **Headroom**：限制單台 BM 的利用率 → 傾向分散

透過調整 `w_consolidation` 和 `w_headroom` 的比值來平衡：

| 場景 | 建議配置 |
|------|----------|
| 測試環境（省資源優先） | `w_consolidation=20, w_headroom=2` |
| 生產環境（穩定優先） | `w_consolidation=5, w_headroom=15` |
| 預設 | `w_consolidation=10, w_headroom=8` |

### Consolidation vs Slot Score

兩者通常互補：

- Consolidation 決定「用幾台 BM」
- Slot score 在同樣用 N 台 BM 的方案中，選擇「剩餘空間最有用」的方案

Slot score 作為 tiebreaker，通常 `w_slot_score` 應遠小於 `w_consolidation`。

---

## Config 參數速查

| 參數 | 型別 | 預設值 | 說明 |
|------|------|--------|------|
| `w_consolidation` | int | 10 | 每多用一台 BM 的懲罰 |
| `w_headroom` | int | 8 | 超過利用率上限的懲罰倍數 |
| `headroom_upper_bound_pct` | int | 90 | 利用率安全上限（百分比） |
| `w_slot_score` | int | 0 | slot score 獎勵倍數（0=停用） |
| `slot_tshirt_sizes` | list[Resources] | [] | 標準 VM 規格，需明確提供 |
| `allow_partial_placement` | bool | false | 是否允許只放部分 VM |
