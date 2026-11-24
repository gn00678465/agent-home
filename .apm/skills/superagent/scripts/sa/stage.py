"""Stage lifecycle: brief skeleton, verify gate and reset stop-loss."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

from .constants import (DEFAULT_MAX_REVIEW_ROUNDS, DEFAULT_MAX_STAGE_RESETS, PLACEHOLDER_RE, ROUND_LIMIT_STATUS,
                        ROUND_LOG_RE)
from .store import Repo, emit, iso_str, run_cmd, utc_now
from .watch import check_report_marker


def cmd_stage_new(args: argparse.Namespace) -> int:
    repo = Repo(args.project)
    repo.load_config()
    stage = args.stage.strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", stage):
        print(f"Error: invalid stage name '{stage}'", file=sys.stderr)
        return 1
    sdir = repo.stage_dir(stage)
    logs = sdir / "logs"
    for d in (sdir, logs, sdir / "watchdog"):
        d.mkdir(parents=True, exist_ok=True)
    base_ref = args.base
    if not base_ref:
        git_res = run_cmd(["git", "-C", str(repo.project), "rev-parse", "--short", "HEAD"], timeout=5.0)
        base_ref = git_res["stdout"].strip() if git_res["ok"] else "HEAD"
    brief = sdir / "brief.md"
    if not brief.exists() or args.force:
        brief.write_text(
            f"# 階段 {stage}：實作 Brief\n\n"
            f"## Mandate（目標與邊界）\n- **目標**：{args.goal}\n- **起點（base）**：`{base_ref}`\n"
            f"- **專案目錄**：`{repo.project}`\n- **日誌目錄**：`{logs}`（每份 `.log` 最後一行必須寫 `exit=<結束碼>`；第 n 輪修正的日誌檔名加 `r<n>_` 前綴）\n\n"
            f"## 不變量與邊界條件\n- （由 Router / Brief Worker 補齊不變量、允許修改範圍與禁止碰觸範圍）\n\n"
            # One brief serves several roles, so the report path lives in each worker's prompt, not here.
            "## 報告\n- 報告路徑和回報指令寫在各 worker 的 prompt 檔（dispatch 依角色各產生一份）。"
            "報告最後一行單獨寫完成標記，例如 `IMPL DONE`。\n",
            encoding="utf-8")
    with repo.lock():
        state = repo.load_state()
        prior = (state.get("stages") or {}).get(stage) or {}
        if str(prior.get("status", "")).startswith("halted"):
            print(f"Error: stage '{stage}' is halted; Owner must rule via `reset-stage --owner-ack`.", file=sys.stderr)
            return 1
        if prior.get("status") == ROUND_LIMIT_STATUS:
            print(f"Error: stage '{stage}' used all its review rounds; redo it via `reset-stage --reason`.", file=sys.stderr)
            return 1
        state["active_stage"] = stage
        state.setdefault("stages", {})[stage] = {
            # Recreating a stage must not clear its reset count or round, or the stop-losses can be bypassed.
            "round": 1,
            **{k: prior[k] for k in ("reset_count", "reset_history", "round", "round_history") if k in prior},
            "stage": stage, "goal": args.goal, "base": base_ref, "status": "briefing",
            "stage_dir": str(sdir), "logs_dir": str(logs), "brief_path": str(brief),
            "created_at": iso_str(utc_now()),
        }
        repo.save_state(state)
    return emit(args, {"stage": state["stages"][stage]}, [f"Stage '{stage}' ready (base={base_ref}).", f"Brief: {brief}"])



INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")


def unquoted(text: str) -> str:
    """Drop inline code that quotes a placeholder inside other text, e.g. the rule `exit=<結束碼>`.

    A span holding nothing but a placeholder (`<SHA>`) is still an unfilled slot, so it stays.
    """
    return INLINE_CODE_RE.sub(lambda m: m.group(0) if PLACEHOLDER_RE.fullmatch(m.group(1).strip()) else "", text)


def log_round(name: str) -> int:
    m = ROUND_LOG_RE.match(name)
    return int(m.group(1)) if m else 1


def archive_logs(sdir: Path) -> str | None:
    """A redo from base starts with empty logs; the failed attempt's logs move to logs-attempt<n>/."""
    logs = sdir / "logs"
    if not logs.is_dir() or not any(logs.iterdir()):
        return None
    n = 1
    while (sdir / f"logs-attempt{n}").exists():
        n += 1
    dest = sdir / f"logs-attempt{n}"
    logs.rename(dest)
    logs.mkdir()
    return str(dest)


