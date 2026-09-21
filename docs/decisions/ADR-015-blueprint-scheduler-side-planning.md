# ADR-015: blueprint 多 cluster 規劃——規劃期聯合求解、執行期靜態置入，全部落在 scheduler side，solver 零改動

- **日期**: 2026-09-21
- **作者**: Claude（與 dy850078 討論定案）
- **相關 PR / commit**: 本 ADR 所在 PR（無程式碼變更）
- **影響範圍**: 無——這是一份「決定不改 solver」的邊界決策記錄

> 寫作對象:一位正在學習 CP-SAT 與排程系統設計的工程師。
> 本 ADR 特殊之處:結論是 solver **一行都不用改**。記錄它的價值在於推演過程——
> 三個看似需要新功能的需求,如何被既有原語逐一接住。

## 1. 背景與問題

生產調度一次只解一個 cluster:前面的批次自由選了一個最優解,卻不知道後面還有
什麼需求,skew 逐批累積(ADR-012 §1)。Topology UI 的多 cluster 聯合模擬因此
與真實調度結果不一致——模擬是 joint、生產是 sequential greedy。

需求:一批 BM 已預留給特定幾個 cluster(專屬池,ADR-003),且 VM 的 id/name/
規格在規劃期就能備齊。能否**規劃階段做一次多 cluster 聯合求解產出藍圖
(blueprint)**,**執行階段逐 cluster 照藍圖靜態置入**?

## 2. 考慮過的方案

**軸一:執行階段如何使用藍圖**
- A. 盲放——scheduler 照藍圖直接寫入,不經 solver。快,但藍圖與現實脫節時
  (BM 故障、計畫外 VM)無聲壞掉。**排除:失去唯一的 drift 偵測點。**
- B. **驗證式置入(採用)**——每批仍呼叫 solver:藍圖 BM 塞進單元素
  `candidate_baremetals`,已建批次以 `pinned_to` + 真實 `used_capacity` 帶入。
  無 drift 時 presolve 把全固定的模型秒收斂成 OPTIMAL(近乎免費);
  INFEASIBLE 即精確的 drift 訊號。
- C. 每批重新聯合求解——後面的解可能想搬已建 VM,搬不動就退化成 rollout,
  且每批結果不可預期。**排除:不確定性正是藍圖要消滅的東西。**

