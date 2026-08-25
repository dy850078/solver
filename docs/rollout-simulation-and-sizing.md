# Enhancement Proposal: Rollout 模擬與建廠估算(pinned VM / rollout / sizing / UI)

> **作者**: Claude(與 dy850078 討論定案)
> **日期**: 2026-08-25
> **狀態**: Implemented
> **相關分支**: `claude/solver-ui-requirements-4sme83`
> **相關 ADR**: [ADR-012](decisions/ADR-012-pinned-vms-grandfathered-caps.md)(pinned VM)、
> [ADR-013](decisions/ADR-013-rollout-simulation.md)(rollout 模擬)、
> [ADR-014](decisions/ADR-014-rollout-sizing.md)(rollout sizing)
> **分講版本**(對外說明用,各自獨立成篇):
> [Add-Node / Pinned VM 說明](add-node-guide.md)、
> [Rollout 模擬說明](rollout-simulation-guide.md)

---

## Summary

本次改動讓 solver 從「一次一批的無記憶擺放器」升級為「看得見過去、能模擬未來」
的調度引擎,分三層堆疊,各自可獨立使用:

1. **Pinned VM 原語**(`VM.pinned_to`):把「已經住在某台 BM 上的 VM」以事實
   的身分帶進請求,讓 C2–C6 全部取得全域視野。這直接解決 **add-node** 場景
   (第二次調度看不見第一次結果),也是 rollout 的基石。
2. **Rollout 模擬**(`POST /v1/placement/rollout`):把「使用者指定順序的多個
   cluster 建置」逐步重放 — 每步真實求解、結果折疊為下一步的 pinned 事實 —
   在規劃期就找出「聯合規劃可行、循序建置卻走進死局」的建置順序。
3. **Rollout sizing**(`POST /v1/placement/rollout/size`):建廠情境的反問題 —
   「照這個順序建,**最少要買幾台 BM**?」以拓撲模板(散到 K 個 AG)描述機隊,
   解析下界 + 逐台上行掃描回答精確最小值。
4. **Rollout UI**(`/ui/rollout.html`):表單化的建置計畫編輯器 — VM spec 目錄、
   逐 cluster 的 build step、多機型 BM pool、拓撲旋鈕、台數留空即估算 —
   功能面可完全取代 Topology 頁(並多出循序模擬與 all-at-once 對照)。

## Goals / Non-Goals

**Goals**

- 一次調度一個 cluster、各 cluster spec 組合不同、順序由使用者指定的循序模擬。
- add-node 時 solver 看得見既有 VM 的分佈(C3/C4/C5/C6 不再只有當下視野)。
- 建廠估算:台數未知、AG 數已知,回答最少台數與 per-AG 分佈,答案可證明最小。
- 以上全部走既有的 pure solve 主路徑(生產主力),splitter 路徑同步支援。

**Non-Goals**

- **混合機型的採購最佳化**:sizing v1 限單一機型;要買哪幾種機型各幾台是
  capacity planner(`/v1/capacity/procure`)的職責。
- **Brownfield sizing**:「已有 N 台再估增購」不在 v1(rollout 模擬本身支援
  brownfield `existing_vms`,但 sizing 端點拒收)。
- **需求飄移的自動校正**:飄移是 rollout 模擬存在的理由(建置當下重新模擬),
  不是要被消除的東西。
- **Per-spec landable 預警**:已討論、刻意降優先權,列在 Open Questions。

## Current State & Problem

規劃時所有已知 cluster 一起看(聯合求解),實際建置卻是一個 cluster 一個
cluster 分開下單。既有系統有三個缺口:

1. **循序死局**:聯合解證明「全部放得下」,但循序建置每步只提交當步的最佳解。
   前面的 cluster 把容量/bucket 吃成碎片,後面的 cluster 可能無處可放 —
   而且要到建置當下才發現。
2. **add-node 無視野**:第二次調度(加節點)的請求只有新 VM;舊 VM 只剩
   `used_capacity` 裡的一坨數字,C3/C5/C6 這些「身分敏感」的約束完全看不見
   它們(used 是純量帳本,只記多少、不記是誰)。
3. **建廠無從估算**:`baremetals` 是必填輸入,但建廠時台數正是要問的答案。

