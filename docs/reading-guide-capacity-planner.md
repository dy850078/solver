# 閱讀指南：Capacity Planner — 從請求到報表的完整走讀

> **讀者定位**：已讀過 `docs/reading-guide-splitter.md`、能看懂 splitter 與 solver
> 共用 CpModel 的 joint solve。本文目標是把你教到**能獨立維護、獨立擴充**
> `app/capacity_planner.py` 的程度。
>
> **與其他文件的分工**：`docs/capacity-planning.md` 記錄「為什麼這樣設計」
> （決議與取捨）；本文講「程式碼實際怎麼運作」。行號以 2026-07 的程式碼為準，
> 對不上時以 docstring 與函式名為錨點。
>
> **建議讀法**：開兩個視窗，一邊讀本文一邊對照 `app/capacity_planner.py`。
> 每一層結尾的「檢查點」是自測問題——答不出來就回頭重讀，不要往下走。

---

## 第 0 層：一句話心智模型

> **採買 = 一群「可選啟用、啟用要付費」的虛擬機器。**

整個 capacity planner 沒有為「買機器」發明任何新的變數種類或約束種類。它做的事
是**翻譯**：把「該買幾台、什麼型、放哪個桶」翻譯成既有 placement solver 早就會解
的問題——「這裡有一堆候選 BM（有些是真的、有些是虛擬的），把 VM 放進去，啟用
虛擬 BM 要付目標函數懲罰」。解出來哪些虛擬 BM 被用到，**那就是採買清單**。

為什麼用翻譯而不是擴充？兩個理由：

1. **約束零重寫**：C1–C5、splitter 的 coverage、所有既有約束自動適用於採買機器。
   若另建一套「整數採買變數」，容量約束會退化成流體鬆弛（見第 3.2 層），
   per-BM 約束（max_per_bm、rack 打散）根本掛不上去。
2. **單一真相**：規劃期和執行期用同一顆腦，「規劃說放得下」= 存在真實 assignment
   （這是整個功能的存在理由，見 capacity-planning.md〈規劃腦與執行腦脫鉤的死局〉）。

---

## 第 1 層：兩個進入點與資料流

```
POST /v1/capacity/procure ──▶ solve_capacity_plan(ProcurementRequest)
                               （單 fab、單期：「這批需求，庫存夠嗎？不夠買幾台？」）

POST /v1/capacity/plan ──▶ solve_capacity_horizon(CapacityPlanRequest)
                            （多 fab × 多月編排器：逐月呼叫上面那個函式，
                             月與月之間滾動庫存/機位/已購池狀態）
```

`solve_capacity_horizon` 是**純編排**：它自己不建任何 CP-SAT 變數，只負責
「切帳本 → 組單月請求 → 呼叫 `solve_capacity_plan` → 把結果滾進下個月」。
所有數學都在 `solve_capacity_plan` 往下的呼叫鏈裡。因此本文先走單期（第 2–7 層），
再走編排（第 8 層）。

單期的內部資料流：

```
ProcurementRequest
   │  入口驗證（機型引用、allowed_bm_types）
   ▼
_solve_once(use_caps=True)          ← pass 1：尊重機位上限
   │  1. _derive_cells        →  (bucket, network) 格子
   │  2. _worst_case_counts   →  每機型最多需要幾台（槽位上界）
   │  3. 生成虛擬 BM           →  committed 先、buyable 後，各掛獨立 vrack
   │  4. slot/pool groups     →  機位上限、浮動池上限
   │  5. 候選過濾              →  network、allowed_bm_types、可達性 precheck
   │  6. 共用 CpModel：ResourceSplitter.build() → VMPlacementSolver.solve()
   ▼
INFEASIBLE？ ──▶ _solve_once(use_caps=False)   ← pass 2：拿掉機位上限重解
   │                └─ 成功 → 成因 = "space"（機位是瓶頸）
   ▼
_to_result  →  ProcurementResult（採買清單 + 四儀表 + roll-forward hooks）
```

