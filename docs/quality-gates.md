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
- **Ubuntu 不要用 snap 版 Chromium**:`apt install chromium-browser` 裝到的是 snap 轉接套件,
  一般帳號在受管機器上常被禁止執行 snap(錯誤訊息:`cannot run snap applications on this
  system`;Playwright 端只會看到 `Target page, context or browser has been closed`),
  帶 `sudo` 才跑得動 —— 但 Stop hook 不會帶 sudo。改裝 Google Chrome 的 `.deb`,
  並設 `SOLVER_CHROMIUM=/usr/bin/google-chrome`。
- 瀏覽器起不來時,先用一般帳號手動啟動,它會直接說出原因:
  `$SOLVER_CHROMIUM --headless --no-sandbox --disable-gpu --dump-dom about:blank`
- **不要用 `sudo` 跑測試**:會留下 root 擁有的 `.pytest_cache` / `__pycache__`,之後一般帳號的
  `make test` 會出權限錯誤(`find . -user root -not -path "./.git/*"` 檢查)。
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

關卡「存在」不等於「有在擋」。搬過去後依序做一次,每一步都寫了預期結果;
結果不符就代表那一關在內部沒有生效。

### A. 基準:環境本身是對的

- [ ] `.claude/` 有跟著進來,且 hook 可執行:`ls -l .claude/hooks/stop-gate.sh`(要有 `x`)
- [ ] `make install && make test` → 全綠
- [ ] `.venv/bin/python -m pytest tests/test_ui_js.py -v` → 是 **PASSED 不是 SKIPPED**(有 Node ≥ 18)
- [ ] `make install-ui && SOLVER_CHROMIUM=<路徑> make test-ui` → 7 passed
- [ ] OR-Tools 版本與 `uv.lock` 一致:`.venv/bin/python -c "import ortools; print(ortools.__version__)"`
      (`make install` 不是照 lock 安裝;版本不同時求解結果可能不同,`config_fingerprint` 也會變)
- [ ] `git symbolic-ref refs/remotes/origin/HEAD` → 指向 `origin/master`(Stop hook 靠它找主幹;
      沒有輸出就執行 `git remote set-head origin -a`)

### B. 破壞測試:每一關都真的會擋

每項做完用 `git checkout -- <檔案>` 還原。

| # | 故意弄壞 | 執行 | 預期 |
|---|---|---|---|
| 1 | `app/solver.py` 的 `_add_max_per_bm_constraints` 開頭加一行 `return` | `make test` | 失敗,訊息含 `[C4]`(獨立驗證器抓到) |
| 2 | `app/web_static/js/util.js` 的 `formatGpu` 把 `:` 改成 `=` | `make test-js` | 失敗 |
| 3 | `app/web_static/js/summary.js` 最後加 `import "./nope.js";` | `make test-ui` | 失敗,訊息含 `HTTP 404` |
| 4 | `app/web_static/index.html` 加 `<script src="https://example.com/x.js"></script>` | `make test-ui` | 失敗,訊息含 `off-site request` |
| 5 | 維持 #3 的狀態 | `echo '{}' \| CLAUDE_PROJECT_DIR=$PWD .claude/hooks/stop-gate.sh; echo $?` | 印出 `BLOCKED`,結束碼 `2` |
| 6 | `app/solver.py` 只加一行註解(不寫 ADR) | 同 #5 的指令 | `BLOCKED … no ADR was added`,結束碼 `2` |

#5 若結束碼是 `0`:多半是瀏覽器測試被 skip 了 —— 確認 `SOLVER_CHROMIUM` 有設。

### C. Claude CLI 端

- [ ] 在 `.claude/settings.local.json` 設 `"env": {"SOLVER_CHROMIUM": "<路徑>"}`
- [ ] 維持 B-3 的破壞,請 Claude 做任何小事後結束回合 → 應該被 Stop hook 擋下並開始修。
      沒被擋 = `env` 沒傳進 hook,改在 shell profile `export SOLVER_CHROMIUM=…`
- [ ] 在 CLI 輸入 `/` 看得到 `adr`、`cpsat-anti-patterns`、`teach-back`、`verify-solver` 四個 skill
- [ ] 問 Claude「改約束語意時要改哪些檔案」→ 應回答 solver 與 `app/verify.py` 都要改(確認 CLAUDE.md 有被讀到)

### D. GitLab 端

- [ ] `.gitlab-ci.yml` 加上 `include`,`test` stage 排在 deploy 之前
- [ ] 設定 `SOLVER_CI_PYTHON_IMAGE` / `SOLVER_CI_NODE_IMAGE` 指到內部 registry
- [ ] 專案設定:`master` 設為 protected;Merge requests 勾選 *Pipelines must succeed*
- [ ] **CI 破壞測試**:開一個帶 B-1 破壞的分支並發 MR → `solver-quality:python` 變紅、
      MR 無法 merge、**deploy job 沒有執行**。確認後關掉 MR、刪分支
- [ ] (建議)啟用 `.solver-quality:ui-browser`,並用帶 B-3 破壞的分支確認它會變紅

### E. 之後定期

- 每次升級 OR-Tools、Node 或 Chromium 後,重跑 A。
- 每新增一條約束(C8+),B-1 的做法對新 builder 再做一次。
