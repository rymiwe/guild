#!/usr/bin/env python3
"""Step 4: guild-update installs only CI-passed Guild commits, and guild-exec-host routes tools."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PINS = json.loads((ROOT / "tests" / "member-pins.json").read_text(encoding="utf-8"))

sys.path.insert(0, str(ROOT / "tests"))
import test_pr_gate_contract as step1  # noqa: E402

REAL_HOME = step1.REAL_HOME
STUB_GH = """#!/bin/sh
# gh api repos/acme/guild/actions/runs?head_sha=SHA&event=push... --jq ...: print the prepared lines.
case "$*" in
  "api repos/acme/guild/actions/runs?head_sha="*"&event=push&"*) cat "$GH_RUNS_FILE" ;;
  *) echo "unexpected gh call: $*" >&2; exit 1 ;;
esac
"""
STUB_NOTIFY = """#!/bin/sh
printf '%s\\n' "$*" >>"$NOTIFY_LOG"
"""
STUB_FALLBACK = """#!/bin/sh
echo "fallback $*"
"""


class GuildUpdateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp_ctx = tempfile.TemporaryDirectory(prefix="guild-update-")
        cls.tmp = Path(cls.tmp_ctx.name).resolve()
        cls.git = step1.find_system_git()
        cls.home = cls.tmp / "home"
        cls.home.mkdir()
        cls.git_env = {
            "PATH": step1.build_clean_path(),
            "HOME": str(cls.home),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_AUTHOR_NAME": "Acme Author",
            "GIT_AUTHOR_EMAIL": "author@acme.example",
            "GIT_COMMITTER_NAME": "Acme Author",
            "GIT_COMMITTER_EMAIL": "author@acme.example",
        }

        # Member checkouts whose origin is local, so guild-update's fetch stays offline.
        raw = Path(os.environ.get("GUILD_MEMBER_SOURCES_DIR") or REAL_HOME / "code").resolve()
        cls.sources = cls.tmp / "sources"
        origins = cls.tmp / "origins"
        origins.mkdir()
        for name, sha in PINS.items():
            src, dest = raw / name, cls.sources / name
            if cls._git("-C", str(src), "rev-parse", "--is-shallow-repository").stdout.strip() == "true":
                shutil.copytree(src, dest, symlinks=True)
                cls._git("-C", str(dest), "checkout", "-q", "-B", "main", sha)
                cls._git("-C", str(dest), "remote", "set-url", "origin", str(dest))
            else:
                bare = origins / f"{name}.git"
                cls._git("init", "-q", "--bare", "-b", "main", str(bare))
                cls._git("-C", str(src), "push", "-q", str(bare), f"{sha}:refs/heads/main")
                cls._git("clone", "-q", str(bare), str(dest))

        cls.pstack = cls.tmp / "cursor-plugins" / "pstack"
        (cls.pstack / "skills" / "poteto-mode").mkdir(parents=True)
        (cls.pstack / "skills" / "poteto-mode" / "SKILL.md").write_text("---\nname: poteto-mode\n---\n")
        kit = cls.pstack.parent / "cursor-team-kit" / "skills" / "deslop"
        kit.mkdir(parents=True)
        (kit / "SKILL.md").write_text("---\nname: deslop\n---\n")

        # A Guild origin holding this working tree's bin/ and tests/.
        cls.guild_origin = cls.tmp / "guild.git"
        cls._git("init", "-q", "--bare", "-b", "main", str(cls.guild_origin))
        cls.guild_work = cls.tmp / "guild-work"
        cls._git("clone", "-q", str(cls.guild_origin), str(cls.guild_work))
        for part in ("bin", "tests"):
            shutil.copytree(ROOT / part, cls.guild_work / part, ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(ROOT / "roster", cls.guild_work / "roster")
        cls.commit_guild("Guild at test time")

        cls.stub_bin = cls.tmp / "stub-bin"
        cls.stub_bin.mkdir()
        for name, body in (("gh", STUB_GH), ("job-notify", STUB_NOTIFY), ("fallback-exec", STUB_FALLBACK)):
            (cls.stub_bin / name).write_text(body)
            (cls.stub_bin / name).chmod(0o755)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp_ctx.cleanup()

    @classmethod
    def _git(cls, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [cls.git, *args], cwd=cwd, env=cls.git_env, text=True, capture_output=True, check=True
        )

    @classmethod
    def commit_guild(cls, message: str) -> str:
        cls._git("add", "-A", cwd=cls.guild_work)
        cls._git("commit", "-qm", message, cwd=cls.guild_work)
        cls._git("push", "-q", "origin", "HEAD:main", cwd=cls.guild_work)
        return cls._git("rev-parse", "HEAD", cwd=cls.guild_work).stdout.strip()

    def setUp(self) -> None:
        case = self.tmp / self.id().rsplit(".", 1)[-1]
        self.root = case / "root"
        self.runs = case / "runs.txt"
        self.notify_log = case / "notify.log"
        self.root.mkdir(parents=True)
        self._git("clone", "-q", str(self.guild_origin), str(self.root / "src"))

    def update(self, runs: str, home: Path | None = None) -> subprocess.CompletedProcess[str]:
        self.runs.write_text(runs)
        env = {
            **self.git_env,
            **({"HOME": str(home)} if home else {}),
            "PATH": step1.build_clean_path(self.stub_bin),
            "GUILD_RELEASE_ROOT": str(self.root),
            "GUILD_MEMBER_SOURCES_DIR": str(self.sources),
            "GUILD_UPDATE_RESTART": "",
            "GUILD_UPDATE_REPO": "acme/guild",
            "PSTACK_HOME": str(self.pstack),
            "GH_RUNS_FILE": str(self.runs),
            "NOTIFY_LOG": str(self.notify_log),
        }
        return subprocess.run(
            [str(self.root / "src" / "bin" / "guild-update")],
            env=env, text=True, capture_output=True, timeout=300,
        )

    def current(self) -> str | None:
        link = self.root / "current"
        return os.readlink(link) if link.is_symlink() else None

    def notifications(self, status: str = "FAILED") -> list[str]:
        lines = self.notify_log.read_text().splitlines() if self.notify_log.exists() else []
        return [line for line in lines if f" guild-update {status} " in line]

    def head_id(self) -> str:
        return "g-" + self._git("rev-parse", "HEAD", cwd=self.guild_work).stdout.strip()[:12]

    def test_home_check_pings_once_per_release_that_home_lags(self) -> None:
        # A real HOME layout: quartermaster's --check finds Guild's host guild-exec under it.
        home = self.root.parent / "home"
        self.root = home / ".local" / "share" / "guild"
        self.root.mkdir(parents=True)
        self._git("clone", "-q", str(self.guild_origin), str(self.root / "src"))
        runs = "completed success pr-gate-contract\n"

        # An empty HOME lags every member.
        proc = self.update(runs, home)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        first = self.head_id()
        self.assertEqual(self.current(), f"releases/{first}")
        lag = self.notifications("HOME-LAG")
        self.assertEqual(len(lag), 1, proc.stderr)
        for member in PINS:
            self.assertIn(member, lag[0])

        # Install every member into HOME through current/, as a release machine does.
        gate = home / ".local" / "share" / "quartermaster" / "bin"
        (home / ".local" / "bin").mkdir(parents=True)
        os.symlink(self.root / "src" / "bin" / "guild-exec-host", home / ".local" / "bin" / "guild-exec")
        (home / ".bash_profile").write_text(f"PATH={gate}:$PATH\n")
        env = {**self.git_env, "HOME": str(home), "PSTACK_HOME": str(self.pstack),
               "PATH": step1.build_clean_path(gate, home / ".local" / "bin")}
        for member in PINS:
            installer = self.root / "current" / "code" / member / "install.sh"
            subprocess.run(["bash", str(installer)], env=env, check=True, capture_output=True)

        # The next release finds HOME current, unit and skill links included.
        (self.guild_work / "home-check-marker").write_text("1\n")
        try:
            self.commit_guild("A second release")
            proc = self.update(runs, home)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(self.current(), f"releases/{self.head_id()}")
            self.assertNotIn("HOME lags", proc.stderr)
            self.assertEqual(len(self.notifications("HOME-LAG")), 1)

            # A wrapper removed by hand shows up at the next release, which names only its member.
            (home / ".local" / "bin" / "conclave-review").unlink()
            (self.guild_work / "home-check-marker").write_text("2\n")
            self.commit_guild("A third release")
            proc = self.update(runs, home)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            lag = self.notifications("HOME-LAG")
            self.assertEqual(len(lag), 2)
            self.assertIn("for: conclave (", lag[1])
            self.assertFalse((self.root / "releases" / first).exists(), "prune should drop the first release")
            self.assertTrue((home / ".claude" / "skills" / "bopeep").exists(), "the skill link must not dangle")
        finally:
            (self.guild_work / "home-check-marker").unlink()
            self.commit_guild("Drop the marker")

    def test_member_fetch_error_is_transient(self) -> None:
        bopeep = self.sources / "bopeep"
        url = self._git("-C", str(bopeep), "remote", "get-url", "origin").stdout.strip()
        self._git("-C", str(bopeep), "remote", "set-url", "origin", str(self.tmp / "missing.git"))
        try:
            proc = self.update("completed success pr-gate-contract\n")
        finally:
            self._git("-C", str(bopeep), "remote", "set-url", "origin", url)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("will retry", proc.stderr)
        self.assertIsNone(self.current())
        self.assertEqual(self.notifications(), [])

        # From the 8th run in a row the error fails each run but pings once.
        self._git("-C", str(bopeep), "remote", "set-url", "origin", str(self.tmp / "missing.git"))
        try:
            codes = [self.update("completed success pr-gate-contract\n").returncode for _ in range(9)]
        finally:
            self._git("-C", str(bopeep), "remote", "set-url", "origin", url)
        self.assertEqual(codes, [0] * 6 + [1, 1, 1])
        self.assertEqual(len(self.notifications()), 1)
        self.assertIn("runs in a row", self.notifications()[0])

    def test_hand_rollback_stands_after_prune(self) -> None:
        bin_ = str(ROOT / "bin" / "guild-release")
        env = {**self.git_env, "PATH": step1.build_clean_path(),
               "GUILD_MEMBER_SOURCES_DIR": str(self.sources), "PSTACK_HOME": str(self.pstack)}
        deploy = [sys.executable, bin_, "deploy", "--root", str(self.root), "--release-id"]
        subprocess.run([*deploy, "old"], env=env, check=True, capture_output=True, timeout=300)
        new = self.head_id()
        self.assertEqual(self.update("completed success pr-gate-contract\n").returncode, 0)
        self.assertEqual(self.current(), f"releases/{new}")
        subprocess.run([sys.executable, bin_, "rollback", "--root", str(self.root)],
                       env=env, check=True, capture_output=True, timeout=300)
        # A hand deploy pushes the rolled-away release out of previous, so prune removes it.
        subprocess.run([*deploy, "hand"], env=env, check=True, capture_output=True, timeout=300)
        subprocess.run([sys.executable, bin_, "prune", "--root", str(self.root)],
                       env=env, check=True, capture_output=True, timeout=300)
        self.assertFalse((self.root / "releases" / new).exists(), "prune should remove the rolled-away release")
        proc = self.update("completed success pr-gate-contract\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.current(), "releases/hand", "the updater must not undo a hand rollback")

    def test_waits_while_ci_runs_and_when_no_runs(self) -> None:
        for runs in ("", "in_progress - pr-gate-contract\n"):
            proc = self.update(runs)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("waiting", proc.stderr)
        self.assertIsNone(self.current())
        self.assertEqual(self.notifications(), [])

        # 24 hours without a finished run fails and pings once.
        waiting = self.root / "update-state" / f"waiting-{self.head_id()}"
        os.utime(waiting, (0, 0))
        for _ in range(2):
            self.assertEqual(self.update("in_progress - CI\n").returncode, 1)
        self.assertEqual(len(self.notifications()), 1)
        self.assertIn("after 24 hours", self.notifications()[0])

    def test_failed_ci_reports_once_and_installs_nothing(self) -> None:
        proc = self.update("completed failure pr-gate-contract\n")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("CI did not pass", proc.stderr)
        self.assertIsNone(self.current())
        self.assertEqual(len(self.notifications()), 1)
        self.assertIn("guild-update FAILED", self.notifications()[0])

        proc = self.update("completed failure pr-gate-contract\n")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(len(self.notifications()), 1, "a known failure must not ping again")

        # A re-run of the failed check that passes recovers the same commit.
        proc = self.update("completed success pr-gate-contract\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.current(), f"releases/{self.head_id()}")

    def test_passed_ci_activates_then_bad_candidate_keeps_it(self) -> None:
        good = self.head_id()
        proc = self.update("completed success pr-gate-contract\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.current(), f"releases/{good}")
        proc = self.update("completed success pr-gate-contract\n")
        self.assertEqual((proc.returncode, proc.stderr), (0, ""), "an installed commit is a no-op")

        # guild-exec-host: a release tool runs from the release, any other from the fallback.
        host_env = {
            **self.git_env,
            "PATH": step1.build_clean_path(self.stub_bin),
            "GUILD_RELEASE_ROOT": str(self.root),
            "GUILD_EXEC_FALLBACK": str(self.stub_bin / "fallback-exec"),
        }
        host = str(ROOT / "bin" / "guild-exec-host")
        lint = subprocess.run([host, "bopeep", "bin/pr-lint", "--help"], env=host_env, text=True, capture_output=True)
        self.assertEqual(lint.returncode, 0, lint.stderr)
        self.assertIn("usage: pr-lint", lint.stdout)
        other = subprocess.run([host, "orderly", "bin/ship-report"], env=host_env, text=True, capture_output=True)
        self.assertEqual(other.stdout.strip(), "fallback orderly bin/ship-report")

        # A CI-passed commit whose pin cannot be checked out fails and leaves the release active.
        pins = json.loads((self.guild_work / "tests" / "member-pins.json").read_text())
        pins["bopeep"] = "0" * 40
        (self.guild_work / "tests" / "member-pins.json").write_text(json.dumps(pins, indent=2) + "\n")
        try:
            self.commit_guild("Pin a missing bopeep commit")
            proc = self.update("completed success pr-gate-contract\n")
            self.assertEqual(proc.returncode, 1)
            self.assertIn("deploy of", proc.stderr)
            self.assertEqual(self.current(), f"releases/{good}")
            self.assertEqual(len(self.notifications()), 1)
            proc = self.update("completed success pr-gate-contract\n")
            self.assertEqual(proc.returncode, 1)
            self.assertIn("to retry", proc.stderr, "a failed deploy must not rebuild every run")
            self.assertEqual(len(self.notifications()), 1)
            src_head = self._git("-C", str(self.root / "src"), "rev-parse", "HEAD").stdout.strip()
            self.assertEqual("g-" + src_head[:12], good, "a failed deploy must not move the updater checkout")
        finally:
            (self.guild_work / "tests" / "member-pins.json").write_text(json.dumps(PINS, indent=2) + "\n")
            self.commit_guild("Restore pins")


if __name__ == "__main__":
    unittest.main()