**檢查點**：`solve_capacity_horizon` 裡有沒有任何一行建立 CP-SAT 變數？
（答案：沒有。如果你未來的改動讓它開始碰 cp_model，停下來想是不是放錯層了。）

---

## 第 2 層：`solve_capacity_plan` 的骨架 — 兩段式 solve 與誠實守則

讀 `capacity_planner.py::solve_capacity_plan`（約 60 行），注意三件事：

### 2.1 入口驗證：fail loudly

`committed_stock` 或 `allowed_bm_types` 引用了不存在的 `type_id` → 立刻回
`INPUT_ERROR`。為什麼不寬容跳過？docstring 講得很清楚：打錯字的機型引用若被
靜默忽略，會**偽裝成 capacity 缺口**——使用者看到「容量不足」，真相卻是
「你的機型名打錯了」。這是全 repo 一致的契約：**輸入錯誤大聲失敗，不做靜默修正**
（與 solver 拒絕重複 BM id 同一條原則）。

同理，splitter 放棄的需求（`dropped_requirements`，例如 count 上下限矛盾）也會
變成 `INPUT_ERROR` 而不是靜默成功——「靜默成功」等於回報「庫存足夠」，但那筆
需求根本沒被放置過。

### 2.2 兩段式 solve：用「兩解比對」判定 space 成因

- **Pass 1**（`use_caps=True`）：尊重 `max_bm` 機位上限。成功 → 直接回結果。
- **Pass 2**（`use_caps=False`）：只有 pass 1 **證明 INFEASIBLE** 且請求真的帶
  `procurement_caps` 時才跑。拿掉機位上限重解，若成功 → 表示「機位是唯一的
  瓶頸」→ 成因 `space`，並回報「如果機位存在，該買什麼」的 what-if 數字。

**替代方案與取捨**：也可以不用兩段式，把機位做成軟約束（超額付懲罰）一次解完。
沒選它的原因：(a) 軟約束的超額懲罰權重要跟七個既有目標項調和，調錯就會出現
「寧願超機位也不多買」的怪解；(b) 兩解比對的語意乾淨——「有上限解不了、沒上限
解得了」就是機位問題的**定義**，不需要解讀權重。代價是多一次 solve，而單期規模小，
划算。

### 2.3 誠實守則：只有「證明 INFEASIBLE」才允許歸因

`solver_status != "INFEASIBLE"`（例如時限到、狀態 UNKNOWN）→ 成因標 `unknown`，
**不進入分類**。因為「沒解出來」≠「無解」；把 UNKNOWN 當 INFEASIBLE 去跑 pass 2，
會把「只是還沒解完的模型」誤報成 `space` 缺口。維護時動到這段邏輯，
這條守則不可退讓。

**檢查點**：如果 pass 1 回 UNKNOWN，pass 2 會跑嗎？成因是什麼？
（答案：不跑；`unknown`，並提示調大 `max_solve_time_seconds`。）

---

## 第 3 層：`_solve_once` — 把採買翻譯成 placement（本文核心）

### 3.1 `_derive_cells`：規劃的計量單位是 (bucket, network) 格子

容量規劃的最小地理單位不是 AG 也不是 BM，是 **(打散桶, BGP 網路域) 的配對**
（決議 #37：一個 AG 可能混多個 BGP，cluster 只看得到自己 BGP 的那部分）。

格子從**供給側**宣告出來：in-stock 機器的拓撲、`ProcurementCap` 命名的桶、
bucketed committed stock。三者都沒有的格子**不存在**——需求側不能憑空生出一個
沒有任何供給宣告過的 (桶, 網域)。每個格子保留一份「代表拓撲」（`rep`），讓虛擬
BM 掛上去之後，AG 以外的維度（site/phase/room）的 anti-affinity 仍能解析。

