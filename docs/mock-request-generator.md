# Mock Request Generator

> **作者**: Claude (claude-opus-4-8)
> **日期**: 2026-06-22
> **狀態**: v1 Implemented
> **相關分支**: `claude/admiring-hypatia-1dfi16`

---

## 目錄

1. [Summary](#1-summary)
2. [Goals / Non-Goals](#2-goals--non-goals)
3. [Problem & Motivation](#3-problem--motivation)
4. [Parameter Surface](#4-parameter-surface)
5. [Feasibility Strategy](#5-feasibility-strategy)
6. [Generation Algorithm](#6-generation-algorithm)
7. [API Contract](#7-api-contract)
8. [Validation Rules](#8-validation-rules)
9. [File Layout](#9-file-layout)
10. [Future Work (v2)](#10-future-work-v2)

---

## 1. Summary

新增一支 `POST /api/mock/generate` 端點，依使用者給定的少量高階參數，**程式化生成一份完整、可直接丟進 `/v1/placement/solve` 的 `PlacementRequest`**。

v1 聚焦在 **greenfield（空 BM，`used_capacity = 0`）+ 建構式可行性保證**：生成器先用貪婪法鋪出一份合法 placement（ground truth），再用真實 solver 自我驗證，確保產出的情境一定可解。產出同時回傳 ground-truth placement，可用於回歸測試與驗證 solver 行為。

---

## 2. Goals / Non-Goals

### Goals
- 用 ~20 個高階旋鈕生成多 cluster、多 role、多拓樸維度的真實情境
- BM 規格由使用者以「幾個固定 profile」描述（對應真實機房只有幾種機型）
- 預設保證可解，並回傳 ground-truth placement
- 完整複用現有 `app/models.py`，不動 `solver.py`

### Non-Goals (v1)
- `stress` 模式（刻意製造不可解情境）
- brownfield（`used_capacity > 0` 的既有負載）
- `split-and-solve` 形態的輸出（`ResourceRequirement`）
- profile 綁定特定 rack（GPU 機集中某些 rack）

---

## 3. Problem & Motivation

目前要測 solver 或 demo，得手刻 `examples/*.json`，VM/BM/規則三者要彼此自洽（candidate 要對、容量要夠、AG 要撐得起 `target_spread`）很容易手滑。生成器把這些不變式封進程式，讓使用者只需描述「我想要什麼規模的情境」。

---

## 4. Parameter Surface

`GenerateRequest` 的欄位（皆有預設）。注意：因預設 `anti_affinity=true` 且 `ip_type_by_role` 不留 fallback，**最小可用請求至少要提供各 multi-VM role 的 `ip_type_by_role`**（或關掉 `anti_affinity`）；空 `{}` 會回 400（見 §8）。

| 組 | 參數 | 預設 | 說明 |
|----|------|------|------|
| 全域 | `seed` | 隨機 | 給定即可重現 |
| | `target` | `"solve"` | v1 僅支援 `solve` |
| | `verify` | `true` | 產後用真實 solver 自我驗證 |
| Cluster/VM | `clusters` | 1 | cluster 數 |
| | `roles` | `{master:3,worker:3,infra:2}` | 每 cluster 各 role 的 VM 數 |
| | `vm_size_profile` | `"medium"` | fallback：`small`/`medium`/`large` 縮放各 role 基準 demand |
| | `role_demands` | null | 逐 role 指定 `Resources`，覆寫 profile |
| | `vm_specs` | `{}` | 具名 VM 規格目錄，如 `{"big": {...}, "small": {...}}` |
| | `spec_by_role` | `{}` | 指派：key 為 `"<role>"` 或 `"<role>:<ip_type>"`（後者優先），value 為 `vm_specs` 的名稱 |
| | `ip_type_by_role` | `{}` | **顯式**設定各 role 的 ip_type；值可為字串或加權分佈 `{routable:0.5,...}`。不自動帶、不留 fallback |
| Baremetal | `bm_profiles` | `[standard]` | 固定機型清單，每項 `{name, capacity, count?, roles?}`；`count` 省略 → 彈性數量（見 §5）；`roles` 設定 → 該機型只服務這些 role（專屬 pool） |
| Topology | `sites`/`phases`/`datacenters`/`rooms`/`racks`/`ags` | `1/1/1/1/4/3` | 各維度桶數，BM 平均撒 |
| 規則 | `anti_affinity` | true | 開啟 solver 自動反親和（吃 `target_spread` 的 key） |
| | `target_spread` | `{ag:3}` | key=分散維度（硬），value=期望桶數（軟，警告線） |
| | `failover` | false | 產生 master→learner 的 N-1 failover 規則 |
| | `max_per_bm` | null | 給數字即開每台同群上限 |
| 其他 | `tightness` | 0.7 | demand/capacity 目標比；僅在有彈性 profile 時用於估數量 |
| | `candidate_strategy` | `"same_site"` | `all`/`same_site`/`same_room`/`topology_affinity`/`by_role_pool`。當任一 `bm_profile` 設了 `roles`，自動切換為 `by_role_pool`（依 role 專屬機隊決定候選），忽略拓樸策略 |
| | `config_overrides` | `{}` | 直接覆寫任何 `SolverConfig` 欄位 |

### `target_spread` 語意（重要）

solver 的 `auto_generate_anti_affinity` 把 VM 依 `(cluster_id, ip_type, node_role)` 分組，每組 ≥2 台產一條規則，`spread_on = target_spread.keys()`。
- **key 決定「分散在哪些維度」→ 硬約束**
- **value 只是「期望分散到幾桶」→ 軟性目標**，不足只發 `spread_below_target` advisory，不會讓求解失敗
- 真正的每桶硬上限是自動平衡值 `⌈|VMs| / 桶數⌉`；要硬綁需用顯式 `cap_per_bucket`

---

## 5. Feasibility Strategy

核心前提：**空 BM**（`used_capacity = 0`），容量爭用只來自本次要放的 VM。

BM 機隊大小由 `bm_profiles` 的 `count` 決定可行性語意：

- **profile 省略 `count`（彈性）**：生成器依 `tightness` 估算需要幾台，複製該機型直到
  `Σ capacity ≥ Σ demand / tightness`（四維皆滿足）且每個 AG 至少一台。此模式下容量必然充足。
- **profile 指定 `count`（固定）**：機隊規格與數量完全照給，`tightness` 被忽略。容量可能不足，屬 best-effort。

無論哪種，產出後若 `verify=true`，會在程序內跑一次真實 `VMPlacementSolver`：
- `OPTIMAL`/`FEASIBLE` → `feasibility = "verified"`
- 其他 → `feasibility = "infeasible"`，並把 `solver_status` 與診斷帶回
- `verify=false` → `feasibility = "unverified"`

這讓「保證可解」不是靠人工推導不變式，而是**真的跑一次 solver 自我證明**。

---

## 6. Generation Algorithm

1. **Topology**：產生 `racks` 個 rack，site/room/ag 以 round-robin 平均分配；若 `ags < target_spread[ag]` 或 `racks < target_spread[rack]` 自動上調並記入診斷。
2. **VMs**：對每個 `cluster-i` 的每個 role 產生對應數量 VM；`ip_type` 由 `ip_type_by_role` 解析（加權分佈用 seeded RNG 抽樣）；demand 解析順序為 `spec_by_role["role:ip_type"]` → `spec_by_role["role"]`（查 `vm_specs`）→ `role_demands[role]` → `vm_size_profile` 縮放後的 role 基準。
3. **BM fleet**：實例化固定 profile，必要時依 §5 補彈性 profile，平均撒到 racks。
4. **Candidates**：
   - **pool 模式**（任一 profile 設 `roles`，或 `candidate_strategy=by_role_pool`）：VM 的 `candidate_baremetals` = 所有「pool 服務其 role」的 BM（空 `roles` = 共用 pool 服務所有 role）；某 role 無任何 pool 服務 → 回 400。彈性 sizing 改為**每 pool 依其服務 role 的 demand 估數量**，並讓每個 pool 各自跨 rack/AG 平均分佈（避免單一 pool 漏 AG 害 anti-affinity 不可解）。
   - **拓樸模式**（預設）：依 `candidate_strategy` 給每個 cluster 決定 home scope，candidate = scope 內的 BM。
5. **Constructive placement（ground truth）**：把每個自動反親和群依 `⌈n/桶數⌉` 平均鋪到各 AG，於候選 BM 中挑容量足夠者放置（同時遵守 `max_per_bm`）；非分群的單台 VM 直接擇一候選放置。
6. **Assemble**：組出 `PlacementRequest`（config 帶上 `auto_generate_anti_affinity`、`target_spread`、`auto_generate_max_per_bm`/`default_max_per_bm`），套用 `config_overrides`。`failover=true` 時**每個 cluster 各產一條** N-1 規則（primary=該 cluster 的 master、backup=同 cluster 的 learner、fault_domain=ag），確保 backup 不會跨 cluster 互相支援；缺 master 或 learner 則略過並記入診斷。
7. **Verify**：選擇性跑 solver，產生 `feasibility`。

---

## 7. API Contract

`POST /api/mock/generate`

- **Request**：`GenerateRequest`（見 §4）
- **Response** `GenerateResponse`：
  - `request`: 完整 `PlacementRequest`
  - `ground_truth`: `list[PlacementAssignment]`（建構式 placement）
  - `feasibility`: `"verified" | "unverified" | "infeasible"`
  - `diagnostics`: dict（自動上調、彈性補機數、solver 驗證狀態等）

---

## 8. Validation Rules

- `anti_affinity=true` 時，任何 `count ≥ 2` 的 role 必須在 `ip_type_by_role` 有非空值，否則回 **400**（因為空 `ip_type` 會被 solver 自動分組靜默略過，導致規則失效）。
- `roles` 的 key 必須是合法 `NodeRole`。
- `bm_profiles` 至少一項；`capacity` 四維非負。
- `target_spread`/`config_overrides` 交由現有 `SolverConfig` validator 把關。

---

## 9. File Layout

```
app/mockgen.py              # 生成器：GenerateRequest/Response + 演算法 + APIRouter
app/server.py               # 掛載 mockgen.router
app/web_static/             # /ui「Generate mock」表單（index.html / js/mockform.js / js/main.js / js/api.js / styles.css）
examples/mock/*.json        # GenerateRequest 範例（UI preset 下拉、curl、CLI）
tests/test_mockgen.py       # 單元 + 端點測試
docs/mock-request-generator.md
```

## 9a. Web UI

`/ui` 側欄新增 **Generate mock** 卡片，採**逐欄位表單**（非 raw JSON），對非開發者友善：
- clusters/seed、**Candidate BMs**（`candidate_strategy`，控制每台 VM 可落的 baremetal）
- **VM specs 動態列**（name + cpu/mem/storage/gpu）定義具名規格目錄
- **Roles 表格**：每 role 的 count、ip_type 下拉、**spec 下拉**（指派該 role/ip_type 用哪個 VM spec）
- **Baremetal profiles 動態列**（name + cpu/mem/storage/gpu + count；count 留白＝自動估數量），可增刪
- topology（sites/rooms/racks/ags）、規則（anti-affinity / failover 勾選、spread AG、max/BM、tightness）
- **Advanced overrides (JSON)** 摺疊區：放 `role_demands`、`config_overrides`、加權 `ip_type` 等
  巢狀進階項，**deep-merge 疊在表單值之上**（escape hatch，確保完整參數面不被欄位化限制）

兩顆按鈕：**Generate**（只把產出的 `PlacementRequest` 灌入 solver 編輯器並切到 `solve`）與
**Generate & Run**（產生後立即求解並視覺化）。狀態列顯示 `feasibility` 與 VM/BM/AG 計數。
選 `examples/mock/` preset 會回填表單（無法欄位化的鍵自動寫入 Advanced 區，不靜默丟失）；
mock preset 為 GenerateRequest（非 PlacementRequest），故從主 example 下拉過濾掉、只出現在 preset 下拉。
表單邏輯獨立在 `app/web_static/js/mockform.js`。

---

## 10. Future Work (v2)

- `feasibility="stress"`：刻意製造容量/分散不可解情境，測 solver 診斷
- brownfield：`used_capacity > 0` 的既有負載旋鈕
- `split-and-solve` 輸出（`ResourceRequirement`）
- profile 綁定特定 rack/room（異質硬體佈局）
