# Review-request gates

Two review-request gates for a work coding bot.

| File | What it does |
| --- | --- |
| `bin/work-review-requests-check.sh` | Lists new review requests to the owner on the work repos. Reads GitHub metadata only. |
| `bin/wes_autostart_draft_review.py` | Decides whether a colleague's review request under N lines may start a draft-only review without asking (G1 to G6). Ships disabled; nothing it starts can post. |

Org facts stay out of this repo. The check reads its repos from
`guild-config --org review_requests.repos`, a JSON list of `owner/name`. A
missing or bad key exits 3 before any GitHub call. The autostart gate reads
`classified_repos` and `org_members` from its prefs file; with no
`classified_repos` it asks.

PR updates (a PR that became ready, or moved past the owner's review) were retired
on 2026-11-19: bopeep-watch's CHANGED pings report them. The check exits 2 on
the old `--pr-updates` flag, so a caller left over from then fails loudly.

The unanswered-threads gate and the follow-up draft gate are not here. They
live in bopeep (`bin/threads-check`, `bin/followup-gate`).

Fixtures: `tests/test_wes_autostart_draft_review.py` and
`tests/work-review-requests-check-fixtures/run_tests.py` (fake `gh` and
`guild-config`, no network).
