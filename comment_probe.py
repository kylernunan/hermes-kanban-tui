#!/usr/bin/env python3
"""comment_probe.py -- prove the comment action writes, and only where told.

Copies the live board, points both the view and the kanban CLI at the copy via
--db / HERMES_KANBAN_DB, then types a comment through the real TUI in a pty and
reads it back out of the copy.

The live board's bytes are hashed before and after: a write that leaked into the
real board shows up as a hash change, not as a claim in a summary.

    python3 comment_probe.py
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import pty
import re
import select
import shutil
import sqlite3
import struct
import sys
import tempfile
import termios
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from ansi_screen import Screen  # noqa: E402

COLS, ROWS = 150, 40
TEXT = "kanban-tui probe: comment action check"


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def comment_count(db, task_id):
    conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM task_comments WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
    finally:
        conn.close()


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


def main():
    live = os.path.expanduser("~/.hermes/kanban.db")
    if not os.path.exists(live):
        print("SKIP: no live kanban.db")
        return 0

    tmp = tempfile.mkdtemp(prefix="kanban-comment-")
    copy = os.path.join(tmp, "kanban.db")
    src = sqlite3.connect("file:%s?mode=ro" % live, uri=True)
    dst = sqlite3.connect(copy)
    src.backup(dst)
    src.close()
    dst.close()

    live_before = digest(live)

    pid, fd = pty.fork()
    if pid == 0:
        os.environ["TERM"] = "xterm-256color"
        os.environ["HERMES_KANBAN_DB"] = copy      # the CLI inherits this
        os.environ.pop("HERDR_ENV", None)
        os.environ.pop("HERDR_PANE_ID", None)
        os.execv(sys.executable, [
            sys.executable, os.path.join(HERE, "kanban_tui.py"), "--db", copy,
        ])
        os._exit(127)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))

    out = bytearray()
    if not pump(fd, out, 2.5):
        print("FAILED: the view exited during startup")
        return 1

    # The view opens on its own selection; take the target from what it drew
    # rather than from an independent guess about board order.
    startup = Screen(COLS, ROWS).feed(bytes(out)).render()
    m = re.search(r">\s*(t_[0-9a-z]+)", startup)
    if not m:
        print("FAILED: could not read the selected task from the screen")
        return 1
    task_id = m.group(1)
    copy_before = comment_count(copy, task_id)

    for ch in "c" + TEXT + "\r":
        os.write(fd, ch.encode())
        time.sleep(0.03)
    pump(fd, out, 4.0)
    screen = Screen(COLS, ROWS).feed(bytes(out)).render()

    os.write(fd, b"q")
    pump(fd, out, 1.0)
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass

    copy_after = comment_count(copy, task_id)
    live_after = digest(live)

    said_ok = "comment added to" in screen
    print("target task: %s" % task_id)
    print("copy: comments on that task %d -> %d" % (copy_before, copy_after))
    print("view reported success: %s" % said_ok)
    print("live board bytes unchanged: %s" % (live_before == live_after))

    ok = copy_after == copy_before + 1 and said_ok and live_before == live_after
    print("\ncomment action: %s" % ("OK" if ok else "FAILED"))
    if not ok:
        tail = "\n".join(screen.split("\n")[-6:])
        print("---- screen tail ----")
        print(tail)
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
