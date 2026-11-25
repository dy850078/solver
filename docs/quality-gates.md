# 品質關卡(Quality Gates)

這份文件回答一個問題:**不管是誰、用哪個模型寫的 code,進主幹前一定會被什麼擋住?**

設計原則是把品質放在 repo 裡會自動執行的檢查上,而不是放在「寫的人(或模型)夠小心」上。
所有關卡都跟著 repo 走,不依賴 GitHub 或任何外部服務。

## 總覽

| 關卡 | 保護什麼 | 怎麼跑 | 需要 |
|---|---|---|---|
| pytest 全套 | solver / splitter / planner / API | `make test` | Python |
| 獨立驗證器 | 「solver 說成功」的結果真的滿足 C1–C7 | 內建在 `make test`;單檔用 `make verify INPUT=…` | Python |
| JS 單元測試 | UI 的解析/格式化邏輯(xlsx、CSV、labels、GPU) | `make test-js`(`make test` 也會帶到) | Node ≥ 18 |
| 工作簿交叉檢查 | UI 寫出的 .xlsx 與讀回的 request,由 Python 端判定 | `make test` | Node + Python |
| 瀏覽器冒煙測試 | 每個頁面能開、能求解、無 console error、無對外連線 | `make test-ui` | Playwright + Chromium |
| Stop hook | Claude 結束回合前:測試要過、核心變更要有 ADR | 自動 | Claude Code |
| GitLab CI | 以上全部,在 merge 前、deploy 前 | `ci/quality.gitlab-ci.yml` | runner |

## 1. 獨立驗證器(`app/verify.py`)

**問題**:原本的測試大多是「問 solver,solver 對不對」。如果 constraint builder 寫錯
(漏了一個 bucket、cap 算錯、少建一個變數),模型會接受一個違規的 layout,
然後理直氣壯地回 `OPTIMAL`。

**做法**:驗證器拿 `(request, result)`,不經過 CP-SAT,用純計數重新檢查 C1–C7。
它**刻意不 import `app.solver`、不重用 solver 的規則展開函式** —— 分組、auto 規則、
cap、pinned 的 grandfathering 全部依文件語意重新推導一次。

> 這是本專案「single source of truth」唯一被允許的例外。
> **考慮過的替代方案**:讓驗證器重用 `SolverConfig.auto_group_key` 等共用函式。
> 好處是不重複;但共用的函式若有 bug,solver 和驗證器會錯得一模一樣,檢查就失去意義。
> 兩份必須一致的獨立實作(N-version)比一份自己檢查自己的實作有價值,所以選擇重複。

**涵蓋方式**:`tests/conftest.py` 有一個 autouse fixture,把測試期間**每一次**成功的
`VMPlacementSolver.solve()` 都送進驗證器 —— placement、split-and-solve、rollout、sizing、
capacity planning、bm-demand 全部包含。既有的 500 多個測試因此全部多了一層檢查,
只 assert `success` 的測試也不例外。

**驗證器自己對不對?** `tests/test_verify.py` 用兩種方式確認它不是永遠回 `[]`:

- *竄改結果*:拿合法的 layout 手動弄壞(搬到非 candidate、塞爆 BM、擠進同一個 AG…),
  驗證器必須指出正確的約束編號。
- *破壞 solver*:把某一條約束的 builder 換成空函式,solver 仍然回報成功,
  autouse fixture 必須讓測試失敗。C2–C7 各一個案例。

**不涵蓋**:目標函數品質(合法但浪費的 layout 會通過)、splitter 的預算覆蓋、
capacity planner 的採購上限。

**維護規則**:改約束語意或新增 C8+ 時,solver 和驗證器**都要改**,並在
`tests/test_verify.py` 補上竄改案例與破壞案例。兩者不一致時,那就是要回報的發現,
不要用 `@pytest.mark.no_verify` 蓋掉。

## 2. UI 測試

UI 沒有第三方 JS、沒有 build step,所以測試也不引入 npm 依賴。

- **`tests/js/*.test.mjs`** —— Node 內建的 `node --test` + `node:assert`。
  涵蓋不碰 DOM 的模組:`util.js`、`demand-xlsx.js`、`demand-csv.js`、`stock-csv.js`。
  選 `node --test` 而不是 Jest/Vitest,是因為後者需要 `npm install` 幾百個套件,
  封閉網路裝不了,也違反「不引入第三方 JS」的精神。
- **`tests/test_ui_js.py`** —— 從 pytest 啟動上面那組 Node 測試,並做交叉檢查:
  用 Python 的 `zipfile` + `ElementTree` 讀 `xlsx.js` 寫出的位元組(CRC、XML、列數),
  用 Pydantic model 比對 `demand-xlsx.js` 讀回的 request。JS 自己 round-trip
  可以「一致地錯」,交給不共用程式碼的另一方判定才算數。
