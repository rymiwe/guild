# Work Wes gates

Two Work Wes gates moved here from the fleet bin. The fleet copies still run
and stay until Ryan says to delete them.

| File | What it does |
| --- | --- |
| `bin/work-review-requests-check.sh` | Lists new review requests to Ryan on the work repos, and with `--pr-updates` PRs that became ready or moved past his review. Reads GitHub metadata only. |
| `bin/wes_autostart_draft_review.py` | Decides whether a colleague's review request under N lines may start a draft-only review without asking (G1 to G6). Ships disabled; nothing it starts can post. |
| `bin/wes-starts-mirror` | Writes the job-ledger mirror the check reads. |

Org facts stay out of this repo. The check reads its repos from
`guild-config --org`: `review_requests.repos` and, with `--pr-updates`,
`review_requests.update_repos`, both JSON lists of `owner/name`. A missing or
bad key exits 3 before any GitHub call. The autostart gate reads
`classified_repos` and `org_members` from its prefs file; with no
`classified_repos` it asks.

The unanswered-threads gate and the follow-up draft gate are not here: the
Nov 13 Wes split moved them into bopeep (`bin/threads-check`,
`bin/followup-gate`).

Fixtures: `tests/test_wes_autostart_draft_review.py` and
`tests/work-review-requests-check-fixtures/run_tests.py` (fake `gh` and
`guild-config`, no network).
