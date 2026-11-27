#!/usr/bin/env bash
# Run one Bridgekeeper posting suite against the pinned Conclave (Guild CI parity).
# usage: tests/run-bridgekeeper-posting.sh TEST_NAME
# Requires GUILD_MEMBER_SOURCES_DIR with bridgekeeper and conclave at the pins
# under test. Exit 77 (suite skip) is a failure here: required checks must run.
set -euo pipefail
name=${1:?test name}
sources=${GUILD_MEMBER_SOURCES_DIR:?GUILD_MEMBER_SOURCES_DIR required}
bk=$sources/bridgekeeper
conclave=$sources/conclave
script=$bk/tests/$name.sh
if [ ! -f "$script" ]; then
  echo "run-bridgekeeper-posting: missing $script" >&2
  exit 1
fi
if [ ! -f "$conclave/scripts/check-comment-labels.py" ]; then
  echo "run-bridgekeeper-posting: pinned conclave missing label checker at $conclave" >&2
  exit 1
fi
command -v gitleaks >/dev/null || {
  echo "run-bridgekeeper-posting: gitleaks required (Guild CI installs it)" >&2
  exit 1
}
export CONCLAVE_HOME=$conclave
set +e
bash "$script"
rc=$?
set -e
if [ "$rc" -eq 77 ]; then
  echo "run-bridgekeeper-posting: required suite $name skipped (exit 77); refusing pass" >&2
  exit 1
fi
exit "$rc"
