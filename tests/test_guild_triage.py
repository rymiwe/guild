#!/usr/bin/env python3
"""Unit tests for bin/guild-triage. Hermetic: temp dirs and stub scripts only."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "bin" / "guild-triage"


def run_triage(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(BIN), *args],
        capture_output=True,
        text=True,
        env=env,
    )


class GuildTriageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="guild-triage-test-"))
        self.state = self.tmp / "state"
        self.cfg = self.tmp / "config"
        self.jobs = self.tmp / "jobs"
        self.health = self.tmp / "seat-health"
        self.notify_log = self.tmp / "notify.log"
        for d in (self.state, self.cfg, self.jobs, self.health):
            d.mkdir(parents=True, exist_ok=True)
        self.base_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.tmp / "home"),
            "GUILD_TRIAGE_STATE_DIR": str(self.state),
            "GUILD_TRIAGE_KILL_SWITCH": str(self.cfg / "triage.off"),
            "GUILD_TRIAGE_NOTIFY_LOG": str(self.notify_log),
            "GUILD_TRIAGE_JOBS_ROOT": str(self.jobs),
            "GUILD_TRIAGE_HEALTH_DIR": str(self.health),
            "GUILD_TRIAGE_FILE_PREFIX": ".acme.",
        }
        (self.tmp / "home").mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ts(self, minutes_ago: int = 10) -> str:
        return (datetime.now() - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")

    def _write_job(self, name: str, report: str | None = None, run_log: str | None = None,
                   failover_log: str | None = None) -> Path:
        d = self.jobs / name
        d.mkdir(parents=True, exist_ok=True)
        if report is not None:
            (d / "report.md").write_text(report, encoding="utf-8")
        if run_log is not None:
            (d / "run.log").write_text(run_log, encoding="utf-8")
        if failover_log is not None:
            (d / "run.log.failover").write_text(failover_log, encoding="utf-8")
        return d

    def test_collect_parses_actionable_alerts_and_writes_bundles(self) -> None:
        self._write_job(
            "review-widgets-42-recovery-abc123456",
            report="# Recovery\n\nPosted and verified.\n",
            run_log="JOB_CLI=codex\nEXIT 0\nmarked codex down\n",
            failover_log="failover-wrap start\n",
        )
        self._write_job(
            "review-gadgets-99-intake-def789012",
            report="# Review\n\nSTOPPED\n",
            run_log="JOB_CLI=codex\nEXIT 0\n",
        )
        until = int(time.time()) + 3600
        (self.health / "codex").write_text(
            f"reason=quick exit 0 with no report: codex (38s, threshold 600s)\nuntil={until}\n",
            encoding="utf-8",
        )
        lines = [
            f"{self._ts(30)} bopeep-watch bopeep-watch CHANGED http=200 reason=routine change",
            f"{self._ts(25)} feedback-widgets-1 feedback-widgets-1 DONE http=200 reason=",
            f"{self._ts(20)} review-gadgets-99-intake-def789012 review-gadgets-99-intake-def789012 STOPPED http=200 reason=",
            f"{self._ts(10)} review-widgets-42-recovery-abc123456 review-widgets-42-recovery-abc123456-f1 STOPPED http=200 reason=second failure, no more failover: quick exit 0 with no report: claude (38s, threshold 600s)",
            f"{self._ts(5)} guild guild-pin FAILED http=200 reason=commit guard blocked",
        ]
        self.notify_log.write_text("\n".join(lines) + "\n", encoding="utf-8")

        res = run_triage(["collect", "--hosts", "local"], self.base_env)
        self.assertEqual(res.returncode, 0, res.stderr)
        data = json.loads(res.stdout)
        self.assertEqual(len(data["incidents"]), 3)
        jobs = [inc["job"] for inc in data["incidents"]]
        self.assertEqual(
            jobs,
            [
                "review-gadgets-99-intake-def789012",
                "review-widgets-42-recovery-abc123456",
                "guild",
            ],
        )
        rec = data["incidents"][1]
        bundle = Path(rec["bundle_dir"])
        self.assertTrue((bundle / "meta.json").is_file())
        self.assertIn("Posted and verified.", (bundle / "report-tail.md").read_text(encoding="utf-8"))
        self.assertIn("marked codex down", (bundle / "run-tail.log").read_text(encoding="utf-8"))
        self.assertIn("failover-wrap start", (bundle / "failover.log").read_text(encoding="utf-8"))
        sh = json.loads((bundle / "seat-health.json").read_text(encoding="utf-8"))
        self.assertEqual(len(sh), 1)
        self.assertEqual(sh[0]["seat"], "codex")

    def test_collect_skips_old_lines_and_expired_seat_health(self) -> None:
        old_ts = (datetime.now() - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")
        self.notify_log.write_text(
            f"{old_ts} review-widgets-1 review-widgets-1 STOPPED http=200 reason=old failure\n",
            encoding="utf-8",
        )
        expired = int(time.time()) - 100
        (self.health / "agy").write_text(f"reason=old\nuntil={expired}\n", encoding="utf-8")

        res = run_triage(["collect", "--hosts", "local", "--since-hours", "24"], self.base_env)
        self.assertEqual(res.returncode, 0, res.stderr)
        data = json.loads(res.stdout)
        self.assertEqual(data["incidents"], [])
        self.assertEqual(data["hosts"]["local"]["active_seat_downs"], [])

    def test_record_deduplicates_incident_and_fingerprint_cooldown(self) -> None:
        t1 = self._ts(20)
        t2 = self._ts(5)
        self.notify_log.write_text(
            f"{t1} review-widgets-10-recovery-aaaa11111 review-widgets-10-recovery-aaaa11111-f1 STOPPED http=200 reason=second failure, no more failover: quick exit 0 with no report: claude (38s, threshold 600s)\n"
            f"{t2} review-widgets-11-recovery-bbbb22222 review-widgets-11-recovery-bbbb22222-f1 STOPPED http=200 reason=second failure, no more failover: quick exit 0 with no report: claude (95s, threshold 600s)\n",
            encoding="utf-8",
        )
        first = json.loads(run_triage(["collect", "--hosts", "local"], self.base_env).stdout)
        self.assertEqual(len(first["incidents"]), 2)
        # Same failure class produces identical fingerprint despite different PRs, SHAs, and durations.
        self.assertEqual(first["incidents"][0]["fingerprint"], first["incidents"][1]["fingerprint"])

        inc0 = first["incidents"][0]
        rec = run_triage(
            [
                "record",
                inc0["incident_id"],
                "--fingerprint",
                inc0["fingerprint"],
                "--outcome",
                "fixed",
                "--summary",
                "Opened draft PR for quick exit bug",
                "--pr",
                "https://github.com/rymiwe/quartermaster/pull/131",
            ],
            self.base_env,
        )
        self.assertEqual(rec.returncode, 0, rec.stderr)

        # Second collect suppresses both the exact incident and the sibling with same fingerprint during cooldown.
        second = json.loads(run_triage(["collect", "--hosts", "local"], self.base_env).stdout)
        self.assertEqual(second["incidents"], [])

        # --all bypasses fingerprint cooldown (but still skips exact seen incident_id).
        with_all = json.loads(run_triage(["collect", "--hosts", "local", "--all"], self.base_env).stdout)
        self.assertEqual(len(with_all["incidents"]), 1)
        self.assertEqual(with_all["incidents"][0]["incident_id"], first["incidents"][1]["incident_id"])

    def test_daily_pr_cap_and_status(self) -> None:
        env = dict(self.base_env, GUILD_TRIAGE_DAILY_CAP="1")
        rec = run_triage(
            [
                "record",
                "deadbeef1234",
                "--outcome",
                "fixed",
                "--summary",
                "Draft PR 1",
                "--pr",
                "https://github.com/rymiwe/quartermaster/pull/131",
            ],
            env,
        )
        self.assertEqual(rec.returncode, 0, rec.stderr)
        data = json.loads(run_triage(["collect", "--hosts", "local"], env).stdout)
        self.assertEqual(data["caps"]["daily_prs"], 1)
        self.assertEqual(data["caps"]["daily_cap"], 1)
        self.assertFalse(data["caps"]["can_open_pr"])

        st = run_triage(["status"], env)
        self.assertEqual(st.returncode, 0, st.stderr)
        sdata = json.loads(st.stdout)
        self.assertEqual(sdata["seen_incidents"], 1)
        self.assertEqual(sdata["caps"]["daily_prs"], 1)
        self.assertFalse(sdata["kill_switch"])

    def test_daily_pr_cap_counts_only_distinct_fixed_prs(self) -> None:
        env = dict(self.base_env, GUILD_TRIAGE_DAILY_CAP="2")
        # Two incidents referencing the same fixed PR count as one PR.
        for inc_id in ("inc111111111", "inc222222222"):
            res = run_triage(
                [
                    "record",
                    inc_id,
                    "--outcome",
                    "fixed",
                    "--summary",
                    "Same PR covers both incidents",
                    "--pr",
                    "https://github.com/rymiwe/quartermaster/pull/131",
                ],
                env,
            )
            self.assertEqual(res.returncode, 0, res.stderr)

        # A blocked incident pointing at an existing PR does not consume the cap.
        blocked = run_triage(
            [
                "record",
                "inc333333333",
                "--outcome",
                "blocked",
                "--summary",
                "Blocked on unmerged fix",
                "--pr",
                "https://github.com/rymiwe/quartermaster/pull/132",
            ],
            env,
        )
        self.assertEqual(blocked.returncode, 0, blocked.stderr)

        sdata = json.loads(run_triage(["status"], env).stdout)
        self.assertEqual(sdata["caps"]["daily_prs"], 1)
        self.assertTrue(sdata["caps"]["can_open_pr"])

    def test_kill_switch_holds_collect_with_exit_11(self) -> None:
        (self.cfg / "triage.off").write_text("paused\n", encoding="utf-8")
        res = run_triage(["collect", "--hosts", "local"], self.base_env)
        self.assertEqual(res.returncode, 11)
        self.assertIn("kill switch", res.stderr.lower())

    def test_dry_run_writes_no_bundles(self) -> None:
        self.notify_log.write_text(
            f"{self._ts(5)} review-widgets-5 review-widgets-5 FAILED http=200 reason=boom\n",
            encoding="utf-8",
        )
        res = run_triage(["collect", "--hosts", "local", "--dry-run"], self.base_env)
        self.assertEqual(res.returncode, 0, res.stderr)
        data = json.loads(res.stdout)
        self.assertEqual(len(data["incidents"]), 1)
        self.assertIsNone(data["incidents"][0]["bundle_dir"])
        self.assertFalse((self.state / "bundles").exists())

    def test_remote_host_collect_via_ssh_stub(self) -> None:
        stub_dir = self.tmp / "stubs"
        stub_dir.mkdir()
        remote_payload = {
            "ok": True,
            "notify_log": "/remote/notify.log",
            "jobs_root": "/remote/jobs",
            "active_seat_downs": [{"seat": "claude", "reason": "usage limit", "until": int(time.time()) + 600}],
            "lines": [
                f"{self._ts(8)} review-widgets-77-intake-abcdef123 review-widgets-77-intake-abcdef123 STOPPED http=200 reason=base moved"
            ],
            "job_files": {
                "review-widgets-77-intake-abcdef123": {
                    "report_head": "# Remote review\n",
                    "report_tail": "STOPPED base moved\n",
                    "run_tail": "EXIT 0\n",
                    "failover_log": None,
                }
            },
        }
        ssh_stub = stub_dir / "ssh"
        ssh_stub.write_text(
            f"#!/usr/bin/env python3\nimport json\nprint({json.dumps(json.dumps(remote_payload))})\n",
            encoding="utf-8",
        )
        ssh_stub.chmod(ssh_stub.stat().st_mode | stat.S_IXUSR)
        env = dict(self.base_env, PATH=f"{stub_dir}:{self.base_env['PATH']}")

        res = run_triage(["collect", "--hosts", "remotebox"], env)
        self.assertEqual(res.returncode, 0, res.stderr)
        data = json.loads(res.stdout)
        self.assertTrue(data["hosts"]["remotebox"]["ok"])
        self.assertEqual(len(data["incidents"]), 1)
        inc = data["incidents"][0]
        self.assertEqual(inc["host"], "remotebox")
        self.assertEqual(inc["reason"], "base moved")
        bundle = Path(inc["bundle_dir"])
        self.assertIn("STOPPED base moved", (bundle / "report-tail.md").read_text(encoding="utf-8"))

    def test_collect_parses_notify_lines_with_op_lookup_fields(self) -> None:
        self.notify_log.write_text(
            f"{self._ts(12)} bopeep-watch bopeep-watch FAILED http=200 mode=item cmds=1 outcome=ok reason=harvest failed: HTTP Error 401\n"
            f"{self._ts(8)} guild guild-host-sync FAILED op-read-failed mode=legacy-refs cmds=1 outcome=op-error exit=1 reason=fetch failed\n",
            encoding="utf-8",
        )
        res = run_triage(["collect", "--hosts", "local", "--dry-run"], self.base_env)
        self.assertEqual(res.returncode, 0, res.stderr)
        data = json.loads(res.stdout)
        self.assertEqual(len(data["incidents"]), 2)
        self.assertEqual(data["incidents"][0]["job"], "bopeep-watch")
        self.assertEqual(data["incidents"][0]["delivery"], "http=200")
        self.assertEqual(data["incidents"][0]["reason"], "harvest failed: HTTP Error 401")
        self.assertEqual(data["incidents"][1]["job"], "guild")
        self.assertEqual(data["incidents"][1]["delivery"], "op-read-failed")
        self.assertEqual(data["incidents"][1]["reason"], "fetch failed")

    def test_resolve_local_paths_prefers_active_guild_release(self) -> None:
        home = self.tmp / "home"
        rel_qm = home / ".local" / "share" / "guild" / "releases" / "g-123" / "code" / "quartermaster" / "bin"
        rel_qm.mkdir(parents=True, exist_ok=True)
        current = home / ".local" / "share" / "guild" / "current"
        current.symlink_to(home / ".local" / "share" / "guild" / "releases" / "g-123")
        (rel_qm / "jobs_env.py").write_text(
            f"print('JOB_CFG_NOTIFY_LOG={self.notify_log}')\n"
            f"print('JOB_CFG_ROOT={self.jobs}')\n"
            f"print('JOB_CFG_HEALTH_DIR={self.health}')\n",
            encoding="utf-8",
        )
        stale_qm = home / "code" / "quartermaster" / "bin"
        stale_qm.mkdir(parents=True, exist_ok=True)
        (stale_qm / "jobs_env.py").write_text("import sys; sys.exit(3)\n", encoding="utf-8")

        self.notify_log.write_text(
            f"{self._ts(5)} review-widgets-1 review-widgets-1 STOPPED http=200 mode=item cmds=1 outcome=ok reason=preflight timed out\n",
            encoding="utf-8",
        )
        env = {
            "PATH": self.base_env["PATH"],
            "HOME": str(home),
            "GUILD_TRIAGE_STATE_DIR": str(self.state),
            "GUILD_TRIAGE_KILL_SWITCH": str(self.cfg / "triage.off"),
        }
        res = run_triage(["collect", "--hosts", "local", "--dry-run"], env)
        self.assertEqual(res.returncode, 0, res.stderr)
        data = json.loads(res.stdout)
        self.assertEqual(data["hosts"]["local"]["notify_log"], str(self.notify_log))
        self.assertEqual(len(data["incidents"]), 1)
        self.assertEqual(data["incidents"][0]["reason"], "preflight timed out")


if __name__ == "__main__":
    unittest.main()
