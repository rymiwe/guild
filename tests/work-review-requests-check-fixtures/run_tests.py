#!/usr/bin/env python3
"""Tests for work-review-requests-check.sh (PR-update re-review, Paul verdict 2026-11-09 U1/U2).
Usage: run_tests.py [SCRIPT]   (default: ../work-review-requests-check.sh). Fake gh only; no network.
Prints PASS/FAIL per case and a summary line; exit 1 when any case fails."""
import hashlib, json, os, subprocess, sys, tempfile, time, pathlib
HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else HERE.parent.parent / "bin" / "work-review-requests-check.sh").resolve()
SK, QU, SM = "work-org/app", "work-org/api", "work-org/web-store"
NOW = 1794300000
MIRROR_TOOL = SCRIPT.parent / "wes-starts-mirror"
A = "a" * 40; B = "b" * 40; C = "c" * 40
results = []

def item(n, author="cole", title="t"):
    return {"number": n, "title": title, "user": {"login": author}, "html_url": f"https://github.com/x/{n}"}

def pull(n, head, state="open", draft=False, author="cole", merged=False):
    return {"number": n, "state": state, "draft": draft, "merged": merged, "user": {"login": author, "type": "User"},
            "head": {"sha": head}, "base": {"ref": "main"}}

def review(rid, sha, ts, state="APPROVED", user="rymiwe"):
    return {"id": rid, "user": {"login": user}, "commit_id": sha, "submitted_at": ts, "state": state}

def ci(head, runs=(("completed", "success"),), statuses=(), repo=SK):
    """runs: (status, conclusion[, name]); statuses: (state[, context])."""
    return {f"repos/{repo}/commits/{head}/check-runs": {"total_count": len(runs), "check_runs": [{"status": r[0], "conclusion": r[1], "name": r[2] if len(r) > 2 else f"job{i}"} for i, r in enumerate(runs)]},
            f"repos/{repo}/commits/{head}/status": {"state": "success", "statuses": [{"state": t[0], "context": t[1] if len(t) > 1 else f"ctx{i}"} for i, t in enumerate(statuses)]}}

def base_fx():
    return {"search": {}, "api": {}}

def add_pr(fixture, n, head, reviews=(), runs=(("completed", "success"),), requested=False, reviewed=True, repo=SK, statuses=(), **kw):
    if requested: fixture["search"].setdefault(f"{repo}|requested", []).append(item(n, kw.get("author", "cole")))
    if reviewed: fixture["search"].setdefault(f"{repo}|reviewed", []).append(item(n, kw.get("author", "cole")))
    fixture["api"][f"repos/{repo}/pulls/{n}"] = pull(n, head, **kw)
    fixture["api"][f"repos/{repo}/pulls/{n}/reviews"] = list(reviews)
    fixture["api"].update(ci(head, runs, statuses, repo))

def mirror_text(lines, ts=NOW):
    """The mirror format wes-starts-mirror writes: one header line, then '<job> [head=<h>]' lines."""
    body = "".join(l + "\n" for l in lines)
    return f"# wes-starts-mirror v1 ts={ts} sha256={hashlib.sha256(body.encode()).hexdigest()} lines={len(lines)}\n" + body