- **`tests/test_ui_smoke.py`** —— 真的瀏覽器對真的 server:五個頁面開機、
  主頁貼範例按 Run、xlsx 在瀏覽器原生 `DecompressionStream`/`DOMParser` 上 round-trip。
  任何 console error、失敗的請求、**離站請求**(違反 no-CDN 規則)都會讓測試失敗。

新增 UI 功能時:邏輯放在不碰 DOM 的模組並寫 Node 測試;頁面接線靠冒煙測試。

### 在封閉網路跑瀏覽器冒煙測試

需要兩樣東西:Playwright 的 Python 套件,以及一個 Chromium 執行檔。

```bash
make install-ui                                # 從內部 PyPI 安裝 playwright(ui extra)
export SOLVER_CHROMIUM=/usr/bin/chromium       # 指向任何 chrome / chromium 執行檔
make test-ui
```

- `SOLVER_CHROMIUM` 是封閉網路的正規路線:用系統套件(`chromium`、`google-chrome`)
  或內部 artifact 上的執行檔即可,**不會下載任何東西**。
- 能連外的機器可以改用 `.venv/bin/playwright install chromium`(從 Playwright CDN 下載),
  這時不必設 `SOLVER_CHROMIUM`。
- 內部 PyPI 沒有 `playwright` 的話,這一關就先不跑 —— `make test` 會顯示 skip,
  其餘關卡不受影響。

### Skip 與 Require

沒有 Node 或瀏覽器時,對應的測試會 **skip**,讓只有 Python 的機器仍能跑 `make test`。
但 skip 在 CI 是危險的(工具不見了卻一路綠燈),所以:

| 環境變數 | 效果 |
|---|---|
| `SOLVER_REQUIRE_NODE=1` | 找不到 `node` → 失敗而非 skip |
| `SOLVER_REQUIRE_BROWSER=1` | 沒有 playwright 或開不了瀏覽器 → 失敗而非 skip |
| `SOLVER_CHROMIUM=/path` | 指定瀏覽器執行檔 |

## 3. GitLab CI

`ci/quality.gitlab-ci.yml` 是一個 **include 檔**,不是 pipeline 本身。它不定義
`stages`、`workflow`、`default` 或任何 deploy job,job 名稱一律以 `solver-quality:` 開頭,
因此不會和既有的 `.gitlab-ci.yml`(部署用)衝突。

在既有的 `.gitlab-ci.yml` 加一行:

```yaml
include:
  - local: ci/quality.gitlab-ci.yml
```

接上之後請確認:

1. **`stages:` 裡有 `test`,而且排在 deploy 的 stage 之前。** 這樣關卡失敗時 pipeline
   會停在部署之前。(既有檔案若沒有 `stages:`,GitLab 預設順序已經是這樣。)
2. **Image**:預設是 `python:3.13` 與 `node:22`。封閉網路請用 CI/CD 變數
   `SOLVER_CI_PYTHON_IMAGE`、`SOLVER_CI_NODE_IMAGE` 指到內部 registry。
3. **MR 也要擋**(建議):include 檔本身不設 `rules`,job 只會出現在既有設定已經會產生的
   pipeline 裡。如果目前只有 merge 進 `master` 才有 pipeline,那關卡是在「部署前」才擋;
   要在 MR 階段就擋,請依 include 檔開頭註解覆寫 `.solver-quality` 的 `rules`。
4. **瀏覽器那一關**是隱藏範本 `.solver-quality:ui-browser`,需要一個同時有
   python、node、chromium 的 image,由你在既有檔案中啟用(寫法見 include 檔內註解)。

> 這個 include 檔在撰寫時**沒有在真正的 GitLab runner 上執行過**(開發環境沒有 GitLab)。
> YAML 語法與每個 job 的指令都在本機驗證過,但第一次接上時請看一次 pipeline 結果。

## 4. 搬到內部之後的檢查清單

- [ ] `.gitlab-ci.yml` 加上 `include`,確認 `test` stage 在 deploy 之前
- [ ] 設定 `SOLVER_CI_PYTHON_IMAGE` / `SOLVER_CI_NODE_IMAGE`
- [ ] 開發機上 `make install && make test` 全綠,且 `tests/test_ui_js.py` 不是 skip(有 Node)
- [ ] 開發機上 `make install-ui && SOLVER_CHROMIUM=… make test-ui` 全綠
- [ ] GitLab 專案設定:`master` 設為 protected,merge 需 pipeline 成功
- [ ] `.claude/` 目錄(settings、hooks、skills)有跟著 repo 進到內部
