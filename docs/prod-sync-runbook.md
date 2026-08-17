# 生產環境同步 Runbook

把「每次手動複製檔案」換成「一條可驗證的 git 流水線」的完整流程。

## 適用情境

生產環境位於封閉網路,code 需經 GitLab Pipeline 做 security scan 後才能落地,
且生產 repo 已綁定 deploy 設定而不可替換。歷史上生產環境的更新靠人工逐檔複製,
導致:

1. 無法確知生產環境與 upstream 的實際差異（delta 不可見）
2. upstream 若改到被「跳過複製」的檔案,該更新會被靜默遺漏
3. 每次更新都是高風險的手工作業

本 runbook 的目標是把這三件事收斂成一條指令:`./scripts/sync-from-upstream.sh`。

## 架構

```
GitHub: dy850078/solver
  └─ 開發: claude/*  ──PR──> release-to-gitlab  (default branch, 已確認)
                                    │
                          GitLab Pipeline: clone + security scan
                                    ↓
                         GitLab mirror repo (release-to-gitlab)
                                    │
                          Phase 3 例行同步 (sync/* 分支 + MR)
                                    ↓
                         GitLab 生產 repo (master)   ← deploy 綁定在此,不可換 repo
                                    │
                          merge 進 master 觸發 deploy pipeline
                                    ↓
                               生產機器
```

**部署模型**:生產機器不做 `git pull`。
**MR merge 進 master 的那一刻就是 deploy**,pipeline 會自己把 code 送上去。
所以 push 前的驗證是上線前最後一道關卡 —— 這也是 `scripts/sync-from-upstream.sh`
把所有檢查做完、卻刻意不幫你 push 的原因。

**已確認的前提**
- 生產 repo 與 mirror **無共同歷史** → 需要 Phase 2 的一次性移植
- 生產 repo 的 deploy 綁定不可動 → 全程在舊 repo 內操作,不 fork、不換 repo
- 生產環境的本地改動已 commit/push

---

## 前置檢查（一次性，5 分鐘）

- [ ] **確認 GitLab Pipeline 抓的是 `release-to-gitlab`**
      換 default branch 後最容易漏的一項。若 pipeline 還設定成抓 `main`,
      它會一直送舊 code 進 mirror,後面全部白做。
- [ ] 確認 mirror repo 裡有 `release-to-gitlab` 這條 branch 且是最新的
- [ ] 準備一台碰得到 GitLab 的工作機（不是生產機器）

---

## Phase 0 — 盤點 delta（你做，約 30 分鐘）

目的:在不解開幾百個 commit 的前提下,把「我到底改了什麼」變成一張清單。
成本上限是你動過的檔案數（通常十來個）,與 commit 數無關。

### 0.1 準備工作區

```bash
git clone https://gitlab.internal/.../solver-prod.git
cd solver-prod
git remote add -t release-to-gitlab mirror https://gitlab.internal/.../solver-mirror.git
git fetch mirror
```

### 0.2 第一層：檔案清單分類（機械化）

```bash
git diff --name-status mirror/release-to-gitlab master | sort > delta-files.txt
cat delta-files.txt
```

| 標記 | 意義 | 處置 |
|---|---|---|
| `A` | 只存在你的 master = **你新增的檔案** | **A 類**:整檔都是 delta,照單全收,不用分析 |
| `D` | 只存在 mirror = upstream 新增、你還沒有 | 純 upstream 進展,**忽略** |
| `M` | 兩邊都有但內容不同 | 混合嫌疑 → 進第二層 |

### 0.3 第二層：用自己的 commit 歷史當嫌疑人名單

```bash
git log --name-only --format="" | sort -u > touched-files.txt
```

拿 `M` 清單跟這份對照:
- `M ∩ touched-files` → **真嫌疑**,進第三層
- `M ∖ touched-files` → 幾乎必然只是「上次複製後 upstream 又前進了」,抽查一兩個確認後整批忽略

### 0.4 第三層：真嫌疑逐檔看 diff

```bash
git diff mirror/release-to-gitlab master -- <嫌疑檔案>
```

你的修改（內部 hostname、port、憑證路徑、proxy、index-url…）
與 upstream 演進在 diff 裡通常一眼可辨。

> **`+` 是你的、`-` 是 upstream 的**。`git diff A B` 裡 `+` 標記的是
> B（你的生產分支）獨有的內容。看反方向會得出「已經對齊」的錯誤結論。

