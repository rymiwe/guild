# Outcome ledger

**Goal.** Every Guild tool records what it decided. None records what happened next, so no gate, council or route can be tuned. The ledger is one shared, append-only record of both. The number it unlocks is human touches per merged PR, with escapes per merged PR as its counterweight.

Unit 1 is the event schema, the writer (`guild-ledger append`), and a backfill (`guild-ledger backfill`) that joins posted council findings to what happened on GitHub. Unit 2 adds `guild-ledger backfill-prs`, each merged or closed PR's lifecycle, so cycle time and reverts have a 30-day baseline. Unit 3 runs both backfills once a day on BLD and pings when one fails. Tools writing events at run time come later, one unit each.

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
- Human touches are not counted on the lifecycle event. Agents act under the owner's GitHub login, so GitHub cannot tell the owner's click from an agent's. The tools record a touch as a `human_stop` decision event when they stop for a person, and `guild-ledger touches` joins those stops to `pr-lifecycle` outcomes.
- `reviewers` counts only GitHub review submissions. On a repo with one author it is near zero: a council review posted under the owner's login on the owner's own PR counts as the author's.
- `hours_open` starts at PR creation, not at the first commit or the first request for review.

## Human stops and touches

When a tool stops for a person on a PR, it writes a `decision` event with `data.action: human_stop`.

- `data.stop` is one of a closed list: `split_decision`, `posting_go`, `manual_merge`, `ack_loop`, `one_way_ack`, or `brief_approval`. `validate()` rejects any other stop name (exit 2).
- `data.answered_at` is an optional ISO 8601 time (or null) for when the person answered.
- `subject` keeps the full schema (`repo`, `pr`, 40-hex `sha`, and `finding` or null). Stops without a PR, such as route asks in `asks.jsonl`, are out of this unit until a stop can name its PR.

`guild-ledger touches [--days N] [--repo OWNER/NAME ...]` reads only the ledger file and makes no GitHub calls. It takes each PR whose latest `pr-lifecycle` outcome is `state: merged` in the last N days (default 7), joins all `human_stop` decisions to it by `(repo, pr)`, and prints per repo: merged PRs in the window, human stops on them, touches per merged PR, and a count per stop name. With `--repo`, it reports those repos in the order given; without `--repo`, it reports every repo present in the ledger's `pr-lifecycle` or `human_stop` events in alphabetical order.

## Scheduled refresh (BLD only)

`guild-ledger refresh` runs `backfill`, then `backfill-prs` with the default 30-day window. `guild-ledger-refresh.timer` starts it once a day, and at the next boot when BLD was off at midnight. It runs on BLD only: Conclave's run folders and the ledger are local state there. Nothing enables it on the Surface.

- **Which repos.** Guild itself, each tool in `roster` under Guild's GitHub owner (read from the checkout's origin URL), then the list at `guild-config --org guild-ledger.pr_repos`, for example `{"guild-ledger": {"pr_repos": ["OWNER/NAME"]}}` in the org `.guild.json`. Repos outside Guild are named only there, never in this repo. A missing or malformed key fails the PR step. An empty list adds no repo.
- **How the unit reaches the tool.** `ExecStart` is `guild-exec guild bin/guild-ledger refresh`, so it runs the main checkout `~/code/guild` and refuses (exit 3) when that checkout is dirty or off its default branch. `bopeep sync` fast-forwards it. The unit's `PATH` puts linuxbrew before `/usr/bin`, because BLD's system `python3` is 3.10.
- **Failure.** A step that fails (exit 3, or an unexpected input shape) does not stop the other step. Each failed kind (`council` or `prs`) sends one `job-notify --send` ping, `job guild`, `session guild-ledger-refresh`, `FAILED`, and writes `refresh-pinged/<kind>` beside the ledger. A run where that kind passes removes the marker, so the next failure pings again. Any failed step exits 3, and the journal (`journalctl --user -u guild-ledger-refresh`) keeps the output.
- **Cost.** About 52 REST calls for the council backfill plus 1 per page and 1 per closed PR in the window for each repo.

Install on BLD, from the main checkout:

```sh
systemctl --user link ~/code/guild/systemd/guild-ledger-refresh.service
systemctl --user enable --now ~/code/guild/systemd/guild-ledger-refresh.timer
```

Undo: `systemctl --user disable --now guild-ledger-refresh.timer guild-ledger-refresh.service`. That removes both links and leaves the ledger as it is.

## Commands

```sh
echo '<event json>' | guild-ledger append   # prints "appended <id>" or "exists <id>"
guild-ledger backfill [--council-dir DIR]    # prints counts; default DIR ~/code/conclave/tmp/council
guild-ledger backfill-prs --repo OWNER/NAME [--repo ...] [--days N]   # prints per-repo cycle time and reverts
guild-ledger refresh [--council-dir DIR]     # both backfills, one ping per failed kind (the daily timer)
guild-ledger touches [--days N] [--repo OWNER/NAME ...]   # prints per-repo human touches per merged PR from the ledger
```

The council dir is `--council-dir`, else `$GUILD_LEDGER_COUNCIL_DIR`, else `~/code/conclave/tmp/council`.

Exit: 0 done; 2 a malformed event or bad arguments, nothing written; 3 the ledger, an input file, the config or GitHub could not be read, or for `refresh`, any step failed. Earlier events from a failed backfill stay, and a rerun skips them.
