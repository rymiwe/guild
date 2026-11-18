# Guild release plan

**Goal.** Update the Surface to one verified set of Guild tools without copying a command into a terminal for each release. Keep the Surface guard in place and keep the previous release usable for rollback.

**Status.** Ready to start the parent integration test. The current `pr-gate` migration has already landed in both repos and passed on BLD and the Surface.

| Step | Work | Done when |
| --- | --- | --- |
| 1. Test the cross-repo contract | Pin a known-good set of member commits in Guild. Install them into a temporary prefix and run `pr-gate` with the real `pr-lint` and `threads-check`. Use synthetic GitHub responses. | Guild CI sees a clear gate, a blocker URL, and an error when a helper is missing. The test never changes the host installation. |
| 2. Prepare and switch a release | Build the candidate from the pins in a separate directory. Check it before atomically switching one active pointer. Retain the prior release. | An interrupted or failed preparation leaves the old commands callable. A successful switch and rollback restore the expected commands and dependencies. |
| 3. Keep running jobs on their release | Resolve the concrete release path at job startup. Give child processes that release's commands on PATH. Define when the watcher restarts and when old releases can be removed. | An old job calling a helper after activation still gets its old helper. A new job gets the new helper. Restarting the watcher leaves running jobs intact. |
| 4. Roll out to the Surface | Verify the complete mechanism on BLD. After approval, enable the Surface-owned updater once. Publish only releases that pass Guild CI on their exact pins. | A routine release activates without a terminal command. A failed candidate leaves the previous release active and reports the error. |

## Scope

Member repos own their unit and component tests. Guild owns the installed command contracts and release lifecycle. Keep machine configuration, credentials, and live job state outside release directories. A change that makes shared state unreadable by the previous release needs its own rollout decision.

Do not add a rollout format or scheduler in step 1. The first deliverable is the integration test and its exact input pins. Record the test command and result in the change that closes each step.

## Open follow-ups

- The Quartermaster cgroup fix merged, but the Surface checkout still predates it. Include that fix or a descendant in the first Surface release and verify the installed launcher version.
- A statechart log write error in the inherited `pr-gate` can hide earlier gate results. Track that independently of release activation. The archived Conclave report is under `~/code/conclave/tmp/council/guild-pr-gate-move/`.
