#!/usr/bin/env python3
"""Cross-repo contract test for installed pr-gate, pr-lint, and threads-check.

Checks out the pinned member commits into a temporary HOME/prefix, installs
quartermaster, bridgekeeper, and bopeep with their real install.sh scripts,
and runs the installed pr-gate command against synthetic GitHub responses.
Never touches the host installation.
"""
from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PINS_FILE = ROOT / "tests" / "member-pins.json"
REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()

GUARDED_HOST_PATHS = (
    REAL_HOME / ".local" / "bin",
    REAL_HOME / ".claude" / "hooks",
    REAL_HOME / ".claude" / "skills",
    REAL_HOME / ".config" / "systemd" / "user",
    REAL_HOME / ".config" / "work-account",
)

STUB_GH = """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

scenario = json.loads(Path(os.environ["SYNTHETIC_GH_SCENARIO"]).read_text(encoding="utf-8"))
args = sys.argv[1:]


def fail(msg: str) -> None:
    print(f"synthetic-gh error: {msg}: {args!r}", file=sys.stderr)
    sys.exit(97)


if args[:2] == ["pr", "view"]:
    print(json.dumps(scenario["pr_view"]))
    sys.exit(0)

if args[:2] == ["pr", "checks"]:
    print(json.dumps(scenario["pr_checks"]))
    sys.exit(0)

if args[:1] == ["api"]:
    rest = args[1:]
    slurp = "--slurp" in rest
    flags_with_val = {"-f", "-F", "--jq"}
    positional = []
    vars_map = {}
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok in ("--paginate", "--slurp"):
            i += 1
        elif tok in flags_with_val:
            if i + 1 >= len(rest):
                fail("missing value for flag")
            val = rest[i + 1]
            if tok in ("-f", "-F") and "=" in val:
                k, v = val.split("=", 1)
                vars_map[k] = v
            i += 2
        elif tok.startswith("-"):
            fail(f"unsupported flag {tok}")
        else:
            positional.append(tok)
            i += 1
    if not positional:
        fail("missing api endpoint")
    endpoint = positional[0].split("?", 1)[0]

    repo = scenario["repo"]
    pr = scenario["pr"]
    if endpoint == f"repos/{repo}/pulls/{pr}/files":
        print(json.dumps(scenario["files"]))
        sys.exit(0)
    if endpoint == f"repos/{repo}/issues/{pr}/comments":
        comments = scenario["comments"]
        print(json.dumps([comments] if slurp else comments))
        sys.exit(0)
    if endpoint == f"repos/{repo}/actions/workflows/claude.yml/runs":
        print(json.dumps(scenario["workflow_runs"]))
        sys.exit(0)
    if endpoint == f"repos/{repo}/pulls/{pr}/commits":
        print(json.dumps(scenario["commits"]))
        sys.exit(0)
    if endpoint == "graphql":
        q = vars_map.get("query", "")
        if "reviewDecision" in q:
            print(json.dumps({"data": {"repository": {"pullRequest": {
                "reviewDecision": scenario["pr_view"]["reviewDecision"],
            }}}}))
            sys.exit(0)
        if "reviewThreads" in q:
            print(json.dumps({"data": {"repository": {"pullRequest": {
                "number": pr,
                "state": scenario["pr_view"]["state"],
                "isDraft": scenario["pr_view"]["isDraft"],
                "baseRefName": scenario["pr_view"]["baseRefName"],
                "headRefName": scenario["pr_view"]["headRefName"],
                "headRefOid": scenario["pr_view"]["headRefOid"],
                "mergedAt": None,
                "author": scenario["pr_view"]["author"],
                "reviewThreads": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": scenario["review_threads"],
                },
            }}}}))
            sys.exit(0)
        if "commits(first:100" in q:
            print(json.dumps({"data": {"repository": {"pullRequest": {
                "commits": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [{"commit": {"oid": scenario["pr_view"]["headRefOid"]}}],
                },
            }}}}))
            sys.exit(0)
        if "reviews(first:100" in q:
            print(json.dumps({"data": {"repository": {"pullRequest": {
                "reviews": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [],
                },
            }}}}))
            sys.exit(0)
        if "comments(first:100" in q:
            print(json.dumps({"data": {"repository": {"pullRequest": {
                "comments": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [],
                },
            }}}}))
            sys.exit(0)
        fail(f"unexpected graphql query: {q[:120]}")

fail("unexpected gh command")
"""

