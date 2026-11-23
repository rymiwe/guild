#!/usr/bin/env python3
"""Fail-first fixtures for the auto-start draft-only review gate (G1-G6).

Nothing reaches GitHub. The gate ships disabled; tests turn it on in-process.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
BIN = HERE.parent / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

NOW = datetime(2026, 11, 12, 5, 15, tzinfo=timezone.utc)

def load_mod():
    return importlib.import_module("autostart_draft_review")


def prefs(td: Path, **overrides):
    data = {
        "enabled": True,
        "state_dir": str(td / "state"),
        "gates_dir": str(td / "gates"),
        "timezone": "America/Los_Angeles",
        "audit_log": str(td / "autonomy-audit-log.md"),
        "pings_path": str(td / "state" / "pings"),
        "jobdir_root": str(td / "drafts"),
        "n_lines": 100,
        "daily_cap": 5,
        "expected_daily_pace": 10,
        "pace_mult": 1.5,
        "owner_login": "example-owner",
        "bot_logins": ["example-bot", "example-bot-2"],
        "org_members": ["erin", "dana", "frank", "grace"],
        "classified_repos": ["app", "work-org/app", "api", "work-org/api"],
    }
    data.update(overrides)
    return data


def pr4004(**overrides):
    data = {
        "repo": "work-org/app",
        "pr": 4004,
        "number": 4004,
        "head": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "classifier_head": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "title": "Sort schema dump",
        "html_url": "https://github.com/work-org/app/pull/4004",
        "additions": 31,
        "deletions": 32,
        "labels": ["risk:low"],
        "effective_tier": "low",
        "confidence": "full",
        "is_draft": False,
        "colleague_request": True,
        "requester": {"login": "erin", "type": "User"},
        "seating_ok": True,
    }
    data.update(overrides)
    return data


def high_pr(n, lines, **overrides):
    head = f"{n:040d}"
    data = pr4004(pr=n, number=n, head=head, classifier_head=head, additions=lines, deletions=0,
                  labels=["risk:high"], effective_tier="high")
    data.update(overrides)
    return data


class GateFixtures(unittest.TestCase):
    def setUp(self):
        self.td = Path(tempfile.mkdtemp(prefix="autostart-"))
        self.p = prefs(self.td)
        self.m = load_mod()
        self.env = {**os.environ, "AUTOSTART_DRAFT": "1"}

    def decide(self, rec, **kw):
        p = kw.pop("prefs", self.p)
        return self.m.decide(rec, p, now=NOW, env=self.env, **kw)

    def start(self, rec, **kw):
        p = kw.pop("prefs", self.p)
        return self.m.start_draft(rec, p, now=NOW, env=self.env, **kw)

    def test_disabled_flag_asks_and_does_not_start(self):
        p = prefs(self.td, enabled=False)
        env = {**self.env, "AUTOSTART_DRAFT": "0"}
        rec = pr4004()
        d = self.m.decide(rec, p, now=NOW, env=env)
        self.assertEqual(d["action"], "ask")
        self.assertEqual(d["reason"], "gate disabled")
        out = self.m.start_draft(rec, p, now=NOW, env=env)
        self.assertEqual(out["status"], "ask")
        self.assertFalse((self.td / "drafts").exists() and any((self.td / "drafts").iterdir()))

    def test_empty_env_ignores_process_env(self):
        p = prefs(self.td, enabled=False)
        old = os.environ.get("AUTOSTART_DRAFT")
        os.environ["AUTOSTART_DRAFT"] = "1"
        try:
            self.assertFalse(self.m.is_enabled(p, env={}))
        finally:
            if old is None:
                os.environ.pop("AUTOSTART_DRAFT")
            else:
                os.environ["AUTOSTART_DRAFT"] = old

    def test_4004_would_auto_start(self):
        d = self.decide(pr4004())
        self.assertEqual(d["action"], "start", d)
        out = self.start(pr4004())
        self.assertEqual(out["status"], "started")
        self.assertTrue(Path(out["draft"]).is_dir())
        self.assertTrue((Path(out["draft"]) / "HOLD-NO-POST").is_file())
        self.assertFalse(out["posted"])

    def test_3624_and_3628_still_ask(self):
        for n, lines in ((3624, 490), (3628, 222)):
            d = self.decide(high_pr(n, lines))
            self.assertEqual(d["action"], "ask", d)
            self.assertIn("high-risk", d["reason"])

    def test_3625_asks_even_if_n_raised(self):
        rec = pr4004(pr=3625, number=3625, additions=189, deletions=0, labels=["risk:medium"],
                     effective_tier="high", head="b" * 40, classifier_head="b" * 40)
        d = self.decide(rec, prefs=prefs(self.td, n_lines=200))
        self.assertEqual(d["action"], "ask")
        self.assertIn("high-risk", d["reason"])

    def test_3626_and_3627_ask_on_size(self):
        for n, lines in ((3626, 172), (3627, 252)):
            rec = pr4004(pr=n, number=n, additions=lines, deletions=0, labels=["risk:low"],
                         effective_tier="low", head=f"{n:040d}", classifier_head=f"{n:040d}")
            d = self.decide(rec)
            self.assertEqual(d["action"], "ask", d)
            self.assertIn("not under 100", d["reason"])

    def test_strictly_under_n(self):
        rec99 = pr4004(additions=99, deletions=0)
        rec100 = pr4004(additions=100, deletions=0)
        rec101 = pr4004(additions=101, deletions=0)
        self.assertEqual(self.decide(rec99)["action"], "start")
        self.assertEqual(self.decide(rec100)["action"], "ask")
        self.assertEqual(self.decide(rec101)["action"], "ask")

    def test_high_risk_small_diff_asks(self):
        d = self.decide(high_pr(9, 40))
        self.assertEqual(d["action"], "ask")
        self.assertIn("high-risk", d["reason"])

    def test_missing_line_count_asks(self):
        rec = pr4004()
        rec.pop("additions")
        rec.pop("deletions")
        d = self.decide(rec)
        self.assertEqual(d["action"], "ask")
        self.assertIn("changed-line", d["reason"])

    def test_missing_risk_label_asks(self):
        rec = pr4004(labels=[], effective_tier="")
        d = self.decide(rec)
        self.assertEqual(d["action"], "ask")
        self.assertIn("risk label", d["reason"])

    def test_no_colleague_request_asks(self):
        d = self.decide(pr4004(colleague_request=False, requester=None))
        self.assertEqual(d["action"], "ask")
        self.assertIn("no colleague", d["reason"])

    def test_stack_request_asks(self):
        d = self.decide(pr4004(stack=True, prs=[3624, 3625, 3626, 3627, 3628]))
        self.assertEqual(d["action"], "ask")
        self.assertIn("stack", d["reason"])

    def test_draft_github_pr_asks(self):
        d = self.decide(pr4004(is_draft=True))
        self.assertEqual(d["action"], "ask")
        self.assertIn("draft", d["reason"])

    def test_seating_failure_asks(self):
        d = self.decide(pr4004(seating_ok=False, seating_reason="frontier data stale"))
        self.assertEqual(d["action"], "ask")
        self.assertIn("frontier data stale", d["reason"])

    # G1
    def test_g1_owner_is_not_a_colleague(self):
        d = self.decide(pr4004(requester={"login": "example-owner", "type": "User"}))
        self.assertEqual(d["action"], "ask")
        self.assertEqual(d["reason"], "requester is the owner")

    def test_g1_bot_asks(self):
        d = self.decide(pr4004(requester={"login": "dependabot[bot]", "type": "Bot"}))
        self.assertEqual(d["action"], "ask")
        self.assertIn("bot", d["reason"])

    def test_g1_work_bot_asks(self):
        d = self.decide(pr4004(requester={"login": "example-bot", "type": "User"}))
        self.assertEqual(d["action"], "ask")
        self.assertEqual(d["reason"], "requester is the work bot")

    def test_g1_unknown_requester_asks(self):
        d = self.decide(pr4004(requester={"login": "stranger", "type": "User"}))
        self.assertEqual(d["action"], "ask")
        self.assertIn("unknown requester", d["reason"])

    def test_g1_org_member_starts(self):
        d = self.decide(pr4004(requester={"login": "dana", "type": "User"}))
        self.assertEqual(d["action"], "start", d)

    def test_g1_hook_row_does_not_check_who_was_requested(self):
        # The review-requested search upstream decides that the owner was asked; G1 checks only the author.
        pulls = {
            "repos/work-org/app/pulls/4010": {"user": {"login": "dana", "type": "User"},
                                              "requested_reviewers": [{"login": "frank"}], "head": {"sha": "a" * 40}},
            "repos/work-org/app/pulls/4011": {"user": {"login": "dependabot[bot]", "type": "Bot"},
                                              "requested_reviewers": [{"login": "example-owner"}], "head": {"sha": "b" * 40}},
        }
        orig = self.m._gh_json
        self.m._gh_json = lambda path, timeout=60: pulls.get(path)
        try:
            human = self.m.build_hook_row("work-org/app#4010", "UPDATED")
            bot = self.m.build_hook_row("work-org/app#4011")
        finally:
            self.m._gh_json = orig
        self.assertTrue(self.m.colleague_status(human["requester"], self.p)["ok"])
        self.assertEqual(self.m.colleague_status(bot["requester"], self.p)["reason"], "requester is a bot")

    def test_no_bot_logins_in_prefs_asks(self):
        p = dict(self.p)
        del p["bot_logins"]
        self.assertEqual(self.m.colleague_status({"login": "erin", "type": "User"}, p)["reason"],
                         "prefs have no bot_logins")

    def test_missing_required_prefs_ask_and_name_the_key(self):
        # No built-in owner, host path or time zone: each missing key asks and names it.
        for key in ("owner_login", "gates_dir", "state_dir", "audit_log", "timezone"):
            p = dict(self.p)
            del p[key]
            d = self.decide(pr4004(), prefs=p)
            self.assertEqual((d["action"], d["reason"]), ("ask", f"prefs have no {key}"), key)

    def test_unknown_timezone_asks(self):
        d = self.decide(pr4004(), prefs=prefs(self.td, timezone="Mars/Olympus"))
        self.assertEqual(d["action"], "ask")
        self.assertIn("timezone", d["reason"])

    def test_no_classified_repos_in_prefs_asks(self):
        p = dict(self.p)
        del p["classified_repos"]
        d = self.decide(pr4004(), prefs=p)
        self.assertEqual((d["action"], d["reason"]), ("ask", "repo not classified"))

    # G2
    def test_g2_one_start_per_pr_head(self):
        rec = pr4004()
        self.assertEqual(self.start(rec)["status"], "started")
        again = self.decide(rec)
        self.assertEqual(again["action"], "ask")
        self.assertIn("already auto-started", again["reason"])

    def test_g2_head_move_marks_stale_and_asks(self):
        rec = pr4004()
        self.assertEqual(self.start(rec)["status"], "started")
        moved = pr4004(head="c" * 40, classifier_head="c" * 40)
        d = self.decide(moved)
        self.assertEqual(d["action"], "ask")
        self.assertIn("head moved", d["reason"])
        heads = json.loads((self.td / "state" / "autostart-draft-review.heads.json").read_text())
        self.assertTrue(heads["work-org/app#4004"]["stale"])

    def test_g2_mid_run_head_change_asks(self):
        rec = pr4004(run_head="d" * 40)
        d = self.decide(rec)
        self.assertEqual(d["action"], "ask")
        self.assertIn("head moved", d["reason"])

    # G3
    def test_g3_post_flag_raises(self):
        rec = pr4004()
        with self.assertRaises(self.m.PostHoldError):
            self.start(rec, argv=["start", "--post"])
        with self.assertRaises(self.m.PostHoldError):
            self.start(rec, post_cmd="/opt/tools/bin/wes-post-review")
        with self.assertRaises(self.m.PostHoldError):
            self.m._assert_no_post(["/tmp/wes-post-review"])

    def test_g3_start_does_not_post(self):
        out = self.start(pr4004())
        self.assertFalse(out["posted"])
        self.assertNotIn("--post", json.dumps(out))

    # G4
    def test_g4_sixth_start_asks(self):
        for i in range(5):
            rec = pr4004(pr=5000 + i, number=5000 + i, head=f"{i:040d}", classifier_head=f"{i:040d}")
            self.assertEqual(self.start(rec)["status"], "started", rec["pr"])
        d = self.decide(pr4004(pr=6000, number=6000, head="e" * 40, classifier_head="e" * 40))
        self.assertEqual(d["action"], "ask")
        self.assertIn("daily cap", d["reason"])

    def test_g4_above_1_5x_pace_asks(self):
        p = prefs(self.td, expected_daily_pace=2, daily_cap=10)
        started = []
        for i in range(1, 5):
            rec = pr4004(pr=i, number=i, head=str(i) * 40, classifier_head=str(i) * 40)
            started.append(self.m.start_draft(rec, p, now=NOW, env=self.env)["status"])
        self.assertEqual(started, ["started"] * 4)
        # 4 starts is above 1.5 * 2 = 3, so the next one asks.
        rec5 = pr4004(pr=5, number=5, head="5" * 40, classifier_head="5" * 40)
        d = self.m.decide(rec5, p, now=NOW, env=self.env)
        self.assertEqual(d["action"], "ask")
        self.assertIn("1.5x pace", d["reason"])

    def test_g4_logs_each_start_to_audit(self):
        self.start(pr4004())
        text = Path(self.p["audit_log"]).read_text(encoding="utf-8")
        self.assertIn("auto-start draft-only review work-org/app#4004", text)

    # G5
    def test_g5_ping_payload_has_link_and_draft_and_does_not_send(self):
        sent = []
        def capture(cmd):
            # notify_cmd is invoked by record_ping; we pass a list and intercept via wrapper
            raise AssertionError("should use notify_cmd list, not this")
        notify = self.td / "notify.sh"
        notify.write_text("#!/bin/sh\nprintf '%s\\n' \"$1\" >> \"$0.out\"\n")
        notify.chmod(0o755)
        out = self.start(pr4004(), notify_cmd=["/bin/sh", str(notify)])
        self.assertIn("https://github.com/work-org/app/pull/4004", out["ping"])
        self.assertIn(out["draft"], out["ping"])
        ping_file = Path(self.p["pings_path"]).read_text(encoding="utf-8")
        self.assertIn("https://github.com/work-org/app/pull/4004", ping_file)
        self.assertIn(out["draft"], ping_file)
        # The helper wrote the payload to the notify script's .out; no GitHub post.
        self.assertFalse(out["posted"])

    # G6
    def test_g6_check_script_ships_disabled_and_hooks_existing_check(self):
        script = BIN / "work-review-requests-check.sh"
        text = script.read_text(encoding="utf-8")
        self.assertIn("AUTO_START_DRAFT_DEFAULT=0", text)
        self.assertIn("autostart_draft_review.py", text)
        self.assertIn("--auto-start-draft", text)
        self.assertNotIn("UpdateRoutine", text)
        # No new poll / cron in the module or hook.
        self.assertNotIn("crontab", text.lower())
        mod = Path(BIN / "autostart_draft_review.py").read_text(encoding="utf-8")
        self.assertIn("ENABLED_DEFAULT = False", mod)
        self.assertIn("No new poll is added", mod)


class MigrationFixtures(unittest.TestCase):
    def test_migration_renames_keys_fills_paths_and_is_idempotent(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "migrate_autostart_prefs", HERE.parent / "scripts" / "migrate-autostart-prefs.py")
        mig = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mig)
        td = Path(tempfile.mkdtemp(prefix="autostart-mig-"))
        (td / "state").mkdir()
        (td / "gates").mkdir()
        (td / "state" / "wes-autostart-draft-review.heads.json").write_text("{}")
        (td / "gates" / "wes-autostart-draft-review.PAUSED").write_text("")
        pf = td / "prefs.json"
        pf.write_text(json.dumps({"ryan_login": "example-owner", "wes_logins": ["example-bot"], "enabled": False}))
        fills = {"state_dir": str(td / "state"), "gates_dir": str(td / "gates"),
                 "audit_log": str(td / "audit.md"), "timezone": "UTC"}
        self.assertTrue(mig.migrate(pf, fills))
        got = json.loads(pf.read_text())
        self.assertEqual(got, {"owner_login": "example-owner", "bot_logins": ["example-bot"], "enabled": False, **fills})
        self.assertTrue((td / "state" / "autostart-draft-review.heads.json").exists())
        self.assertTrue((td / "gates" / "autostart-draft-review.PAUSED").exists())
        self.assertFalse((td / "state" / "wes-autostart-draft-review.heads.json").exists())
        before = pf.read_text()
        self.assertEqual(mig.migrate(pf, {k: "other" for k in fills}), [])
        self.assertEqual(pf.read_text(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
