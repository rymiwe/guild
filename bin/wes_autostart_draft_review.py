#!/usr/bin/env python3
"""Auto-start draft-only reviews on colleague requests under N lines (G1-G6).

Ships disabled. Hooked from work-review-requests-check.sh when --auto-start-draft
or WES_AUTO_START_DRAFT=1 is set. The PR events routine prompt is not used and
must not be edited for this gate. No new poll is added.

Nothing posts without Ryan's OK. The start path has no post capability.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path


N_LINES = 100
DAILY_CAP = 5
PACE_MULT = 1.5
RYAN_LOGIN = "rymiwe"
ENABLED_ENV = "WES_AUTO_START_DRAFT"
POST_NAMES = ("wes-post-review", "gh-pr-review-submit", "--post")

# Hard default off. Only an explicit on-flag enables starts.
ENABLED_DEFAULT = False


class PostHoldError(RuntimeError):
    """Raised if any post path is invoked from the auto-start path."""


def _lower(value) -> str:
    return str(value or "").strip().lower()


def local_today(now: datetime | None = None) -> str:
    """Calendar day in the machine's local zone (honours TZ); a naive clock is local."""
    return (now or datetime.now()).astimezone().date().isoformat()


def is_enabled(prefs: dict | None = None, *, env: dict | None = None) -> bool:
    prefs = prefs or {}
    env = env if env is not None else os.environ
    raw = env.get(ENABLED_ENV)
    if raw is not None and str(raw).strip() != "":
        return str(raw).strip() not in ("0", "false", "False", "off", "no")
    flag = prefs.get("enabled", prefs.get("live", ENABLED_DEFAULT))
    return bool(flag) and flag is not False


def _gate_paused(prefs: dict | None = None) -> bool:
    """Registry pause file: if it exists, the gate asks and does not start."""
    prefs = prefs or {}
    raw = prefs.get("gates_dir")
    root = Path(os.path.expanduser(str(raw))) if raw else Path("/home/box/agent-data/fleet/autonomy-gates.d")
    return (root / "wes-autostart-draft-review.PAUSED").exists() or (root / "wes-autostart-draft-review.paused").exists()


def _env_flag(env: dict | None, name: str) -> bool:
    raw = (env or {}).get(name)
    if raw is None or str(raw).strip() == "":
        return False
    return str(raw).strip() not in ("0", "false", "False", "off", "no")


def is_bot_login(login: str, typ: str | None = None) -> bool:
    name = _lower(login)
    kind = _lower(typ)
    if kind == "bot":
        return True
    if name.endswith("[bot]") or name.endswith("-bot"):
        return True
    if "dependabot" in name or name in {"github-actions[bot]", "github-actions"}:
        return True
    return False


def colleague_status(requester: dict | None, prefs: dict | None = None) -> dict:
    """G1: the PR author is a human work-org member (prefs org_members). Not Ryan, not a bot, not Wes.

    Unknown requester (missing login, or login not in the member list) asks.
    That the PR requests Ryan is decided upstream: work-review-requests-check.sh
    emits only PRs from its review-requested search.
    """
    prefs = prefs or {}
    ryan = _lower(prefs.get("ryan_login") or RYAN_LOGIN)
    wes = {_lower(x) for x in (prefs.get("wes_logins") or [])}
    members = prefs.get("org_members")
    req = requester or {}
    login = _lower(req.get("login") or req.get("actor") or req.get("user"))
    typ = req.get("type") or req.get("actor_type")
    if not login:
        return {"ok": False, "reason": "unknown requester"}
    if not wes:
        return {"ok": False, "reason": "prefs have no wes_logins"}
    if login == ryan:
        return {"ok": False, "reason": "requester is Ryan"}
    if login in wes:
        return {"ok": False, "reason": "requester is Wes"}
    if is_bot_login(login, typ):
        return {"ok": False, "reason": "requester is a bot"}
    if members is None:
        return {"ok": False, "reason": "unknown requester"}
    member_set = {_lower(x) for x in members}
    if login not in member_set:
        return {"ok": False, "reason": "unknown requester"}
    return {"ok": True, "reason": None, "login": login}


