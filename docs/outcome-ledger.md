# Outcome ledger

**Goal.** Every Guild tool records what it decided. None records what happened next, so no gate, council or route can be tuned. The ledger is one shared, append-only record of both. The number it unlocks is human touches per merged PR, with escapes per merged PR as its counterweight.

Unit 1 is the event schema, the writer (`guild-ledger append`), and a backfill (`guild-ledger backfill`) that joins posted council findings to what happened on GitHub. Unit 2 adds `guild-ledger backfill-prs`, each merged or closed PR's lifecycle, so cycle time and reverts have a 30-day baseline. Tools writing events at run time come later, one unit each.

**Done when** (unit 1): on the owner's machine the backfill reproduces the read-only prototype of 2026-11-19 (18 posted findings, 15 testable, cited code gone in 8 and still present in 7, the author replied on 4 of those 7 and 3 got no response), and a second run adds 0 events.

## Where it lives

- `${GUILD_LEDGER:-~/.local/state/guild/ledger.jsonl}`. Local state, never a repo: events name private repos.
- One JSON object per line. Lines are only ever appended.
- Writers take an exclusive `flock` on the file for the read-check-append, so two tools can write at once.

## The event (`v: 1`)

```json
{
  "v": 1,
  "id": "<sha256 hex of the event without id and recorded_at>",
  "kind": "decision",
  "source": "conclave",
  "subject": {"repo": "AcmeCorp/widgets", "pr": 7, "sha": "<40 hex>", "finding": "f1"},
  "at": "2026-11-14T04:48:06+00:00",
  "data": {"action": "finding_posted"},
  "recorded_at": "2026-11-19T20:00:00+00:00"
}
```

| Field | Meaning |
| --- | --- |
| `v` | Schema version. A reader skips a version it does not know. |
| `id` | Deterministic. The writer computes it and appends nothing when the id is already in the ledger. |
| `kind` | `decision`: a tool chose something. `outcome`: what was later observed about that choice. |
| `source` | The tool that decided, or for an outcome, where it was observed (`github`). |
| `subject` | What was judged. `sha` is the commit the decision saw. `finding` is null for a whole-PR decision. A decision and its outcomes share the same subject; that is the join key. |
| `at` | When the thing happened, not when it was written. Null when it has not happened (an outcome on an open PR). |
| `data` | Kind-specific. A decision carries `action`. An outcome carries `proxy`, the name of the method that measured it. |
| `recorded_at` | When the writer appended it. Not part of `id`. |

**An outcome is an observation.** Its `id` covers what was observed, so rerunning against unchanged GitHub state adds nothing, and a changed state (a new head, a later reply, the merge) appends a new outcome. A reader takes the latest outcome per subject and proxy.

## Backfill: posted council findings

`guild-ledger backfill` reads Conclave's `tmp/council/<run>/posted.json` and each run's `findings.json`. It uses REST only (`gh api`), never GraphQL.

- One `decision` per posted review (`action: review_posted`, `finding: null`, the review `state`) and one per posted finding (`action: finding_posted`, `posted_as`, `path`, `line`, `comment_id`).
- One `outcome` per posted finding, `proxy: cited-code-at-head`. `result` is one of:
  - `gone`: the finding's `cited_code` was in the file at the reviewed commit and is not in it at the PR's head.
  - `present`: it is still there.
  - `file_deleted`: the file is absent at the head.
  - `untestable`, with `reason`: `no_path` (a review-body finding), `no_cited_code`, or `control_failed` (the code was not found at the reviewed commit, so absence at the head says nothing).
- The outcome also carries `pr_state`, the `head_sha` measured, `replies` and `author_replied` on the finding's comment thread, `merged_at`, and `posted_after_merge`.

The cited block's longest line, with whitespace collapsed, is the needle. A three-dot compare was rejected: it found 2 of 18 where this check found 8 of 15, because rebased branches break it.

**The proxy's limits.** `gone` can be an unrelated refactor that moved or rewrote the line. `present` can mean the author fixed it elsewhere or answered it in a reply. `posted_after_merge` findings could not change that PR at all. The proxy name is on each event so a better one can be added beside it.

## Backfill: PR lifecycle

`guild-ledger backfill-prs --repo OWNER/NAME [--repo ...] [--days N]` lists each repo's closed PRs (REST, newest updated first, 100 a page, stopping at the first page that reaches PRs last updated before the window) and keeps those closed in the last N days (default 30). The caller names the repos.

- One `outcome` per PR, `source: github`, `proxy: pr-lifecycle`. `subject` is the repo, the PR, its head SHA, and `finding: null`. `at` is the merge or close time.
- `data`: `state` (`merged` or `closed`), `author`, `created_at`, `merged_at` or `closed_at`, `hours_open`, `reviewers` (distinct reviewers who are not the author and not a bot), `reviews` (their submitted reviews), `is_revert` (the title starts with "Revert"), and `reverts_pr` (the number in the body's "Reverts OWNER/NAME#N", else null).
- The summary prints per repo: merged, closed unmerged, median and p90 hours open to merge, reverts and the PRs they name, authors, and merged PRs reviewed by someone else. p90 is nearest rank: the value at rank ceil(0.9 n).

**The proxy's limits.**

- An escape is an explicit revert only. A fix-forward PR that repairs a bad merge is not counted, so reverts undercount escapes.
- Human touches are not counted. Agents act under the owner's GitHub login, so GitHub cannot tell the owner's click from an agent's. The tools must record a touch when they stop for a person. That is a later unit.
- `reviewers` counts only GitHub review submissions. On a repo with one author it is near zero: a council review posted under the owner's login on the owner's own PR counts as the author's.
- `hours_open` starts at PR creation, not at the first commit or the first request for review.

## Commands

```sh
echo '<event json>' | guild-ledger append   # prints "appended <id>" or "exists <id>"
guild-ledger backfill [--council-dir DIR]    # prints counts; default DIR ~/code/conclave/tmp/council
guild-ledger backfill-prs --repo OWNER/NAME [--repo ...] [--days N]   # prints per-repo cycle time and reverts
```

Exit: 0 done; 2 a malformed event or bad arguments, nothing written; 3 the ledger, an input file or GitHub could not be read. Earlier events from a failed backfill stay, and a rerun skips them.
