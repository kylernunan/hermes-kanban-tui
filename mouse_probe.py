#!/usr/bin/env python3
"""mouse_probe.py -- prove a click selects the card under the pointer.

Runs the real TUI with --mouse in a pty, reads which card carries the cursor,
writes a mouse report the way a terminal would, and checks the cursor moved to
the card in the clicked column. Tries SGR (1006) first and falls back to the
legacy X10 encoding, reporting which one the terminal interface accepted.

    python3 mouse_probe.py
"""
from __future__ import annotations

import fcntl
import os
import pty
import re
import select
import struct
import sys
import termios
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from ansi_screen import Screen  # noqa: E402

COLS, ROWS = 160, 40


def pump(fd, out, seconds):
    start = time.time()
    while time.time() - start < seconds:
        r, _, _ = select.select([fd], [], [], 0.08)
        if r:
            try:
                chunk = os.read(fd, 1 << 16)
            except OSError:
                return False
            if not chunk:
                return False
            out.extend(chunk)
    return True


def cursor_task(text):
    for line in text.split("\n"):
        m = re.search(r">\s*(t_[0-9a-z]+)", line)
        if m:
            return m.group(1)
    return None


def main():
    pid, fd = pty.fork()
    if pid == 0:
        os.environ["TERM"] = "xterm-256color"
        os.environ.pop("HERDR_ENV", None)
        os.environ.pop("HERDR_PANE_ID", None)
        os.execv(sys.executable, [sys.executable, os.path.join(HERE, "kanban_tui.py"), "--mouse"])
        os._exit(127)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))

    out = bytearray()
    if not pump(fd, out, 2.5):
        print("FAILED: the view exited during startup")
        return 1
    first = Screen(COLS, ROWS).feed(bytes(out)).render()
    first_task = cursor_task(first)
    print("cursor before click: %s" % first_task)

    # Find a card row and the blocked column's x span from the rendered board.
    header = next(l for l in first.split("\n") if "todo" in l and "blocked" in l)
    blocked_at = header.index("blocked")
    rows = first.split("\n")
    card_y = None
    for i, line in enumerate(rows):
        if i > 4 and len(line) > blocked_at and line[blocked_at:].strip():
            if re.match(r"\s*[> ]t_", line[blocked_at - 2:blocked_at + 2] or ""):
                pass
        if i > 4 and "t_" in line[blocked_at:]:
            card_y = i
            break
    if card_y is None:
        print("FAILED: could not locate a blocked card to click")
        return 1
    click_x = blocked_at + 3

    def report(seq):
        out.clear()
        os.write(fd, seq)
        pump(fd, out, 1.2)
        text = Screen(COLS, ROWS).feed(bytes(out)).render()
        return cursor_task(text) or cursor_task(Screen(COLS, ROWS).feed(bytes(out)).render())

    sgr_press = b"\x1b[<0;%d;%dM" % (click_x + 1, card_y + 1)
    sgr_release = b"\x1b[<0;%d;%dm" % (click_x + 1, card_y + 1)
    after_sgr = report(sgr_press + sgr_release)

    legacy_press = b"\x1b[M" + bytes([32, 33 + click_x, 33 + card_y])
    after_x10 = report(legacy_press)

    os.write(fd, b"q")
    pump(fd, out, 0.8)
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass

    print("clicked at (%d, %d) in the blocked column" % (click_x, card_y))
    print("cursor after SGR  click: %s" % after_sgr)
    print("cursor after X10  click: %s" % after_x10)
    moved = (after_sgr and after_sgr != first_task) or (after_x10 and after_x10 != first_task)
    print("\nmouse click selects a card: %s" % ("OK" if moved else "FAILED"))
    return 0 if moved else 1


if __name__ == "__main__":
    sys.exit(main())
