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

設計原則：**JSON 契約尚未 GA，採一次性 breaking change 換取程式碼單一路徑**。`AntiAffinityRule.max_per_ag` 移除、`spread_on` 必填、新增 `cap_per_bucket` 取代原本由 `max_per_ag` 表達的「顯式上限覆寫」能力。

---

## Goals / Non-Goals

### Goals

- 在 `NodeRole` 加入 `LEARNER`，且使既有 anti-affinity auto-generation（以 `(cluster_id, ip_type, node_role)` 為 group key）天然分開處理 master / learner 兩群
- 在 `Topology` 加入 `room` 維度
- 重新設計 `AntiAffinityRule`：`spread_on: list[str]` 為必填、新增 `cap_per_bucket: dict[str, int] | None` 表達各維度顯式上限、**移除** `max_per_ag` 欄位
- 新增 `FailoverRule(primary, backup, fault_domain, policy="n_minus_1")` 表達跨群組互補
- `SolverConfig.target_ag_spread` 移除，改為 `target_spread: dict[str, int]`
- **一次性 breaking change**：solver 與 Go scheduler 同步升版；不保留 legacy 欄位、不留 fallback 分支

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

#### 1.3 `AntiAffinityRule` 重新設計

**移除 `max_per_ag`、新增 `spread_on`（必填）與 `cap_per_bucket`（選填）**：

```python
class AntiAffinityRule(BaseModel):
    group_id: str
    vm_ids: list[str] = Field(default_factory=list)
    selector: GroupSelector | None = None

    # 必填：對這些 topology 維度做平均分散
    # 合法值: "site" | "phase" | "datacenter" | "room" | "rack" | "ag"
    # 至少給一個維度；給空陣列視為 INPUT_ERROR
    spread_on: list[str]

    # 選填：對指定維度覆寫自動 ceil 上限。未列出的維度走 ceil(N/|buckets|)
    # 例: {"ag": 2}  -> ag 維度每桶最多 2 台，room 維度走 ceil
    cap_per_bucket: dict[str, int] | None = None
```

**語意**：對 `spread_on` 中每個維度 `d`，獨立加一條約束：

```
N = |VMs(rule)|
B_d = buckets of dimension d
cap_d = cap_per_bucket[d] if d in cap_per_bucket else ⌈N / |B_d|⌉

∀ b ∈ B_d:  sum(assign[vm, bm] for vm in VMs(rule) for bm in b) ≤ cap_d
```

多維度約束**獨立疊加**（AND），不是聯合分布。

**JSON 範例**（多維度，自動 ceil）：

```json
{
  "group_id": "masters-A-routable",
  "selector": {"cluster_id": "A", "ip_type": "routable", "node_role": "master"},
  "spread_on": ["ag", "room"]
}
```

**JSON 範例**（顯式 cap 覆寫）：

```json
{
  "group_id": "masters-A-routable",
  "selector": {"cluster_id": "A", "ip_type": "routable", "node_role": "master"},
  "spread_on": ["ag", "room"],
  "cap_per_bucket": {"ag": 2}
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
    # 移除：target_ag_spread: int = 3
    # 新：dict 鍵為 topology 維度名；預設僅 ag 維度有期望桶數
    target_spread: dict[str, int] = Field(default_factory=lambda: {"ag": 3})
```

`target_ag_spread` 欄位**移除**，不留 alias。

### 2. 約束數學

#### C3a: Multi-dimension Anti-Affinity（取代既有 C3）

對每條規則 `r`，對 `r.spread_on` 中每個維度 `d`：

```
N_r = |VMs(r)|
B_d = buckets of dimension d (e.g. distinct ag values, or distinct room values)
cap = r.cap_per_bucket[d] if d in r.cap_per_bucket else ⌈N_r / |B_d|⌉

∀ b ∈ B_d:  Σ assign[vm, bm]  ≤  cap
            vm ∈ VMs(r), bm ∈ b
```