### 3.2 `_worst_case_counts`：槽位上界的 soundness

先教一個概念：**sound 上界**。我們要決定「每個格子生成幾台某機型的虛擬 BM」。
這個數字是搜尋空間的邊界——生太多只是變慢（對稱性讓 solver 多繞路），
**生太少卻會改變答案**：明明買 5 台就可行的方案，因為只生了 4 台槽位而被判
INFEASIBLE，然後被錯誤歸因成 capacity 缺口。所以上界必須「寧多勿少」= sound。

直覺的算法 `ceil(總需求 ÷ 機型容量)` **不 sound**，反例：需求 3 台 40c VM、
機型 64c。`ceil(120/64) = 2`，但一台 64c 只裝得下一台 40c——實際要 3 台。
錯在把資源當成可以像水一樣跨機器流動（**流體鬆弛**，fluid relaxation），
忽略了裝箱的離散性。

正確算法（逐需求、逐 spec）：

```
per_bm  = _fits_count(機型容量, spec)         # 一台此機型裝得下幾台此 spec（逐維 floor、取 min）
per_bm  = min(per_bm, 該需求的 max_per_bm cap) # C4 或 rack-spread 會再收緊（見下）
upper   = spec_count_upper_bound(req, spec)    # 此 spec 最多要幾台（已含 pod floor、min/max_total_vms）
need   += ceil(upper / per_bm)
```

`rack-spread cap 也算 per-BM cap` 的原因：每台採買機掛獨立 vrack（見 3.3），
所以「每 rack 最多 1 台」對採買機而言就是「每台機最多 1 台」。

這個函式和 splitter 共用 `spec_count_upper_bound`——**兩邊算的上界必須一致**，
否則 splitter 可能生出比槽位還多的 VM。這是一條跨模組不變量。

### 3.3 虛擬 BM 生成：committed 先、buyable 後、各掛獨立 vrack

生成順序有語意：**committed 先生成**（它占用機位、減少要買的量）。兩種擺法：

- **bucketed**（`bucket` 有值）：機器已知落點，直接在那個格子生 `count` 台。
- **floating**（`bucket=None`）：在每個 network 相容的格子都生 `count` 份複本，
  再用一條 **pool group**（`Σ bm_used ≤ count`）保證全池總用量不超過已購台數。
  這是「複本 + 基數上限」表達「solver 自選落點」的標準技巧——不需要真的建
  「這台機器落在哪」的選址變數。

**vrack 技巧**：每台虛擬 BM 的 rack 被覆寫成 `vrack-<bm_id>`（`add_virtual`）。
理由：新買的機器總是可以分開上架（rack 級落點是 DC Hardware Team 的權責），
若讓同格子的虛擬 BM 共用代表拓撲的 rack，rack 級 anti-affinity 會把「兩台都還沒
買的機器」誤判為同 rack 而人為卡死。rack 以上的維度則**繼承代表拓撲**（保守）。

### 3.4 slot groups 與 pool groups → 同一個機制

兩種上限最後都變成 `(bm_ids 集合, cap)` 的 tuple，注入 `solver.bm_group_caps`：

| 群組 | 成員 | cap | 語意 |
|---|---|---|---|
| slot group | 某 cap 涵蓋的格子裡**所有**虛擬 BM（跨機型 + committed）| `max_bm` | 桶機位上限 |
| pool group | 某 floating committed 的所有複本 | `count` | 已購池總量 |

注意 slot group 是**跨機型共同計數**的——機位不分你買的是大機還是小機，一台一格。

### 3.5 候選過濾與可達性 precheck

每個需求的候選 = (呼叫端給的 `candidate_baremetals`，或 network 相容的 in-stock)
∪ network 相容的虛擬 BM，再被 `allowed_bm_types`（決議 #38）過濾虛擬 BM 部分。
候選清單按 network 快取，不逐需求重建。

