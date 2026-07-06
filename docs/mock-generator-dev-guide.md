# Mock 情境產生器 — 後端開發／維護指南（Mentor 版）

> 對象：接手維護或擴充 **`app/mockgen.py`** 的工程師
> 前置知識：讀過 `app/models.py`（資料契約）與 `app/solver.py` 的公開介面
> 範圍：**只談後端產生器**。前端表單（`app/web_static/js/mockform.js`）不在此文
> 搭配閱讀：`docs/mock-request-generator.md`（設計決策的 what/why）、`docs/mock-ui-guide.md`（使用者視角）

這份文件的目標，是讓你讀完後能**獨立修 bug、加旋鈕、擴功能**，而不是只知道它「會動」。所以我會花很多篇幅講**為什麼**、**耦合在哪**、**坑在哪**。

---

## 目錄

1. [一句話心智模型](#1-一句話心智模型)
2. [檔案定位與資料流](#2-檔案定位與資料流)
3. [關鍵型別與資料契約](#3-關鍵型別與資料契約)
4. [逐階段深入（generate() 的七步）](#4-逐階段深入generate-的七步)
5. [與 solver 的耦合契約（最重要）](#5-與-solver-的耦合契約最重要)
6. [設計取捨與已知限制](#6-設計取捨與已知限制)
7. [擴充食譜（Cookbook）](#7-擴充食譜cookbook)
8. [測試策略](#8-測試策略)
9. [維護陷阱清單（Gotchas）](#9-維護陷阱清單gotchas)
10. [除錯手冊](#10-除錯手冊)

---

## 1. 一句話心智模型

> **用建構法鋪一份「善意的合法解」（ground truth），再把整包丟給真實 solver 跑一次來「證明」它可解——而不是靠人工推導不變式去保證。**

這是整個模組的靈魂。請記住兩個推論：

- **建構式擺放（`_place`）不是權威**，它只是產出一份參考解與診斷。真正的可行性判定來自 `verify`（跑 `VMPlacementSolver`）。所以 `_place` 允許在擺不下時「放寬」把 VM 硬塞進去——寧可產出一份「可能違反約束的 ground truth」，也要讓 `verify` 去講真話。
- **產生器的職責是「產出與 solver 語意自洽的輸入」**。它大量地「鏡射」solver 的分群與約束邏輯（見 §5）。一旦 solver 改了分群方式，產生器就得跟著改，否則會產出靜默失效的資料。

---

## 2. 檔案定位與資料流

`app/mockgen.py` 是一支 **FastAPI router + 一個生成器類別**。對外只有一個端點：

```
POST /api/mock/generate   (GenerateRequest) -> GenerateResponse
```

純函式入口是 `generate_mock_request(req)`，它 `return _Generator(req).generate()`。**測試都走這個純函式入口**（不經 HTTP），端點只是薄薄一層轉發。

核心流程（`_Generator.generate()`，`mockgen.py:537`）是一條**線性管線**，七個步驟依序呼叫，用 `self.diag` 累積診斷、用 `self._pool_mode` / `self._bm_pool_roles` 傳遞中繼狀態：

```
GenerateRequest
   │
   ▼  _build_racks()                → list[Topology]      (拓樸桶)
   ▼  _build_vms()                  → list[VM]            (含 demand / ip_type 解析)
   ▼  _validate_ip_for_anti_affinity()   （可能丟 400）
   ▼  _build_baremetals()           → list[Baremetal]    (設定 _pool_mode / _bm_pool_roles)
   ▼  _assign_candidates()          → 就地填 vm.candidate_baremetals（可能丟 400）
   ▼  _place()                      → list[PlacementAssignment]  (ground truth)
   ▼  組 PlacementRequest（+ failover / max_per_bm 規則 + config）
   ▼  verify（選擇性）→ 跑真實 solver → feasibility
   ▼
GenerateResponse { request, ground_truth, feasibility, diagnostics }
```

**注意順序的隱性依賴**：`_build_baremetals` 會設定 `self._pool_mode` 與 `self._bm_pool_roles`，而 `_assign_candidates` 依賴它們。若你重排管線順序，這兩個屬性會不存在——`_assign_candidates` 用 `getattr(self, "_pool_mode", False)` 做了防禦，但語意會錯。**擴充時請維持這個順序契約**。

---

## 3. 關鍵型別與資料契約

產生器的「輸入型別」自己定義；「輸出型別」全部複用 `app/models.py`，這是刻意的——**產出物就是 solver 的輸入，不能有第二套定義**。

### 3.1 輸入（本檔定義）

- **`BmProfile`**（`mockgen.py:65`）：一種機型。`count=None` → 彈性（由 tightness 估數量）；`roles` 非空 → 專屬 pool。兩個 field validator 把關 `count>=1`、`roles` 是合法 `NodeRole`。
- **`GenerateRequest`**（`mockgen.py:95`）：所有高階旋鈕，全有預設。驗證分兩層：
  - `@field_validator`：單欄位（roles/max_per_bm_by_role 的 key 是合法 role、tightness ∈ (0,1]、bm_profiles 非空…）。
  - `@model_validator(mode="after")`：跨欄位（`spec_by_role` 的值必須是 `vm_specs` 有定義的名字）。
- **`GenerateResponse`**（`mockgen.py:184`）：`request`（完整 `PlacementRequest`）、`ground_truth`、`feasibility`（`verified`/`unverified`/`infeasible`）、`diagnostics`。

### 3.2 輸出（來自 `app/models.py`，務必熟悉）

| 型別 | 產生器怎麼用 |
|------|-------------|
| `Resources`（cpu/mem/storage/gpu，支援 `+`/`-`/`fits_in`） | demand 與 capacity 的四維向量；`_place`/`_build_baremetals` 大量用其運算子 |
| `Topology`（site>phase>datacenter>room>rack，+ ag） | `_build_racks` 產生；6 個維度就是 `SPREAD_DIMENSIONS` |
| `VM`（demand, node_role, ip_type, cluster_id, candidate_baremetals） | `_build_vms` 產生；`candidate_baremetals` 由 `_assign_candidates` 填 |
| `Baremetal`（total/used_capacity, topology） | 一律 `used_capacity=Resources()`（greenfield） |
| `AntiAffinityRule` | 產生器**不自己生**，交給 solver 的 `auto_generate_anti_affinity`（見 §5） |
| `MaxPerBaremetalRule` / `FailoverRule` | 由 `_build_max_per_bm_rules` / `_build_failover_rules` 顯式產生 |
| `SolverConfig` | `_build_config` 組；帶 `auto_generate_anti_affinity`、`target_spread`，套 `config_overrides` |

---

## 4. 逐階段深入（generate() 的七步）

### 4.1 `_build_racks`（拓樸）—— `mockgen.py:203`

產生 `racks` 個 `Topology`，每個維度用 **round-robin** 攤到各桶：

```python
site=f"site-{r % max(1, req.sites) + 1}"   # r 是 0-based rack index
ag=f"ag-{r % ags + 1}"
```

要點：
- **維度數=1 → 該維度塌成單桶**（所有 rack 同值）。這就是「沒設 = 1」在後端的實現。無害，除非你在該維度做分散。
- **auto-bump**：若 `ags < target_spread["ag"]`（或 rack 同理），會自動把 `ags` 上調到 target 並寫進 `diag["auto_bumped"]`。目的：避免「要求分散到 3 個 AG，卻只生了 1 個 AG」這種一定失敗的情境。
- **坑**：`racks` 不是 `ags` 的倍數時，AG 桶大小不均（例：racks=4, ags=3 → 某 AG 只有 1 個 rack）。這會在吃緊情境讓 anti-affinity 擺不下。目前**沒有**自動修正，是已知限制（見 §6）。

### 4.2 `_build_vms` + `_resolve_ip_type` + `_demand_for` —— `mockgen.py:232-270`

對每個 cluster、每個 role、每台 VM：先解析 ip_type，再依 ip_type 解析 demand。

- **ip_type 解析**（`_resolve_ip_type`）：`ip_type_by_role[role]` 可以是字串（直接用）或加權分佈 dict（用 `self.rng.choices` 抽樣）。**這是唯一的隨機來源**，靠 `random.Random(seed)` 保證可重現。
- **demand 解析鏈**（`_demand_for`，先命中先用）：
  1. `spec_by_role["role:ip_type"]` → `spec_by_role["role"]`（查 `vm_specs`）
  2. 內建 `_ROLE_BASELINE[role]`
  歷史上還有 `vm_size_profile` 與 `role_demands` 兩層，已移除以維持單一來源（見 design doc 的演進紀錄）。
- **決定論**：除了 `rng`，沒有其他亂數／時間來源；dict 迭代是插入序穩定。**同 seed + 同輸入 = 同輸出**，這對回歸測試很重要，改動時別破壞它（例如別用 `set` 的迭代序去影響 VM 產生順序）。

### 4.3 `_validate_ip_for_anti_affinity` —— `mockgen.py:272`

這是一個**因耦合而存在的防呆**（細節見 §5）：solver 的 auto anti-affinity 以 `(cluster_id, ip_type, node_role)` 分群，且**會靜默略過 ip_type 為空的 VM**。所以若 `anti_affinity=true` 且某個 count≥2 的 role 沒給 ip_type，規則會靜默失效。與其產出「看似有 HA、實則無」的資料，不如**當場丟 400**。

維護提醒：這條檢查的觸發條件（count≥2、anti_affinity on、ip_type 空）必須跟 solver 的分群條件同步。solver 改了，這裡要改。

### 4.4 `_build_baremetals`（機隊）—— `mockgen.py:311`（最難的一段）

這段做三件事：**決定 pool 模式 → 估算彈性數量 → 依 pool 各自攤到 rack**。

**(a) pool 模式判定**
```python
self._pool_mode = any(p.roles for p in req.bm_profiles)
```
每台 BM 記成 `(capacity, frozenset(roles))`，空 frozenset = 服務所有 role。

**(b) 彈性 sizing（per-profile）**
固定 `count` 的 profile 直接實例化。彈性（`count=None`）的 profile，對「它服務的 role 的 demand 總和」用 tightness 反推需要的容量，複製到夠：
```python
need = self._required(demand, req.tightness)      # ceil(demand / tightness) 四維
# have 累加「所有服務重疊 role 的既有 BM」容量
while (not self._covers(have, need)
       or copies < (min_pool if self._pool_mode else num_ags)) and guard < 100_000:
    ... append 一台 ...
```
- `min_pool = max(target_spread.values())`（anti_affinity 時），確保**每個 pool 至少有足夠 BM 去撐分散**。非 pool 模式則至少 `num_ags` 台。
- `guard < 100_000` 是防呆上限（理論上不會到）。
- **已知過度配置**：`have` 把「服務重疊 role」的固定 BM 也算進來，pool 互斥時精準；pool 重疊時會略微高估（安全，不會不足）。

**(c) per-pool 攤到 rack（曾經的 bug 來源）**
```python
pools: dict[frozenset[str], list[Resources]] = ...   # 依 roles 分組
for roles, caps in pools.items():
    for j, cap in enumerate(caps):
        topo = racks[j % len(racks)]     # 每個 pool 各自從 rack 0 開始 round-robin
```
**為什麼不能用全域 index？** 早期版本用全域 `idx % len(racks)`，導致 worker pool 的 BM 剛好都落在部分 AG、漏掉某些 AG，讓 worker 的跨-AG anti-affinity 無解。改成**每個 pool 獨立 round-robin**，每個 pool 就能均勻覆蓋 AG。這是「verify 抓到、才發現的真 bug」的典型案例——改這段時務必回歸驗證 pool 情境。

同時這裡建立 `self._bm_pool_roles: {bm_id -> frozenset(roles)}`，供 `_assign_candidates` 用。

### 4.5 `_assign_candidates`（候選機）—— `mockgen.py:382`

只有兩種模式：
- **pool 模式**（`_pool_mode`）：每台 VM 的候選 = 所有「pool 服務其 role」的 BM（空 pool 服務所有 role）。某 role 找不到任何 pool → **丟 400**（提示加一個服務該 role 的 profile）。
- **無 pool**：每台 VM 候選 = 全部 BM。

歷史上這裡曾有拓樸式 `candidate_strategy`（same_site/same_room…），已移除（現實只需要「硬體池」語意）。

`candidate_baremetals` **必須非空**——這是 solver 的硬性契約，空的會被 solver 判 INPUT_ERROR。

### 4.6 `_place`（建構式 ground truth）—— `mockgen.py:411`

把 VM 依 solver 的 auto-AA key `(cluster, ip_type, role)` 分群，逐群擺放：

1. `cand_ags` = 該群候選 BM 涵蓋的 AG；`cap_per_ag = ceil(|members| / |cand_ags|)`——**刻意等於 solver auto-AA 的自動平衡上限**，讓 ground truth 與 solver 的分散約束一致。
2. 對每台 VM，round-robin 挑一個「還沒到 `cap_per_ag`」的 AG，在該 AG 內挑「剩餘 cpu 最多」的 BM（best-fit 傾向）放置，同時檢查 `role_cap`（`max_per_bm_by_role`）。
3. **fallback**：若上面擺不下，就退化成「任一候選 BM 只要容量夠就塞」（此時可能違反 AG 上限）。真的還擺不下 → 記進 `diag["unplaced_ground_truth"]`。

**誠實提醒**：因為有 fallback，ground truth **可能違反 anti-affinity 上限**。這是刻意的設計——ground truth 只是善意參考，`verify` 才是權威。若你需要「ground truth 一定合法」，得移除 fallback 並改成成長式（擺不下就加 BM），但那是更大的工程。

### 4.7 規則、config、orchestration

- **`_build_failover_rules`**（`:488`）：`failover=true` 時，**每個 cluster 各一條** N-1 規則（primary=該 cluster 的 master、backup=同 cluster 的 learner、fault_domain=ag）。用 selector 綁 `cluster_id`，避免跨 cluster 互相支援。缺 master 或 learner → 跳過並記 `diag["failover_skipped"]`。
- **`_build_max_per_bm_rules`**（`:508`）：每個 `max_per_bm_by_role` 項目 × 每個 cluster 展開成一條 `MaxPerBaremetalRule`，selector 帶 `(cluster_id, ip_type, node_role)`。ip_type 只在它是「單一字串」時帶入（加權分佈時設 None，退化成「該 role 不分 ip_type」）。
- **`_build_config`**（`:526`）：帶 `auto_generate_anti_affinity`、`target_spread`，最後 `cfg.update(config_overrides)` 讓進階者能覆寫任何 `SolverConfig` 欄位。
- **`generate`**（`:537`）：串起來，寫入規模診斷，`verify=true` 時跑一次 solver 決定 `feasibility`。

---

## 5. 與 solver 的耦合契約（最重要）

產生器有大量「action at a distance」——它假設 solver 用某種方式解讀資料。**這些假設一旦與 `solver.py` 不同步，就會產出靜默錯誤的資料。** 維護時請把這節當 checklist。

| 耦合點 | solver 的行為 | 產生器的對應假設／鏡射 |
|--------|--------------|------------------------|
| **auto anti-affinity 分群** | 依 `(cluster_id, ip_type, node_role)` 分群，每組 ≥2 台產規則；**ip_type 或 cluster_id 為空的 VM 被略過** | `_validate_ip_for_anti_affinity` 用同一組條件擋空 ip_type；`_place` 用同一把 key 分群並用同樣的 `⌈n/桶數⌉` 當 AG 上限 |
| **target_spread 語意** | key = 要分散的維度（硬約束）；value = 期望桶數（**軟性**，不足只發 advisory） | `_build_racks` 用 value 做 `min_pool` 與 auto-bump；別誤把 value 當硬上限 |
| **candidate_baremetals** | 空清單 → INPUT_ERROR | `_assign_candidates` 保證非空，pool 缺 role 時提前 400 |
| **failover N-1 計數** | 每個 fault domain 桶：`primary + backup ≤ |backup|`；前置檢查 `|primary| ≤ |backup|` | per-cluster 展開，讓 learner 數需 ≥ master 數（同 cluster 內） |
| **max_per_bm 規則** | selector 解析成 vm_ids，對每台 BM 限制該群數量 | `_build_max_per_bm_rules` 的 selector 分群與 `_place` 的 `group_key` 一致 |

**維護準則**：改 `solver.py` 的任何分群 key、約束語意、或輸入驗證，請回來檢查這張表對應的 `_*` 方法。加一個回歸測試「產生 → verify == verified」是最省力的保險。

---

## 6. 設計取捨與已知限制

- **只做 greenfield**：`used_capacity` 一律 0。要支援 brownfield（既有負載）需在 `_build_baremetals` 加既有用量、並讓 `_place`/sizing 扣掉。
- **tightness 是估算、非保證**：它只影響彈性數量的估算，不保證可解——所以才有 verify。
- **AG 分佈不均**：`racks` 非 `ags` 倍數時桶大小不均，吃緊情境可能 infeasible。目前靠使用者自己設好，或未來在 sizing 補「湊成 AG 均分」。
- **ground truth 可能非法**：`_place` 的 fallback 會為了「塞得下」放寬 AG 上限。verify 是唯一權威。
- **pydantic 忽略未知欄位**：舊版 payload 帶著已移除的欄位（如 `max_per_bm`、`candidate_strategy`）不會報錯，只會被無視。若要嚴格，可在 `GenerateRequest` 設 `model_config = ConfigDict(extra="forbid")`——但會讓所有拼錯欄位變 422。
- **verify 成本**：大情境 solve 可能慢（solver 有 `max_solve_time_seconds`）。`verify=false` 可跳過，但就失去可行性保證。

---

## 7. 擴充食譜（Cookbook）

### 7.1 加一個新的高階旋鈕
標準六步：
1. `GenerateRequest` 加欄位（給預設值）。
2. 需要驗證就加 `@field_validator` 或 `@model_validator`。
3. 接進對應的 `_build_*` 或 `_place`（想清楚它影響拓樸／VM／BM／規則哪一塊）。
4. 若它會影響可行性，確認 verify 仍過。
5. `tests/test_mockgen.py` 加測試（至少：欄位生效 + 邊界/驗證）。
6. 更新 `docs/mock-request-generator.md` 參數表與（若有）UI。

### 7.2 加一種新規則（仿 failover / max_per_bm）
1. 確認 `app/models.py` 有對應的 Rule 型別、且 solver 會處理它。
2. 寫 `_build_<rule>_rules()`，注意**是否要 per-cluster 展開**（多 cluster HA 幾乎都要，鏡射 auto-AA 的 cluster keying）。
3. 在 `generate()` 組 `PlacementRequest` 時掛上。
4. 若 `_place` 需要在 ground truth 就遵守它，於 `try_place_on` 加對應檢查（像 `role_cap` 那樣）。
5. 測試「產生 → verify == verified」+ 規則內容斷言。

### 7.3 加 `stress` 模式（刻意不可解）
在 `GenerateRequest` 加 `feasibility: Literal["guaranteed","stress"]`；`stress` 時故意讓 sizing 不足（例如把 `_required` 打折）或加互斥約束，並在 `generate()` 斷言 `verify` 回 infeasible。用途：測 solver 的診斷輸出。

### 7.4 加 brownfield（`used_capacity>0`）
- `GenerateRequest` 加既有負載的描述（例如每 profile 的既有用量比例）。
- `_build_baremetals` 產生 BM 時填 `used_capacity`；`_covers`/`_place` 改用 `available_capacity`。
- sizing 要把既有用量計入 `have` 的可用量。

---

## 8. 測試策略

看 `tests/test_mockgen.py`，抓住三種模式：

- **整合式可行性**：`generate_mock_request(...)` 後 `assert resp.feasibility == "verified"`。這一行等於「跑了真 solver」，是最有力的回歸保護。加任何影響擺放的功能都該配一條。
- **結構斷言**：檢查產出的 `request` 內容（例如 `max_per_bm_rules` 逐 cluster 展開、selector 帶對 ip_type）。
- **驗證錯誤**：`with pytest.raises(...)` 或斷 `HTTPException.status_code == 400`（ip_type 缺、pool 缺 role、未知 role）。

另外 `examples/mock/*.json` 是活的 fixtures——可用一個迴圈載入全部、`assert feasibility == "verified"`，確保範例不腐爛（改動語意時，範例也是回歸網）。

**注意 Python 版本**：專案 `requires-python >= 3.13`；用 `make test`（venv 內建 3.13）跑，別用系統預設 python。

---

## 9. 維護陷阱清單（Gotchas）

- **管線順序**：`_build_baremetals` 必須在 `_assign_candidates` 前跑（前者設 `_pool_mode`/`_bm_pool_roles`）。重排要小心。
- **決定論**：唯一亂數是 `self.rng`。別引入 `random.*`、`set` 迭代序、或時間戳去影響輸出，會破壞 seed 可重現。
- **`HTTPException` vs `ValueError`**：產生器內用 `HTTPException(400)` 表達「語意上不合理但型別合法」的錯（ip_type、pool）；pydantic validator 的 `ValueError` 會變 422。兩者都會被測試接到，但語意不同——沿用既有慣例。
- **鏡射 solver**：§5 那張表是「隱形的合約」。solver 一改，這裡靜默失效。加回歸測試自保。
- **ground truth ≠ 保證合法**：debug 時別把 `ground_truth` 當權威，看 `feasibility`/`diagnostics`。
- **pydantic 吃掉未知欄位**：改欄位名時，舊 payload 不會報錯，只會被無視——容易讓人誤以為「有設到」。
- **guard 迴圈**：sizing 的 `guard < 100_000` 是保命上限；若你把 demand/tightness 改成可能永遠 cover 不了，會撞上限並產出不足的機隊（verify 會抓到，但要知道成因）。

---

## 10. 除錯手冊

**情境：verify 回 infeasible，但我覺得應該可解。**

1. 把 `request` 拉出來，手動再跑一次拿完整診斷：
   ```python
   from app.solver import VMPlacementSolver
   res = VMPlacementSolver(resp.request).solve()
   print(res.diagnostics["constraint_check"])   # 哪個約束先爆
   print(res.diagnostics.get("advisories"))
   ```
   `constraint_check` 會告訴你 `anti_affinity` / `capacity` / `failover` / `max_per_bm` 哪個 `failed_at`。
2. **逐一關約束隔離**：複製 payload，分別 `anti_affinity=False`、清掉 `max_per_bm_by_role`、`failover=False`，看哪個一關就 verified——那個就是元凶。（這正是我們定位過「same_room + rack spread」和「AG 分佈不均」的方法。）
3. 常見根因與解法：
   - **AG 分佈不均** → `racks` 設成 `ags` 倍數。
   - **機隊太小**（固定 count 太少）→ 留空走彈性、或調低 tightness。
   - **max_per_bm 太嚴** vs 候選 BM 不夠 → 放寬上限或加 BM。
   - **failover 計數**：同 cluster learner 數需 ≥ master 數。
4. `diag["unplaced_ground_truth"]` 有東西，代表連建構法都塞不下——通常是容量或候選集合的問題。

**情境：加了功能後某測試壞了但看起來無關。**
多半是**決定論被破壞**（引入了非穩定迭代序）或**管線順序**被動到。先確認 `self.rng` 仍是唯一亂數、`_build_baremetals` 仍在 `_assign_candidates` 前。

---

如需理解「solver 端」怎麼把這些規則變成 CP-SAT 約束，請讀 `app/solver.py` 與 `docs/constraints.md`、`docs/objective-function.md`。本產生器的所有「鏡射」最終都指向那裡。
