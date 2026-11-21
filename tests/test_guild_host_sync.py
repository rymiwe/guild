#!/usr/bin/env python3
"""guild-host-sync fast-forwards local Guild checkouts and restarts orderly when orderly advances."""
from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "bin" / "guild-host-sync"


class GuildHostSyncTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="guild-host-sync-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.bin_dir = self.tmp / "bin"
        self.bin_dir.mkdir()
        (self.bin_dir / "systemctl").write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$SYSTEMCTL_LOG\"\n",
            encoding="utf-8",
        )
        (self.bin_dir / "systemctl").chmod(0o755)
        (self.bin_dir / "job-notify").write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$NOTIFY_LOG\"\n",
            encoding="utf-8",
        )
        (self.bin_dir / "job-notify").chmod(0o755)
        self.env = {
            "PATH": f"{self.bin_dir}:/usr/bin:/bin",
            "HOME": str(self.tmp),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_AUTHOR_NAME": "a",
            "GIT_AUTHOR_EMAIL": "a@acme.example",
            "GIT_COMMITTER_NAME": "a",
            "GIT_COMMITTER_EMAIL": "a@acme.example",
            "GUILD_HOST_SYNC_STATE_DIR": str(self.tmp / "state"),
            "SYSTEMCTL_LOG": str(self.tmp / "systemctl.log"),
            "NOTIFY_LOG": str(self.tmp / "notify.log"),
        }
        self.guild_repo = self._make_repo("guild", "main")
        self.orderly_repo = self._make_repo("orderly", "main")
        self.env["GUILD_SYNC_REPOS"] = f"{self.guild_repo}:{self.orderly_repo}"

    def _git(self, *args: str, cwd: Path) -> str:
        out = subprocess.run(["git", *args], cwd=cwd, env=self.env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip()

    def _make_repo(self, name: str, branch: str) -> Path:
        bare = self.tmp / f"{name}.git"
        work = self.tmp / "src" / name
        local = self.tmp / "local" / name
        self._git("init", "-q", "--bare", "-b", branch, str(bare), cwd=self.tmp)
        work.parent.mkdir(parents=True, exist_ok=True)
        self._git("clone", "-q", str(bare), str(work), cwd=self.tmp)
        (work / "f.txt").write_text("v1\n", encoding="utf-8")
        self._git("add", "f.txt", cwd=work)
        self._git("commit", "-qm", "init", cwd=work)
        self._git("push", "-q", "origin", branch, cwd=work)
        local.parent.mkdir(parents=True, exist_ok=True)
        self._git("clone", "-q", str(bare), str(local), cwd=self.tmp)
        return local

    def _push_commit(self, name: str, branch: str, text: str) -> str:
        work = self.tmp / "src" / name
        (work / "f.txt").write_text(text, encoding="utf-8")
        self._git("commit", "-qam", f"update {text.strip()}", cwd=work)
        self._git("push", "-q", "origin", branch, cwd=work)
        return self._git("rev-parse", "HEAD", cwd=work)

    def run_sync(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(SCRIPT)], env=self.env, capture_output=True, text=True)

    def systemctl_calls(self) -> list[str]:
        p = self.tmp / "systemctl.log"
        return p.read_text().splitlines() if p.exists() else []

    def notifications(self) -> list[str]:
        p = self.tmp / "notify.log"
        return p.read_text().splitlines() if p.exists() else []

    def test_syncs_repos_and_restarts_orderly_only_when_orderly_advances(self) -> None:
        new_guild = self._push_commit("guild", "main", "v2\n")
        (self.orderly_repo / "untracked.tmp").write_text("keep me\n", encoding="utf-8")

        res1 = self.run_sync()
        self.assertEqual(res1.returncode, 0, res1.stderr)
        self.assertEqual(self._git("rev-parse", "HEAD", cwd=self.guild_repo), new_guild)
        self.assertEqual(self.systemctl_calls(), [], "orderly did not advance yet")

        new_orderly = self._push_commit("orderly", "main", "v2\n")
        res2 = self.run_sync()
        self.assertEqual(res2.returncode, 0, res2.stderr)
        self.assertEqual(self._git("rev-parse", "HEAD", cwd=self.orderly_repo), new_orderly)
        self.assertEqual(self.systemctl_calls(), ["--user try-restart orderly.service"])

        res3 = self.run_sync()
        self.assertEqual(res3.returncode, 0, res3.stderr)
        self.assertEqual(len(self.systemctl_calls()), 1, "no restart when orderly is already current")
        self.assertEqual(self.notifications(), [])

    def test_dirty_tracked_file_pings_once_and_clears_on_recovery(self) -> None:
        (self.guild_repo / "f.txt").write_text("dirty\n", encoding="utf-8")
        self.assertEqual(self.run_sync().returncode, 1)
        self.assertEqual(self.run_sync().returncode, 1)
        self.assertEqual(len(self.notifications()), 1)
        self.assertIn("guild-host-sync FAILED", self.notifications()[0])

        self._git("checkout", "--", "f.txt", cwd=self.guild_repo)
        self.assertEqual(self.run_sync().returncode, 0)
        self.assertEqual(list((self.tmp / "state" / "pinged").iterdir()), [])

    def test_skips_when_locked(self) -> None:
        sdir = self.tmp / "state"
        sdir.mkdir(parents=True, exist_ok=True)
        with open(sdir / "lock", "w", encoding="utf-8") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            res = self.run_sync()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("another run is in progress", res.stderr)


if __name__ == "__main__":
    unittest.main()
