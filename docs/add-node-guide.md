# Add-Node 視野擴張:Pinned VM 說明文件

> **作者**: Claude(與 dy850078 討論定案)
> **日期**: 2026-08-25
> **狀態**: Implemented
> **相關 ADR**: [ADR-012](decisions/ADR-012-pinned-vms-grandfathered-caps.md)
> **姊妹篇**: [Rollout 模擬說明](rollout-simulation-guide.md)、
> [整體 Enhancement Proposal](rollout-simulation-and-sizing.md)

---

## TL;DR

對既有 cluster 加節點時,把**舊 VM 以「事實」的身分**(`pinned_to`)跟新 VM
一起送進 `POST /v1/placement/solve`。solver 因此對 C2–C6 取得**全域視野**:
新節點會避開舊節點已佔的 AG、已用的 failover 名額、已獨占的機器。當今日的
規則與歷史佈局不合時,政策是**「既往不咎、不再惡化」**— 歷史違規凍結、
不會讓請求變 INFEASIBLE,但新 VM 不能把任何地方弄得更糟(C6 例外,見下)。

## 問題:第二次調度是失憶的

第一次調度放好 cluster-a 的 3 台 master(good:三個 AG 各一台)。三個月後
要加第 4 台 master — 舊流程的請求裡**只有新 VM**,舊 VM 只剩宿主
`used_capacity` 裡的一坨數字:

```
      第一次調度後的現實                第二次調度 solver 看到的
  ag-1      ag-2      ag-3           ag-1      ag-2      ag-3
┌───────┐┌───────┐┌───────┐       ┌───────┐┌───────┐┌───────┐
│ bm-1  ││ bm-2  ││ bm-3  │       │ bm-1  ││ bm-2  ││ bm-3  │
│ m-1 ✦ ││ m-2 ✦ ││ m-3 ✦ │       │ used↑ ││ used↑ ││ used↑ │  ← 只有數字,
└───────┘└───────┘└───────┘       └───────┘└───────┘└───────┘    不知道是誰
```

`used_capacity` 是**純量帳本** — 只記「多少」,不記「是誰、哪個群、什麼角色」。
但 C3(anti-affinity)、C4(max-per-BM)、C5(failover N-1)、C6(exclusive)
全是**身分敏感**的約束:它們數的是「這個 bucket 裡有幾個**同群成員**」。
失憶的 solver 會把 m-4 疊回 ag-1(那裡容量最鬆),分散度默默劣化;failover
名額被重複發放;exclusive 的機器被外人入住 — 而且每一項都「看起來成功」。

## 機制:pinned = 對過去的陳述

`VM.pinned_to = "<bm-id>"` 宣告「這台 VM **已經**住在那裡」。它是事實標註,
不是擺放請求 — 這個區分決定了你該用哪個欄位:

| 你想表達的 | 用什麼 | 效果 |
|---|---|---|
| 這台 VM 已經在 bm-7 上(過去) | `pinned_to: "bm-7"` | 強制 assign、參與所有約束計數、caps 既往不咎、結果回顯 `pinned: true` |
| 這台**新** VM 我要它去 bm-7(未來) | `candidate_baremetals: ["bm-7"]` | 正常求解,只是候選只有一台;放不下就是誠實的 INFEASIBLE,**不會**被寬貸 |

solver 內部(`app/solver.py`)做三件事:

1. **C1 強制指派**:`assign[vm, pinned_to] == 1`,且該 VM 的 eligibility
   只剩宿主一台(容量檢查跳過 — 它已經在那裡了,這是事實不是選擇)。
2. **容量正規化**:`used_capacity` 維持**庫存真相**(包含 pinned 消耗,DB
   撈出來什麼樣就送什麼樣)。solver 先把 pinned demand 從 used 扣除,再讓
   強制 assign 經 C2 記回 — 淨額歸零,scheduler 完全不需要做 used 的加減。
3. **約束計數**:pinned VM 被 C3/C4/C5/C6 當正常成員計數 — 這就是「視野」。

**為什麼要「先扣再記回」而不是二選一?** 兩個方向的失效都真實存在:

| 情境 | 後果 |
|---|---|
| used 含 pinned、solver 不扣 | pinned demand 記兩次(used 一次 + assign 一次)→ 容量被吃兩份 → **假 INFEASIBLE** |
| used 不含 pinned、solver 照扣 | 扣到負值 → INPUT_ERROR 防呆攔下;若機器原本 used 夠大則**無聲鑄造幽靈容量**(超賣) |

所以合約只有一種正確組合:**used 永遠含 pinned 消耗 + solver 永遠正規化**。
違反時 solver 給的是明確的 INPUT_ERROR,不是無聲修正。

## 規則與歷史偏移:既往不咎、不再惡化

這是本功能最重要的政策決定。規則會演進(後來才加的 anti-affinity、改嚴的
max_per_bm),歷史佈局不會跟著搬家 — 兩者必然偏移。三種處理方式擺在一起看:

| 方案 | 後果 |
|---|---|
| 歷史違規 → INFEASIBLE | add-node **永遠無解**,直到有人手動搬遷 — 現實庫存幾乎總帶著歷史違規,功能形同不可用 |
| 歷史違規 → 忽略(不計 pinned) | 回到失憶狀態,新 VM 繼續往違規的 bucket 疊 — 越來越糟 |
| **歷史違規 → 凍結(採用)** | 承認現實、擋住惡化:新 VM 進不了已超標的 bucket,其他 bucket 照常執法 |