```
 規劃期(聯合)                建置期(循序)
┌──────────────────┐    ┌────────┐ ┌────────┐ ┌────────┐
│ A+B+C 一起解      │    │ 建 A   │→│ 建 B   │→│ 建 C ✗ │ ← 死局在這裡才爆
│ → 可行 ✓         │    └────────┘ └────────┘ └────────┘
└──────────────────┘         ↑ 每步只看當步,看不見彼此
```

## Proposed Design

### 第一層:pinned VM(ADR-012)

`VM.pinned_to = "<bm-id>"` 表示「這台 VM **已經**住在那裡」— 對過去的陳述,
不是擺放請求(想指定新 VM 落點請用單元素 `candidate_baremetals`)。合約:

- **`used_capacity` 維持庫存真相**:包含 pinned VM 的消耗(DB 撈出來什麼樣
  就送什麼樣)。solver 內部正規化:先扣除 pinned demand、再經強制 assign
  變數由 C2 記回 — 帳本淨額歸零,scheduler 不需要做任何 used 的加減。
- **既往不咎、不再惡化(grandfathering)**:C3/C4/C5 的 bucket 上限取
  `max(靜態 cap, 該 bucket 的 pinned 數)` — 既有違規凍結、不會讓請求變
  INFEASIBLE,但新 VM 不能把任何 bucket 弄得更糟。
- **C6 不寬貸**:exclusive 是佔用語意,沒有「超標一點點」可凍結 — pinned
  佈局違反 C6 直接 INPUT_ERROR。
- 回應中 pinned VM 以 `pinned: true` 原樣回顯(完整終態,便於驗證與 UI);
  **scheduler 只執行 `pinned: false` 的 assignment**。

### 第二層:rollout 模擬(ADR-013)

對 steps 依序執行真正的 solve;第 k 步成功後,其 placement 折疊進第 k+1 步
的請求:VM 標 `pinned_to`、host 的 `used_capacity += demand`(維持庫存真相
不變量,solver 端會正規化 — 帳本每步淨額歸零,整數運算無累積漂移)。因此
**每一步都是一次「如假包換」的生產調度呼叫** — 模擬結果就是實際建置會得到
的結果。其他要點:

- **規則聯集**:step k 在 steps 1..k 的規則聯集下求解 — step 1 放好的
  exclusive appliance,在 step 2 依然受 C6 保護。
- **首敗即止**:第一個失敗的 step 之後不再求解,回報
  `BLOCKED: not simulated — step '<failed>' failed` 的 stub(死局的成因在
  失敗步,後面的結果沒有意義)。
- **Brownfield**:`existing_vms` 描述 step 1 之前就存在的 VM(必須帶
  `pinned_to`、demand 已含在起始 used 內),永不重複折疊。

### 第三層:rollout sizing(ADR-014)

`FleetTemplate` 用拓撲計數(sites/phases/datacenters/rooms/racks/ags)描述
機隊:每 rack 一組 topology、上層維度以 rack 序號取模輪替、機器 round-robin
灑進 racks(per-AG 恆平衡到差 ≤1)。搜尋 = **解析下界 → 逐台 +1 上行掃描**,
每次探測跑完整的 `solve_rollout`:

- 下界必須**嚴格低估**(逐欄位體積界 `max_f ⌈Σdemand_f / cap_f⌉` + 張數界 +
  獨占數,詳 `app/sizing_floors.py`),否則「最小」是謊言 — 搜尋不會探測
  下界以下。
- 可行性對台數**不單調**(貪婪路徑依賴、bucket 數隨機隊變、UNKNOWN 誤讀),
  所以是線性掃描而非二分:`[floor, N*)` 全部實測失敗 + `< floor` 由下界排除
  ⇒ `N*` 是精確最小值。
- **Pre-flight** 把「加幾台都沒救」的輸入(VM 大於機型、requirement 的
  network 與模板不符、failover/AA 落在被塌縮成單一 bucket 的維度、預設
  candidates、pinned、existing_vms)擋成 INPUT_ERROR — 否則建模錯誤會燒光
  探測預算,偽裝成「多買機器就好」的採購建議。

### 關鍵設計決策(與被否決的替代方案)

