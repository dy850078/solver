# Enhancement Proposal: Master / Learner Redundancy & Multi-dimension Spread

> **作者**: Claude Opus 4.7
> **日期**: 2026-05-19
> **狀態**: Draft (尚未實作)
> **相關分支**: `claude/master-learner-redundancy-ChL6a`

---

## Summary

針對未來 Master node 將分為 **Master**（一般 master）與 **Learner**（備援 master）兩種角色，且實體 topology 將由「1 AG = 1 實體 DC」轉變為「AG 是虛擬 DC、實體層多出 Room」的情境，本提案擴充 solver 以支援：

1. **新角色 `LEARNER`**：與 `MASTER` 平行，可被獨立分散與容量規劃
2. **多維度反親和**：同一群組可同時要求 by AG 分散 **AND** by Room 分散（兩者皆成立）
3. **跨群組 N-1 互補規則 `FailoverRule`**：表達「任一 fault domain（如 Room）失效時，倖存 fault domain 的 Learner 數量 ≥ 失效 fault domain 的 Master 數量」

設計原則：**API 表面向後相容**，內部資料結構與約束生成邏輯泛化。

---

## Goals / Non-Goals

### Goals

- 在 `NodeRole` 加入 `LEARNER`，且使既有 anti-affinity auto-generation（以 `(cluster_id, ip_type, node_role)` 為 group key）天然分開處理 master / learner 兩群
- 在 `Topology` 加入 `room` 維度
- 擴充 `AntiAffinityRule` 支援 `spread_on: list[str]`，能同時對多個 topology 維度做平均分散
- 新增 `FailoverRule(primary, backup, fault_domain, policy="n_minus_1")` 表達跨群組互補
- `SolverConfig.target_ag_spread` 泛化為 `target_spread: dict[str, int]`
- **保留 JSON 契約相容**：Go scheduler 既有送出的 payload 不需立即修改即可正確運作

### Non-Goals

- **不**負責 runtime 上的 Learner 升 Master 邏輯（屬 cluster control plane / scheduler 層）
- **不**改變 splitter 的 capacity 計算介面 — Master / Learner 數量由外部（Go scheduler / `ResourceRequirement`）決定
- **不**支援 N-2 以上的 redundancy policy（保留為未來擴充點）
- **不**做 quorum-based 容錯模型（同上）
- **不**修改現有 C2 (Capacity)、C4 (Max per BM) 約束

---

## Current State & Problem

### 現況

`models.py` 的關鍵結構：

```python
class Topology:
    site / phase / datacenter / rack / ag

class NodeRole:
    MASTER / WORKER / INFRA / L4LB

class AntiAffinityRule:
    group_id, vm_ids|selector, max_per_ag      # 鎖死 AG 一個維度

class SolverConfig:
    target_ag_spread: int = 3                  # 鎖死 AG 一個維度
```

`solver.py` 的關鍵耦合點：

- L125–127：`ag_to_bms` 字典於建構期一次建立，僅依 `bm.topology.ag` 分組
- L239–342：`_resolve_anti_affinity_rules()` auto-generation 邏輯，`max_per_ag = ceil(N / num_ags)` 寫死 AG 維度
- L564–625：`_add_anti_affinity_constraints()` 對 AG buckets 建約束
- `diagnostics.py` 輸出 `max_per_ag`、`ag_spread_below_target` 等對外欄位

### 痛點

#### 痛點 1：未來新需求是「**多維度同時**分散」

範例：2 Room、3 AG 場景，AG 跨 Room
```
Room1: rack-1(ag1), rack-2(ag2)
Room2: rack-3(ag2), rack-4(ag3)
```
對 5 個 master VM，期望同時滿足：
- by AG 分散：3 個 AG 各最多 ⌈5/3⌉ = 2 台
- by Room 分散：2 個 Room 各最多 ⌈5/2⌉ = 3 台

現有 `AntiAffinityRule` 一條規則只能表達一個 AG 維度。

#### 痛點 2：Master / Learner 是**對偶結構**而非獨立群組

簡單把 Learner 視為一個獨立 role 並分別做 spread 不夠 — 真正的需求是 **N-1 fault domain failure 後仍能補位**：

> 範例：10 台 master node，分 5M + 5L，2 Room
>   - Room1: 2M + 3L
>   - Room2: 3M + 2L
>   - Room2 整個失效 → Room1 剩 3L 站起來補 Room2 死掉的 3M ✓
>   - Room1 整個失效 → Room2 剩 2L 站起來補 Room1 死掉的 2M ✓

