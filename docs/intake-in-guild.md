# Teammate PR intake as part of a team harness

Status: proposed, not built. Nothing here is live.

**Goal.** Any developer on a team can run PR intake from Guild with their own
login, without the first developer's files or help.

## Decision

Guild defines the job. A scheduler only starts it.

| Part | Owns |
| --- | --- |
| Bopeep | candidate search, eligibility, holds, reservation, receipts, and the runbook for judgment steps |
| Quartermaster, Conclave, Bridgekeeper | job launch, review, posting gates, as today |
| `.guild.json` | repos, channel, team-request opt-in |
| T3 automation, one per developer | the schedule and a visible thread |
| Chat bot | optional notifications, never dispatch |

The automation's prompt has no rules of its own. It runs the Bopeep command and
follows the runbook from the same release, in a fresh session on each run.
`docs/guild-triage.md` already uses this pattern. Another scheduler can start
the same command and runbook.

## Who picks up what

- **By default,** a developer's intake takes PRs that request them by name and
  new heads of PRs they reviewed.
- **Team requests are opt-in,** through `team_requests`, off by default. One
  developer per team turns it on. That avoids a shared claim service.
- **Named requests alone are not enough for that developer.** On 2026-11-21,
  9 of 11 open requests were to a team only. 16 of the 20 PRs in the intake
  ledger had a team request and never named the developer.
- **The limit:** nothing detects two developers who both opt in. Add a shared
  claim when a team needs two. GitHub's team review assignment is the other
  route. It changes routing for the whole team, so that team decides it.

## Why code does not replace the agent

A read-only script ran the existing searches, follow-through and preflight on
37 live PRs, twice, with the same result. The pass bar was set before the run.

| Script | PRs | Against the agent loop, which launched none |
| --- | --- | --- |
| skip | 8 | agrees |
| hold, head reserved | 6 | agrees |
| hold, required checks unavailable | 17 | agrees, but not for the owner reason |
| launch | 6 | disagrees |

- The likely cause of the six is owner exclusions that exist only in prose
  (inferred). Holds and owners must become data that the command reads.
- The snapshot had no correct launch, so it never showed a true positive.
- Two of the loop's last five runs resumed or posted held work. The script
  could only report a hold for those.
- The three idle runs used 12% of the loop's input tokens, so skipping them in
  code saves little.

## What blocks a second developer today

Checked 2026-11-21, partly by a deploy into an empty home directory.

- The member repos that a release installs are private to one account.
- The deploy itself works, in 8 seconds, given those repos.
- Without a GitHub login, `bopeep` prints a traceback.
- `bopeep-watch --help` starts the watcher.
- The intake helpers and runbook are private, unversioned files with one
  login, one channel and a repo list written into them.
- T3 0.1.9 has no schedule field in `t3.json` and no schedule command. Setup
  needs the app or an agent's `schedule_task` call.

## Order

This follows the [build order](https://github.com/rymiwe/guild/issues/30) and
does not change it.

1. Finish the ledger steps that precede the second-developer install.
2. Install: grant repo access, and fail with the name of each missing login or
   config key.
3. Move the helpers and runbook into Bopeep, with fixture tests for identity,
   holds, the team opt-in and a double run. Replace the private copies in the
   same change.
4. Document one prompt that a developer gives their agent to create or update
   the automation. Add a setup command only if that proves unreliable.
5. The intake owner points the existing automation at the new command, then
   checks one request and one re-review end to end. On failure, point it back.

**Done when** a second developer goes from no install to a correct dry run in
under 30 minutes, without the first developer's files or help.

## Not building

- A timer that replaces the agent, or a watcher hook that dispatches reviews.
- A shared claim service or a setup skill, until the limits above are hit.
- A revived PR poller or the paused bot routine that can start reviews. Either
  would be a second dispatcher. Bopeep's change ping stays a notification.