| 決策 | 替代方案 | 為何不選替代方案 |
|---|---|---|
| 折疊 = pinned VM + used 同步加 | 只把 demand 加進 used、VM 丟掉 | used 是純量帳本;C3(分散計數)、C5(failover 名額)、C6(佔用)都需要「是誰在哪」的身分資訊,丟掉 VM 就只剩當下視野 |
| C3/C4/C5 grandfathering | 既有違規 → INFEASIBLE | 現實庫存常帶歷史違規;add-node 會永遠無解。凍結但不惡化才可運維 |
| 上行掃描 | 二分搜尋 | 可行性不單調(序列可能 F,F,S,F,S,S),二分回傳垃圾;ADR-008 已為 mockgen 否決過一次 |
| 逐欄位體積下界 | 分組相除再相加 | 後者隱含「不同規格不共機」,是**高估** — 被自己的性質測試抓到,會讓搜尋永遠看不到真正的最小值 |
| 合成 VM 才改名(`{step}/{id}`) | 所有 VM id 都加 step 前綴 | 全面改名會讓使用者寫的 vm_ids 型規則(如 C6)無聲失效 |

---

## API 使用指南

### 我想做什麼 → 打哪個端點

| 情境 | 端點 | 一句話 |
|---|---|---|
| 新 cluster 首次調度 | `POST /v1/placement/solve` | 與過去相同,無需 pinned |
| **對既有 cluster 加節點(add-node)** | `POST /v1/placement/solve` | 舊 VM 標 `pinned_to` 一起送進來 |
| **模擬一個建置順序會不會走進死局** | `POST /v1/placement/rollout` | 逐步重放 + 折疊 |
| **建廠:這個建置順序最少要幾台 BM** | `POST /v1/placement/rollout/size` | 台數是答案不是輸入 |
| 讓 solver 決定 spec 切分 + 擺放 | `POST /v1/placement/split-and-solve` | 既有功能;rollout step 內放 `requirements` 也會走 splitter |
| 採購尺度要買哪些機型各幾台 | `POST /v1/capacity/procure` | 混合機型;近似模型;拒收 pinned |

三個「要幾台」的答案天生不同,偏序:**capacity planner ≤ mockgen elastic
(聯合)≤ rollout sizing(循序)**。循序建置會碎片化,聯合擺放永遠較緊;
拿聯合解的台數去建廠,可能建到一半發現不夠 — 這正是 rollout sizing 存在
的理由。

### 情境 1:add-node(加節點)

**端點**:`POST /v1/placement/solve`(既有端點,新增 `pinned_to` 欄位;
不帶 pinned 的舊請求行為完全不變)。

**Scheduler 端的責任**(標註在輸入、過濾在輸出):

1. `baremetals` = 可調度的 BM 群 **∪ 所有 pinned VM 的宿主**(宿主不在
   請求裡 → INPUT_ERROR;宿主在請求裡**不代表**可調度 — 新 VM 能去哪由
   它自己的 `candidate_baremetals` 決定)。
2. `used_capacity` 直接用 DB 的庫存真相(**含** pinned VM 的消耗),
   不要自行加減。
3. 舊 VM:`pinned_to` = 宿主 id、`candidate_baremetals` = `["<宿主 id>"]`
   (solver 拒收空 candidate 列表)、`demand` = 庫存記錄值(不是使用者重新
   輸入的值 — 正規化要扣的就是這個數)。
4. 拿到回應後,**只執行 `pinned: false` 的 assignment**。

```jsonc
// POST /v1/placement/solve — cluster-a 已有 2 台 master,現在加 1 台
{
  "vms": [
    { "id": "master-3", "demand": { "cpu_cores": 8, "memory_mib": 32768, "storage_gb": 200 },
      "node_role": "master", "ip_type": "routable", "cluster_id": "cluster-a",
      "candidate_baremetals": ["bm-1", "bm-2", "bm-3"] },

    { "id": "master-1", "demand": { "cpu_cores": 8, "memory_mib": 32768, "storage_gb": 200 },
      "node_role": "master", "ip_type": "routable", "cluster_id": "cluster-a",
      "pinned_to": "bm-1", "candidate_baremetals": ["bm-1"] },
    { "id": "master-2", "demand": { "cpu_cores": 8, "memory_mib": 32768, "storage_gb": 200 },
      "node_role": "master", "ip_type": "routable", "cluster_id": "cluster-a",
      "pinned_to": "bm-2", "candidate_baremetals": ["bm-2"] }
  ],
  "baremetals": [
    { "id": "bm-1", "total_capacity": { "cpu_cores": 64, "memory_mib": 262144, "storage_gb": 2000 },
      "used_capacity":  { "cpu_cores": 8,  "memory_mib": 32768,  "storage_gb": 200 },   // 含 master-1
      "topology": { "site": "site-1", "phase": "p1", "datacenter": "dc-1",
                    "room": "room-1", "rack": "rack-1", "ag": "ag-1" } },
    // bm-2(含 master-2 的 used)、bm-3(空機)同形省略
  ],
  "config": { "auto_generate_anti_affinity": true }
}
```

