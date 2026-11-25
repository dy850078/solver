#!/usr/bin/env bash
# Stop gate for the solver harness.
#
# Enforces two things before Claude may end its turn:
#   1. If any Python file, web UI file (app/web_static/), JS test or example
#      request changed on this branch, the test suite must pass.
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

# The trunk this branch will merge into. SOLVER_TRUNK wins when set; else
# only `origin` is probed (a GitHub→GitLab mirror may be attached as a second
# remote and must never be mistaken for the trunk): the remote HEAD the
# server declares (git clone records it), then the names this project has
# used. No trunk → only the working tree is checked, and we say so.
trunk_ref() {
  if [ -n "${SOLVER_TRUNK:-}" ]; then
    git rev-parse --verify -q "$SOLVER_TRUNK" >/dev/null 2>&1 && { echo "$SOLVER_TRUNK"; return; }
    echo "stop-gate: SOLVER_TRUNK='$SOLVER_TRUNK' is not a ref in this clone" >&2
    return
  fi
  head="$(git symbolic-ref -q refs/remotes/origin/HEAD 2>/dev/null | sed 's#^refs/remotes/##')"
  for ref in "$head" origin/release-to-gitlab origin/main origin/master; do
    [ -n "$ref" ] || continue
    git rev-parse --verify -q "$ref" >/dev/null 2>&1 && { echo "$ref"; return; }
  done
}

# Changed files = committed-on-branch (vs merge-base with the trunk) + working tree.
trunk="$(trunk_ref)"
if [ -n "$trunk" ]; then
  base="$(git merge-base "$trunk" HEAD 2>/dev/null || true)"
else
  echo "stop-gate: no trunk ref found; only the working tree is checked (set SOLVER_TRUNK=<remote>/<branch>)" >&2
  base=""
fi
changed="$(
  {
    [ -n "$base" ] && git diff --name-only "$base" HEAD 2>/dev/null
    git status --porcelain -uall 2>/dev/null | awk '{print $NF}'
  } | sort -u
)"

# Code that the test suite covers: Python, the web UI (pytest drives the
# node unit tests and, where a browser is available, the smoke test) and
# the example requests the suite replays.
code_changed="$(printf '%s\n' "$changed" | grep -E '\.py$|^app/web_static/|^tests/js/|^examples/' || true)"
[ -z "$code_changed" ] && exit 0

# UI changes on a machine that has a browser configured (SOLVER_CHROMIUM):
# the node and browser tests must RUN, not skip. Without SOLVER_CHROMIUM
# they keep skipping, so the hook still works on a Python-only machine.
ui_changed="$(printf '%s\n' "$changed" | grep -E '^app/web_static/|^tests/js/|^tests/test_ui_' || true)"
if [ -n "$ui_changed" ] && [ -n "${SOLVER_CHROMIUM:-}" ]; then
  export SOLVER_REQUIRE_NODE=1 SOLVER_REQUIRE_BROWSER=1
fi

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