**precheck**：有需求卻沒有任何候選 → 帶**可行動訊息**的 `INPUT_ERROR`
（`_no_candidates_reason`）。最常見的坑：需求填了一個**沒有任何供給宣告過的
network**——訊息會直接告訴你「用 in-stock 機器、procurement_cap 或
committed_stock 宣告一個該 network 的格子」。

### 3.6 組裝：共用 CpModel 與四個注入

```python
model = cp_model.CpModel()
splitter = ResourceSplitter(model, reqs, all_bms, config)   # 同一個 model
synthetic_vms = splitter.build()
solver = VMPlacementSolver(placement_request, model=model,
                           active_vars=splitter.active_vars)
solver.splitter_waste_terms = splitter.build_waste_objective_terms()
solver.procurement_bm_ids  = set(buyable_type_of)    # 這些 BM 用了要付 w_procurement
solver.committed_bm_ids    = set(committed_type_of)  # 這些付 w_committed_stock
solver.bm_group_caps       = slot_groups + pool_groups
result = solver.solve()
```

capacity planner 對 solver 的**全部**介面就是這四個注入屬性。solver 不 import
capacity_planner、不知道「採買」這個概念——它只知道「某些 BM 用了要加懲罰」
「某些 BM 群組有基數上限」。這個方向性（planner 知道 solver、solver 不知道
planner）是維護時要守住的依賴邊界。

**檢查點**：為什麼 floating committed 不需要「這台機器落在哪個格子」的選址變數？
（答案：複本已經枚舉了所有可能落點，pool group 的基數上限保證總量；solver 選
用哪些複本，等價於選了落點。）

---

## 第 4 層：CP-SAT 模型全覽 — 變數

先教一個概念：**reification（具體化）**——把「某條件是否成立」做成一個 BoolVar，
讓目標函數和其他約束可以引用它。`bm_used` 就是 reification 的典型：

```python
model.add_max_equality(bm_used[bm], [assign[vm1,bm], assign[vm2,bm], ...])
# bm_used = max(所有 assign) = 「有任何 VM 在這台上」的指示變數
```

完整變數清單（按建立者分組）：

| 變數 | 建立者 | 型別 | 語意 |
|---|---|---|---|
| `count[req, spec]` | splitter | IntVar `[0, upper]` | 這個需求切幾台這種 spec |
| `active[k]` | splitter | BoolVar | 第 k 個合成 VM 槽位是否啟用 |
| `assign[(vm, bm)]` | solver | BoolVar | VM 放在這台 BM 上（只為 eligible pair 建）|
| `bm_used[bm]` | solver（lazy）| BoolVar | 這台 BM 有沒有被用到（reified）|
| `util_pct` / `over` 等 | solver | IntVar | headroom 的整數化中間量（見第 6 層）|
| `rem` / slot 計數 | solver | IntVar | slot score 的剩餘空間中間量 |
| `bal_avail_{bucket}` / `bal_max` / `bal_min` | solver | IntVar | balance 目標的桶可用量 |

**沒有**「buy[t,b] 台數變數」——採買數是 `Σ bm_used[buyable]` 的湧現結果，
萃取階段才用 Counter 聚合成 per-type 台數（第 7 層）。

`bm_used` 是 **lazy** 建立的（`_ensure_bm_used_vars`）：只有 consolidation /
procurement / committed / balance / group caps 之一需要它時才建。維護時新目標項
若要引用它，記得先呼叫 `_ensure_bm_used_vars()`。

---

## 第 5 層：約束式全覽

按資料流順序（splitter 的先、solver 的後）。標 ★ 的是 capacity planning
新增或特別倚重的。

### splitter 端

1. **Coverage**：`∀ field: Σ_s count[s] × spec[s].field ≥ total_demand.field`。
   注意是 `≥` 不是 `==`——不能整除的需求（30c 需求、8c spec）若強制相等會
   INFEASIBLE；超額部分（waste）交給目標函數軟性壓低。