STUB_COMMIT_LINT = """#!/usr/bin/env python3
import sys
from pathlib import Path

args = sys.argv[1:]
if args[:2] and args[0] == "--ticket":
    args = args[2:]
if args == ["-"]:
    msg = sys.stdin.read()
elif len(args) >= 2 and args[0] == "--subject":
    msg = args[1]
elif len(args) == 1:
    msg = Path(args[0]).read_text(encoding="utf-8")
else:
    print(f"usage error: {sys.argv[1:]}", file=sys.stderr)
    sys.exit(2)

subject = msg.splitlines()[0] if msg.splitlines() else ""
if not subject or len(subject) > 50 or subject.endswith(".") or not subject[0].isupper():
    print(f"FAIL subject: {subject!r}")
    sys.exit(1)
print("PASS subject")
"""


def snapshot_tree(path: Path) -> dict[str, tuple[int, int, int, int, int]]:
    snap: dict[str, tuple[int, int, int, int, int]] = {}
    if not (path.exists() or path.is_symlink()):
        return snap
    st = path.lstat()
    snap[str(path)] = (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns)
    if path.is_dir() and not path.is_symlink():
        for root, dirs, files in os.walk(path, followlinks=False):
            if Path(root).parts[-2:] == (".claude", "skills"):
                # Claude Code syncs org skills into skills/synced on its own schedule; no installer writes it.
                dirs[:] = [d for d in dirs if d != "synced"]
            for entry in [*dirs, *files]:
                full = Path(root) / entry
                est = full.lstat()
                snap[str(full)] = (est.st_dev, est.st_ino, est.st_mode, est.st_size, est.st_mtime_ns)
    return snap


def snapshot_guarded() -> dict[str, dict[str, tuple[int, int, int, int, int]]]:
    return {str(p): snapshot_tree(p) for p in GUARDED_HOST_PATHS}