**效果**:auto anti-affinity 以 `(cluster_id, ip_type, node_role)` 分群,
master-1/2/3 同群 — C3 看得見 pinned 的兩台各佔一個 AG,於是 master-3 會被
推去第三個 AG,而不是像過去一樣「不知道前兩台在哪」隨意疊上去。回應:

```jsonc
{
  "success": true,
  "assignments": [
    { "vm_id": "master-1", "baremetal_id": "bm-1", "ag": "ag-1", "pinned": true },   // 回顯,勿執行
    { "vm_id": "master-2", "baremetal_id": "bm-2", "ag": "ag-2", "pinned": true },   // 回顯,勿執行
    { "vm_id": "master-3", "baremetal_id": "bm-3", "ag": "ag-3", "pinned": false }   // 只執行這筆
  ]
}
```

**常見 INPUT_ERROR**(合約違反不做無聲修正):

| 訊息片段 | 成因 | 修法 |
|---|---|---|
| `pinned host '...' ... not present` | 宿主 BM 沒送進 `baremetals` | 送「可調度群 ∪ pinned 宿主」 |
| `pinned demand ... exceeds its used_capacity` | used 沒含 pinned 消耗(扣到負) | used 用庫存真相,勿自行預扣 |
| `over-committed (used > total)` | 庫存本身超賣 | 先修庫存資料 |
| `candidate list ... does not contain pinned_to` | 同一台 VM 的兩個欄位互相矛盾 | pinned VM 的 candidates 給 `[宿主]` |
| C6 相關(pinned 違反 exclusive) | 既有佈局踩了 exclusive 規則 | 先搬遷或修正規則 — C6 不寬貸 |

### 情境 2:循序建置模擬(rollout)

**端點**:`POST /v1/placement/rollout`。什麼時候打:規劃期想驗證一個建置
順序、或建置前夕(需求已飄移)想重新確認剩下的步驟仍可行。

```jsonc
// 骨架 — 完整可跑範例見 examples/rollout/multi_cluster_mixed_specs.json
{
  "baremetals": [ /* 共用庫存;greenfield 時 used 全 0 */ ],
  "steps": [
    { "name": "cluster-a",
      "vms": [ /* 明確 spec×數量(生產 pure solve 形式) */ ],
      "exclusive_bm_rules": [ /* 規則跟著首次出現的 step 走,之後自動聯集 */ ] },
    { "name": "cluster-b",
      "vms": [ /* ... */ ],
      "requirements": [ /* 也可放粗粒度需求,該步會走 splitter 聯合切分 */ ] }
  ],
  "existing_vms": [ /* 選填,brownfield 起始態:必帶 pinned_to,demand 已含在起始 used */ ],
  "config": { "auto_generate_anti_affinity": true, "target_spread": { "ag": 3 } }
}
```

**讀回應**:

- `success` = 每一步都成功;`failed_step` = 第一個失敗的步(其後的
  report 是 `BLOCKED:` stub,未實際求解)。
- `reports[k].new_assignments` **只含該步新增的 placement**(前步折疊進來
  的 pins 不重複出現);失敗步的 `unplaced_vms` 也只列該步自己的 VM。
- `final_baremetals` = 所有成功折疊後的庫存快照 — 「下一次真實調度呼叫
  會看到的 used_capacity」。可直接拿來當後續 what-if 的起點。
- 合約層錯誤(如重複 BM id)不逐步模擬,直接在頂層
  `solver_status = "INPUT_ERROR: ..."` 短路,完整清單在
  `diagnostics["input_errors"]`。

**模擬失敗(死局)之後怎麼辦**:調整順序(把碎片化嚴重的大 cluster 提前)、
放寬規則、或加機器 — 改完重打即可;每次呼叫都是無狀態的純模擬,不會動到
任何真實庫存。想知道「加到幾台才夠」,直接進情境 3。

### 情境 3:建廠估算(rollout sizing)