無 legacy 分支：solver 程式只有「對每維度查表或 ceil」一條路徑。

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
```

`ag_to_bms` 屬性移除；既有讀 `self.ag_to_bms` 的程式碼一律改為 `self.dim_to_bms["ag"]`。

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
    vms = self._resolve_rule_vms(rule)
    N = len(vms)
    for dim in rule.spread_on:
        buckets = self.dim_to_bms[dim]
        cap = (
            rule.cap_per_bucket[dim]
            if rule.cap_per_bucket and dim in rule.cap_per_bucket
            else math.ceil(N / max(len(buckets), 1))
        )
        for bucket_name, bm_ids in buckets.items():
            self.model.add(
                sum(self.assign[(vm, bm)] for vm in vms for bm in bm_ids
                    if (vm, bm) in self.assign) <= cap
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
| `infeasible_anti_affinity_rules[].max_per_ag` | **移除**；改以 `per_dimension_caps: dict[str, int]` 表達各維度上限 |
| advisory `type: "ag_spread_below_target"` | **移除**；改為 `type: "spread_below_target"` with `details.dimension` |
| — | 新增 `infeasible_failover_rules`：`[{rule_id, primary_count, backup_count, fault_domain, details}]` |

### 5. JSON 契約變更摘要（**Breaking — 需 Go scheduler 同步升版**）

| 欄位 | 變更 | 影響 |
|---|---|---|
| `AntiAffinityRule.max_per_ag` | **移除** | Breaking |
| `AntiAffinityRule.spread_on` | **新增必填** | Breaking |
| `AntiAffinityRule.cap_per_bucket` | 新增選填 | — |
| `PlacementRequest.failover_rules` | 新增選填陣列 | — |
| `Baremetal.topology.room` | 新增選填 | — |
| `VM.node_role` 可為 `"learner"` | 新增 enum 值 | Go scheduler 需識別 |
| `SolverConfig.target_ag_spread` | **移除** | Breaking |
| `SolverConfig.target_spread` | 新增 dict 取代 | — |
| Diagnostics `infeasible_anti_affinity_rules[].max_per_ag` | **移除** | Breaking（消費端需改） |
| Diagnostics advisory `type: "ag_spread_below_target"` | **移除** | Breaking（消費端需改） |

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

### Alternative 2：保留 `max_per_ag` 與預設 `spread_on=["ag"]` 維持向後相容

於 `AntiAffinityRule` 同時保留 `max_per_ag` 欄位（作為 `spread_on=["ag"]` 時的顯式上限 alias）並讓 `spread_on` 有預設值。

**為何不選**：
1. solver core 會多出 if/else 分支處理「`spread_on == [ag]` 時優先用 `max_per_ag` 還是 `cap_per_bucket['ag']`」
2. Diagnostics 雙寫（既有 `max_per_ag` + 新 `per_dimension_caps`）讓對外格式久了會分裂
3. 既然 Go scheduler 尚未 GA，斷然清理的成本小於日後每次改動繞 legacy 分支的累積代價
4. `cap_per_bucket` 已能完全表達 `max_per_ag` 的所有用法，無功能損失

### Alternative 3：以新類名 `SpreadRule` 正式取代 AntiAffinityRule

**為何不選**：
1. 「反親和」本就是 spread 的別名，類名換不換不影響可讀性
2. 改名波及 6 個 markdown 文件、3 個 example 檔，純打字成本
3. 既有測試 import 全部要改，無語意收益

### Alternative 4：用單一 multi-dim joint 桶（cross product bucket）

對每個 (ag, room) 笛卡兒積建立桶並限制每桶上限。

**為何不選**：
1. 桶數爆炸：3 AG × 2 Room = 6 桶，每桶上限 ⌈5/6⌉ = 1 → 比實際需求更緊（過約束）
2. 與「每維度獨立平均」的需求語意不符
3. 約束數量增加但解空間反而被不必要地縮小

### Alternative 5：將 redundancy 表達成 soft objective 而非 hard constraint

把 N-1 互補做成最大化目標。

**為何不選**：
1. 使用者需求是「N-1 必達」的可用性保證，不是「越多越好」
2. Soft objective 在資源吃緊時可能讓 redundancy 默默退化，違反運維期望

---

## Risk & Mitigations

| 風險 | 說明 | 緩解 |
|---|---|---|
| **過約束 (over-constrained) 變不可解** | 同時要求 by AG + by Room + Failover + 既有 capacity / max_per_bm 可能讓 INFEASIBLE 變多 | 在 diagnostics 中明確標示哪條 spread 維度或哪條 failover rule 不可解；提供 `infeasibility_check` 工具逐維度估算 |
| **JSON 契約 Breaking** | `max_per_ag` / `target_ag_spread` 移除、`spread_on` 變必填，Go scheduler 端必須同步升版才能對接 | 雙方共議單一切版點；solver 端做嚴格 schema 驗證並回 INPUT_ERROR 提示具體欄位名；發版 note 列出所有 breaking 欄位 |
| **Auto-gen 規則維度膨脹** | `target_spread` 含多維時 auto-gen 規則對每條都套多維 spread，可能在小規模 cluster 過嚴 | 維度數量過多或 buckets 過少時 fallback 為 single-dim 並寫 advisory |
| **Multi-dim ceil 計算過嚴** | `cap = ⌈N/|buckets|⌉` 對小 N 大 buckets 趨向 1，可能無解 | 提供 `allow_relax_spread` 配置，溢位時降為 floor + 1 並 emit advisory |
| **FailoverRule 與 spread 互相打架** | 例如 by Room 分散要求平均、Failover 要求互補，兩者數學上可能在 odd-count 群組不可解 | 在 _resolve 階段預檢：若 P_count > L_count，failover 在 N-1 下不可能成立 → 直接拒絕並回報 INPUT_ERROR |
| **效能退化** | 多維度約束讓 CP-SAT 模型變大 | 限制 `spread_on` 維度上限（建議 ≤ 3）；對 buckets 為 1 的維度跳過（無分散意義） |
| **Diagnostics 既有消費者壞掉** | `ag_spread_below_target` 改名 | 既有 type 字串保留；新增 generic type 並存，文件標示 deprecation |

---

## Rollout Plan

本變更為 breaking schema 升版，採**單一切版點**而非漸進相容。所有變更於同一 release tag 一次發布，與 Go scheduler 預先協調好升版時點。

### Phase 1：資料模型重塑

1. 在 `models.py` 加 `Topology.room`、`NodeRole.LEARNER`、`FailoverRule`
2. 重寫 `AntiAffinityRule`：移除 `max_per_ag`、新增必填 `spread_on` 與選填 `cap_per_bucket`
3. 重寫 `SolverConfig`：移除 `target_ag_spread`、新增 `target_spread: dict[str, int]`
4. Pydantic validator：
   - `AntiAffinityRule.spread_on` 不可為空
   - `AntiAffinityRule.cap_per_bucket` 鍵集合必須是 `spread_on` 子集；value 需 ≥ 1（0 視為 INPUT_ERROR）
   - `FailoverRule.fault_domain` 為單一字串、必須屬於合法維度名集合
   - `FailoverRule.policy` 目前僅接受 `"n_minus_1"`
5. 此 Phase 結束預期既有測試**全部紅燈** — 屬於預期，於 Phase 2 同步更新

### Phase 2：Solver 核心泛化 + 既有測試遷移

1. 用 `dim_to_bms` 取代 `ag_to_bms`（不留 alias）
2. 重寫 `_resolve_anti_affinity_rules()` 與 `_add_anti_affinity_constraints()` 走多維度單一路徑
3. 更新既有測試 fixture：`max_per_ag=k` → `spread_on=["ag"], cap_per_bucket={"ag": k}`
4. 確認所有單測綠燈

### Phase 3：FailoverRule 實作

1. `_add_failover_constraints()` 與 `_resolve_failover_rules()`
2. 預檢 P/L 數量關係（`|P| > |L|` 在 N-1 下不可能成立 → INPUT_ERROR）
3. 新增測試：2 Room/3 Room、各維度 fault domain、不可解 case

### Phase 4：Diagnostics 與文件

1. 移除 `infeasible_anti_affinity_rules[].max_per_ag` 與 `ag_spread_below_target` advisory
2. 新增 `per_dimension_caps`、`infeasible_failover_rules`、`spread_below_target` advisory
3. 更新 `docs/constraints.md` 新增 C3 泛化說明與 C5 章節
4. 更新 `docs/go-scheduler-guide.md` 將舊欄位標為 removed、列出新欄位

### Phase 5：Examples 與整合測試

1. 全面更新 `examples/*.json`：把所有 `max_per_ag` 改寫為 `spread_on` + 視需要的 `cap_per_bucket`
2. 新增 `examples/master_learner_2room.json` 涵蓋 LEARNER + FailoverRule + 多維 spread
3. 整合測試覆蓋多維 spread + failover 同時生效情境

### 回滾策略

- 各 Phase 為獨立 commit，於同一 release tag 一次發布
- Go scheduler 升版前 solver 不發布；若發現相容性問題，整個 release tag 一起回退
- **不**設計部分回滾（拆 Phase 回滾會留下半套 schema）

---

## Open Question

_All resolved during 2026-05-19 review — see Decision Log below._

---

## Decision Log

| Decision | Reason | Follow-ups |
|---|---|---|
| Learner 採獨立 `NodeRole.LEARNER`，不採 `master_type=primary\|learner` 子欄位 | 避免在 `GroupSelector` 加 role-specific 子欄位；既有 auto-gen group key `(cluster, ip, role)` 天然分群 | 確認 splitter 是否需感知 master/learner 容量配比 |
| 跨群組 N-1 互補命名為 `FailoverRule` | 直接表達語意；獨立 type 避免污染 `AntiAffinityRule` schema | — |
| 採一次性 breaking change，**移除** `max_per_ag` 與 `target_ag_spread`、`spread_on` 必填 | Go scheduler 尚未 GA；保留 legacy 欄位會在 solver core 永久留 if/else 分支與雙寫 diagnostics | 與 Go scheduler 同步排定切版時間 |
| 新增 `cap_per_bucket: dict[str, int]` 而非把上限併入 `spread_on` 結構 | `spread_on` 維持 `list[str]` 易讀；多數使用情境只需 ceil 自動算，`cap_per_bucket` 為進階覆寫選項 | — |
| 保留類名 `AntiAffinityRule`（不改為 `SpreadRule`） | 改名無語意收益；省去 docs/examples 大幅 churn | — |
| `target_ag_spread` 移除而非改名 alias | 若保留 alias 會在 config 解析期長出優先順序分支 | 升版 note 列為 breaking |
| `FailoverRule.fault_domain: str`（單一字串、不支援多維度） | 約束生成邏輯單純；多 fault_domain 需求由使用者寫多條 rule 表達，意圖更明確 | 若日後實證需要多維互補語意，再考慮升版加 list 支援 |
| Auto-generation **不**自動產生 `FailoverRule`，必須顯式給出 | Failover 是強約束；避免同名 cluster 內不想互補的 master/learner 被誤綁。意圖透過顯式 rule 表達較安全 | — |
| Learner VM 的 `candidate_baremetals` 由 Go scheduler 分別提供，solver **不**繼承、**不**推斷 | 介面語意明確、solver 邏輯單純；避免 splitter 預設行為與 scheduler 端期待不一致 | Go scheduler 端需處理 learner candidate 計算 |
| `cap_per_bucket` 值 `0` 視為 **INPUT_ERROR** | 「禁止此維度」的語意應該直接從 `spread_on` 移除該維度表達，避免 schema 出現兩種等價寫法 | Pydantic validator: cap_per_bucket 的 value 需 ≥ 1 |
| `spread_below_target` advisory **每維度一條**獨立發出（含 `details.dimension`） | 診斷資訊最完整；消費端可分維度排查；多 cluster × 多維度可能產生較多 advisory 但仍可管理 | — |
