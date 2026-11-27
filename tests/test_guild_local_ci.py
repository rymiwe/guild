#!/usr/bin/env python3
"""guild-local-ci runs a suite on each commit a gate waits on; ci-step runs a workflow's steps."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PINS = {"quartermaster": "a" * 40, "conclave": "b" * 40, "bopeep": "c" * 40}
STUB_GH = """#!/usr/bin/env python3
import json, os, sys
data = json.load(open(os.environ["GH_FIXTURE"]))
key = sys.argv[2]
if key not in data:
    print("unexpected gh call: " + " ".join(sys.argv[1:]), file=sys.stderr)
    sys.exit(1)
print(data[key])
"""
STUB_PASS = """#!/bin/sh
printf '%s\\n' "$*" >> "$PASS_LOG"
case "$2" in fail*) echo boom; exit 1 ;; esac
echo ok
"""


class GuildLocalCiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_ctx = tempfile.TemporaryDirectory(prefix="guild-local-ci-")
        self.addCleanup(self.tmp_ctx.cleanup)
        self.tmp = Path(self.tmp_ctx.name).resolve()
        self.env = {"PATH": f"{self.tmp / 'stub'}:{os.environ['PATH']}", "HOME": str(self.tmp),
                    "XDG_STATE_HOME": str(self.tmp / "state"), "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@acme.example",
                    "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@acme.example",
                    "GH_FIXTURE": str(self.tmp / "gh.json"), "PASS_LOG": str(self.tmp / "pass.log")}
        (self.tmp / "stub").mkdir()
        (self.tmp / "stub" / "gh").write_text(STUB_GH)
        (self.tmp / "stub" / "gh").chmod(0o755)
        bare, self.repo = self.tmp / "origin.git", self.tmp / "guild"
        (self.repo / "bin").mkdir(parents=True)
        (self.repo / "tests").mkdir()
        shutil.copy(ROOT / "bin" / "guild-local-ci", self.repo / "bin" / "guild-local-ci")
        (self.repo / "bin" / "guild-local-pass").write_text(STUB_PASS)
        (self.repo / "bin" / "guild-local-pass").chmod(0o755)
        (self.repo / "tests" / "member-pins.json").write_text(json.dumps(PINS, indent=2) + "\n")
        self.git("init", "-q", "--bare", "-b", "main", str(bare), cwd=self.tmp)
        self.git("init", "-q", "-b", "main", cwd=self.repo)
        self.git("add", "-A", cwd=self.repo)
        self.git("commit", "-qm", "pins", cwd=self.repo)
        self.git("remote", "add", "origin", "https://github.com/acme/guild.git", cwd=self.repo)
        self.git("config", f"url.{bare}.insteadOf", "https://github.com/acme/guild.git", cwd=self.repo)
        self.git("push", "-q", "origin", "main", cwd=self.repo)

    def git(self, *args: str, cwd: Path) -> str:
        out = subprocess.run(["git", *args], cwd=cwd, env=self.env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout

    def run_ci(self, gh: dict) -> subprocess.CompletedProcess:
        (self.tmp / "gh.json").write_text(json.dumps(gh))
        return subprocess.run([str(self.repo / "bin" / "guild-local-ci")], cwd=self.tmp, env=self.env,
                              capture_output=True, text=True)

    def runs(self) -> list[str]:
        log = self.tmp / "pass.log"
        return log.read_text().splitlines() if log.exists() else []

    def fixture(self, **over: str) -> dict:
        gh = {"repos/acme/guild/actions/permissions": "false", "list": "[]",
              "repos/acme/quartermaster/actions/permissions": "false",
              "repos/acme/quartermaster/commits/HEAD": "d" * 40,
              f"repos/acme/quartermaster/commits/{'d' * 40}/status": "0",
              "repos/acme/conclave/actions/permissions": "true",
              "repos/acme/bopeep/actions/permissions": "false",
              "repos/acme/bopeep/commits/HEAD": PINS["bopeep"]}
        gh.update(over)
        return gh

    def test_runs_each_waiting_head_once_and_skips_the_rest(self) -> None:
        pr_head = "e" * 40
        gh = self.fixture(list=json.dumps([
            {"headRefName": "pin/quartermaster-ddddddd", "headRefOid": pr_head, "isCrossRepository": False},
            {"headRefName": "feature", "headRefOid": "f" * 40, "isCrossRepository": False}]),
            **{f"repos/acme/guild/commits/{pr_head}/status": "0"})
        out = self.run_ci(gh)
        self.assertEqual(out.returncode, 0, out.stderr)
        # Quartermaster's new head runs first; conclave has Actions on, bopeep sits at its pin, and only
        # the pin PR head of Guild's open PRs runs.
        runs = self.runs()
        self.assertEqual([r.split(" --")[0] for r in runs],
                         [f"quartermaster {'d' * 40}", f"guild {pr_head}"])
        self.assertIn("-- bash ", runs[0])
        self.assertTrue(runs[0].endswith(".github/workflows/tests.yml Run the test suite"), runs[0])
        self.assertTrue(runs[1].endswith("-- scripts/local-suite"), runs[1])
        log = self.tmp / "state" / "guild" / "local-ci" / f"quartermaster-{'d' * 12}.log"
        self.assertEqual(log.read_text(), "ok\n")

    def test_a_head_with_any_local_status_is_left_alone(self) -> None:
        # A failed run stays failed until someone reruns it, rather than rerunning every 10 minutes.
        out = self.run_ci(self.fixture(**{f"repos/acme/quartermaster/commits/{'d' * 40}/status": "1"}))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.runs(), [])
        self.assertIn("no commit waits on a local run", out.stdout)

    def test_a_failed_suite_fails_the_run_and_unreadable_actions_state_runs_nothing(self) -> None:
        failing = "fail" + "0" * 36
        out = self.run_ci(self.fixture(**{"repos/acme/quartermaster/commits/HEAD": failing,
                                          f"repos/acme/quartermaster/commits/{failing}/status": "0"}))
        self.assertEqual(out.returncode, 1)
        self.assertIn("quartermaster fail000 failed", out.stdout)
        gh = self.fixture()
        del gh["repos/acme/quartermaster/actions/permissions"]
        out = self.run_ci(gh)
        self.assertEqual(out.returncode, 1)
        self.assertEqual(len(self.runs()), 1, "a run went ahead without the Actions state")


class CiStepTest(unittest.TestCase):
    def test_runs_named_steps_in_a_clean_env_and_fails_on_a_missing_step(self) -> None:
        with tempfile.TemporaryDirectory(prefix="ci-step-") as tmp:
            work = Path(tmp)
            (work / "wf.yml").write_text("""jobs:
  test:
    steps:
      - uses: actions/checkout@v4
      - name: Block
        run: |
          echo "block home=$HOME" > block.out
          env -i PATH=/usr/bin:/bin sh -c 'echo "$PATH"' >> block.out
      - name: Line
        run: echo "line extra=$EXTRA secret=${SECRET:-unset}" > line.out
      - name: Later
        run: echo later > later.out
""")
            env = {**os.environ, "CI_ENV": "EXTRA=yes", "SECRET": "leak"}
            step = ["bash", str(ROOT / "scripts" / "ci-step"), "wf.yml"]
            out = subprocess.run([*step, "Block", "Line"], cwd=work, env=env, capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)
            block = (work / "block.out").read_text().splitlines()
            self.assertNotEqual(block[0], f"block home={os.environ['HOME']}")
            self.assertTrue(block[1].endswith(":/usr/bin:/bin") and block[1] != "/usr/bin:/bin", block[1])
            self.assertEqual((work / "line.out").read_text(), "line extra=yes secret=unset\n")
            self.assertFalse((work / "later.out").exists())
            out = subprocess.run([*step, "Missing"], cwd=work, env=env, capture_output=True, text=True)
            self.assertNotEqual(out.returncode, 0)
            self.assertIn("no step named 'Missing'", out.stderr)


if __name__ == "__main__":
    unittest.main()
