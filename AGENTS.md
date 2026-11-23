# agent-home

個人的 agent 工作入口。這裡存放共用 skills（`.apm/skills/`）、共用 agents（`.apm/agents/`）和共用文件，並指向其他 repo。

第一次使用先執行 `apm-go install`，把 `.apm/` 扇出到 `.claude/`、`.agents/` 和 `.codex/`。這些扇出目錄不進版控。

## Other Repos

repo 清單、本機路徑與 remote 都在 `repos.local.md`（不進版控，範本是 `repos.local.example.md`）。各機器的系統與路徑不同，所以本檔不列 repo。

每條格式：`<name> (<一句話職責>) [關鍵字: ...] [remote: <url>]: <本機路徑>`。用職責和關鍵字判斷任務屬於哪個 repo。

新增 repo 的步驟見 README.md「新增 repo」。先讀它再動手。
條目超過 20 個時，改成每個領域一個檔案（如 `repos.local/billing.md`），`repos.local.md` 只用 `@` 引入這些檔案。

@repos.local.md

## Repo 判定規則

1. 修改程式碼或執行寫入指令前，先列出目標 repo 的完整路徑。
2. 需求可能對應兩個以上 repo 時，停下來問我，不要猜。
3. `repos.local.md` 沒有目標 repo 的條目時，問我路徑或 clone 位置，再寫回 `repos.local.md`。不要掃描硬碟。
4. 進入路徑前，執行 `git -C <path> remote get-url origin`，與該條目 `remote` 的主機與 `owner/name` 比對，忽略 https 與 ssh 的差異。不符就停下來問我。
5. 進入 repo 後，若它有 `AGENTS.md`，先讀。
6. Git 指令一律用 `git -C <path>`，不依賴 `cd`。
7. 修改前，執行 `git -C <path> status`。不在預期分支或工作區不乾淨時，先回報。
8. 讀 repo 內容前，執行 `git -C <path> fetch`。落後遠端時告訴我，由我決定是否 pull。
9. 除非我明確要求更新 agent-home 本身，不在 agent-home 目錄執行 `git commit`。
