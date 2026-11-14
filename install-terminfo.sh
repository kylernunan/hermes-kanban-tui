#!/usr/bin/env bash
# install-terminfo.sh -- add the xterm-ghostty terminfo entry to ~/.terminfo.
#
# Ghostty exports TERM=xterm-ghostty by default. The stock ncurses-term package
# on this box ships a `ghostty` entry but not `xterm-ghostty`, so ncurses
# programs (including this TUI) die with "setupterm: could not find terminal".
#
# This aliases xterm-ghostty to the shipped ghostty entry in the user's own
# terminfo directory. Nothing system-wide is touched and no sudo is needed.
#
#   ./install-terminfo.sh          install or refresh
#   ./install-terminfo.sh --check  report state only
set -euo pipefail

TERM_NAME="xterm-ghostty"
SOURCE_NAME="ghostty"
DEST_DIR="${HOME}/.terminfo"
CHECK_ONLY=0

if [[ "${1:-}" == "--check" ]]; then
  CHECK_ONLY=1
fi

report() {
  printf '%-16s %s\n' "$1" "$2"
}

have_entry() {
  infocmp "$1" >/dev/null 2>&1
}

if have_entry "$TERM_NAME"; then
  report "$TERM_NAME" "present ($(infocmp -D 2>/dev/null | head -1))"
  exit 0
fi

report "$TERM_NAME" "missing"
if ! have_entry "$SOURCE_NAME"; then
  echo "error: no '$SOURCE_NAME' terminfo entry to copy from." >&2
  echo "       install the ncurses-term package, or set TERM=xterm-256color." >&2
  exit 1
fi
report "$SOURCE_NAME" "present (source)"

if [[ "$CHECK_ONLY" == "1" ]]; then
  echo
  echo "run ./install-terminfo.sh to install the alias into ${DEST_DIR}"
  exit 2
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# infocmp -1 emits one capability per line; rewrite the header so tic installs
# the same capabilities under the name Ghostty actually exports.
infocmp -1 "$SOURCE_NAME" \
  | sed -e "s/^${SOURCE_NAME}|.*,/${TERM_NAME}|Ghostty terminal (xterm-ghostty alias),/" \
        -e "s/^\t${SOURCE_NAME},$/\t${TERM_NAME},/" \
  > "$WORK/xterm-ghostty.src"

if ! grep -q "^${TERM_NAME}|" "$WORK/xterm-ghostty.src"; then
  echo "error: could not rewrite the terminfo header; nothing installed." >&2
  exit 1
fi

mkdir -p "$DEST_DIR"
tic -x -o "$DEST_DIR" "$WORK/xterm-ghostty.src"

if have_entry "$TERM_NAME"; then
  report "$TERM_NAME" "installed in ${DEST_DIR}"
  echo
  echo "verify:   TERM=${TERM_NAME} infocmp -1 ${TERM_NAME} | head -3"
  echo "rollback: rm -rf ${DEST_DIR}/x/xterm-ghostty ${DEST_DIR}/78/xterm-ghostty"
else
  echo "error: tic ran but the entry is still not resolvable." >&2
  echo "check that TERMINFO_DIRS or ~/.terminfo is readable by ncurses." >&2
  exit 1
fi