這要求 **「對每個 fault domain bucket b：不在 b 的 Learner 數 ≥ 在 b 的 Master 數」**，是跨群組的 coupling，不能由「Master 群分散規則」+「Learner 群分散規則」獨立達成。

#### 痛點 3：Fault domain 未來會擴及多種維度

使用者明示未來除 Room 外，也可能以 datacenter / ag / site / 其組合作為 fault domain。互補規則必須通用，不能寫死 Room。

---

## Proposed Design

### 1. 資料模型擴充

#### 1.1 `Topology` 新增 `room`

```python
class Topology(BaseModel):
    site: str = ""
    phase: str = ""
    datacenter: str = ""
    room: str = ""        # NEW
    rack: str = ""
    ag: str = ""
```

層級語意：`site > phase > datacenter > room > rack`，AG 為虛擬維度橫切其上。`rack` 同時隸屬唯一 `room` 與唯一 `ag`，`room` 與 `ag` 為兩個正交的反親和維度。

#### 1.2 `NodeRole` 新增 `LEARNER`

```python
class NodeRole(str, Enum):
    MASTER = "master"
    LEARNER = "learner"   # NEW
    WORKER = "worker"
    INFRA = "infra"
    L4LB = "l4lb-storage"
```

既有 auto-generated anti-affinity 的 group key `(cluster_id, ip_type, node_role)` 不變 — 自然會把 master 與 learner 切成兩個獨立群組做 spread。

#### 1.3 `AntiAffinityRule` 原地擴充 `spread_on`

**保留類名與既有欄位**，新增 `spread_on`：

```python
class AntiAffinityRule(BaseModel):
    group_id: str
    vm_ids: list[str] = Field(default_factory=list)
    selector: GroupSelector | None = None

    # NEW: 同時對這些 topology 維度做平均分散
    # 預設 ["ag"] 等同既有行為
    spread_on: list[str] = Field(default_factory=lambda: ["ag"])

    # 既有欄位，保留為 "spread_on == [ag]" 時的舊行為
    # 若 spread_on 含多維或非 ag，max_per_ag 被忽略（並寫 warning 到 diagnostics）
    max_per_ag: int = 1
```

**多維度語意**：對 `spread_on` 中每個維度 `d`，獨立加一條約束：

> 對每個 bucket b ∈ buckets(d)：`sum(assign[vm,bm] for bm in bucket b) ≤ ⌈N / |buckets(d)|⌉`

其中 N 為群組 VM 數，`|buckets(d)|` 為該維度下的桶數。多維度約束**獨立疊加**（AND），不是聯合分布。

**JSON 範例**（多維度）：

```json
{
  "group_id": "masters-A-routable",
  "selector": {"cluster_id": "A", "ip_type": "routable", "node_role": "master"},
  "spread_on": ["ag", "room"]
}
```

**JSON 範例**（向後相容，未寫 `spread_on` 等於 `["ag"]`）：

```json
{
  "group_id": "legacy-rule",
  "vm_ids": ["m1", "m2", "m3"],
  "max_per_ag": 1
}
```

#### 1.4 新增 `FailoverRule`

```python
class FailoverRule(BaseModel):
    """
    Cross-group N-1 redundancy constraint.

    語意：對 fault_domain 維度的每個 bucket b：
        sum(backup VMs not in b) ≥ sum(primary VMs in b)

    結果：當 b 整個失效，倖存 backup 數量必足以接替失效的 primary。
    """
    rule_id: str
    primary: GroupSelector       # e.g. master 群
    backup:  GroupSelector       # e.g. learner 群
    fault_domain: str            # 任一 topology 維度名: "room"|"ag"|"datacenter"|"site"
    policy: str = "n_minus_1"    # 目前僅實作此值；保留欄位給未來 "n_minus_2" 等
```

選擇 `GroupSelector` 而非 `vm_ids`：互補關係本質上是「角色配對」，selector 比枚舉 VM 更穩定且支援自動產生。

#### 1.5 `SolverConfig` 泛化 spread target

```python
class SolverConfig(BaseModel):
    ...
    # 既有：target_ag_spread: int = 3
    # 新：dict 鍵為 topology 維度名
    # 預設 {"ag": 3} 等同舊行為
    target_spread: dict[str, int] = Field(default_factory=lambda: {"ag": 3})
```