class Env:
    def __init__(self):
        self.d = pathlib.Path(tempfile.mkdtemp(prefix="wrrc-"))
        (self.d / "bin").mkdir(); os.symlink(HERE / "fake-gh", self.d / "bin" / "gh")
        os.symlink(HERE / "fake-guild-config", self.d / "bin" / "guild-config")
        self.org = {"review_requests": {"repos": [SK, QU], "update_repos": [SM]}}
        self.seen = self.d / "state" / "work-review-requests.seen"
        self.jobs = []            # mirror body lines: '<job name> [head=<h>]'
        self.mirror = self.d / "state" / "wes-starts.mirror.log"
        self.mirror_raw = None    # override the mirror file text (None: build a fresh valid mirror)
        self.now = NOW
        self.oracle = None
        self.runs_log = self.d / "routine-runs.log"; self.runs_log.write_text("")
    def ping_line(self, key, minutes_ago, status="new-head", action="RUN"):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        t = datetime.fromtimestamp(self.now - minutes_ago * 60, ZoneInfo("America/Los_Angeles")).isoformat(timespec="seconds")
        with open(self.runs_log, "a") as f:
            f.write(f"{t}\tWork Wes\tSurface job finished (ping)\tok\tno\t0\tno\tpr-change {key} {status} action={action}\n")
    def missed(self):
        f = self.d / "state" / "work-review-requests.poller-missed.log"
        return f.read_text().splitlines() if f.exists() else []
    def run(self, fixture, *extra, pr_updates=True):
        fxp = self.d / "fixture.json"; fxp.write_text(json.dumps(fixture))
        self.mirror.parent.mkdir(parents=True, exist_ok=True)
        if self.mirror_raw is False:
            self.mirror.unlink(missing_ok=True)
        else:
            self.mirror.write_text(self.mirror_raw if self.mirror_raw is not None else mirror_text(self.jobs, self.now))
        env = dict(os.environ, PATH=f"{self.d/'bin'}:{os.environ['PATH']}", FAKE_GH_FIXTURE=str(fxp),
                   FAKE_GH_LOG=str(self.d / "gh.log"), WRRC_JOBS=str(self.mirror), WRRC_NOW=str(self.now),
                   WRRC_RUNS_LOG=str(self.runs_log), FAKE_GUILD_CONFIG=json.dumps(self.org))
        if self.oracle: env["WRRC_PATCHID_ORACLE"] = self.oracle
        args = [str(SCRIPT), "--seen", str(self.seen)] + (["--pr-updates"] if pr_updates else []) + list(extra)
        p = subprocess.run(args, env=env, capture_output=True, text=True, timeout=60)
        return p.returncode, p.stdout, p.stderr
    def state(self, suffix):
        f = self.d / "state" / f"work-review-requests.{suffix}"
        return f.read_text() if f.exists() else ""

def case(name, fn):
    try:
        fn(); results.append((name, True, "")); print(f"PASS: {name}")
    except AssertionError as e:
        results.append((name, False, str(e))); print(f"FAIL: {name}: {e}")
    except Exception as e:
        results.append((name, False, repr(e))); print(f"FAIL: {name}: {type(e).__name__}: {e}")

def lines(out, kind):
    return [l for l in out.splitlines() if l.split("\t")[0] == kind]

# ---------- legacy regression (the 1102 proof's three manual checks, now encoded) ----------
def legacy_fx():
    fixture = base_fx()
    fixture["search"][f"{SK}|requested"] = [item(1, title="one"), item(2, title="two")]
    fixture["api"][f"repos/{SK}/pulls/1"] = pull(1, A); fixture["api"][f"repos/{SK}/pulls/2"] = pull(2, B)
    return fixture

def t_legacy_new():
    e = Env(); rc, out, err = e.run(legacy_fx(), pr_updates=False)
    assert rc == 0, f"rc={rc} err={err!r}"
    assert out == f"{SK}#1\tone\tcole\thttps://github.com/x/1\t{A[:9]}\n{SK}#2\ttwo\tcole\thttps://github.com/x/2\t{B[:9]}\n", repr(out)
def t_legacy_seeded():
    e = Env(); e.run(legacy_fx(), pr_updates=False); rc, out, err = e.run(legacy_fx(), pr_updates=False)
    assert rc == 10 and out == "", f"rc={rc} out={out!r}"
def t_legacy_one_removed():
    e = Env(); e.run(legacy_fx(), pr_updates=False)
    e.seen.write_text(f"{SK}#1\n"); rc, out, _ = e.run(legacy_fx(), pr_updates=False)
    assert rc == 0 and out.startswith(f"{SK}#2\t") and out.count("\n") == 1, f"rc={rc} out={out!r}"
def t_legacy_gh_error():
    e = Env(); fixture = legacy_fx(); fixture["fail"] = ["repo:" + SK]; rc, out, _ = e.run(fixture, pr_updates=False)
    assert rc == 3, f"rc={rc}"
def t_repo_config_fails_closed():
    # the repo list lives in guild-config; missing or bad, nothing is searched
    for org, pr_updates in (({}, False), ({"review_requests": {"repos": "a/b"}}, False),
                            ({"review_requests": {"repos": [SK]}}, True)):   # no update_repos with the flag on
        e = Env(); e.org = org; rc, out, err = e.run(legacy_fx(), pr_updates=pr_updates)
        assert rc == 3 and out == "" and not (e.d / "gh.log").exists(), f"{org}: rc={rc} out={out!r} err={err!r}"
def t_flag_off_no_new_kinds():
    # default (flag off) prints legacy lines only and writes no new state files, even with an updated PR present
    e = Env(); fixture = legacy_fx(); add_pr(fixture, 200, B, reviews=[review(11, A, "2026-11-09T10:00:00Z")])
    rc, out, _ = e.run(fixture, pr_updates=False)
    assert not lines(out, "UPDATED") and not lines(out, "READY"), repr(out)
    assert e.state("updated") == "" and e.state("drafts") == "", "new state written with flag off"
    assert "reviewed-by:" not in (e.d / "gh.log").read_text(), "flag off still queried reviewed PRs"