def changed_lines(pr: dict) -> int | None:
    if pr.get("changed_lines") is not None:
        try:
            return int(pr["changed_lines"])
        except (TypeError, ValueError):
            return None
    if "additions" not in pr or "deletions" not in pr:
        return None
    try:
        return int(pr["additions"]) + int(pr["deletions"])
    except (TypeError, ValueError):
        return None


def _effective_tier(pr: dict) -> str:
    return _lower(pr.get("effective_tier") or pr.get("tier") or "")


def _risk_label(pr: dict) -> str:
    labels = pr.get("labels") or []
    names = []
    for item in labels:
        if isinstance(item, dict):
            names.append(_lower(item.get("name") or ""))
        else:
            names.append(_lower(item))
    for name in names:
        if name.startswith("risk:"):
            return name.split(":", 1)[-1]
    return _lower(pr.get("risk_label") or "")


def _classified_repo(pr: dict, prefs: dict) -> bool:
    repo = _lower(pr.get("repo") or "")
    # The repo list lives in prefs, not here: this repo is public. No list, no start.
    names = {_lower(x) for x in (prefs.get("classified_repos") or [])}
    short = repo.split("/")[-1]
    return repo in names or short in names


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def state_paths(prefs: dict | None = None) -> dict:
    prefs = prefs or {}
    root = Path(os.path.expanduser(str(prefs.get("state_dir") or "/home/box/agent-data/fleet/state")))
    return {
        "starts": Path(prefs["starts_path"]) if prefs.get("starts_path") else root / "wes-autostart-draft-review.starts.json",
        "heads": Path(prefs["heads_path"]) if prefs.get("heads_path") else root / "wes-autostart-draft-review.heads.json",
        "audit": Path(prefs["audit_log"]) if prefs.get("audit_log") else Path("/home/box/agent-data/fleet/handoffs/wes/autonomy-audit-log.md"),
        "pings": Path(prefs["pings_path"]) if prefs.get("pings_path") else root / "wes-autostart-draft-review.pings",
    }


def _pr_key(pr: dict) -> str:
    repo = pr.get("repo") or ""
    n = pr.get("pr") or pr.get("number")
    return f"{repo}#{n}"


def _head(pr: dict) -> str:
    return str(pr.get("head") or pr.get("head_sha") or pr.get("sha") or "")


def already_started(pr: dict, prefs: dict | None = None) -> dict | None:
    paths = state_paths(prefs)
    data = _load_json(paths["heads"], {})
    rec = data.get(_pr_key(pr)) or {}
    if not rec:
        return None
    if _head(pr) and rec.get("head") == _head(pr):
        return rec
    return None


def starts_on(day: str, prefs: dict | None = None) -> list:
    data = _load_json(state_paths(prefs)["starts"], {"days": {}})
    rows = (data.get("days") or {}).get(day) or []
    return list(rows)


def pace_blocks(prefs: dict | None, day: str) -> dict | None:
    """G4: cap 5 per day (6th asks); above 1.5x expected daily pace, every start asks."""
    prefs = prefs or {}
    n = len(starts_on(day, prefs))
    if n >= int(prefs.get("daily_cap") or DAILY_CAP):
        return {"ok": False, "reason": f"daily cap {int(prefs.get('daily_cap') or DAILY_CAP)} already reached ({n} starts)"}
    expected = prefs.get("expected_daily_pace")
    try:
        expected_f = float(expected) if expected is not None else None
    except (TypeError, ValueError):
        expected_f = None
    if expected_f is not None and expected_f > 0 and n > expected_f * float(prefs.get("pace_mult") or PACE_MULT):
        return {"ok": False, "reason": f"above {prefs.get('pace_mult') or PACE_MULT}x pace ({n} starts, expected {expected_f})"}
    return None


def compose_draft_landed_message(pr: dict, draft_path: str) -> str:
    """G5: one short message with the PR link and draft location. Does not send."""
    link = pr.get("html_url") or pr.get("url") or _pr_key(pr)
    title = pr.get("title") or _pr_key(pr)
    return f"Draft review ready for {title}: {link} (draft: {draft_path})"


