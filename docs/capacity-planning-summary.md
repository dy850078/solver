# Capacity Planning — 一頁摘要（Review 用）

> 完整設計見 `docs/capacity-planning.md`（含 35 條 Decision Log、缺口 2 + 3a–3f）。
> 本頁只給決策脈絡，不用讀 700 行。

---

## 要解決什麼

K8s Capacity 大臣接 User 的 CPU/Mem/Storage/**Pod** 需求（per cluster、逐月），要產出：
1. **Add Node Plan** — 加幾台、什麼規格的 VM as K8s Node。
2. **跨廠區 × 跨月的容量報表 + 採買建議** — in-stock、缺口、要買幾台 BM、成因。

現有 `split-and-solve` 已能解「需求→VM spec×count」；本提案補三塊缺口，**不重寫 solver，加一層 Capacity Planning 編排層**復用既有 splitter + solver。

## 三個缺口 → 解法

| 缺口 | 解法 |
|---|---|
| **1. Pod 維度** | 全域 `max_pods_per_node` → 退化成節點數下限 `⌈總Pod/上限⌉`，placement 端零侵入 |
| **2. 規劃模式（要買幾台）** | in-stock 放不下的殘量 → 在 AG/DC 桶內生成虛擬可採買 BM，minimize 台數；桶有 `max_bm` 就受限、缺就理想化無上限 |
| **3. 多月×多廠區報表** | 逐月 roll-forward（庫存+機位滾動）、per-fab 自給自足（可平行）、缺口轉採買、分層報表 |

## 核心設計決策（濃縮）

**需求輸入**
- **增量語意**：需求＝「這個月加多少」，sizing 不需 cluster 現況（只 placement/採買需 BM 現況）。
- **稀疏帳本** `list[DemandEntry]`，每列 `(cluster, role, 月)`，修訂＝upsert；**horizon＝帳本出現的月份**（填幾月規劃幾月）。
- **月份三態**：無列＝未規劃、全 0＝確定不長、某維度>0＝有需求（維度層級 0＝不設下限）。
- **Provenance**：需求單只裝 User 意圖；現況（in-stock、現有分佈、HA policy）由 **Go Scheduler Service** 整合 Inventory 帶入。solver 維持無狀態。

**供給 / 採買**
- 採買單位＝**單台 BM**；落點 = **AG/DC 桶**，`max_bm` 掛桶層級（rack 級由 DC Hardware Team 張羅）。
- **平均**平衡的是「採買後結果**資源**可用量」（考慮 in-stock 現況、補最短的桶），非採買台數。
- 打散維度 `ag | datacenter` 一鍵切換（覆蓋 AG→實體 DC 轉換）。
- 多機型可選；1U 假設（GPU 多 U 未來）。

**新舊一起打散（整個 cluster）**
- anti-affinity 作用範圍＝整個 cluster；帶入現有節點**每桶聚合數**（`ExistingDistribution`）。
- **Role-aware**：master 硬（5 台→2/2/1）、worker 軟（advisory + 慢慢 balance）。
- master 硬約束**容忍既有違規**：`new[b] ≤ max(0, cap−existing[b])` + advisory，不因歷史傾斜擋死。
- `max_per_bm`（master 1/BM）需**per-BM 聚合 count**（`ExistingBmOccupancy`，count-only 免 double count）。

**報表**
- 頭條＝**可落地可用量**（真實 bin-pack）+ 缺口 + **成因**，非名目資源加總（名目會高估）。
- 對象 **2 個**：Capacity 大臣（主，含編預算視圖）、長官。粒度停在 **`(fab, AG/DC, 月)`**，不下 BM/rack（那是執行階段）。
- **node 台數** 與 **BM 台數** 兩欄分開（node 給 owner 建 VM、BM 給財務編預算）。
- 成因**結構化** `ShortfallDetail`：`capacity`/`anti_affinity`/`space` + 哪個桶/維度 + 一句人話。
- 健康儀表 `remaining_node_slots` 用單一全域 `reference_vm_spec`；碎片用 `min_useful_spec`。
- 形式：canonical JSON 優先，Web UI（複用 `app/web_static/`）先做、Excel 後補。

## 資料契約（要點）

```
POST /v1/capacity/plan
Request  CapacityPlanRequest:
  demand_book[]           # 稀疏帳本 (cluster, role, 月, 資源, 可選 vm_specs)
  in_stock[]              # 既有 Baremetal（used_capacity 已含現有 VM 資源）
  procurement_types[]     # 可採買機型 (capacity, fab)
  procurement_caps[]      # 桶機位上限 (fab, bucket, max_bm)；缺=理想化
  existing_distributions[]# 現有節點每桶聚合數（打散用）
  existing_bm_occupancy[] # 現有 VM per-BM count（max_per_bm 用）
  config                  # max_pods_per_node, reference_vm_spec, min_useful_spec,
                          # procurement_spread_dimension, w_procurement_balance
Response CapacityReport:
  by_fab_period[]         # 頭條(node/BM 台數、缺口、shortfalls) + cells(AG/DC×月)
  budget_view[]           # fab × DC × 月 → BM 台數（編預算）
```

## 落地順序（各自可獨立交付、皆 additive）

1. **Phase 1 — Pod 維度**：`max_pods_per_node` + `total_pods` + splitter 節點數下限。最小、向後相容（預設停用）。
2. **Phase 2 — 採買量（單 fab 單月）**：殘量 bin-pack + 多機型 + max_bm + 可落地/碎片報表。
3. **Phase 3 — 多月 roll-forward + 多 fab 聚合**：`/v1/capacity/plan` + 分層報表 + Web UI。
4. **Phase 4（未來）— 跨 fab 調撥**。

## 復用 vs 新增

- **復用**：splitter（需求→VM）、solver（placement + `bm_used` + anti-affinity + max_per_bm）、diagnostics（成因分層）、`app/web_static/`。
- **新增**：Pod 下限、採買虛擬 BM + max_bm + 平衡目標、existing-baseline 打散、報表聚合層、`/v1/capacity/plan`。

## 現階段刻意不做（未來）

跨 fab 調撥、what-if 多情境比較、rack 級機位精算（GPU 多 U）、完整 `existing_vms`（per-VM 身分）、per-spec Pod 上限。