2. **Count bounds**：`Σ count ≥ max(min_total_vms, pod_floor)`、`Σ count ≤ max_total_vms`。
   ★ pod floor = `⌈total_pods / max_pods_per_node⌉`（缺口 1 的全部實作——Pod
   不是資源欄位，是台數下限）。
3. **Link**：`Σ_k active[k] == count[s]`——把「切幾台」連到「哪幾個槽位啟用」。
4. **Symmetry breaking**：`active[k] ≥ active[k+1]`。教學：同 spec 的 10 個槽位
   兩兩可互換，不加這條，solver 會把「啟用 {1,3}」和「啟用 {2,7}」當不同解
   各搜一遍；強制「前面的先啟用」把等價解砍到只剩一個。

### solver 端

5. **C1（active 變體）**：合成 VM 是 `Σ_bm assign == active_var`（啟用才必須放、
   未啟用全為 0）；一般 VM 是 `== 1`（或 partial 模式 `≤ 1`）。
6. **C2 容量**：`∀ bm, field: Σ demand × assign ≤ available_capacity`。虛擬 BM
   的 `used_capacity` 是零、`available` 就是機型容量——**同一條約束**同時管
   真機和虛擬機，這就是「翻譯」策略的紅利。
7. **C3 anti-affinity**：靜態 cap 是 `⌈N/|buckets|⌉`；含合成 VM 的 auto rule
   改用**動態 ceil**：`count_in_bucket × |B| ≤ total_active + (|B|−1)`。
   教學：這是「`a ≤ ⌈b/n⌉` 的整數線性化」標準寫法——CP-SAT 沒有 ceil 運算，
   兩邊乘 `n` 後用 `+(n−1)` 補整數餘數。★ 在採買情境，C3 作用在虛擬 BM 的
   落點拓撲上，**逼採買去補打散缺的桶**——這是「買對形狀」的數學保證。
8. **C4 max_per_bm**：`∀ bm: Σ assign[群組成員, bm] ≤ max_per_bm`。合成 VM 不需
   特判——未啟用的槽位其 assign 全為 0，自然退出總和。
9. **C5 failover**：沿用，不特判虛擬 BM。
10. ★ **bm_group_caps**（採買唯一新增的約束種類）：`Σ bm_used[群組] ≤ cap`。
    **數機器、不數放置**——一台機器住五個 VM 只占一個機位。寫在 assign 總和上
    會把機位限制偷換成 VM 數量限制（這是本模組最經典的錯誤模式，測資必須包含
    「一台 BM 多個 VM」的場景才能抓到）。

---

## 第 6 層：目標函數 — 權重階梯與每一項的角色

全部是**單一 minimize 的加權和**（不是 lexicographic 多目標）：

```
minimize   w_consolidation      × Σ bm_used[所有]          # 少用機器
         + w_headroom           × Σ headroom_penalty       # 單機利用率別超標
         − w_slot_score         × Σ slot_score             # 剩餘空間要可用（獎勵，故為負）
         + w_resource_waste     × Σ (allocated − demand)   # split 超配少一點
         + w_procurement        × Σ bm_used[buyable]       # ★ 買越少越好
         + w_committed_stock    × Σ bm_used[committed]     # ★ 拆封已購也有小代價
         + w_procurement_balance × (bal_max − bal_min)     # ★ 桶間結果可用量拉平
```

### 6.1 三層權重階梯（決議：in-stock → committed → 新買）

| 層 | 每台啟用成本 | 預設值 |
|---|---|---|
| in-stock | 0（只有 consolidation 的 10）| — |
| committed | `w_committed_stock` | 100 |
| buyable | `w_procurement` | 10,000 |

