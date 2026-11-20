#!/usr/bin/env bash
# Cheap pre-check for review requests to Ryan (rymiwe, incl. his teams) on the work repos.
# Repos come from `guild-config --org review_requests.repos` (a JSON list of owner/name). This repo is public,
#   so the names live there. A missing key or bad value exits 3 before any gh call.
# Metadata only (number, title, author, head sha, url); never code.
# Seen key: repo#number while Ryan or his team is requested. Head SHA is NOT part of the key, so restacks don't re-fire.
# Usage: work-review-requests-check.sh [--dry-run] [--seen FILE] [--auto-start-draft]
# Exit 0 = new requests printed (one TSV line each); 10 = nothing new (print nothing); 3 = gh error;
#   2 = unknown argument. PR updates (--pr-updates) were retired on 2026-11-19: bopeep-watch reports them.
# Containment: gh GET of search and pulls/<n> only. Never reads code, diffs, files or commit patches.
# Never writes to GitHub.
set -uo pipefail
SEEN=/home/box/agent-data/fleet/state/work-review-requests.seen
DRY=0
# Auto-start draft-only reviews (Paul G1-G6). OFF by default. Ships disabled.
# Only --auto-start-draft or WES_AUTO_START_DRAFT=1 turns it on for that process.
AUTO_START_DRAFT_DEFAULT=0
AUTOSTART=$AUTO_START_DRAFT_DEFAULT
[ "${WES_AUTO_START_DRAFT:-0}" = 1 ] && AUTOSTART=1
while [ $# -gt 0 ]; do case "$1" in
  --dry-run) DRY=1;; --seen) SEEN="$2"; shift;; --auto-start-draft) AUTOSTART=1;;
  *) echo "work-review-requests-check: unknown argument $1" >&2; exit 2;;
esac; shift; done
mkdir -p "$(dirname "$SEEN")"; touch "$SEEN"
cur=$(mktemp); trap 'rm -f "$cur"' EXIT
_r=$(guild-config --org review_requests.repos) || exit 3
_r=$(printf '%s' "$_r" | python3 -c 'import json,re,sys
v = json.load(sys.stdin)
ok = isinstance(v, list) and v and all(isinstance(r, str) and re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", r) for r in v)
sys.exit(0 if ok and not print("\n".join(v)) else 3)') || { echo "guild-config: review_requests.repos is not a list of owner/name" >&2; exit 3; }
mapfile -t REPOS <<<"$_r"
for repo in "${REPOS[@]}"; do
  out=$(gh api -X GET search/issues -f q="is:pr is:open draft:false repo:$repo review-requested:rymiwe -author:rymiwe -author:app/dependabot" -f per_page=100 \
      -q '.items[]|[.number,.title,.user.login,.html_url]|@tsv' 2>/dev/null) || { echo "gh error on $repo" >&2; exit 3; }
  [ -n "$out" ] && while IFS=$'\t' read -r n t a u; do printf '%s#%s\t%s\t%s\t%s\n' "$repo" "$n" "$t" "$a" "$u"; done <<<"$out" >>"$cur"
done
new=$(cut -f1 "$cur" | sort -u | comm -23 - <(sort -u "$SEEN"))
if [ "$DRY" = 0 ]; then cut -f1 "$cur" | sort -u > "$SEEN"; fi   # requests that cleared drop out, so a real re-request fires again
# Auto-start hook (flag off by default). Uses this pass's new requests only (G6, no new poll).
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
    { [ -n "$new" ] && printf '%s\n' $new | awk '{print "NEW\t"$1}'; } > "$_keys" 2>/dev/null || true
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
[ -z "$new" ] && exit 10
while read -r k; do
  line=$(grep -m1 -F "$k"$'\t' "$cur"); repo=${k%#*}; n=${k#*#}
  sha=$(gh api repos/$repo/pulls/$n -q .head.sha 2>/dev/null | cut -c1-9)
  printf '%s\t%s\n' "$line" "$sha"
done <<<"$new"
exit 0