def record_ping(message: str, prefs: dict | None = None, *, notify_cmd=None) -> None:
    """Write the ping payload locally. Never posts to GitHub. Optional notify_cmd is for tests."""
    paths = state_paths(prefs)
    paths["pings"].parent.mkdir(parents=True, exist_ok=True)
    with paths["pings"].open("a", encoding="utf-8") as fh:
        fh.write(message.rstrip() + "\n")
    if notify_cmd:
        subprocess.run(list(notify_cmd) + [message], check=True)


def append_audit(pr: dict, draft_path: str, prefs: dict | None = None, *, now: datetime | None = None) -> None:
    paths = state_paths(prefs)
    ts = (now or datetime.now()).astimezone().strftime("%Y-%m-%d %-I:%M:%S %p %Z")
    line = f"- {ts}: auto-start draft-only review {_pr_key(pr)} head={_head(pr)[:9]} draft={draft_path}\n"
    paths["audit"].parent.mkdir(parents=True, exist_ok=True)
    if not paths["audit"].exists():
        paths["audit"].write_text("", encoding="utf-8")
    with paths["audit"].open("a", encoding="utf-8") as fh:
        fh.write(line)


def record_start(pr: dict, draft_path: str, prefs: dict | None = None, *, now: datetime | None = None) -> None:
    prefs = prefs or {}
    day = local_today(now)
    paths = state_paths(prefs)
    starts = _load_json(paths["starts"], {"days": {}})
    days = starts.setdefault("days", {})
    days.setdefault(day, []).append({"pr": _pr_key(pr), "head": _head(pr), "draft": draft_path})
    _write_json(paths["starts"], starts)
    heads = _load_json(paths["heads"], {})
    heads[_pr_key(pr)] = {"head": _head(pr), "draft": draft_path, "day": day, "stale": False}
    _write_json(paths["heads"], heads)


def mark_stale(pr: dict, prefs: dict | None = None) -> None:
    paths = state_paths(prefs)
    heads = _load_json(paths["heads"], {})
    rec = heads.get(_pr_key(pr)) or {}
    rec["stale"] = True
    rec["started_head"] = rec.get("head")
    rec["current_head"] = _head(pr)
    heads[_pr_key(pr)] = rec
    _write_json(paths["heads"], heads)


def decide(pr: dict, prefs: dict | None = None, *, now: datetime | None = None, env: dict | None = None) -> dict:
    """Decide start or ask. Never launches. Flag off always asks (gate disabled)."""
    prefs = prefs or {}
    env = env if env is not None else os.environ
    out = {"action": "ask", "reason": None, "n_lines": changed_lines(pr), "enabled": is_enabled(prefs, env=env)}
    if not out["enabled"]:
        out["reason"] = "gate disabled"
        return out
    if _gate_paused(prefs):
        out["reason"] = "gate paused"
        return out
    if pr.get("bundle") or pr.get("stack") or (pr.get("prs") and len(pr.get("prs") or []) > 1):
        out["reason"] = "stack or bundle request"
        return out
    if not pr.get("colleague_request", pr.get("review_requested", False)):
        out["reason"] = "no colleague review request"
        return out
    col = colleague_status(pr.get("requester") or pr.get("requested_by"), prefs)
    if not col.get("ok"):
        out["reason"] = col.get("reason") or "unknown requester"
        return out
    if not _classified_repo(pr, prefs):
        out["reason"] = "repo not classified"
        return out
    if pr.get("is_draft") or pr.get("draft"):
        out["reason"] = "GitHub PR is a draft"
        return out
    if not _head(pr):
        out["reason"] = "missing head sha"
        return out
    if pr.get("classifier_head") and pr.get("classifier_head") != _head(pr):
        out["reason"] = "risk label not on current head"
        return out
    if pr.get("confidence") not in (None, "full", "high"):
        out["reason"] = "classifier not full confidence"
        return out
    if pr.get("risk_stale") or pr.get("label_stale"):
        out["reason"] = "risk label stale"
        return out
    risk = _risk_label(pr)
    tier = _effective_tier(pr)
    if not risk and not tier:
        out["reason"] = "missing or unknown risk label"
        return out
    if risk in {"unknown", "stale"}:
        out["reason"] = "missing or unknown risk label"
        return out
    if risk == "high" or tier == "high":
        out["reason"] = "high-risk still asks"
        return out
    n = out["n_lines"]
    if n is None:
        out["reason"] = "changed-line count missing"
        return out
    cap = int(prefs.get("n_lines") or N_LINES)
    if n >= cap:
        out["reason"] = f"{n} lines is not under {cap}"
        return out
    prev = already_started(pr, prefs)
    if prev and not prev.get("stale"):
        out["reason"] = "already auto-started on this head"
        return out
    heads = _load_json(state_paths(prefs)["heads"], {})
    rec = heads.get(_pr_key(pr)) or {}
    if rec.get("head") and rec.get("head") != _head(pr):
        # Head moved since a prior start (or mid-run). Mark stale and ask before any restart.
        mark_stale(pr, prefs)
        out["reason"] = "head moved; draft stale"
        return out
    if pr.get("run_head") and pr.get("run_head") != _head(pr):
        mark_stale(pr, prefs)
        out["reason"] = "head moved; draft stale"
        return out
    if pr.get("seating_ok") is False:
        out["reason"] = pr.get("seating_reason") or "seating rules not met"
        return out
    blocked = pace_blocks(prefs, local_today(now))
    if blocked:
        out["reason"] = blocked["reason"]
        return out
    out["action"] = "start"
    out["reason"] = "under N, colleague request, draft-only"
    return out


