# Rollout 模擬說明文件:循序建置的預演與死局偵測

> **作者**: Claude(與 dy850078 討論定案)
> **日期**: 2026-08-25
> **狀態**: Implemented
> **相關 ADR**: [ADR-013](decisions/ADR-013-rollout-simulation.md)(模擬)、
> [ADR-014](decisions/ADR-014-rollout-sizing.md)(sizing)
> **姊妹篇**: [Add-Node / Pinned VM 說明](add-node-guide.md)、
> [整體 Enhancement Proposal](rollout-simulation-and-sizing.md)

---

## TL;DR

規劃時所有 cluster 一起看(聯合求解),實際建置卻是**一次一個 cluster、
順序由使用者指定**。`POST /v1/placement/rollout` 把這個建置順序**逐步重放**:
每一步跑真實的 solve,結果折疊成下一步的既成事實 — 在規劃期就找出「聯合
規劃可行、循序建置卻走進死局」的順序。模擬是**無狀態**的:需求飄移了就用
當下現實重打一次,成本只是一次 API 呼叫。

## 問題:聯合解證明的事,循序建置不保證

聯合求解回答的是「這些 VM **同時**放得下嗎」;循序建置每步只提交**當步**
的最佳解,然後釘死。前面的 cluster 把容量與 bucket 吃成碎片,後面的
cluster 可能無處可放:

```
 規劃期(聯合)                建置期(循序)
┌──────────────────┐    ┌────────┐ ┌────────┐ ┌────────┐
│ A+B+C 一起解      │    │ 建 A   │→│ 建 B   │→│ 建 C ✗ │ ← 死局在這裡才爆
│ → 可行 ✓         │    └────────┘ └────────┘ └────────┘
└──────────────────┘         ↑ 每步各自最佳,彼此看不見
```

過去只有兩個都不對的選項:UI 上把多個 cluster 合成一次聯合呼叫(不是真實
的建置方式),或手動一步一步打、**自己**把上一步的用量加進下一步的 used
(易錯,而且 C3–C6 依然失憶 — 原因見姊妹篇 add-node 文件)。

## 機制:每一步都是一次「如假包換」的生產調度

模擬迴圈(`app/rollout.py`)對 steps 依序執行:

```
step k 求解成功
  └─ 對每筆 new_assignment 折疊(fold-forward):
       vm.pinned_to = 宿主        ← 身分留下:C3–C6 的全域視野
       vm.candidate_baremetals = [宿主]
       宿主.used_capacity += vm.demand   ← 維持「used = 庫存真相(含 pinned)」不變量
  └─ step k+1 在「前面所有 pins + 累計 used」之上求解
```

關鍵性質:

- **淨額歸零的帳本**:折疊加進 used 的量,solver 端正規化會扣回(pinned
  再經強制 assign 記回)。整數運算精確,跨步驟零漂移。效果是**每一步的
  請求與真實 scheduler 從 DB 組出來的請求形狀完全相同** — 模擬結果就是
  實際建置會得到的結果,沒有「只在模擬裡成立」的特殊語意。
- **規則聯集**:step k 在 steps 1..k 的規則聯集下求解。step 1 放好的
  exclusive appliance(如 F5),在 step 2、3… 依然受 C6 保護,不會被後面
  的 cluster 入住。
- **首敗即止**:第一個失敗的 step 之後不再求解,回報
  `BLOCKED: not simulated — step '<failed>' failed` 的 stub — 死局的成因
  在失敗步,繼續模擬只會產生誤導性的結果。
- **只有合成 VM 改名**(`{step}/{synthetic_id}`):使用者自己的 VM id 原樣
  保留 — 全面改名會讓 vm_ids 型規則(如 C6)無聲失效。

## 與偏移共處:模擬不是預言,是隨時可重打的預演

這是設計上最重要的立場。兩種偏移分開講:

### 1. 需求飄移(規劃 vs 建置當下)

規劃時模擬過的順序,到實際建置時需求可能已經變了 — 數量調整、spec 換代、
甚至**當初不知道的 cluster 插了隊**。我們**不試圖預測或校正飄移**,而是把
重新模擬做到夠便宜:

- **模擬完全無狀態**:不動任何真實庫存,重打一次就是一次全新預演。
- **建置前夕的標準流程**:從 DB 撈當下現實 → 已建好的 VM 放進
  `existing_vms`(brownfield 起始態,必帶 `pinned_to`、demand 已含在起始
  used)→ 剩下要建的 cluster 照當下已知的需求列成 steps → 重打
  `/v1/placement/rollout`。**過去用 pins 表達、未來用 steps 表達**,同一個
  端點同時吃兩者。
- **未知的未來 cluster** 無法被模擬(這是誠實的限制,不是缺陷)。對策是
  間接的:objective 的 headroom / slot-score 項讓每一步的解保留彈性,
  `target_spread` advisory 提醒分散度 — 然後在每個新 cluster 確定時重新
  預演一次剩餘的順序。

