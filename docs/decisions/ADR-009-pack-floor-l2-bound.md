# ADR-009: elastic sizing 加入配對界(bin-packing L2 bound),tightness=1.0 不再結構性 INFEASIBLE

- **日期**: 2026-08-12
- **作者**: Claude (Fable)
- **相關 PR / commit**: branch `claude/topology-infeasible-analysis-ytig4x`(接續 ADR-008)
- **影響範圍**: `app/mockgen.py`, `tests/test_mockgen.py`, `docs/mock-request-generator.md`, `examples/mock/`

> 寫作對象:一位正在學習 CP-SAT 與排程系統設計的工程師。

## 1. 背景與問題

ADR-008 之後,`count` 留空的語意是「最少需要幾台」:解析下限(容量、spread、
張數)起步,escalation 每輪 +1 收尾。使用者實測(團隊常態是 `tightness=1.0`
「盡量塞滿」)發現大場景仍 INFEASIBLE:8 clusters × 5 infra,Spec2 memory
393,216 MiB、BM 737,280 MiB——**兩個 Spec2 加起來必爆**,所以 40 個 Spec2
至少要 40 台。但容量界把 VM 當可切分的液體,`tightness=1.0` 時只算出 27 台;
escalation 27+10 輪 = 37 < 40,**永遠爬不到**。`tightness=0.7` 沒踩到純屬
僥倖:分母灌水 1.43 倍剛好把起點抬過真值。下限漏了一項,鬆的 tightness
一直在幫忙遮醜。

## 2. 考慮過的方案

1. **escalation 改指數步進**(1, 2, 4, …,10 輪內可蓋 2^10 的 gap)——
   實作一行,任何 gap 都能收斂。**排除**:overshoot 之後沒有向下修正機制,
   會回傳「可行但不是最小」的台數,直接犧牲工具的核心語意;而且它治標——
   下限算錯才是病因。
2. **調高 `_MAX_ESCALATIONS`**——同樣治標:gap 與 cluster 數線性成長,
   任何常數上限都會在更大的場景再破一次;且每輪一次完整 solve,輪數
   放大是拿延遲換正確性的壞交易。**排除**。
3. **補配對界**(採用)——bin-packing 的經典 L2 下限:某維度需求
   `> capacity/2` 的兩個 item 加總必超過容量,不可同機 ⇒ 這種「大件」的
   **總數**是台數下界。純算術、一步到位,escalation 退回處理碎片級殘差
   (0–2 台)的原設計角色。

## 3. 最終決策

elastic floor 從三個必要條件擴成四個:`max(容量界, spread 界, 張數界,
**配對界**, min_copies)`。配對界對每個資源維度數出 pool 服務範圍內
`demand × 2 > capacity` 的 VM 數、取各維度 max;已存在的 BM 以
`cap // min(big_demands)` 估計可吸收的大件數並抵扣——**刻意高估抵扣、
低估 floor**,寧可留給 escalation 補刀也絕不過度配置(floor 沒有回頭路)。

## 4. 實作走讀

- `app/mockgen.py:518-530` 配對界本體:對維度 f,
  `bigs = [demand_f for vm in pool if demand_f × 2 > cap_f]`。數學上這是
  把 C2(容量約束,Step C 的 `Σ demand ≤ available`)投影到「兩個 item」
  的特例:`a > C/2 ∧ b > C/2 ⇒ a + b > C`,即大件在該維度構成一張
  pairwise conflict 的 complete graph,其著色數 = 節點數 = 台數下界。
  注意它與張數界(ADR-008)的**加總方向相反**:張數界跨 cluster 不乘
  (規則 per-cluster 獨立計數、可重用 BM),配對界跨 cluster **要乘**
  (大件是物理實體,40 個就是 40 個)——兩個下界一個來自規則、一個來自
  物理,混淆方向就會少算或多算。
- `app/mockgen.py:527-529` 抵扣估計:`slots = Σ cap_e_f // min(bigs)`。
  每台既有 BM 實際最多容納 1 個大件(相對本 profile 的 capacity),但對
  異質機型(更大的 fixed BM)用 `// min(bigs)` 可能高估——這是**方向性
  的選擇**:高估 slots → 低估 floor → escalation 兜底;反向(低估 slots
  → floor 過高)則會永久多開機器,無機制回收。
- `app/mockgen.py:531` floor 合成:配對界與其他下限平行進 `max`,
  escalation 的 `min_copies` 注入點不變(ADR-008 的絕對值語意)。
- 測試更新的連鎖:原 `test_escalation_resolves_fragmentation` 用 5×32c
  on 48c——`32×2 > 48`,新配對界把它「解析化」了,escalation 不再觸發。
  改用 20c on 48c(`20×2 ≤ 48`,配對界失明)× 7 台:每台 BM 裝 2 個、
  容量界說 3 台、真值 4 台——escalation 僅存的守備範圍就是這種
  「非大件的碎片」。

## 5. 取捨與風險

- 配對界只看**單一維度的一半**;三個「各占 40%」的 VM(兩兩可同機、
  三個不行)它看不到,仍靠 escalation。這是已知邊界,不是 bug。
- 抵扣估計在異質 fixed 機型下偏鬆(見走讀),混 pool 場景的 floor 可能
  低 1–2 台,由 escalation 補——代價是多幾次 solve,不是錯誤結果。
- `tightness` 的語意從此收斂:它只決定 headroom(容量界的灌水係數),
  不再影響可行性。若未來又出現「調 tightness 才能過」的案例,代表
  又漏了一個下限——那是重看這份 ADR 的訊號。

## 6. 你應該帶走的知識

- **`a > C/2 ∧ b > C/2 ⇒ a + b > C`**:超過半容量的 item 兩兩互斥,
  其總數是 bin packing 的經典下限(L2)。容量除法(液體視角)永遠看不到
  它——遇到「容量明明夠卻 INFEASIBLE」,先找大件。
- **下限的加總方向要跟著約束的作用域走**:per-cluster 的規則界跨 cluster
  取 max,物理性的配對界跨 cluster 累加。搞反其中一個就是差 5 倍的錯。
- **估不準的量,選錯誤方向**:floor 寧低勿高——低了有 escalation 兜底,
  高了沒有機制把多開的機器收回來。單向安全網下,誤差方向是設計決策。

## 7. 驗證方式

- `tests/test_mockgen.py::test_pack_floor_big_items_at_tightness_one`(2 cluster × 5 大件 → 10 台,零 escalation)
- `tests/test_mockgen.py::test_pack_floor_credits_fixed_bms`(3 fixed 抵扣 → elastic 只補 2)
- `tests/test_mockgen.py::test_escalation_resolves_fragmentation`(非大件碎片仍由 escalation 收斂)
- 回歸:全套 268 綠。
- 手動:`examples/mock/multi_cluster_big_infra.json`(UI preset 下拉可選)
  → 25 台 OPTIMAL 零 escalation;同 payload 改 `clusters=8` → 40 台、
  `clusters=10` → 50 台,tightness 0.7/0.9/1.0 結果一致。