具體語意(grandfathering,`app/solver.py` C3/C4/C5 builder):每個 bucket 的
上限取 **`max(規則要求的 cap, 該 bucket 現有的 pinned 數)`**。

**例**:規則 cap = 每 AG 最多 2 台,歷史上 ag-1 已有 3 台 pinned:

```
        ag-1(歷史超標)   ag-2        ag-3
cap:    max(2, 3) = 3      max(2,0)=2  max(2,0)=2
效果:   3 台 pinned 合法    新 VM 可進  新 VM 可進
        但新 VM 進不來      (≤2)       (≤2)
```

- 凍結是 **per-bucket** 的:ag-1 被寬貸,不影響 ag-2/ag-3 照規則執法。
- 寬貸的量 = 現況,**一台都不多**:cap 抬到 3 不是 4 — 新 VM 不能「趁著
  已經違規再多塞一台」。
- C5 同理:pinned 的 primary 與 backup 都計入名額,已超標的 fault-domain
  bucket 凍結在現值。

**C6 是唯一的例外:不寬貸,直接 INPUT_ERROR。** exclusive 是佔用語意 —
機器要嘛被獨占、要嘛沒有,不存在「超標一點點」可凍結的中間態。pinned 佈局
違反 exclusive 規則(獨占機上住著外人、或兩個獨占成員同機)表示**庫存資料
與規則之間有必須人工裁決的矛盾**:要嘛搬遷 VM、要嘛修規則。solver 拒絕替
你選(`_validate_pinned_exclusive`)。

**分散度的提示不擋路**:pinned 分佈低於 `config.target_spread`(例如三台
master 全擠在 2 個 AG)只產生 `spread_below_target` advisory(diagnostics),
不影響求解 — 歷史的分散不足是事實,擋新請求無濟於事,但你會被告知。

## API 使用

**端點**:`POST /v1/placement/solve`(既有端點;`pinned_to` 選填,不帶 =
舊行為,零 breaking change)。

**Scheduler 端的四條責任**(口訣:標註在輸入、過濾在輸出):

1. `baremetals` = **可調度的 BM 群 ∪ 所有 pinned VM 的宿主**。宿主不在
   請求裡 → INPUT_ERROR;宿主在請求裡**不代表**它可調度 — 新 VM 能去哪由
   自己的 `candidate_baremetals` 決定(所以兩次調度間 BM 群變了也沒關係,
   只要記得把宿主聯集進來)。
2. `used_capacity` 直接用 DB 庫存真相(含 pinned 消耗),不要自行加減。
3. 每台 pinned VM:`pinned_to` = 宿主 id、`candidate_baremetals` =
   `["<宿主 id>"]`(空列表是合約違反)、`demand` = **庫存記錄值**(不是
   使用者重新輸入的值 — 正規化要扣的就是這個數)。
4. 回應中**只執行 `pinned: false`** 的 assignment;`pinned: true` 是回顯的
   完整終態(給驗證與 UI 用)。

範例請求/回應與逐欄說明見
[Enhancement Proposal §情境 1](rollout-simulation-and-sizing.md#情境-1add-node加節點)。

**常見 INPUT_ERROR**:

| 訊息片段 | 成因 | 修法 |
|---|---|---|
| `pinned host '...' not present` | 宿主 BM 沒送進 baremetals | 送「可調度群 ∪ pinned 宿主」 |
| `pinned demand ... exceeds its used_capacity` | used 沒含 pinned 消耗(扣到負) | used 用庫存真相,勿預扣 |
| `over-committed (used > total)` | 庫存本身超賣 | 先修庫存資料 |
| `candidate list ... does not contain pinned_to` | 同一台 VM 兩個欄位矛盾 | pinned 的 candidates 給 `[宿主]` |
| exclusive 相關 | 歷史佈局違反 C6 | 搬遷或修規則 — C6 不寬貸 |

## FAQ

- **使用者想手動指定某台新 VM 的落點?** 用單元素 `candidate_baremetals`,
  **不要**用 `pinned_to`。pinned 會繞過容量檢查與規則(它是事實),使用者
  的願望應該被正常執法 — 放不下就該誠實地 INFEASIBLE。
- **capacity planner 可以吃 pinned 嗎?** 不行,明確拒收(INPUT_ERROR)。
  採購尺度的近似模型與逐台事實不相容。
- **pinned 會出現在 unplaced_vms 嗎?** 不會 — 它的指派是強制的;若強制
  指派撞上硬性矛盾,錯誤在更早的 INPUT_ERROR / INFEASIBLE 就爆出來了。

## 參考

- `app/models.py` `VM.pinned_to` docstring(合約的權威版本)
- `app/solver.py` `_normalize_pinned_capacity` / `_validate_pinned_exclusive` /
  C3–C5 builder 內的 grandfather 註解
- `tests/test_solver.py` pinned 測試群(含兩個失效方向的紅線測試)
- [ADR-012](decisions/ADR-012-pinned-vms-grandfathered-caps.md)(完整決策推導)
