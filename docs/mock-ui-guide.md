# Mock 情境產生器 — 使用手冊

> 對象：使用 Web UI 產生 VM 放置測試情境的使用者
> 頁面路徑：啟動服務後開啟 **`/ui`**

---

## 目錄

1. [這是什麼](#1-這是什麼)
2. [開啟方式](#2-開啟方式)
3. [畫面總覽](#3-畫面總覽)
4. [快速上手（3 步）](#4-快速上手3-步)
5. [表單欄位逐區說明](#5-表單欄位逐區說明)
6. [兩顆按鈕：Generate / Generate & Run](#6-兩顆按鈕generate--generate--run)
7. [看懂結果](#7-看懂結果)
8. [常見情境配方](#8-常見情境配方)
9. [小技巧與注意事項](#9-小技巧與注意事項)
10. [疑難排解](#10-疑難排解)

---

## 1. 這是什麼

這個頁面讓你用**少量高階參數**，一鍵生成一份完整、可直接求解的 **VM → Baremetal 放置情境**（`PlacementRequest`），不必手刻 JSON。生成器會：

- 依你的設定產生 VM、Baremetal、拓樸與規則
- 先鋪出一份合法的 ground-truth 放置
- **用真實 solver 自我驗證**，確認這個情境「解得出來」

你可以拿它來 demo、壓測 solver、或快速產生測試資料。

---

## 2. 開啟方式

```bash
make run            # 啟動服務（預設 http://localhost:50051）
```

瀏覽器開 **`http://localhost:50051/ui`**（首頁會自動導向）。

---

## 3. 畫面總覽

| 區域 | 內容 |
|------|------|
| **左側欄「Generate mock」** | 主要操作區：填參數 → 產生情境 |
| 左側欄「Input」 | 產生後的 `PlacementRequest` JSON、Run solver 按鈕、載入既有 example |
| **右側內容區** | 結果：統計、Rack 機架圖、AG × Rack 矩陣、指派明細 |

> 💡 **左側欄可拖曳加寬**：把滑鼠移到側欄右邊界，出現左右箭頭游標時拖曳即可，寬度會自動記住。

---

## 4. 快速上手（3 步）

1. 左側欄最上方選一個 **Preset**（例如 `basic_single_cluster`），表單會自動填好。
2. 按 **Generate & Run**。
3. 看右側：狀態列顯示 `✓ verified`，下方出現機架圖與指派結果。

想從零開始也可以：直接改左側表單欄位，再按 Generate & Run。

---

## 5. 表單欄位逐區說明

### 5.1 Clusters / Seed
- **Clusters**：要生成幾個 K8s cluster。
- **Seed（選填）**：給同一個 seed，每次會生出**完全一樣**的資料（可重現）；留空則每次隨機。

### 5.2 VM specs（規格目錄）
定義一份「具名 VM 規格」清單，欄位：`name · cpu · mem(MiB) · storage(GB) · gpu`。
- 這裡只是**定義**規格；要不要用、哪個 role 用，在下面 Roles 表格的 **spec** 欄指派。
- 按 **+ Add spec** 可新增多種規格。

### 5.3 Roles（每個 role 一列）
共 6 種 role：`master / learner / worker / infra / l4lb-storage / bastion`。每列可設：

| 欄位 | 意義 |
|------|------|
| **count** | 每個 cluster 要幾台這種 VM（0 = 不產生） |
| **ip_type** | 這個 role 的網路類型（`routable` / `non-routable` / none） |
| **spec** | 這個 role 用哪個 VM spec；`(default)` = 用內建預設規格 |
| **max/BM** | 一台 baremetal 上、**同一個 cluster** 這個 role 最多幾台（留空 = 不限，顯示 ∞） |

> ⚠️ **ip_type 很重要**：只要開了 anti-affinity（見 5.6），凡是 count ≥ 2 的 role **必須**選 ip_type，否則按 Generate 會報錯。原因：分散規則是以 `(cluster, ip_type, role)` 分群，ip_type 空的會被略過。

> **max/BM 的範圍**：填在 master 列 = 「每個 cluster 的 master，一台 BM 最多 N 台」。各 cluster 獨立計算。

### 5.4 Baremetal profiles（機型）
定義有哪幾種實體機，欄位：`name · cpu · mem · storage · gpu · count · roles`。

| 欄位 | 意義 |
|------|------|
| **count** | 這種機型幾台；**留空 = 自動估**（依 Tightness 算到剛好夠裝） |
| **roles** | 這種機型**只服務哪些 role**（逗號分隔）；**留空 = 所有 role 都能用** |

> **用 `roles` 做「硬體池」**：例如一列 `control-plane` 填 `roles = master,learner,infra,l4lb-storage`、另一列 `worker` 填 `roles = worker`，就代表控制面 VM 只落在控制面機、worker 只落在 worker 機。**只要任何一列填了 roles，候選機就自動改用「依 role 分池」，不看拓樸位置。**

### 5.5 Topology（拓樸維度）
六個維度（由大到小）：`Sites · Phases · DCs · Rooms · Racks · Ags`。
- **灰底、淡字「1」的欄位**代表「沒填 = 預設 1」（該維度只有一個桶，所有機器同屬它）。
- 一旦填入 > 1，該維度才會分成多個桶。
- **沒用來做分散的維度，設不設都不影響結果。**

> `Racks` 建議設成 `Ags` 的倍數（例如 ags=3 就用 racks=3/6/9…），AG 分佈才會平均。

### 5.6 Rules（規則）
- **anti-affinity（勾選）**：自動讓「同 cluster、同 ip_type、同 role」的 VM 分散到不同 AG（高可用）。
- **failover（勾選）**：為每個 cluster 產生 master ← learner 的 N-1 備援規則（需要該 cluster 同時有 master 與 learner）。
- **Spread AGs**：anti-affinity 希望分散到「幾個 AG」的目標值。這是**軟性目標**——撐不起只會發出警告，不會讓情境失敗。
- **Tightness**：機房要塞多滿（0～1）。**只在有機型 count 留空（自動估數量）時生效**；越接近 1 越擠、機器越少，越小越寬鬆、機器越多。

### 5.7 Advanced overrides（JSON，進階）
摺疊區，給少數表單沒有的進階項（例如 `config_overrides`、加權 ip_type 分佈）。內容會**疊加覆蓋**在表單值之上。一般使用者可忽略。

---

## 6. 兩顆按鈕：Generate / Generate & Run

| 按鈕 | 行為 |
|------|------|
| **Generate** | 只產生情境，把 `PlacementRequest` JSON 灌進下方「Input」編輯器；你可再檢視/微調後自己按 Run solver |
| **Generate & Run** | 產生後**立刻求解並畫出結果**（最常用） |

按鈕下方的**狀態列**會顯示可行性與規模（VM / BM / AG 數量）。

---

## 7. 看懂結果

### 可行性狀態
| 狀態 | 意思 |
|------|------|
| ✓ **verified** | 已用真實 solver 驗證，這個情境**解得出來** |
| ⚠ **infeasible** | 產生了，但 solver 判定**無解**（條件太緊）；仍會載入讓你檢視 |
| unverified | 沒跑驗證（一般不會遇到） |

### 右側視覺
- **統計列**：放置成功數、未放置數等。
- **Topology（機架圖）**：每台 BM 一格，顯示上面放了哪些 VM；右上「Group by」可切換用 Site / Room / Rack / AG 等維度重新分組檢視。
- **AG × Rack 矩陣**：一眼檢查 anti-affinity 有沒有把 VM 平均分散開。
- **Filter**：可依 cluster / role / ip_type 篩選只看某群。
- **Result**：逐台 VM → BM 的指派明細。

---

## 8. 常見情境配方

### A. 控制面機與 worker 機分開（硬體池）
- Baremetal profiles 開兩列：
  - `control-plane`：roles = `master,learner,infra,l4lb-storage`
  - `worker`：roles = `worker`
- 控制面 VM 就只會落到控制面機，worker 只落到 worker 機。

### B. 每個 cluster 的 non-routable master，一台 BM 只能有一台
- Roles → master 列：`ip_type = non-routable`、`max/BM = 1`。
- 效果：每個 cluster 各自「一台 BM ≤ 1 個 master」，不影響其他 role。

### C. master 跨 AG 高可用分散
- 勾 **anti-affinity**，`Spread AGs = 3`，`Ags = 3`（或更多）。
- master count 至少 3，並記得選 ip_type。

### D. 不同 cluster 落在不同機房
- `Clusters ≥ 2`，且把 `Rooms`（或 `Sites`）設成 > 1。
- 生成器會把不同 cluster 分配到不同 room/site。

### E. 想模擬「機房很滿 / 很空」
- 機型 count **留空**（走自動估數量），再調 **Tightness**：0.9 很滿、0.5 很空。

---

## 9. 小技巧與注意事項

- **ip_type 是分散規則的關鍵**：開 anti-affinity 時，多台的 role 一定要選 ip_type，否則報錯。
- **Topology 沒填 = 1**：灰底淡字「1」代表預設；不拿來分散就完全不影響。
- **Tightness 只對「count 留空的機型」有效**；機型都寫死 count 時它被忽略。
- **Preset 會回填整個表單**；表單放不下的進階項會自動寫進 Advanced 區，不會遺失。
- **Seed 固定 = 完全可重現**，方便回歸比對。
- **Generate 只是灌 JSON、Generate & Run 會直接求解**；想先看/改 JSON 就用前者。

---

## 10. 疑難排解

| 現象 | 原因 / 解法 |
|------|-------------|
| 按 Generate 跳 **400 錯誤**、提到 `ip_type_by_role` | 有 count ≥ 2 的 role 沒選 ip_type。補上 ip_type，或關掉 anti-affinity。 |
| 狀態 **infeasible** | 條件太緊。常見做法：把機型 count 留空讓它自動估、調低 Tightness（更寬鬆）、或放寬 `max/BM`。 |
| infeasible 且 `Racks` 不是 `Ags` 的倍數 | AG 分佈不平均會讓分散擺不下。把 `Racks` 設成 `Ags` 的倍數（如 racks=6, ags=3）。 |
| 某個 role「找不到可用的 baremetal pool」 | 你用了機型 `roles` 分池，但沒有任何機型服務這個 role。幫它加一列服務該 role 的機型，或把某機型的 roles 留空當共用池。 |
| failover 沒生效 | 該 cluster 需要**同時有 master 和 learner**；缺一個就會略過。 |

---

如需更深入的參數對應與後端邏輯，請參考開發文件 `docs/mock-request-generator.md`。
