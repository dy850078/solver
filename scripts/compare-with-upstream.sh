#!/usr/bin/env bash
# Review every difference between a production repo and scanned upstream,
# with a recommended action per file, before adopting anything.
#
# Usage (inside the production repo):
#   ./scripts/compare-with-upstream.sh [upstream_ref] [local_ref]
#     defaults: mirror/release-to-gitlab  master
#
# Adopting upstream is the default action for a reason: a file you forget
# to review keeps moving with upstream instead of going quietly stale. The
# report below is the place to spot the ones that must NOT move.

set -euo pipefail

UPSTREAM="${1:-mirror/release-to-gitlab}"
LOCAL="${2:-master}"

git rev-parse --verify "$UPSTREAM" >/dev/null 2>&1 || {
  echo "error: ref '$UPSTREAM' not found — did you 'git fetch mirror'?" >&2; exit 1; }
git rev-parse --verify "$LOCAL" >/dev/null 2>&1 || {
  echo "error: ref '$LOCAL' not found" >&2; exit 1; }

echo "upstream : $UPSTREAM  ($(git log -1 --format='%h %s' "$UPSTREAM" | cut -c1-60))"
echo "local    : $LOCAL  ($(git log -1 --format='%h %s' "$LOCAL" | cut -c1-60))"
echo

# --- files only in the local repo -------------------------------------
# Reported as D because the diff is upstream -> local; these are the
# "A-class" production-only files that must be carried across by hand.
echo "== 只存在生產端 — 必須手動帶過去 (A 類) =="
only_local=$(git diff --name-status "$UPSTREAM" "$LOCAL" | awk '$1=="A"{print $2}')
if [ -n "$only_local" ]; then
  echo "$only_local" | sed 's/^/   /'
  echo
  echo "   移植時: git checkout $LOCAL -- <上列檔案>"
else
  echo "   (無)"
fi
echo

# --- files only upstream ----------------------------------------------
echo "== 只存在 upstream — 採用即可,無決策 =="
only_up=$(git diff --name-status "$UPSTREAM" "$LOCAL" | awk '$1=="D"{print $2}')
if [ -n "$only_up" ]; then
  echo "$only_up" | sed 's/^/   /' | head -40
  n=$(echo "$only_up" | wc -l)
  [ "$n" -gt 40 ] && echo "   … 另有 $((n-40)) 個"
else
  echo "   (無)"
fi
echo

# --- files that differ -------------------------------------------------
echo "== 兩邊都有但內容不同 — 逐項決定 =="
printf "   %-52s %8s %8s\n" "檔案" "+生產" "-上游"
git diff --numstat "$UPSTREAM" "$LOCAL" | while read -r add del path; do
  [ "$add" = "-" ] && { printf "   %-52s %8s %8s\n" "$path" "binary" ""; continue; }
  printf "   %-52s %8s %8s\n" "$path" "$add" "$del"
done
echo
echo "   看單一檔案的差異:"
echo "     git diff $UPSTREAM $LOCAL -- <檔案>"
echo "   （+ 號是生產端獨有的內容,- 號是 upstream 的）"
echo

# --- external contract check ------------------------------------------
# Endpoint and env-var differences are the ones that fail in production
# rather than in the test run, so they get their own pass.
echo "== 對外契約交叉檢查 =="
for f in app/server.py; do
  git cat-file -e "$UPSTREAM:$f" 2>/dev/null || continue
  echo "   --- $f: endpoints"
  diff <(git show "$UPSTREAM:$f" | grep -oE '@(api|router)\.(get|post|put|delete)\("[^"]+"' | sort) \
       <(git show "$LOCAL:$f"    | grep -oE '@(api|router)\.(get|post|put|delete)\("[^"]+"' | sort) \
    | sed 's/^/     /' || true
  echo "   --- $f: environment variables"
  diff <(git show "$UPSTREAM:$f" | grep -oE '"[A-Z][A-Z0-9_]{2,}"' | sort -u) \
       <(git show "$LOCAL:$f"    | grep -oE '"[A-Z][A-Z0-9_]{2,}"' | sort -u) \
    | sed 's/^/     /' || true
done
echo
echo "   （'<' 是 upstream 才有,'>' 是生產端才有。生產端才有的 endpoint"
echo "     若不帶過去就會 404 —— probe 與呼叫端設定在這個 repo 之外。）"