# ---------- READY ----------
def t_ready():
    e = Env(); fixture = base_fx()
    fixture["search"][f"{SK}|drafts"] = [item(100)]; fixture["api"][f"repos/{SK}/pulls/100"] = pull(100, A, draft=True)
    rc, out, _ = e.run(fixture); assert rc == 10 and out == "", f"draft run: rc={rc} out={out!r}"
    assert f"{SK}#100" in e.state("drafts"), "draft not recorded"
    fixture = base_fx(); fixture["search"][f"{SK}|requested"] = [item(100)]; fixture["api"][f"repos/{SK}/pulls/100"] = pull(100, A)
    fixture["api"][f"repos/{SK}/pulls/100/reviews"] = []
    rc, out, _ = e.run(fixture)
    assert rc == 0, f"rc={rc}"
    assert lines(out, "READY") == [f"READY\t{SK}#100\t{A[:9]}\tdraft=false"], repr(out)
    assert not [l for l in out.splitlines() if l.startswith(f"{SK}#100\t")], "legacy line also printed (double start)"
    rc, out, err = e.run(fixture); assert rc == 10 and out == "", f"READY refired: rc={rc} out={out!r} err={err!r}"

# ---------- UPDATED ----------
def t_updated():
    e = Env(); fixture = base_fx(); add_pr(fixture, 200, B, reviews=[review(11, A, "2026-11-09T10:00:00Z")])
    rc, out, err = e.run(fixture)
    assert rc == 0, f"rc={rc} err={err!r}"
    assert lines(out, "UPDATED") == [f"UPDATED\t{SK}#200\t{B[:9]}\tprev={A[:9]}\tci=green\troute=review\treview=11"], repr(out)
    assert f"{SK}#200\t{B}\tfired" in e.state("updated"), repr(e.state("updated"))

def t_updated_vs_job_head():
    # our review job is newer than Ryan's review and Ryan is still requested: baseline is the job head
    e = Env(); e.jobs = ["review-app-201 head=" + A[:9]]
    fixture = base_fx(); add_pr(fixture, 201, B, reviews=[review(12, C, "2026-11-08T10:00:00Z")], requested=True)
    e.seen.parent.mkdir(parents=True, exist_ok=True); e.seen.write_text(f"{SK}#201\n")   # request already seen; then a push
    rc, out, _ = e.run(fixture)
    u = lines(out, "UPDATED"); assert len(u) == 1 and f"prev={A[:9]}" in u[0], repr(out)

def t_no_change():
    control(202)
    e = Env(); fixture = base_fx(); add_pr(fixture, 202, A, reviews=[review(13, A, "2026-11-09T10:00:00Z")])
    rc, out, _ = e.run(fixture); assert rc == 10 and out == "", f"rc={rc} out={out!r}"

def t_same_head_no_refire():
    e = Env(); fixture = base_fx(); add_pr(fixture, 200, B, reviews=[review(11, A, "2026-11-09T10:00:00Z")])
    rc1, out1, _ = e.run(fixture); rc2, out2, _ = e.run(fixture)
    assert rc1 == 0 and lines(out1, "UPDATED"), f"first run did not fire: {out1!r}"
    assert rc2 == 10 and out2 == "", f"same head refired: rc={rc2} out={out2!r}"
    fx2 = base_fx(); add_pr(fx2, 200, C, reviews=[review(11, A, "2026-11-09T10:00:00Z")])
    rc3, out3, _ = e.run(fx2); assert rc3 == 0 and lines(out3, "UPDATED"), f"new head did not fire: {out3!r}"

def t_restack_replay():
    e = Env(); orc = e.d / "oracle"; orc.write_text('#!/bin/sh\n[ "$2" = 203 ] && { echo "match 150,151"; exit 0; }\necho nomatch; exit 1\n')
    orc.chmod(0o755); e.oracle = str(orc)
    fixture = base_fx(); add_pr(fixture, 203, B, reviews=[review(14, A, "2026-11-09T10:00:00Z")]); add_pr(fixture, 204, B, reviews=[review(15, A, "2026-11-09T10:00:00Z")])
    rc, out, _ = e.run(fixture)
    u = {l.split("\t")[1]: l for l in lines(out, "UPDATED")}
    assert u.get(f"{SK}#203", "").endswith("\troute=replay\tsrc=150,151\treview=14"), repr(out)
    assert "\troute=review\t" in u.get(f"{SK}#204", ""), repr(out)

