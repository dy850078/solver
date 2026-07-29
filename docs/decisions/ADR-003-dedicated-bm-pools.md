# ADR-003: 獨佔 BM 池——精確匹配的資格過濾 + assignment-level spill 罰項

> **追記 (2026-07-28)**：spill 能力已於 review 後**整段移除**（Capacity 負責人
> 決議：隔離優先；真需要溢出時由人工調配——在 Inventory 改機器的 pool 標籤,
> 下次 canonical run 自然生效）。移除的是 `PoolPolicy`、`w_pool_spill`、
> assignment-level 罰項與 `spilled` 計數;**保留**的是雙邊精確匹配隔離、
> 買進池、committed 池標籤與 `(bucket, network, pool)` 報表座標。隔離因此從
> 「政策保證」升為「結構保證」（想開也開不了）。本文 §2「spill 偏好的
> objective 表達」與 §4 的 spill 走讀保留為歷史紀錄與未來重啟時的設計依據
> ——實作在 git 歷史 `3549a56`。

- **日期**: 2026-07-28
- **作者**: Claude (claude-code)
- **相關 PR / commit**: `9d72371`(隔離)、`3549a56`(spill)、`072bbee`(報表)
- **影響範圍**: `app/models.py`、`app/capacity_planner.py`、`app/solver.py`、
  `app/web_static/js/report.js`、對應測試、`examples/capacity/plan_dedicated_pool.json`

> 寫作對象:一位正在學習 CP-SAT 與排程系統設計的工程師。

## 1. 背景與問題

