#!/usr/bin/env bash
# Routine sync: pull scanned upstream into a dated branch, verify it, stop.
#
#   ./scripts/sync-from-upstream.sh [options]
#
#     --mirror-ref REF   upstream ref to merge   (default mirror/release-to-gitlab)
#     --base REF         branch to build on      (default origin/master)
#     --branch NAME      sync branch name        (default sync/YYYY-MM-DD)
#     --verify-only      skip fetch+merge, just re-run the checks on HEAD
#     --no-smoke         skip the live server smoke test
#     --keep-going       run every check even after one fails
#     --log-file PATH    sync-log path, repo root  (default SYNC_LOG.md)
#     --no-log           do not append a sync-log entry
#
# It never pushes. Merging the resulting MR into master is what deploys to
# production, so the decision to push stays with a human — this script's job
# is to make that decision an informed one.

set -uo pipefail

MIRROR_REF="mirror/release-to-gitlab"
BASE_REF="origin/master"
SYNC_BRANCH=""
RUN_SMOKE=1
KEEP_GOING=0
VERIFY_ONLY=0
WRITE_LOG=1
SYNC_LOG="SYNC_LOG.md"
LOG_MAX=50
SMOKE_PORT="${SMOKE_PORT:-55051}"
CLI_EXAMPLE="examples/success_basic.json"

while [ $# -gt 0 ]; do
  case "$1" in
    --mirror-ref) MIRROR_REF="$2"; shift 2 ;;
    --base)       BASE_REF="$2";   shift 2 ;;
    --branch)     SYNC_BRANCH="$2"; shift 2 ;;
    --verify-only) VERIFY_ONLY=1; shift ;;
    --no-smoke)   RUN_SMOKE=0; shift ;;
    --keep-going) KEEP_GOING=1; shift ;;
    --log-file)   SYNC_LOG="$2"; shift 2 ;;
    --no-log)     WRITE_LOG=0; shift ;;
    -h|--help)    sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

BOLD=$(tput bold 2>/dev/null || true); RESET=$(tput sgr0 2>/dev/null || true)
RED=$(tput setaf 1 2>/dev/null || true); GREEN=$(tput setaf 2 2>/dev/null || true)
YELLOW=$(tput setaf 3 2>/dev/null || true)