def t_restack_oracle_error_is_review():
    e = Env(); orc = e.d / "oracle"; orc.write_text("#!/bin/sh\necho 'match'; exit 2\n"); orc.chmod(0o755); e.oracle = str(orc)
    fixture = base_fx(); add_pr(fixture, 205, B, reviews=[review(16, A, "2026-11-09T10:00:00Z")])
    rc, out, _ = e.run(fixture); u = lines(out, "UPDATED")
    assert len(u) == 1 and "\troute=review\t" in u[0], repr(out)

# ---------- U1 ----------
def t_u1_running():
    e = Env(); fixture = base_fx(); add_pr(fixture, 300, B, reviews=[review(21, A, "2026-11-09T10:00:00Z")], runs=(("in_progress", None), ("completed", "success")))
    rc, out, _ = e.run(fixture); assert rc == 10 and out == "", f"running CI fired: {out!r}"
    assert f"{SK}#300" not in e.state("updated"), "running CI was marked seen"
    fixture = base_fx(); add_pr(fixture, 300, B, reviews=[review(21, A, "2026-11-09T10:00:00Z")])
    rc, out, _ = e.run(fixture); assert rc == 0 and lines(out, "UPDATED"), f"green after running did not fire: {out!r}"
def t_u1_pending_status():
    control(301)
    e = Env(); fixture = base_fx(); add_pr(fixture, 301, B, reviews=[review(22, A, "2026-11-09T10:00:00Z")])
    fixture["api"][f"repos/{SK}/commits/{B}/status"] = {"state": "pending", "statuses": [{"state": "pending"}]}
    rc, out, _ = e.run(fixture); assert rc == 10 and out == "" and f"{SK}#301" not in e.state("updated"), f"pending status: {out!r}"
def t_u1_no_ci_yet():
    control(302)
    e = Env(); fixture = base_fx(); add_pr(fixture, 302, B, reviews=[review(23, A, "2026-11-09T10:00:00Z")], runs=())
    rc, out, _ = e.run(fixture); assert rc == 10 and out == "" and f"{SK}#302" not in e.state("updated"), f"no CI: {out!r}"
def t_u1_red():
    e = Env(); fixture = base_fx(); add_pr(fixture, 303, B, reviews=[review(24, A, "2026-11-09T10:00:00Z")], runs=(("completed", "failure"), ("completed", "success")))
    rc, out, _ = e.run(fixture); assert rc == 10 and out == "", f"red CI fired: {out!r}"
    assert f"{SK}#303\t{B}\tci=red" in e.state("updated"), repr(e.state("updated"))
    e.now += 3600; rc, out, _ = e.run(fixture); assert rc == 10 and out == "", f"still-red head fired: {out!r}"
def t_u1_green():
    e = Env(); fixture = base_fx(); add_pr(fixture, 304, B, reviews=[review(25, A, "2026-11-09T10:00:00Z")], runs=(("completed", "success"), ("completed", "skipped"), ("completed", "neutral")))
    rc, out, _ = e.run(fixture); u = lines(out, "UPDATED"); assert rc == 0 and len(u) == 1 and "\tci=green\t" in u[0], repr(out)

# ---------- U2 ----------
def control(n, **kw):
    """Positive control: the same PR without the exclusion fires UPDATED (so each exclusion test fails first)."""
    e = Env(); e.seen.parent.mkdir(parents=True, exist_ok=True); e.seen.write_text(f"{SK}#{n}\n"); fixture = base_fx(); add_pr(fixture, n, B, reviews=[review(30 + n, A, "2026-11-09T10:00:00Z")], **kw)
    rc, out, _ = e.run(fixture)
    assert rc == 0 and len(lines(out, "UPDATED")) == 1, f"control (no exclusion) did not fire: rc={rc} out={out!r}"
def u2(n, **kw):
    control(n)
    e = Env(); fixture = base_fx(); add_pr(fixture, n, B, reviews=[review(30 + n, A, "2026-11-09T10:00:00Z")], **kw)
    rc, out, _ = e.run(fixture); return rc, out, e
def t_u2_closed():
    rc, out, e = u2(400, state="closed"); assert rc == 10 and out == "", repr(out)
def t_u2_merged():
    rc, out, e = u2(401, state="closed", merged=True); assert rc == 10 and out == "", repr(out)
def t_u2_draft():
    rc, out, e = u2(402, draft=True); assert rc == 10 and out == "", repr(out)
