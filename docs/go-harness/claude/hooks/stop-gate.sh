#!/usr/bin/env bash
# Stop gate for the go-scheduler harness.
#
# Enforces two things before Claude may end its turn:
#   1. If any Go file changed on this branch, build + vet + tests must pass.
#   2. If contract-critical code changed (see CORE_PATTERN), a new ADR must
#      exist under docs/decisions/ on this branch.
#
# Exit 2 blocks the stop and feeds stderr back to Claude as instructions.
# To avoid infinite loops, the gate yields after it has blocked once in the
# same stop cycle (stop_hook_active) — Claude gets one forced fix round.
#
# INSTALL: chmod +x .claude/hooks/stop-gate.sh and register it in
# .claude/settings.json (see docs/go-harness/README.md). Adjust CORE_PATTERN
# and DEFAULT_BRANCH to match this repo.

set -u

# --- repo-specific knobs ----------------------------------------------------
DEFAULT_BRANCH="origin/main"
# Files whose change demands an ADR: the solver contract wrapper, planning
# orchestration, candidate derivation, and DB migrations.
CORE_PATTERN='^(internal/solverclient/|internal/planning/|internal/scheduler/candidates|migrations/)'
# ---------------------------------------------------------------------------

input="$(cat)"
if printf '%s' "$input" | grep -q '"stop_hook_active"[[:space:]]*:[[:space:]]*true'; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0

# Changed files = committed-on-branch (vs merge-base with the default branch)
# + working tree.
base="$(git merge-base "$DEFAULT_BRANCH" HEAD 2>/dev/null || true)"
changed="$(
  {
    [ -n "$base" ] && git diff --name-only "$base" HEAD 2>/dev/null
    git status --porcelain -uall 2>/dev/null | awk '{print $NF}'
  } | sort -u
)"

go_changed="$(printf '%s\n' "$changed" | grep -E '\.go$' || true)"
[ -z "$go_changed" ] && exit 0

# Gate 1a: it must compile.
if ! build_output="$(go build ./... 2>&1)"; then
  {
    echo "BLOCKED: the build is failing. Fix it, then finish. Output:"
    printf '%s\n' "$build_output" | tail -20
  } >&2
  exit 2
fi

# Gate 1b: vet catches the mistakes the compiler allows.
if ! vet_output="$(go vet ./... 2>&1)"; then
  {
    echo "BLOCKED: go vet reported problems. Fix them, then finish. Output:"
    printf '%s\n' "$vet_output" | tail -20
  } >&2
  exit 2
fi

# Gate 1c: tests must pass. Integration tests needing a live solver should be
# behind a build tag so this stays fast and hermetic.
if ! test_output="$(go test ./... 2>&1)"; then
  {
    echo "BLOCKED: tests are failing. Fix the failures (or explain to the user why they are pre-existing), then finish. Last lines:"
    printf '%s\n' "$test_output" | tail -20
  } >&2
  exit 2
fi

# Gate 2: contract-critical changes require a new ADR on this branch.
core_changed="$(printf '%s\n' "$changed" | grep -E "$CORE_PATTERN" || true)"
adr_added="$(printf '%s\n' "$changed" | grep -E '^docs/decisions/.+\.md$' | grep -v 'TEMPLATE\.md' || true)"
if [ -n "$core_changed" ] && [ -z "$adr_added" ]; then
  {
    echo "BLOCKED: contract-critical code changed ($(printf '%s' "$core_changed" | tr '\n' ' ')) but no ADR was added to docs/decisions/."
    echo "Write a mentor-style ADR now using the /adr skill (template: docs/decisions/TEMPLATE.md, in Traditional Chinese), then finish."
  } >&2
  exit 2
fi

exit 0
