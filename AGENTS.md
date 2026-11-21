# agent-home

個人的 agent 工作入口。這裡存放共用 skills（`.apm/skills/`）、共用 agents（`.apm/agents/`）和共用文件，並指向其他 repo。

第一次使用先執行 `apm-go install`，把 `.apm/` 扇出到 `.claude/`、`.agents/` 和 `.codex/`。這些扇出目錄不進版控。

## Other Repos

每條指標格式：`<name> (<一句話職責>) [關鍵字: ...]`。用職責和關鍵字判斷任務屬於哪個 repo。

- StatusLine (Claude Code 的自訂狀態列，顯示模型、token、速率限制與 git 資訊) [關鍵字: statusline, 狀態列, token, rate limit]: Clone from https://github.com/gn00678465/StatusLine.git if missing. 先讀它的 AGENTS.md。
- subs-tracker (Cloudflare Workers 訂閱管理系統，追蹤續約並提醒) [關鍵字: 訂閱, 續約, 提醒, Cloudflare Workers]: Clone from https://github.com/gn00678465/subs-tracker.git if missing. 先讀它的 AGENTS.md。
- handbrake-agent (用 GitHub Copilot SDK 給影片轉碼參數建議) [關鍵字: HandBrake, 轉碼, 影片, Copilot SDK]: Clone from https://github.com/gn00678465/handbrake-agent.git if missing. 先讀它的 AGENTS.md。
- coding-agent-toolkit (只放外部 skills 的 marketplace，Claude Code 與 Codex plugins 集合；自有 skills 在 agent-home) [關鍵字: marketplace, 外部 skills, plugin, apm.yml]: Clone from https://github.com/gn00678465/coding-agent-toolkit.git if missing.

## Repo 判定規則

每個 repo 的本機路徑記在 `repos.local.md`（不進版控，範本是 `repos.local.example.md`）。

1. 修改程式碼或執行寫入指令前，先列出目標 repo 的完整路徑。
2. 需求可能對應兩個以上 repo 時，停下來問我，不要猜。
3. `repos.local.md` 沒有目標 repo 的條目時，問我路徑或 clone 位置，再寫回 `repos.local.md`。不要掃描硬碟。
4. 進入路徑前，執行 `git -C <path> remote get-url origin`，與上面 remote 的 `owner/name` 比對，忽略 https 與 ssh 的差異。不符就停下來問我。
5. Git 指令一律用 `git -C <path>`，不依賴 `cd`。
6. 修改前，執行 `git -C <path> status`。不在預期分支或工作區不乾淨時，先回報。
7. 讀 repo 內容前，執行 `git -C <path> fetch`。落後遠端時告訴我，由我決定是否 pull。
8. 除非我明確要求更新 agent-home 本身，不在 agent-home 目錄執行 `git commit`。