def t_u2_own():
    rc, out, e = u2(403, author="rymiwe"); assert rc == 10 and out == "", repr(out)
def t_u2_dependabot():
    rc, out, e = u2(404, author="dependabot[bot]"); assert rc == 10 and out == "", repr(out)
def t_u2_newer_job_not_requested():
    control(405)
    e = Env(); e.jobs = ["review-app-405 head=" + C[:9]]
    fixture = base_fx(); add_pr(fixture, 405, B, reviews=[review(35, A, "2026-11-09T10:00:00Z")], requested=False)
    rc, out, _ = e.run(fixture); assert rc == 10 and out == "", repr(out)
def t_u2_requested_no_ryan_review_no_job():
    # a plain new request (no baseline) stays on the legacy NEW path, never UPDATED
    e = Env(); fixture = base_fx(); add_pr(fixture, 406, B, reviews=[], requested=True, reviewed=False)
    rc, out, _ = e.run(fixture); assert not lines(out, "UPDATED") and out.startswith(f"{SK}#406\t"), repr(out)
    control(406, requested=True)

# ---------- containment + dry run ----------
def t_dry_run_writes_nothing():
    e = Env(); fixture = base_fx(); add_pr(fixture, 500, B, reviews=[review(50, A, "2026-11-09T10:00:00Z")])
    rc, out, _ = e.run(fixture, "--dry-run"); assert rc == 0 and lines(out, "UPDATED"), repr(out)
    assert e.state("updated") == "" and e.state("drafts") == "", "dry run wrote state"
def t_metadata_only():
    e = Env(); fixture = base_fx(); add_pr(fixture, 501, B, reviews=[review(51, A, "2026-11-09T10:00:00Z")]); rc, out, _ = e.run(fixture)
    assert lines(out, "UPDATED"), f"control did not fire: {out!r}"
    calls = (e.d / "gh.log").read_text()
    bad = [c for c in calls.splitlines() if any(x in c for x in ("/files", "/compare", "/contents", ".diff", "/commits?", "pulls/501/commits", "-X POST", "-X PATCH", "-X PUT", "-X DELETE"))]
    assert not bad, f"non-metadata gh calls: {bad}"

# ---------- U1b (Paul re-verify 17:40): red stays re-checkable 24 h, fires once on green ----------
def t_u1b_red_then_green_fires_once():
    e = Env(); fixture = base_fx(); add_pr(fixture, 600, B, reviews=[review(60, A, "2026-11-09T10:00:00Z")], runs=(("completed", "failure"),))
    rc, out, _ = e.run(fixture); assert rc == 10 and out == "", f"red fired: {out!r}"
    assert f"{SK}#600\t{B}\tci=red" in e.state("updated"), repr(e.state("updated"))
    fixture = base_fx(); add_pr(fixture, 600, B, reviews=[review(60, A, "2026-11-09T10:00:00Z")])
    e.now += 3600; rc, out, _ = e.run(fixture)
    assert rc == 0 and len(lines(out, "UPDATED")) == 1, f"green after red did not fire: rc={rc} out={out!r}"
    e.now += 3600; rc, out, _ = e.run(fixture); assert rc == 10 and out == "", f"fired twice: {out!r}"
def t_u1b_cancelled_only_pending():
    control(601)
    e = Env(); fixture = base_fx(); add_pr(fixture, 601, B, reviews=[review(61, A, "2026-11-09T10:00:00Z")], runs=(("completed", "cancelled"), ("completed", "success")))
    rc, out, _ = e.run(fixture); assert rc == 10 and out == "", f"cancelled fired: {out!r}"
    assert f"{SK}#601" not in e.state("updated"), f"cancelled-only was marked (red or seen): {e.state('updated')!r}"
def t_u1b_stale_only_pending():
    control(602)
    e = Env(); fixture = base_fx(); add_pr(fixture, 602, B, reviews=[review(62, A, "2026-11-09T10:00:00Z")], runs=(("completed", "stale"),))
    rc, out, _ = e.run(fixture); assert rc == 10 and out == "" and f"{SK}#602" not in e.state("updated"), f"stale: {out!r} {e.state('updated')!r}"
