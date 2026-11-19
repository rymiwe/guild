# Guild release plan

**Goal.** Update the Surface to one verified set of Guild tools without copying a command into a terminal for each release. Keep the Surface guard in place and keep the previous release usable for rollback.

**Status.** Steps 1 to 3 are done. Step 4 is done: the owner enabled the updater on the Surface on 2026-11-18, and its first run activated `g-086883398120` with the Quartermaster cgroup fix. Running jobs stay on the release they started on across a switch and a `bopeep-watch` restart, and `guild-release prune` removes only releases that no running process is bound to. Running `.github/workflows/ci.yml` on GitHub Actions requires the `GUILD_READ_TOKEN` repository secret (`contents:read` on `bridgekeeper`, `bopeep`, `quartermaster`, `conclave`, and `thresher`).

| Step | Work | Done when |
| --- | --- | --- |
| 1. Test the cross-repo contract | Pin a known-good set of member commits in Guild. Install them into a temporary prefix and run `pr-gate` with the real `pr-lint` and `threads-check`. Use synthetic GitHub responses. | Guild CI sees a clear gate, a blocker URL, and an error when a helper is missing. The test never changes the host installation. |
| 2. Prepare and switch a release | Build the candidate from the pins in a separate directory. Check it before atomically switching one active pointer. Retain the prior release. | An interrupted or failed preparation leaves the old commands callable. A successful switch and rollback restore the expected commands and dependencies. |
| 3. Keep running jobs on their release | Resolve the concrete release path at job startup. Give child processes that release's commands on PATH. Define when the watcher restarts and when old releases can be removed. | An old job calling a helper after activation still gets its old helper. A new job gets the new helper. Restarting the watcher leaves running jobs intact. |
| 4. Roll out to the Surface | Verify the complete mechanism on BLD. After approval, enable the Surface-owned updater once. Publish only releases that pass Guild CI on their exact pins. | A routine release activates without a terminal command. A failed candidate leaves the previous release active and reports the error. |

## Scope

Member repos own their unit and component tests. Guild owns the installed command contracts and release lifecycle. Keep machine configuration, credentials, and live job state outside release directories. A change that makes shared state unreadable by the previous release needs its own rollout decision.

Do not add a rollout format or scheduler in step 1. The first deliverable is the integration test and its exact input pins. Record the test command and result in the change that closes each step.

## Step 1 checks and pins

- **Pins**: `tests/member-pins.json` holds the member commits. CI and `guild-update` read that file; git history records each bump and why.
- **Command**: `python3 tests/test_pr_gate_contract.py -v`
- **Result**: `Ran 7 tests in 5.500s, OK` (installed wrappers, clear gate `pr-gate PASS`, blocker URL `FAIL: unanswered_threads: unanswered: https://github.com/AcmeCorp/widgets/pull/7#discussion_r456`, real `pr-lint` failure, missing `threads-check` error, missing `pr-lint` error, and unchanged host install paths; also verified against shallow detached-HEAD CI-style source clones with `GUILD_MEMBER_SOURCES_DIR`).
- **CI workflow**: `.github/workflows/ci.yml` checks out the exact SHAs from `tests/member-pins.json` using `secrets.GUILD_READ_TOKEN` (failing closed if unset) and runs `python3 tests/test_pr_gate_contract.py -v`.

## Step 2 checks and mechanism

- **Release tool (`bin/guild-release`)**: `prepare`, `check`, `switch`, `deploy`, `rollback`, and `status` manage releases under `--root`:
  - Builds candidate checkouts, `install.sh` prefixes, and release-bound `bin/` + `hooks/` launchers (with `bin/guild-exec` binding `PATH`, `CONCLAVE_HOME`, `BRIDGEKEEPER_HOME`, `BOPEEP_HOME`, and `QUARTERMASTER_HOME` to that concrete release while keeping `HOME`, `.guild.json`, and `bopeep.state_dir` external). pstack is not copied into a release: installers read the user's one checkout (`PSTACK_HOME`, default `~/code/cursor-plugins/pstack`), and `pstack-root` resolves it from `HOME` at run time.
  - Verifies checkout integrity, `install.sh --check`, and the installed `pr-gate` contract before promoting from `.staging/` and atomically replacing the `current` symlink while retaining `previous`.
- **Commands**: `python3 tests/test_pr_gate_contract.py -v && python3 tests/test_release_lifecycle.py -v`
- **Results**:
  - `python3 tests/test_pr_gate_contract.py -v`: `Ran 7 tests in 6.342s, OK`
  - `python3 tests/test_release_lifecycle.py -v`: `Ran 4 tests in 67.341s, OK` (external config/state separation and fail-closed initial rollback, interrupted preparation leaves `r1` active and callable, failed candidate check on broken `conclave/scripts/risk_freshness.py` and failing `--check-cmd` leaves `r1` active, and `r1` -> `r2` switch + `rollback` restores both commands such as `bopeep-v2-only`/`pr-lint` and dependencies such as `CONCLAVE_HOME/scripts/risk_freshness.py` without modifying guarded host paths).
