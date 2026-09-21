# Scheduler 端需求：control-plane / learner 共站政策

- **作者**: Claude (Fable)
- **日期**: 2026-09-21
- **狀態**: Draft — 待 Go scheduler 團隊 review
- **相關**: ADR-016、PR #36、`docs/go-scheduler-guide.md` §2、
  `examples/control_plane_learner_separate.json`
- **讀者**: 實作 Go scheduler 的工程師。solver 端已完成；本文只列 scheduler 要做的事。

---

## 1. 背景與目標

上游 DB 將把 master 與 learner 都標成 `role="control-plane"`，只靠
`is_learner` 欄位區分。每個 cluster 需要二選一的放置政策：

| 情境 | 政策 | 每種 role 各自 |
|---|---|---|
| 1 | learner **不可**與 master 同一台 BM | 1 台 / BM |
| 2 | learner **可以**與 master 同一台 BM | 1 台 / BM |

solver 端（ADR-016）已支援 `GroupSelector.node_role` 接受字串清單，
一條 C4 rule 就能表達情境 1。scheduler 端要做的事歸結為一句話：

> **身分（identity）由 VM 欄位表達，政策（policy）由 rule 表達。role 字串永遠不隨政策改變。**

### 為什麼不把政策塞進 role 名稱

曾考慮「情境 1 兩者都送 `control-plane`、情境 2 才分開」。被排除的理由（細節見 ADR-016 §1）：

- **C3 分散被稀釋**：auto anti-affinity 與 auto max-per-BM 共用分組 key
  `(cluster_id, ip_type, node_role)`。合併 role 後 3 master + 3 learner 分到 3 個 AG，
  每 AG cap 從 ⌈3/3⌉=1 變 ⌈6/3⌉=2，兩個 master 可以合法落在同一 AG。
- **C5 failover 寫不出來**：primary / backup 用 role selector 選人，role 同名時兩個集合相同。

---

## 2. 需求清單

每條附驗收條件。R1–R3 是資料正確性、R4–R7 是規則產生、R8–R10 是生命週期與整合。

### R1 身分映射（identity）

DB 記錄 → 送給 solver 的 `node_role`，固定對應、與政策無關：

| DB `role` | DB `is_learner` | `node_role` |
|---|---|---|
| control-plane | false | `control-plane` |
| control-plane | true | `control-plane-learner` |

適用**所有**送 solver 的路徑：

| 路徑 | 欄位 |
|---|---|
| `POST /v1/placement/solve` | `vms[].node_role` |
| `POST /v1/placement/split-and-solve` | `requirements[].node_role` |
| `POST /v1/placement/rollout` | `existing_vms[].node_role`、每個 step 的 VM |
| add-node 帶入的 pinned VM（`VM.pinned_to`） | `node_role` |
| `POST /v1/capacity/plan` | `demand_book[].node_role` |

映射函式應只有一個出口，不得在各路徑各寫一份。

**驗收**：同一筆 DB VM 在任何 request、任何政策下 `node_role` 相同；
contract test 覆蓋上表五條路徑。

### R2 auto-gen 必填欄位（實跑確認的陷阱）

auto C3 / auto C4 的分組 key 是 `(cluster_id, ip_type, node_role)`，
**`cluster_id` 或 `ip_type` 為空字串的 VM 會被 auto-gen 直接跳過**，不報錯
（`app/solver.py::_resolve_anti_affinity_rules` / `_resolve_max_per_bm_rules`）。

實跑結果（情境 2、`ip_type=""`）：6 台控制平面 VM 被放到 **2 台 BM**，狀態 OPTIMAL，
無任何 advisory。這是靜默失效，不是錯誤。

**驗收**：scheduler 對控制平面 VM 一律填非空 `cluster_id` 與 `ip_type`；
contract test 斷言兩者非空。

### R3 candidate 過濾

兩種 role 的 `candidate_baremetals` 都只含 control-plane BM，沿用
`docs/go-scheduler-guide.md` 情境 C 的 role-based filtering，把 learner 納入同一組。
空清單在 `/solve` 是 INPUT_ERROR；在 `/split-and-solve` 該 requirement 會被 drop 導致 INFEASIBLE。

**驗收**：learner 的 candidate 清單與 master 相同（同 cluster）。

### R4 政策輸入來源

政策來自 request 的 `schedule strategy` → DB `intra_cluster_policy`
（cluster 內調度規則，含 per-BM limit by role）。**不放** `inter_fz_policy`，那是 cluster 間的語意。
DB 表示法見 §3，由 scheduler 團隊決定。