def t_u1b_red_past_24h_quiet():
    e = Env(); fixture = base_fx(); add_pr(fixture, 603, B, reviews=[review(63, A, "2026-11-09T10:00:00Z")], runs=(("completed", "failure"),))
    e.run(fixture)
    fixture = base_fx(); add_pr(fixture, 603, B, reviews=[review(63, A, "2026-11-09T10:00:00Z")])
    e.now += 25 * 3600; rc, out, _ = e.run(fixture); assert rc == 10 and out == "", f"fired after 24 h: {out!r}"
    c = Env(); c.run(base_fx() | {"search": {}, "api": {}}); cfx = base_fx(); add_pr(cfx, 603, B, reviews=[review(63, A, "2026-11-09T10:00:00Z")], runs=(("completed", "failure"),))
    c.run(cfx); c.now += 23 * 3600; rc, out, _ = c.run(fixture); assert rc == 0 and lines(out, "UPDATED"), f"control: green at 23 h did not fire: {out!r}"
def t_u1b_required_checks_only():
    # branch protection readable: a red non-required check does not block; a red required one does
    e = Env(); fixture = base_fx(); add_pr(fixture, 604, B, reviews=[review(64, A, "2026-11-09T10:00:00Z")], runs=(("completed", "success", "ci/test"), ("completed", "failure", "optional-lint")))
    fixture["api"][f"repos/{SK}/branches/main/protection/required_status_checks"] = {"contexts": ["ci/test"], "checks": [{"context": "ci/test"}]}
    rc, out, _ = e.run(fixture); assert rc == 0 and len(lines(out, "UPDATED")) == 1, f"non-required red blocked: {out!r}"
    e2 = Env(); fx2 = base_fx(); add_pr(fx2, 605, B, reviews=[review(65, A, "2026-11-09T10:00:00Z")], runs=(("completed", "failure", "ci/test"), ("completed", "success", "optional-lint")))
    fx2["api"][f"repos/{SK}/branches/main/protection/required_status_checks"] = {"contexts": ["ci/test"], "checks": [{"context": "ci/test"}]}
    rc, out, _ = e2.run(fx2); assert rc == 10 and out == "" and "ci=red" in e2.state("updated"), f"required red: {out!r}"
def t_u1b_required_missing_is_pending():
    e = Env(); fixture = base_fx(); add_pr(fixture, 606, B, reviews=[review(66, A, "2026-11-09T10:00:00Z")], runs=(("completed", "success", "optional-lint"),))
    fixture["api"][f"repos/{SK}/branches/main/protection/required_status_checks"] = {"contexts": ["ci/test"], "checks": []}
    rc, out, _ = e.run(fixture); assert rc == 10 and out == "" and f"{SK}#606" not in e.state("updated"), f"missing required: {out!r}"

# ---------- ledger mirror (Paul re-verify point 1) ----------
def upd_fx(n=700):
    fixture = base_fx(); add_pr(fixture, n, B, reviews=[review(70, A, "2026-11-09T10:00:00Z")]); return fixture
def t_ledger_copy_missing_starts_nothing():
    e = Env(); rc, out, _ = e.run(upd_fx()); assert lines(out, "UPDATED"), f"control did not fire: {out!r}"
    e = Env(); e.mirror_raw = False; rc, out, err = e.run(upd_fx())
    assert rc == 4 and not lines(out, "UPDATED") and lines(out, "MIRROR_FAIL"), f"rc={rc} out={out!r} err={err!r}"
    assert e.state("updated") == "", "state written on mirror failure"
def t_ledger_copy_sha_mismatch_starts_nothing():
    e = Env(); good = mirror_text(["review-app-700 head=" + A[:9]]); e.mirror_raw = good.replace(A[:9], C[:9])
    rc, out, _ = e.run(upd_fx()); assert rc == 4 and not lines(out, "UPDATED") and lines(out, "MIRROR_FAIL"), f"rc={rc} out={out!r}"
def t_ledger_copy_stale_starts_nothing():
    e = Env(); e.mirror_raw = mirror_text([], ts=NOW - 3 * 3600)
    rc, out, _ = e.run(upd_fx()); assert rc == 4 and not lines(out, "UPDATED") and lines(out, "MIRROR_FAIL"), f"rc={rc} out={out!r}"
def t_ledger_copy_fail_keeps_legacy():
    e = Env(); e.mirror_raw = False; fixture = upd_fx(); fixture["search"][f"{SK}|requested"] = [item(701, title="x")]
    fixture["api"][f"repos/{SK}/pulls/701"] = pull(701, A); fixture["api"][f"repos/{SK}/pulls/701/reviews"] = []
    rc, out, _ = e.run(fixture); assert rc == 4 and out.startswith(f"{SK}#701\t") and lines(out, "MIRROR_FAIL"), f"rc={rc} out={out!r}"