### 0.4a 結構化交叉檢查（diff 太長無法逐行讀時）

長 diff 靠人眼描述必漏。用這幾條指令產生**短清單**兩邊對照,
特別是對外契約（endpoint、環境變數),漏掉會直接造成生產故障:

```bash
# 對外 endpoint —— 漏一個就是 404,probe/呼叫端會炸
for ref in mirror/release-to-gitlab master; do
  echo "--- $ref"
  git show $ref:app/server.py | grep -oE '@api\.(get|post|put|delete)\("[^"]+"' | sort
done

# 讀取的環境變數 —— 決定部署時要設什麼
for ref in mirror/release-to-gitlab master; do
  echo "--- $ref"
  git show $ref:app/server.py | grep -oE 'environ(\.get)?\[?"[A-Z_]+"' | sort -u
done

# 掛載點與 import
for ref in mirror/release-to-gitlab master; do
  echo "--- $ref"
  git show $ref:app/server.py | grep -nE '^(import|from) |\.mount\('
done
```

兩份輸出各十來行,肉眼 diff 即可,不需要複製貼上整份 patch。

### 0.5 產出（交給 Claude）

1. `delta-files.txt` 內容（檔名通常不敏感）
2. 真嫌疑檔案的 diff，**敏感值遮成 `<INTERNAL_HOST>` 之類的佔位符**
3. A 類檔案清單 + 各自用途一句話

> **關鍵區分**
> - **A 類（你新增的檔案）**:upstream 不碰 → merge 幾乎不 conflict
>   → **不繳同步稅**,不急著 externalize,Phase 2 疊一個 commit 即可
>   ⚠️ 「A 類」是以**檔案路徑**為單位,不是以目錄為單位。
>   `examples/mock/` 這種**兩邊都有的目錄**,只要你新增的檔名有天跟 upstream
>   新增的撞在一起,就會是 add/add conflict → 見 3.4 的命名紀律
> - **M 類（你改的共用檔案）**:每次同步會撞 → 這才是 externalize 的對象

---

## Phase 1 — Externalize（Claude 做）

目的:讓 mirror 裡的 code **原生支援你的環境**,使 Phase 2 變成「零 delta 移植」。

1. 你交出 Phase 0.5 的產出
2. Claude 提實作 plan 給你過目（動 `server.py` 屬核心變更,照專案慣例先 plan）
3. 實作:M 類差異全部改成**環境變數 / `.env` 驅動**,預設值 = 現行公開行為
4. 測試 + ADR → PR → merge 進 `release-to-gitlab`
5. Pipeline 掃描 → 流進 mirror

**完成標準**:生產環境只要設好環境變數,就能直接跑 mirror 裡的 code,
不需要修改任何被 git 追蹤的檔案。

**環境值的去處**（git 不追蹤,pull 永遠不會碰）:
- systemd unit / container env / shell profile 的環境變數
- 或一個 `.gitignore` 掉的 `.env`
- pip index / proxy → `PIP_INDEX_URL` / `UV_INDEX_URL` 環境變數或 `pip.conf`
  （本來就不該在 pyproject.toml）

> **注意 pyproject 裡「沒被 import 卻存在」的依賴**:`pandas==2.3.3` 沒有任何
> 程式碼 import,但它釘的是 `ortools` 拉進來的**傳遞依賴**。這類項目 grep
> 不到使用點,卻不是死重量 —— 刪掉會讓解析器改抓 pandas 3.x。判斷一個依賴
> 該不該留,要看它有沒有在**約束別人**,不能只看有沒有被 import。
- 本機專屬的 git ignore 規則 → `.git/info/exclude`（git 不追蹤,永不衝突）

### 已完成的 externalize（本專案現況）

| 原本的本地修改 | 現在的做法 | 對應 |
|---|---|---|
| Makefile `PYTHON ?= python3.13` → 3.12 | `export PYTHON=python3.12` | `?=` 本來就吃環境變數 |
| pyproject `requires-python >=3.13` | 已放寬為 `>=3.12` | codebase 無 3.13-only 語法 |
| 移除 `swagger_ui_bundle` import、自建 `/static` 掛載 | `SWAGGER_STATIC_DIR` 指向 vendored 目錄 | 套件改為 optional dependency |
| `/health` 回 `{"status":"ok"}` | upstream 已對齊此格式 | probe 設定在 repo 外,回傳格式是契約 |
| `.gitignore` 的 `*_cache` | upstream 已補 cache 規則 | pytest/mypy/ruff |
| `.gitignore` 的 `benchmark_*` | `.git/info/exclude` | 本機產物,git 不追蹤 |

