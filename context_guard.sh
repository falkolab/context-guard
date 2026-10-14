#!/bin/sh
# Prefilter for PostToolUse: the fast path starts no process at all (only /bin/sh
# itself). It reads the skip counter and exits. Python runs only when context usage
# may have changed noticeably.
D="$HOME/.claude/checkpoints/.state"
in=''
l=''
while IFS= read -r l || [ -n "$l" ]; do
  in="$in$l"
  l=''
done
case "$in" in *'"session_id":"'*) ;; *) exit 0 ;; esac
rest=${in#*\"session_id\":\"}; sid=${rest%%\"*}
case "$sid" in ''|*[!A-Za-z0-9_.-]*) sid='' ;; esac
if [ -n "$sid" ] && [ -f "$D/$sid.skip" ]; then
  n=''
  read -r n < "$D/$sid.skip" 2>/dev/null
  case "$n" in ''|*[!0-9]*) n=0 ;; esac
  if [ "$n" -gt 0 ]; then
    printf '%s' "$((n-1))" > "$D/$sid.skip" 2>/dev/null
    exit 0
  fi
fi
printf '%s' "$in" | exec /usr/bin/python3 "$(dirname "$0")/context_guard.py"
