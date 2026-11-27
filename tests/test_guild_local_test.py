#!/usr/bin/env python3
"""Fail-closed behavior for exact-commit local-test evidence."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import sys

sys.path.insert(0, str(ROOT / "bin"))
import guild_local_test as lt  # noqa: E402


class LocalTestEvidenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="guild-local-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.state = self.tmp / "state"
        self.env_patch = {"GUILD_LOCAL_TEST_DIR": str(self.state)}
        self._old = {k: os.environ.get(k) for k in self.env_patch}
        os.environ.update(self.env_patch)
        # Tiny fixed suite for acme/toy — replaces real SUITES for this process.
        self._suites = dict(lt.SUITES)
        self._digests = dict(lt.WORKFLOW_DIGESTS)
        wf = ".github/workflows/tests.yml"
        self.toy_id = "toy-tests-yml-v1"
        body = b"name: toy\n"
        self.digest = __import__("hashlib").sha256(body).hexdigest()
        lt.WORKFLOW_DIGESTS[self.toy_id] = (wf, self.digest)
        lt.SUITES["toy"] = (self.toy_id, lambda _c: [["python3", "tests/test_ok.py"]])

        self.repo = self.tmp / "toy"
        self.repo.mkdir()
        (self.repo / ".github" / "workflows").mkdir(parents=True)
        (self.repo / ".github" / "workflows" / "tests.yml").write_bytes(body)
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_ok.py").write_text("print('ok')\n")
        self.git = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.tmp),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        }
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.repo, env=self.git, check=True)
        subprocess.run(["git", "add", "-A"], cwd=self.repo, env=self.git, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=self.repo, env=self.git, check=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/acme/toy.git"],
            cwd=self.repo, env=self.git, check=True,
        )
        self.sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, env=self.git,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.tree = subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"], cwd=self.repo, env=self.git,
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    def tearDown(self) -> None:
        lt.SUITES.clear()
        lt.SUITES.update(self._suites)
        lt.WORKFLOW_DIGESTS.clear()
        lt.WORKFLOW_DIGESTS.update(self._digests)
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_run_binds_sha_and_rejects_other_sha_same_tree(self) -> None:
        rec = lt.run(checkout=self.repo)
        self.assertEqual(rec["status"], "passed")
        self.assertEqual(rec["sha"], self.sha)
        self.assertIsNotNone(lt.accepts("acme/toy", sha=self.sha, tree=self.tree))
        # Same tree, different commit SHA must not authorize.
        other = "a" * 40
        self.assertIsNone(lt.accepts("acme/toy", sha=other, tree=self.tree))

    def test_true_and_omitted_commands_cannot_authorize(self) -> None:
        fake = {
            "schema": lt.SCHEMA,
            "authority": lt.AUTHORITY_LOCAL,
            "suite_id": self.toy_id,
            "workflow_digest": self.digest,
            "repo": "acme/toy",
            "sha": self.sha,
            "tree": self.tree,
            "pins": None,
            "status": "passed",
            "commands": [{"argv": ["true"], "exit_code": 0}],
            "recorded_at": "2026-01-01T00:00:00Z",
            "host": "test",
        }
        with self.assertRaises(ValueError):
            lt.write_record(fake)
        fake["commands"] = []  # omitted required
        with self.assertRaises(ValueError):
            lt.write_record({**fake, "commands": []})

    def test_fabricated_import_rejected_validated_transfer_ok(self) -> None:
        junk = json.dumps({
            "schema": lt.SCHEMA,
            "authority": lt.AUTHORITY_LOCAL,
            "suite_id": self.toy_id,
            "workflow_digest": self.digest,
            "repo": "acme/toy",
            "sha": self.sha,
            "tree": self.tree,
            "pins": None,
            "status": "passed",
            "commands": [{"argv": ["true"], "exit_code": 0}],
        })
        with self.assertRaises(ValueError):
            lt.import_record(junk)
        good = lt.run(checkout=self.repo)
        exported = lt.export_record("acme/toy", good["sha"])
        # Clear and re-import
        shutil.rmtree(self.state)
        path = lt.import_record(exported)
        self.assertTrue(path.is_file())
        self.assertIsNotNone(lt.accepts("acme/toy", sha=self.sha, tree=self.tree))

    def test_workflow_drift_fails_closed(self) -> None:
        (self.repo / ".github" / "workflows" / "tests.yml").write_bytes(b"name: drifted\n")
        subprocess.run(["git", "add", "-A"], cwd=self.repo, env=self.git, check=True)
        subprocess.run(["git", "commit", "-qm", "drift"], cwd=self.repo, env=self.git, check=True)
        with self.assertRaises(RuntimeError) as ctx:
            lt.run(checkout=self.repo)
        self.assertIn("workflow drift", str(ctx.exception))

    def test_mismatched_origin_rejected(self) -> None:
        subprocess.run(
            ["git", "remote", "set-url", "origin", "https://github.com/acme/other.git"],
            cwd=self.repo, env=self.git, check=True,
        )
        with self.assertRaises(ValueError):
            lt.run(checkout=self.repo)

    def test_unsupported_member_has_no_suite(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            lt.suite_for("acme/thresher")
        self.assertIn("no workflow-equivalent suite", str(ctx.exception))

    def test_failed_exit_does_not_pass(self) -> None:
        lt.SUITES["toy"] = (self.toy_id, lambda _c: [["python3", "-c", "raise SystemExit(1)"]])
        # Path check looks for tests/ scripts/ only; -c is fine.
        # But suite path existence loop skips non-tests paths. Need a failing test file.
        (self.repo / "tests" / "test_ok.py").write_text("raise SystemExit(1)\n")
        lt.SUITES["toy"] = (self.toy_id, lambda _c: [["python3", "tests/test_ok.py"]])
        subprocess.run(["git", "add", "-A"], cwd=self.repo, env=self.git, check=True)
        subprocess.run(["git", "commit", "-qm", "fail"], cwd=self.repo, env=self.git, check=True)
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, env=self.git,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        tree = subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"], cwd=self.repo, env=self.git,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        rec = lt.run(checkout=self.repo)
        self.assertEqual(rec["status"], "failed")
        self.assertIsNone(lt.accepts("acme/toy", sha=sha, tree=tree, require_passed=True))
        self.assertIsNotNone(lt.accepts("acme/toy", sha=sha, tree=tree, require_passed=False))


class GuildUpdateLocalTestGate(unittest.TestCase):
    """Changed-pins candidate must read pins from $sha, not the installed checkout."""

    def test_status_cli_requires_sha(self) -> None:
        cli = ROOT / "bin" / "guild-local-test"
        out = subprocess.run(
            [sys.executable, str(cli), "status", "--repo", "acme/guild", "--tree", "a" * 40],
            capture_output=True, text=True,
        )
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("--sha", out.stderr)


class GuildPinResumeBehavior(unittest.TestCase):
    """Existing pin PR can be resumed when evidence covers the candidate."""

    def test_resume_flag_in_help(self) -> None:
        text = (ROOT / "bin" / "guild-pin").read_text(encoding="utf-8")
        self.assertIn("--resume", text)
        self.assertIn("local-test on pin candidate", text)

    def test_guild_update_reads_pins_from_candidate_sha(self) -> None:
        """Installed src pins must not be used; git show $sha pins must match evidence."""
        text = (ROOT / "bin" / "guild-update").read_text(encoding="utf-8")
        self.assertIn('show "$sha:tests/member-pins.json"', text)
        self.assertNotIn("pins_file=$src/tests/member-pins.json", text)
        self.assertIn("cannot read Actions permissions", text)


if __name__ == "__main__":
    unittest.main()