每 fab 有 2 個長期存在的獨佔池(特定 cluster 專屬的一批 BM,可跨 AG)。執行期
Go 的 candidate filter 已能隔離;但規劃期不知道池的存在,池容量會被算進「大家
都能用」的可落地可用量——**系統性高估**,直接違反「規劃承諾的容量執行時放得
進去」的核心價值主張(e2e-vision 設計主題 4、決議 #18/#21)。已拍板政策:
消化順位=自己池 in-stock → 自己池 committed → spill 吃共用 → 新買**進池**;
spill 開關掛 `PoolPolicy` 表;獨佔採買一律掛池標籤。

## 2. 考慮過的方案

**池的過濾語意**
- 照抄 network 的 `net_ok`(requirement 端 `""` = wildcard)——**排除**:共用池
  必須是獨立 domain,`pool=""` 需求若是萬用,共用 cluster 就能踩進獨佔硬體,
  隔離失效。這是 pool 與 network 的關鍵不對稱。
- 池模擬成 pseudo-fab(借 per-fab 迴圈隔離)——**排除**:池與本 fab 共用 AG
  拓撲與實體機位(`max_bm` 跨池共用),假 fab 會算錯機位帳。
- ✅ 雙邊精確匹配,spill 是 policy 顯式開的例外。

**spill 偏好的 objective 表達**
- reified per-BM 罰項(`spilled_on_bm = OR(assign[...])` + `add_max_equality`)
  ——**排除**:要引入 reification 這個新建模技巧、每台共用 BM 多一個 BoolVar
  與約束,且對溢出量 scale-blind(開了門就不在乎溢幾台)。
- ✅ **assignment-level 線性罰項**:`w_pool_spill × Σ assign[vm, bm]`(spill
  pairs)。零新變數、零新約束,語意剛好是「溢出量本身要被最小化」。

**權重 `w_pool_spill` 的取值**
- 候選 500——**排除**:21 VM/BM 就會讓「spill 先於買」反轉(21×500 > 10,010),
  在實務範圍(~32 VM/BM)內。
- ✅ **200**:`> 110`(committed 100 + consolidation 10)保證單 VM 也不會為了
  省開 committed 機而溢出;`k×200 < 10,010` 保證 50 VM/BM 內 spill 先於買。

## 3. 最終決策

pool 作為第二個資格過濾維度(仿 network 的路徑、不仿它的 wildcard):模型加
`pool` 標籤(Baremetal/ResourceRequirement/CommittedStock/DemandEntry/
BucketMonthCell/BudgetRow)、`PoolPolicy{fab, pool, allow_spill}` 契約表、
buyable 依需求池分池生成並掛標籤、spill 以 assignment-level 罰項(w=200)表達
順位、計量 cell 延伸為 `(bucket, network, pool)`(無池請求形狀不變)。

## 4. 實作走讀

- `capacity_planner.py:312-345` — Step B(candidate 組裝):`candidates_for`
  以 `(network, pool)` 為鍵,雙邊 `bm.pool == pool` 精確匹配;spill 開啟時
  併入共用 in-stock 與共用 **committed** 虛擬機(集合 membership 判別),
  **刻意排除共用 buyable**——新錢永遠買進池。spill-BM 集合同時被記下來,
  這是罰項的原料。
- `capacity_planner.py:425-437` — 用 E0/S2 的 `splitter.vm_req_of` 反查每個
  池需求的合成 VM,組成 `solver.pool_spill_sets = [(vm_ids, bm_ids)]` 交給
  solver(比照既有 `procurement_bm_ids` hook)。顯式 `request.vms` 不在
  映射裡,天然不受罰。
- `solver.py:1083-1092` — Step C(objective):`Σ assign[(v,b)]`(pair 存在才
  取)乘上 `w_pool_spill` 加進 minimize。數學意義:每「一台 VM 落在共用機」
  付 200;它**必須**掛在 assign 而非 `bm_used`,因為同一台共用 BM 可以同時
  載共用需求(免費)與溢出需求(受罰)——bm_used 是布林,無法分辨誰在用。
- `capacity_planner.py:956-960` — `_cell_of` 回 `(bucket, network, pool)`。
  無池請求所有 key 的第三元素都是 `""`,cells/budget 的列數與排序
  byte-identical,只多一個 defaulted key(ADR-001 的 additive 先例)。
  slot caps(`cell_members`/`slot_groups`/roll-forward 遞減)**刻意不學 pool**
  ——機位是物理的、跨池共用。

## 5. 取捨與風險

買機是固定成本,因此「spill 先於買」只在 spill **能避免**買機時成立:殘量
一旦逼你買一台,solver 會把全部 VM 併進買來的機器而不再溢出(10,020 <
10,430)——這是湧現行為,已用測試釘住並寫進 docstring,報表讀者要知道
「spill=0 且有買機」不代表 spill 失效。反轉界:>50 VM/BM 的大量溢出會改買
(測試釘住);調 `w_pool_spill` 前先重算兩條界。虛擬 BM 數量乘上
|demanded pools|,池多時模型變大(生成僅限需求實際引用的池,已封頂)。
`config_fingerprint` 因 config 多欄位而全面翻新——這是 S4 設計內行為。

## 6. 你應該帶走的知識

- **同名維度未必同語意**:`""` 在 network 是 wildcard、在 pool 是獨立 domain
  ——複製既有 filter 前先問「空值是萬用還是一個真實的域?」,答錯就是隔離漏洞。
- **成本掛在哪個變數=表達哪種語意**:per-BM(`bm_used`)罰「開機器」,
  per-assignment(`assign`)罰「每台 VM 的落點」;共享資源上的差別待遇
  只能用後者。
- **固定成本會吃掉邊際偏好**:「A 先於 B」的權重順位,在 B 反正要發生時
  會自然失效——這不是 bug,是 minimize 的正確答案;設計時要把這個湧現
  行為講給報表讀者聽。

## 7. 驗證方式

- `tests/test_capacity_planner.py::TestDedicatedPools`(17 條:雙向隔離、
  spill on/off、三條順位、不碰共用 buyable、質量溢出反轉、roll-forward 池標籤、
  cells/budget 分池、spilled 覆蓋、無池形狀 guard)、
  `::TestPoolPolicyValidation`(重複/未具名 fab 拒絕、INPUT_ERROR 提示)。
- 手驗:POST `examples/capacity/plan_dedicated_pool.json` 到
  `/v1/capacity/plan`——月 1 各池自給(`in_stock_used=2`)、月 2 買 1 台
  `pool-ml`(budget row 帶池)、cells 出現 `(ag-2, "", pool-ml)` 座標。