# One scratch dir for every captured log, cleaned up on any exit path — the
# script has several (die, finish, conflict), and each one leaking its own
# /tmp file is how a machine ends up with a year of sync logs.
WORK=$(mktemp -d "${TMPDIR:-/tmp}/sync-from-upstream.XXXXXX") || exit 1
SERVER_PID=""
cleanup() {
  [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null
  rm -rf "$WORK"
}
trap cleanup EXIT

# Set before any code path that can reach finish(), which prints it as the
# branch to switch back to.
CURRENT_BRANCH=""
FAILURES=()
step()  { printf '\n%s== %s%s\n' "$BOLD" "$1" "$RESET"; }
ok()    { printf '   %s✓%s %s\n' "$GREEN" "$RESET" "$1"; }
warn()  { printf '   %s!%s %s\n' "$YELLOW" "$RESET" "$1"; }
fail()  { printf '   %s✗%s %s\n' "$RED" "$RESET" "$1"; FAILURES+=("$1"); }
die()   { printf '\n%serror:%s %s\n' "$RED" "$RESET" "$1" >&2; exit 1; }

# Under --verify-only the run starts on the sync branch, so there is no
# earlier branch to go back to and nothing this script created to delete.
backout_hint() {
  [ "$CURRENT_BRANCH" = "$SYNC_BRANCH" ] && return 0
  printf '%sgit switch %s && git branch -D %s\n' "$1" "$CURRENT_BRANCH" "$SYNC_BRANCH"
}

finish() {
  printf '\n%s== Summary%s\n' "$BOLD" "$RESET"
  if [ ${#FAILURES[@]} -eq 0 ]; then
    printf '   %sAll checks passed.%s  branch: %s\n\n' "$GREEN" "$RESET" "$SYNC_BRANCH"
    printf '   Nothing has been pushed. When you are ready:\n\n'
    printf '     %sgit push origin %s%s\n\n' "$BOLD" "$SYNC_BRANCH" "$RESET"
    printf '   then open the MR into master in GitLab — %smerging it deploys%s.\n' "$BOLD" "$RESET"
    [ "$WRITE_LOG" = 1 ] \
      && printf '   The MR touches %s at the root, so the pipeline will fire.\n' "$SYNC_LOG"
    backout_hint "   To back out instead:  "
    exit 0
  fi
  printf '   %s%s check(s) failed:%s\n' "$RED" "${#FAILURES[@]}" "$RESET"
  for f in "${FAILURES[@]}"; do printf '     - %s\n' "$f"; done
  printf '\n   %sDo not push.%s You are left on %s with the merge in place,\n' \
    "$BOLD" "$RESET" "$SYNC_BRANCH"
  printf '   so you can reproduce the failure and fix it here.\n'
  backout_hint "   Discard it instead with:  "
  exit 1
}

# A failed check stops the run unless --keep-going: once the tree is suspect,
# later results describe a state nobody will ship anyway.
guard() { [ "$KEEP_GOING" = 1 ] || [ ${#FAILURES[@]} -eq 0 ] || finish; }

cd "$(git rev-parse --show-toplevel 2>/dev/null)" || die "not inside a git repository"

# ---------------------------------------------------------------- preflight
step "Preflight"

git remote get-url "${MIRROR_REF%%/*}" >/dev/null 2>&1 \
  || die "remote '${MIRROR_REF%%/*}' not configured — see docs/prod-sync-runbook.md §0.1"
ok "remote '${MIRROR_REF%%/*}' present"

# Tracked changes are a hard stop: they would ride along into the sync branch
# and end up in the MR. Untracked files only warn — they survive the switch
# untouched, but they can still make `make test` pass for the wrong reason.
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  git status --short --untracked-files=no | sed 's/^/     /'
  die "tracked files modified — commit or stash first, or they ride along into the sync branch"
fi
ok "no uncommitted changes to tracked files"

UNTRACKED=$(git status --porcelain --untracked-files=normal | grep -c '^??')
[ "$UNTRACKED" -gt 0 ] && warn "$UNTRACKED untracked path(s) present — they stay put, but can skew test results"

# On a detached HEAD --abbrev-ref prints the literal "HEAD", which is not
# something `git switch` can take you back to; fall back to the commit.
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
[ "$CURRENT_BRANCH" = "HEAD" ] && CURRENT_BRANCH=$(git rev-parse --short HEAD)

if [ "$VERIFY_ONLY" = 1 ]; then
  : "${SYNC_BRANCH:=$CURRENT_BRANCH}"
  [ "$SYNC_BRANCH" = "$CURRENT_BRANCH" ] \
    || git switch "$SYNC_BRANCH" --quiet \
    || die "could not switch to '$SYNC_BRANCH'"
  ok "verify-only: checking '$SYNC_BRANCH' as it stands, no fetch or merge"
else
  : "${SYNC_BRANCH:=sync/$(date +%Y-%m-%d)}"
  if git show-ref --verify --quiet "refs/heads/$SYNC_BRANCH"; then
    die "branch '$SYNC_BRANCH' already exists — re-check it with --verify-only, delete it (git branch -D $SYNC_BRANCH), or pass --branch"
  fi
  ok "branch name '$SYNC_BRANCH' available"
  ok "starting from '$CURRENT_BRANCH' (the sync branch is built on $BASE_REF regardless)"
fi

# -------------------------------------------------------- fetch and merge
fetch_and_merge() {
  step "Fetch"

  git fetch "${MIRROR_REF%%/*}" --quiet || die "fetch from '${MIRROR_REF%%/*}' failed"
  git fetch origin --quiet              || die "fetch from 'origin' failed"
  git rev-parse --verify "$MIRROR_REF" >/dev/null 2>&1 || die "ref '$MIRROR_REF' not found after fetch"
  git rev-parse --verify "$BASE_REF"   >/dev/null 2>&1 || die "ref '$BASE_REF' not found after fetch"
  ok "$MIRROR_REF  $(git log -1 --format='%h %s' "$MIRROR_REF" | cut -c1-58)"
  ok "$BASE_REF  $(git log -1 --format='%h %s' "$BASE_REF" | cut -c1-58)"

  if git merge-base --is-ancestor "$MIRROR_REF" "$BASE_REF"; then
    printf '\n%sNothing to sync%s — %s already contains every commit from %s.\n' \
      "$BOLD" "$RESET" "$BASE_REF" "$MIRROR_REF"
    printf 'If you expected changes, check that the GitLab pipeline has run and\n'
    printf 'pushed the latest %s into the mirror.\n' "${MIRROR_REF#*/}"
    exit 0
  fi

  INCOMING=$(git log --oneline "$BASE_REF..$MIRROR_REF" | wc -l | tr -d ' ')
  printf '\n   %s%s incoming commit(s)%s — this is what an MR would deploy:\n' \
    "$BOLD" "$INCOMING" "$RESET"
  git log --oneline --no-decorate "$BASE_REF..$MIRROR_REF" | head -30 | sed 's/^/     /'
  [ "$INCOMING" -gt 30 ] && printf '     … and %s more\n' "$((INCOMING - 30))"

  step "Merge into $SYNC_BRANCH"

  git switch -c "$SYNC_BRANCH" "$BASE_REF" --quiet \
    || die "could not create '$SYNC_BRANCH' from $BASE_REF"
  ok "created from $BASE_REF"

  if git merge --no-edit "$MIRROR_REF" >"$WORK/merge.log" 2>&1; then
    ok "$(tail -1 "$WORK/merge.log" | cut -c1-70)"
    return 0
  fi

  sed 's/^/     /' "$WORK/merge.log"
  printf '\n%smerge conflict%s. Resolve it, `git add` the files and `git commit`,\n' "$RED" "$RESET"
  printf 'then run the checks with:  ./scripts/sync-from-upstream.sh --verify-only\n'
  printf 'Or abandon:  git merge --abort && git switch %s && git branch -D %s\n' \
    "$CURRENT_BRANCH" "$SYNC_BRANCH"
  printf '\nA conflict means upstream touched a file this repo also customises —\n'
  printf 'worth understanding rather than resolving on autopilot.\n'
  exit 1
}

if [ "$VERIFY_ONLY" = 1 ]; then
  # $BASE_REF still drives the contract diff below, so it has to be current.
  git fetch origin --quiet || die "fetch from 'origin' failed"
else
  fetch_and_merge
fi

# ---------------------------------------------------------------- sync log
# The deploy pipeline triggers on a change at the repository root, and a sync
# merge on its own may touch nothing there — upstream could have changed only
# app/ or tests/. Appending here guarantees the MR always has a root-level
# change, and the file doubles as the deploy history: one entry, one MR, one
# deploy.
#
# It lives only in this repo, never upstream, so it never conflicts (git keeps
# a file only one side ever touched). It does mean the repo is permanently
# non-identical to the mirror — see the Phase 4 note in the runbook.
write_sync_log() {
  [ "$WRITE_LOG" = 1 ] || return 0
  step "Sync log ($SYNC_LOG)"

  # An entry for this sync is already committed if the file differs from base.
  # Keeps repeated --verify-only runs from stacking duplicate entries.
  if ! git diff --quiet "$BASE_REF" -- "$SYNC_LOG"; then
    ok "already carries an entry for this sync — not appending a second one"
    return 0
  fi

  if [ ! -f "$SYNC_LOG" ]; then
    cat > "$SYNC_LOG" <<'HDR'
# Sync log

Append-only record of upstream syncs into this repository. One entry is one
MR into `master`, which is one deploy.

Written by `scripts/sync-from-upstream.sh`; it also guarantees every sync MR
touches a file at the repository root, which is what the deploy pipeline
triggers on.
HDR
    ok "created $SYNC_LOG"
  fi

  local count
  count=$(git log --no-merges --oneline "$BASE_REF..HEAD" | wc -l | tr -d ' ')
  {
    printf '\n## %s — %s\n\n' "$(date +'%Y-%m-%dT%H:%M:%S%z')" "$SYNC_BRANCH"
    printf -- '- upstream: %s @ %s\n' "$MIRROR_REF" \
      "$(git rev-parse --short "$MIRROR_REF" 2>/dev/null || echo 'unknown')"
    printf -- '- base:     %s @ %s\n' "$BASE_REF" "$(git rev-parse --short "$BASE_REF")"
    printf -- '- %s commit(s):\n' "$count"
    git log --no-merges --format='  - %h %s' "$BASE_REF..HEAD" | head -"$LOG_MAX"
    [ "$count" -gt "$LOG_MAX" ] && printf -- '  - … and %s more\n' "$((count - LOG_MAX))"
  } >> "$SYNC_LOG"

  git add -- "$SYNC_LOG" || { fail "could not stage $SYNC_LOG"; return 0; }
  if git commit -qm "Record upstream sync into $SYNC_BRANCH"; then
    ok "appended $count-commit entry, committed as $(git rev-parse --short HEAD)"
  else
    fail "could not commit $SYNC_LOG"
  fi
}

write_sync_log
guard

# ------------------------------------------------------------------ verify
step "Install dependencies"
if [ -n "${PIP_INDEX_URL:-}" ] && [ -z "${UV_INDEX_URL:-}" ] && [ -z "${UV_DEFAULT_INDEX:-}" ]; then
  warn "PIP_INDEX_URL set without UV_INDEX_URL — uv would resolve against public PyPI"
fi
if make install >"$WORK/install.log" 2>&1; then
  ok "make install"
else
  tail -15 "$WORK/install.log" | sed 's/^/     /'
  fail "make install"
fi
guard

step "Test suite"
if make test >"$WORK/test.log" 2>&1; then
  ok "$(grep -oE '[0-9]+ passed[^=]*' "$WORK/test.log" | tail -1 | sed 's/[[:space:],]*$//')"
else
  grep -E "^(FAILED|ERROR)" "$WORK/test.log" | head -10 | sed 's/^/     /'
  tail -3 "$WORK/test.log" | sed 's/^/     /'
  fail "make test"
fi
guard

step "CLI end-to-end"
PY=".venv/bin/python"
if [ ! -x "$PY" ]; then
  fail "no $PY — install step must have failed"
else
  OUT=$("$PY" -m app.server --cli --input "$CLI_EXAMPLE" 2>/dev/null)
  STATUS=$(printf '%s' "$OUT" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["solver_status"])' 2>/dev/null)
  case "$STATUS" in
    OPTIMAL|FEASIBLE) ok "$CLI_EXAMPLE → $STATUS" ;;
    "")               fail "CLI produced no parsable result for $CLI_EXAMPLE" ;;
    *)                fail "$CLI_EXAMPLE → $STATUS (expected OPTIMAL/FEASIBLE)" ;;
  esac
fi
guard

if [ "$RUN_SMOKE" = 1 ] && [ -x "$PY" ]; then
  step "Live server smoke test (port $SMOKE_PORT)"
  "$PY" -m app.server --port "$SMOKE_PORT" >"$WORK/server.log" 2>&1 &
  SERVER_PID=$!

  for _ in $(seq 1 30); do
    curl -sf --noproxy '*' --max-time 2 "http://127.0.0.1:$SMOKE_PORT/health" >/dev/null 2>&1 && break
    sleep 1
  done

  HEALTH=$(curl -s --noproxy '*' --max-time 5 "http://127.0.0.1:$SMOKE_PORT/health" 2>/dev/null)
  # The probe contract lives in deployment manifests this repo cannot see;
  # a changed shape here is a failed liveness check there.
  if [ "$HEALTH" = '{"status":"ok"}' ]; then
    ok "GET /health → $HEALTH"
  else
    fail "GET /health → '${HEALTH:-<no response>}' (expected {\"status\":\"ok\"})"
    tail -5 "$WORK/server.log" | sed 's/^/     /'
  fi

  SOLVE=$(curl -s --noproxy '*' --max-time 30 -X POST \
            "http://127.0.0.1:$SMOKE_PORT/v1/placement/solve" \
            -H 'Content-Type: application/json' -d @"$CLI_EXAMPLE" 2>/dev/null \
          | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["solver_status"])' 2>/dev/null)
  case "$SOLVE" in
    OPTIMAL|FEASIBLE) ok "POST /v1/placement/solve → $SOLVE" ;;
    *)                fail "POST /v1/placement/solve → '${SOLVE:-<no response>}'" ;;
  esac

  kill "$SERVER_PID" 2>/dev/null; wait "$SERVER_PID" 2>/dev/null
  SERVER_PID=""
fi

# -------------------------------------------------------- contract changes
step "External contract changes vs $BASE_REF"
# Endpoints and env vars are configured outside this repo (probes, manifests,
# callers), so a change here needs a matching change over there.
EP_DIFF=$(diff \
  <(git show "$BASE_REF:app/server.py" 2>/dev/null | grep -oE '@api\.(get|post|put|delete)\("[^"]+"' | sort) \
  <(grep -oE '@api\.(get|post|put|delete)\("[^"]+"' app/server.py | sort) || true)
ENV_DIFF=$(diff \
  <(git show "$BASE_REF:app/server.py" 2>/dev/null | grep -oE 'environ(\.get)?\(?"[A-Z][A-Z0-9_]+"' | sort -u) \
  <(grep -oE 'environ(\.get)?\(?"[A-Z][A-Z0-9_]+"' app/server.py | sort -u) || true)

if [ -z "$EP_DIFF" ]; then
  ok "no endpoint changes"
else
  warn "endpoints changed ('<' removed, '>' added):"
  printf '%s\n' "$EP_DIFF" | sed 's/^/     /'
fi
if [ -z "$ENV_DIFF" ]; then
  ok "no environment-variable changes"
else
  warn "environment variables changed — deployment config may need updating:"
  printf '%s\n' "$ENV_DIFF" | sed 's/^/     /'
fi

if ! git diff --quiet "$BASE_REF" -- pyproject.toml uv.lock; then
  warn "pyproject.toml / uv.lock changed — production venv needs a reinstall after deploy"
fi

finish