**軸二:blueprint 概念的歸屬**
- A. 下沉進 solver(rollout 加 `mode: "joint"`,或新開 blueprint 端點)。
  曾經成立的理由:合併多 cluster 請求的邏輯(規則 flat-map、synthetic id 歸屬、
  切片回報)已在 UI JS 存在一份(`rollout-form.js:554-565` 的 "All at once"),
  Go 再寫是第三份。**被消解而排除:**一旦確定走明確 `vms`(見軸三),合併只剩
  兩個 list 串接;切片是 Go 對自家 DB 的 GROUP BY;而藍圖是**有狀態物**
  (世代、active/superseded、關聯調度單)——solver 是無狀態純函數,狀態歸
  Go/DB 是 reconcile 已立過的原則(ADR-005,決議 #25)。
- B. **(採用)scheduler side 全包**——規劃 = 一次普通 `split-and-solve`
  (多 cluster 明確 VM 合併請求);執行 = 一次普通驗證 solve;重規劃 = 一次
  普通 `rollout`。指紋:`config_fingerprint` 既有,BM 快照 hash Go 自算。
  命名張力靠歸屬消解:`rollout` 保留「模擬建置順序」語意,`blueprint` 是
  scheduler 側名詞,**不改任何端點名**。

**軸三:明確 VM vs splitter synthetic**——synthetic 只在 `requirements`
(預算→切分)時存在;規劃期已備齊 id/name 就走明確 `vms`,splitter 整段跳過。
藍圖裡全是真實身分,「執行期必須凍結切分決策」的風險整個消失。`requirements`
保留為預算先行場景的可選能力,若用,藍圖必須把切分結果物化成明確 VM。

## 3. 最終決策

blueprint 完全落在 inventory DB + scheduler + UI 側;solver 維持無狀態純函數,
零改動。三段協議:

1. **規劃**:Go 合併目標 cluster 為一個 split-and-solve 請求 → 結果與請求
   JSON 原文一起存 DB(`blueprint` 主檔:`bm_group_id`、`generation` +
   `parent_blueprint_id` 世代鏈、status、兩個指紋;`blueprint_placement`
   明細:vm/bm/cluster、`pinned` 旗標。與 `schedule_placement` 分表——
   後者是事實、前者是意圖,同一 VM 會出現在多個世代的意圖裡)。
2. **逐批執行**:Go 切出 cluster k 片段組驗證請求(單元素
   `candidate_baremetals`;已建 VM `pinned_to`;真實 `used_capacity`)→
   OPTIMAL 則執行,assignments 必等於藍圖;`schedule_request` 以 nullable
   `blueprint_id` 關聯,`schedule_placement` 以 `blueprint_placement_id`
   記錄「事實兌現了哪筆計畫」,VM 粒度的 plan-vs-actual 於是可查
   (reconcile.py 只到容量格粒度)。
3. **重規劃**(雙觸發、同機制):被動(驗證批 INFEASIBLE / INPUT_ERROR /
   指紋不一致)或主動(後續 cluster 需求改版)→ 已建 VM 全部作
   `existing_vms` pins 重新聯合求解,只放剩餘 cluster → 新世代藍圖,舊版
   superseded。ADR-012 的 grandfathering 保證已建叢集永不逼出 INFEASIBLE。

## 4. 實作走讀(既有縫隙——為何零改動就站得起來)

- `models.py:160-173`(Step B 之前的輸入語意):`pinned_to` 是「過去的事實」,
  「新 VM 必須放某台」的正確表達是單元素 `candidate_baremetals`(ADR-012 §2
  否決過混用)。藍圖執行正是後者的設計用途——這個二分讓同一套模型服務
  規劃、驗證、重規劃三種用途。
- `solver.py:244-321` `_normalize_pinned_capacity`(Step A 前的正規化):
  `effective_used = used - Σ pinned_demand`,pinned 的固定 assign 變數再經
  C2 加回,帳面淨額為零;四道 INPUT_ERROR 守門(host 不存在、candidate 衝突、
  `used > total`、effective < 0)。驗證式置入的 drift 偵測大半來自這裡——
  「免費」不是比喻,是既有程式碼路徑。
- `rollout.py:151-166` fold-forward 帳本:`pinned_to` 與
  `used_capacity += demand` **成對**更新。Go 組重規劃請求時必須複製同一套
  算術,少一半就是 double-count 或 phantom capacity。
- `models.py:498-513` `PlacementAssignment.pinned`:結果回描完整終態,
  scheduler 只執行 `pinned=False` 的條目——藍圖世代的自我完整性
  (重規劃帶入的事實 vs 本代新規劃)直接沿用這個旗標。

## 5. 取捨與風險

- **前綴安全性**:C2/C3/C4/C6 全是「≤ 上限」約束,聯合可行解的任何子集必
  可行——照藍圖靜態置入,任何建置順序的任何中間狀態自動可行。這與 ADR-013
  的「聯合可行 ≠ 逐批可行」**不矛盾**:那警告針對「每批重新求解」的貪婪
  路徑;藍圖消除了執行期的選擇,也就消除了路徑依賴。
- **C5 例外**:failover N-1 不具前綴安全性——backup 建成之前保護本來就不
  成立。建置期暫態,今日逐批亦然,非新風險,但排程順序宜讓 backup 早建。
- **drift 分類與處理**:BM 消失/縮容 → 驗證批 INFEASIBLE 或 INPUT_ERROR;
  計畫外 VM 插入 → `used_capacity` 上升被驗證批自動看見,擠得下照常、擠不下
  重規劃;需求改版 → 主動重規劃;config 漂移 → `config_fingerprint` 不一致
  即拒。快照指紋是快路徑,驗證求解是慢路徑兜底。
- **重規劃的邊界**:已建 VM 永不搬遷,最佳性降為「給定凍結前綴下的最優」。
  前綴嚴重卡死(占走關鍵 AG)時唯一出路是 rebalance(unpin + minimize
  moves)——那是 ADR-012 §5 言明的獨立未來模式,不是重規劃的副作用。
- **Go 合併碼是新的第一線暴露面**:裸 solve 對規則裡未知的 `vm_ids` 是
  無聲丟棄(`solver.py:492-516` 只去重不驗存在;未知 id 無 assign 變數,
  建約束時自然消失)。C6 成員集變空 → 獨占保護蒸發,仍回 OPTIMAL;完整
  檢查只在 rollout `_validate`(`rollout.py:188-262`),裸路徑僅零成員時發
  advisory(`solver.py:801-810`),部分打錯連警告都沒有。不能一律
  INPUT_ERROR 是因 split 路徑允許引用尚未物化的 synthetic id。短期:Go
  合併後自查規則 id ⊆ VM 清單、讀回 advisory;中期 follow-up:在無
  `requirements` 的 pure-solve 路徑補 INPUT_ERROR。
- **重新審視訊號**:drift 率高到頻繁重規劃,藍圖失去意義——縮短規劃視窗,
  或退回逐批 rollout。

## 6. 你應該帶走的知識

- **約束的方向決定子集可行性**:全是「≤ cap」的模型,可行解的任何前綴都
  可行——「聯合規劃、分批執行」的安全性是從約束形狀讀出來的,不是測出來的。
- **事實/約束二分 + 狀態外置**:`pinned_to`(事實)、單元素 candidate
  (約束)、DB(狀態)三者各就各位,一個純函數 solver 就能服務規劃、驗證、
  重規劃三種用途,不需要任何新模式。
- **驗證式執行**:把 solver 當 checker——全固定的模型 presolve 秒解,
  等於用一次 API 呼叫免費換到 drift 偵測。盲目執行計畫是把偵測點丟掉。

## 7. 驗證方式

無程式碼變更,`make test` 維持全綠即可。設計本身可用既有機能親手驗證:
- 前綴安全性:`make cli INPUT=examples/rollout/multi_cluster_mixed_specs.json`
  的 joint 對照(UI "All at once")取得聯合解,手工切出單一 cluster 組驗證
  請求(候選=藍圖 BM、他批為 pins)重放,應得 OPTIMAL 且位置不變。
- drift:同一驗證請求把某台 BM 的 `used_capacity` 調高到擠不下,應轉
  INFEASIBLE。
- scheduler 側實作時的測試要點:active 藍圖唯一性、世代鏈、
  plan-vs-actual join 的 drift 查詢、合併請求的規則 id 自查。