### 2. 規則與歷史偏移(今日的規則 vs 既成的佈局)

重新模擬時,`existing_vms` 帶進來的歷史佈局可能不符合**今天**的規則
(規則後來才加、或改嚴了)。處理方式與 add-node 完全一致 — 因為 brownfield
起始態走的就是同一個 pinned 原語:

- **C3 / C4 / C5:既往不咎、不再惡化**。每個 bucket 的上限取
  `max(規則 cap, 該 bucket 現有 pinned 數)` — 歷史超標凍結在現值(一台都
  不多)、其他 bucket 照常執法、絕不因歷史而 INFEASIBLE。
- **C6:不寬貸,INPUT_ERROR**。獨占被違反沒有「凍結」的中間態,這是庫存
  與規則的矛盾,需要人工裁決(搬遷或修規則),solver 拒絕替你選。
- 完整推導與範例見[姊妹篇](add-node-guide.md#規則與歷史偏移既往不咎不再惡化)。

**Rollout 內部還有一個規則時序的細節**:規則聯集是「從首次宣告的 step 起
生效」。把 exclusive 規則宣告在群組**首次出現**的那一步(UI 自動如此),
它就從一開始保護到底;若拖到後面的 step 才宣告,而前面步驟的 placement
已經踩線,那一步會以 INPUT_ERROR 失敗 — 這是正確行為(規則與已成事實
矛盾),但通常代表規則放錯了位置。C3/C4/C5 晚宣告則只是對既成事實凍結,
不回溯重排。

## 延伸:建廠時連機隊都還沒有(sizing)

模擬要求先給機隊;建廠情境問題是反的 —「照這個順序建,**最少要買幾台**?」
`POST /v1/placement/rollout/size` 用拓撲模板(散到 K 個 AG)描述機隊形狀,
以「解析下界 → 逐台上行掃描」回答**可證明的精確最小值**,每次探測跑的就是
上面的完整模擬。三個「要幾台」的答案天生不同:

**capacity planner(採購近似)≤ 聯合求解(mockgen elastic)≤ rollout
sizing(循序)** — 循序建置會碎片化,拿聯合解的台數去建廠可能建到一半
不夠。細節(下界拆解、探測足跡、預算語意、六類 pre-flight INPUT_ERROR)見
[EP §情境 3](rollout-simulation-and-sizing.md#情境-3建廠估算rollout-sizing)
與 ADR-014。

## API 使用

**端點**:`POST /v1/placement/rollout`。

```jsonc
// 骨架 — 完整可跑範例見 examples/rollout/multi_cluster_mixed_specs.json
{
  "baremetals": [ /* 共用庫存;used 必含 existing_vms 的消耗 */ ],
  "steps": [
    { "name": "cluster-a", "vms": [ /* spec×數量 */ ],
      "exclusive_bm_rules": [ /* 規則宣告在群組首次出現的 step */ ] },
    { "name": "cluster-b", "vms": [ /* ... */ ] }
  ],
  "existing_vms": [ /* 選填 brownfield:必帶 pinned_to */ ],
  "config": { "auto_generate_anti_affinity": true, "target_spread": { "ag": 3 } }
}
```

**讀回應**:

| 欄位 | 意義 |
|---|---|
| `success` | 每一步都成功才 true |
| `failed_step` | 第一個失敗的 step;其後的 report 是 `BLOCKED:` stub |
| `reports[k].new_assignments` | **只含該步新增**的 placement(pins 不重複列) |
| `reports[k].unplaced_vms` | 失敗步只列**該步自己**的 VM |
| `final_baremetals` | 全部成功折疊後的庫存快照 —「下一次真實調度會看到的 used」,可直接當 what-if 起點 |
| 頂層 `solver_status` | 平常為空;合約違反時 `INPUT_ERROR: ...` 短路整包(清單在 `diagnostics["input_errors"]`) |

**模擬失敗(死局)後的選項**:調整順序(碎片化嚴重的大 cluster 提前)、
放寬規則、加機器(要加幾台 → sizing 端點)。改完重打,無副作用。

**UI**(`/ui/rollout.html`,`ENABLE_UI=enable`):spec 目錄 + 逐 cluster
step 卡(↑/↓ 排序)+ 多機型 BM pool;`Sequential` 跑本文的循序模擬、
`All at once` 跑聯合對照 — 兩者對比就是「循序的代價」的可視化。表單蓋
不住的(brownfield、粗粒度 requirements、vm_ids 規則)走 Advanced JSON。

## 參考

- `app/rollout.py` 模組 docstring(fold-forward 合約的權威版本)
- `tests/test_rollout.py`(折疊帳本、規則聯集、BLOCKED、brownfield)
- [ADR-013](decisions/ADR-013-rollout-simulation.md)(完整決策推導)
- [EP:API 使用指南](rollout-simulation-and-sizing.md#api-使用指南)(全端點對照)