`target_ag_spread` 欄位保留為 deprecated alias，若同時設定則 dict 中的 `ag` 鍵優先。

### 2. 約束數學

#### C3a: Multi-dimension Anti-Affinity（取代既有 C3）

對每條規則 `r`，對 `r.spread_on` 中每個維度 `d`：

```
N_r = |VMs(r)|
B_d = buckets of dimension d (e.g. distinct ag values, or distinct room values)
cap = ⌈N_r / |B_d|⌉

∀ b ∈ B_d:  Σ assign[vm, bm]  ≤  cap
            vm ∈ VMs(r), bm ∈ b
```

舊規則的 `max_per_ag` 行為視為 `spread_on=["ag"]` 且 `cap = max_per_ag`（顯式覆寫 ceil 計算）。

#### C5: Failover Redundancy（新增）

對每條 `FailoverRule` `f`：

```
P = VMs matching f.primary
L = VMs matching f.backup
d = f.fault_domain
B_d = buckets of d

∀ b ∈ B_d:  Σ assign[vm, bm]      ≥  Σ assign[vm, bm]
            vm ∈ L, bm ∉ b            vm ∈ P, bm ∈ b
```

等價改寫（移項，較易於 CP-SAT 表達）：

```
∀ b ∈ B_d:  Σ (assign[vm, bm], vm ∈ L, bm ∈ b)
          + Σ (assign[vm, bm], vm ∈ P, bm ∈ b)
          ≤  |L|       # 因 Σ_all assign[vm,bm] for vm in L 等於 |L|
```

直觀解釋：對任一 bucket b，「b 內 Master 數 + b 內 Learner 數 ≤ Learner 總數」。
（推導：失效 b 後倖存 Learner = |L| − b 內 Learner ≥ b 內 Master）

#### 約束總表更新

| 代號 | 名稱 | 變更 |
|------|------|------|
| C1 | One-BM-per-VM | 不變 |
| C2 | Capacity | 不變 |
| **C3** | **Multi-dim Anti-Affinity** | 從單 AG 維度泛化為 `spread_on` 多維度 |
| C4 | Max per Baremetal | 不變 |
| **C5** | **Failover Redundancy** | **新增** |

### 3. Solver 內部重構

#### 3.1 `dimension_to_bms` 取代 `ag_to_bms`

`solver.py` L125–127 改為：

```python
SPREAD_DIMENSIONS = ["site", "phase", "datacenter", "room", "rack", "ag"]

self.dim_to_bms: dict[str, dict[str, list[str]]] = {}
for dim in SPREAD_DIMENSIONS:
    buckets: dict[str, list[str]] = defaultdict(list)
    for bm in self.request.baremetals:
        buckets[getattr(bm.topology, dim)].append(bm.id)
    self.dim_to_bms[dim] = dict(buckets)

# Backward compat alias
self.ag_to_bms = self.dim_to_bms["ag"]
```

#### 3.2 Auto-generation 對 multi-dim 的處理

`_resolve_anti_affinity_rules()` 自動為 `(cluster_id, ip_type, node_role)` 產生規則時，依 `config.target_spread` 的鍵集合決定 `spread_on`：

```python
spread_dims = sorted(self.config.target_spread.keys())   # e.g. ["ag", "room"]
auto_rule = AntiAffinityRule(
    group_id=f"auto/{cid}/{ip}/{role}",
    selector=GroupSelector(...),
    spread_on=spread_dims,
)
```

每個維度獨立檢查 `target_spread[d]` 並產生 advisory：`spread_below_target` 替代既有 `ag_spread_below_target`，details 含 `dimension` 欄位。

#### 3.3 `_add_anti_affinity_constraints()` 迴圈外移

```python
for rule in self.effective_rules:
    for dim in rule.spread_on:
        buckets = self.dim_to_bms[dim]
        N = len(VMs_of(rule))
        cap = math.ceil(N / max(len(buckets), 1))
        for bucket_name, bm_ids in buckets.items():
            self.model.add(
                sum(self.assign[(vm, bm)] for vm in ... for bm in bm_ids) <= cap
            )
```

#### 3.4 新增 `_add_failover_constraints()`

