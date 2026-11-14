#!/usr/bin/env python3
"""refresh_probe.py -- prove the running view picks up board changes.

Copies kanban.db to a temp file, starts the TUI against the copy in a pty, then
inserts a task into the copy from a second connection and checks that the
header counts in the running view change without any keystroke.

The copy is used so the live board is never touched by this probe.

    python3 refresh_probe.py
"""
from __future__ import annotations

import fcntl
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


def main():
    live = os.path.expanduser("~/.hermes/kanban.db")
    if not os.path.exists(live):
        print("SKIP: no live kanban.db to copy")
        return 0

    tmpdir = tempfile.mkdtemp(prefix="kanban-refresh-")
    db = os.path.join(tmpdir, "kanban.db")
    # Copy through SQLite so a hot WAL is included in the snapshot.
    src = sqlite3.connect("file:%s?mode=ro" % live, uri=True)
    dst = sqlite3.connect(db)
    src.backup(dst)
    src.close()
    dst.close()

    def counts():
        c = sqlite3.connect(db)
        try:
            return dict(c.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status"))
        finally:
            c.close()

    before = counts()

    pid, fd = pty.fork()
    if pid == 0:
        os.environ["TERM"] = "xterm-ghostty"
        os.environ.pop("HERDR_ENV", None)
        os.environ.pop("HERDR_PANE_ID", None)
        os.execv(sys.executable, [sys.executable, os.path.join(HERE, "kanban_tui.py"),
                                 "--db", db, "--interval", "1"])
        os._exit(127)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))

    out = bytearray()

    def pump(seconds):
        start = time.time()
        while time.time() - start < seconds:
            r, _, _ = select.select([fd], [], [], 0.1)
            if r:
                try:
                    chunk = os.read(fd, 1 << 16)
                except OSError:
                    return
                if not chunk:
                    return
                out.extend(chunk)

    pump(3.0)
    first = Screen(COLS, ROWS).feed(bytes(out)).render()
    header_before = first.split("\n")[0]
    task_id = "t_refreshprobe01"
    # The list clips ids to 11 cells and titles to the column width, so the
    # probe looks for a short, unambiguous fragment of each.
    id_fragment = "t_refreshpr"
    title_fragment = "Refresh probe task"

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO tasks (id,title,assignee,status,priority,created_at,body) "
        "VALUES (?,?,?,?,?,?,?)",
        (task_id, "Refresh probe task inserted while the view is running",
         "probe", "todo", 999, int(time.time()), "inserted by refresh_probe.py"),
    )
    conn.commit()
    conn.close()
    after = counts()

    pump(4.0)
    second = Screen(COLS, ROWS).feed(bytes(out)).render()
    header_after = second.split("\n")[0]

    os.write(fd, b"q")
    pump(1.5)
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass

    todo_before = before.get("todo", 0)
    todo_after = after.get("todo", 0)
    m_before = re.search(r"tod\s+(\d+)", header_before)
    m_after = re.search(r"tod\s+(\d+)", header_after)
    shown_before = int(m_before.group(1)) if m_before else None
    shown_after = int(m_after.group(1)) if m_after else None

    print("board copy: %s" % db)
    print("todo in db       before=%d after=%d" % (todo_before, todo_after))
    print("todo in header   before=%s after=%s" % (shown_before, shown_after))
    print("new task visible in the running view: id=%s title=%s"
          % (id_fragment in second, title_fragment in second))
    print()
    print("header before: %s" % header_before.strip()[:120])
    print("header after:  %s" % header_after.strip()[:120])

    if os.environ.get("REFRESH_PROBE_DUMP"):
        with open(os.environ["REFRESH_PROBE_DUMP"], "wb") as fh:
            fh.write(bytes(out))
        print("raw stream written to %s" % os.environ["REFRESH_PROBE_DUMP"])

    ok = (
        todo_after == todo_before + 1
        and shown_before == todo_before
        and shown_after == todo_after
        and id_fragment in second
        and title_fragment in second
    )
    print("\nlive refresh: %s" % ("OK" if ok else "FAILED"))
    shutil.rmtree(tmpdir, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