- **CI workflow**: `.github/workflows/ci.yml` also runs `python3 tests/test_release_lifecycle.py -v` against the pinned `.deps` checkouts.

## Step 3 checks and mechanism

- **A job binds to one release when it starts.** A launcher reached through `current` resolves the link once, with `readlink -f`. Its `guild-exec` then puts that release's concrete `releases/<id>/bin` first on `PATH` and exports `GUILD_RELEASE_DIR`. Every child inherits that `PATH`, so each later helper call, by name or through a member-format host wrapper (`exec guild-exec TOOL PATH`), runs the same release. No member repo change was needed.
- **tmux jobs keep it too.** `job-launch` starts each job in a detached tmux session. A new session takes `PATH` from the tmux client, not from the server, so the job gets the launcher's release. Other variables come from the server's environment, which is that of the job that started the server. So a job's `GUILD_RELEASE_DIR` and `*_HOME` can name an older release than its `PATH`. Guild commands are unaffected, because `guild-exec` resets those variables from its own release. A process that reads them directly sees the older release. The test starts the tmux server with r1's `PATH` and variables and shows a job started from r2 still runs r2.
- **Rules for entry points.** Inside a job, call Guild commands by name, or through a host wrapper that dispatches with `guild-exec` on `PATH`. A direct path into `<root>/current/bin` or `<root>/current/hooks` resolves `current` at each call and does not stay bound. A job bound to a release that lacks a command fails closed: that release's `guild-exec` refuses it.
- **When `bopeep-watch` restarts.** A switch or rollback does not restart the watcher. A running watcher keeps starting jobs on the release it started on. The test shows this: the old watcher starts its job after the switch, and the job still runs r1. After a successful `switch` or `rollback`, run `systemctl --user try-restart bopeep-watch.service`. The step 4 updater owns that call. The unit sets `KillMode=process`, so the restart stops only the watcher process. Jobs already running in tmux keep running on their own release.
- **When an old release can be removed.** Never remove `current` or `previous`. Remove any other release only when no running process is bound to it. `guild-release status` reports `in_use` from `/proc`. A process counts for a release when its executable, working directory, a command-line argument, a `PATH` entry, `GUILD_RELEASE_DIR`, or a `*_HOME` variable that `guild-exec` sets lies in that release. Counting the inherited variables keeps a release that a job could still reach through them; it can hold an old release until the tmux sessions that inherited it exit. A tmux server, which leads its own session, is skipped: it keeps the environment of the job that started it but runs no Guild code. A tmux client still counts, so a session being started keeps its release. Processes of other users are not read. `guild-release prune [--dry-run]` removes each release that is not current, not previous, and not in use. A new job can only bind to `current`, or to a release that a running bound process already holds, so a release found unused stays unused.
- **Commands**: `python3 tests/test_pr_gate_contract.py -v && python3 tests/test_release_lifecycle.py -v`
- **Results**:
  - `python3 tests/test_pr_gate_contract.py -v`: `Ran 7 tests in 6.521s, OK`
  - `python3 tests/test_release_lifecycle.py -v`: `Ran 5 tests in 117.487s, OK`. The new `test_running_jobs_keep_their_release_across_switch_and_watcher_restart` runs the real `bopeep-watch --once` through `current/bin`. Its harvest command starts each job in a detached tmux session, as `job-launch` does. Watcher W1 starts on r1 and holds while `current` switches to r2, then starts job A. A restarted watcher W2 starts job B on r2. After a further switch to r3, each job calls `pr-lint` again, by name and through a host wrapper whose `guild-exec` links to `current`. Job A prints the r1 result (`pr-lint PASS`) on both calls, and job B prints the r2 result (`pr-lint PASS (v2)`) on both. The same host wrapper outside a job prints the r2 result. While A runs, `prune` keeps r1. Once A exits, `prune` removes r1, even though the tmux server still has r1 on its `PATH`.
- **CI workflow**: `.github/workflows/ci.yml` installs tmux before the lifecycle tests. The job binding test fails, rather than skips, when tmux is missing under `CI`.

## Step 4 checks and mechanism

