# jev-router (shadow only)

`bin/jev-router shadow` asks Jev whether a routine about to retry the same
failure should `continue`, `stop_retry` or `ask_human`. It writes the pick as a
JSON line to `~/.local/state/guild/jev-router/decisions.jsonl` with
`"acted": false` and does nothing else. It exits 0 on every path, prints
nothing to stdout, and logs a given routine, key and attempt once.

- **Kill switch:** create `~/.config/guild/jev-router.off`. The router then
  returns before it reads anything or calls Jev.
- **Wired to:** bopeep's merge-gate retry, where a gate run that gave no
  verdict is run again on the same head.
- **Acting:** no code path acts on a pick. After one week of logs, compare the
  picks with what happened before giving it any action.
