#!/usr/bin/env bash
# verify.sh -- run every check for kanban-tui and print a verdict.
#
#   ./verify.sh          unit tests + all probes
#   ./verify.sh --quick  unit tests only
#
# Exit status is the number of failed stages.
set -uo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"
PY="${KANBAN_TUI_PYTHON:-python3}"
QUICK=0
[[ "${1:-}" == "--quick" ]] && QUICK=1

fails=0
run() {
  local name="$1"; shift
  printf '\n=== %s ===\n' "$name"
  if "$@"; then
    printf -- '--- %s: PASS\n' "$name"
  else
    printf -- '--- %s: FAIL\n' "$name"
    fails=$((fails + 1))
  fi
}

run "unit tests" "$PY" -m unittest test_kanban_tui

if [[ "$QUICK" == "0" ]]; then
  run "snapshot at live size" "$PY" kanban_tui.py --print 160x48
  run "snapshot with history" "$PY" kanban_tui.py --print 160x48 --all
  run "pty: ghostty TERM, real size" \
      "$PY" pty_drive.py --term xterm-ghostty --cols 164 --rows 50 --keys jj2q
  run "pty: terminfo fallback" \
      env TERMINFO=/tmp/empty-ti "$PY" pty_drive.py --term xterm-ghostty --cols 100 --rows 28 --keys q --settle 1.2 --total 6
  run "live resize" "$PY" resize_probe.py
  run "live refresh" "$PY" refresh_probe.py
  run "mouse click" "$PY" mouse_probe.py
  run "comment action (board copy)" "$PY" comment_probe.py
  run "terminfo state" ./install-terminfo.sh --check \
      || true  # exit 2 means "not installed", which is a valid state
fi

printf '\n'
if [[ "$fails" == "0" ]]; then
  echo "ALL CHECKS PASSED"
else
  echo "$fails CHECK(S) FAILED"
fi
exit "$fails"