def mirror_tool(src_text, now=NOW):
    d = pathlib.Path(tempfile.mkdtemp(prefix="wsm-")); src = d / "extract.txt"; src.write_text(src_text)
    out = d / "state" / "wes-starts.mirror.log"
    p = subprocess.run([str(MIRROR_TOOL), "--from", str(src), "--out", str(out)], capture_output=True, text=True,
                       env=dict(os.environ, WRRC_NOW=str(now)), timeout=30)
    return p, out, d / "state" / "wes-starts.mirror.shalog"
def t_mirror_tool_keeps_name_head_logs_sha():
    p, out, shalog = mirror_tool("review-app-3814 head=7c2c79f4f\nreview-api-5866\n# end lines=2\n")
    assert p.returncode == 0, f"rc={p.returncode} err={p.stderr!r}"
    body = ["review-app-3814 head=7c2c79f4f", "review-api-5866"]
    assert out.read_text() == mirror_text(body), repr(out.read_text())
    sha = hashlib.sha256("".join(l + "\n" for l in body).encode()).hexdigest()
    assert f"sha256={sha}" in shalog.read_text() and " ok" in shalog.read_text(), repr(shalog.read_text())
def t_mirror_tool_rejects_raw_ledger_fields():
    p, out, _ = mirror_tool("2026-11-09 1794273333 review-app-3814 claude poteto=on dir=x head=7c2c79f4f\n# end lines=1\n")
    assert p.returncode == 4 and not out.exists(), f"raw ledger line accepted: rc={p.returncode} {out.read_text() if out.exists() else ''!r}"
def t_mirror_tool_truncated_fails_closed():
    p, out, shalog = mirror_tool("review-app-3814 head=7c2c79f4f\n")   # no end sentinel
    assert p.returncode == 4 and not out.exists(), f"rc={p.returncode}"
    assert " fail " in shalog.read_text(), repr(shalog.read_text())
    p2, out2, _ = mirror_tool("review-app-1 head=aaaaaaaaa\n# end lines=3\n")   # count mismatch
    assert p2.returncode == 4 and not out2.exists(), f"count mismatch rc={p2.returncode}"

# ---------- the update-only repo in the same repo list ----------
def t_update_repo_same_rules():
    e = Env(); e.jobs = ["review-web-store-122 head=" + A[:9]]
    D = "d" * 40; E = "e" * 40
    fixture = base_fx(); add_pr(fixture, 122, B, repo=SM, reviews=[review(80, A, "2026-11-09T10:00:00Z")])
    add_pr(fixture, 123, D, repo=SM, reviews=[review(81, A, "2026-11-09T10:00:00Z")], runs=(("in_progress", None),))     # U1 running
    add_pr(fixture, 124, E, repo=SM, reviews=[review(82, A, "2026-11-09T10:00:00Z")], author="dependabot[bot]")        # U2 Dependabot
    rc, out, _ = e.run(fixture)
    u = lines(out, "UPDATED")
    assert u == [f"UPDATED\t{SM}#122\t{B[:9]}\tprev={A[:9]}\tci=green\troute=review\treview=80"], repr(out)
    assert f"{SM}#123" not in e.state("updated"), "running CI marked"
def t_new_request_not_doubled_by_updated():
    # a fresh legacy request line already starts a review at the current head: no UPDATED for the same PR
    e = Env(); fixture = base_fx(); add_pr(fixture, 710, B, reviews=[review(71, A, "2026-11-09T10:00:00Z")], requested=True)
    rc, out, _ = e.run(fixture)
    assert rc == 0 and out.startswith(f"{SK}#710\t") and not lines(out, "UPDATED"), repr(out)
    assert f"{SK}#710\t{B}\tfired" in e.state("updated"), repr(e.state("updated"))
    rc, out, _ = e.run(fixture); assert rc == 10 and out == "", f"refired: {out!r}"

# ---------- Q3 (Paul poller verdict): hourly backstop logs changes the poller did not ping ----------
MON10 = 1794247200   # 2026-11-09 10:00 PT, inside the poller window
def q3_env():
    e = Env(); e.now = MON10; return e
def q3_control(n):
    """Positive control: the same UPDATED PR at Mon 10:00 PT with no ping logs one miss."""
    e = q3_env(); fixture = base_fx(); add_pr(fixture, n, B, reviews=[review(n, A, "2026-11-09T10:00:00Z")]); e.run(fixture)
    assert len(e.missed()) == 1, f"control: no miss logged for #{n}: {e.missed()!r}"