> **不要保留生產版的 `.gitignore`** —— 它只有 4 行,沒有忽略 `.venv/`、
> `.env`、`output/`、`*.log`。尤其 `.env` 正是本階段要用來放環境值的檔案,
> 沒忽略等於把設定(可能含憑證)推上 repo 的風險。upstream 版本嚴格更完整。

---

## Phase 2 — 移植手術（你做，一次性）

在**舊生產 repo 內部**把 mirror 的血脈引進來。repo URL、名稱、deploy 綁定全程不變。

### 2.0 審查:決定每個差異要採用哪一邊

```bash
./scripts/compare-with-upstream.sh          # 預設 mirror/release-to-gitlab vs master
```

報告分三區:**只在生產端**（必須手動帶過去）、**只在 upstream**（採用即可）、
**兩邊都有但不同**（逐項決定),外加 endpoint 與環境變數的對外契約交叉檢查。

逐項看差異:

```bash
git diff mirror/release-to-gitlab master -- <檔案>
```

把決定記成一張清單,只需要列「**要保留生產版**」的那些:

```
保留生產版: <檔案A>  # 原因
保留生產版: <檔案B>  # 原因
其餘一律採用 upstream
```

> **為什麼預設是採用 upstream**:兩種基底的失敗方向相反。以 upstream 為基底時,
> 漏掉一項客製 → 測試/煙測會抓到;以生產為基底時,漏掉一項 upstream 更新 →
> **靜默過時**,而且往後每次同步都會再發生一次 —— 那正是本 runbook 要根治的病。
> 你仍然對每個檔案有完全的決定權,只是「保留生產版」必須明講並留下原因。

### 2.1 舊歷史存檔（保險）

```bash
# 帶日期的名稱比 master-legacy 好:語意明確,且多次移植不會撞名
git branch retain-master-before-upstream/$(date +%Y-%m-%d) master
git push origin retain-master-before-upstream/$(date +%Y-%m-%d)
```

### 2.2 建立新血脈分支

```bash
git fetch mirror
git switch -c adopt-upstream mirror/release-to-gitlab
```

此時工作目錄 = 純淨的最新 upstream code。

### 2.3 疊上決定保留生產版的檔案

```bash
# 2.0 清單裡「保留生產版」的每一個檔案(含 A 類生產專屬檔案)
git checkout retain-master-before-upstream/<日期> -- <檔案A> <檔案B> ...
git add -A
git commit -m "prod: local-only files and retained customizations"
```

commit message 裡寫下每個檔案保留的原因 —— 下一次同步時,
那就是「這個檔案為什麼不跟 upstream 走」的唯一權威記錄。

> M 類不用疊 —— Phase 1 已經讓它們變成環境變數了。
> 若 Phase 1 尚未完成而你想先跑通流程,可暫時 `git apply` M 類 patch,
> 但那樣就會回到「有 delta 要繳稅」的狀態。**建議等 Phase 1 完成再做 Phase 2。**

### 2.4 驗證（關鍵，別跳過）

```bash
git push origin adopt-upstream        # 先推分支,master 完全沒被碰
```

**先更新 venv。** `.venv/` 被 gitignore,切換分支不會動它 —— 移植後裡面裝的
還是舊 code 的依賴。`make install` 不會重建 venv（`.venv/bin/python` 已存在,
該 target 直接跳過）,只會把套件裝進去:

```bash
export PYTHON=python3.12              # Makefile 的 ?= 會讓環境變數勝出
# 封閉網路的內部索引 —— pip 與 uv 讀的是「不同」的變數,兩個都設:
export PIP_INDEX_URL=https://<內部索引>/simple    # pip 用
export UV_INDEX_URL=https://<內部索引>/simple     # uv 用(新版亦可用 UV_DEFAULT_INDEX)
make install
```

