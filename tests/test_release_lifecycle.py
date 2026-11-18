#!/usr/bin/env python3
"""Integration tests for Steps 2 and 3: prepare, check, atomic switch, rollback, and per-job release binding."""
from __future__ import annotations

import json
import os
import pwd
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
GUILD_RELEASE_BIN = ROOT / "bin" / "guild-release"
PINS_FILE = ROOT / "tests" / "member-pins.json"
REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()

GUARDED_HOST_PATHS = (
    REAL_HOME / ".local" / "bin",
    REAL_HOME / ".claude" / "hooks",
    REAL_HOME / ".claude" / "skills",
    REAL_HOME / ".config" / "systemd" / "user",
    REAL_HOME / ".config" / "work-account",
)

# Reuse synthetic gh + commit-lint definitions from the Step 1 contract test.
sys.path.insert(0, str(ROOT / "tests"))
import test_pr_gate_contract as step1  # noqa: E402


def snapshot_guarded() -> dict[str, dict[str, tuple[int, int, int, int, int]]]:
    return {str(p): step1.snapshot_tree(p) for p in GUARDED_HOST_PATHS}


def find_system_git() -> str:
    return step1.find_system_git()


def build_clean_path(*extra_dirs: Path) -> str:
    return step1.build_clean_path(*extra_dirs)


