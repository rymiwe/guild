#!/usr/bin/env bash
# Cheap pre-check for review requests to Ryan (rymiwe, incl. his teams) on the work repos.
# Repos come from `guild-config --org`: review_requests.repos always, review_requests.update_repos
#   with --pr-updates (JSON lists of owner/name). This repo is public, so the names live there.
#   A missing key or bad value exits 3 before any gh call.
# Metadata only (number, title, author, head sha, url); never code.
# Seen key: repo#number while Ryan or his team is requested. Head SHA is NOT part of the key, so restacks don't re-fire.
# Usage: work-review-requests-check.sh [--dry-run] [--seen FILE] [--pr-updates] [--jobs FILE] [--explain] [--from-ping] [--auto-start-draft]
# Exit 0 = new requests printed (one TSV line each); 10 = nothing new (print nothing); 3 = gh error.
# Exit 4 (flag on only) = ledger mirror missing, stale or bad: UPDATED failed closed (no UPDATED lines, no
#   UPDATED state written); legacy and READY lines still print, and a MIRROR_FAIL<TAB><why> line is printed last.
#
# PR updates (Paul verdict wes/2026-11-09-paul-verdict-pr-update-rereview.md, PASS WITH 2 FIXES U1/U2).
# OFF by default (PR_UPDATES_DEFAULT=0); only --pr-updates turns it on for that one process. Not live.
# With it on, two more line kinds may print (legacy lines are unchanged; column 1 names the kind):
#   READY<TAB>repo#n<TAB>head9<TAB>draft=false
#       a PR last seen as a draft that is now ready and still requests rymiwe or his teams.
#       It replaces that PR's legacy line, so one PR never prints twice.
#   UPDATED<TAB>repo#n<TAB>head9<TAB>prev=<head9><TAB>ci=green<TAB>route=review|replay[<TAB>src=N,N]<TAB>review=<id|->
#       head matches neither Ryan's latest review (its commit_id) nor any head= of our review jobs for that PR.
#       prev = our newest review job head, else Ryan's review commit.
#   Ledger: the mirror written by wes-starts-mirror (--jobs or $WRRC_JOBS, default state/wes-starts.mirror.log):
#       header "# wes-starts-mirror v1 ts= sha256= lines=", body "<job name>[ head=<sha>]" in ledger order.
#       Checked every run: header, sha256 of the body, line count, age <= $WRRC_MIRROR_MAX_AGE_S (7200).
#       Any failure: exit 4, MIRROR_FAIL line, no UPDATED.
#   U1/U1b: UPDATED prints only when CI finished green at the new head. Judged checks are the branch's
#       required contexts when protection/required_status_checks is readable, else all checks and statuses.
#       Pending (not reported, running, or a conclusion of cancelled or stale): skip, not marked seen.
#       Red (failure, timed_out, action_required, startup_failure; status failure/error): marked ci=red with the
#       first-red time, nothing printed; re-checked every run for 24 h from the first red, and fires once
#       when it goes green. After 24 h at the same head it stays quiet until the head moves.
#   U2: open, non-draft, not authored by rymiwe, not Dependabot, not merged or closed, and Ryan still
#       requested or his latest review is the newest job (our newest review job head is his review commit,
#       or we have no job on that PR).
#   Seen state for UPDATED: <seen>.updated, key (repo#n, head) -> fired | ci=red<TAB><first red epoch>.
#   Draft state for READY: <seen>.drafts (requested drafts at the last run).
#   Restacks: route=review (full review) until wes-approve-gate's patch-id matcher is wired to the single
#       hook $WRRC_PATCHID_ORACLE (argv: repo n head; exit 0 printing "match N[,N]" -> route=replay src=...).
#       No matcher ships here (Paul: reuse wes-approve-gate's, never a second one). Unset, an error, or any
#       other answer: route=review. The replay and G1 gates still make the Approve decision.
#   Repos: one list. review_requests.update_repos is appended to it when the flag is on (legacy lines included).
#   --explain prints one reason line per candidate to stderr (for dry runs).
#   Poller backstop (Paul poller verdict Q3): with the flag on and without --from-ping (the hourly run), every
#       printed change (legacy NEW, READY, UPDATED) with no pr-change ping for that repo#n in the run log
#       ($WRRC_RUNS_LOG, default fleet/routine-runs.log) in the last 70 minutes, during the poller window
#       (Mon-Fri 07:00-19:00 PT), appends "<ts><TAB>poller-missed<TAB>repo#n<TAB>kind" to
#       <seen>.poller-missed.log and prints "poller-missed repo#n kind" to stderr. Dry run: stderr only.
#       --from-ping (the pr-change intake RUN path) never logs misses.
# Containment: gh GET of search, pulls/<n>, pulls/<n>/reviews, commits/<sha>/check-runs and /status only.
# Never reads code, diffs, files or commit patches. Never writes to GitHub.
set -uo pipefail
SEEN=/home/box/agent-data/fleet/state/work-review-requests.seen
DRY=0
PR_UPDATES_DEFAULT=0
PRUPD=$PR_UPDATES_DEFAULT
# Auto-start draft-only reviews (Paul G1-G6). OFF by default. Ships disabled.
# Only --auto-start-draft or WES_AUTO_START_DRAFT=1 turns it on for that process.
# Hooked here so the PR events routine prompt stays untouched. No new poll.
AUTO_START_DRAFT_DEFAULT=0
AUTOSTART=$AUTO_START_DRAFT_DEFAULT
[ "${WES_AUTO_START_DRAFT:-0}" = 1 ] && AUTOSTART=1
JOBS=${WRRC_JOBS:-/home/box/agent-data/fleet/state/wes-starts.mirror.log}
EXPLAIN=0
FROM_PING=0
while [ $# -gt 0 ]; do case "$1" in --dry-run) DRY=1;; --seen) SEEN="$2"; shift;; --pr-updates) PRUPD=1;; --jobs) JOBS="$2"; shift;; --explain) EXPLAIN=1;; --from-ping) FROM_PING=1;; --auto-start-draft) AUTOSTART=1;; esac; shift; done
mkdir -p "$(dirname "$SEEN")"; touch "$SEEN"
cur=$(mktemp); ext=$(mktemp); mf=$(mktemp); trap 'rm -f "$cur" "$ext" "$mf"' EXIT
repo_list() {   # one owner/name per line from a guild-config JSON list; anything else fails
  local raw; raw=$(guild-config --org "$1") || return 3
  printf '%s' "$raw" | python3 -c 'import json,re,sys
v = json.load(sys.stdin)
ok = isinstance(v, list) and v and all(isinstance(r, str) and re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", r) for r in v)
sys.exit(0 if ok and not print("\n".join(v)) else 3)' || { echo "guild-config: $1 is not a list of owner/name" >&2; return 3; }
}
_r=$(repo_list review_requests.repos) || exit 3; mapfile -t REPOS <<<"$_r"
if [ "$PRUPD" = 1 ]; then _r=$(repo_list review_requests.update_repos) || exit 3; mapfile -t _upd <<<"$_r"; REPOS+=("${_upd[@]}"); fi
for repo in "${REPOS[@]}"; do
  out=$(gh api -X GET search/issues -f q="is:pr is:open draft:false repo:$repo review-requested:rymiwe -author:rymiwe -author:app/dependabot" -f per_page=100 \
      -q '.items[]|[.number,.title,.user.login,.html_url]|@tsv' 2>/dev/null) || { echo "gh error on $repo" >&2; exit 3; }
  [ -n "$out" ] && while IFS=$'\t' read -r n t a u; do printf '%s#%s\t%s\t%s\t%s\n' "$repo" "$n" "$t" "$a" "$u"; done <<<"$out" >>"$cur"
done
new=$(cut -f1 "$cur" | sort -u | comm -23 - <(sort -u "$SEEN"))
if [ "$PRUPD" = 1 ]; then
  # Python reads only gh metadata; prints READY/UPDATED lines to $ext and the READY keys (to drop from legacy) to stdout.
  ready_keys=$(WRRC_SEEN="$SEEN" WRRC_DRY="$DRY" WRRC_JOBS_FILE="$JOBS" WRRC_EXPLAIN="$EXPLAIN" WRRC_CUR="$cur" WRRC_EXT="$ext" WRRC_MF="$mf" WRRC_REPOS="${REPOS[*]}" WRRC_NEW="$new" python3 - <<'PY'
import hashlib, json, os, re, subprocess, sys, time
from datetime import datetime
REPOS = os.environ["WRRC_REPOS"].split()
NOW = int(os.environ.get("WRRC_NOW") or time.time())
MIRROR_MAX_AGE = int(os.environ.get("WRRC_MIRROR_MAX_AGE_S") or 7200)
RED_WINDOW = 24 * 3600
ME = "rymiwe"
seen = os.environ["WRRC_SEEN"]; dry = os.environ["WRRC_DRY"] == "1"; explain = os.environ["WRRC_EXPLAIN"] == "1"
base = seen[:-5] if seen.endswith(".seen") else seen
drafts_f, upd_f = base + ".drafts", base + ".updated"
class GhError(Exception): pass
def gh(path, q=None):
    args = ["gh", "api", "-X", "GET", path]
    if q is not None: args += ["-f", "q=" + q, "-f", "per_page=100"]
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0: raise GhError(path)
    try: return json.loads(r.stdout)
    except ValueError: raise GhError(path)
def why(msg):
    if explain: print("explain: " + msg, file=sys.stderr)
def ts(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=__import__("datetime").timezone.utc).timestamp()
def same(a, b):
    if not a or not b: return False
    n = min(len(a), len(b)); return n >= 7 and a[:n] == b[:n]
def read_lines(f):
    try: return [l.rstrip("\n") for l in open(f) if l.strip()]
    except FileNotFoundError: return []
requested = {l.split("\t")[0] for l in read_lines(os.environ["WRRC_CUR"])}
new_keys = {k for k in os.environ.get("WRRC_NEW", "").split("\n") if k}
prev_drafts = set(read_lines(drafts_f))
upd_fired, upd_red = set(), {}
for l in read_lines(upd_f):
    p = l.split("\t")
    if len(p) < 3: continue
    if p[2] == "fired": upd_fired.add((p[0], p[1]))
    elif p[2] == "ci=red":
        first = int(p[3]) if len(p) > 3 and p[3].isdigit() else NOW
        upd_red[(p[0], p[1])] = min(first, upd_red.get((p[0], p[1]), first))
def load_mirror(path):
    """(jobs, None) or (None, why). jobs: repo#n -> list of heads in ledger order ("" = no head= on that line)."""
    try: rows = open(path).read().splitlines()
    except OSError: return None, "mirror missing"
    if not rows: return None, "mirror empty"
    m = re.fullmatch(r"# wes-starts-mirror v1 ts=([0-9]+) sha256=([0-9a-f]{64}) lines=([0-9]+)", rows[0])
    if not m: return None, "mirror header bad"
    body = rows[1:]
    if hashlib.sha256("".join(l + "\n" for l in body).encode()).hexdigest() != m.group(2): return None, "mirror sha mismatch"
    if int(m.group(3)) != len(body): return None, "mirror line count mismatch"
    age = NOW - int(m.group(1))
    if age > MIRROR_MAX_AGE or age < -300: return None, f"mirror stale (age {age}s)"
    by_name = {r.split("/")[-1]: r for r in REPOS}   # job names carry the repo name, not the owner
    jobs = {}
    for l in body:
        mm = re.fullmatch(r"review-([a-z]+(?:-[a-z]+)*)-([0-9]+)(?:-[A-Za-z0-9-]+)?(?: head=([0-9a-f]{7,40}))?", l)
        if not mm: return None, "mirror line bad"
        repo = by_name.get(mm.group(1))
        if repo: jobs.setdefault(f"{repo}#{mm.group(2)}", []).append(mm.group(3) or "")
    return jobs, None
jobs, mirror_fail = load_mirror(os.environ["WRRC_JOBS_FILE"])
if mirror_fail: why(f"ledger mirror failed ({mirror_fail}): UPDATED fails closed")
def gh_soft(path):
    try: return gh(path)
    except GhError: return None
def ci_state(repo, head, base_ref):
    """green | pending | red, judged on required contexts when readable, else on every check and status."""
    req = None
    if base_ref:
        rp = gh_soft(f"repos/{repo}/branches/{base_ref}/protection/required_status_checks")
        if isinstance(rp, dict):
            req = set(rp.get("contexts") or []) | {c.get("context") for c in (rp.get("checks") or []) if c.get("context")}
            req = req or None
    cr = gh(f"repos/{repo}/commits/{head}/check-runs?per_page=100").get("check_runs", [])
    st = gh(f"repos/{repo}/commits/{head}/status").get("statuses", [])
    if req is not None:
        cr = [c for c in cr if c.get("name") in req]; st = [s for s in st if s.get("context") in req]
        seen_ctx = {c.get("name") for c in cr} | {s.get("context") for s in st}
        if req - seen_ctx: return "pending"
    red = {"failure", "timed_out", "action_required", "startup_failure"}
    if any(c.get("status") == "completed" and c.get("conclusion") in red for c in cr) or any(s.get("state") in ("failure", "error") for s in st):
        return "red"
    if not cr and not st: return "pending"
    if any(c.get("status") != "completed" or c.get("conclusion") in ("cancelled", "stale") for c in cr) or any(s.get("state") == "pending" for s in st):
        return "pending"
    return "green"
out_lines, ready_keys, new_drafts, new_upd = [], [], set(), []
try:
    for repo in REPOS:
        d = gh("search/issues", f"is:pr is:open draft:true repo:{repo} review-requested:{ME} -author:{ME} -author:app/dependabot")
        for it in d.get("items", []): new_drafts.add(f"{repo}#{it['number']}")
        rv = gh("search/issues", f"is:pr is:open draft:false repo:{repo} reviewed-by:{ME} -author:{ME} -author:app/dependabot")
        cands = sorted({int(k.split("#")[1]) for k in requested if k.startswith(repo + "#")} | {int(it["number"]) for it in rv.get("items", [])})
        for n in cands:
            key = f"{repo}#{n}"
            pr = gh(f"repos/{repo}/pulls/{n}")
            head = (pr.get("head") or {}).get("sha") or ""
            author = (pr.get("user") or {}).get("login") or ""
            # READY: last run saw it as a requested draft; now ready and still requested.
            if key in prev_drafts and key in requested and pr.get("state") == "open" and not pr.get("draft"):
                out_lines.append(f"READY\t{key}\t{head[:9]}\tdraft=false"); ready_keys.append(key)
                why(f"{key} READY draft->ready, still requested"); continue
            # U2 scope
            if mirror_fail: why(f"{key} skip UPDATED (ledger mirror failed)"); continue
            if pr.get("state") != "open" or pr.get("merged"): why(f"{key} skip U2 closed/merged"); continue
            if pr.get("draft"): why(f"{key} skip U2 draft"); continue
            if author == ME: why(f"{key} skip U2 Ryan's own PR"); continue
            if author.startswith("dependabot"): why(f"{key} skip U2 Dependabot"); continue
            revs = [r for r in gh(f"repos/{repo}/pulls/{n}/reviews?per_page=100") if (r.get("user") or {}).get("login") == ME and r.get("submitted_at")]
            ryan = max(revs, key=lambda r: ts(r["submitted_at"])) if revs else None
            job_heads = jobs.get(key, [])
            newest_job = job_heads[-1] if job_heads else None
            is_req = key in requested
            ryan_c = (ryan.get("commit_id") or "") if ryan else ""
            if not ryan and newest_job is None: why(f"{key} no baseline (fresh request, legacy path)"); continue
            known = [h for h in job_heads if h] + ([ryan_c] if ryan_c else [])
            if any(same(head, h) for h in known): why(f"{key} no change at {head[:9]}"); continue
            if not is_req and not (ryan and (newest_job is None or same(newest_job, ryan_c))):
                why(f"{key} skip U2 not requested and Ryan's latest review is not the newest job"); continue
            if newest_job == "": why(f"{key} skip newest job has no head= (baseline unknown)"); continue
            prev = newest_job if newest_job else ryan_c
            rid = str(ryan["id"]) if ryan else "-"
            if (key, head) in upd_fired: why(f"{key} seen at {head[:9]} (fired)"); continue
            if key in new_keys:
                new_upd.append(f"{key}\t{head}\tfired"); why(f"{key} covered by its legacy request line at {head[:9]} (no UPDATED)"); continue
            first_red = upd_red.get((key, head))
            if first_red is not None and NOW - first_red > RED_WINDOW:
                why(f"{key} U1b red at {head[:9]} for over 24 h: quiet until the head moves"); continue
            c = ci_state(repo, head, (pr.get("base") or {}).get("ref"))
            if c == "pending": why(f"{key} skip U1 CI pending at {head[:9]} (not marked seen)"); continue
            if c == "red":
                if first_red is None: new_upd.append(f"{key}\t{head}\tci=red\t{NOW}")
                why(f"{key} U1 CI red at {head[:9]}: ci=red, no start, re-checked hourly for 24 h"); continue
            route, src = "review", ""
            orc = os.environ.get("WRRC_PATCHID_ORACLE")
            if orc:
                try:
                    r = subprocess.run([orc, repo, str(n), head], capture_output=True, text=True, timeout=120)
                    m = re.fullmatch(r"match ([0-9]+(?:,[0-9]+)*)\s*", r.stdout)
                    if r.returncode == 0 and m: route, src = "replay", m.group(1)
                except Exception: pass
            out_lines.append(f"UPDATED\t{key}\t{head[:9]}\tprev={prev[:9]}\tci=green\troute={route}" + (f"\tsrc={src}" if src else "") + f"\treview={rid}")
            new_upd.append(f"{key}\t{head}\tfired"); why(f"{key} UPDATED {prev[:9]}->{head[:9]} ci=green route={route}")
except GhError as e:
    print(f"gh error on {e}", file=sys.stderr); sys.exit(3)
if mirror_fail: open(os.environ["WRRC_MF"], "w").write(mirror_fail)
if not dry:
    open(drafts_f, "w").write("".join(k + "\n" for k in sorted(new_drafts)))
    if new_upd and not mirror_fail: open(upd_f, "a").write("".join(l + "\n" for l in new_upd))
open(os.environ["WRRC_EXT"], "w").write("".join(l + "\n" for l in out_lines))
print("\n".join(ready_keys))
PY
) || exit 3
  [ -n "$ready_keys" ] && new=$(comm -23 <(printf '%s\n' "$new" | sed '/^$/d' | sort -u) <(printf '%s\n' "$ready_keys" | sort -u))
fi
if [ "$DRY" = 0 ]; then cut -f1 "$cur" | sort -u > "$SEEN"; fi   # requests that cleared drop out, so a real re-request fires again
# Auto-start hook (flag off by default). Uses this shepherding pass only (G6, no new poll).
# When AUTOSTART=1 and WES_AUTO_START_HOOK_JSON is unset, write hook JSON from this pass's keys.
if [ "$AUTOSTART" = 1 ]; then
  _as="$(cd "$(dirname "$0")" && pwd)/wes_autostart_draft_review.py"
  : "${WES_AUTO_START_PREFS:=/home/box/agent-data/fleet/state/wes-autostart-draft-review.prefs.json}"
  if [ ! -f "$WES_AUTO_START_PREFS" ]; then unset WES_AUTO_START_PREFS; fi
  _hook_own=0
  if [ -z "${WES_AUTO_START_HOOK_JSON:-}" ]; then
    WES_AUTO_START_HOOK_JSON=$(mktemp)
    _hook_own=1
    _keys=$(mktemp)
    { [ -n "$new" ] && printf '%s\n' $new | awk '{print "NEW\t"$1}';
      [ -s "$ext" ] && awk -F'\t' '$1=="READY"||$1=="UPDATED"{print $1"\t"$2}' "$ext";
    } > "$_keys" 2>/dev/null || true
    python3 "$_as" write-hook --keys "$_keys" --json "$WES_AUTO_START_HOOK_JSON" || printf '[]\n' > "$WES_AUTO_START_HOOK_JSON"
    rm -f "$_keys"
    export WES_AUTO_START_HOOK_JSON
  fi
  if [ -f "$WES_AUTO_START_HOOK_JSON" ]; then
    if [ "$DRY" = 1 ]; then export WES_AUTO_START_DRY=1; fi
    python3 "$_as" hook ${WES_AUTO_START_PREFS:+--prefs "$WES_AUTO_START_PREFS"} --json "$WES_AUTO_START_HOOK_JSON" || true
  fi
  if [ "$_hook_own" = 1 ]; then
    rm -f "$WES_AUTO_START_HOOK_JSON"
    unset WES_AUTO_START_HOOK_JSON
  fi
fi
[ -z "$new" ] && [ ! -s "$ext" ] && [ ! -s "$mf" ] && exit 10
[ -n "$new" ] && while read -r k; do
  line=$(grep -m1 -F "$k"$'\t' "$cur"); repo=${k%#*}; n=${k#*#}
  sha=$(gh api repos/$repo/pulls/$n -q .head.sha 2>/dev/null | cut -c1-9)
  printf '%s\t%s\n' "$line" "$sha"
done <<<"$new"
cat "$ext"
if [ "$PRUPD" = 1 ] && [ "$FROM_PING" = 0 ]; then
  { [ -n "$new" ] && printf '%s\tNEW\n' $new; awk -F'\t' '$1=="READY"||$1=="UPDATED"{print $2"\t"$1}' "$ext"; } | \
  WRRC_MISSED="${SEEN%.seen}.poller-missed.log" WRRC_DRY="$DRY" python3 -c '
import os, sys, time
from datetime import datetime
from zoneinfo import ZoneInfo
PT = ZoneInfo("America/Los_Angeles")
now = int(os.environ.get("WRRC_NOW") or time.time()); t = datetime.fromtimestamp(now, PT)
stamp = t.isoformat(timespec="seconds")
if not (t.weekday() < 5 and 7 <= t.hour < 19): sys.exit(0)
pinged = set()
try: rows = open(os.environ.get("WRRC_RUNS_LOG") or "/home/box/agent-data/fleet/routine-runs.log").read().splitlines()
except OSError: rows = []
for l in rows:
    f = l.split("\t")
    if len(f) < 8 or f[2] != "Surface job finished (ping)" or not f[7].startswith("pr-change "): continue
    try: ts = datetime.fromisoformat(f[0]).timestamp()
    except ValueError: continue
    if 0 <= now - ts <= 70 * 60: pinged.add(f[7].split()[1])
out = []
for l in sys.stdin.read().splitlines():
    k, kind = l.split("\t")
    if k in pinged: continue
    print(f"poller-missed {k} {kind}", file=sys.stderr); out.append(f"{stamp}\tpoller-missed\t{k}\t{kind}\n")
if out and os.environ["WRRC_DRY"] != "1":
    open(os.environ["WRRC_MISSED"], "a").write("".join(out))
' || true
fi
if [ -s "$mf" ]; then printf 'MIRROR_FAIL\t%s\n' "$(cat "$mf")"; echo "ledger mirror failed: $(cat "$mf"); UPDATED failed closed" >&2; exit 4; fi
exit 0
