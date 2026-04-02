# Go Scheduler 實作指南：VM Placement Solver API

> **適用版本**: branch `zs/implement-splitter`
> **最後更新**: 2026-03-30

---

## 目錄

1. [兩條 API 路徑](#1-兩條-api-路徑)
2. [Breaking Change：`slot_tshirt_sizes` → `vm_specs`](#2-breaking-changeslot_tshirt_sizes--vm_specs)
3. [新 endpoint 的 Request 格式](#3-新-endpoint-的-request-格式)
4. [完整 Request 範例](#4-完整-request-範例)
5. [Response 格式說明](#5-response-格式說明)
6. [Hostname 分配指引（Scheduler 端邏輯）](#6-hostname-分配指引scheduler-端邏輯)
7. [新舊 endpoint 對照](#7-新舊-endpoint-對照)

---

## 1. 兩條 API 路徑

### 舊方式：`POST /v1/placement/solve`

Scheduler **自行決定**每台 VM 的規格與數量，solver 只負責 placement（放哪台 BM）。

```
Scheduler                             Solver
    │                                    │
    │  自行決定：3 master × 4 CPU         │
    │            4 worker × 8 CPU         │
    │─────── POST /v1/placement/solve ──▶ │
    │                                    │  只做 placement
    │ ◀────── assignments ─────────────── │
```

**問題**：若 scheduler 猜錯 VM 數量（例如 anti-affinity 只有 2 個 AG 卻送 3 台 master），
solver 回傳 INFEASIBLE，scheduler 要自己重試，沒有系統性的最優解。

---

### 新方式：`POST /v1/placement/split-and-solve`

Scheduler **只送資源預算**，solver 同時決定「幾台 × 哪種 spec」+ placement（一次 solve）。

```
Scheduler                                  Solver
    │                                         │
    │  總需求：master 12 CPU / 48 GiB          │
    │          worker 64 CPU / 256 GiB         │
    │──── POST /v1/placement/split-and-solve ─▶│
    │                                         │  joint optimization:
    │                                         │  split + placement 同時決定
    │ ◀──── split_decisions + assignments ──── │
    │                                         │
    │  得到：master 3 台 × 4 CPU               │
    │        worker 8 台 × 8 CPU               │
    │        + 每台 VM 對應的 BM               │
```

---

## 2. Breaking Change：`slot_tshirt_sizes` → `vm_specs`

`config` 裡的欄位改名。**兩個 endpoint 都受影響。**

```diff
 {
   "config": {
-    "slot_tshirt_sizes": [
+    "vm_specs": [
       {"cpu_cores": 4,  "memory_mib": 16000, "storage_gb": 100,  "gpu_count": 0},
       {"cpu_cores": 8,  "memory_mib": 32000, "storage_gb": 200,  "gpu_count": 0},
       {"cpu_cores": 16, "memory_mib": 64000, "storage_gb": 400,  "gpu_count": 0}
     ]
   }
 }
```

`vm_specs` 有**雙重用途**：
1. **Slot score** objective（原 `slot_tshirt_sizes` 的用途）：評估 BM 剩餘空間能放幾台標準 VM
2. **Splitter spec pool**：`split-and-solve` 的 requirement 未指定 `vm_specs` 時，從這裡 fallback

> ⚠️ 舊版 `sample_request.json` 裡的 `slot_tshirt_sizes` 已不再有效，送出會被 solver 忽略（Pydantic 會丟棄未知欄位）。

---

## 3. 新 endpoint 的 Request 格式

### 頂層欄位

| 欄位 | 型別 | 必填 | 說明 |
|------|------|:----:|------|
| `requirements` | list[ResourceRequirement] | ✅ | 各 role 的資源預算列表 |
| `baremetals` | list[Baremetal] | ✅ | 同 `/solve`，需填 `used_capacity` |
| `vms` | list[VM] | — | 可混入既有 explicit VM（例如已存在不需重排的 VM） |
| `anti_affinity_rules` | list[AntiAffinityRule] | — | 明確指定的 anti-affinity 規則 |
| `config` | SolverConfig | — | solver 調參，見下表 |

### `ResourceRequirement` 欄位

| 欄位 | 型別 | 必填 | 說明 |
|------|------|:----:|------|
| `total_resources` | Resources | ✅ | 這個 role 的**總**資源預算（非單台 VM） |
| `node_role` | string | ✅ | `master` / `worker` / `infra` / `l4lb` |
| `cluster_id` | string | — | 同原本 `VM.cluster_id` |
| `ip_type` | string | — | 同原本 `VM.ip_type`（`auto_generate_anti_affinity` 用） |
| `vm_specs` | list[Resources] \| null | — | 候選 spec；`null` 時 fallback 到 `config.vm_specs` |
| `min_total_vms` | int \| null | — | 強制最少幾台（例如 master 固定 3 台：填 `3`） |
| `max_total_vms` | int \| null | — | 強制最多幾台 |
| `candidate_baremetals` | list[string] | — | 限制此 role 只能放在哪些 BM 上（空 = 不限制）。Go scheduler 依 BM/VM role 篩選後填入，例如 master 只能住 control-plane BM |

### `SolverConfig` 完整欄位（含新增）

| 欄位 | 型別 | 預設值 | 說明 |
|------|------|:------:|------|
| `max_solve_time_seconds` | float | 30.0 | solver 最長搜尋時間 |
| `num_workers` | int | 8 | CP-SAT 並行工作數 |
| `allow_partial_placement` | bool | false | 容許部分放置（不強制全部 VM 必須有位置） |
| `auto_generate_anti_affinity` | bool | true | 自動為同 role + ip_type 的 VM 生成 anti-affinity 規則 |
| `w_consolidation` | int | 10 | 集中放置（少用 BM）的權重 |
| `w_headroom` | int | 8 | 避免 BM 超載的權重 |
| `headroom_upper_bound_pct` | int | 90 | BM 使用率安全上限（%） |
| `w_slot_score` | int | 0 | 剩餘空間可用性獎勵權重 |
| `vm_specs` | list[Resources] | [] | 全局 spec pool（原 `slot_tshirt_sizes`） |
| `w_resource_waste` | int | 5 | **新增**：懲罰 over-allocation（split 盡量選 zero-waste spec） |

---

## 4. 完整 Request 範例

### 情境 A：Initial Cluster Build（3 master + worker 預算）

適用：全新 cluster，每個 role 從序號 0001 開始。

```json
POST /v1/placement/split-and-solve
{
  "requirements": [
    {
      "total_resources": {
        "cpu_cores": 12, "memory_mib": 48000, "storage_gb": 300, "gpu_count": 0
      },
      "node_role": "master",
      "cluster_id": "cluster-prod-1",
      "ip_type": "routable",
      "vm_specs": [
        {"cpu_cores": 4, "memory_mib": 16000, "storage_gb": 100, "gpu_count": 0}
      ],
      "min_total_vms": 3,
      "max_total_vms": 3
    },
    {
      "total_resources": {
        "cpu_cores": 64, "memory_mib": 256000, "storage_gb": 1600, "gpu_count": 0
      },
      "node_role": "worker",
      "cluster_id": "cluster-prod-1",
      "ip_type": "routable",
      "vm_specs": [
        {"cpu_cores": 8,  "memory_mib": 32000, "storage_gb": 200, "gpu_count": 0},
        {"cpu_cores": 16, "memory_mib": 64000, "storage_gb": 400, "gpu_count": 0}
      ]
    }
  ],
  "baremetals": [
    {
      "id": "bm-001",
      "hostname": "bare-001.rack1.site-a",
      "total_capacity": {"cpu_cores": 64, "memory_mib": 256000, "storage_gb": 2000, "gpu_count": 0},
      "used_capacity":  {"cpu_cores": 0,  "memory_mib": 0,      "storage_gb": 0,    "gpu_count": 0},
      "topology": {"site": "site-a", "phase": "p1", "datacenter": "dc-1", "rack": "rack-1", "ag": "ag-1"}
    },
    {
      "id": "bm-002",
      "hostname": "bare-002.rack2.site-a",
      "total_capacity": {"cpu_cores": 64, "memory_mib": 256000, "storage_gb": 2000, "gpu_count": 0},
      "used_capacity":  {"cpu_cores": 0,  "memory_mib": 0,      "storage_gb": 0,    "gpu_count": 0},
      "topology": {"site": "site-a", "phase": "p1", "datacenter": "dc-1", "rack": "rack-2", "ag": "ag-2"}
    },
    {
      "id": "bm-003",
      "hostname": "bare-003.rack3.site-a",
      "total_capacity": {"cpu_cores": 96, "memory_mib": 384000, "storage_gb": 4000, "gpu_count": 0},
      "used_capacity":  {"cpu_cores": 0,  "memory_mib": 0,      "storage_gb": 0,    "gpu_count": 0},
      "topology": {"site": "site-a", "phase": "p1", "datacenter": "dc-2", "rack": "rack-3", "ag": "ag-3"}
    }
  ],
  "config": {
    "auto_generate_anti_affinity": true,
    "w_consolidation": 10,
    "w_headroom": 8,
    "w_resource_waste": 5
  }
}
```

---

### 情境 B：Add Node（cluster 現有 worker 0001–0003，追加 worker 預算）

**Request 結構與情境 A 完全相同。** 差異只在兩處：

1. `requirements` 只填**新增的**資源預算（不含已存在的 VM）
2. `baremetals` 的 `used_capacity` 要填入**目前已使用量**（含現有 VM 佔用的資源）

```json
POST /v1/placement/split-and-solve
{
  "requirements": [
    {
      "total_resources": {
        "cpu_cores": 32, "memory_mib": 128000, "storage_gb": 800, "gpu_count": 0
      },
      "node_role": "worker",
      "cluster_id": "cluster-prod-1",
      "ip_type": "routable",
      "vm_specs": [
        {"cpu_cores": 8, "memory_mib": 32000, "storage_gb": 200, "gpu_count": 0}
      ]
    }
  ],
  "baremetals": [
    {
      "id": "bm-001",
      "hostname": "bare-001.rack1.site-a",
      "total_capacity": {"cpu_cores": 64, "memory_mib": 256000, "storage_gb": 2000, "gpu_count": 0},
      "used_capacity":  {"cpu_cores": 24, "memory_mib": 96000,  "storage_gb": 600,  "gpu_count": 0},
      "topology": {"site": "site-a", "phase": "p1", "datacenter": "dc-1", "rack": "rack-1", "ag": "ag-1"}
    },
    {
      "id": "bm-002",
      "hostname": "bare-002.rack2.site-a",
      "total_capacity": {"cpu_cores": 64, "memory_mib": 256000, "storage_gb": 2000, "gpu_count": 0},
      "used_capacity":  {"cpu_cores": 0,  "memory_mib": 0,      "storage_gb": 0,    "gpu_count": 0},
      "topology": {"site": "site-a", "phase": "p1", "datacenter": "dc-1", "rack": "rack-2", "ag": "ag-2"}
    }
  ],
  "config": {
    "auto_generate_anti_affinity": false,
    "w_consolidation": 10,
    "w_headroom": 8,
    "w_resource_waste": 5
  }
}
```

> **注意**：`used_capacity` 必須正確反映 BM 目前的使用量，solver 依此計算剩餘容量。
> Scheduler 在送出 request 前應先查詢 inventory API 取得最新數字。

---

### 情境 C：Role-Based BM Filtering（master 只住 control-plane BM）

適用：BM 有 role 區分（control-plane / worker），需限制 VM 只能放在對應 role 的 BM 上。

**做法**：Go scheduler 在送出 request 前，依 BM/VM role 篩選出合格的 BM ID 列表，填入 `candidate_baremetals`。

```json
POST /v1/placement/split-and-solve
{
  "requirements": [
    {
      "total_resources": {
        "cpu_cores": 12, "memory_mib": 48000, "storage_gb": 300, "gpu_count": 0
      },
      "node_role": "master",
      "cluster_id": "cluster-prod-1",
      "ip_type": "routable",
      "vm_specs": [
        {"cpu_cores": 4, "memory_mib": 16000, "storage_gb": 100, "gpu_count": 0}
      ],
      "min_total_vms": 3,
      "max_total_vms": 3,
      "candidate_baremetals": ["bm-cp-01", "bm-cp-02", "bm-cp-03"]
    },
    {
      "total_resources": {
        "cpu_cores": 64, "memory_mib": 256000, "storage_gb": 1600, "gpu_count": 0
      },
      "node_role": "worker",
      "cluster_id": "cluster-prod-1",
      "ip_type": "routable",
      "vm_specs": [
        {"cpu_cores": 8,  "memory_mib": 32000, "storage_gb": 200, "gpu_count": 0},
        {"cpu_cores": 16, "memory_mib": 64000, "storage_gb": 400, "gpu_count": 0}
      ],
      "candidate_baremetals": ["bm-wk-01", "bm-wk-02"]
    }
  ],
  "baremetals": [
    {
      "id": "bm-cp-01",
      "total_capacity": {"cpu_cores": 32, "memory_mib": 128000, "storage_gb": 1000, "gpu_count": 0},
      "used_capacity":  {"cpu_cores": 0,  "memory_mib": 0,      "storage_gb": 0,    "gpu_count": 0},
      "topology": {"site": "site-a", "phase": "p1", "datacenter": "dc-1", "rack": "rack-1", "ag": "ag-1"}
    },
    {
      "id": "bm-cp-02",
      "total_capacity": {"cpu_cores": 32, "memory_mib": 128000, "storage_gb": 1000, "gpu_count": 0},
      "used_capacity":  {"cpu_cores": 0,  "memory_mib": 0,      "storage_gb": 0,    "gpu_count": 0},
      "topology": {"site": "site-a", "phase": "p1", "datacenter": "dc-1", "rack": "rack-2", "ag": "ag-2"}
    },
    {
      "id": "bm-cp-03",
      "total_capacity": {"cpu_cores": 32, "memory_mib": 128000, "storage_gb": 1000, "gpu_count": 0},
      "used_capacity":  {"cpu_cores": 0,  "memory_mib": 0,      "storage_gb": 0,    "gpu_count": 0},
      "topology": {"site": "site-a", "phase": "p1", "datacenter": "dc-2", "rack": "rack-3", "ag": "ag-3"}
    },
    {
      "id": "bm-wk-01",
      "total_capacity": {"cpu_cores": 96, "memory_mib": 384000, "storage_gb": 4000, "gpu_count": 0},
      "used_capacity":  {"cpu_cores": 0,  "memory_mib": 0,      "storage_gb": 0,    "gpu_count": 0},
      "topology": {"site": "site-a", "phase": "p1", "datacenter": "dc-2", "rack": "rack-4", "ag": "ag-1"}
    },
    {
      "id": "bm-wk-02",
      "total_capacity": {"cpu_cores": 96, "memory_mib": 384000, "storage_gb": 4000, "gpu_count": 0},
      "used_capacity":  {"cpu_cores": 0,  "memory_mib": 0,      "storage_gb": 0,    "gpu_count": 0},
      "topology": {"site": "site-a", "phase": "p1", "datacenter": "dc-2", "rack": "rack-5", "ag": "ag-2"}
    }
  ],
  "config": {
    "auto_generate_anti_affinity": true,
    "w_consolidation": 10,
    "w_headroom": 8,
    "w_resource_waste": 5
  }
}
```

> **重點**：
> - `candidate_baremetals` 為**可選欄位**，空陣列或不填表示不限制（所有 BM 都可用）
> - Solver 內部會將此清單傳遞給每個 synthetic VM，與原本 `/v1/placement/solve` 的 `VM.candidate_baremetals` 行為一致
> - 若 `candidate_baremetals` 中的 BM ID 不在 `baremetals` 陣列中，該 ID 會被靜默忽略
> - Splitter 在篩選 spec 時只會考慮 candidate BMs 的容量（避免選到只能放在非候選 BM 上的 spec）

---

## 5. Response 格式說明

```json
{
  "success": true,
  "split_decisions": [
    {
      "node_role": "master",
      "vm_spec": {"cpu_cores": 4, "memory_mib": 16000, "storage_gb": 100, "gpu_count": 0},
      "count": 3
    },
    {
      "node_role": "worker",
      "vm_spec": {"cpu_cores": 8, "memory_mib": 32000, "storage_gb": 200, "gpu_count": 0},
      "count": 8
    }
  ],
  "assignments": [
    {"vm_id": "split-r0-s0-0", "baremetal_id": "bm-001", "ag": "ag-1"},
    {"vm_id": "split-r0-s0-1", "baremetal_id": "bm-002", "ag": "ag-2"},
    {"vm_id": "split-r0-s0-2", "baremetal_id": "bm-003", "ag": "ag-3"},
    {"vm_id": "split-r1-s0-0", "baremetal_id": "bm-001", "ag": "ag-1"},
    ...
  ],
  "solver_status": "OPTIMAL",
  "solve_time_seconds": 0.23,
  "unplaced_vms": [],
  "diagnostics": {}
}
```

### 欄位說明

| 欄位 | 說明 |
|------|------|
| `split_decisions` | **Scheduler 的行動指令**：根據這份清單在 Kubernetes 建立對應數量與規格的 VM |
| `assignments` | **Placement mapping**：`vm_id → baremetal_id`，決定每台 VM 放在哪台 BM 上 |
| `vm_id` 格式 | `split-r{req_idx}-s{spec_idx}-{k}`，為 internal ID，**不應在 scheduler 端 parse 格式** |
| `solver_status` | `OPTIMAL` / `FEASIBLE` / `INFEASIBLE` / `UNKNOWN` |
| `unplaced_vms` | 放置失敗的 vm_id 列表（通常為空；`allow_partial_placement=true` 時可能有值） |

---

## 6. Hostname 分配指引（Scheduler 端邏輯）

**Hostname 命名與流水號完全由 Scheduler 負責**，solver 不涉及這塊業務邏輯。

Solver 是 stateless 最佳化工具，不知道 cluster 目前有哪些 VM 或命名規則。

### 流程

```
步驟 1：呼叫 solver 之前
  查詢 cluster 現有 VM 列表，取得每個 node_role 的最大序號
  例：
    master → max_seq = 3  (cluster-prod-1-mst-0001 ~ 0003 已存在)
    worker → max_seq = 0  (初次建置，尚無 worker)

步驟 2：收到 solver response
  解讀 split_decisions，得知需要建立的 VM 數量與規格
  例：master 3 台 × 4CPU、worker 8 台 × 8CPU

步驟 3：分配 hostname
  同一 role 的 synthetic VM 按 vm_id 字母序排列（vm_id 格式已保證排列穩定）
  從 max_seq + 1 開始依序分配

  master（max_seq=3，需要 3 台，從 0004 開始）:
    split-r0-s0-0  →  cluster-prod-1-mst-0004  on bm-001  (ag-1)
    split-r0-s0-1  →  cluster-prod-1-mst-0005  on bm-002  (ag-2)
    split-r0-s0-2  →  cluster-prod-1-mst-0006  on bm-003  (ag-3)

  worker（max_seq=0，需要 8 台，從 0001 開始）:
    split-r1-s0-0  →  cluster-prod-1-wrk-0001  on bm-001  (ag-1)
    split-r1-s0-1  →  cluster-prod-1-wrk-0002  on bm-001  (ag-1)
    ...

步驟 4：在 Kubernetes 建立真實 VM
  以 split_decisions 決定 VM spec 與數量
  以 assignments 決定每台 VM 的 target BM（placement）
```

### 關鍵原則

- `vm_id` 只是 solver 的 internal 識別符，不帶任何業務含義
- Hostname 格式（`{cluster_name}-{role_abbrev}-{seq:04d}`）由 scheduler 端統一定義
- 同一 spec 的 synthetic VM 在 `assignments` 中已按 vm_id 排列，順序確定性有保證
- Add node 場景只需查好 `max_seq` 再從 `max_seq + 1` 繼續即可，無需改 solver request 格式

---

## 7. 新舊 endpoint 對照

| 比較項目 | 舊：`POST /v1/placement/solve` | 新：`POST /v1/placement/split-and-solve` |
|---------|-------------------------------|----------------------------------------|
| Scheduler 需提供 | 每台 VM 的確切 spec + hostname | 每個 role 的總資源預算 |
| VM 數量 | Scheduler 自行決定 | Solver 決定（可加 `min/max_total_vms` 限制） |
| Spec 選擇 | Scheduler 自行決定 | Solver 從 `vm_specs` pool 中選 waste 最小的組合 |
| Hostname | Request 裡直接填入 | Response 後由 Scheduler 按序號分配 |
| INFEASIBLE 重試 | Scheduler 猜錯要自行重試 | Solver 在同一次 solve 內自動找可行解 |
| Add node 複雜度 | 需自行計算幾台 + 選哪種 spec | 只需填總預算 + 正確的 `used_capacity` |
| `config.vm_specs` | Slot score 評估用 | 同上，同時也是 splitter 的 spec pool |