- **Updater (`bin/guild-update`, `systemd/guild-update.{service,timer}`)**: every 15 minutes it fetches its own Guild checkout (`~/.local/share/guild/src`) and reads `origin/HEAD`. It installs release `g-<sha12>` only when the latest attempt of every push workflow run on that exact commit completed with success, skipped, or neutral. It waits while CI is queued or running, and fails after 24 hours of waiting. A run stops at 20 minutes.
  - It exports that commit to a candidate directory, fetches each pinned member under `~/code`, and runs the candidate's `guild-release deploy` (or `switch`, when an earlier run built the release but did not activate it). On success it records `<root>/installed/<id>`, runs `systemctl --user try-restart bopeep-watch.service` and `guild-release prune`, and moves its checkout to that commit.
  - A failed deploy writes `<root>/failed/<id>` and leaves the active release and the checkout in place. Later runs skip that commit until the next Guild commit; delete the file to retry it. A failed CI run is checked again each run, so a passing re-run recovers the commit.
  - Every failure exits 1 and sends one `job-notify --send` ping per commit and kind. A fetch or GitHub API error retries quietly; from the eighth in a row each run fails, with one ping for the streak.
  - The updater and host `guild-exec` run from that checkout, so a new version of either takes effect only with a release that passed CI and deployed.
  - `installed/` outlives `prune`. A commit installed once is never reinstalled, so a hand rollback or switch stands until the next Guild commit.
- **Host `guild-exec` (`bin/guild-exec-host`)**: member wrappers in `~/.local/bin` and `~/.claude/hooks` call `guild-exec TOOL PATH`. Linked as `~/.local/bin/guild-exec`, this sends a tool the active release carries to that release, and any other tool (for example `orderly`) to `~/code/quartermaster/bin/guild-exec`. With no release it sends everything there, so installing it first changes nothing.
- **BLD verification (2026-11-18)**: `guild-release deploy` into a scratch root with `tests/member-pins.json` built `bld1` in 8 s. Through `current/bin`, `pr-gate --repo rymiwe/guild --pr 6` printed the same eleven lines as the live `~/.local/bin/pr-gate`. A second release from member trunks switched and rolled back. The Quartermaster pin [d47a182](https://github.com/rymiwe/quartermaster/commit/d47a182c8e5d03f95c681fcb4a51de34d690f8e3) is the cgroup fix ([quartermaster#109](https://github.com/rymiwe/quartermaster/pull/109)).
- **Command**: `python3 tests/test_guild_update.py -v`. **Result**: `Ran 5 tests, OK` (waits on running or missing CI and fails once after 24 hours; a member fetch error retries quietly and fails from the eighth run in a row with one ping; failed CI pings once, installs nothing, and recovers when a re-run passes; a hand rollback stands after prune removes the rolled-away release; passed CI activates, a repeat run is a no-op, the host `guild-exec` routes release and non-release tools, and a CI-passed commit with an unfetchable pin leaves the release and the updater checkout where they were, then skips that commit).

### Enable on the Surface (owner runs, once)

```bash
gh repo clone rymiwe/guild ~/.local/share/guild/src
ln -sfn ~/.local/share/guild/src/bin/guild-exec-host ~/.local/bin/guild-exec   # no-op until a release exists
~/.local/share/guild/src/bin/guild-update && readlink ~/.local/share/guild/current
systemctl --user link ~/.local/share/guild/src/systemd/guild-update.service ~/.local/share/guild/src/systemd/guild-update.timer
systemctl --user enable --now guild-update.timer
```

Check the launcher version: `~/.local/share/guild/current/code/quartermaster` must contain [d47a182](https://github.com/rymiwe/quartermaster/commit/d47a182c8e5d03f95c681fcb4a51de34d690f8e3). Undo: `systemctl --user disable --now guild-update.timer`, then `ln -sfn ~/code/quartermaster/bin/guild-exec ~/.local/bin/guild-exec` and `systemctl --user try-restart bopeep-watch.service`.

## Every roster tool is in the release

`guild-release prepare` and `deploy` refuse a pins file that leaves out a tool named in [`roster`](../roster), unless `NOT_RELEASED` in `bin/guild-release` lists it with a reason. Only `orderly` is listed: it has no `guild-exec` commands. A release-bound job runs a host wrapper's `guild-exec TOOL` through its release, and that `guild-exec` refuses a tool the release lacks. On 2026-11-19 the Surface's scheduled `thresher` harvest exited 3 for that reason: `tests/member-pins.json` had no Thresher pin. `switch` and `rollback` still accept a release built before this rule.

## Normal release

1. Set the member's commit in `tests/member-pins.json` and merge to `main`.
2. CI checks out every pin and runs the three test files.
3. Within 15 minutes of CI passing, the Surface's `guild-update.timer` builds and activates `g-<sha12>` and restarts `bopeep-watch`.

A new roster tool also needs a pin, a checkout step in `.github/workflows/ci.yml`, and `contents:read` for `GUILD_READ_TOKEN`.

## Open follow-ups

- Passing `-e GUILD_RELEASE_DIR=...` and the `*_HOME` variables to `tmux new-session` in `job-launch` (quartermaster) would make a job's variables match its `PATH` and let `prune` free an old release sooner. Guild commands do not need it.

- A statechart log write error in the inherited `pr-gate` can hide earlier gate results. Track that independently of release activation. The archived Conclave report is under `~/code/conclave/tmp/council/guild-pr-gate-move/`.
