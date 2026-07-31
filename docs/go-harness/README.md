# go-scheduler 的 Claude harness

把 solver repo 這套 harness 移植到 Go 端。**骨架相同**(CLAUDE.md +
Stop hook 兩道閘 + skills + ADR),**內容重寫**——solver 端防的是 CP-SAT
建模陷阱,Go 端防的是**契約漂移**與**靜默說謊的資料**。

## 安裝

```bash
# 在 go-scheduler repo 根目錄
cp <此目錄>/CLAUDE.md               ./CLAUDE.md
mkdir -p .claude/hooks .claude/skills docs/decisions
cp -r <此目錄>/claude/skills/*      .claude/skills/
cp <此目錄>/claude/hooks/stop-gate.sh .claude/hooks/
chmod +x .claude/hooks/stop-gate.sh
cp <此目錄>/decisions-TEMPLATE.md   docs/decisions/TEMPLATE.md
```

`.claude/settings.json` 註冊 Stop hook:

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          { "type": "command", "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/stop-gate.sh" }
        ]
      }
    ]
  }
}
```

## 安裝後必改

| 檔案 | 改什麼 |
|---|---|
| `CLAUDE.md` | 所有 `<...>` 佔位符;`## Architecture` 的實際套件結構;`## Commands` 的實際指令 |
| `.claude/hooks/stop-gate.sh` | `DEFAULT_BRANCH`、`CORE_PATTERN`(哪些路徑改動要求 ADR) |
| `.claude/skills/*/SKILL.md` | 出現的套件路徑(`internal/...`)換成實際的 |

`CLAUDE.md` 的**領域語意**那一節不要改——那些是設計決議,改了 agent 就會
產出違反契約的程式碼。

## 內容物

| 檔案 | 作用 |
|---|---|
| `CLAUDE.md` | 專案指令:架構、領域語意(兩期一致性 / pool / 帳本三態 / 採購 / 指標 / 狀態字串)、mentor 協作約定、workflow |
| `claude/hooks/stop-gate.sh` | Stop 閘:① Go 檔有改 → build + vet + test 必須綠;② 契約關鍵路徑有改 → 必須有新 ADR。擋一次後放行,避免無限迴圈 |
| `claude/skills/contract-check/` | 15 條整合 anti-patterns + 快速複查表。**這份是移植的重點**,內容全部來自實際設計決議 |
| `claude/skills/verify-e2e/` | 對真 solver 跑一遍:用真程式碼組 request、驗語意而非只驗 `success`、動到候選推導時做一致性回放 |
| `claude/skills/adr/` | mentor 風格 ADR 產出規則 |
| `claude/skills/teach-back/` | 三題 active recall 小考,鞏固理解 |
| `decisions-TEMPLATE.md` | ADR 模板(繁體中文七節) |

## 與 solver 端 harness 的差異

| | solver(Python) | go-scheduler |
|---|---|---|
| Gate 1 | `pytest` | `go build` + `go vet` + `go test` |
| Gate 2 觸發 | `app/{solver,splitter,split_solver,models}.py` | 契約封裝層 / 規劃編排 / 候選推導 / migrations |
| 檢查清單 | CP-SAT 建模陷阱(12 條) | 契約整合陷阱(15 條) |
| E2E 驗證 | `make cli` 跑 examples,對 C1–C5 驗算 | 對真 solver POST,驗契約語意 + 一致性回放 |
| 共通 | mentor mode、繁體中文對話、決策附替代方案、核心變更 plan-first + ADR | 同左 |

## 建議

- **先裝 hook 再開始寫 code**。閘門的價值在於「壞掉時會擋」,事後補裝時
  既有的違規已經進 repo 了。
- `contract-check` 的 15 條建議**照抄不要精簡**。每一條都對應一個實際會發生
  且不會被測試抓到的失效模式;刪掉的那條通常就是之後出事的那條。
- Stop hook 的 Gate 1 要保持**快**(秒級)。需要真 solver 的整合測試放在
  build tag 後面,由 `/verify-e2e` 手動觸發,不要進 Gate 1。
