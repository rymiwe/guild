# Review-request gates

Two review-request gates for a work coding bot.

| File | What it does |
| --- | --- |
| `bin/work-review-requests-check.sh` | Lists new review requests to the owner on the work repos. Reads GitHub metadata only. |
| `bin/autostart_draft_review.py` | Decides whether a colleague's review request under N lines may start a draft-only review without asking (G1 to G6). Ships disabled; nothing it starts can post. |

Org facts stay out of this repo. The check reads its repos from
`guild-config --org review_requests.repos`, a JSON list of `owner/name`, and
the owner's GitHub login from `guild-config --org review_requests.owner_login`.
It takes the login from config, not `gh api user`, because it may run under
the work bot's GitHub account. Its seen file is `--seen FILE` or
`$REVIEW_REQUESTS_SEEN`. A missing or bad key, or no seen file, exits 3 before
any GitHub call.

The autostart gate turns on with `--auto-start-draft` or `AUTOSTART_DRAFT=1`
and reads its prefs file from `$AUTOSTART_PREFS` (the check exits 3 without
it). Required prefs: `owner_login`, `bot_logins`, `org_members`,
`classified_repos`, `timezone`, `gates_dir`, `audit_log`, and `state_dir`.
With any one missing, the gate asks and the reason names the key.
`AUTOSTART_DRY=1` decides without starting.

To move a prefs file from the old names (`ryan_login`, `wes_logins`, the
`wes-autostart-draft-review.*` state files) run
`scripts/migrate-autostart-prefs.py`. It is idempotent.

PR updates (a PR that became ready, or moved past the owner's review) were retired
on 2026-11-19: bopeep-watch's CHANGED pings report them. The check exits 2 on
the old `--pr-updates` flag, so a caller left over from then fails loudly.

The unanswered-threads gate and the follow-up draft gate are not here. They
live in bopeep (`bin/threads-check`, `bin/followup-gate`).

Fixtures: `tests/test_autostart_draft_review.py` and
`tests/work-review-requests-check-fixtures/run_tests.py` (fake `gh` and
`guild-config`, no network).