**端點**:`POST /v1/placement/rollout/size`。什麼時候打:機隊還不存在,
問「照這個順序建,最少要買幾台」。

```jsonc
// 骨架 — 完整可跑範例見 examples/rollout_sizing/greenfield_three_clusters.json
{
  "fleet": {
    "total_capacity": { "cpu_cores": 64, "memory_mib": 262144, "storage_gb": 2000 },
    "racks": 3, "ags": 3,          // sites/phases/datacenters/rooms 預設 1(塌縮成單一 bucket)
    "network": ""                   // steps 有 requirements 時需與其 network 相容
  },
  "steps": [ /* 與 rollout 相同,但「不可」預填 candidate_baremetals(機隊每次探測重新生成)*/ ],
  "config": { "auto_generate_anti_affinity": true },
  "max_probes": 12, "deadline_seconds": 120, "max_baremetals": 200   // 預設值,可省略
}
```

**讀回應**:

- `success: true` 時 `required_baremetals` 是**精確最小值**(每個更小的
  台數,要嘛被下界排除、要嘛實測失敗),`per_ag` 給分佈(恆平衡到差 ≤1),
  `baremetals` 給生成好的機隊清單(id 依序號固定,可直接拿去當
  rollout / 採購的輸入),`rollout` 附上勝出那次探測的完整逐步報告。
- `analytic_floor` / `floor_breakdown` = 下界與拆解(capacity / headcount /
  pack / solo / ags);`probes` = 探測足跡(哪個 N、什麼狀態、哪步失敗)—
  信任答案的依據,探測輪數經常 >3 表示下界該補強,值得回報。
- **預算用盡**:`success: false`、`solver_status: "BUDGET_EXHAUSTED"`,
  `lower_bound` / `upper_bound` 夾出答案區間(不給裸失敗)。放大
  `max_probes` / `deadline_seconds` 重打即可從區間繼續逼近。
- **UNKNOWN 即中止**:探測逾時不會被誤讀成「不可行」而繼續加碼;調大
  `config.max_solve_time_seconds` 或 `deadline_seconds` 再試。
- **INPUT_ERROR = 這個輸入加幾台都沒救**,修輸入而不是加預算:VM/spec 塞
  不進機型、requirement 的 network 與模板不符、failover 或 AA 的維度被模板
  塌縮成單一 bucket、步驟預填了 candidates、帶 pinned、帶 existing_vms。

**注意**:答案是「**在你指定的拓撲形狀下**(K 個 AG、R 個 rack)」的最小值,
不是全域最小 — K 是提問的一部分,`ags < target_spread.ag` 只發 advisory
不自動上調。

### 狀態字串速查(Go scheduler 分支依據,勿改字串)

| 狀態 | 出現在 | 意義 |
|---|---|---|
| `OPTIMAL` / `FEASIBLE` | 各 solve 端點、step report | 有解(最佳/可行) |
| `INFEASIBLE: ...` | 各 solve 端點、step report | 約束下無解,附 diagnostics |
| `INPUT_ERROR: ...` | 全部端點 | 合約違反,修輸入(不做無聲修正) |
| `BLOCKED: not simulated ...` | rollout step report | 前面已有步驟失敗,本步未求解 |
| `BUDGET_EXHAUSTED` | sizing 頂層 | 探測預算用盡,看 lower/upper bound |

### UI 對照(`/ui/rollout.html`,需 `ENABLE_UI=enable`)

| 想做的事 | 操作 |
|---|---|
| 多 cluster、各自不同 spec 組合 | VM spec 目錄定義一次,各 step 的群組列下拉選用;⧉ 複製群組/整個 step |
| 控制建置順序 | step 卡片 ↑/↓;Sequential = rollout、All at once = 聯合對照 |
| 跨 cluster 共用 + 獨占(如 F5) | 群組勾 `sh`(cluster_id 變 "shared")+ `ex`(C6);或直接開一個名為 `shared` 的 step 先建 appliance — 分組效果等價,差別只在建置時機 |
| 自訂 node role / ip type | 直接在欄位打字(datalist 建議、不限制)— 對齊 ADR-010 開放字串 |
| 多機型 / 專用 pool | Baremetal fleet 卡片「+ BM model」;Roles 欄填角色清單即成專用 pool(留空 = 通用機) |
| 估算最少台數 | 單一機型時把 How many 留空 → 自動改打 sizing 端點,結果附探測足跡 |
| 表單蓋不住的功能 | Advanced JSON(粗粒度 requirements、brownfield、vm_ids 型規則)勾 override |