def _assert_no_post(argv=None, post_cmd=None) -> None:
    args = [str(x) for x in (argv or [])]
    blob = " ".join(args)
    if post_cmd or any(name in blob for name in POST_NAMES):
        raise PostHoldError("auto-start path has no post capability")
    # Structural: wes-post-review must not be reachable from this path.
    for part in args:
        if Path(str(part)).name == "wes-post-review":
            raise PostHoldError("wes-post-review is not reachable from auto-start")


def start_draft(
    pr: dict,
    prefs: dict | None = None,
    *,
    now: datetime | None = None,
    env: dict | None = None,
    argv=None,
    post_cmd=None,
    notify_cmd=None,
    jobdir=None,
) -> dict:
    """Start a draft-only review jobdir. Posting is structurally impossible here."""
    _assert_no_post(argv, post_cmd)
    prefs = prefs or {}
    env = env if env is not None else os.environ
    decision = decide(pr, prefs, now=now, env=env)
    if decision["action"] != "start":
        return {"status": "ask", "decision": decision, "posted": False}
    root = Path(jobdir or prefs.get("jobdir_root") or (state_paths(prefs)["starts"].parent / "autostart-drafts"))
    dest = root / f"draft-{_pr_key(pr).replace('/', '-')}-{_head(pr)[:9]}"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "HOLD-NO-POST").write_text("draft-only; nothing posts without Ryan's OK\n", encoding="utf-8")
    (dest / "pr.json").write_text(json.dumps(pr, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    record_start(pr, str(dest), prefs, now=now)
    append_audit(pr, str(dest), prefs, now=now)
    message = compose_draft_landed_message(pr, str(dest))
    record_ping(message, prefs, notify_cmd=notify_cmd)
    return {
        "status": "started",
        "decision": decision,
        "draft": str(dest),
        "ping": message,
        "posted": False,
    }


def hook_lines(rows: list[dict], prefs: dict | None = None, *, env=None, now=None) -> list[dict]:
    """Called from work-review-requests-check.sh only when the flag is on."""
    env = env if env is not None else os.environ
    out = []
    dry = _env_flag(env, "WES_AUTO_START_DRY")
    for pr in rows:
        if dry:
            decision = decide(pr, prefs, env=env, now=now)
            out.append({"status": "dry", "decision": decision, "posted": False, "draft": None})
        else:
            out.append(start_draft(pr, prefs, env=env, now=now))
    return out


def _gh_json(path: str, timeout: int = 60):
    """Metadata-only gh api GET. Returns None on error. Never prints tokens."""
    try:
        result = subprocess.run(
            ["gh", "api", "-X", "GET", path],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def build_hook_row(key: str, kind: str = "NEW") -> dict | None:
    """Build one hook PR row from github metadata (no diffs, no code)."""
    key = (key or "").strip()
    if "#" not in key:
        return None
    repo, _, raw_n = key.partition("#")
    try:
        number = int(raw_n)
    except ValueError:
        return None
    if not repo:
        return None
    pull = _gh_json(f"repos/{repo}/pulls/{number}")
    if not isinstance(pull, dict):
        return None
    head = ((pull.get("head") or {}).get("sha")) or ""
    labels = []
    for item in pull.get("labels") or []:
        if isinstance(item, dict) and item.get("name"):
            labels.append(item["name"])
        elif item:
            labels.append(str(item))
    user = pull.get("user") or {}
    return {
        "repo": repo,
        "pr": number,
        "number": number,
        "head": head,
        "title": pull.get("title") or "",
        "html_url": pull.get("html_url") or pull.get("url") or "",
        "additions": pull.get("additions"),
        "deletions": pull.get("deletions"),
        "labels": labels,
        "is_draft": bool(pull.get("draft") or pull.get("is_draft")),
        "colleague_request": True,
        "requester": {"login": user.get("login"), "type": user.get("type")},
        "kind": kind,
    }


def write_hook_json(lines: list[str], dest: Path) -> list[dict]:
    """Write hook JSON from 'KIND\trepo#n' or 'repo#n' lines produced by this pass."""
    rows = []
    for raw in lines:
        raw = (raw or "").strip()
        if not raw or raw.startswith("#"):
            continue
        if "	" in raw:
            kind, key = raw.split("	", 1)
        else:
            kind, key = "NEW", raw
        row = build_hook_row(key.strip(), kind.strip() or "NEW")
        if row:
            rows.append(row)
    dest = Path(dest)
    _write_json(dest, rows)
    return rows


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    _assert_no_post(argv)
    # CLI is decide/start from a JSON fixture. Disabled unless the env flag or prefs say on.
    if not argv or argv[0] in {"-h", "--help"}:
        print("usage: wes_autostart_draft_review.py decide|start|hook|write-hook", file=sys.stderr)
        return 2
    cmd = argv[0]
    prefs = {}
    if "--prefs" in argv:
        i = argv.index("--prefs")
        prefs = json.loads(Path(argv[i + 1]).read_text(encoding="utf-8"))
    pr = {}
    if len(argv) > 1 and not argv[1].startswith("-") and cmd in {"decide", "start"}:
        pr = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    if cmd == "decide":
        print(json.dumps(decide(pr, prefs), indent=2, sort_keys=True))
        return 0
    if cmd == "start":
        print(json.dumps(start_draft(pr, prefs, argv=argv), indent=2, sort_keys=True))
        return 0
    if cmd == "write-hook":
        keys_path = Path(argv[argv.index("--keys") + 1]) if "--keys" in argv else None
        json_path = Path(argv[argv.index("--json") + 1]) if "--json" in argv else None
        if not keys_path or not json_path:
            print("usage: wes_autostart_draft_review.py write-hook --keys FILE --json FILE", file=sys.stderr)
            return 2
        lines = keys_path.read_text(encoding="utf-8").splitlines() if keys_path.is_file() else []
        rows = write_hook_json(lines, json_path)
        print(f"AUTO_START_WRITE_HOOK rows={len(rows)} path={json_path}", file=sys.stderr)
        return 0
    if cmd == "hook":
        # Invoked only from work-review-requests-check.sh when the flag is on.
        # Results go to stderr so they never mix with the check script's TSV stdout.
        json_path = None
        if "--json" in argv:
            json_path = Path(argv[argv.index("--json") + 1])
        rows = json.loads(json_path.read_text(encoding="utf-8")) if json_path and json_path.is_file() else []
        results = hook_lines(rows, prefs)
        posted = 0
        started = 0
        for result in results:
            if result.get("posted"):
                posted += 1
            if result.get("status") == "started":
                started += 1
            print(
                json.dumps(
                    {
                        "status": result.get("status"),
                        "draft": result.get("draft"),
                        "reason": (result.get("decision") or {}).get("reason"),
                        "enabled": (result.get("decision") or {}).get("enabled"),
                        "posted": result.get("posted"),
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
        enabled = is_enabled(prefs)
        dry = _env_flag(os.environ, "WES_AUTO_START_DRY")
        print(
            f"AUTO_START_HOOK enabled={str(enabled).lower()} dry={str(dry).lower()} rows={len(rows)} started={started} posted={posted}",
            file=sys.stderr,
        )
        return 0
    print("unknown command", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
