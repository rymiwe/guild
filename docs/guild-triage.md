# guild-triage

**Goal.** Automatically read Guild job-alert logs (`STOPPED`, `FAILED`, `HELD`, `ENDED-NO-REPORT`) and active seat-down markers across BLD and remote job hosts, diagnose root causes, and open small, tested **draft PRs** in public Guild repositories without bypassing any Guild gate or leaking private repo names.

**Boundary.** [`bin/guild-triage`](../bin/guild-triage) on BLD owns deterministic log collection, read-only SSH reads, bounded local evidence bundles, incident/fingerprint deduplication, kill switch, and daily draft-PR cap tracking. A scheduled T3 agent on BLD owns root-cause classification and drafting fixes in isolated worktrees.

---

## Commands

```bash
# Collect new alert incidents from BLD and a remote host (read-only over SSH):
~/code/guild/bin/guild-triage collect --hosts local,surface --since-hours 24

# Dry-run without writing bundles:
~/code/guild/bin/guild-triage collect --hosts local,surface --dry-run

# Record disposition (suppresses duplicate incident + 24h fingerprint cooldown):
~/code/guild/bin/guild-triage record <incident_id> \
  --fingerprint <fp> \
  --outcome fixed \
  --summary "One-line sanitized summary" \
  --repo rymiwe/quartermaster \
  --pr https://github.com/rymiwe/quartermaster/pull/131

# Check state, caps, and recent outcomes:
~/code/guild/bin/guild-triage status
```

## Standing instructions for the scheduled triage agent

Run on BLD only (never Surface, never work checkouts):

1. Run `~/code/guild/bin/guild-triage collect --hosts local,surface --since-hours 24`.
   - If it exits `11` (kill switch), stop immediately.
   - If `incidents` is empty and there are no unexpected active seat-down markers, stop immediately (no-op).
2. For each incident, read its local evidence bundle under `bundle_dir` (`meta.json`, `report-head.md`, `report-tail.md`, `run-tail.log`, `failover.log`, `seat-health.json`) and classify into one of three buckets:
   - **`expected`**: legitimate operational hold (base moved, PR conflict, real `401`/`402`/`429` seat error, external outage). Record `expected` and move on.
   - **`cleared`**: provably bogus local state (for example a `seat-health/<seat>` marker written by a false quick-exit trigger on a job whose `report.md` and `run.log` show completion). Clear only that exact marker and record `cleared` (never clear genuine `401`/`402`/`429` limits).
   - **`fixed` (Guild tool bug)**: concrete defect in `guild` or a repo listed in `roster`, and `caps.can_open_pr` is `true`:
     - Branch an isolated worktree from `origin/main` under `~/.t3/worktrees/<repo>/` (never modify `~/code/<repo>` main checkouts, never touch work repos).
     - Commit 1 (`test(...): ...`): failing regression test reproducing the bug.
     - Commit 2 (`fix(...): ...`): minimal fix making the test suite pass.
     - Never edit `.github/`, `tests/member-pins.json`, `.guild.json`, or credentials.
     - Never put work org or work repo names in commit messages, PR titles/bodies, or test fixtures.
     - Run the target repo's test suite and `~/code/guild/bin/true-check` on the diff.
     - Push and open a **draft** PR (`gh pr create --draft`). Never mark ready, request reviewers, or merge.
     - Record `fixed` with `--pr <url>` and `--fingerprint <fp>`.
   - **`blocked`**: real bug, but cap reached, fix is risky/large, or root cause is ambiguous. Record `blocked` with a short summary for Ryan.

## Controls

- **Kill switch:** `touch ~/.config/guild/triage.off` makes `collect` exit `11` immediately. Delete to resume.
- **State:** `~/.local/state/guild/triage/{seen.json,events.jsonl,bundles/,lock}` (override with `GUILD_TRIAGE_STATE_DIR`). Local state only; never committed.
- **Caps & cooldown:** `GUILD_TRIAGE_DAILY_CAP` (default `2` draft PRs/day PT), `GUILD_TRIAGE_COOLDOWN_HOURS` (default `24` hours per normalized failure fingerprint).
