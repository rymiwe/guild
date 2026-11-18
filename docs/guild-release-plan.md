# Guild release plan

**Goal.** Update the Surface to one verified set of Guild tools without copying a command into a terminal for each release. Keep the Surface guard in place and keep the previous release usable for rollback.

**Status.** Step 1 complete locally (ready for Step 2). The cross-repo contract test pins the migrated baseline (`tests/member-pins.json`) and passes against the real installed commands in an isolated temporary prefix. Running `.github/workflows/ci.yml` on GitHub Actions requires configuring the `GUILD_READ_TOKEN` repository secret (`contents:read` on `bridgekeeper`, `bopeep`, `quartermaster`, and `conclave`).

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

- **Pins (`tests/member-pins.json`)**:
  - `bridgekeeper`: `a55d996336ecceed421c80637160d7a5742d8242`
  - `bopeep`: `94d07f8e3ec1eb4d7e8665c58be2938360c2c746`
  - `quartermaster`: `d47a182c8e5d03f95c681fcb4a51de34d690f8e3`
  - `conclave`: `79775e7806ddfc8db18a8c188a2a31a7af7429c7`
- **Command**: `python3 tests/test_pr_gate_contract.py -v`
- **Result**: `Ran 7 tests in 5.500s, OK` (installed wrappers, clear gate `pr-gate PASS`, blocker URL `FAIL: unanswered_threads: unanswered: https://github.com/AcmeCorp/widgets/pull/7#discussion_r456`, real `pr-lint` failure, missing `threads-check` error, missing `pr-lint` error, and unchanged host install paths; also verified against shallow detached-HEAD CI-style source clones with `GUILD_MEMBER_SOURCES_DIR`).
- **CI workflow**: `.github/workflows/ci.yml` checks out the exact SHAs from `tests/member-pins.json` using `secrets.GUILD_READ_TOKEN` (failing closed if unset) and runs `python3 tests/test_pr_gate_contract.py -v`.

## Open follow-ups

- The Quartermaster cgroup fix merged, but the Surface checkout still predates it. Include that fix or a descendant in the first Surface release and verify the installed launcher version.
- A statechart log write error in the inherited `pr-gate` can hide earlier gate results. Track that independently of release activation. The archived Conclave report is under `~/code/conclave/tmp/council/guild-pr-gate-move/`.
