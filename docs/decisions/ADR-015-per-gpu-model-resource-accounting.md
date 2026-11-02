# ADR-015: GPU 資源改為逐型號記帳——`gpu: dict[str,int]` 取代 scalar `gpu_count`

- **日期**: 2026-08-31
- **作者**: Claude（與 dysiang 協作定案）
- **相關 PR / commit**: branch `claude/bm-gpu-vm-affinity-k71sce`
- **影響範圍**: `app/models.py`、`app/solver.py`、`app/splitter.py`、`app/diagnostics.py`、`app/sizing_floors.py`、`app/capacity_planner.py`、`app/mockgen.py`、examples/docs/web_static、全測試套件

> 寫作對象:一位正在學習 CP-SAT 與排程系統設計的工程師。

## 1. 背景與問題

Go scheduler 傳入的 inventory 帶有 GPU「型號 + 數量」（例如一台 BM 有 5 顆
H200），但 `Resources.gpu_count` 是單一 scalar：所有 GPU 在 solver 眼中同質
（fungible）。「這台 VM 要 2 顆 h200」無法表達，混插機（3×H200 + 2×A100）的
型號資訊在進入 solver 前就遺失。需求方已定案三個前提：(1) demand 一律指名
型號、不需要 "any GPU" 萬用需求；(2) `gpu_count` 直接汰換、不留雙軌；(3)
型號名稱由 scheduler 正規化，solver 只驗格式。

## 2. 考慮過的方案

**A. 逐型號獨立維度（採用）**：`gpu: dict[str,int]`，每個型號展開成一個
`gpu:<model>` 資源維度，C2 對每個維度各建一條線性約束。
優點：因為 demand 指名型號，不需要任何新變數——模型形狀不變，只是維度變多。
缺點：打破全 codebase 的 `getattr(res, field) -> int` 慣例（約 24 處迴圈）。

**B. 分配變數（allocation vars）**：demand 寫「N 顆任意 GPU」，由 CP-SAT 決定
每顆由哪個型號供應。被排除：前提 (1) 說 demand 一律指名型號，這組變數沒有
需求支撐，卻讓每條 C2 從純線性變成帶中介變數的結構，模型變大、可解釋性變差。
YAGNI——未來真要 wildcard 再加，本 ADR 的維度抽象不會擋路。

**C. scheduler 端過濾（維持現狀）**：靠 `candidate_baremetals` 把 h200 VM 限制
在 h200 機器上。被排除：混插機無法表達（`gpu_count` 會把兩種型號加總）、
solver 無法對 GPU 做 accounting（headroom / slot score / floors 全盲）。

**D. `getattr` 慣例的去留**——每個使用點各加一層 GPU 內迴圈 vs 統一抽象。
被排除（前者）：24 處重複迭代邏輯，solver C2 與 diagnostics shadow C2 幾乎
必然漂移，違反本 repo「共用邏輯抽 module-level 函式」的鐵律（explainability.md）。

## 3. 最終決策

維度變成字串：scalar 欄位名或 `gpu:<model>`。`app/models.py` 提供單一事實來源
`resource_dims(resources) -> list[str]`（scalar + 出現過的型號聯集、sorted 保證
確定性）與 `res_get(r, dim) -> int`（缺 key ≡ 0）。兩份 `RESOURCE_FIELDS` 常數
**刪除不留 alias**——漏改處在 import 時就炸，是 breaking change 想要的失敗模式。
`gpu_count` 在 payload 中出現 → 422 明確拒絕，不是 `extra="ignore"` 靜默吞掉：
未遷移的 scheduler 的 GPU demand 被無聲丟棄，比一次 422 危險得多（契約原則：
input violation → INPUT_ERROR，永不 silent fix）。

## 4. 實作走讀

- `models.py:60-99`（`Resources`）：`_validate_gpu` 剔除 0 值（`{}` 是唯一
  canonical 的「無 GPU」，`__eq__` 才可靠）但**保留負值**——`__sub__` 的
  key-union 差集是 pinned 正規化的輸入，`used − pinned_demand` 在某型號上為負
  正是 INPUT_ERROR 的偵測訊號，validator 加 `ge=0` 會把 bug 藏起來。
