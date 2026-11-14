#!/usr/bin/env python3
"""pty_drive.py -- run kanban_tui.py in a real pty and capture its frames.

Not a mock: it allocates a pty, sets the window size with TIOCSWINSZ, runs the
actual TUI, sends keystrokes, and keeps every byte the program wrote. Used to
prove the curses path works under a given TERM before claiming it does.

    python3 pty_drive.py --term xterm-ghostty --cols 160 --rows 48 --keys 'jj2' 
"""
from __future__ import annotations

import argparse
import fcntl
import os
import pty
import re
import select
import signal
import struct
import sys
import termios
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ANSI = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]|\x1b[()][A-Z0-9]|\x1b\][^\x07]*\x07|\x1b[=>]|\r")


def run(term, cols, rows, keys, extra_args, db=None, settle=1.6, total=14.0):
    argv = [sys.executable, os.path.join(HERE, "kanban_tui.py")]
    if db:
        argv += ["--db", db]
    argv += extra_args

    pid, fd = pty.fork()
    if pid == 0:
        os.environ["TERM"] = term
        for var in ("HERDR_ENV", "HERDR_PANE_ID"):
            os.environ.pop(var, None)
        os.execv(argv[0], argv)
        os._exit(127)

    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    out = bytearray()
    start = time.time()
    sent = False
    exit_code = None
    while time.time() - start < total:
        r, _, _ = select.select([fd], [], [], 0.15)
        if r:
            try:
                chunk = os.read(fd, 1 << 16)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
            continue
        if not sent and time.time() - start > settle:
            for ch in keys:
                os.write(fd, ch.encode())
                time.sleep(0.18)
            sent = True
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            exit_code = status
            break
    if exit_code is None:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        os.waitpid(pid, 0)
    try:
        os.close(fd)
    except OSError:
        pass
    return bytes(out), exit_code


def screen_text(raw):
    text = ANSI.sub(b"", raw).decode("utf-8", "replace")
    return text.replace("\x00", "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--term", default="xterm-256color")
    ap.add_argument("--cols", type=int, default=160)
    ap.add_argument("--rows", type=int, default=48)
    ap.add_argument("--keys", default="q")
    ap.add_argument("--db", default=None)
    ap.add_argument("--settle", type=float, default=1.6)
    ap.add_argument("--total", type=float, default=14.0)
    ap.add_argument("--args", default="", help="extra TUI args, shell-split")
    ap.add_argument("--raw-out", default=None, help="write captured bytes here")
    ap.add_argument("--show", action="store_true", help="print the captured text")
    ap.add_argument("--tail", type=int, default=4000)
    a = ap.parse_args()

    extra = a.args.split() if a.args else []
    raw, code = run(a.term, a.cols, a.rows, a.keys, extra, a.db, a.settle, a.total)
    if a.raw_out:
        with open(a.raw_out, "wb") as fh:
            fh.write(raw)
    text = screen_text(raw)
    print("term=%s size=%dx%d bytes=%d exit=%s" % (a.term, a.cols, a.rows, len(raw), code))
    print("saw_alt_screen=%s saw_colour=%s" % (
        b"\x1b[?1049h" in raw, b"\x1b[3" in raw or b"\x1b[4" in raw))
    if a.show:
        print("---- captured text ----")
        print(text[-a.tail:])
    return 0


if __name__ == "__main__":
    sys.exit(main())