def find_system_git() -> str:
    for candidate in ("/usr/bin/git", "/bin/git"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    found = shutil.which("git")
    if not found:
        raise RuntimeError("git not found on PATH")
    return found


def build_clean_path(*extra_dirs: Path) -> str:
    dirs: list[str] = [str(d) for d in extra_dirs]
    py_dir = str(Path(sys.executable).resolve().parent)
    for base in (py_dir, "/usr/local/bin", "/usr/bin", "/bin"):
        resolved = Path(base).resolve()
        # Never include host user installs (~/.local/bin or anything under REAL_HOME).
        if REAL_HOME == resolved or REAL_HOME in resolved.parents:
            continue
        if base not in dirs:
            dirs.append(base)
    return os.pathsep.join(dirs)


class PrGateContractTest(unittest.TestCase):
    pins: dict[str, str]
    host_before: dict[str, dict[str, tuple[int, int, int, int, int]]]
    tmp_ctx: tempfile.TemporaryDirectory[str]
    tmp: Path
    prefix: Path
    stub_bin: Path
    work_root: Path
    repo_dir: Path
    head_sha: str
    evidence_file: Path
    ci_first_file: Path
    base_env: dict[str, str]

    @classmethod
    def setUpClass(cls) -> None:
        cls.pins = json.loads(PINS_FILE.read_text(encoding="utf-8"))
        cls.host_before = snapshot_guarded()

        cls.tmp_ctx = tempfile.TemporaryDirectory(prefix="guild-step1-")
        cls.tmp = Path(cls.tmp_ctx.name).resolve()
        cls.prefix = cls.tmp / "home"
        cls.prefix.mkdir(parents=True)
        cls.stub_bin = cls.tmp / "stub-bin"
        cls.stub_bin.mkdir(parents=True)

        system_git = find_system_git()
        git_env = {
            "PATH": build_clean_path(),
            "HOME": str(cls.prefix),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_AUTHOR_NAME": "Acme Author",
            "GIT_AUTHOR_EMAIL": "author@acme.example",
            "GIT_COMMITTER_NAME": "Acme Author",
            "GIT_COMMITTER_EMAIL": "author@acme.example",
        }

        def git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [system_git, *args],
                cwd=str(cwd) if cwd else None,
                env=git_env,
                text=True,
                capture_output=True,
                check=True,
            )

        # Materialize clean default-branch checkouts at the exact pinned commits.
        sources_root = Path(os.environ["GUILD_MEMBER_SOURCES_DIR"]).resolve() if os.environ.get("GUILD_MEMBER_SOURCES_DIR") else REAL_HOME / "code"
        origins_dir = cls.tmp / "origins"
        origins_dir.mkdir(parents=True)
        code_dir = cls.prefix / "code"
        code_dir.mkdir(parents=True)

        for name, expected_sha in cls.pins.items():
            src = sources_root / name
            if not src.is_dir():
                raise RuntimeError(f"missing member source checkout for {name}: {src}")
            obj_type = git("-C", str(src), "cat-file", "-t", expected_sha).stdout.strip()
            if obj_type != "commit":
                raise RuntimeError(f"{name}@{expected_sha} is {obj_type!r}, expected commit")
            dest = code_dir / name
            is_shallow = git("-C", str(src), "rev-parse", "--is-shallow-repository").stdout.strip()
            if is_shallow == "true":
                shutil.copytree(src, dest, symlinks=True)
                git("-C", str(dest), "reset", "--hard", "-q", expected_sha)
                git("-C", str(dest), "clean", "-fdq")
                git("-C", str(dest), "checkout", "-q", "-B", "main", expected_sha)
                git("-C", str(dest), "update-ref", "refs/remotes/origin/main", expected_sha)
                git("-C", str(dest), "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
            else:
                bare = origins_dir / f"{name}.git"
                git("init", "-q", "--bare", "-b", "main", str(bare))
                git("-C", str(src), "push", "-q", str(bare), f"{expected_sha}:refs/heads/main")
                git("clone", "-q", str(bare), str(dest))
            actual_sha = git("-C", str(dest), "rev-parse", "HEAD").stdout.strip()
            if actual_sha != expected_sha:
                raise RuntimeError(f"{name}: checked out {actual_sha}, expected {expected_sha}")
            dirty = git("-C", str(dest), "status", "--porcelain").stdout.strip()
            if dirty:
                raise RuntimeError(f"{name}: expected clean checkout, got: {dirty}")

        # Provide a local pstack placeholder so quartermaster/install.sh never hits the network.
        pstack_dir = cls.tmp / "cursor-plugins" / "pstack"
        (pstack_dir / "skills" / "poteto-mode").mkdir(parents=True)
        (pstack_dir / "skills" / "poteto-mode" / "SKILL.md").write_text("---\nname: poteto-mode\n---\n", encoding="utf-8")
        kit_dir = pstack_dir.parent / "cursor-team-kit"
        (kit_dir / "skills" / "deslop").mkdir(parents=True)
        (kit_dir / "skills" / "deslop" / "SKILL.md").write_text("---\nname: deslop\n---\n", encoding="utf-8")

        installer_home = cls.tmp / "installer-home"
        installer_home.mkdir()
        install_env = {
            **git_env,
            "PATH": build_clean_path(
                cls.prefix / ".local" / "share" / "quartermaster" / "bin",
                cls.prefix / ".local" / "bin",
            ),
            "PSTACK_HOME": str(pstack_dir),
            "HOME": str(installer_home),
        }
        for tool in ("quartermaster", "bridgekeeper", "bopeep"):
            installer = code_dir / tool / "install.sh"
            cp = subprocess.run(
                ["bash", str(installer), "--prefix", str(cls.prefix)],
                env=install_env,
                text=True,
                capture_output=True,
            )
            if cp.returncode != 0:
                raise RuntimeError(f"{tool}/install.sh failed ({cp.returncode}):\n{cp.stdout}\n{cp.stderr}")
            chk = subprocess.run(
                ["bash", str(installer), "--prefix", str(cls.prefix), "--check"],
                env=install_env,
                text=True,
                capture_output=True,
            )
            if chk.returncode != 0:
                raise RuntimeError(f"{tool}/install.sh --check failed ({chk.returncode}):\n{chk.stdout}\n{chk.stderr}")

        # Synthetic work root (AcmeCorp/widgets) with a real local git bare remote.
        cls.work_root = cls.tmp / "work"
        state_dir = cls.work_root / "state"
        (state_dir / "autonomy-gates.d").mkdir(parents=True)
        (cls.work_root / "gnupg").mkdir(parents=True)
        (state_dir / "pr-grant.md").write_text("Status: GRANTED\n", encoding="utf-8")

        guild_cfg = {
            "github_owner": "AcmeCorp",
            "default_repo": "AcmeCorp/widgets",
            "author_logins": ["acme-author"],
            "author_emails": ["author@acme.example"],
            "author_personal_emails": ["author@home.example"],
            "persona_names": ["Robo", "Helper"],
            "bopeep": {
                "state_dir": str(state_dir),
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
            },
        }
        (cls.work_root / ".guild.json").write_text(json.dumps(guild_cfg, indent=2) + "\n", encoding="utf-8")

        work_account_cfg = cls.tmp / "work-account.json"
        work_account_cfg.write_text(
            json.dumps({
                "work_path_prefixes": [str(cls.work_root)],
                "work_github_owners": ["AcmeCorp"],
            }) + "\n",
            encoding="utf-8",
        )

        widgets_origin = origins_dir / "widgets.git"
        git("init", "-q", "--bare", "-b", "main", str(widgets_origin))
        cls.repo_dir = cls.work_root / "widgets"
        git("clone", "-q", str(widgets_origin), str(cls.repo_dir))
        (cls.repo_dir / "README.md").write_text("# widgets\n", encoding="utf-8")
        git("add", "README.md", cwd=cls.repo_dir)
        git("commit", "-qm", "Initial widgets commit", cwd=cls.repo_dir)
        git("push", "-q", "origin", "HEAD:main", cwd=cls.repo_dir)

        git("checkout", "-qb", "feat/contract", cwd=cls.repo_dir)
        (cls.repo_dir / "src").mkdir()
        (cls.repo_dir / "src" / "widget.py").write_text("x = 1\n", encoding="utf-8")
        git("add", "src/widget.py", cwd=cls.repo_dir)
        git(
            "commit",
            "-q",
            "-m",
            "Add widget gate contract\n\nFixed: test widget gate contract",
            cwd=cls.repo_dir,
        )
        git("push", "-q", "origin", "HEAD:feat/contract", cwd=cls.repo_dir)
        cls.head_sha = git("-C", str(cls.repo_dir), "rev-parse", "HEAD").stdout.strip()

        # Synthetic test helpers on stub_bin (only gh + test org commit-lint/notify).
        gh_bin = cls.stub_bin / "gh"
        gh_bin.write_text(STUB_GH, encoding="utf-8")
        gh_bin.chmod(0o755)

        commit_lint = cls.stub_bin / "acme-commit-lint"
        commit_lint.write_text(STUB_COMMIT_LINT, encoding="utf-8")
        commit_lint.chmod(0o755)

        notify = cls.stub_bin / "acme-notify"
        notify.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        notify.chmod(0o755)

        cls.evidence_file = cls.tmp / "evidence.md"
        cls.evidence_file.write_text(
            "## Blast-radius\n\nScoped to widgets only.\n",
            encoding="utf-8",
        )

        cls.ci_first_file = cls.tmp / "ci-first.json"
        cls.ci_first_file.write_text(
            json.dumps({
                "threads": [
                    {"is_ci": True, "head": cls.head_sha, "resolved": True, "replies": []},
                ],
                "commit_shas": [cls.head_sha],
            }) + "\n",
            encoding="utf-8",
        )

        cls.base_env = {
            **git_env,
            "HOME": str(cls.prefix),
            "WORK_ACCOUNT_CONFIG": str(work_account_cfg),
            "CONCLAVE_HOME": str(code_dir / "conclave"),
            "BOPEEP_CI_FIRST_FIXTURE": str(cls.ci_first_file),
            "BOPEEP_OWN_PR_AUTHORS": "acme-author",
            "BOPEEP_GH": str(gh_bin),
            "CONCLAVE_GH": str(gh_bin),
            "GH_TOKEN": "",
            "GITHUB_TOKEN": "",
            "PATH": build_clean_path(cls.prefix / ".local" / "bin", cls.stub_bin),
        }

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            host_after = snapshot_guarded()
            if host_after != cls.host_before:
                raise AssertionError("Host installation paths changed during contract test")
        finally:
            cls.tmp_ctx.cleanup()

    def write_scenario(self, *, review_threads: list[dict], body: str | None = None) -> Path:
        pr_body = body or (
            "## Why\n\nExercise the installed pr-gate contract.\n\n"
            "## What changes\n\nAdds widget contract logic.\n\n"
            "## What to check\n\nGate helper wiring.\n"
        )
        scenario = {
            "repo": "AcmeCorp/widgets",
            "pr": 7,
            "pr_view": {
                "number": 7,
                "title": "Add widget gate contract",
                "body": pr_body,
                "state": "OPEN",
                "isDraft": False,
                "headRefName": "feat/contract",
                "headRefOid": self.head_sha,
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
                        f"<sub>Reviewed commit `{self.head_sha}`. "
                        "Add the `risk:reclassify` label to run it again.</sub>"
                    ),
                }
            ],
            "workflow_runs": {"workflow_runs": []},
            "commits": [
                {
                    "sha": self.head_sha,
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
            "review_threads": review_threads,
        }
        tag = "lintfail" if body is not None else str(len(review_threads))
        path = self.tmp / f"scenario-{os.getpid()}-{tag}.json"
        path.write_text(json.dumps(scenario), encoding="utf-8")
        return path

    def run_installed_gate(
        self,
        scenario_path: Path,
        *,
        bin_dir: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        active_bin = bin_dir or (self.prefix / ".local" / "bin")
        env = {
            **self.base_env,
            "SYNTHETIC_GH_SCENARIO": str(scenario_path),
            "PATH": build_clean_path(active_bin, self.stub_bin),
        }
        return subprocess.run(
            [
                str(active_bin / "pr-gate"),
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

    def test_installed_wrappers_resolve_inside_temp_prefix(self) -> None:
        installed_bin = self.prefix / ".local" / "bin"
        for cmd, tool, rel in (
            ("pr-gate", "bridgekeeper", "bin/pr-gate"),
            ("pr-lint", "bopeep", "bin/pr-lint"),
            ("threads-check", "bopeep", "bin/threads-check"),
            ("guild-config", "quartermaster", "bin/guild-config"),
        ):
            wrapper = installed_bin / cmd
            self.assertTrue(wrapper.is_file() and not wrapper.is_symlink(), wrapper)
            self.assertEqual(
                wrapper.read_text(encoding="utf-8"),
                f'#!/bin/sh\nexec guild-exec {tool} {rel} "$@"\n',
            )
            resolved = shutil.which(cmd, path=self.base_env["PATH"])
            self.assertEqual(Path(resolved).resolve(), wrapper.resolve())

    def test_clear_gate_passes_with_real_installed_helpers(self) -> None:
        scenario = self.write_scenario(review_threads=[])
        cp = self.run_installed_gate(scenario)
        self.assertEqual(cp.returncode, 0, f"stdout:\n{cp.stdout}\nstderr:\n{cp.stderr}")
        self.assertIn("PASS: lint: pr-lint PASS", cp.stdout)
        self.assertIn("PASS: unanswered_threads:", cp.stdout)
        self.assertIn("pr-gate PASS", cp.stdout)

    def test_blocker_url_fails_with_discussion_url(self) -> None:
        blocker_url = "https://github.com/AcmeCorp/widgets/pull/7#discussion_r456"
        threads = [
            {
                "id": "THREAD_1",
                "isResolved": False,
                "isOutdated": False,
                "path": "src/widget.py",
                "line": 1,
                "comments": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [
                        {
                            "author": {"login": "a-reviewer"},
                            "createdAt": "2026-11-17T00:01:00Z",
                            "url": blocker_url,
                            "body": "Please fix this before merge.",
                            "databaseId": 456,
                        }
                    ],
                },
            }
        ]
        scenario = self.write_scenario(review_threads=threads)
        cp = self.run_installed_gate(scenario)
        self.assertEqual(cp.returncode, 1, f"stdout:\n{cp.stdout}\nstderr:\n{cp.stderr}")
        self.assertIn("PASS: lint: pr-lint PASS", cp.stdout)
        self.assertIn(f"FAIL: unanswered_threads: unanswered: {blocker_url}", cp.stdout)
        self.assertIn("pr-gate FAIL (1 checks)", cp.stdout)

    def test_pr_lint_failure_fails_gate(self) -> None:
        bad_body = (
            "## Why\n\nHas an em dash \u2014 should fail real pr-lint.\n\n"
            "## What changes\n\nAdds widget contract logic.\n"
        )
        scenario = self.write_scenario(review_threads=[], body=bad_body)
        cp = self.run_installed_gate(scenario)
        self.assertEqual(cp.returncode, 1, f"stdout:\n{cp.stdout}\nstderr:\n{cp.stderr}")
        self.assertIn("FAIL: lint:", cp.stdout)
        self.assertIn("em dash", cp.stdout)
        self.assertIn("pr-gate FAIL (1 checks)", cp.stdout)

    def test_missing_threads_check_fails_closed(self) -> None:
        isolated_bin = self.tmp / "bin-no-threads-check"
        shutil.copytree(self.prefix / ".local" / "bin", isolated_bin, symlinks=True)
        (isolated_bin / "threads-check").unlink()
        self.assertIsNone(shutil.which("threads-check", path=build_clean_path(isolated_bin, self.stub_bin)))

        scenario = self.write_scenario(review_threads=[])
        cp = self.run_installed_gate(scenario, bin_dir=isolated_bin)
        self.assertEqual(cp.returncode, 2, f"stdout:\n{cp.stdout}\nstderr:\n{cp.stderr}")
        self.assertIn("PASS: lint: pr-lint PASS", cp.stdout)
        self.assertIn("ERROR: unanswered_threads:", cp.stdout)
        self.assertIn("pr-gate ERROR (1 checks; not ready)", cp.stdout)

    def test_missing_pr_lint_fails_closed(self) -> None:
        isolated_bin = self.tmp / "bin-no-pr-lint"
        shutil.copytree(self.prefix / ".local" / "bin", isolated_bin, symlinks=True)
        (isolated_bin / "pr-lint").unlink()
        self.assertIsNone(shutil.which("pr-lint", path=build_clean_path(isolated_bin, self.stub_bin)))

        scenario = self.write_scenario(review_threads=[])
        cp = self.run_installed_gate(scenario, bin_dir=isolated_bin)
        self.assertEqual(cp.returncode, 2, f"stdout:\n{cp.stdout}\nstderr:\n{cp.stderr}")
        self.assertIn("ERROR: lint: pr-lint not on PATH", cp.stdout)
        self.assertIn("PASS: unanswered_threads:", cp.stdout)
        self.assertIn("pr-gate ERROR (1 checks; not ready)", cp.stdout)

    def test_host_installs_never_modified(self) -> None:
        self.assertEqual(snapshot_guarded(), self.host_before)


if __name__ == "__main__":
    unittest.main()
