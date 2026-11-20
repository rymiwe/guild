#!/usr/bin/env python3
"""Tests for work-review-requests-check.sh (review-request intake).
Usage: run_tests.py [SCRIPT]   (default: ../work-review-requests-check.sh). Fake gh only; no network.
Prints PASS/FAIL per case and a summary line; exit 1 when any case fails."""
import atexit
import shutil
import json, os, subprocess, sys, tempfile, pathlib
HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else HERE.parent.parent / "bin" / "work-review-requests-check.sh").resolve()
SK, QU = "work-org/app", "work-org/api"
A = "a" * 40; B = "b" * 40
results = []

def item(n, author="cole", title="t"):
    return {"number": n, "title": title, "user": {"login": author}, "html_url": f"https://github.com/x/{n}"}

def pull(n, head, author="cole"):
    return {"number": n, "state": "open", "draft": False, "merged": False, "user": {"login": author, "type": "User"},
            "head": {"sha": head}, "base": {"ref": "main"}}

class Env:
    def __init__(self):
        self.d = pathlib.Path(tempfile.mkdtemp(prefix="wrrc-")); atexit.register(shutil.rmtree, str(self.d), True)
        (self.d / "bin").mkdir(); os.symlink(HERE / "fake-gh", self.d / "bin" / "gh")
        os.symlink(HERE / "fake-guild-config", self.d / "bin" / "guild-config")
        self.org = {"review_requests": {"repos": [SK, QU]}}
        self.seen = self.d / "state" / "work-review-requests.seen"
    def run(self, fixture, *extra):
        fxp = self.d / "fixture.json"; fxp.write_text(json.dumps(fixture))
        env = dict(os.environ, PATH=f"{self.d/'bin'}:{os.environ['PATH']}", FAKE_GH_FIXTURE=str(fxp),
                   FAKE_GH_LOG=str(self.d / "gh.log"), FAKE_GUILD_CONFIG=json.dumps(self.org))
        args = [str(SCRIPT), "--seen", str(self.seen)] + list(extra)
        p = subprocess.run(args, env=env, capture_output=True, text=True, timeout=60)
        return p.returncode, p.stdout, p.stderr

def case(name, fn):
    try:
        fn(); results.append((name, True, "")); print(f"PASS: {name}")
    except AssertionError as e:
        results.append((name, False, str(e))); print(f"FAIL: {name}: {e}")
    except Exception as e:
        results.append((name, False, repr(e))); print(f"FAIL: {name}: {type(e).__name__}: {e}")

# ---------- intake (the 1102 proof's three manual checks, now encoded) ----------
def legacy_fx():
    fixture = {"search": {}, "api": {}}
    fixture["search"][f"{SK}|requested"] = [item(1, title="one"), item(2, title="two")]
    fixture["api"][f"repos/{SK}/pulls/1"] = pull(1, A); fixture["api"][f"repos/{SK}/pulls/2"] = pull(2, B)
    return fixture

def t_legacy_new():
    e = Env(); rc, out, err = e.run(legacy_fx())
    assert rc == 0, f"rc={rc} err={err!r}"
    assert out == f"{SK}#1\tone\tcole\thttps://github.com/x/1\t{A[:9]}\n{SK}#2\ttwo\tcole\thttps://github.com/x/2\t{B[:9]}\n", repr(out)
def t_legacy_seeded():
    e = Env(); e.run(legacy_fx()); rc, out, err = e.run(legacy_fx())
    assert rc == 10 and out == "", f"rc={rc} out={out!r}"
def t_legacy_one_removed():
    e = Env(); e.run(legacy_fx())
    e.seen.write_text(f"{SK}#1\n"); rc, out, _ = e.run(legacy_fx())
    assert rc == 0 and out.startswith(f"{SK}#2\t") and out.count("\n") == 1, f"rc={rc} out={out!r}"
def t_legacy_gh_error():
    e = Env(); fixture = legacy_fx(); fixture["fail"] = ["repo:" + SK]; rc, out, _ = e.run(fixture)
    assert rc == 3, f"rc={rc}"
def t_dry_run_writes_no_seen():
    e = Env(); rc, out, _ = e.run(legacy_fx(), "--dry-run")
    assert rc == 0 and out.count("\n") == 2 and e.seen.read_text() == "", f"rc={rc} out={out!r}"
def t_repo_config_fails_closed():
    # the repo list lives in guild-config; missing or bad, nothing is searched
    for org in ({}, {"review_requests": {"repos": "a/b"}}, {"review_requests": {"repos": []}}):
        e = Env(); e.org = org; rc, out, err = e.run(legacy_fx())
        assert rc == 3 and out == "" and not (e.d / "gh.log").exists(), f"{org}: rc={rc} out={out!r} err={err!r}"
def t_retired_pr_updates_flag_fails_loudly():
    # the PR-updates path was retired; a caller still passing it must not think updates were checked
    e = Env(); rc, out, err = e.run(legacy_fx(), "--pr-updates")
    assert rc == 2 and out == "" and "--pr-updates" in err and not (e.d / "gh.log").exists(), f"rc={rc} out={out!r} err={err!r}"

for n, f in list(globals().items()):
    if n.startswith("t_") and callable(f): case(n[2:], f)
p = sum(1 for _, ok, _ in results if ok); fl = len(results) - p
print(f"SUMMARY: {p} passed, {fl} failed ({SCRIPT.name})")
sys.exit(1 if fl else 0)