順位靠**數量級落差**成立，不是 hard constraint。設計原因：順位是偏好不是可行性
——當 in-stock 全是碎片、硬塞的 headroom 懲罰極高時，「開一台 committed」理論上
可以贏，這是**想要的**行為。維護守則：改任何權重時保持
`w_committed_stock ≫ 其他一般項`、`w_procurement ≫ w_committed_stock`，
否則三層順位靜默失效（不會有任何測試爆炸，只有採買數字悄悄變怪）。

注意 committed 權重**刻意非零**：設 0 的話 committed 和 in-stock 無差別，
solver 可能放著現有空間不用先拆封已購機。

### 6.2 headroom 的整數化管線（教學：CP-SAT 沒有浮點）

「利用率 > 90% 的部分要罰」需要除法和百分比，但 CP-SAT 只有整數。實作是一條
五步管線（`_compute_headroom_penalties`）：

```
after×100 = (used + Σ demand×assign) × 100     # 先放大 100 倍，避免小數
util_pct  = after×100 ÷ total                   # add_division_equality（整數除法）
raw       = util_pct − 90                       # 可能為負
over      = max(0, raw)                         # ReLU：add_max_equality 夾零
penalty   = max_field(over)                     # 最差維度決定罰量
```

這個「×100 → 整數除 → 減門檻 → max(0,·)」的套路是 CP-SAT 處理百分比門檻的
標準寫法，之後任何「超過 X% 就罰」的新需求都照抄這條管線。

### 6.3 balance 項（`_compute_procurement_balance_terms`）

平衡的是**採買後各桶的結果可用 CPU**（不是採買台數——in-stock 少的桶要多買去
補平，決議 #11）。三個實作細節都有陷阱：

1. **虛擬 BM 的容量是條件式的**：`bm_used × capacity`——沒被買的機器不貢獻
   可用量（它不存在）。
2. **桶集合只從真實 in-stock BM 播種**：一個只有虛擬 BM 的桶會貢獻 avail=0，
   把 `bal_min` 釘死在 0，讓 `(max−min)` 退化成「minimize max」——方向整個歪掉。
3. `bal_max`/`bal_min` 用 `add_max_equality`/`add_min_equality` 建。

預設 `w_procurement_balance=0`（**opt-in**）——設計草案寫 3，實作改 0，
要平衡必須顯式開。

### 6.4 平手區間：型別盲的台數目標

`w_procurement × Σ bm_used` 對機型**一視同仁**（一台就是一份懲罰）。後果：
「2 台大機」和「1 大 + 1 小」同分，選哪個由次要項或搜尋順序決定，**無保證**。
機型偏好目前只有 `allowed_bm_types` 硬過濾一種表達方式；per-type 成本權重是
預留的升級路徑（把 `w_procurement × Σ` 換成 `Σ cost[type] × bm_used`，
模型結構不用動）。

---

## 第 7 層：萃取 — `_to_result`

solve 完成後，從 `assignments` 反推語意：

1. **採買清單**：落在 `buyable_type_of` 的 BM id = 被買的機器；
   `Counter(type_id)` 聚合成 `procurement: [{type_id, count}]`。
2. **已購抽用**：同理得 `committed_used`；另外 `committed_entry_used` 按
   **committed_stock 的 entry index** 記帳——roll-forward 要精準扣「solver
   實際抽的那一筆」，不能只記型號總數（同型號可能有多筆不同桶的 entry）。
3. **split decisions**：從 splitter + cp_solver 取（哪個需求切了幾台什麼 spec）。
4. **四儀表**（對「in-stock ∪ 實際用到的虛擬 BM」的後處理，沒被買的機器不算）：
   - `nominal_available`：剩餘容量直加總（明知高估，當對照組）；
   - `remaining_node_slots`：每台 BM 還塞得下幾台 `reference_vm_spec`，加總。
     **貪婪逐機計算，不含 anti-affinity**——它是儀表尺標，不是可行性證明；
   - `stranded_available`：連 `min_useful_spec` 都塞不下的機器，其剩餘空間總和
     （真碎片）；
   - `balance_after`：各桶剩餘 CPU（平衡目標的證據欄）。
