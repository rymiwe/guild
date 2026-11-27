"""Exact-commit local-test evidence for Guild pin and release gates.

Evidence is produced only by running each repo's workflow-equivalent suite
against a clean checkout. Repo identity comes from origin; guild pins come from
tests/member-pins.json in that checkout. Member evidence is keyed by the full
commit SHA (not only its tree): tests may read git history/metadata. Cross-host
import authorizes only after suite/tree/pins/sha and successful exit codes
validate. Workflow digests fail closed on drift. This is not GitHub CI.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
STATUSES = frozenset({"passed", "failed"})
SCHEMA = 3
AUTHORITY_LOCAL = "local-run"
AUTHORITY_IMPORTED = "imported"
BILLING_NEEDLE = "spending limit needs to be increased"
BILLING_NEEDLE_ALT = "recent account payments have failed"

# suite_id -> (workflow relative path, sha256 of that file at the mapped command set).
# A checkout whose workflow digest is absent here cannot produce evidence until the
# mapping is updated after verifying commands still match CI.
WORKFLOW_DIGESTS: dict[str, tuple[str, str]] = {
    "quartermaster-tests-yml-v1": (
        ".github/workflows/tests.yml",
        "8774a18f37fe869749cf9e9fcf66fe0369a2f890371a3310518efd2c2689991c",
    ),
    "guild-ci-yml-v2": (
        ".github/workflows/ci.yml",
        "e388d2ec320e60fda65546fea01d78290e09512e08ae099724db49f33a2448f4",
    ),
    "conclave-tests-yml-v1": (
        ".github/workflows/tests.yml",
        "23c8bcf4e000609a4450dd78942c24334585f408acded71295143bf3a1131ed8",
    ),
}

_QM_PY = [
    "tests/test_seat_usage.py",
    "tests/test_ci_review_route.py",
    "tests/test_properties.py",
    "tests/test_contract.py",
    "tests/test_update.py",
    "tests/test_install.py",
    "tests/test_seat_usage_push.py",
    "tests/test_seat_meters.py",
    "tests/test_account_gate.py",
    "tests/test_guild_config.py",
    "tests/test_guild_exec.py",
    "tests/test_draw_calibrate.py",
]


def _qm_commands(_checkout: Path) -> list[list[str]]:
    cmds = [["python3", p] for p in _QM_PY]
    cmds.append(["bash", "tests/jobs/run_all.sh"])
    return cmds


def _guild_commands(_checkout: Path) -> list[list[str]]:
    return [
        ["python3", "tests/test_true_check.py", "-v"],
        ["python3", "tests/test_jev_router.py", "-v"],
        ["python3", "-B", "tests/test_autostart_draft_review.py"],
        ["python3", "-B", "tests/work-review-requests-check-fixtures/run_tests.py"],
        ["python3", "tests/test_pr_gate_contract.py", "-v"],
        ["bash", "tests/run-bridgekeeper-posting.sh", "test-evidence-check"],
        ["bash", "tests/run-bridgekeeper-posting.sh", "test-post-gated-review"],
        ["bash", "tests/run-bridgekeeper-posting.sh", "test-review-lint"],
        ["python3", "tests/test_release_lifecycle.py", "-v"],
        ["python3", "tests/test_release_retention.py", "-v"],
        ["python3", "tests/test_guild_update.py", "-v"],
        ["python3", "tests/test_guild_pin.py", "-v"],
        ["python3", "tests/test_guild_host_sync.py", "-v"],
        ["python3", "tests/test_grokbot_pace_check.py", "-v"],
        ["python3", "-W", "error::ResourceWarning", "tests/test_lint_scorecard.py", "-v"],
        ["python3", "-W", "error::ResourceWarning", "tests/test_poteto_judge.py", "-v"],
        ["python3", "tests/test_guild_ledger.py", "-v"],
        ["python3", "tests/test_poteto_watch.py", "-v"],
        ["python3", "tests/test_guild_triage.py", "-v"],
    ]


def _conclave_commands(checkout: Path) -> list[list[str]]:
    """Match .github/workflows/tests.yml discovery: sorted scripts/test-*.{py,sh}."""
    scripts = checkout / "scripts"
    found = sorted(
        [p for p in scripts.glob("test-*.py")] + [p for p in scripts.glob("test-*.sh")],
        key=lambda p: p.as_posix(),
    )
    if not found:
        raise RuntimeError("conclave suite discovered no scripts/test-*.py or scripts/test-*.sh")
    cmds: list[list[str]] = []
    for path in found:
        rel = path.relative_to(checkout).as_posix()
        if path.suffix == ".py":
            cmds.append(["python3", rel])
        else:
            cmds.append(["bash", rel])
    return cmds


# short-name -> (suite_id, commands builder). Unsupported members stay blocked.
SUITES: dict[str, tuple[str, Callable[[Path], list[list[str]]]]] = {
    "quartermaster": ("quartermaster-tests-yml-v1", _qm_commands),
    "guild": ("guild-ci-yml-v2", _guild_commands),
    "conclave": ("conclave-tests-yml-v1", _conclave_commands),
}

_EMPTY_HOME: str | None = None


def _empty_home() -> str:
    global _EMPTY_HOME
    if _EMPTY_HOME is None:
        d = Path(tempfile.mkdtemp(prefix="guild-local-test-home-"))
        _EMPTY_HOME = str(d)
    return _EMPTY_HOME


def state_dir() -> Path:
    env = os.environ.get("GUILD_LOCAL_TEST_DIR")
    if env:
        return Path(env)
    xdg = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(xdg) / "guild" / "local-test"


def normalize_sha(sha: str) -> str:
    s = sha.strip().lower()
    if not SHA_RE.fullmatch(s):
        raise ValueError(f"need a full 40-character commit SHA, got {sha!r}")
    return s


def normalize_repo(repo: str) -> str:
    r = repo.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", r):
        raise ValueError(f"need owner/name repo, got {repo!r}")
    return r


def short_name(repo: str) -> str:
    return normalize_repo(repo).split("/", 1)[1]


def suite_for(repo: str, checkout: Path | None = None) -> tuple[str, list[list[str]]]:
    name = short_name(repo)
    if name not in SUITES:
        raise ValueError(
            f"local-test has no workflow-equivalent suite for {repo}; "
            f"supported: {', '.join(sorted(SUITES))}"
        )
    suite_id, builder = SUITES[name]
    if checkout is None:
        # Fixed suites can list commands without a checkout; dynamic ones cannot.
        if name == "conclave":
            raise ValueError("conclave suite requires a checkout to discover tests")
        return suite_id, builder(Path("."))
    return suite_id, builder(checkout)


def workflow_digest(checkout: Path, suite_id: str) -> str:
    if suite_id not in WORKFLOW_DIGESTS:
        raise RuntimeError(f"no workflow digest registered for suite {suite_id}")
    rel, expected = WORKFLOW_DIGESTS[suite_id]
    path = checkout / rel
    if not path.is_file():
        raise RuntimeError(f"missing workflow file {rel} in checkout")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != expected:
        raise RuntimeError(
            f"workflow drift: {rel} digest {digest} is not registered for {suite_id} "
            f"(expected {expected}); update the local-test mapping after verifying commands"
        )
    return digest


def _evidence_path(repo: str, sha: str) -> Path:
    owner, name = normalize_repo(repo).split("/", 1)
    return state_dir() / owner / name / f"{normalize_sha(sha)}.json"


def git(cwd: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {out.stderr.strip()}")
    return out.stdout.strip()


def origin_repo(checkout: Path) -> str:
    url = git(checkout, "remote", "get-url", "origin")
    m = re.search(r"github\.com[:/]+([^/]+/[^/]+?)(?:\.git)?$", url)
    if not m:
        raise RuntimeError(f"origin is not a github.com URL: {url}")
    return normalize_repo(m.group(1))


def checkout_identity(checkout: Path) -> tuple[str, str]:
    root = checkout.resolve()
    try:
        git(root, "rev-parse", "--git-dir")
    except RuntimeError as exc:
        raise RuntimeError(f"{root} is not a git checkout") from exc
    if git(root, "status", "--porcelain"):
        raise RuntimeError(f"{root} is dirty; refuse local-test on a changed tree")
    sha = normalize_sha(git(root, "rev-parse", "HEAD"))
    tree = normalize_sha(git(root, "rev-parse", "HEAD^{tree}"))
    return sha, tree


def load_pins(path: Path) -> dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not data:
        raise ValueError(f"pins file {path} must be a non-empty object")
    out: dict[str, str] = {}
    for key, val in data.items():
        if not isinstance(key, str) or not isinstance(val, str):
            raise ValueError(f"pins file {path} has a non-string entry")
        out[key] = normalize_sha(val)
    return out


def pins_from_checkout(checkout: Path, repo: str) -> dict[str, str] | None:
    if short_name(repo) != "guild":
        return None
    path = checkout / "tests" / "member-pins.json"
    if not path.is_file():
        raise RuntimeError(f"guild checkout missing {path}")
    return load_pins(path)


def materialize_pinned_members(pins: dict[str, str], sources_root: Path) -> Path:
    deps = Path(tempfile.mkdtemp(prefix="guild-local-test-deps-"))
    for name, sha in pins.items():
        src = sources_root / name
        try:
            git(src, "rev-parse", "--git-dir")
        except (RuntimeError, OSError) as exc:
            raise RuntimeError(
                f"member source missing for pin {name}@{sha[:12]} under {sources_root}"
            ) from exc
        dest = deps / name
        clone = subprocess.run(
            ["git", "clone", "-q", "--shared", str(src), str(dest)],
            capture_output=True, text=True,
        )
        if clone.returncode != 0:
            raise RuntimeError(f"cannot clone {name} for local-test: {clone.stderr.strip()}")
        try:
            git(dest, "checkout", "-q", "--detach", sha)
        except RuntimeError as exc:
            raise RuntimeError(
                f"cannot check out pin {name}@{sha}; fetch that commit into {src}"
            ) from exc
        if normalize_sha(git(dest, "rev-parse", "HEAD")) != normalize_sha(sha):
            raise RuntimeError(f"deps/{name} HEAD is not pin {sha}")
    return deps


def require_tools(*tools: str) -> None:
    for tool in tools:
        if shutil.which(tool) is None:
            raise RuntimeError(
                f"required tool {tool!r} missing (CI installs it); refusing to skip"
            )


def _commands_prefix_of_suite(commands: list[dict[str, Any]], suite_cmds: list[list[str]]) -> bool:
    if not commands or len(commands) > len(suite_cmds):
        return False
    for got, want in zip(commands, suite_cmds):
        if list(got.get("argv") or []) != want:
            return False
    return True


def _conclave_commands_plausible(commands: list[dict[str, Any]]) -> bool:
    if not commands:
        return False
    for cmd in commands:
        argv = list(cmd.get("argv") or [])
        if len(argv) != 2 or argv[0] not in ("python3", "bash"):
            return False
        if not re.fullmatch(r"scripts/test-[^/]+\.(py|sh)", argv[1]):
            return False
        if argv[0] == "python3" and not argv[1].endswith(".py"):
            return False
        if argv[0] == "bash" and not argv[1].endswith(".sh"):
            return False
    return True


def _valid_record(data: Any, *, suite_cmds: list[list[str]] | None = None) -> dict[str, Any] | None:
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        return None
    try:
        repo = normalize_repo(str(data.get("repo", "")))
        sha = normalize_sha(str(data.get("sha", "")))
        tree = normalize_sha(str(data.get("tree", "")))
    except ValueError:
        return None
    status = data.get("status")
    authority = data.get("authority")
    suite_id = data.get("suite_id")
    workflow = data.get("workflow_digest")
    if status not in STATUSES or authority not in (AUTHORITY_LOCAL, AUTHORITY_IMPORTED):
        return None
    if not isinstance(suite_id, str) or suite_id not in WORKFLOW_DIGESTS:
        return None
    if not isinstance(workflow, str) or workflow != WORKFLOW_DIGESTS[suite_id][1]:
        return None
    name = short_name(repo)
    if name not in SUITES or SUITES[name][0] != suite_id:
        return None
    commands = data.get("commands")
    if not isinstance(commands, list) or not commands:
        return None
    for cmd in commands:
        if not isinstance(cmd, dict):
            return None
        if not isinstance(cmd.get("argv"), list) or not cmd["argv"]:
            return None
        if not isinstance(cmd.get("exit_code"), int):
            return None
    if name == "conclave":
        if suite_cmds is not None:
            if not _commands_prefix_of_suite(commands, suite_cmds):
                return None
        elif not _conclave_commands_plausible(commands):
            return None
        want_len = len(suite_cmds) if suite_cmds is not None else len(commands)
    else:
        if suite_cmds is None:
            try:
                _sid, suite_cmds = suite_for(repo)
            except ValueError:
                return None
        if not _commands_prefix_of_suite(commands, suite_cmds):
            return None
        want_len = len(suite_cmds)
    pins = data.get("pins")
    if pins is not None:
        if not isinstance(pins, dict) or not pins:
            return None
        try:
            pins = {k: normalize_sha(v) for k, v in pins.items()
                    if isinstance(k, str) and isinstance(v, str)}
        except ValueError:
            return None
        if len(pins) != len(data["pins"]):
            return None
    expect_pins = name == "guild"
    if expect_pins and pins is None:
        return None
    if not expect_pins and pins is not None:
        return None
    if status == "passed":
        if len(commands) != want_len:
            return None
        if any(c["exit_code"] != 0 for c in commands):
            return None
    else:
        if len(commands) == want_len and all(c["exit_code"] == 0 for c in commands):
            return None
        if len(commands) < want_len and commands[-1]["exit_code"] == 0:
            return None
    return {
        "schema": SCHEMA,
        "authority": authority,
        "suite_id": suite_id,
        "workflow_digest": workflow,
        "repo": repo,
        "sha": sha,
        "tree": tree,
        "pins": pins,
        "status": status,
        "commands": [{"argv": list(c["argv"]), "exit_code": c["exit_code"]} for c in commands],
        "recorded_at": data.get("recorded_at"),
        "host": data.get("host"),
    }


def read_sha(repo: str, sha: str) -> dict[str, Any] | None:
    path = _evidence_path(repo, sha)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    rec = _valid_record(raw)
    if rec is None or rec["sha"] != normalize_sha(sha) or rec["repo"] != normalize_repo(repo):
        return None
    return rec


def _match(
    rec: dict[str, Any],
    *,
    tree: str,
    pins: dict[str, str] | None,
    require_passed: bool,
) -> dict[str, Any] | None:
    if require_passed and rec["status"] != "passed":
        return None
    if not require_passed and rec["status"] not in STATUSES:
        return None
    if rec["tree"] != normalize_sha(tree):
        return None
    want_pins = None if pins is None else {k: normalize_sha(v) for k, v in pins.items()}
    if rec.get("pins") != want_pins:
        return None
    return rec


def accepts(
    repo: str,
    *,
    sha: str,
    tree: str,
    pins: dict[str, str] | None = None,
    require_passed: bool = True,
) -> dict[str, Any] | None:
    """Accept evidence for this exact commit SHA (and tree/pins).

    Members never authorize via tree alone. Guild may authorize a different SHA
    only when that SHA's tree and pins are identical to a passed record (merge
    commit of a tested pin PR); callers must pass the install candidate's real
    tree from git.
    """
    repo = normalize_repo(repo)
    sha = normalize_sha(sha)
    tree = normalize_sha(tree)
    exact = read_sha(repo, sha)
    if exact is not None:
        return _match(exact, tree=tree, pins=pins, require_passed=require_passed)
    if short_name(repo) != "guild" or not require_passed:
        return None
    # Guild merge-commit path: same tree+pins as a previously tested candidate.
    want_pins = None if pins is None else {k: normalize_sha(v) for k, v in pins.items()}
    owner, name = repo.split("/", 1)
    base = state_dir() / owner / name
    if not base.is_dir():
        return None
    for path in sorted(base.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rec = _valid_record(raw)
        if rec is None or rec["repo"] != repo:
            continue
        if rec["status"] != "passed":
            continue
        if rec["tree"] != tree or rec.get("pins") != want_pins:
            continue
        return rec
    return None


def write_record(rec: dict[str, Any], *, suite_cmds: list[list[str]] | None = None) -> Path:
    valid = _valid_record(rec, suite_cmds=suite_cmds)
    if valid is None:
        raise ValueError("refusing to write malformed local-test evidence")
    path = _evidence_path(valid["repo"], valid["sha"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(valid, indent=2) + "\n", encoding="utf-8")
    return path


def run(*, checkout: Path, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Run the fixed suite for checkout's origin repo. No caller-chosen commands."""
    checkout = checkout.resolve()
    repo = origin_repo(checkout)
    suite_id, commands = suite_for(repo, checkout)
    digest = workflow_digest(checkout, suite_id)
    sha, tree = checkout_identity(checkout)
    pins = pins_from_checkout(checkout, repo)
    for argv in commands:
        for part in argv:
            if part.startswith("tests/") or part.startswith("scripts/"):
                if not (checkout / part).exists():
                    raise RuntimeError(f"required suite path missing: {part}")
    results: list[dict[str, Any]] = []
    deps: Path | None = None
    name = short_name(repo)
    if name == "quartermaster":
        run_env = {
            "PATH": "/usr/bin:/bin",
            "HOME": _empty_home(),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        if os.environ.get("PYTHONPATH"):
            run_env["PYTHONPATH"] = os.environ["PYTHONPATH"]
        probe = subprocess.run(
            ["python3", "-c", "import PIL"], env=run_env, capture_output=True, text=True,
        )
        if probe.returncode != 0:
            raise RuntimeError(
                "required Pillow missing for python3 (CI installs python3-pil); "
                "refusing to skip test_seat_meters"
            )
    elif name == "guild":
        require_tools("tmux", "gitleaks")
        if pins is None:
            raise RuntimeError("guild local-test requires tests/member-pins.json")
        sources_root = Path(
            os.environ.get("GUILD_MEMBER_SOURCES_DIR") or Path.home() / "code"
        ).resolve()
        deps = materialize_pinned_members(pins, sources_root)
        run_env = os.environ.copy()
        run_env["CI"] = "1"
        run_env["GUILD_MEMBER_SOURCES_DIR"] = str(deps)
        run_env["CONCLAVE_HOME"] = str(deps / "conclave")
    elif name == "conclave":
        require_tools("tmux", "gitleaks", "jq", "perl")
        sources_root = Path(
            os.environ.get("GUILD_MEMBER_SOURCES_DIR") or Path.home() / "code"
        ).resolve()
        bk = sources_root / "bridgekeeper"
        try:
            git(bk, "rev-parse", "--git-dir")
        except (RuntimeError, OSError) as exc:
            raise RuntimeError(
                "conclave local-test requires bridgekeeper under GUILD_MEMBER_SOURCES_DIR"
            ) from exc
        run_env = os.environ.copy()
        run_env["CI"] = "1"
        run_env["BRIDGEKEEPER_HOME"] = str(bk)
        run_env.setdefault("GIT_AUTHOR_NAME", "conclave-ci")
        run_env.setdefault("GIT_AUTHOR_EMAIL", "ci@example.invalid")
        run_env.setdefault("GIT_COMMITTER_NAME", "conclave-ci")
        run_env.setdefault("GIT_COMMITTER_EMAIL", "ci@example.invalid")
    elif name in SUITES:
        # Extension/test suites registered in SUITES use the ambient environment.
        run_env = os.environ.copy()
    else:
        raise RuntimeError(f"local-test suite not configured for {repo}")
    if env:
        run_env.update(env)
    try:
        try:
            for argv in commands:
                proc = subprocess.run(argv, cwd=checkout, env=run_env, capture_output=True, text=True)
                code = proc.returncode
                results.append({"argv": list(argv), "exit_code": code})
                if code != 0:
                    break
        except KeyboardInterrupt as exc:
            raise RuntimeError("local-test interrupted; no evidence written") from exc
        status = (
            "passed"
            if len(results) == len(commands) and all(r["exit_code"] == 0 for r in results)
            else "failed"
        )
        sha2, tree2 = checkout_identity(checkout)
        if (sha2, tree2) != (sha, tree):
            raise RuntimeError("checkout HEAD or tree changed during local-test; no evidence written")
        if pins_from_checkout(checkout, repo) != pins:
            raise RuntimeError("pins changed during local-test; no evidence written")
        rec = {
            "schema": SCHEMA,
            "authority": AUTHORITY_LOCAL,
            "suite_id": suite_id,
            "workflow_digest": digest,
            "repo": repo,
            "sha": sha,
            "tree": tree,
            "pins": pins,
            "status": status,
            "commands": results,
            "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "host": socket.gethostname(),
        }
        write_record(rec, suite_cmds=commands)
        return rec
    finally:
        if deps is not None:
            shutil.rmtree(deps, ignore_errors=True)


def export_record(repo: str, sha: str) -> str:
    rec = read_sha(repo, sha)
    if rec is None:
        raise FileNotFoundError(f"no local-test evidence for {repo} sha {sha}")
    return json.dumps(rec, indent=2) + "\n"


def import_record(text: str) -> Path:
    """Import evidence after suite/sha/tree/pins/result validation."""
    raw = json.loads(text)
    if not isinstance(raw, dict):
        raise ValueError("import rejected: not an object")
    raw = dict(raw)
    raw["authority"] = AUTHORITY_IMPORTED
    rec = _valid_record(raw)
    if rec is None:
        raise ValueError("import rejected: malformed or suite-mismatched evidence")
    return write_record(rec)


def actions_enabled(repo: str) -> bool:
    override = os.environ.get("GUILD_ACTIONS_ENABLED")
    if override is not None:
        return override.strip() not in ("0", "false", "False", "")
    repo = normalize_repo(repo)
    out = subprocess.run(
        ["gh", "api", f"repos/{repo}/actions/permissions", "--jq", ".enabled"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(f"cannot read Actions permissions for {repo}: {out.stderr.strip()}")
    return out.stdout.strip() == "true"


def check_run_is_billing_failure(repo: str, run_id: int) -> bool:
    out = subprocess.run(
        ["gh", "api", f"repos/{normalize_repo(repo)}/check-runs/{run_id}/annotations"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        return False
    try:
        notes = json.loads(out.stdout or "[]")
    except json.JSONDecodeError:
        return False
    text = " ".join(str(n.get("message", "")) for n in notes if isinstance(n, dict)).lower()
    return BILLING_NEEDLE in text or BILLING_NEEDLE_ALT in text


def only_billing_failures(repo: str, runs: list[dict[str, Any]]) -> bool:
    if not runs:
        return False
    for run in runs:
        if run.get("status") != "completed" or run.get("conclusion") != "failure":
            return False
        run_id = run.get("id")
        if not isinstance(run_id, int) or not check_run_is_billing_failure(repo, run_id):
            return False
    return True