---

## Alternative & Trade-offs

主要替代方案已列於「關鍵設計決策」表(pinned vs used-only、掃描 vs 二分、
grandfather vs INFEASIBLE 等),完整推導見三份 ADR。整體層面上還考慮過
**「把循序性建成單一 CP-SAT 模型」**(S 個步驟展開成一個大模型、台數當決策
變數):理論上一次 solve 得精確答案,但循序建置的語意正是「逐步提交、不能
回頭搬遷」— 展開成聯合模型就失去了要量測的東西,等於重寫 solver 核心去
回答一個更簡單工具就能回答的問題。

**接受的代價**:sizing 下界離真值遠時線性掃描多跑幾輪(有預算护栏與
bounds 回報);rollout 模擬與真實建置之間仍有需求飄移(建置前夕重打一次
即可,呼叫無狀態);sizing 答案綁定使用者指定的拓撲形狀。

## Risk & Mitigations

| 風險 | 緩解 |
|---|---|
| Scheduler 端誤解 used 合約(自行預扣 pinned)→ 幽靈容量 | 正規化扣到負值即 INPUT_ERROR(「used 必含 pinned 消耗」的訊息);合約寫進 `VM.pinned_to` docstring 與本文件 |
| 有人「優化」回二分搜尋 | `tests/test_rollout_sizing.py::TestSizingNonMonotonic` 以暴力核對釘死非單調反例,改回二分即紅 |
| 下界被改成高估(如分組相加)→「最小」不再最小 | 性質測試 `test_floor_never_exceeds_the_real_answer`(floor ≤ 暴力線性掃描的真值) |
| 建模錯誤燒光探測預算,偽裝成「多買機器」 | `rollout_sizing._validate` pre-flight 六類 INPUT_ERROR |
| UI 反推(loadIntoForm)悄悄改寫使用者拓撲/pool | 生成器 replay 核對,不能精確重現就退回 JSON mode(原樣送出) |
| 狀態字串是 Go 端分支依據 | 本文件與 CLAUDE.md 均標注「勿改字串」;新增狀態走新字串 |

## Rollout Plan

- **純新增、無 breaking change**:`pinned_to` 選填(預設 None),不帶就是
  舊行為;兩個新端點不影響既有路由;UI 仍由 `ENABLE_UI` 閘控。
- **Scheduler 接入順序**:(1) 先接 add-node(標註在輸入、過濾在輸出兩條
  規則);(2) 規劃工具接 rollout 模擬;(3) 建廠流程接 sizing。三者獨立,
  可分批上線。
- **回滾**:scheduler 停止送 `pinned_to` 即回到舊行為;無資料遷移。

## Open Questions

- Per-spec landable 預警(「哪個 spec 快放不下了」)的優先權與呈現方式。
- Brownfield sizing:「已有 N 台,照此順序還要增購幾台」— 搜尋迴圈可重用,
  需要定義起始庫存與模板的關係。
- 飄移工作流:建置前自動重跑剩餘步驟的模擬(scheduler 端排程 vs 人工觸發)。
- 探測輪數長期 >3 時是否補強下界(ADR-014 的重新審視訊號)。

## Decision Log

| Decision | Reason | Follow-ups |
|---|---|---|
| 建置順序由使用者指定,不做自動排序 | 順序受業務約束(有時就是不能照最優順序建);模擬器忠實重放輸入 | 未來可加「試排序」建議模式 |
| 主路徑是 pure solve,splitter 同步支援 | 生產環境大量使用 pure solve;splitter 路徑順手修了 failover/exclusive 未傳遞的既有 bug | — |
| sizing:最小總台數、per-AG 差 ≤1、單一機型、greenfield | 與使用者確認的 v1 範圍 | 混合機型歸 planner;brownfield 見 Open Questions |
| Rollout UI 以取代 Topology UI 為目標 | 功能面已對齊(sh/ex、max/BM、failover、spread AGs、all-at-once);tightness 為 mockgen 專屬(0.7)不搬 | Topology 頁短期並存 |
| 文件語言:對話/ADR/EP 中文,程式/註解/commit 英文 | 專案既有慣例 | — |