> **venv 是 uv 建的?** `uv venv` 預設不把 pip 裝進 venv（uv 自己管套件),
> 所以舊版 Makefile 的 `python -m pip` 會噴 `No module named pip`。
> 現在 Makefile 會自動偵測:uv 在 PATH 就用 uv,否則用 venv+pip;
> 兩者皆無時給出明確指示而不是難懂的 traceback。
>
> **⚠️ uv 不讀 `PIP_INDEX_URL`。** 實測:只設 `PIP_INDEX_URL` 時 uv 會**靜默
> 改用公開 PyPI** —— 在封閉網路等於繞過安全邊界拉套件(或連不出去而失敗,
> 錯誤訊息還指不到真正原因)。`make install` 偵測到「有設 PIP_INDEX_URL 卻沒設
> UV_INDEX_URL」時會出警告,但**設對兩個變數才是解法**。

**驗證分兩層**,能跑多少跑多少:

| 層級 | 指令 | 需要 | 驗什麼 |
|---|---|---|---|
| 邏輯正確性 | `make test` | dev extras（pytest、httpx） | code 本身 —— GitHub CI 已驗過,這裡是複驗 |
| **環境相容性** | CLI + 煙測 | 只要 runtime deps | **這份 code 在這個環境能不能跑** ← 生產端真正該驗的 |

```bash
make cli INPUT=examples/success_basic.json

export SWAGGER_STATIC_DIR=/path/to/vendored/swagger
make run &
curl -s localhost:50051/health      # 必須是 {"status":"ok"} —— probe 契約
curl -s localhost:50051/openapi.json | head -c 100
curl -s -X POST localhost:50051/v1/placement/solve \
     -H 'Content-Type: application/json' \
     -d @examples/success_basic.json | head -c 200
```

> 內部索引若沒有 `pytest` / `httpx`,生產端就跳過 `make test`,改在工作機上跑
> 全套;生產端只做環境相容性那一層。**別為了跑測試把 dev 套件塞進生產環境。**

驗證不過 → 修分支或直接棄用,**master 從頭到尾沒動過,生產零風險**。

### 2.5 切換 master（唯一需要 force 的一步）

```bash
# GitLab UI: Settings → Repository → Protected branches → 暫時允許 force push
git push origin adopt-upstream:master --force-with-lease
# → 立刻把保護加回去
```

- repo URL / branch 名稱不變 → **deploy 綁定完全無感**
- 用 force 而非 MR:兩條歷史無血緣,MR 會產生把兩個 root 縫起來的怪異
  merge,衝突面是整個 repo。一次性 force 切換更乾淨誠實。

### 2.6 生產機器對齊（一次性）

```bash
# 在生產機器上,因為歷史被改寫,做一次 reset
git fetch origin
git reset --hard origin/master
```

> 這是**唯一一次**需要 reset。之後 master 只前進不改寫,每次同步都走
> `sync/*` 分支 + MR,工作區用 `git fetch && git switch master && git merge --ff-only`
> 跟上即可。
> 執行前確認生產機器上沒有未 commit 的重要東西（Phase 0 已經盤點過就安全）。
> `.env`、`.venv/` 等未追蹤檔案不受 reset 影響。

**過程中會看到的兩個正常訊息**（都不是出錯):

1. 在 `adopt-upstream` 上 `git status` 顯示
   `ahead of 'mirror/release-to-gitlab' by N commits` ——
   分支是從 mirror ref 建的,tracking 就設在那裡。**不代表 commit 進了 mirror**;
   commit 去哪由 `git push` 的目標決定,不是 tracking。可用
   `git branch -u origin/master adopt-upstream` 消掉,或直接切回 master。
2. `git switch master` 顯示 `have diverged, and have N and M different commits` ——
   本機 master 還停在舊血脈,遠端已是新的。這正是 2.6 要解決的事,
   **不要照 git 的建議跑 `git pull`**（會嘗試合併兩條無關歷史),用上面的 reset。

### 2.7 觀察期

舊分支 `retain-master-before-upstream/<日期>` 保留數週當 rollback 退路,確認穩定後再考慮刪除。

---

## Phase 3 — 例行同步（每次釋出）

### 3.1 GitHub 側（開發）

feature branch 開發完 → PR（自動 target `release-to-gitlab`）→ review → merge
→ Pipeline 掃描 → 流進 mirror

### 3.2 GitLab 側（工作機，不是生產機器）

```bash
cd solver-prod
./scripts/sync-from-upstream.sh
```

這支 script 把 3.2 的手動步驟包起來,並且**永遠不會 push**。
它做的事,依序:

