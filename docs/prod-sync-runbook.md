# 生產環境同步 Runbook

把「每次手動複製檔案」換成「git pull」的完整流程。

## 適用情境

生產環境位於封閉網路,code 需經 GitLab Pipeline 做 security scan 後才能落地,
且生產 repo 已綁定 deploy 設定而不可替換。歷史上生產環境的更新靠人工逐檔複製,
導致:

1. 無法確知生產環境與 upstream 的實際差異（delta 不可見）
2. upstream 若改到被「跳過複製」的檔案,該更新會被靜默遺漏
3. 每次更新都是高風險的手工作業

本 runbook 的目標是把這三件事收斂成 `git pull --ff-only`。

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
                                git pull --ff-only
                                    ↓
                               生產機器
```

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
> - **A 類（你新增的檔案）**:upstream 永遠不碰 → merge 永遠不 conflict
>   → **不繳同步稅**,不急著 externalize,Phase 2 疊一個 commit 即可
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
- pip index / proxy → `PIP_INDEX_URL` 環境變數或 `pip.conf`（本來就不該在 pyproject.toml）
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
git branch master-legacy master
git push origin master-legacy
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
git checkout master-legacy -- <檔案A> <檔案B> ...
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
export PIP_INDEX_URL=https://<內部索引>/simple    # 封閉網路
make install                          # = pip install -e ".[dev]"
```

**驗證分兩層**,能跑多少跑多少:

| 層級 | 指令 | 需要 | 驗什麼 |
|---|---|---|---|
| 邏輯正確性 | `make test` | dev extras（pytest、httpx） | code 本身 —— GitHub CI 已驗過,這裡是複驗 |
| **環境相容性** | CLI + 煙測 | 只要 runtime deps | **這份 code 在這個環境能不能跑** ← 生產端真正該驗的 |

```bash
make cli INPUT=examples/success_basic.json

export SWAGGER_STATIC_DIR=/path/to/vendored/swagger
make run &
curl -s :50051/health      # 必須是 {"status":"ok"} —— probe 契約
curl -s :50051/openapi.json | head -c 100
curl -s -X POST :50051/v1/placement/solve \
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

> 這是**唯一一次**需要 reset。之後 master 只前進不改寫,永遠是 `git pull --ff-only`。
> 執行前確認生產機器上沒有未 commit 的重要東西（Phase 0 已經盤點過就安全）。

### 2.7 觀察期

舊分支 `master-legacy` 保留數週當 rollback 退路,確認穩定後再考慮刪除。

---

## Phase 3 — 例行同步（每次釋出）

### 3.1 GitHub 側（開發）

feature branch 開發完 → PR（自動 target `release-to-gitlab`）→ review → merge
→ Pipeline 掃描 → 流進 mirror

### 3.2 GitLab 側（工作機，不是生產機器）

```bash
cd solver-prod
git fetch mirror
git switch -c sync/$(date +%Y-%m-%d) origin/master
git merge mirror/release-to-gitlab
#   Phase 1 完成後:應該永遠自動 merge 成功
#   若 conflict → upstream 動到了你 A 類以外的東西,值得看一眼

make install && make test
make cli INPUT=examples/success_basic.json

git push origin sync/2026-08-18
# → GitLab 開 MR (sync/* → master) → review → merge
```

**部署 = MR merge 進 master 的那一刻。**

### 3.3 生產機器

```bash
git pull --ff-only
# pyproject.toml / uv.lock 有變動時重裝依賴:
make install
```

### 紀律

- **sync 分支要短命**:每次從 master 新開、當天走完 MR 收掉。
  留長期分支 = 在 fork 裡重新養出一個 delta 問題。
- **master 設 protected**（只准 MR 進入）→ 沒有人能直接推生產 code。
- `--ff-only` 是免費的漂移偵測器:有人手癢直接改生產 repo 並 commit,
  它會拒絕合併而不是靜默輾過去。

---

## Phase 4 — 終態

Phase 1 完成後 delta 已歸零,sync MR 退化成純轉發。此時可二選一:

**選項 A — 全自動**
生產 repo 開 GitLab 內建 **pull mirroring**
（Settings → Repository → Mirroring,方向 pull,來源 = mirror repo）
→ GitLab 自動定時同步,人工同步完全消失。
> 只適合 delta=0:它會強制對齊分支,有本地 commit 會被輾掉或同步失敗。
> 若 A 類檔案仍留在生產 repo,**不能用這個選項**,走 B。

**選項 B — 保留 MR 關卡**
維持 Phase 3 流程,但每次 merge 都是自動成功的純轉發。
好處:保留 MR 作為審計記錄與人工放行點,A 類檔案也能繼續存在。

**最終狀態**
```
GitHub merge PR → pipeline 掃描 → mirror → (自動/純轉發) → 生產 repo
                                                    → 生產機器 git pull --ff-only
```

---

## 進度追蹤

- [ ] 前置檢查:pipeline 抓 `release-to-gitlab`
- [ ] Phase 0:盤點完成,產出交給 Claude
- [ ] Phase 1:externalize 完成並流進 mirror
- [ ] Phase 2:移植手術 + 生產機器對齊
- [ ] Phase 3:跑過至少一輪例行同步
- [ ] Phase 4:選定 A 或 B 並設定完成

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