### R5 C4 rule 產生

從 per-BM limit 產生 `max_per_bm_rules`，翻譯規則與 DB 表示法無關：

| DB 條目 | 產生的 selector | `max_per_bm` |
|---|---|---|
| 單一 role `r` → n | `{"cluster_id": X, "node_role": "r"}` | n |
| role 集合 `{r1, r2}` → n | `{"cluster_id": X, "node_role": ["r1", "r2"]}` | n |

情境 1 就是多送一條：

```json
{
  "group_id": "<cluster>-control-plane-no-colocate",
  "selector": {"cluster_id": "<cluster>", "node_role": ["control-plane", "control-plane-learner"]},
  "max_per_bm": 1
}
```

情境 2 = 沒有這條。

兩個實作要點：

- **一律送 explicit rule，不依賴 `default_max_per_bm`**。那是全域單一數字，
  無法表達 master 1 / worker 3 這種 per-role 值。
- **per-role rule 與聯集 rule 同時存在是正確的**。solver 的 auto C4 會跳過已被
  explicit rule 覆蓋的 VM，但 explicit rule 之間不互斥，兩條都成立；
  聯集 cap 1 本來就蘊含 per-role cap 1。`group_id` 請給有意義的名字，
  INFEASIBLE 時 diagnostics 用它回報（R9）。

**驗收**：情境 1 的 request 含聯集 rule；情境 2 不含；selector 的 `cluster_id` 有填。

### R6 C3 anti-affinity

維持 `config.auto_generate_anti_affinity=true`，`target_spread` 至少含 `"ag"`，
BM 拓樸有兩個以上 room 時加 `"room"`。role 分開送即保證 master 與 learner
各自跨 AG，**不需要**新的 anti-affinity rule。

**驗收**：情境 1、2 的結果中 master 跨 3 AG、learner 跨 3 AG（拓樸允許時）。

### R7 C5 failover — 兩種情境都送，但有前置條件

```json
{
  "rule_id": "<cluster>-cp-learners-by-room",
  "primary": {"cluster_id": "<cluster>", "node_role": "control-plane"},
  "backup":  {"cluster_id": "<cluster>", "node_role": "control-plane-learner"},
  "fault_domain": "room",
  "policy": "n_minus_1"
}
```

solver 對 C5 有 pre-flight 與可行性檢查，以下三種情況**scheduler 必須先判斷、不送 C5、記 warning**
（實跑結果）：

| 情況 | solver 回應 |
|---|---|
| learner 數 = 0 | `INPUT_ERROR: failover rule '...': backup selector matches no VMs` |
| learner 數 < master 數 | `INPUT_ERROR: failover rule '...': \|primary\|=3 > \|backup\|=1; n_minus_1 redundancy is infeasible by counting` |
| candidate BM 全在同一個 room | `INFEASIBLE`，`diagnostics.constraint_check.failed_at = "failover"`（單 room 時 Σ primary + Σ backup ≤ \|backup\| 必然不成立） |

前兩種是 INPUT_ERROR，整個 request 失敗，不是只跳過 C5。第三種要在送出前檢查
candidate BM 的 room 數 ≥ 2。

**驗收**：上述三種情況 scheduler 不送 C5 且有 warning log；其餘情況 request 含 C5。

### R8 add-node / rollout 與歷史狀態

- pinned VM 同樣走 R1 映射；`used_capacity` 維持 inventory 真值（含 pinned 消耗），
  solver 內部會正規化（ADR-012）。
- 既有 cluster 原本可共住、之後切成不共住：solver 對 C3/C4/C5 的 cap 採
  **grandfather**（`max(cap, 該桶已 pinned 的數量)`）。已共站的 master + learner
  不會導致 INFEASIBLE，只是新 VM 不能再讓情況更糟。scheduler **不需要**搬遷既有 VM，
  但要知道結果可能保留歷史違規。
- 若要主動修正歷史共站，是另一個功能（migration），不在本需求範圍。

**驗收**：add-node 情境的 contract test：既有 pinned master+learner 同 BM、
政策為不共住、新增一台 learner → 結果 success，新 learner 不與任何 master 同 BM。

### R9 回應處理

