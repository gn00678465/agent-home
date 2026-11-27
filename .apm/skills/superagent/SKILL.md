---
name: superagent
description: >-
  Run a multi-stage coding delivery as a Router that never does the heavy work:
  it dispatches brief, implement, fix and review workers (Claude Code, Codex,
  Antigravity) onto herdr-core (hc), Orca or the harness's own subagents, and
  coordinates them through a per-project board that pushes each post to its
  recipient, with a watchdog for workers that stop without reporting, hit a
  prompt, or hang silently. Use when delegating isolated worktree
  implementation plus independent cross-model review, running a multi-round
  review/fix loop with a stop-loss, or supervising long-running agent workers.
---

# superagent：Router–Worker 交付流程

Router 是執行這個 skill 的 agent，也是唯一和使用者（Owner）對話的窗口。重活交給 worker；worker 之間、worker 和 Router 之間的交接都走 board。

```text
Owner ⇄ Router ──set-role, dispatch──▶ worker（角色依任務命名：impl、migration、perf-review…）
          ▲                     │
          └──── board 推送 ◀────┘  post --kind done|ask
          ▲
          └──── watch：停了沒回報、卡在提示、靜默卡死
```

Router 守則：

1. 不自己寫程式、跑長測試。Router 對齊 Mandate、核對 brief、派工、讀證據、裁定。
2. 省 Owner 的注意力。工程細節自己裁定並寫進 ruling；只有產品語意空白、無法安全自動恢復的阻塞、最終驗收才問 Owner，而且濃縮成一個有選項的問題。
3. worker 不直接對 Owner 說話，只透過 `post` 交接。
4. 以證據為準。合併前一定要跑 `verify-gate`。

## 檔案位置

所有紀錄放在 skill 所屬 repo（agent-home）根目錄下的 `.superagent/<owner>-<repo>/`，不放進目標專案。`<owner>-<repo>` 取自目標專案的 `origin`；沒有 origin 就用資料夾名稱。`.superagent/` 已列入 `.gitignore`，每台機器各自一份。要放在別處就設 `SUPERAGENT_HOME`。

```text
.superagent/<owner>-<repo>/
  config.json     kinds（write / review 的預設）、roles（本專案建立過的角色）、監控門檻、重做上限
  members.json    Router 端點、每個 worker 的位置、讀取游標、告警計數
  state.json      目前階段、各階段輪次與狀態、重做紀錄
  board.jsonl     所有 post，一行一則，seq 遞增
  stages/<stage>/ brief.md、<worker>.prompt.md、report-<角色>[-r<n>].md、logs/、logs-attempt<n>/、watchdog/
```

## 指令

一律以 `uv run <skill>/scripts/superagent.py <子命令> --project <目標專案>` 執行。加 `--output <檔案>` 會另外寫出完整 JSON；stdout 只印摘要。給 worker 的回報指令是 `uv run --no-cache`，因為 codex 的 sandbox 不能寫 uv 的快取；機器上沒有 uv 時改用 `python3`，腳本只用標準函式庫。

| 子命令 | 用途 |
|---|---|
| `init [--use-defaults]` | 載入或建立 config，並登記 Router 端點。Router 換了 terminal 就再跑一次 |
| `set-role --role <名稱> [--kind write\|review] [--purpose] [--ade hc\|orca\|native] [--cli claude\|codex\|agy] [--model] [--effort] [--confirm]` | 建立或修改角色。新角色要給 `--kind`；沒加 `--confirm` 時只回傳建議值（exit 3），問過 Owner 才加 `--confirm` 寫入 |
| `stage-new --stage <s> --goal <g> [--base]` | 建立階段與 brief 骨架 |
| `dispatch --stage <s> --role <r> [--marker] [--worktree --create-worktree-branch] [--dry-run]` | 依角色設定啟動 worker 並登記 |
| `post --as <me> --to router\|<worker>\|all --kind brief\|done\|ask\|answer\|note\|alert\|ruling\|objection [--stage <s>] [--reply-to <seq>] [--owner-ack <裁定>] [--skip-check] --text\|--file` | 發 post 並推送通知；worker 發 `done` 前先跑本地自檢（標記、佔位符、本輪既有日誌 `exit=`） |
| `read --as <me> [--peek] [--since N]` | 讀未讀的 post，並把游標推到最新 |
| `watch [--auto-nudge] [--loop]` / `watch-start` | 巡檢；詳見 [watchdog_protocol.md](references/watchdog_protocol.md) |
| `hook-stop --as <worker>` | worker CLI 的 Stop hook 呼叫，Router 不必手動執行 |
| `diagnose --worker <id>` | 單一 worker 的畫面、日誌、子行程樹 |
| `verify-gate --stage <s> --report <f> --marker <m> [--commit] [--round N]` | 檢查完成標記、佔位符、本輪日誌 `exit=`、commit、未解 `objection`、待確認 `ruling`；帶 `--commit` 時另擋未處理的 `VERDICT: FAIL` |
| `next-round --stage <s> --ruling <ruling_r<n>.md>` | 審查沒過時開下一輪修正並自動把 `ruling` 寫上 board；已到 `max_review_rounds`（預設 3）就回 exit 2 和 `ROUND_LIMIT` |
| `status` | 回答「進度如何」用 |
| `reset-stage --stage <s> --reason <r>` / `--owner-ack <裁定>` | 記一次從 base 重做（遞增 `attempt`，隔離舊異議與舊裁定）；達上限就停下交給 Owner |
| `close-worker --worker <id> [--remove-worktree]` | 結束 worker 並清理 |

