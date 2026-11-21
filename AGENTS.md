# agent-home

個人的 agent 工作入口。這裡存放共用 skills（`.apm/skills/`）、共用 agents（`.apm/agents/`）和共用文件，並指向其他 repo。

第一次使用先執行 `apm-go install`，把 `.apm/` 扇出到 `.claude/`、`.agents/` 和 `.codex/`。這些扇出目錄不進版控。

## Other Repos

每個 repo 在本機的路徑記在 `repos.local.md`（不進版控，每台機器各自一份，格式見 `repos.local.example.md`）。

- 找不到某個 repo 的條目時，問我它在哪、或要 clone 到哪，clone 後把路徑寫進 `repos.local.md`。
- 條目指向的目錄不存在時，視為沒有，不要自行掃描硬碟。

- StatusLine (Claude Code 的自訂狀態列，顯示模型、token、速率限制與 git 資訊): Clone from https://github.com/gn00678465/StatusLine.git if missing. 先讀它的 AGENTS.md。
- subs-tracker (Cloudflare Workers 訂閱管理系統，追蹤續約並提醒): Clone from https://github.com/gn00678465/subs-tracker.git if missing. 先讀它的 AGENTS.md。
- handbrake-agent (用 GitHub Copilot SDK 給影片轉碼參數建議): Clone from https://github.com/gn00678465/handbrake-agent.git if missing. 先讀它的 AGENTS.md。
- coding-agent-toolkit (外部 skills 的 marketplace，Claude Code 與 Codex plugins 集合): Clone from https://github.com/gn00678465/coding-agent-toolkit.git if missing. 自有 skills 已搬到本 repo，它只放外部 skills。