```python
def _add_failover_constraints(self):
    for f in self.request.failover_rules:
        if f.policy != "n_minus_1":
            self._input_errors.append(...)
            continue
        P = self._resolve_selector(f.primary)
        L = self._resolve_selector(f.backup)
        buckets = self.dim_to_bms.get(f.fault_domain)
        if not buckets:
            self.advisories.append({"type": "failover_unknown_dimension", ...})
            continue
        for b_name, bm_ids in buckets.items():
            in_b_primary = sum(self.assign[(vm, bm)] for vm in P for bm in bm_ids)
            in_b_backup  = sum(self.assign[(vm, bm)] for vm in L for bm in bm_ids)
            self.model.add(in_b_primary + in_b_backup <= len(L))
```

### 4. Diagnostics 變更

| 既有欄位 | 變更 |
|---|---|
| `infeasible_anti_affinity_rules[].max_per_ag` | 保留；新增 `per_dimension_caps: dict[str, int]` |
| advisory `type: "ag_spread_below_target"` | 保留；新增 `type: "spread_below_target"` with `details.dimension` |
| — | 新增 `infeasible_failover_rules`：`[{rule_id, primary_count, backup_count, fault_domain, details}]` |

### 5. JSON 契約變更摘要

| 欄位 | 變更 | 影響 |
|---|---|---|
| `PlacementRequest.anti_affinity_rules[].spread_on` | NEW 選填 | 向後相容 |
| `PlacementRequest.failover_rules` | NEW 選填陣列 | 向後相容 |
| `Baremetal.topology.room` | NEW 選填 | 向後相容 |
| `VM.node_role` 可為 `"learner"` | NEW enum 值 | Go scheduler 需識別 |
| `SolverConfig.target_spread` | NEW dict | 與 `target_ag_spread` 並存 |

---

## Alternative & Trade-offs

### Alternative 1：MASTER 加 `master_type=primary|learner` 子分類

- 不新增 `NodeRole.LEARNER`
- `VM` 與 `GroupSelector` 加 `master_type` 欄位

**為何不選**：
1. 未來其他 role（worker/storage 等）若也想要主從配對，每個都要加自己的 sub-type 欄位，`GroupSelector` 越來越胖
2. `master_type` 在非 master role 上無意義，schema 異味
3. Cross-pair 語意（FailoverRule）兩種模型下幾乎一樣寫，但獨立 role 模型語意更乾淨
4. 既有 auto-gen anti-affinity 的 group key 必須變大（含 master_type），有破壞既有測試行為的風險

### Alternative 2：以 SpreadRule 正式取代 AntiAffinityRule（breaking change）

**為何不選**：
1. JSON 契約變動波及 Go scheduler、6 個 markdown 文件、3 個 example 檔
2. 內部核心邏輯該重寫的都一樣要寫，重命名沒有省工
3. 「反親和」本就是 spread 的別名，名字保留不影響可讀性

### Alternative 3：用單一 multi-dim joint 桶（cross product bucket）

對每個 (ag, room) 笛卡兒積建立桶並限制每桶上限。

**為何不選**：
1. 桶數爆炸：3 AG × 2 Room = 6 桶，每桶上限 ⌈5/6⌉ = 1 → 比實際需求更緊（過約束）
2. 與「每維度獨立平均」的需求語意不符
3. 約束數量增加但解空間反而被不必要地縮小

### Alternative 4：將 redundancy 表達成 soft objective 而非 hard constraint

把 N-1 互補做成最大化目標。

**為何不選**：
1. 使用者需求是「N-1 必達」的可用性保證，不是「越多越好」
2. Soft objective 在資源吃緊時可能讓 redundancy 默默退化，違反運維期望

---

## Risk & Mitigations

