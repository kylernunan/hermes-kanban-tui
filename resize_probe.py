#!/usr/bin/env python3
"""resize_probe.py -- prove the TUI survives a live window resize.

Allocates a pty, starts the view, sends SIGWINCH with a new size mid-run, and
reports what the program wrote afterwards plus the reconstructed screen.

    python3 resize_probe.py
"""
from __future__ import annotations

import fcntl
import os
import pty
import select
import signal
import struct
import sys
import termios
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from ansi_screen import Screen  # noqa: E402


def set_size(fd, cols, rows):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def main():
    sizes = [(160, 46), (110, 30), (70, 22), (150, 40)]
    pid, fd = pty.fork()
    if pid == 0:
        os.environ["TERM"] = "xterm-ghostty"
        os.environ.pop("HERDR_ENV", None)
        os.environ.pop("HERDR_PANE_ID", None)
        os.execv(sys.executable, [sys.executable, os.path.join(HERE, "kanban_tui.py")])
        os._exit(127)
    set_size(fd, *sizes[0])

    out = bytearray()
    # First mark is the initial geometry: the app's first paint belongs to that
    # size, and diffing means later segments only carry the rows that changed.
    marks = [(0, sizes[0][0], sizes[0][1])]
    start = time.time()
    step = 0
    exited = None
    while time.time() - start < 24:
        r, _, _ = select.select([fd], [], [], 0.1)
        if r:
            try:
                chunk = os.read(fd, 1 << 16)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
            continue
        elapsed = time.time() - start
        if step < len(sizes) and elapsed > 3.0 + step * 2.5:
            cols, rows = sizes[step]
            if (cols, rows) != (marks[-1][1], marks[-1][2]):
                set_size(fd, cols, rows)
                os.kill(pid, signal.SIGWINCH)
                marks.append((len(out), cols, rows))
            step += 1
        if step == len(sizes) and elapsed > 3.0 + len(sizes) * 2.5:
            os.write(fd, b"q")
            step += 1
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            exited = status
            break
    if exited is None:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
    try:
        os.close(fd)
    except OSError:
        pass

    # Each segment starts at a resize mark: the program erases and repaints on
    # resize, so bytes before the mark belong to the previous geometry.
    ok = True
    print("captured %d bytes across %d resizes" % (len(out), len(marks)))
    for i, (offset, cols, rows) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(out)
        # Diffing means a segment alone is not a full screen, so replay from the
        # start of that geometry's repaint, which is byte 0 for the first one.
        seg = bytes(out[offset:end])
        text = Screen(cols, rows).feed(seg).render()
        if not seg:
            print("  at %-9s no bytes for this geometry" % ("%dx%d" % (cols, rows)))
            ok = False
            continue
        lines = text.split("\n")
        widest = max((len(l) for l in lines), default=0)
        has_board = "KANBAN" in text
        overflow = widest > cols
        print("  at %-9s segment=%-6d rows_seen=%d widest=%d overflows=%s board_drawn=%s"
              % ("%dx%d" % (cols, rows), len(seg), len(lines), widest, overflow, has_board))
        if not has_board or overflow:
            ok = False
    final = Screen(sizes[-1][0], sizes[-1][1]).feed(bytes(out)).render()
    print("\nfinal screen at %dx%d:" % sizes[-1])
    print(final[:1200])
    print("\nresize handling: %s" % ("OK" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