5. **roll-forward hooks**：`bm_placed`（每台機器被吃掉的資源）、`bought_bms` /
   `committed_bms`（materialize 用的完整 Baremetal 物件）、`bought_type_of`
   （id→type，讓下游不用去 parse 合成 id 字串）。

---

## 第 8 層：`solve_capacity_horizon` — 逐月滾動編排

### 8.1 切帳本、建 per-fab 狀態

`demand_book` 索引成 `(fab, period) → entries`；horizon = 帳本裡實際出現的月份
（排序）。每個 fab 複製一份自己的滾動狀態：`stock` / `caps_state` /
`committed_state`（`model_copy` 淺拷貝——因此有一條**程式碼慣例**：狀態更新
一律用屬性重綁（`bm.used_capacity = ...`），絕不就地修改巢狀物件，淺拷貝才安全。
docstring 明文記載，改動 roll-forward 時必守）。

`fab=""` 是單廠模式（整池）；混用空/具名 fab 被 `CapacityPlanRequest` 的
validator 拒絕——同一批機器不能活在兩個獨立滾動狀態裡被賣兩次。

### 8.2 單月迴圈

```
entries 不存在        → 跳過（未規劃月 ≠ 零成長月，報表上根本沒這列）
之前有月份失敗        → _blocked_report stub，不求解
否則:
  preq = ProcurementRequest(本月需求, 滾動中的 stock/caps/committed, 規則, config)
  res  = solve_capacity_plan(preq)
  cell attribution（必須在 roll-forward 之前——之後 id 就被改名了）
  成功 → _roll_forward + budget 記帳
  失敗 → 標記 failed_period，後續月份全部 BLOCKED
```

### 8.3 `_roll_forward` 四步

1. **消耗**：placement 吃掉的資源加進 in-stock 的 `used_capacity`。
2. **materialize**：被用到的採買/已購機器 append 進 stock，id 前綴
   `acq-<period>-`、vrack 同步改名。**為什麼必須改名**：虛擬 BM 的 id 是決定性
   生成的（`buy-{type}-{bucket}|{net}-{k}`），下個月會生成一模一樣的 id——不改
   id 會觸發 solver 的重複檢查（INPUT_ERROR，大聲爆炸）；改了 id 不改 vrack，
   則下月的新虛擬 BM 和上月買的機器**共用 rack**，rack 級打散把它們當同 rack，
   靜默地多買或誤報缺口（安靜的錯，更危險）。
3. **機位遞減**：每台 materialize 的機器從所有匹配 (bucket, network) 的 cap
   扣 1（決議 #30，防跨月超賣機位）。
4. **已購池 drain**：按 `committed_entry_used` 精準扣 count。

### 8.4 BLOCKED 語意與聚合

某月失敗後，該 fab 後續月份**不求解**，回 `BLOCKED` stub。理由是**結構性污染**：
失敗月的需求沒進庫存狀態，拿這個狀態去解下個月，數字會系統性偏樂觀——寧可
不給數字，也不給錯的數字。修法：修輸入、整本重算（solver 無狀態，重算便宜）。

`totals` 和 `budget_view` **只計成功月**；失敗月自己的 what-if 採買數只留在它的
`PeriodFabReport` 裡。維護時加任何聚合欄位，遵守同一條線。

**檢查點**：為什麼 cell attribution 要在 roll-forward 之前做？
（答案：attribution 靠 `res.assignments` 的 BM id 對回格子；roll-forward 會把
id 改成 `acq-` 前綴，之後就對不上了。）

---

## 第 9 層：維護者守則

改動前先過這張不變量清單——每一條的違反都不會立刻爆炸，但會產出錯的採買數字：

1. **槽位上界必須 sound**（寧多勿少）：動 `_worst_case_counts` 後，問自己
   「存在任何可行方案需要比我生成的更多台嗎？」答不出「不存在」就不要合併。