| 階段 | 檢查 | 失敗代表 |
|---|---|---|
| Preflight | `mirror` remote 存在、tracked 檔案沒有未 commit 變更、分支名沒被佔用、目前在哪個 branch | 環境沒準備好,先修 |
| Fetch | 兩邊 remote 都 fetch;`mirror` 沒有新東西就直接結束 | pipeline 可能還沒跑 |
| | 列出 incoming commits ——「**這就是 MR 會 deploy 的東西**」 | |
| Merge | 從 `origin/master` 開 `sync/YYYY-MM-DD`,merge mirror | conflict → upstream 動到你也客製的檔案 |
| Sync log | 在根路徑的 `SYNC_LOG.md` append 一筆「時間戳 + upstream/base SHA + commit 清單」並 commit | 見下方說明 |
| Install | `make install`（並在 `PIP_INDEX_URL` 有設但 `UV_INDEX_URL` 沒設時警告） | 內網 index 沒吃到 |
| Test | `make test` | |
| CLI | `make cli` 等效,斷言 `solver_status` 是 OPTIMAL/FEASIBLE | 只跑不夠,要看結果 |
| Smoke | 真的起 server,斷言 `/health` 回 `{"status":"ok"}`、`POST /v1/placement/solve` 會解 | liveness probe 的契約 |
| Contract | 與 `origin/master` 比對 endpoint / 環境變數 / `pyproject.toml`+`uv.lock` 變動 | 這些設定在 repo 外面,要同步改 |

**任一檢查失敗 → 印出「Do not push」並把分支留在原地讓你查。**
全過 → 印出 `git push origin sync/YYYY-MM-DD` 讓**你自己決定**要不要送。

常用選項:

```bash
./scripts/sync-from-upstream.sh --no-smoke      # 不方便起 server 時
./scripts/sync-from-upstream.sh --keep-going    # 一次看完所有失敗,不要 fail-fast
./scripts/sync-from-upstream.sh --verify-only   # 解完 conflict 後,只重跑驗證
./scripts/sync-from-upstream.sh --no-log        # 不寫 SYNC_LOG.md（見下）
./scripts/sync-from-upstream.sh --branch sync/hotfix --mirror-ref mirror/other-branch
```

驗證通過後,**你自己** push 並在 GitLab 開 MR (`sync/*` → `master`)。

#### `SYNC_LOG.md` — 為什麼要有

deploy pipeline 是靠「根路徑有檔案變動」觸發的,但一次 sync 很可能只動到
`app/` 或 `tests/`,根路徑一個字都沒改 → **MR merge 了卻不會 deploy**。
script 因此在 merge 之後、驗證之前,固定往根目錄的 `SYNC_LOG.md` append 一筆:

```markdown
## 2026-08-18T09:12:03+0800 — sync/2026-08-18

- upstream: mirror/release-to-gitlab @ 0333ba2
- base:     origin/master @ c3e635d
- 3 commit(s):
  - 0333ba2 upstream: touch README
  - ...
```

一筆 = 一個 MR = 一次 deploy,所以它同時是**部署歷史**。

幾個行為上的細節:

- **不會 conflict**。upstream 從來沒有這個檔案,只有單側新增的檔案 git 會直接保留。
- **不會重複寫**。判斷依據是「這個分支的 `SYNC_LOG.md` 是否已異於 `$BASE_REF`」,
  所以 `--verify-only` 重跑幾次都只有一筆。
- **conflict 解完後才寫**。merge 失敗時 script 直接結束,那筆是在你
  `--verify-only` 時才補上的 —— 否則 MR 會缺少根路徑變動而不觸發 deploy。
- 檔名可用 `--log-file` 換,或 `--no-log` 完全關掉。

### 3.3 Deploy

MR merge 進 `master` → deploy pipeline 自動送上生產機器。
**生產機器不需要（也不應該）手動 `git pull`。**

`pyproject.toml` / `uv.lock` 有變動時要確認 deploy pipeline 有重裝依賴 ——
script 的 Contract 階段就是為了在 push 前先讓你看到這件事。

### 3.4 遇到 conflict 怎麼辦

script 在 merge 失敗時**直接結束、不會猜**,分支留在原地。
因為 merge = deploy,這裡解錯的東西會直接上生產,所以流程是
**辨型 → 決定誰是權威 → 解 → 重跑驗證**,不是憑印象按「接受目前變更」。

