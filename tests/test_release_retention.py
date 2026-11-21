#!/usr/bin/env python3
"""guild-release prune keeps a release that a running job's attribution baseline names.

A job's attribution baseline records every repo the job's git commands touched,
Guild release checkouts included. The job's end-of-job check fails closed when a
recorded repo is missing, so prune must keep that release until the job ends,
even when no process of the job has the release on PATH or in its environment.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUILD_RELEASE_BIN = ROOT / "bin" / "guild-release"


class ReleaseRetentionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="guild-retention-")).resolve()
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        self.root = self.tmp / "root"
        releases = self.root / "releases"
        for rid in ("r0", "r1", "r2", "r3"):
            (releases / rid / "code" / "conclave" / ".git").mkdir(parents=True)
        os.symlink("releases/r3", self.root / "current")
        os.symlink("releases/r2", self.root / "previous")
        self.jobs = self.tmp / "jobs"

    def job(self, name: str, *rids: str, text: str | None = None) -> Path:
        """A job dir whose baseline records each release's conclave checkout and one other repo."""
        d = self.jobs / name
        d.mkdir(parents=True)
        repos = {str(self.tmp / "work" / "widgets" / ".git"): {"repo": str(self.tmp / "work" / "widgets"), "tips": []}}
        for rid in rids:
            git_dir = str(self.root / "releases" / rid / "code" / "conclave" / ".git")
            repos[git_dir] = {"repo": git_dir, "tips": ["0" * 40]}
        if text is None:
            text = json.dumps({"repositories": repos, "discover": True, "started": 1})
        (d / ".acme.attribution-baseline.json").write_text(text, encoding="utf-8")
        return d

    def run_job(self, job_dir: Path) -> subprocess.Popen[bytes]:
        """A running job process: its cwd is the job dir, and nothing in it names a release."""
        proc = subprocess.Popen(["sleep", "300"], cwd=str(job_dir), env={"PATH": "/usr/bin:/bin"})
        self.addCleanup(self.settle, proc)
        return proc

    def settle(self, proc: subprocess.Popen[bytes]) -> None:
        proc.kill()
        proc.wait(timeout=10)

    def prune(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(GUILD_RELEASE_BIN), "prune", "--root", str(self.root), "--json", *extra],
            text=True, capture_output=True, timeout=60,
        )

    def releases(self) -> list[str]:
        return sorted(p.name for p in (self.root / "releases").iterdir())

    def test_running_job_baseline_keeps_only_its_release_until_the_job_ends(self) -> None:
        active = self.run_job(self.job("review-active", "r1"))
        self.job("review-settled", "r0")  # no process: this job has ended

        dry = self.prune("--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        report = json.loads(dry.stdout)
        self.assertEqual(report["removed"], ["r0"])
        self.assertIn(active.pid, report["in_use"]["r1"])

        pr = self.prune()
        self.assertEqual(pr.returncode, 0, pr.stderr)
        self.assertEqual(json.loads(pr.stdout)["removed"], ["r0"])
        self.assertEqual(self.releases(), ["r1", "r2", "r3"])

        # Once the job ends, normal pruning removes r1.
        self.settle(active)
        deadline = time.monotonic() + 10
        while True:
            pr = self.prune()
            self.assertEqual(pr.returncode, 0, pr.stderr)
            removed = json.loads(pr.stdout)["removed"]
            if removed or time.monotonic() > deadline:
                break
            time.sleep(0.2)
        self.assertEqual(removed, ["r1"])
        self.assertEqual(self.releases(), ["r2", "r3"])

    def test_a_process_below_the_job_dir_keeps_the_release(self) -> None:
        job_dir = self.job("review-nested", "r1")
        (job_dir / "review-source").mkdir()
        proc = self.run_job(job_dir / "review-source")
        pr = self.prune()
        self.assertEqual(pr.returncode, 0, pr.stderr)
        report = json.loads(pr.stdout)
        self.assertEqual(report["removed"], ["r0"])
        self.assertIn(proc.pid, report["in_use"]["r1"])

    def test_unreadable_baseline_of_a_running_job_removes_nothing(self) -> None:
        self.run_job(self.job("review-broken", text="{not json"))
        pr = self.prune()
        self.assertNotEqual(pr.returncode, 0, pr.stdout)
        self.assertIn("attribution baseline", pr.stderr)
        self.assertEqual(self.releases(), ["r0", "r1", "r2", "r3"])


if __name__ == "__main__":
    unittest.main()