| solver 狀態 | scheduler 行為 |
|---|---|
| OPTIMAL / FEASIBLE | 直接用 `assignments`；`pinned=true` 的項目過濾掉不重複建立 |
| INPUT_ERROR | 讀 `diagnostics.input_errors[]`，多半是 R2 / R7 前置條件沒守住，屬 scheduler bug |
| INFEASIBLE | 讀 `diagnostics.infeasible_max_per_bm_rules[]`，欄位 `group_id / vm_count / max_per_bm / reachable_bms / slots_available`；`group_id` 命中 `*-no-colocate` 時回報「不共住政策下 control-plane BM 不足：需要 N 台、可用 M 台」 |
| 任何狀態 | `diagnostics.advisories[]` 中 `type="spread_below_target"` 記 log，不阻擋 |

### R10 版本相依與部署順序

`node_role` 清單形式在舊版 solver 會回 **HTTP 422**（Pydantic 驗證失敗）。
清單形式是嚴格超集，舊 payload 在新版 solver 行為不變。因此：

1. 先部署 solver（PR #36）。
2. scheduler 以 feature flag 控制是否送清單形式；flag 關閉時退回情境 2 行為。
3. scheduler 收到 422 時要有明確錯誤，不得 panic。

---

## 3. DB 表示法：per-BM limit 如何表達 role 集合

現況：`intra_cluster_policy.per_bm_limit` 是 JSON，例如 `{"master": 1, "worker": 3}`，
語意是「同 cluster 的 master 每台 BM 最多 1 台」。

### 3.1 先講清楚語意

`"master": 1` 的數學是：對每台 BM，`Σ assign[vm, bm] over {vm : role ∈ {master}} ≤ 1`。
聯集 rule 只是集合多一個元素：`Σ over {vm : role ∈ {control-plane, control-plane-learner}} ≤ 1`。
**算的是集合內 VM 的台數，不是「組合」的個數**：

| BM 上的內容 | 集合內 VM 數 | 違反 ≤ 1？ |
|---|---|---|
| 1 master | 1 | 否 |
| 1 learner | 1 | 否 |
| 1 master + 1 learner | 2 | **是** |
| 2 master | 2 | 是 |

第三列就是「不可共住」。與現有欄位是同一個運算子。

### 3.2 三種表示法（不下結論，由 scheduler 團隊決定）

| 方案 | 形式 | 優點 | 缺點 |
|---|---|---|---|
| **1** `\|` 分隔的 role 集合 key | `{"control-plane": 1, "control-plane-learner": 1, "control-plane\|control-plane-learner": 1}` | 零新欄位；既有資料 100% 相容；`\|` 讀作「或」，與 solver `role ∈ set` 對齊；`\|` 不在 role 字元集 `^[\w.-]+$` 內，不會撞名 | key 承載結構，需要文件說明讀法 |
| **2** rule 陣列 JSON | `{"rules": [{"roles": ["control-plane", "control-plane-learner"], "max_per_bm": 1}]}` | 與 solver `MaxPerBaremetalRule` 一比一；可帶 `group_id`；未來可延伸到 anti-affinity / exclusive 等其他 rule 類型 | 既有 `{"master": 1}` 資料要遷移，或雙讀一段時間 |
| **3** 另加 `co_location_deny` 欄位 | `[["control-plane", "control-plane-learner"]]` | 可讀性最好，意圖直白 | 又一個新欄位；它只是聯集 rule 的特例，未來加其他規則時仍會回到方案 2 |

明確排除的兩個方向：

- **boolean 欄位 `learner_colocate`**：每個新政策一個欄位，正是目前被 challenge 的型態；
  且只能表達這一對 role。
- **放 `inter_fz_policy`**：語意是 cluster 之間，不對。

長期方向一句話：獨立的 `placement_rules` 表（`rule_type` + selector JSON + params），
一比一對應 solver 的四種 rule（anti-affinity / max-per-BM / exclusive / failover），
可 FK、可審計。本次不做。

不論選哪種，R5 的翻譯規則不變：單一 role → 字串 selector，role 集合 → 清單 selector，值 → `max_per_bm`。

---

## 4. 完整 request 範例

### 情境 1（不共住）

完整可跑檔案：`examples/control_plane_learner_separate.json`。關鍵段落：