- `solver.py:96-113`（`request_dims`）：維度聯集必須涵蓋 BM 容量 ∪ VM demand ∪
  `vm_specs`——若只看容量，demand 指名但無機器供應的型號會讀成 0 而非構成
  約束。module-level 與 diagnostics 共用（Step C 的 C2 與 shadow C2 迭代同一份
  清單，failing-layer 歸因才不會說謊）。
- `solver.py:930-942`（C2, Step C）：`Σ demand_d·assign ≤ capacity_d` 對每個
  `dim ∈ self.dims` 各一條。缺型號的安全性其實由 Step A 保證：`fits_in` 對
  `capacity.gpu.get(m, 0)` 比較，h200 VM 在無 h200 的 BM 上直接不建 assign 變數
  ——eligibility 以資料（不建變數）表達比約束表達傳播更快（anti-pattern #6）。
- `solver.py:1395-1420`（slot score, Step C）：`remaining` 的下界
  `total−used−max_new_d` 中，`max_new_d` 只加總 **eligible** VM；eligibility 保證
  無人對 BM 缺的型號有需求，所以 GPU 維度退化為全 0，不會出現「下界排除模型
  可達值 → 假 INFEASIBLE」（anti-pattern #4）。headroom 的 `total_d == 0` skip
  （`solver.py:1300`）自動變成「BM 沒載的型號跳過」。
- `splitter.py:277-291`（coverage, Step C 共用模型）：`Σ count_s·spec_s[d] ≥
  total[d]` 逐型號成立——CPU-only spec 在 `gpu:h200` 維度上係數為 0，無法覆蓋
  GPU 需求；沒有任何 gpu spec 時走既有 infeasible 路徑，絕不靜默降級。

## 5. 取捨與風險

- **Wire contract 斷裂是刻意的**：Go scheduler 必須同版遷移；422 訊息附遷移
  提示。`config_fingerprint` 因 JSON 形狀改變全面換值，reconcile 會對舊 plan
  報 config drift——那確實是 drift，屬正確行為。
- **維度數 = 3 + |型號|**：headroom/slot score 的變數量隨型號數成長。現實
  fleet 型號數少（個位數），風險低；若未來單一 request 出現數十型號，slot
  score 是先出問題的地方（每 BM × 每 t-shirt × 每維度的除法變數）。
- **未竟事項（非目標）**：`DemandEntry`（capacity-planning 需求輸入）尚無 GPU
  欄位；procurement balance 與 reconcile drift 敘事維持 CPU-only。需要 GPU
  採購規劃時是下一個 ADR。

## 6. 你應該帶走的知識

- **需求語意決定模型複雜度**：「demand 指名型號」讓 GPU 型號只是「更多維度」
  而非「分配問題」——先釘死語意再建模，能省掉整類變數。
- **拒絕比忽略安全**：對移除的欄位，`extra="ignore"` 是把 bug 推遲到生產環境
  的靜默資料遺失；422 + 遷移訊息把成本付在整合當下。
- **負值可以是訊號**：不要反射性地給資料模型加 `ge=0`——`used − pinned` 的
  per-model 負值正是 inventory 不一致的偵測手段，clamp 掉它就是掩蓋 bug。

## 7. 驗證方式

- `tests/test_models.py`（新檔）：dict 算術、負值保留、`gpu_count` 拒絕。
- `tests/test_solver.py::TestGpuModelCapacity`（混插機逐型號記帳、型號不可互換、
  bound 冒煙）與 `TestGpuPinnedValidation`（per-model 負值/超賣 → INPUT_ERROR）。
- `tests/test_splitter.py::TestGpuSplit`（逐型號 coverage）。
- 端對端：`make cli INPUT=examples/gpu_models.json`——混插 BM 上 h200/a100 分開
  記帳、h200 VM 只落在有 h200 的機器、預設 3-AG 分散同時成立。
