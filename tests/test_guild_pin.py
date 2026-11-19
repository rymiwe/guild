#!/usr/bin/env python3
"""guild-pin plans pin moves from member trunk heads and changes nothing without --apply."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OLD = {"alpha": "a" * 40, "beta": "b" * 40}
STUB_GH = """#!/usr/bin/env python3
import json, os, sys
data = json.load(open(os.environ["GH_FIXTURE"]))
path = sys.argv[2]
for key, out in data.items():
    if path == key:
        print(out)
        sys.exit(0)
print("unexpected gh call: " + " ".join(sys.argv[1:]), file=sys.stderr)
sys.exit(1)
"""


class GuildPinTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="guild-pin-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        env = {"PATH": f"{self.tmp / 'bin'}:/usr/bin:/bin", "HOME": str(self.tmp), "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@acme.example",
               "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@acme.example",
               "GH_FIXTURE": str(self.tmp / "gh.json")}
        self.env = env
        (self.tmp / "bin").mkdir()
        (self.tmp / "bin" / "gh").write_text(STUB_GH)
        (self.tmp / "bin" / "gh").chmod(0o755)
        bare, self.repo = self.tmp / "origin.git", self.tmp / "guild"
        self.git("init", "-q", "--bare", "-b", "main", str(bare), cwd=self.tmp)
        (self.repo / "bin").mkdir(parents=True)
        (self.repo / "tests").mkdir()
        shutil.copy(ROOT / "bin" / "guild-pin", self.repo / "bin" / "guild-pin")
        (self.repo / "tests" / "member-pins.json").write_text(json.dumps(OLD, indent=2) + "\n")
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

    def run_pin(self, gh: dict[str, str], *args: str) -> subprocess.CompletedProcess:
        (self.tmp / "gh.json").write_text(json.dumps(gh))
        return subprocess.run([str(self.repo / "bin" / "guild-pin"), *args], cwd=self.repo, env=self.env,
                              capture_output=True, text=True)

    def state(self) -> str:
        return self.git("for-each-ref", cwd=self.repo) + self.git("status", "--porcelain", cwd=self.repo)

    def test_green_head_moves_and_red_head_is_blocked(self) -> None:
        new_a, new_b = "c" * 40, "d" * 40
        gh = {
            "repos/acme/alpha/commits/HEAD": new_a,
            "repos/acme/alpha/commits/" + new_a + "/check-runs": json.dumps(
                [{"name": "test", "status": "completed", "conclusion": "success"}]),
            f"repos/acme/alpha/compare/{OLD['alpha']}...{new_a}": "c" * 7 + " Fix the gate",
            "repos/acme/beta/commits/HEAD": new_b,
            "repos/acme/beta/commits/" + new_b + "/check-runs": json.dumps(
                [{"name": "suite", "status": "completed", "conclusion": "failure"}]),
            f"repos/acme/beta/compare/{OLD['beta']}...{new_b}": "d" * 7 + " Break it",
        }
        before = self.state()
        out = self.run_pin(gh)
        self.assertEqual(out.returncode, 1, out.stderr)
        self.assertIn("**alpha** aaaaaaa -> ccccccc (checks passed)", out.stdout)
        self.assertIn("[ccccccc](https://github.com/acme/alpha/commit/ccccccc) Fix the gate", out.stdout)
        self.assertIn("**beta** bbbbbbb -> ddddddd (blocked: suite failure)", out.stdout)
        self.assertEqual(self.state(), before, "a plan without --apply changed the checkout")

    def test_member_without_check_runs_moves_and_says_so(self) -> None:
        new_a = "e" * 40
        gh = {"repos/acme/alpha/commits/HEAD": new_a,
              "repos/acme/alpha/commits/" + new_a + "/check-runs": "[]",
              f"repos/acme/alpha/compare/{OLD['alpha']}...{new_a}": ""}
        out = self.run_pin(gh, "alpha")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("**alpha** aaaaaaa -> eeeeeee (no check runs)", out.stdout)

    def test_pins_at_trunk_need_nothing(self) -> None:
        gh = {"repos/acme/alpha/commits/HEAD": OLD["alpha"], "repos/acme/beta/commits/HEAD": OLD["beta"]}
        out = self.run_pin(gh)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("every pin is at its member's trunk head", out.stdout)

    def test_unknown_member_is_refused(self) -> None:
        out = self.run_pin({}, "gamma")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("not a pinned member: gamma", out.stderr)


if __name__ == "__main__":
    unittest.main()
