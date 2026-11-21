# agent-home

個人的 agent 工作入口。這裡存放共用 skills（`.apm/skills/`）、共用 agents（`.apm/agents/`）和共用文件，並指向其他 repo。

第一次使用先執行 `apm-go install`，把 `.apm/` 扇出到 `.claude/`、`.agents/` 和 `.codex/`。這些扇出目錄不進版控。

## Other Repos

- StatusLine (Claude Code 的自訂狀態列，顯示模型、token、速率限制與 git 資訊): ~/Desktop/projects/StatusLine. Clone from https://github.com/gn00678465/StatusLine.git if missing. 先讀它的 AGENTS.md。
- subs-tracker (Cloudflare Workers 訂閱管理系統，追蹤續約並提醒): ~/Desktop/projects/subs-tracker. Clone from https://github.com/gn00678465/subs-tracker.git if missing. 先讀它的 AGENTS.md。
- handbrake-agent (用 GitHub Copilot SDK 給影片轉碼參數建議): ~/Desktop/projects/handbrake-agent. Clone from https://github.com/gn00678465/handbrake-agent.git if missing. 先讀它的 AGENTS.md。
- coding-agent-toolkit (外部 skills 的 marketplace，Claude Code 與 Codex plugins 集合): ~/Desktop/projects/coding-agent-toolkit. Clone from https://github.com/gn00678465/coding-agent-toolkit.git if missing. 自有 skills 已搬到本 repo，它只放外部 skills。
