"""Run: uv run python -m unittest discover -s <skill>/tests"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

from sa.adaptors import ADAPTORS, LaunchRequest, get_adaptor  # noqa: E402
from sa.ade import ensure_watcher, notify  # noqa: E402
from sa.board import cli_cmd, notice_text, post_message  # noqa: E402
from sa.hooks import stop_decision  # noqa: E402
from sa.store import Repo, parse_remote_key  # noqa: E402
from sa.watch import watch_tick  # noqa: E402
from superagent import main  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home, self.proj = root / "home", root / "proj"
        self.home.mkdir()
        self.proj.mkdir()
        for d in (self.home, self.proj):
            subprocess.run(["git", "-C", str(d), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(self.proj), "remote", "add", "origin", "git@github.com:own/demo.git"], check=True)
        self.env = {k: os.environ.pop(k, None) for k in ("SUPERAGENT_HOME", "HC_TERMINAL_ID", "ORCA_TERMINAL_HANDLE")}
        os.environ["SUPERAGENT_HOME"] = str(self.home)
        self.run_cli("init", "--use-defaults")
        self.run_cli("stage-new", "--stage", "s1", "--goal", "g", "--base", "HEAD")
        self.repo = Repo(self.proj)

    def tearDown(self) -> None:
        for k, v in self.env.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        self.tmp.cleanup()

    def run_cli(self, *argv: str) -> int:
        return main([argv[0], "--project", str(self.proj), *argv[1:]])

    def add_worker(self, wid: str, ade: str = "hc", marker: str | None = None) -> None:
        with self.repo.lock():
            m = self.repo.load_members()
            m["workers"][wid] = {"id": wid, "stage": "s1", "role": "impl", "ade": ade, "backend_ref": wid,
                                 "report_path": str(self.repo.stage_dir("s1") / f"{wid}.md"), "expected_marker": marker,
                                 "watch_logs": [], "status": "active", "last_progress_at": "2026-01-01T00:00:00Z",
                                 "last_read_seq": 0, "alert_count": 0}
            self.repo.save_members(m)

    def tick(self, now: str, probes: dict) -> dict:
        mock = Path(self.tmp.name) / "probe.json"
        mock.write_text(json.dumps(probes))
        return watch_tick(self.repo, Namespace(now=now, mock_probe=str(mock), auto_nudge=True))


class RemoteKey(unittest.TestCase):
    def test_forms(self) -> None:
        self.assertEqual(parse_remote_key("git@github.com:own/demo.git"), "own-demo")
        self.assertEqual(parse_remote_key("https://gitlab.x/grp/sub/demo"), "grp-sub-demo")
        self.assertEqual(parse_remote_key("ssh://git@host:22/own/demo.git"), "own-demo")
        self.assertIsNone(parse_remote_key("not a url"))


class Board(Base):
    def test_store_location_and_ignore(self) -> None:
        self.assertEqual(self.repo.dir, self.home / ".superagent" / "own-demo")
        self.assertIn(".superagent/", (self.home / ".gitignore").read_text())

    def test_post_read_cursor_and_done(self) -> None:
        self.add_worker("w1")
        self.add_worker("w2")
        post_message(self.repo, "w1", ["router"], "done", "ok")
        post_message(self.repo, "router", ["all"], "note", "hi")
        m = self.repo.load_members()
        self.assertEqual(m["workers"]["w1"]["status"], "ready")
        # `all` expands to live workers other than the sender; w1 is ready, so still included.
        self.assertEqual(self.repo.read_board()[-1]["to"], ["w1", "w2"])
        self.assertEqual(self.run_cli("read", "--as", "w2"), 0)
        self.assertEqual(self.repo.load_members()["workers"]["w2"]["last_read_seq"], 2)

    def test_unknown_recipient_rejected(self) -> None:
        with self.assertRaises(ValueError):
            post_message(self.repo, "router", ["ghost"], "note", "x")

    def test_home_override_travels_with_commands(self) -> None:
        self.assertTrue(cli_cmd("read", self.repo, "--as", "w1").startswith(f"env SUPERAGENT_HOME={self.home}"))

    def test_worker_commands_prefer_uv_without_cache(self) -> None:
        # A sandboxed worker cannot write uv's cache: uv must skip it, and python3 is the fallback without uv.
        with mock.patch("sa.store.shutil.which", return_value="/usr/bin/uv"):
            self.assertIn(" uv run --no-cache ", cli_cmd("post", self.repo, "--as", "w1"))
            post = {"seq": 1, "from": "w1", "to": ["router"], "kind": "done", "text": "ok"}
            self.assertIn(" uv run --no-cache ", notice_text(self.repo, post, "router"))  # the pushed `read` command
            self.assertIn("--no-cache", ensure_watcher(self.repo, dry_run=True)["command"])
        with mock.patch("sa.store.shutil.which", return_value=None):
            cmd = cli_cmd("post", self.repo, "--as", "w1")
        self.assertIn(" python3 ", cmd)
        self.assertNotIn("uv run", cmd)


    def test_watcher_does_not_need_router_on_hc(self) -> None:
        with self.repo.lock():
            m = self.repo.load_members()
            m["router"].update(ade="orca", ref="term_x")
            self.repo.save_members(m)
        res = ensure_watcher(self.repo, dry_run=True)
        self.assertTrue(res["started"])
        self.assertIn("--loop", res["command"])

class Watch(Base):
    def test_stopped_worker_nudged_once_then_alerts(self) -> None:
        self.add_worker("w1")
        probe = {"w1": {"agent_status": "idle", "screen_text": "same"}}
        self.tick("2026-01-01T00:00:00Z", probe)  # first probe counts as progress
        r = self.tick("2026-01-01T00:04:00Z", probe)
        self.assertEqual(r["events"][0]["event"], "AUTO_NUDGED")
        self.assertTrue(r["events"][0]["nudge_result"]["dry_run"])
        r = self.tick("2026-01-01T00:08:00Z", probe)
        self.assertEqual(r["events"][0]["event"], "STOPPED_NO_POST")
        self.assertTrue(r["events"][0]["should_notify"])
        self.assertEqual(self.repo.read_board()[-1]["kind"], "alert")

    def test_marker_without_post_is_posted_for_worker(self) -> None:
        self.add_worker("w1", marker="IMPL DONE")
        Path(self.repo.load_members()["workers"]["w1"]["report_path"]).write_text("x\nIMPL DONE\n")
        self.tick("2026-01-01T00:00:00Z", {})
        last = self.repo.read_board()[-1]
        self.assertEqual((last["from"], last["kind"], last["by"]), ("w1", "done", "watch"))
        self.assertEqual(self.repo.load_members()["workers"]["w1"]["status"], "ready")

    def test_router_unread_alerts_once_per_seq(self) -> None:
        self.add_worker("w1", ade="native")
        post_message(self.repo, "w1", ["router"], "ask", "?")
        later = "2099-01-01T00:00:00Z"
        events = [e["event"] for e in self.tick(later, {})["events"]]
        self.assertIn("ROUTER_UNREAD", events)
        events = [e["event"] for e in self.tick(later, {})["events"]]
        self.assertNotIn("ROUTER_UNREAD", events)

    def test_silent_hang(self) -> None:
        self.add_worker("w1")
        probe = {"w1": {"agent_status": "working", "screen_text": "same"}}
        self.tick("2026-01-01T00:00:00Z", probe)
        r = self.tick("2026-01-01T00:11:00Z", probe)
        self.assertEqual(r["events"][0]["event"], "SILENT_HANG")
        self.assertTrue(Path(r["events"][0]["incident_path"]).is_file())


class Stage(Base):
    def test_reset_cap_halts_dispatch(self) -> None:
        self.assertEqual(self.run_cli("set-role", "--role", "impl", "--kind", "write", "--confirm"), 0)
        self.assertEqual(self.run_cli("reset-stage", "--stage", "s1", "--reason", "a"), 0)
        self.assertEqual(self.run_cli("reset-stage", "--stage", "s1", "--reason", "b"), 0)
        self.assertEqual(self.run_cli("reset-stage", "--stage", "s1", "--reason", "c"), 2)
        self.assertEqual(self.run_cli("dispatch", "--stage", "s1", "--role", "impl", "--dry-run"), 1)
        self.assertEqual(self.run_cli("reset-stage", "--stage", "s1", "--owner-ack", "ok"), 0)
        self.assertEqual(self.run_cli("dispatch", "--stage", "s1", "--role", "impl", "--dry-run"), 0)


class Rounds(Base):
    def gate(self, report: Path, *extra: str) -> dict:
        out = Path(self.tmp.name) / "gate.json"
        self.run_cli("verify-gate", "--stage", "s1", "--report", str(report), "--marker", "IMPL DONE",
                     "--output", str(out), *extra)
        return json.loads(out.read_text())

    def log(self, name: str, code: int) -> None:
        (self.repo.stage_dir("s1") / "logs" / name).write_text(f"...\nexit={code}\n")

    def ruling(self, n: int) -> str:
        path = self.repo.stage_dir("s1") / f"ruling_r{n}.md"
        path.write_text("FAIL")
        return str(path)

    def test_quoted_placeholder_is_not_a_hit(self) -> None:
        report = self.repo.stage_dir("s1") / "r.md"
        self.log("test.log", 0)
        report.write_text("每份 log 結尾都是 `exit=<結束碼>`。\nIMPL DONE\n")
        self.assertTrue(self.gate(report)["pass"])
        report.write_text("commit: `<SHA>`\nIMPL DONE\n")  # a span that is only a placeholder is unfilled
        self.assertEqual(self.gate(report)["checks"][1]["hits"], ["<SHA>"])
        report.write_text("commit: <SHA>\nIMPL DONE\n")
        self.assertFalse(self.gate(report)["pass"])

    def test_gate_checks_only_current_round_logs(self) -> None:
        report = self.repo.stage_dir("s1") / "r.md"
        report.write_text("IMPL DONE\n")
        self.log("install.log", 1)
        self.assertFalse(self.gate(report)["pass"])
        self.assertEqual(self.run_cli("next-round", "--stage", "s1", "--ruling", self.ruling(1)), 0)
        res = self.gate(report)
        self.assertFalse(res["pass"])  # round 2 has no r2_ logs yet
        self.assertEqual(res["checks"][2]["expected"], "r2_*.log")
        self.log("r2_install.log", 0)
        res = self.gate(report)
        self.assertTrue(res["pass"])
        self.assertEqual(res["checks"][2]["other_rounds"], ["install.log"])
        self.assertFalse(self.gate(report, "--round", "1")["pass"])

    def test_round_cap_blocks_fourth_round_until_reset(self) -> None:
        self.assertEqual(self.run_cli("set-role", "--role", "fix", "--kind", "write", "--confirm"), 0)
        self.assertEqual(self.run_cli("next-round", "--stage", "s1", "--ruling", "/nonexistent.md"), 1)
        self.assertEqual(self.run_cli("next-round", "--stage", "s1", "--ruling", self.ruling(1)), 0)
        self.assertEqual(self.run_cli("next-round", "--stage", "s1", "--ruling", self.ruling(2)), 0)
        self.assertEqual(self.repo.load_state()["stages"]["s1"]["round"], 3)
        self.assertEqual(self.run_cli("stage-new", "--stage", "s1", "--goal", "g"), 0)
        self.assertEqual(self.repo.load_state()["stages"]["s1"]["round"], 3)  # recreating keeps the round
        self.assertEqual(self.run_cli("next-round", "--stage", "s1", "--ruling", self.ruling(3)), 2)
        self.assertEqual(self.repo.load_state()["stages"]["s1"]["status"], "round_limit")
        self.assertEqual(self.run_cli("dispatch", "--stage", "s1", "--role", "fix", "--dry-run"), 1)
        self.assertEqual(self.run_cli("stage-new", "--stage", "s1", "--goal", "g", "--force"), 1)  # no bypass
        self.log("test.log", 1)
        self.assertEqual(self.run_cli("reset-stage", "--stage", "s1", "--reason", "gap"), 0)
        st = self.repo.load_state()["stages"]["s1"]
        self.assertEqual((st["round"], st["status"]), (1, "briefing"))
        # The failed attempt's logs move aside so the redo's round 1 starts clean.
        self.assertEqual(list((self.repo.stage_dir("s1") / "logs").iterdir()), [])
        self.assertTrue((self.repo.stage_dir("s1") / "logs-attempt1" / "test.log").is_file())
        self.assertEqual(self.run_cli("dispatch", "--stage", "s1", "--role", "fix", "--dry-run"), 0)


class Redispatch(Base):
    def dispatch(self, *extra: str) -> tuple[int, dict]:
        out = Path(self.tmp.name) / "d.json"
        out.unlink(missing_ok=True)
        code = self.run_cli("dispatch", "--stage", "s1", "--role", "impl", "--marker", "IMPL DONE",
                            "--output", str(out), *extra)
        return code, json.loads(out.read_text()) if out.exists() else {}

    def test_same_role_gets_a_fresh_report(self) -> None:
        # Real (non-dry-run) dispatch on native: prompt, members and board are written as for any ADE.
        self.run_cli("set-role", "--role", "impl", "--kind", "write", "--ade", "native", "--confirm")
        sdir = self.repo.stage_dir("s1")
        code, d1 = self.dispatch()
        self.assertEqual(code, 0)
        r1 = Path(d1["worker"]["report_path"])
        self.assertEqual(r1, sdir / "report-impl.md")
        r1.write_text("old\nIMPL DONE\n")
        post_message(self.repo, "impl-s1", ["router"], "done", "ok", push=False)
        self.run_cli("close-worker", "--worker", "impl-s1")
        code, d2 = self.dispatch()
        self.assertEqual(code, 0)
        r2 = Path(d2["worker"]["report_path"])
        self.assertEqual(r2, sdir / "report-impl-r2.md")
        self.assertIn(str(r2), Path(d2["worker"]["prompt_path"]).read_text())
        self.assertEqual(r1.read_text(), "old\nIMPL DONE\n")  # the old report is kept
        # The old report's marker no longer makes watch take the new worker as done.
        events = [e["event"] for e in self.tick("2026-01-01T00:00:00Z", {})["events"]]
        self.assertNotIn("MARKER_WITHOUT_POST", events)
        self.assertEqual(self.repo.load_members()["workers"]["impl-s1"]["status"], "active")
        # A parallel worker of the same role (--name) must not share a report that is not written yet.
        code, d3 = self.dispatch("--name", "impl-s1-b")
        self.assertEqual(Path(d3["worker"]["report_path"]), sdir / "report-impl-r3.md")
        code, _ = self.dispatch("--name", "impl-s1-c", "--report", str(r1))
        self.assertEqual(code, 1)  # an explicit report that already exists is refused


class Roles(Base):
    def test_roles_start_empty_and_are_created_per_task(self) -> None:
        self.assertEqual(self.repo.load_config()["roles"], {})
        self.assertEqual(self.run_cli("dispatch", "--stage", "s1", "--role", "migration", "--dry-run"), 1)
        self.assertEqual(self.run_cli("set-role", "--role", "migration"), 1)  # a new role needs --kind
        self.assertEqual(self.run_cli("set-role", "--role", "Bad Name", "--kind", "write", "--confirm"), 1)
        args = ["set-role", "--role", "perf-review", "--kind", "review", "--cli", "agy", "--model", "gemini-3.8-pro",
                "--purpose", "效能審查"]
        self.assertEqual(self.run_cli(*args), 3)  # only a proposal until the Owner confirms
        self.assertNotIn("perf-review", self.repo.load_config()["roles"])
        self.assertEqual(self.run_cli(*args, "--confirm"), 0)
        role = self.repo.load_config()["roles"]["perf-review"]
        self.assertEqual((role["kind"], role["ade"], role["cli"], role["model"], role["effort"], role["purpose"]),
                         ("review", "hc", "agy", "gemini-3.8-pro", "high", "效能審查"))
        self.assertEqual(self.run_cli("set-role", "--role", "perf-review", "--effort", "low"), 0)  # update keeps kind
        self.assertEqual(self.repo.load_config()["roles"]["perf-review"]["cli"], "agy")
        self.assertEqual(self.run_cli("dispatch", "--stage", "s1", "--role", "perf-review", "--dry-run"), 0)

    def test_kind_defaults_follow_config(self) -> None:
        cfg = self.repo.load_config()
        cfg["kinds"]["write"]["model"] = "sonnet"
        self.repo.config.write_text(json.dumps(cfg))
        self.run_cli("set-role", "--role", "impl", "--kind", "write", "--confirm")
        self.assertEqual(self.repo.load_config()["roles"]["impl"]["model"], "sonnet")


class StopHook(Base):
    def decide(self, wid: str = "w1") -> str | None:
        with self.repo.lock():
            return stop_decision(self.repo, wid)

    def test_blocks_until_worker_posts(self) -> None:
        self.add_worker("w1")
        post_message(self.repo, "router", ["w1"], "brief", "do it", push=False)
        with self.repo.lock():
            m = self.repo.load_members()
            m["workers"]["w1"]["last_read_seq"] = 1  # dispatch starts the cursor past the brief
            self.repo.save_members(m)
        self.assertIn("--kind done", self.decide())
        self.assertIsNone(self.decide())  # same board state is blocked only once
        post_message(self.repo, "w1", ["router"], "ask", "A or B?", push=False)
        self.assertIsNone(self.decide())  # waiting on the Router is a legal stop
        post_message(self.repo, "router", ["w1"], "answer", "A", push=False)
        self.assertIn("read", self.decide())  # unread answer comes first
        self.run_cli("read", "--as", "w1")
        self.assertIn("--kind done", self.decide())  # read but still owes a post
        post_message(self.repo, "w1", ["router"], "done", "ok", push=False)
        self.assertIsNone(self.decide())

    def test_claude_hc_worker_gets_settings(self) -> None:
        self.run_cli("set-role", "--role", "impl", "--kind", "write", "--confirm")
        out = Path(self.tmp.name) / "d.json"
        self.run_cli("dispatch", "--stage", "s1", "--role", "impl", "--dry-run", "--output", str(out))
        self.assertIn("--settings", json.loads(out.read_text())["command"])


class Screen(unittest.TestCase):
    def test_add_dir_trust_prompt_is_interactive(self) -> None:
        from sa.screen import detect_interactive_block
        lines = [" Adding a new directory:", " /tmp/x", " This session hasn't worked here before. Is this a directory you created or one you trust?",
                 " ❯ No, exit", "   Yes, add it"]
        self.assertEqual(detect_interactive_block(lines)["pattern"], "trust_or_auth")


class Adaptors(unittest.TestCase):
    def req(self, **kw: object) -> LaunchRequest:
        base = dict(wid="w", role="impl", stage="s1", workdir=Path("/p"), prompt_file=Path("/s/w.prompt.md"),
                    instructions="do it", cli="claude", model="opus", effort="high")
        return LaunchRequest(**{**base, **kw})  # type: ignore[arg-type]

    def test_every_adaptor_launches(self) -> None:
        for name, adaptor in ADAPTORS.items():
            plan = adaptor.launch(self.req())
            self.assertTrue(plan.command or plan.native_spawn, name)

    def test_hc_label_only_with_space(self) -> None:
        self.assertNotIn("--label", get_adaptor("hc").launch(self.req()).command)
        cmd = get_adaptor("hc").launch(self.req(herdr_space="sp1")).command
        self.assertIn("herdr.space=sp1", cmd)
        self.assertIn("--outlive-parent", cmd)

    def test_orca_rules(self) -> None:
        orca = get_adaptor("orca")
        self.assertEqual(orca.validate(None, "high"), "ORCA_EFFORT_NEEDS_MODEL")
        cmd = orca.launch(self.req(cli="agy", branch="feat/x")).command
        self.assertIn("antigravity", cmd)
        self.assertIn("new-child", cmd)
        self.assertEqual(orca.parse_ref('{"result": {"dispatch": {"dispatchId": "d1"}}}', "w"), "d1")
        self.assertNotIn("--effort", orca.launch(self.req(effort="none")).command)
        self.assertNotIn("--effort", get_adaptor("hc").launch(self.req(effort="none")).command)

    def test_native_has_no_push_or_probe(self) -> None:
        native = get_adaptor("native")
        self.assertFalse(native.can_push or native.can_probe or native.needs_pinned_model)
        self.assertFalse(notify("native", "x", "hi")["sent"])
        with self.assertRaises(ValueError):
            get_adaptor("tmux")


class RulingsAndObjections(Base):
    def gate(self, report: Path, marker: str = "IMPL DONE", *extra: str) -> tuple[int, dict]:
        out = Path(self.tmp.name) / "gate.json"
        out.unlink(missing_ok=True)
        code = self.run_cli("verify-gate", "--stage", "s1", "--report", str(report), "--marker", marker,
                            "--output", str(out), *extra)
        return code, json.loads(out.read_text())

    def check_by_name(self, payload: dict, name: str) -> dict:
        return next(c for c in payload["checks"] if c["name"] == name)

    def make_commit(self) -> str:
        f = self.proj / "a.txt"
        f.write_text("hi\n")
        subprocess.run(["git", "-C", str(self.proj), "add", "a.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(self.proj), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"],
            check=True,
        )
        return subprocess.run(
            ["git", "-C", str(self.proj), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
        ).stdout.strip()

    def test_next_round_posts_ruling_and_dispatch_injects_into_review_prompt(self) -> None:
        self.add_worker("active-w")
        self.add_worker("ready-w")
        with self.repo.lock():
            m = self.repo.load_members()
            m["workers"]["ready-w"]["status"] = "ready"
            self.repo.save_members(m)
        ruling_path = self.repo.stage_dir("s1") / "ruling_r1.md"
        ruling_path.write_text("# Ruling R1\n駁回 P2\n")
        out = Path(self.tmp.name) / "nr.json"
        self.assertEqual(self.run_cli("next-round", "--stage", "s1", "--ruling", str(ruling_path), "--output", str(out)), 0)
        nr = json.loads(out.read_text())
        post = self.repo.read_board()[-1]
        self.assertEqual(post["seq"], nr["ruling_post_seq"])
        self.assertEqual((post["kind"], post["stage"], post["round"], post["attempt"]), ("ruling", "s1", 1, 1))
        # Pushes only to active stage workers, not ready workers from the completed round.
        self.assertEqual(post["to"], ["active-w"])
        self.assertEqual(self.repo.load_members()["workers"]["ready-w"]["status"], "ready")

        self.run_cli("set-role", "--role", "rev", "--kind", "review", "--ade", "native", "--confirm")
        dout = Path(self.tmp.name) / "d.json"
        self.assertEqual(self.run_cli("dispatch", "--stage", "s1", "--role", "rev", "--dry-run", "--output", str(dout)), 0)
        d = json.loads(dout.read_text())
        self.assertIn(str(ruling_path.resolve()), d["instructions"])
        self.assertIn("逐條核對", d["instructions"])
        self.assertIn("--kind objection", d["instructions"])

    def test_objection_blocks_gate_until_reply_to_and_escalation_requires_owner_ack(self) -> None:
        self.add_worker("w1")
        self.add_worker("w2")
        report = self.repo.stage_dir("s1") / "report.md"
        report.write_text("ok\nIMPL DONE\n")
        (self.repo.stage_dir("s1") / "logs" / "test.log").write_text("ok\nexit=0\n")

        # Router posts a ruling (#1), w1 asks an unrelated question (#2), w1 objects to #1 (#3).
        r_res = post_message(self.repo, "router", ["w1"], "ruling", "駁回 P2", stage="s1", push=False)
        rseq = r_res["post"]["seq"]
        ask_res = post_message(self.repo, "w1", ["router"], "ask", "路徑?", push=False)
        obj_res = post_message(self.repo, "w1", ["router"], "objection", "P2 成立", reply_to=rseq, push=False)
        oseq = obj_res["post"]["seq"]

        # Answering the unrelated ask must NOT resolve the objection.
        post_message(self.repo, "router", ["w1"], "answer", "用絕對路徑", reply_to=ask_res["post"]["seq"], push=False)
        code, res = self.gate(report)
        self.assertEqual(code, 1)
        self.assertFalse(self.check_by_name(res, "unresolved_objections")["pass"])

        # Router resolves first-level objection with --reply-to oseq.
        ans_out = Path(self.tmp.name) / "ans.json"
        self.assertEqual(
            self.run_cli("post", "--as", "router", "--to", "w1", "--kind", "answer", "--reply-to", str(oseq),
                         "--text", "維持原判：已核對", "--no-push", "--output", str(ans_out)),
            0,
        )
        ans_seq = json.loads(ans_out.read_text())["post"]["seq"]
        code, res = self.gate(report)
        self.assertEqual(code, 0)

        # Re-objection (replying to Router's answer) escalates: Router cannot self-close without --owner-ack.
        reobj_res = post_message(self.repo, "w1", ["router"], "objection", "再異議：仍會失效", reply_to=ans_seq, push=False)
        reseq = reobj_res["post"]["seq"]
        err_out = Path(self.tmp.name) / "err.json"
        self.assertEqual(
            self.run_cli("post", "--as", "router", "--to", "w1", "--kind", "answer", "--reply-to", str(reseq),
                         "--text", "我還是不同意", "--no-push", "--output", str(err_out)),
            2,
        )
        self.assertEqual(json.loads(err_out.read_text())["error_code"], "ESCALATION_REQUIRED")
        code, res = self.gate(report)
        self.assertEqual(code, 1)
        self.assertTrue(self.check_by_name(res, "unresolved_objections")["unresolved"][0]["requires_owner_ack"])

        # Owner ruling unlocks the re-objection.
        self.assertEqual(
            self.run_cli("post", "--as", "router", "--to", "w1", "--kind", "ruling", "--reply-to", str(reseq),
                         "--owner-ack", "Owner 裁定改用 monotonic clock", "--text", "依 Owner 裁定採納", "--no-push"),
            0,
        )
        code, _ = self.gate(report)
        self.assertEqual(code, 0)

        # Multi-worker objection on a new ruling (#R2): 2 distinct workers objecting requires --owner-ack.
        r2_seq = post_message(self.repo, "router", ["w1", "w2"], "ruling", "跳過驗證", stage="s1", push=False)["post"]["seq"]
        o1 = post_message(self.repo, "w1", ["router"], "objection", "不安全", reply_to=r2_seq, push=False)["post"]["seq"]
        post_message(self.repo, "w2", ["router"], "objection", "我也不贊成", reply_to=r2_seq, push=False)
        self.assertEqual(
            self.run_cli("post", "--as", "router", "--to", "w1", "--kind", "answer", "--reply-to", str(o1),
                         "--text", "照做", "--no-push"),
            2,
        )
        # One Owner ruling targeting the shared ruling r2_seq resolves both workers' objections.
        self.assertEqual(
            self.run_cli("post", "--as", "router", "--to", "w1,w2", "--kind", "ruling", "--reply-to", str(r2_seq),
                         "--owner-ack", "Owner 裁定保留驗證", "--text", "保留驗證", "--no-push"),
            0,
        )
        code, _ = self.gate(report)
        self.assertEqual(code, 0)

    def test_reset_stage_and_owner_ack_isolate_attempts(self) -> None:
        self.add_worker("w1")
        report = self.repo.stage_dir("s1") / "report.md"
        report.write_text("ok\nIMPL DONE\n")
        (self.repo.stage_dir("s1") / "logs" / "test.log").write_text("ok\nexit=0\n")
        post_message(self.repo, "w1", ["router"], "objection", "舊 attempt 的異議", push=False)
        self.assertEqual(self.gate(report)[0], 1)

        # Resetting increments attempt to 2; old attempt's objection must not block attempt 2.
        self.assertEqual(self.run_cli("reset-stage", "--stage", "s1", "--reason", "brief gap"), 0)
        self.assertEqual(self.repo.load_state()["stages"]["s1"]["attempt"], 2)
        (self.repo.stage_dir("s1") / "logs" / "test.log").write_text("ok\nexit=0\n")
        self.assertEqual(self.gate(report)[0], 0)

        # Exhaust reset cap and resume with --owner-ack: attempt must keep increasing (never reset back to 1).
        self.assertEqual(self.run_cli("reset-stage", "--stage", "s1", "--reason", "gap2"), 0)
        self.assertEqual(self.run_cli("reset-stage", "--stage", "s1", "--reason", "gap3"), 2)
        self.assertEqual(self.run_cli("reset-stage", "--stage", "s1", "--owner-ack", "redo"), 0)
        st = self.repo.load_state()["stages"]["s1"]
        self.assertEqual((st["reset_count"], st["attempt"]), (0, 4))
        (self.repo.stage_dir("s1") / "logs" / "test.log").write_text("ok\nexit=0\n")
        self.assertEqual(self.gate(report)[0], 0)

    def test_fail_to_pass_ruling_reactivates_reviewer_and_guards_merge(self) -> None:
        sha = self.make_commit()
        self.run_cli("set-role", "--role", "rev", "--kind", "review", "--ade", "native", "--confirm")
        self.add_worker("rev-s1", ade="hc", marker="VERDICT: PASS|VERDICT: FAIL")
        with self.repo.lock():
            m = self.repo.load_members()
            m["workers"]["rev-s1"].update(kind="review", round=1, attempt=1)
            self.repo.save_members(m)
        rev_report = Path(self.repo.load_members()["workers"]["rev-s1"]["report_path"])
        rev_report.write_text("P2 issue\nVERDICT: FAIL\n")
        self.assertEqual(
            self.run_cli("post", "--as", "rev-s1", "--to", "router", "--kind", "done", "--file", str(rev_report), "--no-push"),
            0,
        )
        self.assertEqual(self.repo.load_members()["workers"]["rev-s1"]["status"], "ready")

        impl_report = self.repo.stage_dir("s1") / "report-impl.md"
        impl_report.write_text("done\nIMPL DONE\n")
        (self.repo.stage_dir("s1") / "logs" / "test.log").write_text("ok\nexit=0\n")

        # Checking the review report itself without --commit must pass (not blocked by unaddressed_fail_reviews).
        code, _ = self.gate(rev_report, "VERDICT: PASS|VERDICT: FAIL", "--allow-empty-logs")
        self.assertEqual(code, 0)

        # Merge gate (--commit) fails when a current-round FAIL review has no ruling.
        code, res = self.gate(impl_report, "IMPL DONE", "--commit", sha)
        self.assertEqual(code, 1)
        self.assertFalse(self.check_by_name(res, "unaddressed_fail_reviews")["pass"])

        # Router sends targeted ruling to ready reviewer -> reactivates it and blocks gate on pending_ruling_acks.
        rout = Path(self.tmp.name) / "r.json"
        self.assertEqual(
            self.run_cli("post", "--as", "router", "--to", "rev-s1", "--kind", "ruling",
                         "--text", "核對後 P2 不成立", "--no-push", "--output", str(rout)),
            0,
        )
        rseq = json.loads(rout.read_text())["post"]["seq"]
        self.assertEqual(self.repo.load_members()["workers"]["rev-s1"]["status"], "active")
        code, res = self.gate(impl_report, "IMPL DONE", "--commit", sha)
        self.assertEqual(code, 1)
        self.assertTrue(self.check_by_name(res, "unaddressed_fail_reviews")["pass"])
        self.assertFalse(self.check_by_name(res, "pending_ruling_acks")["pass"])

        # Stop hook blocks rev-s1 first to read, then to ack or object.
        with self.repo.lock():
            self.assertIn("read", stop_decision(self.repo, "rev-s1") or "")
        self.run_cli("read", "--as", "rev-s1")
        with self.repo.lock():
            reason = stop_decision(self.repo, "rev-s1") or ""
            self.assertIn(f"--reply-to {rseq}", reason)

        # Reviewer confirms ruling -> status returns to ready and merge verify-gate passes.
        self.assertEqual(
            self.run_cli("post", "--as", "rev-s1", "--to", "router", "--kind", "note",
                         "--reply-to", str(rseq), "--text", "同意裁定", "--no-push"),
            0,
        )
        self.assertEqual(self.repo.load_members()["workers"]["rev-s1"]["status"], "ready")
        code, _ = self.gate(impl_report, "IMPL DONE", "--commit", sha)
        self.assertEqual(code, 0)

    def test_unpushable_native_reviewer_ack_warns_and_unlocks_with_owner_ack(self) -> None:
        self.add_worker("rev-nat", ade="native", marker="VERDICT: PASS|VERDICT: FAIL")
        with self.repo.lock():
            m = self.repo.load_members()
            m["workers"]["rev-nat"].update(status="ready", kind="review", done_seq=1)
            self.repo.save_members(m)
        rout = Path(self.tmp.name) / "rn.json"
        self.assertEqual(
            self.run_cli("post", "--as", "router", "--to", "rev-nat", "--kind", "ruling",
                         "--text", "駁回 P2", "--no-push", "--output", str(rout)),
            0,
        )
        rpayload = json.loads(rout.read_text())
        self.assertEqual(rpayload["unpushable_ack_workers"], ["rev-nat"])
        rseq = rpayload["post"]["seq"]
        report = self.repo.stage_dir("s1") / "report.md"
        report.write_text("ok\nIMPL DONE\n")
        (self.repo.stage_dir("s1") / "logs" / "test.log").write_text("ok\nexit=0\n")
        self.assertEqual(self.gate(report)[0], 1)
        self.assertEqual(
            self.run_cli("post", "--as", "router", "--to", "rev-nat", "--kind", "ruling",
                         "--reply-to", str(rseq), "--owner-ack", "Owner 確認可合併", "--no-push"),
            0,
        )
        self.assertEqual(self.gate(report)[0], 0)

    def test_watch_skips_marker_on_reactivated_reviewer_and_all_ruling_skips_ready(self) -> None:
        self.add_worker("rev-s1", ade="hc", marker="VERDICT: PASS|VERDICT: FAIL")
        self.add_worker("impl-s1", ade="hc", marker="IMPL DONE")
        with self.repo.lock():
            m = self.repo.load_members()
            m["workers"]["rev-s1"].update(kind="review", round=1, attempt=1)
            m["workers"]["impl-s1"].update(status="ready", done_seq=1)
            self.repo.save_members(m)
        rev_report = Path(self.repo.load_members()["workers"]["rev-s1"]["report_path"])
        rev_report.write_text("P2\nVERDICT: FAIL\n")
        self.assertEqual(
            self.run_cli("post", "--as", "rev-s1", "--to", "router", "--kind", "done", "--file", str(rev_report), "--no-push"),
            0,
        )

        # Broadcast ruling (--to all) must not wake up already-ready workers or add require_ack_from.
        self.assertEqual(
            self.run_cli("post", "--as", "router", "--to", "all", "--stage", "s1", "--kind", "ruling",
                         "--text", "設計補充", "--no-push"),
            0,
        )
        bcast = self.repo.read_board()[-1]
        self.assertEqual(bcast["to"], [])
        self.assertNotIn("require_ack_from", bcast)
        self.assertEqual(self.repo.load_members()["workers"]["impl-s1"]["status"], "ready")

        # Targeted ruling reactivates rev-s1; watch_tick must NOT misread the old report marker as MARKER_WITHOUT_POST.
        self.assertEqual(
            self.run_cli("post", "--as", "router", "--to", "rev-s1", "--kind", "ruling", "--text", "駁回 P2", "--no-push"),
            0,
        )
        events = self.tick("2026-01-01T00:00:00Z", {"rev-s1": {"agent_status": "working", "screen_text": "reading"}})["events"]
        self.assertNotIn("MARKER_WITHOUT_POST", [e["event"] for e in events])
        self.assertEqual(self.repo.load_members()["workers"]["rev-s1"]["status"], "active")

        # Closing a FAIL reviewer cannot bypass pending_ruling_acks.
        self.add_worker("rev-closed", ade="hc", marker="VERDICT: PASS|VERDICT: FAIL")
        with self.repo.lock():
            m = self.repo.load_members()
            m["workers"]["rev-closed"].update(kind="review", status="closed", done_seq=1)
            self.repo.save_members(m)
        cout = Path(self.tmp.name) / "rc.json"
        self.assertEqual(
            self.run_cli("post", "--as", "router", "--to", "rev-closed", "--kind", "ruling",
                         "--text", "駁回", "--no-push", "--output", str(cout)),
            0,
        )
        cp = json.loads(cout.read_text())
        self.assertEqual(cp["post"]["require_ack_from"], ["rev-closed"])
        self.assertEqual(cp["unpushable_ack_workers"], ["rev-closed"])


class LocalDoneCheck(Base):
    def test_done_precheck_blocks_bad_marker_placeholders_and_logs_but_allows_empty_logs(self) -> None:
        self.add_worker("w1", marker="IMPL DONE")
        report = Path(self.repo.load_members()["workers"]["w1"]["report_path"])

        # Missing marker -> rejected locally, nothing posted, status stays active.
        report.write_text("finished without marker\n")
        out = Path(self.tmp.name) / "p.json"
        self.assertEqual(
            self.run_cli("post", "--as", "w1", "--to", "router", "--kind", "done", "--file", str(report),
                         "--no-push", "--output", str(out)),
            1,
        )
        self.assertEqual(json.loads(out.read_text())["error_code"], "LOCAL_CHECK_FAILED")
        self.assertEqual(self.repo.read_board(), [])
        self.assertEqual(self.repo.load_members()["workers"]["w1"]["status"], "active")

        # Unfilled placeholder -> rejected locally.
        report.write_text("commit: <SHA>\nIMPL DONE\n")
        self.assertEqual(
            self.run_cli("post", "--as", "w1", "--to", "router", "--kind", "done", "--file", str(report), "--no-push"),
            1,
        )

        # Broken log in current round -> rejected locally.
        report.write_text("commit: abc123\nIMPL DONE\n")
        logf = self.repo.stage_dir("s1") / "logs" / "test.log"
        logf.write_text("ran tests without exit line\n")
        self.assertEqual(
            self.run_cli("post", "--as", "w1", "--to", "router", "--kind", "done", "--file", str(report), "--no-push"),
            1,
        )

        # --skip-check escape hatch still lets a worker post when needed.
        self.assertEqual(
            self.run_cli("post", "--as", "w1", "--to", "router", "--kind", "done", "--file", str(report),
                         "--skip-check", "--no-push"),
            0,
        )
        self.assertEqual(len(self.repo.read_board()), 1)

        # Empty logs directory (e.g. brief or static review worker) passes local check.
        logf.unlink()
        self.add_worker("brief-w", marker="BRIEF DONE")
        breport = Path(self.repo.load_members()["workers"]["brief-w"]["report_path"])
        breport.write_text("all set\nBRIEF DONE\n")
        self.assertEqual(
            self.run_cli("post", "--as", "brief-w", "--to", "router", "--kind", "done", "--file", str(breport), "--no-push"),
            0,
        )
        self.assertEqual(self.repo.load_members()["workers"]["brief-w"]["status"], "ready")


if __name__ == "__main__":
    unittest.main()