#### 步驟一:辨型

```bash
git status --short                      # 開頭是 UU / AA / DU / UD 的就是衝突
git diff --name-only --diff-filter=U    # 只列未解決的檔案
```

| 標記 | git 說法 | 意義 | 常見來源 |
|---|---|---|---|
| `AA` | add/add | 兩側**各自新增同一條路徑** | 你的 prod-only preset 跟 upstream 新檔撞名 |
| `UU` | both modified | 兩側都改了同一個共用檔案 | M 類,Phase 1 externalize 沒做完 |
| `DU` / `UD` | deleted by us / by them | 一側刪檔、另一側改了它 | upstream 重構搬檔案 |

#### 步驟二:先搞清楚 `--ours` 是誰

在 sync 分支上 merge mirror 時:

| 寫法 | 指的是 |
|---|---|
| `--ours` / `:2:` | **sync 分支 = 你的生產內容** |
| `--theirs` / `:3:` | **mirror = upstream** |

> 跟 Phase 0.4 的 `+`/`-` 是同一類陷阱:方向搞反會做出完全相反的決定,
> 而且結果看起來很正常。**不確定就先看內容再動手**:
>
> ```bash
> git show :2:<檔案> | head      # ours   = 生產
> git show :3:<檔案> | head      # theirs = upstream
> ```

#### 步驟三:決定權威方

| 情況 | 處理 | 為什麼 |
|---|---|---|
| `AA`,你的是 prod-only preset | **兩邊都留**:你的改名,原路徑讓給 upstream | 直接 take ours 會把 upstream 的新檔案**靜默吃掉** |
| `UU`,共用檔案 | 預設採用 upstream,你的客製回頭補 externalize | 同 Phase 2.0:以 upstream 為基底,漏掉客製會被測試/煙測抓到;反過來則是靜默過時 |
| `DU`/`UD` | 看 upstream 那支 commit 的 message 判斷是搬家還是廢棄 | `git log --merge -- <檔案>` |
| 看不懂 | **放棄這輪**,不要硬解 | merge = deploy,猜錯的代價是生產故障 |

#### AA 的標準解法（prod preset 撞名）

這是你最可能遇到的一種 —— `examples/mock/` 底下的業務 preset:

```bash
# 1. 你的版本存成一個不會再撞的檔名
git show :2:examples/mock/foo.json > examples/mock/prod-foo.json
# 2. 原路徑讓給 upstream
git checkout --theirs -- examples/mock/foo.json
# 3. 兩個都 add,然後完成 merge
git add examples/mock/foo.json examples/mock/prod-foo.json
git commit -m "resolve add/add: keep upstream at foo.json, prod preset renamed"
```

**改名是重點**,不是可選的收尾。留在原路徑的話,下次 upstream 再動那個檔案
就會再撞一次,而且每次都要重解。

#### 步驟四:解完一定要重跑驗證

```bash
./scripts/sync-from-upstream.sh --verify-only
```

不只是為了跑測試 —— **`SYNC_LOG.md` 是在這一步才補寫的**。
跳過它,MR 會沒有根路徑變動,merge 進 master 也**不會觸發 deploy**,
而且失敗得很安靜:MR 顯示成功,生產機器還是舊 code。

#### 放棄這輪

```bash
git merge --abort
git switch master && git branch -D sync/YYYY-MM-DD
```

master 全程沒被碰過,零風險。upstream 的東西下次同步照樣拿得到,不會遺失。

#### 從根本避免撞名

生產專屬、含業務內容而**不會進 GitHub** 的檔案（例如 `examples/mock/` 底下的
真實情境 preset）,挑一個 upstream 不會用的命名空間:

```
examples/mock/prod-*.json      # 前綴
examples/mock/local/*.json     # 或整個子目錄
```

代價是一次性的命名紀律,換掉的是「某天剛好撞名」這種無法預期的 conflict。

> 順帶一提,放在 `examples/mock/` 而非頂層 `examples/` 是對的:
> `tests/test_examples.py` 只掃**頂層** `examples/*.json` 並斷言每個都要 solve
> 成功,放頂層的話一個故意 INFEASIBLE 的 preset 會讓 `make test` 變紅、
> 被 script 擋下。UI 那側用的是 `rglob`,遞迴,所以子目錄照樣列得出來。
>
> 另外 `examples/mock/x.json` 不在根路徑,**單獨 push 不會觸發 deploy**,
> 要等下一次帶 `SYNC_LOG.md` 的 sync MR 才會一起上生產。

