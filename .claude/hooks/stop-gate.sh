#!/usr/bin/env bash
# Stop gate for the solver harness.
#
# Enforces two things before Claude may end its turn:
#   1. If any Python file changed on this branch, the test suite must pass.
#   2. If core solver logic changed (app/solver.py, app/splitter.py,
#      app/split_solver.py, app/models.py), a new ADR must exist under
#      docs/decisions/ on this branch.
#
# Exit 2 blocks the stop and feeds stderr back to Claude as instructions.
# To avoid infinite loops, the gate yields after it has blocked once in the
# same stop cycle (stop_hook_active) — Claude gets one forced fix round.

set -u

input="$(cat)"
if printf '%s' "$input" | grep -q '"stop_hook_active"[[:space:]]*:[[:space:]]*true'; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0

# Changed files = committed-on-branch (vs merge-base with origin/main) + working tree.
base="$(git merge-base origin/main HEAD 2>/dev/null || true)"
changed="$(
  {
    [ -n "$base" ] && git diff --name-only "$base" HEAD 2>/dev/null
    git status --porcelain -uall 2>/dev/null | awk '{print $NF}'
  } | sort -u
)"

py_changed="$(printf '%s\n' "$changed" | grep -E '\.py$' || true)"
[ -z "$py_changed" ] && exit 0

# Gate 1: tests must pass.
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi
test_output="$("$PY" -m pytest -q --no-header 2>&1)"
if [ $? -ne 0 ]; then
  {
    echo "BLOCKED: the test suite is failing. Fix the failures (or explain to the user why they are pre-existing), then finish. Last lines of pytest output:"
    printf '%s\n' "$test_output" | tail -20
  } >&2
  exit 2
fi

# Gate 2: core logic changes require a new ADR on this branch.
core_changed="$(printf '%s\n' "$changed" | grep -E '^app/(solver|splitter|split_solver|models)\.py$' || true)"
adr_added="$(printf '%s\n' "$changed" | grep -E '^docs/decisions/.+\.md$' | grep -v 'TEMPLATE\.md' || true)"
if [ -n "$core_changed" ] && [ -z "$adr_added" ]; then
  {
    echo "BLOCKED: core solver logic changed ($(printf '%s' "$core_changed" | tr '\n' ' ')) but no ADR was added to docs/decisions/."
    echo "Write a mentor-style ADR now using the /adr skill (template: docs/decisions/TEMPLATE.md, in Traditional Chinese), then finish."
  } >&2
  exit 2
fi

exit 0