| 風險 | 說明 | 緩解 |
|---|---|---|
| **過約束 (over-constrained) 變不可解** | 同時要求 by AG + by Room + Failover + 既有 capacity / max_per_bm 可能讓 INFEASIBLE 變多 | 在 diagnostics 中明確標示哪條 spread 維度或哪條 failover rule 不可解；提供 `infeasibility_check` 工具逐維度估算 |
| **JSON 相容性破壞** | Go scheduler 若拿到不認識的 enum `"learner"` 或新欄位 `room` / `failover_rules` 可能 panic | 採 c 策略 — 既有欄位不變，新欄位皆選填；發版前與 Go scheduler 對齊 enum 列表 |
| **Auto-gen 規則維度膨脹** | `target_spread` 含多維時 auto-gen 規則對每條都套多維 spread，可能在小規模 cluster 過嚴 | 維度數量過多或 buckets 過少時 fallback 為 single-dim 並寫 advisory |
| **Multi-dim ceil 計算過嚴** | `cap = ⌈N/|buckets|⌉` 對小 N 大 buckets 趨向 1，可能無解 | 提供 `allow_relax_spread` 配置，溢位時降為 floor + 1 並 emit advisory |
| **FailoverRule 與 spread 互相打架** | 例如 by Room 分散要求平均、Failover 要求互補，兩者數學上可能在 odd-count 群組不可解 | 在 _resolve 階段預檢：若 P_count > L_count，failover 在 N-1 下不可能成立 → 直接拒絕並回報 INPUT_ERROR |
| **效能退化** | 多維度約束讓 CP-SAT 模型變大 | 限制 `spread_on` 維度上限（建議 ≤ 3）；對 buckets 為 1 的維度跳過（無分散意義） |
| **Diagnostics 既有消費者壞掉** | `ag_spread_below_target` 改名 | 既有 type 字串保留；新增 generic type 並存，文件標示 deprecation |

---

## Rollout Plan

### Phase 1：資料模型 + Topology 擴充（向後相容，無功能變化）

1. 在 `models.py` 加 `Topology.room`、`NodeRole.LEARNER`、`FailoverRule`、`AntiAffinityRule.spread_on`、`SolverConfig.target_spread`
2. 所有新欄位提供合理預設值，舊測試應**全部繼續綠燈**
3. Tag: 0.x.0

### Phase 2：Solver 核心泛化

1. 用 `dim_to_bms` 取代 `ag_to_bms`（保留 alias 供既有程式碼）
2. `_resolve_anti_affinity_rules()` 與 `_add_anti_affinity_constraints()` 改為多維度
3. 既有測試（只用 `max_per_ag` / `spread_on=[ag]`）行為不變

### Phase 3：FailoverRule 實作

1. `_add_failover_constraints()` 與 `_resolve_failover_rules()`
2. 預檢 P/L 數量關係，失敗 → INPUT_ERROR
3. 新增測試：2 Room/3 Room、各維度 fault domain、不可解 case

### Phase 4：Diagnostics 與文件

1. 新增 `infeasible_failover_rules`、`spread_below_target` advisory
2. 更新 `docs/constraints.md` 新增 C3 泛化說明與 C5 章節
3. 更新 `docs/go-scheduler-guide.md` 標註新欄位與 deprecation

### Phase 5：Examples 與整合測試

1. 新增 `examples/master_learner_2room.json`
2. 整合測試覆蓋多維 spread + failover 同時生效情境

### 回滾策略

- 各 Phase 為獨立 commit；任一 Phase 出包可單獨 revert 而不影響其他
- 新欄位皆選填，回滾不影響線上既有 request payload
- `target_spread` 同時保留 `target_ag_spread` 作 fallback；config 任一可運作

---

## Open Question

1. **`spread_on` 中各維度的 cap 是否該允許獨立指定**（而非一律 ceil）？例如 `spread_on={"ag": 2, "room": 3}` 直接指定每維度上限。目前 proposal 採全自動 ceil，簡潔但彈性低。
2. **FailoverRule 是否該允許多個 fault_domain**？例如 `fault_domain: ["room", "ag"]` 表達「任一 Room 或任一 AG 失效都要能補位」。會大幅增加約束數量。
3. **Auto-generation 是否自動產生 FailoverRule**？目前提案僅自動產生 anti-affinity，failover 必須顯式給出。若觀察到 cluster 同時有 `(cluster, ip, master)` 與 `(cluster, ip, learner)` 群組，是否預設配對？
4. **`target_spread` 含多個維度時 advisory 是否各維度獨立發出**？目前提案每維度一條，可能讓 diagnostics 變吵雜。
5. **Learner 是否預設繼承 Master 的 candidate_baremetals 池**，還是 Go scheduler 必須分別提供？影響 splitter 預設行為。
6. **既有 `max_per_ag` 顯式設值與 `spread_on=["ag"]` 自動 ceil 衝突時的優先順序**：目前提案 `max_per_ag` 優先（顯式覆寫），是否合理？

---

## Decision Log (Review 後補)

| Decision | Reason | Follow-ups |
|---|---|---|
| _(待填)_ | | |
