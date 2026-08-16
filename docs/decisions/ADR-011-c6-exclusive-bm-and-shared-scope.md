# ADR-011: C6 獨占約束(exclusive BM)與跨 cluster 共用的 eco-system 群組

- **日期**: 2026-08-16
- **作者**: Claude (Fable)
- **相關 PR / commit**: branch `claude/topology-infeasible-analysis-ytig4x`(與 ADR-010 同批)
- **影響範圍**: `app/models.py`, `app/solver.py`, `app/diagnostics.py`, `app/mockgen.py`, UI, `examples/mock/`

> 寫作對象:一位正在學習 CP-SAT 與排程系統設計的工程師。

## 1. 背景與問題

Eco-system 節點(F5 / LVSLB / Bastion)有兩個既有模型表達不了的性質:
(a) **獨占整機**——appliance 一律一機一台,BM 上不得再有任何其他 VM;
(b) **跨 cluster 共用**——例如 5 個 cluster 共用同一組 6 台 F5,數量是
總量而非每 cluster 一份。現況 mockgen 的 dedicated pool + `max_per_bm=1`
可以在 candidate 層面近似 (a),但那只是 mockgen 的生成習慣——solver 契約
裡**沒有任何約束**阻止 Go scheduler 送來的 request 把別的 VM 排上 F5 的
機器;(b) 則因 `node_groups` 逐 cluster 展開而完全無法表達。

## 2. 考慮過的方案

1. **VM 屬性 `exclusive: bool`** —— 契約最短,但與既有規則目錄
   (C3/C4/C5 皆為 rule + selector)形制不一,Go 端要多學一種表達方式。
   **排除:一致性輸給便利性。**
2. **只靠 candidate 過濾**(維持現狀,要求 scheduler 濾乾淨)——零改動,
   但約束存在於呼叫方的自律而非模型;candidate 沒濾乾淨時 solver 會
   回一個「合法」卻違反營運事實的解。**排除:違反本專案
   「契約要在模型裡」的原則。**
3. **`ExclusiveBaremetalRule` + C6 約束**(採用)——與 C3/C4/C5 同形制
   的規則,約束進 CP-SAT 模型,INFEASIBLE 時可診斷、可歸因。
4. cluster_id 命名:**每個 eco 家族一個 id**(`shared-f5`/`shared-lvslb`)
   vs **單一 `"shared"`**(採用)。規則分組 key 是
   `(cluster_id, ip_type, node_role)`,家族的 node_role 本來就不同,
   拆細 id 沒有分組收益,只會多吃 UI 色盤 slot(8 色上限,ADR-009 的
   natural-sort 修正正是為了色盤稀缺);要單看某家族用 Role filter。
5. **合併成單一 `eco: bool` 旗標** vs **`scope` 與 `exclusive` 兩軸**
   (採用)。兩者回答的是**不同問題**,且四種組合都有真實情境:

   | | 非獨占 | 獨占(C6) |
   |---|---|---|
   | **cluster scope** | 一般節點(master/worker) | 每 cluster 自有的專用 appliance |
   | **shared scope** | 全 cluster 共用但可共機的池(如共用監控/log 節點) | F5 / LVSLB / Bastion(本案) |

   合併成一個旗標會讓對角線的兩格無法表達。**排除:把兩個正交維度
   壓成一個布林,是用今天的用例綁死明天的模型。**

## 3. 最終決策

新增 `ExclusiveBaremetalRule {group_id, vm_ids|selector}` 與約束 **C6:
群成員獨居**——成員所在 BM 上不得有任何其他 VM(含同群成員;appliance
語意,由使用者訪談確認)。mockgen 的 `NodeGroup` 增加
`scope: "cluster"|"shared"`(shared 群只生成一次,cluster_id 固定
`"shared"`)與 `exclusive: bool`(自動產出 C6 規則,並要求該 role 有
專屬 bm_profile pool)。

## 4. 實作走讀

**C6 的數學**(Step C)。對每台 BM b 建指示變數 `z_b`(reification——
把「b 被獨占群 G 佔用」這個條件物化成 BoolVar,讓其他約束能引用它):

```
z_b = max(assign[v,b] : v ∈ G)          (add_max_equality; bool 的 max 即 OR)
assign[u,b] + z_b ≤ 1     ∀ u ∉ G      (z_b=1 ⇒ 外人禁入)
Σ_{v∈G} assign[v,b] ≤ 1                 (同群成員也不同機——獨居)
```

每 BM 約束量 O(|G| + |V∖G|),遠優於逐對互斥的 O(|G|·|V∖G|)。

**為什麼用完整 reification 而不是更省的一側化**——這是本 ADR 最重要的
一段。一側化寫法(`z_b ≥ assign[v,b]` 只寫「⇒」方向)在 assign 空間的
投影可以**證明**與完整版相同:任何意圖合法的 assign 都能配一組老實的 z
通過約束(不砍解);任何通過約束的解,其 assign 必然滿足獨占(不放水);
亂升旗的解只是同一個 assign 配了多餘保守的 z。但這個證明有兩個**隱含
前提**:(1) z 不得進目標函數,(2) z 不得被其他程式碼當成「群真的在場」
讀取。兩者都是未來的程式碼可以默默打破的——正確性從數學保證退化為
code-review 紀律。在數十台 BM 的規模,`add_max_equality` 的成本是零,
於是選擇**用一條約束把不變量寫死在模型裡**,讓 z 對未來任何讀者、任何
用途(含目標函數)都語意精確。一側化是數萬 BM 規模才值得的最佳化,
屆時降級並回頭引用本段論證。