各 ADE 的差異見 [backends.md](references/backends.md)，brief 範本見 [brief_templates.md](references/brief_templates.md)。

## 0. 設定（第一次用）

`init` 找不到設定檔時，會以 exit 3 和 `CONFIG_MISSING` 停下，並在輸出中附上 `defaults_preview`。裡面是兩種 kind 的預設：`write`（寫 brief、實作、修正，預設 hc + claude opus high）和 `review`（審 brief、審程式，預設 hc + codex gpt-6.1-terra high）。兩種預設分屬不同廠商，讓審查是跨模型的。把它濃縮成一個問題問 Owner：照預設，或指定 ade、cli、model、effort。Owner 選預設才加 `--use-defaults` 重跑；有指定的就改 `config.json` 的 `kinds`。

新設定的 `roles` 是空的。角色不預先定義，由 Router 在派工前依任務或 Owner 的提示建立，見下一節。

已經有設定檔時，`init` 不會覆寫。Owner 要求換模型或 ADE 時才改設定檔；已經在跑的 worker 不受影響。

### 角色

角色名稱由 Router 依任務決定，例如一般實作叫 `impl`，資料庫遷移叫 `migration`，效能審查叫 `perf-review`。Owner 在提示裡指定的分工或模型（例如「請 Gemini 審安全」）優先。

建立新角色一律先問 Owner，分兩步：

```bash
# 1. 提案：不加 --confirm，exit 3 和 ROLE_CONFIRM_REQUIRED，輸出 proposal（建議值）和 from_kind_defaults（哪些欄位是沿用預設）
superagent.py set-role --project $P --role perf-review --kind review --purpose "效能與查詢次數審查"
# 2. Owner 同意或改了值之後，帶上最後的值和 --confirm 才寫入 config.json
superagent.py set-role --project $P --role perf-review --kind review --purpose "效能與查詢次數審查" --model gpt-6.1-terra --confirm
```

- 問 Owner 時，一次列出這次階段要建立的所有角色，每個角色寫清楚用途和建議的 ade、cli、model、effort，並說明建議的理由。例如：審查類預設用和寫作類不同廠商的模型，審查才是跨模型的。Owner 可以整批同意，也可以逐項改。
- 新角色一定要給 `--kind`。`kind` 決定建議值要取 `kinds.write` 還是 `kinds.review`，也讓 Router 知道該用哪一份 brief 範本。
- 已經建立的角色會留在 `config.json`，之後的階段可以直接重用，不用再問。要調整已有的角色就再跑一次 `set-role`，不需要 `--confirm`，`kind` 也不用重給。
- `dispatch --role` 只接受 `config.json` 裡有的角色，沒有就回 `ROLE_UNDEFINED`。啟動參數一律從設定檔來，不靠 Router 記。

## 1. Intake

用自己的話複述目標、交付物和不在範圍內的事。只就會影響產品或可見行為的空白提問，一次問完。接著跑 `stage-new`。

## 2. Brief 與開工前審查