def t_q3_missed_logged():
    e = q3_env(); fixture = base_fx(); add_pr(fixture, 800, B, reviews=[review(90, A, "2026-11-09T10:00:00Z")])
    rc, out, err = e.run(fixture); assert rc == 0 and lines(out, "UPDATED"), repr(out)
    m = e.missed(); assert len(m) == 1 and m[0].split("\t")[1:] == ["poller-missed", f"{SK}#800", "UPDATED"], repr(m)
    assert f"poller-missed {SK}#800 UPDATED" in err, repr(err)
def t_q3_pinged_recently_not_missed():
    q3_control(801); e = q3_env(); e.ping_line(f"{SK}#801", 30, action="SKIP-COALESCE"); fixture = base_fx(); add_pr(fixture, 801, B, reviews=[review(91, A, "2026-11-09T10:00:00Z")])
    rc, out, _ = e.run(fixture); assert lines(out, "UPDATED") and e.missed() == [], repr(e.missed())
def t_q3_old_ping_is_missed():
    e = q3_env(); e.ping_line(f"{SK}#802", 80); fixture = base_fx(); add_pr(fixture, 802, B, reviews=[review(92, A, "2026-11-09T10:00:00Z")])
    e.run(fixture); assert len(e.missed()) == 1, repr(e.missed())
def t_q3_other_pr_ping_does_not_count():
    e = q3_env(); e.ping_line(f"{SK}#8030", 10); fixture = base_fx(); add_pr(fixture, 803, B, reviews=[review(93, A, "2026-11-09T10:00:00Z")])
    e.run(fixture); assert len(e.missed()) == 1, repr(e.missed())
def t_q3_legacy_and_ready_kinds():
    e = q3_env(); fixture = base_fx(); fixture["search"][f"{QU}|requested"] = [item(804)]; fixture["api"][f"repos/{QU}/pulls/804"] = pull(804, A)
    fixture["api"][f"repos/{QU}/pulls/804/reviews"] = []
    e.run(fixture); assert [l.split("\t")[2:] for l in e.missed()] == [[f"{QU}#804", "NEW"]], repr(e.missed())
    e2 = q3_env(); fixture = base_fx(); fixture["search"][f"{SK}|drafts"] = [item(805)]; fixture["api"][f"repos/{SK}/pulls/805"] = pull(805, A, draft=True)
    e2.run(fixture); fixture = base_fx(); fixture["search"][f"{SK}|requested"] = [item(805)]; fixture["api"][f"repos/{SK}/pulls/805"] = pull(805, A); fixture["api"][f"repos/{SK}/pulls/805/reviews"] = []
    rc, out, _ = e2.run(fixture); assert lines(out, "READY"), repr(out)
    assert [l.split("\t")[2:] for l in e2.missed()] == [[f"{SK}#805", "READY"]], repr(e2.missed())
def t_q3_from_ping_no_log():
    q3_control(806); e = q3_env(); fixture = base_fx(); add_pr(fixture, 806, B, reviews=[review(94, A, "2026-11-09T10:00:00Z")])
    rc, out, err = e.run(fixture, "--from-ping"); assert lines(out, "UPDATED") and e.missed() == [] and "poller-missed" not in err, repr(e.missed())
def t_q3_outside_window_no_log():
    q3_control(807)
    for when in (1794074400, MON10 - 3 * 3600 - 60, MON10 + 9 * 3600):   # Sat 10:00, Mon 06:59, Mon 19:00 PT
        e = Env(); e.now = when; fixture = base_fx(); add_pr(fixture, 807, B, reviews=[review(95, A, "2026-11-09T10:00:00Z")])
        rc, out, _ = e.run(fixture); assert lines(out, "UPDATED") and e.missed() == [], f"{when}: {e.missed()!r}"
def t_q3_dry_run_no_write():
    e = q3_env(); fixture = base_fx(); add_pr(fixture, 808, B, reviews=[review(96, A, "2026-11-09T10:00:00Z")])
    rc, out, err = e.run(fixture, "--dry-run"); assert e.missed() == [] and f"poller-missed {SK}#808 UPDATED" in err, repr(err)
def t_q3_flag_off_no_log():
    q3_control(809); e = q3_env(); rc, out, err = e.run(legacy_fx(), pr_updates=False); assert out and e.missed() == [] and "poller-missed" not in err, repr(err)

for n, f in list(globals().items()):
    if n.startswith("t_") and callable(f): case(n[2:], f)
p = sum(1 for _, ok, _ in results if ok); fl = len(results) - p
print(f"SUMMARY: {p} passed, {fl} failed ({SCRIPT.name})")
sys.exit(1 if fl else 0)
