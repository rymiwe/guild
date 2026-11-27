#!/usr/bin/env python3
"""guild-local-pass runs a command at one exact commit and posts the result as a local-tests status."""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STUB_GH = """#!/bin/sh
printf '%s\\n' "$*" >> "$GH_LOG"
"""


class GuildLocalPassTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_ctx = tempfile.TemporaryDirectory(prefix="guild-local-pass-")
        self.addCleanup(self.tmp_ctx.cleanup)
        self.tmp = Path(self.tmp_ctx.name).resolve()
        (self.tmp / "bin").mkdir()
        (self.tmp / "bin" / "gh").write_text(STUB_GH)
        (self.tmp / "bin" / "gh").chmod(0o755)
        self.env = {"PATH": f"{self.tmp / 'bin'}:{os.environ['PATH']}", "HOME": str(self.tmp),
                    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
                    "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@acme.example",
                    "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@acme.example",
                    "GUILD_MEMBER_SOURCES_DIR": str(self.tmp / "code"), "GH_LOG": str(self.tmp / "gh.log")}
        self.src = self.tmp / "code" / "alpha"
        self.src.mkdir(parents=True)
        self.git("init", "-q", "-b", "main")
        self.git("remote", "add", "origin", "git@github.com:acme/alpha.git")
        self.shas = []
        for n in ("one", "two"):
            (self.src / "marker").write_text(n + "\n")
            self.git("add", "-A")
            self.git("commit", "-qm", n)
            self.shas.append(self.git("rev-parse", "HEAD"))

    def git(self, *args: str) -> str:
        out = subprocess.run(["git", *args], cwd=self.src, env=self.env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip()

    def run_pass(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([str(ROOT / "bin" / "guild-local-pass"), *args], env=self.env,
                              capture_output=True, text=True)

    def posts(self) -> list[str]:
        log = self.tmp / "gh.log"
        return log.read_text().splitlines() if log.exists() else []

    def test_command_runs_at_the_given_commit_and_its_pass_posts_to_that_commit(self) -> None:
        first = self.shas[0]
        out = self.run_pass("alpha", first[:7], "--", "sh", "-c", 'test "$(cat marker)" = one')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(len(self.posts()), 1)
        post = self.posts()[0]
        self.assertIn(f"api -X POST repos/acme/alpha/statuses/{first} ", post)
        self.assertIn("-f state=success -f context=local-tests", post)
        self.assertRegex(post, r"description=passed on \S+: sh -c ")
        self.assertEqual(len(self.git("worktree", "list").splitlines()), 1, "the worktree must go")
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_failing_command_posts_failure_and_keeps_its_exit_code(self) -> None:
        out = self.run_pass("alpha", self.shas[1], "--", "sh", "-c", "exit 3")
        self.assertEqual(out.returncode, 3, out.stderr)
        self.assertIn(f"statuses/{self.shas[1]} -f state=failure", self.posts()[0])
        self.assertIn("description=failed (exit 3) on", self.posts()[0])

    def test_long_command_keeps_its_end_within_githubs_limit(self) -> None:
        out = self.run_pass("alpha", self.shas[1], "--", "true", "x" * 200, "last-argument")
        self.assertEqual(out.returncode, 0, out.stderr)
        description = self.posts()[0].split("description=", 1)[1]
        self.assertEqual(len(description), 140)
        self.assertTrue(description.endswith(" last-argument"), description)

    def test_unknown_commit_and_bad_usage_post_nothing(self) -> None:
        self.assertEqual(self.run_pass("alpha", "deadbee", "--", "true").returncode, 1)
        self.assertEqual(self.run_pass("alpha", self.shas[0], "true").returncode, 2)
        self.assertEqual(self.posts(), [])


class LocalSuiteMatchesCiTest(unittest.TestCase):
    def test_local_suite_runs_the_ci_test_steps_in_order(self) -> None:
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        local = (ROOT / "scripts" / "local-suite").read_text()
        python = re.compile(r"python3 (?:-\S+ )*tests/\S+(?: -v)?")
        bridgekeeper = re.compile(r"for t in ([\w -]+);")
        self.assertEqual(python.findall(local), python.findall(ci))
        self.assertEqual(bridgekeeper.findall(local), bridgekeeper.findall(ci))
        self.assertTrue(python.findall(ci))


if __name__ == "__main__":
    unittest.main()