複雜的階段先派一個 `write` 類角色（例如 `brief`）寫 `brief.md`。brief 要有 Mandate、不變量、邊界表、驗證指令和完成標記，路徑一律寫絕對路徑。關鍵階段再派一個 `review` 類角色（例如 `brief-review`）做紅隊審查：工程空白由 Router 補進設計並以 `post --as router --to all --stage <s> --kind ruling` 留紀錄，產品空白問 Owner。

## 3. 實作

```bash
superagent.py set-role --project $P --role impl --kind write             # 提案，問 Owner；已建立過就跳過
superagent.py set-role --project $P --role impl --kind write --confirm   # Owner 同意後
superagent.py dispatch --project $P --stage 01-x --role impl --marker "IMPL DONE" \
  --worktree $P-01 --create-worktree-branch feat/01-x
```

`dispatch` 依角色設定做這些事：

- 啟動 worker，並在 board 記一則帶 `stage`、`round`、`attempt` 的 `brief` post；若本階段（同 `attempt`）已有 `ruling`，會自動注入 worker 的 prompt 檔。
- hc 上的 claude worker 會掛上 stop hook：沒 post 就結束回合會被擋回去。其他組合靠 watch。
- 機器上有 hc 時，自動以 hc 背景 terminal 啟動 `watch` 迴圈，不論 Router 在 hc 或 Orca。
- 角色是 `native` 時，輸出 `native_spawn.prompt`。Router 用自己的 subagent 工具把它原樣交給 subagent。

`dispatch` 輸出的 `Report:` 就是這次的報告路徑，`verify-gate --report` 用它。同一個角色再派一次（例如關掉卡死的 worker 後重派）時，報告依序改成 `report-<角色>-r2.md`、`-r3.md`，舊報告保留，它的完成標記也不會讓 `watch` 把新 worker 當成已完成。`--report` 指定的檔案已經存在時，`dispatch` 回 `REPORT_EXISTS`。

worker 執行 `post --kind done` 時，會先在本地檢查完成標記與佔位符；非 `review` 角色還會檢查本輪既有日誌的 `exit=`（空日誌不擋，避免誤擋 `brief`；特殊情況可加 `--skip-check`）。沒過會直接在 worker 端 exit 1 退件，不會送進 board。

派完工就結束這一回合。worker 發 post 時，通知會推到 Router：

- hc：通知直接出現在對話中。
- Orca：先出現一行 `You have 1 orchestration message…`，跑 `orca orchestration check` 後才看得到通知。
- native：subagent 結束時由 harness 通知。

不管哪一種，都照通知裡的指令跑 `read --as router` 讀全文。

- 收到 `done`：跑 `verify-gate`，沒過就退回補齊。
- 收到 `ask`：決定後用 `post --as router --to <worker> --kind answer --reply-to <seq>` 回覆，worker 會收到推送。
- 收到 `objection`：未獲對應回覆前，`verify-gate` 一律不通過。首次異議由 Router 附證據用 `post --as router --to <worker> --kind answer|ruling --reply-to <seq>` 回覆；若出現**再異議**（worker 對 Router 回覆 `objection` 的 `answer`／`ruling` 再提 `objection`）或**兩名以上不同 worker 反對同一則 `ruling`**，Router 無法自行關閉（回 exit 2 和 `ESCALATION_REQUIRED`），必須升級交給 Owner 裁定，再以 `post --as router --to <worker> --kind ruling --reply-to <seq> --owner-ack "<裁定>"` 解鎖。
- 收到 `alert`：照 watchdog_protocol.md 的表格處理。

## 4. 審查與最多 3 輪修正

小批（≤ 5 個檔案）派一個 `review` 類角色；大批或需要雙審時，建立兩個不同視角的 `review` 類角色同時派（例如 `review-correctness` 看正確性與測試，`review-security` 看安全與設計），最好用不同的 CLI 或模型。標記用 `"VERDICT: PASS|VERDICT: FAIL"`。修正時派 `write` 類角色（例如 `fix`）。

一輪是一次寫作（第 1 輪是實作，之後是修正）加上它的審查。階段的 `round` 記在 `state.json`，從 1 開始，只有 `next-round` 會加一。審查回報後，Router 逐條對照程式碼，寫出 `ruling_r<n>.md`（`n` 是目前的 round）：

