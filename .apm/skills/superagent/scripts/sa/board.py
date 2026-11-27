"""The board: posts, push notices and the worker instructions that point at them."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .adaptors import ADAPTORS
from .ade import notify
from .constants import ROUTER, WATCHER
from .store import SCRIPT, Repo, home_env_argv, iso_str, runner_argv, utc_now


class EscalationRequiredError(Exception):
    """Raised when the Router attempts to self-close an escalated objection without Owner acknowledgement."""

    def __init__(self, seq: int, reason: str, message: str):
        super().__init__(message)
        self.seq = seq
        self.reason = reason


def member_endpoint(members: dict[str, Any], name: str) -> tuple[str | None, str | None]:
    if name == ROUTER:
        r = members.get("router") or {}
        return r.get("ade"), r.get("ref")
    w = (members.get("workers") or {}).get(name) or {}
    return w.get("ade"), w.get("backend_ref")


def cli_cmd(sub: str, repo: Repo, *rest: str) -> str:
    """The exact command line a worker or the Router runs; absolute paths so any cwd works."""
    parts = [*home_env_argv(), *runner_argv(), str(SCRIPT), sub, "--project", str(repo.project), *rest]
    return " ".join(_shq(p) for p in parts)


def _shq(s: str) -> str:
    return s if re.fullmatch(r"[A-Za-z0-9_./:=@%+,-]+", s) else "'" + s.replace("'", "'\"'\"'") + "'"


def notice_text(repo: Repo, post: dict[str, Any], reader: str) -> str:
    first = (post.get("text") or "").strip().splitlines()[0:1]
    head = (first[0] if first else "")[:140]
    to_label = ",".join(post["to"]) if post.get("to") else "(stage)"
    return (f"[superagent {repo.key}] #{post['seq']} {post['from']} → {to_label} ({post['kind']}): {head} "
            f"— 讀全文：{cli_cmd('read', repo, '--as', reader)}")


def _same_scope(post: dict[str, Any], stage: str | None, attempt: int) -> bool:
    if stage is None:
        return True
    return post.get("stage") == stage and int(post.get("attempt", 1)) == attempt


def is_escalated_objection(posts: list[dict[str, Any]], obj: dict[str, Any]) -> tuple[bool, str | None]:
    """An objection cannot be self-closed by the Router if it is a re-objection or backed by 2+ distinct workers."""
    by_seq = {p["seq"]: p for p in posts}
    ref = obj.get("reply_to")
    if ref is not None:
        parent = by_seq.get(ref)
        if parent and parent.get("from") == ROUTER:
            if parent.get("kind") == "answer":
                return True, "re_objection"
            if parent.get("kind") == "ruling":
                gp_ref = parent.get("reply_to")
                gp = by_seq.get(gp_ref) if gp_ref is not None else None
                if gp and gp.get("kind") == "objection":
                    return True, "re_objection"
        stage, attempt = obj.get("stage"), int(obj.get("attempt", 1))
        objectors = {
            p["from"] for p in posts
            if p.get("kind") == "objection" and p.get("reply_to") == ref and _same_scope(p, stage, attempt)
        }
        if len(objectors) >= 2:
            return True, "multi_worker_objection"
    return False, None


def evaluate_objections(posts: list[dict[str, Any]], stage: str, attempt: int) -> dict[str, Any]:
    """Check whether every objection in (stage, attempt) has a valid resolution."""
    scoped = [p for p in posts if _same_scope(p, stage, attempt)]
    objections = [p for p in scoped if p.get("kind") == "objection"]
    unresolved: list[dict[str, Any]] = []
    for obj in objections:
        escalated, reason = is_escalated_objection(scoped, obj)
        resolved = False
        for p in scoped:
            if p["seq"] <= obj["seq"] or p.get("from") != ROUTER:
                continue
            direct = p.get("reply_to") == obj["seq"]
            shared_owner_ruling = (
                p.get("kind") == "ruling" and bool(p.get("owner_ack"))
                and obj.get("reply_to") is not None and p.get("reply_to") == obj["reply_to"]
            )
            if not (direct or shared_owner_ruling):
                continue
            if escalated:
                if p.get("kind") == "ruling" and p.get("owner_ack"):
                    resolved = True
                    break
            elif p.get("kind") in ("answer", "ruling"):
                resolved = True
                break
        if not resolved:
            unresolved.append({
                "seq": obj["seq"],
                "from": obj["from"],
                "reply_to": obj.get("reply_to"),
                "escalated": escalated,
                "escalation_reason": reason,
                "requires_owner_ack": escalated,
                "text": (obj.get("text") or "").strip().splitlines()[0][:140] if (obj.get("text") or "").strip() else "",
            })
    return {"pass": not unresolved, "total": len(objections), "unresolved": unresolved}


def evaluate_ruling_acks(posts: list[dict[str, Any]], stage: str, attempt: int) -> dict[str, Any]:
    """Check whether targeted rulings sent to ready workers received a confirmation note or objection."""
    scoped = [p for p in posts if _same_scope(p, stage, attempt)]
    pending: list[dict[str, Any]] = []
    for rp in scoped:
        req = rp.get("require_ack_from") or []
        if rp.get("kind") != "ruling" or not req:
            continue
        overridden = any(
            p["seq"] > rp["seq"] and p.get("from") == ROUTER and p.get("kind") == "ruling"
            and p.get("reply_to") == rp["seq"] and p.get("owner_ack")
            for p in scoped
        )
        if overridden:
            continue
        for wid in req:
            acked = any(
                p["seq"] > rp["seq"] and p.get("from") == wid and p.get("reply_to") == rp["seq"]
                and p.get("kind") in ("note", "objection")
                for p in scoped
            )
            if not acked:
                pending.append({"ruling_seq": rp["seq"], "worker_id": wid})
    return {"pass": not pending, "pending": pending}


def stage_rulings(posts: list[dict[str, Any]], stage_rec: dict[str, Any], stage: str, attempt: int) -> list[dict[str, Any]]:
    """Collect prior rulings for (stage, attempt) from the board and round_history for dispatch prompt injection."""
    rulings: list[dict[str, Any]] = []
    seen_files: set[str] = set()
    for p in posts:
        if p.get("kind") == "ruling" and _same_scope(p, stage, attempt):
            f = p.get("file")
            if f:
                seen_files.add(str(f))
            first = (p.get("text") or "").strip().splitlines()[0:1]
            rulings.append({
                "seq": p["seq"],
                "round": p.get("round"),
                "file": f,
                "summary": (first[0] if first else "")[:140],
                "owner_ack": p.get("owner_ack"),
            })
    for h in stage_rec.get("round_history") or []:
        if int(h.get("attempt", 1)) != attempt:
            continue
        f = h.get("ruling")
        if f and str(f) not in seen_files:
            seen_files.add(str(f))
            rulings.append({"seq": None, "round": h.get("round"), "file": str(f), "summary": "", "owner_ack": None})
    return rulings


def post_message(repo: Repo, sender: str, to: list[str], kind: str, text: str, *,
                 extra: dict[str, Any] | None = None, stage: str | None = None, reply_to: int | None = None,
                 owner_ack: str | None = None, push: bool = True, dry_run: bool = False) -> dict[str, Any]:
    """Append one post, enforce objection/ruling rules, then push a short notice to each recipient."""
    unpushable_ack_workers: list[str] = []
    with repo.lock():
        members = repo.load_members()
        state = repo.load_state()
        posts = repo.read_board()
        by_seq = {p["seq"]: p for p in posts}
        workers = members.get("workers") or {}
        known = {ROUTER, *workers}

        if reply_to is not None and reply_to not in by_seq:
            raise ValueError(f"unknown reply_to post #{reply_to}")
        if owner_ack and not (sender == ROUTER and kind == "ruling"):
            raise ValueError("--owner-ack is only valid for router ruling posts")

        # Resolve stage / round / attempt for the post.
        resolved_stage = stage
        if not resolved_stage and reply_to is not None:
            resolved_stage = by_seq[reply_to].get("stage")
        if not resolved_stage and sender in workers:
            resolved_stage = workers[sender].get("stage")
        if not resolved_stage:
            rec_stages = {workers[n].get("stage") for n in to if n in workers and workers[n].get("stage")}
            if len(rec_stages) == 1:
                resolved_stage = next(iter(rec_stages))
        if not resolved_stage:
            resolved_stage = state.get("active_stage")

        stage_rec = ((state.get("stages") or {}).get(resolved_stage) or {}) if resolved_stage else {}
        resolved_attempt = int(stage_rec.get("attempt", 1))
        if extra and "round" in extra:
            resolved_round = int(extra["round"])
        elif sender in workers and workers[sender].get("round") is not None:
            resolved_round = int(workers[sender]["round"])
        else:
            resolved_round = int(stage_rec.get("round", 1))

        broadcast_all = "all" in to
        if broadcast_all:
            allowed_statuses = ("active",) if kind == "ruling" else ("active", "ready")
            to = sorted(
                n for n, w in workers.items()
                if w.get("status") in allowed_statuses and n != sender
                and (stage is None or w.get("stage") == stage)
            )
        unknown = [n for n in to if n not in known]
        if unknown:
            raise ValueError(f"unknown recipient(s): {', '.join(unknown)}")
        if sender not in known and sender != WATCHER:
            raise ValueError(f"unknown sender: {sender}")

        # Prevent Router from self-closing an escalated objection without Owner acknowledgement.
        if sender == ROUTER and kind in ("answer", "ruling") and reply_to is not None:
            target = by_seq[reply_to]
            if target.get("kind") == "objection" and not owner_ack:
                escalated, reason = is_escalated_objection(posts, target)
                if escalated:
                    raise EscalationRequiredError(
                        reply_to,
                        reason or "escalated",
                        f"ESCALATION_REQUIRED: objection #{reply_to} ({reason}) cannot be closed by Router alone; "
                        f"ask the Owner and resolve via `post --as router --to {target['from']} --kind ruling "
                        f"--reply-to {reply_to} --owner-ack \"<裁定>\"`.",
                    )

        now_iso = iso_str(utc_now())
        # Targeted ruling sent to ready/closed worker(s) requires confirmation (or owner_ack) before verify-gate can pass.
        reactivated: list[str] = []
        require_ack_from: list[str] = []
        if sender == ROUTER and kind == "ruling" and not owner_ack and not broadcast_all:
            for name in to:
                if name not in workers:
                    continue
                w = workers[name]
                st = w.get("status")
                if st == "ready":
                    w["status"] = "active"
                    w["last_progress_at"] = now_iso
                    w["nudged_at"] = None
                    w["alert_count"] = 0
                    w["last_alert_sig"] = None
                    w["last_alert_at"] = None
                    reactivated.append(name)
                    require_ack_from.append(name)
                    adaptor = ADAPTORS.get(w.get("ade") or "")
                    if not adaptor or not adaptor.can_push or not w.get("backend_ref"):
                        unpushable_ack_workers.append(name)
                elif st == "closed":
                    require_ack_from.append(name)
                    unpushable_ack_workers.append(name)

        entry: dict[str, Any] = {"from": sender, "to": to, "kind": kind, "text": text, "time": now_iso}
        if resolved_stage:
            entry["stage"] = resolved_stage
            entry["round"] = resolved_round
            entry["attempt"] = resolved_attempt
        if reply_to is not None:
            entry["reply_to"] = reply_to
        if owner_ack:
            entry["owner_ack"] = owner_ack
        if require_ack_from:
            entry["require_ack_from"] = require_ack_from
        if extra:
            entry.update(extra)

        post = repo.append_board(entry)
        if sender in workers:
            w = workers[sender]
            w["last_post_seq"] = post["seq"]
            if kind == "done" and w.get("status") == "active":
                w["status"] = "ready"
                w["done_seq"] = post["seq"]
                w["completed_at"] = post["time"]
            elif (kind == "note" and reply_to is not None and w.get("status") == "active" and w.get("done_seq")
                  and sender in (by_seq[reply_to].get("require_ack_from") or [])):
                # Worker completed earlier and was only reactivated to confirm a ruling; return it to ready.
                w["status"] = "ready"
        repo.save_members(members)

    results = {}
    if push:
        for name in to:
            ade, ref = member_endpoint(members, name)
            results[name] = notify(ade, ref, notice_text(repo, post, name), dry_run=dry_run)
        with repo.lock():
            members = repo.load_members()
            for name, res in results.items():
                if res.get("sent"):
                    rec = members["router"] if name == ROUTER else members["workers"][name]
                    rec["last_notified_seq"] = max(int(rec.get("last_notified_seq", 0)), post["seq"])
            repo.save_members(members)
    payload: dict[str, Any] = {"post": post, "notified": results}
    if reactivated:
        payload["reactivated_workers"] = reactivated
    if unpushable_ack_workers:
        payload["unpushable_ack_workers"] = unpushable_ack_workers
    return payload


def worker_instructions(repo: Repo, wid: str, role: str, brief: Path, report: Path, *,
                        role_kind: str = "write", prior_rulings: list[dict[str, Any]] | None = None) -> str:
    post_done = cli_cmd("post", repo, "--as", wid, "--to", ROUTER, "--kind", "done", "--file", str(report))
    post_ask = cli_cmd("post", repo, "--as", wid, "--to", ROUTER, "--kind", "ask", "--text", "<問題>")
    post_obj = cli_cmd("post", repo, "--as", wid, "--to", ROUTER, "--kind", "objection", "--reply-to", "<seq>", "--text", "<反駁理由與證據>")
    post_ack = cli_cmd("post", repo, "--as", wid, "--to", ROUTER, "--kind", "note", "--reply-to", "<seq>", "--text", "同意裁定")
    read = cli_cmd("read", repo, "--as", wid)
    rulings_section = ""
    if prior_rulings:
        items = []
        for r in prior_rulings:
            seq_tag = f"#{r['seq']} " if r.get("seq") is not None else ""
            rnd_tag = f"(R{r['round']}) " if r.get("round") is not None else ""
            loc = r.get("file") or r.get("summary") or ""
            items.append(f"- {seq_tag}{rnd_tag}`{loc}`")
        review_note = (
            "\n**審查必做**：逐條核對上述裁定中被駁回的意見是否真的不成立；"
            "若依程式碼證據認為駁回不成立，務必用 `--kind objection --reply-to <seq>` 反駁（未獲回覆前 verify-gate 不會通過）。\n"
            if role_kind == "review" else
            "\n若發現上述裁定或 brief 設計有矛盾或錯誤，用 `--kind objection --reply-to <seq>` 反駁。\n"
        )
        rulings_section = "\n## 本階段既有裁定（Rulings）\n" + "\n".join(items) + "\n" + review_note

    return (
        f"# superagent worker「{wid}」\n\n"
        f"你是 superagent 的 worker，角色 `{role}`（`{role_kind}`），專案 `{repo.project}`。\n"
        f"{rulings_section}\n"
        f"1. 讀 `{brief}` 並照做。\n"
        f"2. 完成後把報告寫到 `{report}`，再執行下面這行回報，只執行一次（會先在本地檢查標記、佔位符與既有日誌 `exit=`）：\n\n"
        f"   ```\n   {post_done}\n   ```\n\n"
        f"3. 需要 Router 決定才能繼續時，執行下面這行，然後結束回合等待；收到通知後再讀回覆：\n\n"
        f"   ```\n   {post_ask}\n   {read}\n   ```\n\n"
        f"4. 反駁 Router 的裁定或 brief 設計時（針對特定 post 加 `--reply-to <seq>`，針對 brief 本身可省略 `--reply-to`）：\n\n"
        f"   ```\n   {post_obj}\n   ```\n\n"
        f"5. 若完成後又收到 Router 的 `ruling` 通知，先 `read`，同意就回 `--kind note --reply-to <seq>`，不同意就回 `--kind objection --reply-to <seq>`：\n\n"
        f"   ```\n   {post_ack}\n   ```\n\n"
        f"6. 被通知有新訊息時，先執行 `read` 再繼續。不要直接對使用者說話；交接都經過這些指令。\n"
    )