2. **跨期 id/vrack 唯一**：任何新的「機器跨月存活」路徑（例如未來的 fleet
   events `add`）都要比照 `acq-` 前綴 + vrack 改名。
3. **只有證明 INFEASIBLE 才歸因**；UNKNOWN 永遠是 `unknown`。
4. **bm_group_caps 數機器不數放置**：新的群組上限一律掛 `bm_used`，不掛 assign。
5. **balance 桶只從真實 BM 播種**。
6. **權重階梯數量級**：`一般項 ≪ w_committed_stock ≪ w_procurement`。
7. **輸入錯誤大聲失敗**，不偽裝成缺口。
8. **cell attribution 先於 roll-forward**。
9. **狀態更新用屬性重綁**，不就地改巢狀物件（淺拷貝前提）。
10. **splitter 與 planner 共用 `spec_count_upper_bound`**，不各自實作。

已知的刻意簡化（改之前先讀 capacity-planning.md 對應決議）：
缺口 3e/3f 未實作（既有節點不參與打散/max_per_bm）、`ShortfallDetail.bucket`
未填、graceful partial 未做、committed 無到貨月概念、機型無價格概念。

常見擴充的落點：

| 想加的東西 | 動哪裡 |
|---|---|
| 新的缺口成因 | `_classify` + `_shortfall_details` + `ShortfallDetail` docstring |
| 新的健康儀表 | `_to_result` 的 post_bms 迴圈 + `ProcurementResult` / `PeriodFabReport` 欄位 |
| 機型新屬性（如價格）| `BaremetalType` + objective 的 procurement 項改 per-type 權重 |
| 機器生命週期事件 | `solve_capacity_horizon` 的月迴圈（比照 backlog fleet events 設計）|
| 新的桶級上限 | 生成一組 `(bm_ids, cap)` 進 `bm_group_caps`，solver 端零改動 |

---

## 第 10 層：動手練習（畢業考）

按順序做，每題都有明確的「做完你會懂什麼」：

1. **走一遍真實請求**：`make run` 後把 `examples/capacity/plan_two_fabs.json`
   POST 到 `/v1/capacity/plan`，把回傳 JSON 對照第 7、8 層逐欄看懂。
   然後改壞它：把某個月的需求加大十倍，觀察 `shortfall_cause` 與 BLOCKED 的
   出現位置。→ 懂報表每一欄的來源。
2. **拆保護機制**：把 `_roll_forward` 的 vrack 改名（`capacity_planner.py`
   materialize 段的 `if mbm.topology.rack == ...` 三行）註解掉，跑
   `pytest tests/test_capacity_planner.py -k horizon`，觀察哪些測試紅、紅的
   訊息是什麼。→ 懂跨期命名不變量（第 9 層第 2 條）為什麼存在。
3. **觀察平手行為**：造一個兩機型（64c / 16c）、需求 10 台 8c VM 的
   procurement 請求，跑幾次觀察機型組合。→ 懂第 6.4 層的平手區間，以及為什麼
   「加 per-type 成本」是被預留的升級路徑。
4. **畢業考——把 `ShortfallDetail.bucket` 填起來**：`space` 成因目前說不出
   「哪個桶機位用罄」。提示：pass 1（capped）與 pass 2（uncapped）兩個解的
   差異裡藏著答案——uncapped 解在哪些桶買超過了 `max_bm`，哪些桶就是瓶頸。
   做完走完整流程：測試、`/verify-solver`、ADR。→ 能獨立完成一個「新資訊
   從 solver 流到報表」的全鏈路改動，就是可以獨立維護的標準。

---

*本文對應的設計決策記錄：`docs/capacity-planning.md`（含 Decision Log 與
實作對照勘誤）。兩份文件描述同一套程式碼，發現不一致時，以程式碼為準、
並回頭修文件。*