### 紀律

- **sync 分支要短命**:每次從 master 新開、當天走完 MR 收掉。
  留長期分支 = 在 fork 裡重新養出一個 delta 問題。
- **master 設 protected**（只准 MR 進入）→ 沒有人能直接推生產 code。
  因為 merge 就是 deploy,這條規則等於「沒有人能繞過 review 上線」。
- **script 的 preflight 是免費的漂移偵測器**:有人手癢直接改工作區並 commit,
  它會擋在 sync 開始之前,而不是把那些改動靜默夾帶進 MR 一起 deploy。

---

## Phase 4 — 終態

Phase 1 完成後 delta 已歸零,sync MR 退化成純轉發。原本可二選一,
但採用 `SYNC_LOG.md` 之後**已經確定走 B**;A 留在這裡是為了記錄為什麼不選它。

**選項 A — 全自動**
生產 repo 開 GitLab 內建 **pull mirroring**
（Settings → Repository → Mirroring,方向 pull,來源 = mirror repo）
→ GitLab 自動定時同步,人工同步完全消失。
> 只適合 delta=0:它會強制對齊分支,有本地 commit 會被輾掉或同步失敗。
> 若 A 類檔案仍留在生產 repo,**不能用這個選項**,走 B。

> **`SYNC_LOG.md` 已經讓選項 A 出局。**
> 它是永久的 A 類檔案(只存在生產 repo),pull mirroring 會把它輾掉;
> 而且 A 沒有 MR,也就沒有「根路徑變動」這個觸發點可言。
> 這不是遺憾 —— A 本來就等於拿掉 script 那道上線前的驗證關卡。
> 真的想全自動,正確做法是把等價的驗證搬進 pipeline,而不是改用 A。

**選項 B — 保留 MR 關卡（現況即此）**
維持 Phase 3 流程,但每次 merge 都是自動成功的純轉發。
好處:保留 MR 作為審計記錄與人工放行點,A 類檔案也能繼續存在,
`SYNC_LOG.md` 也在這裡才有意義。

**最終狀態**
```
GitHub merge PR → pipeline 掃描 → mirror
   → sync-from-upstream.sh (merge + 驗證 + SYNC_LOG.md) → 你 push → MR
   → merge 進生產 repo master → deploy pipeline → 生產機器
```

---

## 進度追蹤

- [x] 前置檢查:pipeline 抓 `release-to-gitlab`
- [x] Phase 0:盤點完成（delta = server.py / pyproject / Makefile / .gitignore）
- [x] Phase 1:externalize 完成並流進 mirror（PR #31、#32）
- [x] Phase 2:移植手術 + 對齊完成，delta 歸零
- [x] Phase 3:例行同步自動化（`scripts/sync-from-upstream.sh`）
- [ ] Phase 3:用該 script 跑過至少一輪真實同步
- [x] Phase 4:選定 B（保留 MR 關卡；`SYNC_LOG.md` 使 A 不可行）
- [ ] Phase 4:確認 deploy pipeline 在 `pyproject.toml`/`uv.lock` 變動時會重裝依賴

## 相關 script

| Script | 用途 |
|---|---|
| `scripts/sync-from-upstream.sh` | Phase 3 例行同步:fetch → merge → 驗證 → 交由你決定是否 push |
| `scripts/compare-with-upstream.sh` | Phase 0 盤點:三區塊 diff 報告 + endpoint/環境變數契約交叉檢查 |

---

## 常見問題

**Q: 幾百個 commit 的歷史差異要處理嗎?**
不用。delta 是「檔案現狀的差」,不是「commit 歷史的差」。
Phase 0 的 `git diff mirror/... master` 直接比兩棵樹,與歷史無關。

**Q: 中途發現做錯了怎麼辦?**
Phase 2.5 之前:所有操作都在分支上,master 沒被碰,直接棄分支。
Phase 2.5 之後:`master-legacy` 還在,force 推回去即可。

**Q: 為什麼不 fork？**
Fork 的好處是「血緣保證」,但你的 deploy 綁定在舊 repo 上不能換。
Phase 2 的體內移植在舊 repo 裡達成同樣效果,repo 身分不變。