def cmd_verify_gate(args: argparse.Namespace) -> int:
    repo = Repo(args.project)
    report = Path(args.report).resolve()
    logs_dir = Path(args.logs_dir).resolve() if args.logs_dir else repo.stage_dir(args.stage) / "logs"
    stage_rec = (repo.load_state().get("stages") or {}).get(args.stage) or {}
    rnd = args.round or int(stage_rec.get("round", 1))
    checks: list[dict[str, Any]] = []
    marker_ok, matched = check_report_marker(report, args.marker)
    checks.append({"name": "report_marker", "pass": marker_ok, "report": str(report), "matched": matched})
    hits = sorted(set(PLACEHOLDER_RE.findall(unquoted(report.read_text(encoding="utf-8", errors="replace"))))) if report.exists() else []
    checks.append({"name": "no_placeholders", "pass": report.exists() and not hits, "hits": hits})
    # Only this round's logs: a failure fixed in a later round must not fail every gate after it.
    all_logs = sorted(logs_dir.glob("*.log")) if logs_dir.exists() else []
    log_files = [lf for lf in all_logs if log_round(lf.name) == rnd]
    other_rounds = [lf.name for lf in all_logs if log_round(lf.name) != rnd]
    if not log_files and not args.allow_empty_logs:
        checks.append({"name": "logs_present", "pass": False, "logs_dir": str(logs_dir), "round": rnd,
                       "expected": "*.log (no r<n>_ prefix)" if rnd == 1 else f"r{rnd}_*.log",
                       "other_rounds": other_rounds})
    else:
        bad, checked = [], []
        for lf in log_files:
            lines = [ln.strip() for ln in lf.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
            last = lines[-1] if lines else ""
            m = re.fullmatch(r"exit=(-?\d+)", last)
            is_mut = "mutation" in lf.name.lower()
            if not m:
                bad.append({"file": str(lf), "reason": "missing final exit=<code> line", "last_line": last})
                continue
            code = int(m.group(1))
            good = (code != 0) if is_mut else (code == 0)
            checked.append({"file": lf.name, "exit_code": code, "is_mutation": is_mut, "pass": good})
            if not good:
                bad.append({"file": str(lf), "exit_code": code, "is_mutation": is_mut})
        checks.append({"name": "logs_exit_codes", "pass": not bad, "round": rnd, "checked": checked, "failures": bad,
                       "other_rounds": other_rounds})
    if args.commit:
        ok = run_cmd(["git", "-C", str(repo.project), "cat-file", "-e", f"{args.commit}^{{commit}}"], timeout=5.0)["ok"]
        checks.append({"name": "commit_exists", "pass": ok, "commit": args.commit})
    all_pass = all(c["pass"] for c in checks)
    lines = [f"Verify-gate for stage '{args.stage}' round {rnd}: {'PASS' if all_pass else 'FAIL'}"]
    lines += [f"  - {c['name']}: {'ok' if c['pass'] else 'FAIL'}" for c in checks]
    return emit(args, {"pass": all_pass, "stage": args.stage, "round": rnd, "checks": checks}, lines,
                exit_code=0 if all_pass else 1)



def cmd_next_round(args: argparse.Namespace) -> int:
    """Open the next fix round after a failed review; at max_review_rounds refuse and point at reset-stage."""
    repo = Repo(args.project)
    cap = int(repo.load_config().get("max_review_rounds", DEFAULT_MAX_REVIEW_ROUNDS))
    ruling = Path(args.ruling).resolve()
    if not ruling.is_file():
        print(f"Error: ruling file does not exist: {ruling}", file=sys.stderr)
        return 1
    now_iso = iso_str(utc_now())
    with repo.lock():
        state = repo.load_state()
        stage = (state.get("stages") or {}).get(args.stage)
        if not stage:
            print(f"Error: stage '{args.stage}' not found", file=sys.stderr)
            return 1
        if str(stage.get("status", "")).startswith("halted"):
            return emit(args, {"ok": False, "error_code": "STAGE_HALTED", "halt": stage.get("halt")},
                        [f"Error: stage '{args.stage}' is halted; Owner must rule via `reset-stage --owner-ack`."], exit_code=1)
        cur = int(stage.get("round", 1))
        history = stage.setdefault("round_history", [])
        if cur >= cap:
            if stage.get("status") != ROUND_LIMIT_STATUS:
                stage["status"] = ROUND_LIMIT_STATUS
                history.append({"at": now_iso, "event": "round_limit", "round": cur, "ruling": str(ruling)})
                repo.save_state(state)
            return emit(args, {"ok": False, "error_code": "ROUND_LIMIT", "round": cur, "cap": cap, "round_history": history},
                        [f"ROUND LIMIT: round {cur}/{cap} failed review; do not open round {cur + 1}. Find the brief gap "
                         "across the review reports, then `reset-stage --reason \"<gap>\"` and redo from base."], exit_code=2)
        stage.update(round=cur + 1, status=f"round:{cur + 1}")
        history.append({"at": now_iso, "event": "next_round", "round": cur + 1, "ruling": str(ruling)})
        repo.save_state(state)
    n = cur + 1
    return emit(args, {"ok": True, "stage": args.stage, "round": n, "cap": cap, "round_history": history},
                [f"Stage '{args.stage}' round {n}/{cap}: write brief_r{n}.md; its logs go to logs/r{n}_*.log."])



def cmd_reset_stage(args: argparse.Namespace) -> int:
    """Count a from-base redo of a stage; halt instead of redoing once the cap is hit."""
    repo = Repo(args.project)
    cap = int(repo.load_config().get("max_stage_resets", DEFAULT_MAX_STAGE_RESETS))
    now_iso = iso_str(utc_now())
    with repo.lock():
        state = repo.load_state()
        stage = (state.get("stages") or {}).get(args.stage)
        if not stage:
            print(f"Error: stage '{args.stage}' not found", file=sys.stderr)
            return 1
        history = stage.setdefault("reset_history", [])
        halted = str(stage.get("status", "")).startswith("halted")
        if args.owner_ack:
            if not halted:
                print(f"Error: stage '{args.stage}' is not halted", file=sys.stderr)
                return 1
            stage.update(reset_count=0, status="briefing", round=1)
            stage.pop("halt", None)
            archived = archive_logs(repo.stage_dir(args.stage))
            history.append({"at": now_iso, "event": "owner_ack", "note": args.owner_ack, "archived_logs": archived})
            repo.save_state(state)
            return emit(args, {"ok": True, "stage": args.stage, "resumed": True, "archived_logs": archived},
                        [f"Stage '{args.stage}' resumed by Owner ruling; reset budget restored to {cap}."])
        if halted:
            return emit(args, {"ok": False, "error_code": "STAGE_HALTED", "halt": stage.get("halt")},
                        [f"Error: stage '{args.stage}' already halted."], exit_code=1)
        if not args.reason:
            print("Error: --reason is required (the structural brief gap behind the round-3 failure)", file=sys.stderr)
            return 1
        count = int(stage.get("reset_count", 0))
        if count >= cap:
            stage["status"] = "halted:owner_required"
            stage["halt"] = {"at": now_iso, "reset_count": count, "cap": cap, "last_reason": args.reason}
            history.append({"at": now_iso, "event": "halt", "note": args.reason})
            repo.save_state(state)
            return emit(args, {"ok": False, "error_code": "STAGE_HALTED", "halt": stage["halt"], "reset_history": history},
                        [f"HALT: stage '{args.stage}' already reset {count}/{cap} times; escalate to Owner, "
                         "then resume via `reset-stage --owner-ack \"<ruling>\"`."], exit_code=2)
        stage.update(reset_count=count + 1, round=1, status="briefing")
        archived = archive_logs(repo.stage_dir(args.stage))
        history.append({"at": now_iso, "event": "reset", "note": args.reason, "archived_logs": archived})
        repo.save_state(state)
    return emit(args, {"ok": True, "stage": args.stage, "reset_count": count + 1, "cap": cap, "reset_history": history,
                       "archived_logs": archived},
                [f"Stage '{args.stage}' reset {count + 1}/{cap}; revise brief, drop old branch/worktree, redispatch."])