class ReleaseLifecycleTest(unittest.TestCase):
    host_before: dict[str, dict[str, tuple[int, int, int, int, int]]]
    tmp_ctx: tempfile.TemporaryDirectory[str]
    tmp: Path
    sources_dir: Path
    pstack_dir: Path
    pins_r1: dict[str, str]
    pins_r1_file: Path
    user_home: Path
    work_root: Path
    state_dir: Path
    work_account_cfg: Path
    stub_bin: Path
    evidence_file: Path
    ci_first_file: Path
    scenario_clear: Path
    system_git: str
    git_env: dict[str, str]

    @classmethod
    def setUpClass(cls) -> None:
        cls.host_before = snapshot_guarded()
        cls.tmp_ctx = tempfile.TemporaryDirectory(prefix="guild-step2-")
        cls.tmp = Path(cls.tmp_ctx.name).resolve()
        cls.system_git = find_system_git()

        cls.user_home = cls.tmp / "external-user-home"
        cls.user_home.mkdir(parents=True)
        cls.git_env = {
            "PATH": build_clean_path(),
            "HOME": str(cls.user_home),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_AUTHOR_NAME": "Acme Author",
            "GIT_AUTHOR_EMAIL": "author@acme.example",
            "GIT_COMMITTER_NAME": "Acme Author",
            "GIT_COMMITTER_EMAIL": "author@acme.example",
        }

        cls.pins_r1 = json.loads(PINS_FILE.read_text(encoding="utf-8"))
        cls.pins_r1_file = cls.tmp / "pins-r1.json"
        cls.pins_r1_file.write_text(json.dumps(cls.pins_r1, indent=2) + "\n", encoding="utf-8")

        # Materialize isolated mutable clones of the pinned member repos under tmp/sources
        # so we can commit R2 and broken-candidate branches without touching ~/code/*.
        raw_sources = (
            Path(os.environ["GUILD_MEMBER_SOURCES_DIR"]).resolve()
            if os.environ.get("GUILD_MEMBER_SOURCES_DIR")
            else REAL_HOME / "code"
        )
        cls.sources_dir = cls.tmp / "sources"
        cls.sources_dir.mkdir(parents=True)
        origins_dir = cls.tmp / "source-origins"
        origins_dir.mkdir(parents=True)
        for name, expected_sha in cls.pins_r1.items():
            src = raw_sources / name
            dest = cls.sources_dir / name
            is_shallow = cls._git("-C", str(src), "rev-parse", "--is-shallow-repository").stdout.strip()
            if is_shallow == "true":
                shutil.copytree(src, dest, symlinks=True)
                cls._git("-C", str(dest), "reset", "--hard", "-q", expected_sha)
                cls._git("-C", str(dest), "clean", "-fdq")
                cls._git("-C", str(dest), "checkout", "-q", "-B", "main", expected_sha)
                cls._git("-C", str(dest), "update-ref", "refs/remotes/origin/main", expected_sha)
                cls._git(
                    "-C", str(dest), "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"
                )
            else:
                bare = origins_dir / f"{name}.git"
                cls._git("init", "-q", "--bare", "-b", "main", str(bare))
                cls._git("-C", str(src), "push", "-q", str(bare), f"{expected_sha}:refs/heads/main")
                cls._git("clone", "-q", str(bare), str(dest))
                cls._git(
                    "-C", str(dest), "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"
                )

        # Offline pstack + cursor-team-kit placeholder.
        cls.pstack_dir = cls.tmp / "cursor-plugins" / "pstack"
        (cls.pstack_dir / "skills" / "poteto-mode").mkdir(parents=True)
        (cls.pstack_dir / "skills" / "poteto-mode" / "SKILL.md").write_text(
            "---\nname: poteto-mode\n---\n", encoding="utf-8"
        )
        kit_dir = cls.pstack_dir.parent / "cursor-team-kit"
        (kit_dir / "skills" / "deslop").mkdir(parents=True)
        (kit_dir / "skills" / "deslop" / "SKILL.md").write_text(
            "---\nname: deslop\n---\n", encoding="utf-8"
        )

        # External machine config + live state (strictly outside any release root).
        cls.work_root = cls.tmp / "work"
        cls.state_dir = cls.work_root / "state"
        (cls.state_dir / "autonomy-gates.d").mkdir(parents=True)
        (cls.work_root / "gnupg").mkdir(parents=True)
        (cls.state_dir / "pr-grant.md").write_text("Status: GRANTED\n", encoding="utf-8")

        guild_cfg = {
            "github_owner": "AcmeCorp",
            "default_repo": "AcmeCorp/widgets",
            "author_logins": ["acme-author"],
            "author_emails": ["author@acme.example"],
            "author_personal_emails": ["author@home.example"],
            "persona_names": ["Robo", "Helper"],
            "bopeep": {
                "state_dir": str(cls.state_dir),
                "gate_prefix": "acme-",
                "commands": {
                    "commit_lint": "acme-commit-lint",
                    "notify": "acme-notify",
                },
                "signatures": {
                    "signing_fpr": "0123456789ABCDEF0123456789ABCDEF01234567",
                    "gnupg_home": str(cls.work_root / "gnupg"),
                },
                "pr_gate": {
                    "ci_review_checks": ["danger/widgets", "review / Bot Review"],
                },
                "ready_review_gate": {
                    "review_skill": "acme-tools:review-work",
                },
                # bopeep-watch runs this every cycle; the Step 3 test uses it to start jobs.
                "watch": {
                    "harvest": {"command": str(cls.tmp / "job-start"), "every_s": 1},
                },
            },
        }
        (cls.work_root / ".guild.json").write_text(json.dumps(guild_cfg, indent=2) + "\n", encoding="utf-8")

        cls.work_account_cfg = cls.user_home / ".config" / "work-account" / "config.json"
        cls.work_account_cfg.parent.mkdir(parents=True)
        cls.work_account_cfg.write_text(
            json.dumps({
                "work_path_prefixes": [str(cls.work_root)],
                "work_github_owners": ["AcmeCorp"],
            }) + "\n",
            encoding="utf-8",
        )

        widgets_origin = origins_dir / "widgets.git"
        cls._git("init", "-q", "--bare", "-b", "main", str(widgets_origin))
        repo_dir = cls.work_root / "widgets"
        cls._git("clone", "-q", str(widgets_origin), str(repo_dir))
        (repo_dir / "README.md").write_text("# widgets\n", encoding="utf-8")
        cls._git("add", "README.md", cwd=repo_dir)
        cls._git("commit", "-qm", "Initial widgets commit", cwd=repo_dir)
        cls._git("push", "-q", "origin", "HEAD:main", cwd=repo_dir)

        cls._git("checkout", "-qb", "feat/contract", cwd=repo_dir)
        (repo_dir / "src").mkdir()
        (repo_dir / "src" / "widget.py").write_text("x = 1\n", encoding="utf-8")
        cls._git("add", "src/widget.py", cwd=repo_dir)
        cls._git(
            "commit",
            "-q",
            "-m",
            "Add widget gate contract\n\nFixed: test widget gate contract",
            cwd=repo_dir,
        )
        cls._git("push", "-q", "origin", "HEAD:feat/contract", cwd=repo_dir)
        head_sha = cls._git("-C", str(repo_dir), "rev-parse", "HEAD").stdout.strip()

        cls.stub_bin = cls.tmp / "stub-bin"
        cls.stub_bin.mkdir(parents=True)
        gh_bin = cls.stub_bin / "gh"
        gh_bin.write_text(step1.STUB_GH, encoding="utf-8")
        gh_bin.chmod(0o755)
        commit_lint = cls.stub_bin / "acme-commit-lint"
        commit_lint.write_text(step1.STUB_COMMIT_LINT, encoding="utf-8")
        commit_lint.chmod(0o755)
        notify = cls.stub_bin / "acme-notify"
        notify.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        notify.chmod(0o755)

        cls.evidence_file = cls.tmp / "evidence.md"
        cls.evidence_file.write_text("## Blast-radius\n\nScoped to widgets only.\n", encoding="utf-8")

        cls.ci_first_file = cls.tmp / "ci-first.json"
        cls.ci_first_file.write_text(
            json.dumps({
                "threads": [
                    {"is_ci": True, "head": head_sha, "resolved": True, "replies": []},
                ],
                "commit_shas": [head_sha],
            }) + "\n",
            encoding="utf-8",
        )

        cls.scenario_clear = cls.tmp / "scenario-clear.json"
        cls.scenario_clear.write_text(
            json.dumps({
                "repo": "AcmeCorp/widgets",
                "pr": 7,
                "pr_view": {
                    "number": 7,
                    "title": "Add widget gate contract",
                    "body": (
                        "## Why\n\nExercise the installed pr-gate contract.\n\n"
                        "## What changes\n\nAdds widget contract logic.\n\n"
                        "## What to check\n\nGate helper wiring.\n"
                    ),
                    "state": "OPEN",
                    "isDraft": False,
                    "headRefName": "feat/contract",
                    "headRefOid": head_sha,
                    "baseRefName": "main",
                    "labels": [],
                    "author": {"login": "acme-author"},
                    "commits": [
                        {
                            "messageHeadline": "Add widget gate contract",
                            "messageBody": "Fixed: test widget gate contract",
                        }
                    ],
                    "reviewDecision": "APPROVED",
                    "mergeable": "MERGEABLE",
                    "url": "https://github.com/AcmeCorp/widgets/pull/7",
                },
                "pr_checks": [
                    {"name": "danger/widgets", "state": "SUCCESS", "bucket": "pass"},
                    {"name": "ci", "state": "SUCCESS", "bucket": "pass"},
                ],
                "files": [
                    {"filename": "src/widget.py", "patch": "@@ -0,0 +1 @@\n+x = 1\n"},
                ],
                "comments": [
                    {
                        "id": 101,
                        "updated_at": "2026-11-17T00:00:00Z",
                        "user": {"login": "github-actions[bot]", "type": "Bot"},
                        "body": (
                            "<!-- risk-review-marker -->\n"
                            f"<sub>Reviewed commit `{head_sha}`. "
                            "Add the `risk:reclassify` label to run it again.</sub>"
                        ),
                    }
                ],
                "workflow_runs": {"workflow_runs": []},
                "commits": [
                    {
                        "sha": head_sha,
                        "commit": {
                            "message": "Add widget gate contract\n\nFixed: test widget gate contract",
                            "author": {
                                "name": "Acme Author",
                                "email": "author@acme.example",
                                "date": "2026-11-17T00:00:00Z",
                            },
                            "committer": {
                                "name": "Acme Author",
                                "email": "author@acme.example",
                                "date": "2026-11-17T00:00:00Z",
                            },
                            "verification": {"verified": True, "reason": "valid"},
                        },
                        "author": {"login": "acme-author"},
                    }
                ],
                "review_threads": [],
            }),
            encoding="utf-8",
        )

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            host_after = snapshot_guarded()
            if host_after != cls.host_before:
                raise AssertionError("Host installation paths changed during release lifecycle test")
        finally:
            cls.tmp_ctx.cleanup()

    @classmethod
    def _git(cls, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [cls.system_git, *args],
            cwd=str(cwd) if cwd else None,
            env=cls.git_env,
            text=True,
            capture_output=True,
            check=True,
        )

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {
            **self.git_env,
            "PATH": build_clean_path(),
            "GUILD_MEMBER_SOURCES_DIR": str(self.sources_dir),
            "PSTACK_HOME": str(self.pstack_dir),
        }
        return subprocess.run(
            [sys.executable, str(GUILD_RELEASE_BIN), *args],
            env=env,
            text=True,
            capture_output=True,
            timeout=120,
        )

    def run_release_gate(self, bin_dir: Path) -> subprocess.CompletedProcess[str]:
        # Intentionally do NOT pass CONCLAVE_HOME or WORK_ACCOUNT_CONFIG from release dir:
        # HOME is external user_home and bin_dir's guild-exec must bind release dependencies.
        env = {
            **self.git_env,
            "HOME": str(self.user_home),
            "BOPEEP_CI_FIRST_FIXTURE": str(self.ci_first_file),
            "BOPEEP_OWN_PR_AUTHORS": "acme-author",
            "BOPEEP_GH": str(self.stub_bin / "gh"),
            "CONCLAVE_GH": str(self.stub_bin / "gh"),
            "GH_TOKEN": "",
            "GITHUB_TOKEN": "",
            "SYNTHETIC_GH_SCENARIO": str(self.scenario_clear),
            "PATH": build_clean_path(bin_dir, self.stub_bin),
        }
        return subprocess.run(
            [
                str(bin_dir / "pr-gate"),
                "--pr",
                "7",
                "--repo",
                "AcmeCorp/widgets",
                "--evidence",
                str(self.evidence_file),
            ],
            env=env,
            text=True,
            capture_output=True,
            timeout=60,
        )

    def test_deploy_and_external_state_separation(self) -> None:
        root = self.tmp / "root-deploy"
        cp = self.run_cli("deploy", "--root", str(root), "--pins", str(self.pins_r1_file), "--release-id", "r1")
        self.assertEqual(cp.returncode, 0, f"stdout:\n{cp.stdout}\nstderr:\n{cp.stderr}")

        current = root / "current"
        self.assertTrue(current.is_symlink())
        self.assertEqual(os.readlink(current), "releases/r1")
        self.assertFalse((root / "previous").exists() or (root / "previous").is_symlink())

        # Machine config & live state stay outside the release directory.
        self.assertFalse((root / "releases" / "r1" / ".config" / "work-account").exists())
        self.assertTrue(self.work_account_cfg.is_file())
        self.assertTrue((self.state_dir / "pr-grant.md").is_file())

        gate = self.run_release_gate(current / "bin")
        self.assertEqual(gate.returncode, 0, f"stdout:\n{gate.stdout}\nstderr:\n{gate.stderr}")
        self.assertIn("pr-gate PASS", gate.stdout)

        # Rollback with no previous release fails closed and leaves r1 active.
        rb = self.run_cli("rollback", "--root", str(root))
        self.assertNotEqual(rb.returncode, 0)
        self.assertEqual(os.readlink(current), "releases/r1")

    def test_interrupted_preparation_leaves_old_release_active(self) -> None:
        root = self.tmp / "root-interrupted"
        cp1 = self.run_cli("deploy", "--root", str(root), "--pins", str(self.pins_r1_file), "--release-id", "r1")
        self.assertEqual(cp1.returncode, 0, cp1.stderr)

        # Load bin/guild-release as a module to simulate an interruption mid-preparation.
        import importlib.machinery
        import importlib.util

        loader = importlib.machinery.SourceFileLoader("guild_release_mod", str(GUILD_RELEASE_BIN))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        assert spec is not None
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)

        with mock.patch.object(mod, "run_member_installers", side_effect=KeyboardInterrupt("simulated ctrl-c")):
            with self.assertRaises(KeyboardInterrupt):
                mod.prepare_release(
                    root=root,
                    pins_path=self.pins_r1_file,
                    sources_dir=self.sources_dir,
                    pstack_home=self.pstack_dir,
                    release_id="r2-interrupted",
                    check_cmd=None,
                )

        self.assertEqual(os.readlink(root / "current"), "releases/r1")
        self.assertFalse((root / "releases" / "r2-interrupted").exists())
        self.assertEqual(list((root / ".staging").iterdir()), [])
        gate = self.run_release_gate(root / "current" / "bin")
        self.assertEqual(gate.returncode, 0, f"stdout:\n{gate.stdout}\nstderr:\n{gate.stderr}")
        self.assertIn("pr-gate PASS", gate.stdout)

    def test_failed_candidate_check_leaves_old_release_active(self) -> None:
        root = self.tmp / "root-failed-check"
        cp1 = self.run_cli("deploy", "--root", str(root), "--pins", str(self.pins_r1_file), "--release-id", "r1")
        self.assertEqual(cp1.returncode, 0, cp1.stderr)

        # Case 1: Broken conclave risk_freshness dependency in a candidate commit.
        conclave_repo = self.sources_dir / "conclave"
        self._git("-C", str(conclave_repo), "checkout", "-qb", "bad-candidate")
        rf_path = conclave_repo / "scripts" / "risk_freshness.py"
        rf_path.write_text("this is invalid python syntax ((((\n", encoding="utf-8")
        self._git("-C", str(conclave_repo), "commit", "-am", "Break risk_freshness")
        bad_conclave_sha = self._git("-C", str(conclave_repo), "rev-parse", "HEAD").stdout.strip()
        self._git("-C", str(conclave_repo), "checkout", "-q", "main")

        bad_pins_file = self.tmp / "pins-bad-conclave.json"
        bad_pins = {**self.pins_r1, "conclave": bad_conclave_sha}
        bad_pins_file.write_text(json.dumps(bad_pins, indent=2) + "\n", encoding="utf-8")

        cp_bad = self.run_cli(
            "deploy", "--root", str(root), "--pins", str(bad_pins_file), "--release-id", "r2-bad"
        )
        self.assertNotEqual(cp_bad.returncode, 0)
        self.assertIn("candidate clear pr-gate check failed", cp_bad.stderr)

        # Case 2: Failing custom check hook on otherwise valid pins.
        cp_hook_fail = self.run_cli(
            "deploy",
            "--root",
            str(root),
            "--pins",
            str(self.pins_r1_file),
            "--release-id",
            "r2-hook-fail",
            "--check-cmd",
            "exit 17",
        )
        self.assertNotEqual(cp_hook_fail.returncode, 0)
        self.assertIn("custom check command failed", cp_hook_fail.stderr)

        # Old release remains active, retained cleanly, and callable.
        self.assertEqual(os.readlink(root / "current"), "releases/r1")
        self.assertFalse((root / "previous").exists() or (root / "previous").is_symlink())
        self.assertEqual(sorted(p.name for p in (root / "releases").iterdir()), ["r1"])
        self.assertEqual(list((root / ".staging").iterdir()), [])
        gate = self.run_release_gate(root / "current" / "bin")
        self.assertEqual(gate.returncode, 0, f"stdout:\n{gate.stdout}\nstderr:\n{gate.stderr}")
        self.assertIn("pr-gate PASS", gate.stdout)

    _pins_r2_file: Path | None = None

    @classmethod
    def pins_r2(cls) -> Path:
        """Pins for r2: bopeep adds bopeep-v2-only and marks pr-lint (v2); conclave marks risk_freshness."""
        if cls._pins_r2_file is not None:
            return cls._pins_r2_file
        # Build real v2 commits in isolated sources_dir for bopeep, conclave, and bridgekeeper.
        bopeep_repo = cls.sources_dir / "bopeep"
        cls._git("-C", str(bopeep_repo), "checkout", "-qb", "v2-release")
        v2_cmd = bopeep_repo / "bin" / "bopeep-v2-only"
        v2_cmd.write_text("#!/bin/sh\necho 'bopeep-v2-only ok'\n", encoding="utf-8")
        v2_cmd.chmod(0o755)
        bopeep_install = bopeep_repo / "install.sh"
        bopeep_install_text = bopeep_install.read_text(encoding="utf-8")
        bopeep_install.write_text(
            bopeep_install_text.replace(
                '".local/bin/feedback-start:bin/feedback-start"',
                '".local/bin/feedback-start:bin/feedback-start"\n  ".local/bin/bopeep-v2-only:bin/bopeep-v2-only"',
            ),
            encoding="utf-8",
        )
        pr_lint_path = bopeep_repo / "bin" / "pr-lint"
        pr_lint_text = pr_lint_path.read_text(encoding="utf-8")
        assert 'print("pr-lint PASS")' in pr_lint_text
        pr_lint_path.write_text(
            pr_lint_text.replace('print("pr-lint PASS")', 'print("pr-lint PASS (v2)")'),
            encoding="utf-8",
        )
        cls._git("-C", str(bopeep_repo), "add", "bin/bopeep-v2-only", "install.sh", "bin/pr-lint")
        cls._git("-C", str(bopeep_repo), "commit", "-qm", "bopeep v2 command and lint marker")
        bopeep_v2_sha = cls._git("-C", str(bopeep_repo), "rev-parse", "HEAD").stdout.strip()
        cls._git("-C", str(bopeep_repo), "checkout", "-q", "main")

        conclave_repo = cls.sources_dir / "conclave"
        cls._git("-C", str(conclave_repo), "checkout", "-qb", "v2-release")
        rf_path = conclave_repo / "scripts" / "risk_freshness.py"
        rf_text = rf_path.read_text(encoding="utf-8")
        # Append a v2 marker to evaluate()'s return reasons cleanly without breaking semantics.
        rf_path.write_text(
            rf_text + '\n_orig_evaluate = evaluate\ndef evaluate(*args, **kwargs):\n'
            '    res = _orig_evaluate(*args, **kwargs)\n'
            '    res.reason = f"{res.reason} [conclave-v2]"\n'
            '    return res\n',
            encoding="utf-8",
        )
        cls._git("-C", str(conclave_repo), "commit", "-am", "conclave v2 risk_freshness marker")
        conclave_v2_sha = cls._git("-C", str(conclave_repo), "rev-parse", "HEAD").stdout.strip()
        cls._git("-C", str(conclave_repo), "checkout", "-q", "main")

        pins_r2 = {
            **cls.pins_r1,
            "bopeep": bopeep_v2_sha,
            "conclave": conclave_v2_sha,
        }
        pins_r2_file = cls.tmp / "pins-r2.json"
        pins_r2_file.write_text(json.dumps(pins_r2, indent=2) + "\n", encoding="utf-8")

        cls._pins_r2_file = pins_r2_file
        return pins_r2_file

    def test_switch_and_rollback_restore_commands_and_dependencies(self) -> None:
        pins_r2_file = self.pins_r2()
        root = self.tmp / "root-switch-rollback"

        # 1. Deploy R1.
        cp1 = self.run_cli("deploy", "--root", str(root), "--pins", str(self.pins_r1_file), "--release-id", "r1")
        self.assertEqual(cp1.returncode, 0, f"stdout:\n{cp1.stdout}\nstderr:\n{cp1.stderr}")
        self.assertEqual(os.readlink(root / "current"), "releases/r1")
        self.assertFalse((root / "current" / "bin" / "bopeep-v2-only").exists())
        gate_r1 = self.run_release_gate(root / "current" / "bin")
        self.assertEqual(gate_r1.returncode, 0, f"stdout:\n{gate_r1.stdout}\nstderr:\n{gate_r1.stderr}")
        self.assertNotIn("[conclave-v2]", gate_r1.stdout)

        # 2. Deploy R2 beside R1 -> atomic switch of current, R1 retained as previous.
        cp2 = self.run_cli("deploy", "--root", str(root), "--pins", str(pins_r2_file), "--release-id", "r2")
        self.assertEqual(cp2.returncode, 0, f"stdout:\n{cp2.stdout}\nstderr:\n{cp2.stderr}")
        self.assertEqual(os.readlink(root / "current"), "releases/r2")
        self.assertEqual(os.readlink(root / "previous"), "releases/r1")
        self.assertTrue((root / "releases" / "r1").is_dir())
        self.assertTrue((root / "releases" / "r2").is_dir())

        # R2 exposes the new command and the updated pr-lint + conclave dependency.
        v2_exec = subprocess.run(
            [str(root / "current" / "bin" / "bopeep-v2-only")],
            env={"PATH": build_clean_path()},
            text=True,
            capture_output=True,
        )
        self.assertEqual(v2_exec.returncode, 0, v2_exec.stderr)
        self.assertEqual(v2_exec.stdout.strip(), "bopeep-v2-only ok")

        gate_r2 = self.run_release_gate(root / "current" / "bin")
        self.assertEqual(gate_r2.returncode, 0, f"stdout:\n{gate_r2.stdout}\nstderr:\n{gate_r2.stderr}")
        self.assertIn("[conclave-v2]", gate_r2.stdout)

        # Verify R2 pr-lint directly emits its v2 line while R1's retained pr-lint still emits v1.
        lint_body = self.tmp / "lint-body.md"
        lint_body.write_text(
            "## Why\n\nCheck direct pr-lint behavior.\n\n"
            "## What changes\n\nNothing.\n\n"
            "## What to check\n\nOutput marker.\n",
            encoding="utf-8",
        )
        lint_args = [
            "--body",
            str(lint_body),
            "--branch",
            "feat/contract",
            "--base",
            "main",
            "--create-cmd",
            "gh pr create --label no-changelog-needed",
        ]
        lint_env = {**self.git_env, "HOME": str(self.user_home), "PATH": build_clean_path(root / "current" / "bin")}
        r2_lint_out = subprocess.run(
            [str(root / "current" / "bin" / "pr-lint"), *lint_args],
            cwd=str(self.work_root / "widgets"),
            env=lint_env,
            text=True,
            capture_output=True,
        )
        self.assertEqual(r2_lint_out.returncode, 0, r2_lint_out.stderr)
        self.assertEqual(r2_lint_out.stdout.strip(), "pr-lint PASS (v2)")

        # Run retained R1 directly to confirm R1 stayed intact beside R2.
        gate_r1_retained = self.run_release_gate(root / "releases" / "r1" / "bin")
        self.assertEqual(gate_r1_retained.returncode, 0)
        self.assertNotIn("[conclave-v2]", gate_r1_retained.stdout)

        # 3. Roll back to R1 -> restores R1 commands and dependencies, drops R2-only command.
        rb = self.run_cli("rollback", "--root", str(root))
        self.assertEqual(rb.returncode, 0, f"stdout:\n{rb.stdout}\nstderr:\n{rb.stderr}")
        self.assertEqual(os.readlink(root / "current"), "releases/r1")
        self.assertEqual(os.readlink(root / "previous"), "releases/r2")
        self.assertFalse((root / "current" / "bin" / "bopeep-v2-only").exists())

        gate_after_rb = self.run_release_gate(root / "current" / "bin")
        self.assertEqual(gate_after_rb.returncode, 0, f"stdout:\n{gate_after_rb.stdout}\nstderr:\n{gate_after_rb.stderr}")
        self.assertNotIn("[conclave-v2]", gate_after_rb.stdout)
        self.assertIn("PASS: lint: pr-lint PASS", gate_after_rb.stdout)

        r1_lint_after_rb = subprocess.run(
            [str(root / "current" / "bin" / "pr-lint"), *lint_args],
            cwd=str(self.work_root / "widgets"),
            env=lint_env,
            text=True,
            capture_output=True,
        )
        self.assertEqual(r1_lint_after_rb.returncode, 0, r1_lint_after_rb.stderr)
        self.assertEqual(r1_lint_after_rb.stdout.strip(), "pr-lint PASS")

        # Check status reports both retained releases accurately.
        st = self.run_cli("status", "--root", str(root), "--json")
        self.assertEqual(st.returncode, 0, st.stderr)
        st_data = json.loads(st.stdout)
        self.assertEqual(st_data["current"], "r1")
        self.assertEqual(st_data["previous"], "r2")
        self.assertEqual(st_data["releases"], ["r1", "r2"])

        # A pruned active release leaves `current` dangling. switch must still
        # repair it, and status must still report.
        shutil.rmtree(root / "releases" / "r1")
        st_dangling = self.run_cli("status", "--root", str(root), "--json")
        self.assertEqual(st_dangling.returncode, 0, st_dangling.stderr)
        self.assertIsNone(json.loads(st_dangling.stdout)["current"])
        fix = self.run_cli("switch", "--root", str(root), "--release-id", "r2")
        self.assertEqual(fix.returncode, 0, f"stdout:\n{fix.stdout}\nstderr:\n{fix.stderr}")
        self.assertEqual(os.readlink(root / "current"), "releases/r2")

    def test_running_jobs_keep_their_release_across_switch_and_watcher_restart(self) -> None:
        """Step 3: a job stays on the release it started on, through a switch and a watcher restart.

        Mirrors the live path: bopeep-watch, started through `current`, runs its
        harvest command, which starts the job in a detached tmux session the way
        job-launch does. The tmux server is started with r1 first on PATH, so job
        B getting r2 shows each session takes PATH from its client, not the server.
        The server also carries r1's GUILD_RELEASE_DIR and *_HOME, which every
        session inherits; B's Guild commands still run r2 because guild-exec
        resets them.
        """
        tmux = shutil.which("tmux")
        if not tmux:
            if os.environ.get("CI"):
                self.fail("tmux is required in CI for the job binding test")
            self.skipTest("tmux is not installed")
        pins_r2_file = self.pins_r2()
        root = self.tmp / "root-jobs"

        for verb, rid, pins in (
            ("deploy", "r1", self.pins_r1_file),
            ("prepare", "r2", pins_r2_file),
            ("prepare", "r3", pins_r2_file),
        ):
            cp = self.run_cli(verb, "--root", str(root), "--pins", str(pins), "--release-id", rid)
            self.assertEqual(cp.returncode, 0, f"{verb} {rid}\nstdout:\n{cp.stdout}\nstderr:\n{cp.stderr}")
        r1_bin = root / "releases" / "r1" / "bin"

        # A host entry point in the member wrapper format, with guild-exec linked to `current`.
        host_bin = self.tmp / "jobs-host-bin"
        host_bin.mkdir()
        os.symlink(root / "current" / "bin" / "guild-exec", host_bin / "guild-exec")
        (host_bin / "pr-lint").write_text('#!/bin/sh\nexec guild-exec bopeep bin/pr-lint "$@"\n', encoding="utf-8")
        (host_bin / "pr-lint").chmod(0o755)

        lint_body = self.tmp / "jobs-lint-body.md"
        lint_body.write_text(
            "## Why\n\nJob release binding.\n\n## What changes\n\nNothing.\n\n## What to check\n\nOutput marker.\n",
            encoding="utf-8",
        )
        lint = " ".join(shlex.quote(a) for a in (
            "--body", str(lint_body), "--branch", "feat/contract", "--base", "main",
            "--create-cmd", "gh pr create --label no-changelog-needed",
        ))
        host_lint = shlex.quote(str(host_bin / "pr-lint"))
        job_script = self.tmp / "job.sh"
        job_script.write_text(
            "#!/bin/sh\n"
            'd=$1\n'
            f"cd {shlex.quote(str(self.work_root / 'widgets'))}\n"
            'printf "%s\\n" "$PATH" > "$d/path"\n'
            f'pr-lint {lint} > "$d/first" 2>&1\n'
            f'{host_lint} {lint} > "$d/host-first" 2>&1\n'
            ': > "$d/ready"\n'
            'while [ ! -e "$d/go" ]; do sleep 0.1; done\n'
            f'pr-lint {lint} > "$d/second" 2>&1\n'
            f'{host_lint} {lint} > "$d/host-second" 2>&1\n'
            ': > "$d/done"\n',
            encoding="utf-8",
        )
        # The harvest command: like job-launch, start the job in a detached tmux session.
        job_start = self.tmp / "job-start"
        job_start.write_text(
            "#!/bin/sh\nset -eu\n"
            'd=$JOBS_DIR/$JOB_LABEL\n'
            'mkdir -p "$d"\n'
            ': > "$d/starting"\n'
            'if [ -n "${JOB_GATE-}" ]; then while [ ! -e "$JOB_GATE" ]; do sleep 0.1; done; fi\n'
            '"$JOB_TMUX" -L "$JOB_TMUX_SOCKET" new-session -d -s "job-$JOB_LABEL" "sh $JOB_SCRIPT $d"\n',
            encoding="utf-8",
        )
        job_start.chmod(0o755)

        jobs_dir = self.tmp / "jobs"
        socket = f"guild-step3-{os.getpid()}"
        # As in production, where the first job-launch call starts the server through
        # r1's guild-exec: r1 first on PATH and r1's release variables set.
        r1_dir = root.resolve() / "releases" / "r1"
        server_env = {
            **self.git_env,
            "HOME": str(self.user_home),
            "PATH": build_clean_path(r1_bin, host_bin),
            "GUILD_RELEASE_DIR": str(r1_dir),
            "CONCLAVE_HOME": str(r1_dir / "code" / "conclave"),
            "BOPEEP_HOME": str(r1_dir / "code" / "bopeep"),
        }
        subprocess.run(
            [tmux, "-L", socket, "-f", "/dev/null", "start-server", ";", "set", "-g", "exit-empty", "off"],
            env=server_env, check=True, capture_output=True, text=True,
        )
        self.addCleanup(
            subprocess.run, [tmux, "-L", socket, "kill-server"], env=server_env, capture_output=True
        )

        def watcher(label: str, gate: Path | None = None) -> subprocess.Popen[str]:
            env = {
                **self.git_env,
                "HOME": str(self.user_home),
                "PATH": build_clean_path(host_bin, self.stub_bin),
                "BOPEEP_GH": str(self.stub_bin / "gh"),
                "BOPEEP_STATE": str(self.tmp / f"watch-state-{label}"),
                "SYNTHETIC_GH_SCENARIO": str(self.scenario_clear),
                "JOBS_DIR": str(jobs_dir),
                "JOB_LABEL": label,
                "JOB_TMUX": tmux,
                "JOB_TMUX_SOCKET": socket,
                "JOB_SCRIPT": str(job_script),
            }
            if gate is not None:
                env["JOB_GATE"] = str(gate)
            return subprocess.Popen(
                [str(root / "current" / "bin" / "bopeep-watch"), "--once"],
                env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )

        def wait_for(path: Path, what: str, timeout: float = 60.0) -> None:
            deadline = time.monotonic() + timeout
            while not path.exists():
                if time.monotonic() > deadline:
                    logs = {p.name: p.read_text(errors="replace") for p in path.parent.glob("*") if p.is_file()}
                    self.fail(f"timed out waiting for {what}: {path}\n{logs}")
                time.sleep(0.1)

        def finish(proc: subprocess.Popen[str], label: str) -> None:
            out, _ = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, f"watcher {label} output:\n{out}")
            self.assertIn("harvest exit 0", out, f"watcher {label} output:\n{out}")

        # 1. Watcher W1 starts on r1, then holds in its harvest command across the switch to r2.
        gate = self.tmp / "jobs-gate-a"
        w1 = watcher("a", gate)
        wait_for(jobs_dir / "a" / "starting", "watcher W1 to reach its harvest command", timeout=120)
        sw = self.run_cli("switch", "--root", str(root), "--release-id", "r2", "--skip-check")
        self.assertEqual(sw.returncode, 0, sw.stderr)
        gate.touch()
        finish(w1, "a")

        # 2. A restarted watcher W2 starts from `current`, now r2, and starts job B.
        finish(watcher("b"), "b")
        wait_for(jobs_dir / "a" / "ready", "job A's first helper calls")
        wait_for(jobs_dir / "b" / "ready", "job B's first helper calls")

        # 3. Both releases are in use while their jobs run; prune keeps r1 though it is
        #    neither current nor previous after a further switch to r3.
        sw3 = self.run_cli("switch", "--root", str(root), "--release-id", "r3", "--skip-check")
        self.assertEqual(sw3.returncode, 0, sw3.stderr)
        st = self.run_cli("status", "--root", str(root), "--json")
        self.assertEqual(st.returncode, 0, st.stderr)
        in_use = json.loads(st.stdout)["in_use"]
        self.assertIn("r1", in_use)
        self.assertIn("r2", in_use)
        server_pid = int(subprocess.run(
            [tmux, "-L", socket, "display-message", "-p", "#{pid}"],
            env=server_env, check=True, capture_output=True, text=True,
        ).stdout.strip())
        self.assertNotIn(server_pid, in_use["r1"], "the tmux server must not hold r1")
        pr = self.run_cli("prune", "--root", str(root), "--json")
        self.assertEqual(pr.returncode, 0, pr.stderr)
        self.assertEqual(json.loads(pr.stdout)["removed"], [])
        self.assertTrue((root / "releases" / "r1").is_dir())

        # 4. The jobs make their second helper calls after both switches.
        for label in ("a", "b"):
            (jobs_dir / label / "go").touch()
        for label in ("a", "b"):
            wait_for(jobs_dir / label / "done", f"job {label} to finish")

        def out(label: str, name: str) -> str:
            """The last line: pr-lint prints its warnings first."""
            return (jobs_dir / label / name).read_text(encoding="utf-8").strip().splitlines()[-1]

        self.assertEqual(out("a", "path").split(os.pathsep)[0], str(root.resolve() / "releases" / "r1" / "bin"))
        self.assertEqual(out("b", "path").split(os.pathsep)[0], str(root.resolve() / "releases" / "r2" / "bin"))
        for name in ("first", "host-first", "second", "host-second"):
            self.assertEqual(out("a", name), "pr-lint PASS", f"job A {name}")
            self.assertEqual(out("b", name), "pr-lint PASS (v2)", f"job B {name}")

        # Control: outside a job, the same host entry point follows `current`.
        unbound = subprocess.run(
            [str(host_bin / "pr-lint"), *shlex.split(lint)],
            cwd=str(self.work_root / "widgets"),
            env={**self.git_env, "HOME": str(self.user_home), "PATH": build_clean_path(host_bin)},
            text=True, capture_output=True,
        )
        self.assertEqual(unbound.stdout.strip().splitlines()[-1], "pr-lint PASS (v2)", unbound.stderr)

        # 5. Once job A has exited, r1 is no longer in use and prune removes it. The tmux
        #    server still has r1 on its PATH, and does not count.
        deadline = time.monotonic() + 30
        while True:
            st = json.loads(self.run_cli("status", "--root", str(root), "--json").stdout)
            if "r1" not in st["in_use"] or time.monotonic() > deadline:
                break
            time.sleep(0.2)
        self.assertNotIn("r1", st["in_use"])
        pr = self.run_cli("prune", "--root", str(root), "--json")
        self.assertEqual(pr.returncode, 0, pr.stderr)
        pruned = json.loads(pr.stdout)
        self.assertEqual(pruned["removed"], ["r1"])
        self.assertEqual((pruned["current"], pruned["previous"]), ("r3", "r2"))
        self.assertEqual(sorted(p.name for p in (root / "releases").iterdir()), ["r2", "r3"])


if __name__ == "__main__":
    unittest.main()
