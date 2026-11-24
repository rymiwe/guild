#!/usr/bin/env python3
"""guild-release prune keeps a release that a running job's attribution baseline names.

A job's attribution baseline records every repo the job's git commands touched,
Guild release checkouts included. The job's end-of-job check fails closed when a
recorded repo is missing, so prune must keep that release until the job ends,
even when no process of the job has the release on PATH or in its environment.
"""
from __future__ import annotations

import hashlib
import hmac
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
        self.key = self.tmp / "pass.key"
        self.key.write_bytes(b"k" * 32)
        os.environ["GUILD_ATTRIBUTION_PASS_KEY"] = str(self.key)

    def sign(self, job_dir: Path, result: str, reason: str | None = None) -> None:
        baseline = next(job_dir.glob("*attribution-baseline.json"))
        body = {
            "v": 1,
            "job": str(job_dir.resolve()),
            "baseline_sha256": hashlib.sha256(baseline.read_bytes()).hexdigest(),
            "evidence_sha256": "ab" * 32,
            "result": result,
        }
        if reason is not None:
            body["reason"] = reason
        message = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        body["mac"] = hmac.new(self.key.read_bytes(), message, hashlib.sha256).hexdigest()
        (job_dir / ".acme.attribution-pass.json").write_text(json.dumps(body), encoding="utf-8")

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
            [sys.executable, str(GUILD_RELEASE_BIN), "prune", "--root", str(self.root),
             "--jobs-root", str(self.jobs), "--json", *extra],
            text=True, capture_output=True, timeout=60,
        )

    def releases(self) -> list[str]:
        return sorted(p.name for p in (self.root / "releases").iterdir())

    def test_running_job_baseline_keeps_only_its_release_until_the_job_ends(self) -> None:
        active_job = self.job("review-active", "r1")
        active = self.run_job(active_job)
        settled = self.job("review-settled", "r0")  # no process: this job has ended
        self.sign(settled, "pass")

        dry = self.prune("--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        report = json.loads(dry.stdout)
        self.assertEqual(report["removed"], ["r0"])
        self.assertIn(active.pid, report["in_use"]["r1"])

        pr = self.prune()
        self.assertEqual(pr.returncode, 0, pr.stderr)
        self.assertEqual(json.loads(pr.stdout)["removed"], ["r0"])
        self.assertEqual(self.releases(), ["r1", "r2", "r3"])

        # The guard writes a pass at job exit. Prune then removes r1.
        self.settle(active)
        self.sign(active_job, "pass")
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

    def test_settled_job_without_a_pass_keeps_its_release(self) -> None:
        self.job("review-settled", "r1")
        pr = self.prune()
        self.assertEqual(pr.returncode, 0, pr.stderr)
        report = json.loads(pr.stdout)
        self.assertEqual(report["removed"], ["r0"])
        self.assertEqual(report["held_for_attribution"], ["r1"])
        self.assertEqual(self.releases(), ["r1", "r2", "r3"])

    def test_verified_failure_lets_prune_resume(self) -> None:
        job = self.job("review-failed", "r1")
        self.sign(job, "fail", reason="1 hits")
        pr = self.prune()
        self.assertEqual(pr.returncode, 0, pr.stderr)
        self.assertEqual(json.loads(pr.stdout)["removed"], ["r0", "r1"])
        self.assertEqual(self.releases(), ["r2", "r3"])

    def test_bogus_pass_keeps_the_release(self) -> None:
        job = self.job("review-bogus", "r1")
        self.sign(job, "pass")
        path = job / ".acme.attribution-pass.json"
        data = json.loads(path.read_text())
        data["mac"] = "0" * 64
        path.write_text(json.dumps(data), encoding="utf-8")
        pr = self.prune()
        self.assertEqual(pr.returncode, 0, pr.stderr)
        report = json.loads(pr.stdout)
        self.assertEqual(report["held_for_attribution"], ["r1"])
        self.assertNotIn("r1", report["removed"])

    def test_pass_for_a_changed_baseline_keeps_the_release(self) -> None:
        job = self.job("review-changed", "r1")
        self.sign(job, "pass")
        baseline = job / ".acme.attribution-baseline.json"
        data = json.loads(baseline.read_text())
        data["started"] = 2
        baseline.write_text(json.dumps(data), encoding="utf-8")
        pr = self.prune()
        self.assertEqual(pr.returncode, 0, pr.stderr)
        self.assertEqual(json.loads(pr.stdout)["held_for_attribution"], ["r1"])

    def test_unreadable_baseline_of_a_running_job_removes_nothing(self) -> None:
        self.run_job(self.job("review-broken", text="{not json"))
        pr = self.prune()
        self.assertNotEqual(pr.returncode, 0, pr.stdout)
        self.assertIn("attribution baseline", pr.stderr)
        self.assertEqual(self.releases(), ["r0", "r1", "r2", "r3"])


if __name__ == "__main__":
    unittest.main()
