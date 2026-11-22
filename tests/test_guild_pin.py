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
with open(os.environ["GH_FIXTURE"] + ".log", "a") as log:
    log.write(" ".join(sys.argv[1:]) + "\\n")
for key, out in data.items():
    if path == key:
        if isinstance(out, list):
            # A list is the reply sequence for repeated calls; the last reply repeats.
            seen = os.environ["GH_FIXTURE"] + "." + key
            n = int(open(seen).read()) if os.path.exists(seen) else 0
            open(seen, "w").write(str(n + 1))
            out, rc = out[min(n, len(out) - 1)]
            print(out)
            sys.exit(rc)
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
        (self.tmp / "bin" / "job-notify").write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$NOTIFY_LOG\"\n"
        )
        (self.tmp / "bin" / "job-notify").chmod(0o755)
        env["NOTIFY_LOG"] = str(self.tmp / "notify.log")
        env["GUILD_PIN_STATE_DIR"] = str(self.tmp / "pin-state")
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

    def run_pin(self, gh: dict, *args: str) -> subprocess.CompletedProcess:
        (self.tmp / "gh.json").write_text(json.dumps(gh))
        return subprocess.run([str(self.repo / "bin" / "guild-pin"), *args], cwd=self.repo, env=self.env,
                              capture_output=True, text=True)

    def notifications(self) -> list[str]:
        p = self.tmp / "notify.log"
        return p.read_text().splitlines() if p.exists() else []

    def state(self) -> str:
        # Branches, origin/main and the work tree; refs/guild-pin/main is the plan's own fetch of origin.
        return (self.git("for-each-ref", "refs/heads", "refs/remotes", cwd=self.repo)
                + self.git("status", "--porcelain", cwd=self.repo))

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

    def apply_fixture(self, checks: list) -> dict:
        new_a = "f" * 40
        return {"repos/acme/alpha/commits/HEAD": new_a,
                "repos/acme/alpha/commits/" + new_a + "/check-runs": "[]",
                f"repos/acme/alpha/compare/{OLD['alpha']}...{new_a}": "",
                "create": "https://github.com/acme/guild/pull/9", "checks": checks, "merge": ""}

    def gh_calls(self) -> list[str]:
        return (self.tmp / "gh.json.log").read_text().splitlines()

    def test_apply_waits_for_checks_that_register_late_then_merges(self) -> None:
        out = self.run_pin(self.apply_fixture([["", 1], ['[{"name": "ci"}]', 0], ["", 0]]), "--apply", "alpha")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("guild-pin: merged https://github.com/acme/guild/pull/9", out.stdout)
        self.assertIn("pr merge https://github.com/acme/guild/pull/9 --merge", self.gh_calls())

    def test_apply_removes_the_pin_branch_after_the_merge(self) -> None:
        # gh's own --delete-branch checks out main locally, which fails when another worktree holds main.
        out = self.run_pin(self.apply_fixture([['[{"name": "ci"}]', 0], ["", 0]]), "--apply", "alpha")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("--delete-branch", " ".join(self.gh_calls()))
        self.assertEqual(self.git("ls-remote", "--heads", "origin", "pin/*", cwd=self.repo), "")
        self.assertEqual(self.git("branch", "--list", "pin/*", cwd=self.repo), "")

    def test_apply_does_not_merge_when_ci_fails(self) -> None:
        out = self.run_pin(self.apply_fixture([['[{"name": "ci"}]', 0], ["", 1]]), "--apply", "alpha")
        self.assertEqual(out.returncode, 1, out.stderr)
        self.assertIn("Guild CI did not pass on https://github.com/acme/guild/pull/9; not merged", out.stderr)
        self.assertFalse([c for c in self.gh_calls() if c.startswith("pr merge")], "merged after CI failed")
        self.assertEqual(self.git("branch", "--list", "pin/*", cwd=self.repo), "")

    def test_failed_commit_leaves_no_branch_and_the_next_run_retries(self) -> None:
        hook = self.repo / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\necho blocked >&2\nexit 1\n")
        hook.chmod(0o755)
        out = self.run_pin(self.apply_fixture([['[{"name": "ci"}]', 0], ["", 0]]), "--apply", "alpha")
        self.assertEqual(out.returncode, 1, out.stderr)
        self.assertEqual(self.git("branch", "--list", "pin/*", cwd=self.repo), "")
        hook.unlink()
        out = self.run_pin(self.apply_fixture([['[{"name": "ci"}]', 0], ["", 0]]), "--apply", "alpha")
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_auto_skips_when_open_pin_pr_exists(self) -> None:
        gh = {
            "list": json.dumps([{"number": 40, "headRefName": "pin/alpha-ccccccc",
                                "url": "https://github.com/acme/guild/pull/40"}]),
        }
        out = self.run_pin(gh, "--auto")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("open pin PR already exists: https://github.com/acme/guild/pull/40", out.stdout)
        self.assertFalse([c for c in self.gh_calls() if c.startswith("pr create")])
        self.assertEqual(self.notifications(), [])

    def test_auto_waits_with_exit_0_and_no_ping_when_checks_pending(self) -> None:
        new_a = "c" * 40
        gh = {
            "list": "[]",
            "repos/acme/alpha/commits/HEAD": new_a,
            "repos/acme/alpha/commits/" + new_a + "/check-runs": json.dumps(
                [{"name": "test", "status": "in_progress", "conclusion": None}]),
            f"repos/acme/alpha/compare/{OLD['alpha']}...{new_a}": "",
            "repos/acme/beta/commits/HEAD": OLD["beta"],
        }
        out = self.run_pin(gh, "--auto")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.notifications(), [])

    def test_auto_pings_once_on_failed_check_and_clears_on_recovery(self) -> None:
        new_b = "d" * 40
        bad_gh = {
            "list": "[]",
            "repos/acme/alpha/commits/HEAD": OLD["alpha"],
            "repos/acme/beta/commits/HEAD": new_b,
            "repos/acme/beta/commits/" + new_b + "/check-runs": json.dumps(
                [{"name": "suite", "status": "completed", "conclusion": "failure"}]),
            f"repos/acme/beta/compare/{OLD['beta']}...{new_b}": "",
        }
        self.assertEqual(self.run_pin(bad_gh, "--auto").returncode, 1)
        self.assertEqual(self.run_pin(bad_gh, "--auto").returncode, 1)
        self.assertEqual(len(self.notifications()), 1)
        self.assertIn("guild-pin FAILED", self.notifications()[0])

        ok_gh = {
            "list": "[]",
            "repos/acme/alpha/commits/HEAD": OLD["alpha"],
            "repos/acme/beta/commits/HEAD": OLD["beta"],
        }
        self.assertEqual(self.run_pin(ok_gh, "--auto").returncode, 0)
        self.assertEqual(list((self.tmp / "pin-state" / "pinged").iterdir()), [])

    def test_auto_reads_new_pins_while_another_fetch_holds_origin_main(self) -> None:
        # guild-host-sync fetches this checkout on its own timer and holds refs/remotes/origin/main's lock meanwhile.
        other = self.tmp / "other"
        self.git("clone", "-q", str(self.tmp / "origin.git"), str(other), cwd=self.tmp)
        (other / "tests" / "member-pins.json").write_text(json.dumps({**OLD, "alpha": "c" * 40}, indent=2) + "\n")
        self.git("commit", "-qam", "pin alpha", cwd=other)
        self.git("push", "-q", "origin", "main", cwd=other)
        (self.repo / ".git" / "refs" / "remotes" / "origin" / "main.lock").touch()
        gh = {"list": "[]", "repos/acme/alpha/commits/HEAD": "c" * 40, "repos/acme/beta/commits/HEAD": OLD["beta"]}
        out = self.run_pin(gh, "--auto")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("every pin is at its member's trunk head", out.stdout)

    def test_auto_skips_when_locked(self) -> None:
        import fcntl
        sdir = self.tmp / "pin-state"
        sdir.mkdir(parents=True, exist_ok=True)
        with open(sdir / "lock", "w", encoding="utf-8") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            out = self.run_pin({}, "--auto")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("another run is in progress", out.stderr)

    def test_unknown_member_is_refused(self) -> None:
        out = self.run_pin({}, "gamma")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("not a pinned member: gamma", out.stderr)

    def write_failing_fetch_git(self, stderr_msg: str) -> Path:
        real_git = shutil.which("git", path="/usr/bin:/bin") or "/usr/bin/git"
        wrapper = self.tmp / "bin" / "git"
        wrapper.write_text(f'#!/bin/sh\n[ "$1" = "fetch" ] && {{ printf "%s\\n" {json.dumps(stderr_msg)} >&2; exit 1; }}\nexec "{real_git}" "$@"\n')
        wrapper.chmod(0o755)
        return wrapper

    def failing_fetch_runs(self, count: int) -> list[int]:
        codes = []
        for i in range(count):
            self.write_failing_fetch_git(f"error: cannot lock ref: is at {i:040x} but expected {i + 1:040x}")
            codes.append(self.run_pin({}, "--auto").returncode)
        return codes

    def test_auto_fetch_retry_recovery_streak_and_manual_failure(self) -> None:
        mismatch = (
            "error: cannot lock ref 'refs/remotes/origin/main': "
            "is at dc2256d392f31054eae43a68cd8f8e747034060b "
            "but expected d6083990581a7ebd6c1c42c62f5586a58a090f34"
        )
        wrapper = self.write_failing_fetch_git(mismatch)

        # Manual CLI fetch failure remains immediate on the first failure.
        manual = self.run_pin({})
        self.assertEqual(manual.returncode, 1)
        self.assertIn("cannot lock ref 'refs/remotes/origin/main'", manual.stderr)
        self.assertEqual(self.notifications(), [])

        # Automatic runs retry quietly for the first 7 runs with logs and no page.
        for _ in range(7):
            out = self.run_pin({}, "--auto")
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn("cannot lock ref 'refs/remotes/origin/main'", out.stderr)
            self.assertIn("will retry", out.stderr)
        self.assertEqual(self.notifications(), [])

        # A successful fetch resets the streak even when the GitHub call after it fails, which still pages at once.
        wrapper.unlink()
        self.assertEqual(self.run_pin({"list": "[]"}, "--auto").returncode, 1)
        self.assertEqual(len(self.notifications()), 1)

        # A lasting streak pages once, although each ref-lock error names different commits.
        self.assertEqual(self.failing_fetch_runs(10), [0] * 7 + [1, 1, 1])
        self.assertEqual(len(self.notifications()), 2)
        self.assertIn("8 or more runs in a row", self.notifications()[1])

        # A successful fetch resets the streak immediately, even when member checks are still pending.
        wrapper.unlink()
        new_a = "c" * 40
        pending_gh = {
            "list": "[]",
            "repos/acme/alpha/commits/HEAD": new_a,
            "repos/acme/alpha/commits/" + new_a + "/check-runs": json.dumps(
                [{"name": "test", "status": "in_progress", "conclusion": None}]),
            f"repos/acme/alpha/compare/{OLD['alpha']}...{new_a}": "",
            "repos/acme/beta/commits/HEAD": OLD["beta"],
        }
        self.assertEqual(self.run_pin(pending_gh, "--auto").returncode, 0)
        self.assertFalse((self.tmp / "pin-state" / "transient").exists())

        # Persistent streak after reset: first 7 succeed quietly, 8th+ return failure and page once per streak.
        self.assertEqual(self.failing_fetch_runs(10), [0] * 7 + [1, 1, 1])
        self.assertEqual(len(self.notifications()), 3)
        self.assertIn("8 or more runs in a row", self.notifications()[2])

        # Recovery clears the streak and ping marker.
        wrapper.unlink()
        ok_gh = {"list": "[]", "repos/acme/alpha/commits/HEAD": OLD["alpha"], "repos/acme/beta/commits/HEAD": OLD["beta"]}
        self.assertEqual(self.run_pin(ok_gh, "--auto").returncode, 0)
        self.assertFalse((self.tmp / "pin-state" / "transient").exists())
        self.assertFalse((self.tmp / "pin-state" / "pinged" / "streak").exists())


if __name__ == "__main__":
    unittest.main()