- 審查全部原本即 `VERDICT: PASS` 且無成立的 P1／P2：通過，進入步驟 5。
- 審查報告為 `VERDICT: FAIL`，但 Router 核對後駁回所有意見、打算直接 PASS：不允許靜默略過。先執行 `post --as router --to <rev_worker> --kind ruling --file ruling_r<n>.md` 喚醒原審查者（工具會自動將該 `ready` worker 切回 `active` 並追蹤確認）。原審查者讀完後回 `--kind note --reply-to <seq>`（同意）或 `--kind objection --reply-to <seq>`（反駁）；確認前 `verify-gate` 不通過，直接跑 `verify-gate --commit` 也會被 `unaddressed_fail_reviews` 擋下。若原審查者是不可推送的 `native` worker，則重派確認或交由 Owner 以 `--owner-ack` 裁定。
- 有成立的 P1／P2：跑 `next-round --stage <s> --ruling ruling_r<n>.md`。`next-round` 會自動發出一則 `ruling` post（只推送給同階段 `active` worker，不吵醒已 `ready` 的舊 worker），且之後 `dispatch` 會自動把本階段既有 `ruling` 注入新 worker prompt（`review` 角色必須逐條核對前輪被駁回的意見）。exit 0 時 round 變成 `n+1`：寫 `brief_r<n+1>.md`，派 `fix` 修正並做變異驗證，日誌寫成 `logs/r<n+1>_*.log`，再送審。
- 第 3 輪仍不過：`next-round` 回 exit 2 和 `ROUND_LIMIT`，之後 `dispatch` 也會拒絕。不開第 4 輪。整合 3 份審查報告，找出 brief 漏掉的不變量。跑 `reset-stage --reason "<brief 的結構性空白>"`，修訂 `brief.md`，丟掉舊分支和 worktree，從 base 重新派工。`reset-stage` 把 round 歸回 1、遞增 `attempt`（隔離舊 attempt 的 `objection` 與 `ruling`），並把舊日誌移到 `logs-attempt<n>/`。
- `verify-gate` 只檢查目前這一輪的日誌：第 1 輪是沒有 `r<n>_` 前綴的 `.log`，第 n 輪是 `r<n>_*.log`。前一輪失敗的日誌不會讓之後的輪次過不了。要檢查別輪就加 `--round`。
- 同一階段最多重做 `max_stage_resets` 次（預設 2）。超過時，`reset-stage` 回 exit 2 和 `STAGE_HALTED`，`dispatch` 和 `stage-new` 都會拒絕。把 `reset_history` 交給 Owner 裁定，裁定後用 `reset-stage --owner-ack "<裁定>"` 解除。

## 5. 收尾

全部 `VERDICT: PASS`（或 `FAIL` 駁回已獲原審查者確認）且 `verify-gate --commit <SHA>` 通過後：

1. `close-worker --remove-worktree`。worker 回報後仍停在自己的輸入提示，不會自己結束。
2. 依專案規範合併，再跑最終驗證。
3. 最終交付停在 Owner 驗收：Router 先自己處理能處理的實測發現，只把需要 Owner 決定的項目和驗證入口交出去。

## 常見錯誤

- 輪詢 worker 畫面或 Orca 的 `check` 來等完成。完成以 board 推送為準，卡住交給 `watch`。
- 只看 `working` 就以為沒卡住。把測試日誌放進 `--watch-log` 或階段的 `logs/`，`watch` 才能判斷靜默卡死。
- 卡在權限提示時代按，或用 `hc send --force`。改 brief 的邊界或問 Owner。
- 用 `stage-new --force` 或換階段名稱，繞過重做上限或輪次上限。
- 修正輪的日誌沒加 `r<n>_` 前綴。`verify-gate` 找不到本輪日誌，回報 `logs_present` 失敗；在 brief 裡寫清楚日誌檔名。
- 審查沒過卻沒跑 `next-round` 就派 `fix`。round 停在原地，`verify-gate` 會去看上一輪的日誌，上限也擋不住。
- 回覆 `objection` 沒帶 `--reply-to <seq>`，導致 `verify-gate` 仍判 `unresolved_objections` 失敗；或是想駁回 `VERDICT: FAIL` 直接合併卻沒對原 reviewer 發 `ruling` 確認，被 `verify-gate --commit` 擋下。
- 沒有先 `set-role` 就派工，`dispatch` 會回 `ROLE_UNDEFINED`。角色的 model 或 effort 是空的，則回 `ROLE_UNPINNED`。用 `--cli` 換 CLI 時要同時給 `--model`。
- Router 換了 terminal 卻沒再跑 `init`。推送會送到舊的端點。
