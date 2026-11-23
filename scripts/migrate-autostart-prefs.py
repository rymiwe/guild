#!/usr/bin/env python3
"""Migrate the autostart draft-review gate's prefs and state to the generic names. Idempotent.

The gate dropped its built-in owner login, host paths and time zone, and its
persona-named keys and files:

  prefs keys   ryan_login -> owner_login, wes_logins -> bot_logins
  state files  <state_dir>/wes-autostart-draft-review.{starts.json,heads.json,pings}
               -> <state_dir>/autostart-draft-review.*
  pause files  <gates_dir>/wes-autostart-draft-review.{PAUSED,paused}
               -> <gates_dir>/autostart-draft-review.*

The flags fill the keys that are now required, only where the prefs file lacks
them. Pass the values the gate used before (its old built-in defaults) to keep
its behaviour. An old key is renamed only when the new key is absent; a state
file is renamed only when the new file is absent. A second run changes nothing.

usage: migrate-autostart-prefs.py PREFS --state-dir DIR --gates-dir DIR --audit-log FILE --timezone ZONE [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

KEY_RENAMES = {"ryan_login": "owner_login", "wes_logins": "bot_logins"}
OLD_GATE, NEW_GATE = "wes-autostart-draft-review", "autostart-draft-review"
STATE_SUFFIXES = (".starts.json", ".heads.json", ".pings")
PAUSE_SUFFIXES = (".PAUSED", ".paused")


def migrate(prefs_path: Path, fills: dict, dry_run: bool = False) -> list[str]:
    prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
    changes = []
    prefs_changed = False
    for old, new in KEY_RENAMES.items():
        if old in prefs and new not in prefs:
            prefs[new] = prefs.pop(old)
            prefs_changed = True
            changes.append(f"prefs: {old} -> {new}")
        elif old in prefs:
            changes.append(f"prefs: both {old} and {new} set; left {old} for review")
    for key, value in fills.items():
        if not prefs.get(key):
            prefs[key] = value
            prefs_changed = True
            changes.append(f"prefs: set {key}")
    renames = []
    for dir_key, suffixes in (("state_dir", STATE_SUFFIXES), ("gates_dir", PAUSE_SUFFIXES)):
        root = Path(os.path.expanduser(str(prefs[dir_key])))
        for suffix in suffixes:
            src, dst = root / f"{OLD_GATE}{suffix}", root / f"{NEW_GATE}{suffix}"
            if src.exists() and not dst.exists():
                renames.append((src, dst))
                changes.append(f"file: {src} -> {dst}")
    if not dry_run:
        if prefs_changed:
            tmp = prefs_path.with_suffix(prefs_path.suffix + ".tmp")
            tmp.write_text(json.dumps(prefs, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            tmp.replace(prefs_path)
        for src, dst in renames:
            src.rename(dst)
    return changes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("prefs", type=Path)
    ap.add_argument("--state-dir", required=True)
    ap.add_argument("--gates-dir", required=True)
    ap.add_argument("--audit-log", required=True)
    ap.add_argument("--timezone", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    fills = {"state_dir": args.state_dir, "gates_dir": args.gates_dir,
             "audit_log": args.audit_log, "timezone": args.timezone}
    changes = migrate(args.prefs, fills, args.dry_run)
    for line in changes or ["nothing to migrate"]:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