```json
{
  "vms": [
    {"id": "m-1", "node_role": "control-plane",         "ip_type": "routable", "cluster_id": "A", "candidate_baremetals": ["..."], "demand": {"...": 0}},
    {"id": "l-1", "node_role": "control-plane-learner", "ip_type": "routable", "cluster_id": "A", "candidate_baremetals": ["..."], "demand": {"...": 0}}
  ],
  "max_per_bm_rules": [
    {"group_id": "A-control-plane-no-colocate",
     "selector": {"cluster_id": "A", "node_role": ["control-plane", "control-plane-learner"]},
     "max_per_bm": 1}
  ],
  "failover_rules": [
    {"rule_id": "A-cp-learners-by-room",
     "primary": {"cluster_id": "A", "node_role": "control-plane"},
     "backup":  {"cluster_id": "A", "node_role": "control-plane-learner"},
     "fault_domain": "room", "policy": "n_minus_1"}
  ],
  "config": {"auto_generate_anti_affinity": true, "target_spread": {"ag": 3, "room": 2}}
}
```

實跑結果：OPTIMAL，6 台 VM 各佔一台 BM，master 跨 3 AG、learner 跨 3 AG。

### 情境 2（可共住）

與情境 1 唯一差異：**拿掉 `max_per_bm_rules` 那條聯集 rule**，其餘（含 C5）不變。
若 DB 有 per-role 的 `{"control-plane": 1, "control-plane-learner": 1}`，就送這兩條 per-role explicit rule。

實跑結果：OPTIMAL，4 台 BM，兩台上 master 與 learner 共站，每種 role 仍各自跨 3 AG。

> 注意 objective 的 `w_consolidation` 會**主動**把 learner 疊到 master 的 BM 上，
> 因為用到的 BM 越少越好。這不是 master / learner 之間有親和性，任何兩台可合法同住的 VM
> 都會被疊。「允許但不偏好」需要新的 soft term，不在本需求範圍。

---

## 5. 邊界情境

| 情境 | 行為 | scheduler 要做的事 |
|---|---|---|
| learner 數 = 0 或 < master 數 | 送 C5 → INPUT_ERROR | R7：不送 C5 |
| candidate BM 只有一個 room | 送 room-C5 → INFEASIBLE | R7：room 數 < 2 不送 C5 |
| `cluster_id` 或 `ip_type` 為空 | auto-gen 靜默跳過，VM 被堆疊 | R2：必填 |
| cluster 只有 master、無 learner | 情境 1/2 無差別；聯集 rule 退化為 per-role rule | 正常送，C5 不送 |
| 既有 cluster 從可共住切到不共住 | grandfather，不 INFEASIBLE | R8：不需搬遷 |
| 同 cluster 兩種 `ip_type` | auto C3/C4 各自一群；聯集 rule 的 selector 不含 `ip_type`，跨 ip_type 仍不共住 | 預期行為，無需處理 |
| solver 舊版 | 清單形式 → 422 | R10：feature flag + 明確錯誤 |

---

## 6. 驗收 / contract test 清單

- [ ] 情境 1：6 台 VM 6 台不同 BM；master 跨 3 AG；learner 跨 3 AG。
- [ ] 情境 2：同 role 不同 BM；允許 master + learner 共站；**不斷言**一定共站（由 objective 決定）。
- [ ] R1：solve / split-and-solve / rollout 三條路徑對同一筆 DB VM 產生相同 `node_role`。
- [ ] R2：控制平面 VM 的 `cluster_id`、`ip_type` 非空。
- [ ] R7：learner < master、learner = 0、單 room 三種情況不送 C5 且有 warning log。
- [ ] R8：pinned 共站 + 不共住政策 + 新增 learner → success 且新 learner 不與 master 同 BM。
- [ ] R9：INFEASIBLE 時人類可讀訊息含 `group_id` 與 `reachable_bms / slots_available`。
- [ ] R10：對舊版 solver 送清單形式 → 422 → scheduler 明確錯誤、不 panic。

---

## 7. Open questions 給 scheduler 團隊

1. `schedule strategy` → `intra_cluster_policy` 的查詢，是否已發生在 rule 組裝之前？
2. §3.2 三種表示法選哪一種？若選方案 1，分隔符 `|` 是否與現有任何 role 命名衝突？
3. 這個政策要不要在 UI / API 暴露，還是只由營運改 DB？
4. R7 的 room 數檢查要用 candidate BM 的 room 還是整個 fab 的 room？

---

## 8. 參考

- `docs/decisions/ADR-016-multi-role-group-selector.md` — 設計理由與替代方案
- `docs/go-scheduler-guide.md` §2「`GroupSelector.node_role` 的清單形式」、情境 C
- `docs/constraints.md` C3 / C4 / C5、Pinned VMs
- `docs/add-node-guide.md` — pinned 與 grandfather 機制
- `examples/control_plane_learner_separate.json`
