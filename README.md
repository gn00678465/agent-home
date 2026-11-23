# agent-home

個人的 agent 工作入口。從這個 repo 啟動 agent，它會讀 `AGENTS.md`，找到其他 repo 並使用共用 skills 與 agents。

## 內容

| 路徑 | 說明 |
|---|---|
| `AGENTS.md` | agent 的入口，引入 `repos.local.md` 並定義 repo 判定規則 |
| `.apm/skills/` | 共用 skills 的唯一來源 |
| `.apm/agents/` | 共用 agents，檔名為 `<name>.agent.md` |
| `evals/` | subtractive 系列 skill 與 agent 的評測案例 |
| `repos.local.example.md` | 本機路徑範本 |
| `repos.local.md` | 這台機器的 repo 路徑，不進版控 |
| `apm.yml`、`apm.lock.yaml` | apm-go 設定與鎖定檔 |

`.claude/`、`.agents/`、`.codex/` 是 `apm-go install` 產生的結果，不進版控。

### Skills

`done-or-not`、`issue-template`、`pull-request`、`review-forge`、`slim-agents-md`、`subtractive-engineering`、`superagent`、`ui-design`。

### Agents

`impeccable-ui-designer`、`subtractive-brake`。

## 新機器設定

1. 安裝 [apm-go](https://github.com/gn00678465/apm-go)。
2. Clone 並安裝：

   ```bash
   git clone https://github.com/gn00678465/agent-home.git
   cd agent-home
   apm-go install
   ```

3. 建立本機路徑檔：

   ```bash
   cp repos.local.example.md repos.local.md
   ```

   把每行改成這台機器上的路徑。沒有的 repo 不用列，agent 會問你路徑或 clone 位置，再寫回這個檔案。
4. 在 `agent-home` 目錄啟動 agent。

## 新增 repo

`AGENTS.md` 不列 repo，只用 `@repos.local.md` 引入。

1. 在每台機器的 `repos.local.md` 加一行，路徑寫該機器的實際位置：

   ```text
   - <name> (<一句話職責>) [關鍵字: ...] [remote: <url>]: <本機路徑>
   ```
2. 想讓其他機器共用這個 repo，在 `repos.local.example.md` 加同一行，路徑改成 `~/Desktop/projects/<name>`，再 commit 並推送。

條目超過約 20 個時，改成每個領域一個檔案（例如 `repos.local/billing.md`），`repos.local.md` 只用 `@` 引入這些檔案。`repos.local/` 含本機路徑，不進版控。

## 新增或修改 skill、agent

1. 改 `.apm/skills/<name>/` 或 `.apm/agents/<name>.agent.md`。不要改扇出目錄。
2. 執行 `apm-go install`，更新 `.claude/`、`.agents/`、`.codex/`。
3. commit `.apm/` 與 `apm.lock.yaml`。

外部 skills 不放在這裡，由 [coding-agent-toolkit](https://github.com/gn00678465/coding-agent-toolkit) 轉發。