**兩軸各自落在哪一層**(常見誤解,寫清楚):

- `exclusive` → **solver 契約**。它變成 `ExclusiveBaremetalRule`,C6 進
  CP-SAT 模型。**`exclusive` ≠ `max_per_bm=1`**:後者只說「同群成員不
  同機」,外人照樣可以擠上同一台;前者說「整台機器沒有別人」。實測同一
  組輸入(2 台 f5 + 2 台 worker / 2 台 BM):`max_per_bm=1` 解出
  `bm-2: [f5-1, w-1, w-2]`(worker 與 f5 共機),`exclusive` 則
  INFEASIBLE(`failed_at: "exclusive"`)。C6 在數學上蘊含
  `max_per_bm=1`(第三條),但反之不成立。
- `scope` → **僅存在於 mockgen**,`PlacementRequest` 裡沒有這個概念。
  solver 只看得到 `cluster_id="shared"` 的 VM。之所以仍需要這個旗標,
  是因為 mockgen 的輸入語意是「per-cluster 模板 × N clusters」,而
  `node_groups` 沒有 cluster_id 欄位(它由展開迴圈產生)——`scope` 是
  用來說「這一組**不要**參與展開」。換句話說:**在 request 層,shared
  確實就只是 cluster_id 的值;在 generator 層,它是「不要複製 N 份」
  的指令**,而後者才是真正無法用其他既有旋鈕表達的東西。
  (考慮過改成更通用的 `cluster_id: str | None` 覆寫欄位——None 走展開、
  給值就照用。捨棄:它允許指向不存在的 cluster,而 `scope` 只表達真正
  需要的二分,錯誤空間更小。)

**其餘落點**:
- `app/models.py:322` `ExclusiveBaremetalRule` + `PlacementRequest.exclusive_bm_rules`
- `app/solver.py:533` Step B `_resolve_exclusive_rules()`(materialize 成
  vm_ids、空群 advisory;**無 auto-gen** — 獨占永遠是明確宣告)、
  `app/solver.py:816` Step C `_add_exclusive_constraints()`(`CONSTRAINT C6:`)
- `app/diagnostics.py:229` `_check_exclusive_feasibility()`:counting
  pre-check — 獨居使 |G| 成員需要 |G| 台可達 BM,與容量無關;
  constraint ladder 增加 `exclusive` 層(排 C4 之後)
- `app/mockgen.py:120` `NodeGroup.scope/exclusive`;shared 群單次生成
  (cluster_id="shared");`app/mockgen.py:784` `_build_exclusive_rules()`;
  `app/mockgen.py:597` sizing 的 solo floor(獨占 pool 一 VM 一 BM);
  exclusive 與一般 role 混用同一 bm_profile → 400(獨占機容量不可
  誤算給他人),同 role 同時出現在 exclusive 與非 exclusive 群 → 400

## 5. 取捨與風險

- C6 的獨居語意是為 appliance 定的;若未來出現「整機給一對 VM、群內可
  同機」的需求,加一個 `allow_group_colocation` 旗標放寬第三條即可,
  對外語意不變。
- 單一 `"shared"` cluster_id 意味著 shared 群在 UI 共用一個徽章色;
  家族識別靠 chip 文字與 Role filter。若某天 shared 家族間需要規則層
  的隔離(目前想不到場景),再拆 id。
- `add_max_equality` 在極大規模下比一側化多一半 clause;訊號:BM 數
  上萬且 solve 時間以 C6 為瓶頸時,依第 4 節論證降級。

## 6. 你應該帶走的知識

- **Reification** = 把條件物化成 BoolVar。一側化(只寫 ⇒)常見且省,
  但其正確性依賴「輔助變數不進目標、不被他人讀」兩個隱形前提——
  **省下的約束會變成需要人肉維護的不變量**。規模允許時,用等式把語意
  寫死(`add_max_equality`)是更便宜的保險。
- **投影論證**是檢驗建模捷徑的標準工具:比較兩種寫法在「你在乎的變數」
  上的投影是否相同,而不是憑感覺說「solver 不會那樣做」。
- 共用資源的分組 key 設計:先列出 key 的**實際作用點**(規則分組、UI、
  filter)再決定粒度——本案 node_role 已天然區分家族,cluster_id 拆細
  是零收益付色盤成本。

## 7. 驗證方式

- solver:`tests/test_solver.py::TestExclusiveOccupancy`(6 條:成員
  獨居、外人禁入、台數不足 INFEASIBLE 且 ladder failed_at=exclusive、
  pre-check 數字、兩獨占群互斥、INPUT_ERROR)
- mockgen:`test_shared_group_built_once_not_per_cluster`、
  `test_exclusive_group_emits_c6_rule_and_solo_bms`、
  `test_exclusive_role_requires_dedicated_pool`、
  `test_role_both_exclusive_and_not_rejected`、
  `test_shared_cap_not_expanded_per_cluster`
- 端到端:`examples/mock/shared_f5_ecosystem.json`(5 cluster 共用
  6 台 exclusive F5 + 2 台 bastion)→ 15 BM OPTIMAL,實際 solve 驗證
  8 個 eco VM 全部獨居(0 violations);UI preset 載入後 sh/ex 勾選
  正確、shared VM 徽章顯示 `sh`
