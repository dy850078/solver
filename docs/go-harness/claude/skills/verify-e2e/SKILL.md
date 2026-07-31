---
name: verify-e2e
description: End-to-end verification against a real solver sidecar — assemble real requests, POST them, and check the responses are actually consumed correctly. Use after any change to contract assembly, candidate derivation, or metric computation; unit tests with hand-written fixtures are not sufficient verification.
---

# 對真的 solver 跑一遍

單元測試用的是我們自己寫的 fixture——它只證明「程式碼符合我們對契約的
理解」,不證明「我們的理解對」。這個 skill 檢查後者。

## Steps

1. **起 solver**:`make solver-up`(或直接在 solver repo 跑 `make run`,
   :50051)。確認 `curl -s :50051/health` 回 `ok`。

2. **用真程式碼組 request**,不要手刻 JSON:
   走 `internal/planning` 的快照組裝路徑產出 `CapacityPlanRequest`,
   走 `internal/scheduler` 的候選推導產出 placement request。
   手刻 JSON 會繞過正要驗證的那段程式碼。

3. **POST 並檢查回應被正確消化**(逐個端點):
   - `/v1/capacity/plan` → `success`、`by_fab_period[]`、`budget_view[]`、
     `demand_coverage[]` 都解得開,欄位沒有掉。
   - `/v1/capacity/reconcile` → 四指標讀得出來,**空分母是 null 不是 0**。
   - `/v1/placement/split-and-solve` → assignments 對得回 VM id。

4. **驗語意,不只驗 `success: true`**——挑至少三項對照本次改動:
   - **pool 隔離**:給一筆 `pool=X` 的需求 + 一台 `pool=""` 的機器,
     確認它**沒有**被當候選;反向也試(共用需求 vs 池機器)。
   - **三態月份**:缺列的月份不出現在報表;全 0 列出現且 `node_adds=0`。
   - **狀態前綴**:故意送壞輸入(空 candidate、不存在的 type_id),確認
     Go 端 branch 到 `INPUT_ERROR` 而不是當成一般失敗。
   - **可落地量**:同樣的名目容量但碎片化(每台留一點點),確認
     `in_stock_slots` 掉下來而 `in_stock_available` 沒變——這證明我們讀的
     是對的欄位。
   - 本次改動觸及的其他語意。

5. **一致性回放**(動到候選推導時必做):同一份快照
   - ① planning profile → `/v1/capacity/plan` → 應可行
   - ② execution profile → `/v1/placement/split-and-solve` → 必須也可行
   - ② 失敗且缺的機器不在明文差集內 → **紅燈,改動未完成**
   判定邏輯可對照 solver repo 的 `tests/test_consistency_replay.py::replay`。

6. **報告**:表格列出 端點 → 狀態 → 驗到的語意,加上任何落差。
   有落差就是這次改動**還沒完成**——修掉,或帶著失敗案例回報使用者。

## Rules

- solver 起不來就停下來說,不要退回 mock 然後宣稱驗過了。
- 契約有疑問時去讀 solver repo 的 `app/models.py` 或 `GET /openapi.json`,
  不要猜。
- 這個 skill 的產出是**證據**,不是「我認為應該沒問題」。
