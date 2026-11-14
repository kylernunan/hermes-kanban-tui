#!/usr/bin/env python3
"""kanban_tui.py -- read-only Hermes Kanban board for the terminal.

Runs standalone in any xterm-compatible terminal and inside a herdr pane
(Ghostty, kitty, tmux, ...). It opens kanban.db read-only and never writes to
the board, the queue, or the dispatcher state.

Layout: task list on the left, task detail on the right, one header line of
counts, one footer line of keys. Narrow panes drop the detail pane (toggle
with 'z'), and short panes drop the filter line.

Non-interactive snapshot mode (no TTY needed):
    kanban_tui.py --print 120x40

Standalone: stdlib only, no build step, no third-party dependency.
"""
from __future__ import annotations

import argparse
import json
import locale
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
import unicodedata

# Windows ships without curses. The snapshot and JSON modes do not need it, so a
# missing module must not stop the import; only the interactive view requires it.
try:
    import curses
except ImportError:  # pragma: no cover - exercised by stubbing, not on POSIX
    curses = None  # type: ignore[assignment]

NO_CURSES = (
    "kanban-tui: this Python has no curses module, so the interactive view cannot run.\n"
    "On Windows install it with:  pip install windows-curses\n"
    "--print, --json, --where and --version work without curses."
)

VERSION = "1.0.1"
SOURCE_ID = "hermes-kanban-tui"

STATUS_ORDER = (
    "triage",
    "todo",
    "scheduled",
    "ready",
    "running",
    "review",
    "blocked",
    "done",
    "archived",
)
STATUS_TAG = {
    "triage": "tri",
    "todo": "tod",
    "scheduled": "sch",
    "ready": "rdy",
    "running": "run",
    "review": "rev",
    "blocked": "blk",
    "done": "don",
    "archived": "arc",
}
CLOSED_STATUSES = ("done", "archived")
# Done and archived hold 917 rows on the live board; a column cannot show that
# and should not try. The header keeps the true total.
HISTORY_CAP = 25
OPEN_STATUSES = tuple(s for s in STATUS_ORDER if s not in CLOSED_STATUSES)

# Attribute slots. Screens map these to colour pairs; the text screen ignores
# them. Keeping the renderer attribute-agnostic is what makes --print and the
# unit tests exercise the same drawing code as the interactive UI.
A_PLAIN = 0
A_HEADER = 1
A_DIM = 2
A_SELECTED = 3
A_TITLE = 4
A_OK = 5
A_WARN = 6
A_BAD = 7
A_INFO = 8
A_KEY = 9
A_STATUS = {s: 20 + i for i, s in enumerate(STATUS_ORDER)}
A_BORDER = 10


# --------------------------------------------------------------------------
# text geometry: terminals count cells, not code points
# --------------------------------------------------------------------------
def cell_width(ch):
    if unicodedata.combining(ch):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def disp_len(text):
    return sum(cell_width(c) for c in text)


def sanitize(text, limit=None):
    """Make free text safe for a single terminal cell run.

    Tabs, newlines and control characters would desynchronise a cursor model,
    so they become spaces before anything reaches the screen.
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    if limit is not None:
        text = text[: max(0, int(limit))]
    out = []
    for ch in text:
        if ch in "\r\n\t":
            out.append(" ")
            continue
        code = ord(ch)
        if (code < 32 or code == 127) and ch != "\x1b":
            out.append(" ")
            continue
        if code == 0x1B:
            out.append(" ")
            continue
        out.append(ch)
    return "".join(out)


def clip(text, width):
    """Truncate to at most `width` display cells."""
    text = sanitize(text)
    if width <= 0:
        return ""
    out, used = [], 0
    for ch in text:
        w = cell_width(ch)
        if used + w > width:
            break
        out.append(ch)
        used += w
    return "".join(out)


def fit(text, width):
    """Truncate or pad to exactly `width` display cells."""
    text = clip(text, width)
    pad = width - disp_len(text)
    return text + " " * max(0, pad)


# --------------------------------------------------------------------------
# formatting helpers
# --------------------------------------------------------------------------
def _shared_widths(counts, avail, minimum=16, maximum=40):
    """Split `avail` cells between columns in proportion to their card counts.

    Equal columns waste space: the blocked column with 13 cards gets the same 24
    cells as the empty running column. Every column keeps `minimum`, the rest is
    shared by load, and no column exceeds `maximum` so one busy status cannot
    take the whole board. The result always sums to at most `avail`, so a row
    can never overflow.
    """
    n = len(counts)
    if n == 0:
        return []
    if n * minimum > avail:
        return [max(8, avail // n)] * n
    widths = [minimum] * n
    extra = avail - n * minimum
    weights = [max(1, int(c or 0)) for c in counts]
    total = sum(weights)
    shares = [extra * wt / total for wt in weights]
    for i, share in enumerate(shares):
        widths[i] += min(int(share), maximum - minimum)
    leftover = avail - sum(widths)
    order = sorted(
        range(n), key=lambda i: (shares[i] - int(shares[i]), weights[i]), reverse=True
    )
    step = 0
    limit = n * (maximum - minimum + 1)
    while leftover > 0 and step < limit:
        i = order[step % n]
        if widths[i] < maximum:
            widths[i] += 1
            leftover -= 1
        step += 1
    return widths


def human_age(seconds):
    """Compact age: 12s, 4m05s, 3h18m, 2d07h."""
    if seconds is None:
        return "-"
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return "-"
    if s < 0:
        s = 0
    if s < 60:
        return "%ds" % s
    if s < 3600:
        return "%dm%02ds" % (s // 60, s % 60)
    if s < 86400:
        return "%dh%02dm" % (s // 3600, (s % 3600) // 60)
    return "%dd%02dh" % (s // 86400, (s % 86400) // 3600)


def human_clock(ts):
    if not ts:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))


def human_time(ts):
    if not ts:
        return "-"
    return time.strftime("%H:%M:%S", time.localtime(int(ts)))


# --------------------------------------------------------------------------
# data access (read-only)
# --------------------------------------------------------------------------
def _sqlite_uri(path):
    return "file:%s?mode=ro" % path.replace("?", "%3f").replace("#", "%23")


class KanbanStore:
    """Read-only view of a Hermes kanban.db."""

    def __init__(self, db_path=None, board=None):
        self.requested = db_path
        self.board = board
        self.db_path = self._resolve_path(db_path, board)
        self.conn = None
        self.mode = "unopened"
        self.connect()

    # -- path resolution ---------------------------------------------------
    @staticmethod
    def hermes_root():
        root = os.environ.get("HERMES_KANBAN_HOME") or os.environ.get("HERMES_HOME")
        if root:
            return os.path.expanduser(root)
        return os.path.expanduser("~/.hermes")

    @classmethod
    def _resolve_path(cls, db_path, board):
        if db_path:
            return os.path.abspath(os.path.expanduser(db_path))
        env_db = os.environ.get("HERMES_KANBAN_DB", "").strip()
        if env_db:
            return os.path.abspath(os.path.expanduser(env_db))
        root = cls.hermes_root()
        slug = board or os.environ.get("HERMES_KANBAN_BOARD", "").strip()
        if not slug:
            try:
                with open(os.path.join(root, "kanban", "current"), "r", encoding="utf-8-sig") as fh:
                    slug = fh.read().strip()
            except OSError:
                slug = ""
        if slug and slug != "default":
            candidate = os.path.join(root, "kanban", "boards", slug, "kanban.db")
            if os.path.exists(candidate):
                return candidate
        return os.path.join(root, "kanban.db")

    @property
    def board_label(self):
        parts = self.db_path.split(os.sep)
        if "boards" in parts:
            i = len(parts) - 2
            if parts[max(0, i - 1)] == "boards":
                return parts[i]
        return "default"

    # -- connection --------------------------------------------------------
    def connect(self):
        if not os.path.exists(self.db_path):
            raise SystemExit("kanban tui: no board database at %s" % self.db_path)
        last = None
        for mode, target, uri in (
            ("ro", _sqlite_uri(self.db_path), True),
            # A board live with a hot WAL can refuse a second read-only handle
            # on some filesystems; query_only on a normal handle still cannot
            # write, it just opens the file writable.
            ("query_only", self.db_path, False),
        ):
            try:
                conn = sqlite3.connect(target, uri=uri, timeout=4.0)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout=4000")
                conn.execute("PRAGMA query_only=1")
                conn.execute("SELECT 1 FROM tasks LIMIT 1")
                self.conn = conn
                self.mode = mode
                return
            except sqlite3.Error as exc:
                last = exc
                try:
                    conn.close()
                except Exception:
                    pass
        raise SystemExit("kanban tui: cannot open %s read-only: %s" % (self.db_path, last))

    def close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass
            self.conn = None

    # -- queries -----------------------------------------------------------
    def counts(self):
        rows = self.conn.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    def tasks(self, include_closed=False):
        sql = (
            "SELECT id, title, assignee, status, priority, created_at, started_at, "
            "completed_at, current_run_id, worker_pid, last_heartbeat_at, "
            "block_kind, block_recurrences, consecutive_failures, "
            "max_runtime_seconds, model_override, provider_override, scheduled_due_at "
            "FROM tasks"
        )
        if not include_closed:
            sql += " WHERE status NOT IN ('done','archived')"
        sql += " ORDER BY priority DESC, created_at ASC"
        return [dict(r) for r in self.conn.execute(sql)]

    def task(self, task_id):
        row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None

    def runs(self, task_id, limit=8):
        rows = self.conn.execute(
            "SELECT id, profile, status, started_at, ended_at, outcome, "
            "max_runtime_seconds, last_heartbeat_at, summary, error "
            "FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT ?",
            (task_id, limit),
        )
        return [dict(r) for r in rows]

    def events(self, task_id, limit=300):
        rows = self.conn.execute(
            "SELECT id, kind, payload, created_at, run_id FROM task_events "
            "WHERE task_id = ? ORDER BY id DESC LIMIT ?",
            (task_id, limit),
        )
        return [dict(r) for r in rows]

    def comments(self, task_id):
        rows = self.conn.execute(
            "SELECT id, author, body, created_at FROM task_comments "
            "WHERE task_id = ? ORDER BY id ASC",
            (task_id,),
        )
        return [dict(r) for r in rows]

    def links(self, task_id):
        parents = self.conn.execute(
            "SELECT t.id, t.title, t.status FROM task_links l "
            "JOIN tasks t ON t.id = l.parent_id WHERE l.child_id = ? ORDER BY t.id",
            (task_id,),
        )
        children = self.conn.execute(
            "SELECT t.id, t.title, t.status FROM task_links l "
            "JOIN tasks t ON t.id = l.child_id WHERE l.parent_id = ? ORDER BY t.id",
            (task_id,),
        )
        return ([dict(r) for r in parents], [dict(r) for r in children])

    def open_parent_waits(self):
        """task_id -> number of parents still open. One query for the whole
        board: a card gated by unfinished parents is the single most common
        thing the columns fail to show."""
        rows = self.conn.execute(
            "SELECT l.child_id AS child, COUNT(*) AS n FROM task_links l "
            "JOIN tasks p ON p.id = l.parent_id "
            "WHERE p.status NOT IN ('done','archived') GROUP BY l.child_id"
        )
        return {r["child"]: r["n"] for r in rows}

    def running_runs(self):
        """task_id -> its live run row (latest first)."""
        rows = self.conn.execute(
            "SELECT id, task_id, profile, status, started_at, ended_at, "
            "max_runtime_seconds, last_heartbeat_at FROM task_runs "
            "WHERE status = 'running' ORDER BY id DESC"
        )
        out = {}
        for r in rows:
            out.setdefault(r["task_id"], dict(r))
        return out

    def attachments(self, task_id):
        rows = self.conn.execute(
            "SELECT filename, size FROM task_attachments WHERE task_id = ? ORDER BY id",
            (task_id,),
        )
        return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# screens
# --------------------------------------------------------------------------
class TextScreen:
    """Fixed-size character grid. Used by --print and by the unit tests."""

    def __init__(self, width, height):
        self.w = max(1, int(width))
        self.h = max(1, int(height))
        self.rows = [[" "] * self.w for _ in range(self.h)]

    def line(self, y, segments):
        if y < 0 or y >= self.h:
            return
        x = 0
        row = self.rows[y]
        for text, _attr in segments:
            for ch in sanitize(text):
                w = cell_width(ch)
                if x + w > self.w:
                    return
                row[x] = ch
                if w == 2:
                    row[x + 1] = ""
                x += w

    def render(self):
        return "\n".join("".join(r).rstrip() for r in self.rows)


class CursesScreen:
    def __init__(self, stdscr):
        self.scr = stdscr
        self.last = None
        self.resize()

    def resize(self):
        try:
            self.h, self.w = self.scr.getmaxyx()
        except curses.error:  # pragma: no cover
            self.h, self.w = 24, 80

    def line(self, y, segments):
        if y < 0 or y >= self.h:
            return
        limit = self.w if y < self.h - 1 else max(1, self.w - 1)
        # Clear the row before writing it. paint() only rewrites changed rows,
        # so cells this row does not cover would otherwise keep the previous
        # frame's characters (for example a column separator showing through a
        # gap). clrtoeol is an explicit clear; writing spaces over the gap is
        # not, because ncurses can optimise a run of default blanks away.
        try:
            self.scr.move(y, 0)
            self.scr.clrtoeol()
        except curses.error:
            pass
        x = 0
        for text, attr in segments:
            if x >= limit:
                break
            text = clip(text, limit - x)
            if not text:
                continue
            try:
                self.scr.addstr(y, x, text, attr)
            except curses.error:
                # The bottom-right cell cannot be written; drop just that cell.
                try:
                    self.scr.addstr(y, x, text[:-1], attr)
                except curses.error:
                    pass
            x += disp_len(text)


# --------------------------------------------------------------------------
# herdr bridge (optional, never fatal)
# --------------------------------------------------------------------------
class HerdrBridge:
    """Mirror board state into the herdr pane title when running in a pane.

    Failures are recorded, never raised: a herdr version difference must not
    take the board down.
    """

    def __init__(self, args):
        self.pane = os.environ.get("HERDR_PANE_ID", "").strip()
        self.enabled = (
            bool(self.pane)
            and os.environ.get("HERDR_ENV") == "1"
            and not getattr(args, "no_herdr", False)
        )
        self.last_title = ""
        self.last_error = ""
        self.pending = ""
        self.next_try = 0.0
        self.commands = 0
        self.bin = getattr(args, "herdr_bin", None) or "herdr"

    def report(self, title):
        if not self.enabled or not title or title == self.last_title:
            return
        now = time.time()
        if title == self.pending and now < self.next_try:
            return
        cmd = [
            self.bin, "pane", "report-metadata", self.pane,
            "--source", SOURCE_ID, "--title", title,
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=2.0)
            self.commands += 1
            if res.returncode != 0:
                self.last_error = (res.stderr or res.stdout or "exit %d" % res.returncode).strip()[:160]
                self.pending = title
                self.next_try = now + 15.0
            else:
                self.last_error = ""
                self.last_title = title
                self.pending = ""
        except (OSError, subprocess.SubprocessError) as exc:
            self.last_error = str(exc)[:160]
            self.pending = title
            self.next_try = now + 15.0

    def clear(self):
        """Remove our pane title when the view exits, so a dead board is not
        advertised in the herdr tab strip."""
        if not self.enabled or not self.last_title:
            return
        cmd = [self.bin, "pane", "report-metadata", self.pane, "--source", SOURCE_ID, "--clear-title"]
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=2.0)
        except (OSError, subprocess.SubprocessError):
            pass
        self.last_title = ""


# --------------------------------------------------------------------------
# application state and rendering
# --------------------------------------------------------------------------
class App:
    """UI state plus frame construction.

    render() performs no I/O, so --print, the tests and the curses loop all
    draw through the same code path.
    """

    def __init__(self, store, args):
        self.store = store
        self.args = args
        self.include_closed = bool(getattr(args, "all", False))
        self.query = (getattr(args, "query", "") or "").strip()
        self.status_filter = getattr(args, "status", "") or ""
        self.assignee_filter = getattr(args, "assignee", "") or ""
        self.interval = max(0.5, float(getattr(args, "interval", 2.0) or 2.0))
        self.paused = False
        self.focus = "list"
        self.detail_tab = "body"
        self.show_detail = True
        self.selected_id = None
        self.list_offset = 0
        # Hit-test map rebuilt by every render: y -> task id, plus the column
        # spans, so a mouse click can be resolved without re-deriving layout.
        self.hit_cards = {}
        self.hit_cols = []
        self.body_top = 0
        self.detail_offset = 0
        self.message = ""
        self.message_until = 0.0
        self.search_mode = False
        self.search_buf = ""
        # Comment compose. The only write path in this program: it shells out
        # to `hermes kanban comment`, never touches the database itself.
        self.compose_mode = False
        self.compose_buf = ""
        self.compose_task = ""
        self.last_write = {"ok": None, "argv": [], "detail": ""}
        self.show_help = False
        # None = pick from the pane size; "board" / "list" = user picked with 'b'.
        self.mode_override = None
        self.col_offsets = {}
        self.parent_waits = {}
        self.live_runs = {}
        self.list_offset = self.list_offset
        self.last_size = (0, 0)
        self.last_error = ""
        self.last_refresh = 0.0
        self.gen = 0
        self.counts = {}
        self.tasks = []
        self.rows = []
        self.detail = None
        self.detail_gen = -1
        self.terminfo_note = ""
        self.herdr = HerdrBridge(args)
        self.refresh(force=True)

    # -- data --------------------------------------------------------------
    def refresh(self, force=False):
        now = time.time()
        if self.paused and not force:
            return
        try:
            self.counts = self.store.counts()
            self.tasks = self.store.tasks(include_closed=self.include_closed)
            self.parent_waits = self.store.open_parent_waits()
            self.live_runs = self.store.running_runs()
            self.last_error = ""
        except sqlite3.Error as exc:
            self.last_error = "%s: %s" % (type(exc).__name__, exc)
            if not force:
                return
        self.gen += 1
        self.last_refresh = now
        self.build_rows()
        self.clamp_selection()
        self.load_detail(force=True)
        self.herdr.report(self.title_bar())

    def filtered(self):
        q = self.query.lower()
        out = []
        for t in self.tasks:
            if self.status_filter and t["status"] != self.status_filter:
                continue
            if self.assignee_filter and (t.get("assignee") or "") != self.assignee_filter:
                continue
            if q:
                hay = " ".join(
                    str(t.get(k) or "") for k in ("id", "title", "assignee", "status")
                ).lower()
                if q not in hay:
                    continue
            out.append(t)
        return out

    def build_rows(self):
        by_status = {}
        for t in self.filtered():
            by_status.setdefault(t["status"], []).append(t)
        rows = []
        for status in STATUS_ORDER:
            bucket = by_status.get(status)
            if not bucket:
                continue
            rows.append({"kind": "group", "status": status, "count": len(bucket)})
            for t in bucket:
                rows.append({"kind": "task", "task": t})
        self.rows = rows

    def selected_index(self):
        for i, r in enumerate(self.rows):
            if r["kind"] == "task" and r["task"]["id"] == self.selected_id:
                return i
        return None

    def clamp_selection(self):
        idx = self.selected_index()
        if idx is not None:
            return
        first = next((i for i, r in enumerate(self.rows) if r["kind"] == "task"), None)
        if first is None:
            self.selected_id = None
            self.detail = None
            self.detail_gen = -1
            return
        self.selected_id = self.rows[first]["task"]["id"]
        self.detail_offset = 0

    def _go(self, index):
        new_id = self.rows[index]["task"]["id"]
        if new_id != self.selected_id:
            self.selected_id = new_id
            self.detail_offset = 0
            self.load_detail(force=True)

    def select_relative(self, delta):
        idxs = [i for i, r in enumerate(self.rows) if r["kind"] == "task"]
        if not idxs:
            return
        cur = self.selected_index()
        if cur is None:
            self._go(idxs[0])
            return
        try:
            pos = idxs.index(cur)
        except ValueError:
            pos = 0
        pos = max(0, min(len(idxs) - 1, pos + delta))
        self._go(idxs[pos])

    def visible_statuses(self):
        """Columns to show, in workflow order. Fixed so columns do not shift
        when a status empties; an empty column still shows its count."""
        return [s for s in STATUS_ORDER if s not in CLOSED_STATUSES or self.include_closed]

    def by_status(self):
        grouped = {s: [] for s in self.visible_statuses()}
        for t in self.filtered():
            grouped.setdefault(t["status"], []).append(t)
        return grouped

    def selected_status(self):
        for t in self.tasks:
            if t["id"] == self.selected_id:
                return t["status"]
        return self.visible_statuses()[0] if self.visible_statuses() else "todo"

    def select_in_column(self, delta):
        grouped = self.by_status()
        status = self.selected_status()
        cards = grouped.get(status) or []
        if not cards:
            return
        ids = [t["id"] for t in cards]
        if self.selected_id in ids:
            pos = ids.index(self.selected_id)
        else:
            pos = 0
        pos = max(0, min(len(ids) - 1, pos + delta))
        self._go_id(ids[pos])

    def select_column(self, delta):
        """Move to the next/previous column that has cards."""
        grouped = self.by_status()
        order = [s for s in self.visible_statuses() if grouped.get(s)]
        if not order:
            return
        current = self.selected_status()
        if current in order:
            pos = order.index(current)
        else:
            pos = 0
        pos = max(0, min(len(order) - 1, pos + delta))
        target = order[pos]
        cards = grouped[target]
        self._go_id(cards[0]["id"] if cards else self.selected_id)

    def _go_id(self, task_id):
        if task_id and task_id != self.selected_id:
            self.selected_id = task_id
            self.detail_offset = 0
            self.load_detail(force=True)

    def click(self, x, y):
        """Select the card under the cursor. Returns True when something was hit."""
        hits = self.hit_cards.get(y) or []
        for x0, x1, task_id in hits:
            if x0 <= x <= x1:
                self._go_id(task_id)
                self.focus = "list"
                return True
        # Empty part of a column: select that column's first card.
        for status, x0, x1 in self.hit_cols:
            if x0 <= x <= x1:
                cards = self.by_status().get(status) or []
                if cards:
                    self._go_id(cards[0]["id"])
                    self.focus = "list"
                    return True
        return False

    def wheel(self, direction):
        """Scroll the pane under the pointer, or the focused one."""
        if self.focus == "detail" and self.show_detail:
            self.detail_offset = max(0, self.detail_offset + direction)
        else:
            self._move(direction)

    def select_first(self):
        idxs = [i for i, r in enumerate(self.rows) if r["kind"] == "task"]
        if idxs:
            self._go(idxs[0])

    def select_last(self):
        idxs = [i for i, r in enumerate(self.rows) if r["kind"] == "task"]
        if idxs:
            self._go(idxs[-1])

    def load_detail(self, force=False):
        if not self.selected_id:
            self.detail = None
            return
        if not force and self.detail_gen == self.gen and self.detail:
            return
        try:
            task = self.store.task(self.selected_id)
            if task is None:
                self.detail = None
                return
            parents, children = self.store.links(self.selected_id)
            self.detail = {
                "task": task,
                "runs": self.store.runs(self.selected_id),
                "events": self.store.events(self.selected_id),
                "comments": self.store.comments(self.selected_id),
                "parents": parents,
                "children": children,
                "attachments": self.store.attachments(self.selected_id),
            }
            self.detail_gen = self.gen
        except sqlite3.Error as exc:
            self.last_error = "%s: %s" % (type(exc).__name__, exc)
            self.detail = None

    def can_write(self):
        return not bool(getattr(self.args, "no_write", False))

    def post_comment(self, task_id, text):
        """Append a comment through the kanban CLI.

        Returns (ok, detail). The body is one argv element, so nothing here can
        be reinterpreted by a shell. Records the exact argv for the record and
        for tests.
        """
        text = (text or "").strip()
        if not task_id or not text:
            return False, "nothing to post"
        if not self.can_write():
            return False, "writes disabled (--no-write)"
        argv = [
            "hermes", "kanban", "comment", task_id,
            "--author", getattr(self.args, "author", None) or "kanban-tui",
            text,
        ]
        self.last_write = {"ok": None, "argv": argv, "detail": "posting"}
        try:
            res = subprocess.run(
                argv, capture_output=True, text=True, timeout=25.0
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self.last_write = {"ok": False, "argv": argv, "detail": str(exc)[:200]}
            return False, str(exc)[:200]
        detail = (res.stdout or res.stderr or "").strip()
        ok = res.returncode == 0
        self.last_write = {"ok": ok, "argv": argv, "detail": detail[:400]}
        return ok, detail[:200] or ("exit %d" % res.returncode)

    def submit_comment(self):
        """Called on Enter in compose mode."""
        text = self.compose_buf.strip()
        task_id = self.compose_task or (self.selected_id or "")
        if not text:
            self.compose_mode = False
            self.compose_buf = ""
            self.set_message("nothing to post")
            return
        ok, detail = self.post_comment(task_id, text)
        if ok:
            self.compose_mode = False
            self.compose_buf = ""
            self.refresh(force=True)
            self.detail_tab = "comments"
            self.focus = "detail"
            self.set_message("comment added to %s" % task_id, 4.0)
        else:
            # Keep the text so it can be retried or copied out.
            self.set_message("comment failed: %s" % detail, 8.0)

    def set_message(self, text, seconds=3.0):
        self.message = text
        self.message_until = time.time() + seconds

    # -- filters -----------------------------------------------------------
    def _status_choices(self):
        return [""] + [
            s for s in STATUS_ORDER
            if s not in CLOSED_STATUSES or self.include_closed
        ]

    def cycle_status(self, delta):
        choices = self._status_choices()
        try:
            pos = choices.index(self.status_filter)
        except ValueError:
            pos = 0
        self.status_filter = choices[(pos + delta) % len(choices)]
        self._rebuild()
        self.set_message("status filter: %s" % (self.status_filter or "all open"))

    def cycle_assignee(self, delta):
        seen = sorted({(t.get("assignee") or "(none)") for t in self.tasks})
        choices = [""] + seen
        try:
            pos = choices.index(self.assignee_filter)
        except ValueError:
            pos = 0
        self.assignee_filter = choices[(pos + delta) % len(choices)]
        self._rebuild()
        self.set_message("assignee filter: %s" % (self.assignee_filter or "all"))

    def _rebuild(self):
        self.build_rows()
        self.clamp_selection()
        self.load_detail(force=True)

    def toggle_closed(self):
        self.include_closed = not self.include_closed
        if self.status_filter in CLOSED_STATUSES and not self.include_closed:
            self.status_filter = ""
        self.refresh(force=True)
        self.set_message("history %s" % ("shown" if self.include_closed else "hidden"))

    def title_bar(self):
        return "kanban %s: %d running, %d ready, %d blocked" % (
            self.store.board_label,
            self.counts.get("running", 0),
            self.counts.get("ready", 0),
            self.counts.get("blocked", 0),
        )

    # -- frame -------------------------------------------------------------
    def mode(self, w=None, h=None):
        """Board (status columns) or list (vertical sections)."""
        if self.mode_override:
            return self.mode_override
        if w is None:
            w, h = self.last_size
        if h is None:
            h = 40
        # Columns need roughly 16 cells each; below that the board cannot show
        # its statuses side by side, so the list layout takes over.
        return "board" if w >= 96 and h >= 18 else "list"

    def render(self, height, width, now=None):
        h = max(4, int(height))
        w = max(20, int(width))
        if now is None:
            now = time.time()
        self.last_size = (w, h)
        frame = [None] * h
        mode = self.mode(w, h)

        # Layout tiers.
        #   board:   status columns across the top, detail strip underneath
        #   list:    wide panes show list + detail side by side, narrow panes
        #            alternate between them, and a hidden detail returns the
        #            whole width to the list.
        detail_w = 0
        list_w = w
        narrow_detail = False
        strip_h = 0
        if self.show_detail:
            if mode == "board":
                if h >= 26:
                    strip_h = min(16, max(9, h // 3))
            elif w >= 82:
                list_w = min(max(48, int(w * 0.54)), w - 34)
                detail_w = w - list_w - 1
            elif w >= 30 and self.focus == "detail":
                # Too narrow for two columns: give the detail the whole pane so
                # 1/2/3 and Tab still reach body, events and comments.
                narrow_detail = True
                detail_w = w
                list_w = 0

        header_h = 3 if h >= 12 else 2
        footer_h = 2 if h >= 10 else 1
        top = header_h
        bottom = h - footer_h
        body_h = max(1, bottom - top)

        self.hit_cards = {}
        self.hit_cols = []
        self.body_top = top
        frame[0] = self._header_row(w, now)
        if header_h >= 3:
            frame[1] = self._filter_row(w)
            frame[2] = [("-" * w, A_BORDER)]
        else:
            frame[1] = [("-" * w, A_BORDER)]

        if mode == "board":
            board_h = max(4, body_h - (strip_h + 1 if strip_h else 0))
            board_rows = self._board_rows(board_h, w, now)
            for i in range(board_h):
                frame[top + i] = board_rows[i] if i < len(board_rows) else []
            if strip_h:
                rule_y = top + board_h
                frame[rule_y] = [("-" * w, A_BORDER)]
                strip = self._strip_rows(strip_h, w, now)
                for i in range(strip_h):
                    frame[rule_y + 1 + i] = strip[i] if i < len(strip) else []
        elif narrow_detail:
            # Full-width detail; the list is one Tab away.
            _, det = self._detail_frame(body_h, detail_w, now)
            for i in range(body_h):
                frame[top + i] = det[i]
        else:
            list_rows = self._list_frame(body_h, list_w, now)
            if detail_w:
                sep, det = self._detail_frame(body_h, detail_w, now)
            else:
                sep = det = None
            for i in range(body_h):
                row = list_rows[i] if i < len(list_rows) else []
                if detail_w:
                    frame[top + i] = row + sep[i] + det[i]
                else:
                    frame[top + i] = row

        note, attr = self._status_note(now, w)
        frame[h - footer_h] = [(fit(note, w), attr)]
        frame[h - 1] = self._footer_row(w)
        if self.show_help:
            frame = self._overlay_help(frame, h, w)
        return [r if r is not None else [] for r in frame]

    def _overlay_help(self, frame, h, w):
        """Draw help over the board, leaving one column of margin each side."""
        inner = max(20, w - 4)
        lines = self.help_lines(inner)
        top = 1
        avail = max(1, h - 2)
        if len(lines) > avail:
            lines = lines[:avail]
        for i, segs in enumerate(lines):
            y = top + i
            row = [("  ", A_PLAIN)]
            used = 2
            for text, attr in segs:
                if used + disp_len(text) > w - 2:
                    break
                row.append((text, attr))
                used += disp_len(text)
            row.append((" " * max(0, w - 2 - used), A_PLAIN))
            frame[y] = row
        return frame

    def _status_note(self, now, w=80):
        if self.compose_mode:
            label = "comment %s: " % self.compose_task
            room = max(8, w - disp_len(label) - 1)
            buf = self.compose_buf
            if disp_len(buf) > room:
                buf = "\u2026" + buf[-room:]
            return label + buf, A_TITLE
        if self.search_mode:
            return "/" + self.search_buf, A_INFO
        if self.show_help:
            return "help -- press any key to close", A_DIM
        note = ""
        attr = A_DIM
        if self.message and now < self.message_until:
            note = self.message
        if self.last_error:
            note = ("%s  |  " % note if note else "") + "read error: " + self.last_error
            attr = A_BAD
        if self.paused:
            note = ("%s  |  " % note if note else "") + "paused"
            attr = A_WARN if attr == A_DIM else attr
        return note, attr

    def _header_row(self, w, now):
        left = [(" KANBAN ", A_HEADER), (" " + self.store.board_label + " ", A_INFO)]
        right = "read-only | %s | %s" % (
            "paused" if self.paused else "%gs" % self.interval,
            human_time(now),
        )
        segs = list(left)
        used = sum(disp_len(t) for t, _ in segs)
        for status in STATUS_ORDER:
            n = self.counts.get(status, 0)
            if status in CLOSED_STATUSES and not self.include_closed:
                continue
            tag = STATUS_TAG[status]
            if used + len(tag) + 2 >= w - len(right) - 1:
                break
            segs.append((tag, A_STATUS[status]))
            segs.append((" %d " % n, A_PLAIN))
            used += len(tag) + len(" %d " % n)
        pad = w - used - len(right)
        if pad >= 1:
            segs.append((" " * pad, A_PLAIN))
            segs.append((right, A_DIM))
        else:
            segs.append((" " + clip(right, max(0, w - used - 1)), A_DIM))
        return segs

    def _filter_row(self, w):
        parts = [
            (" show ", A_KEY),
            ("%s " % ("history" if self.include_closed else "open"), A_INFO),
            (" status ", A_KEY),
            ("%s " % (self.status_filter or "all"), A_INFO),
            (" assignee ", A_KEY),
            ("%s " % (self.assignee_filter or "all"), A_INFO),
        ]
        if self.query:
            parts.append((" q ", A_KEY))
            parts.append(("%s " % self.query, A_INFO))
        parts.append(("  %d listed" % len([r for r in self.rows if r["kind"] == "task"]), A_DIM))
        out, used = [], 0
        for text, attr in parts:
            if used + len(text) > w:
                break
            out.append((text, attr))
            used += len(text)
        return out

    # -- board (status columns) -------------------------------------------
    def _board_rows(self, height, width, now):
        statuses = self.visible_statuses()
        grouped = self.by_status()
        if not statuses:
            return [[(fit(" no statuses to show", width), A_DIM)]]
        if not any(grouped.values()):
            return [[(fit(" no tasks match the current filter", width), A_DIM)]]

        # Keep every column visible while it stays readable; otherwise drop the
        # empty ones first (an empty triage column is not worth a card's width),
        # and only then page a window that follows the selection.
        if (width - (len(statuses) - 1)) // max(1, len(statuses)) >= 16:
            shown = statuses
        else:
            shown = [s for s in statuses if grouped.get(s)] or statuses
        n = len(shown)
        cw = (width - (n - 1)) // n
        start = 0
        if cw >= 16:
            widths = _shared_widths(
                [len(grouped.get(s) or ()) for s in shown], width - (n - 1)
            )
        else:
            widths = None
        if cw < 16:
            visible_n = max(1, (width + 1) // 17)
            sel = self.selected_status()
            sel_i = shown.index(sel) if sel in shown else 0
            start = max(0, min(sel_i - visible_n // 2, n - visible_n))
            cw = max(8, (width - (visible_n - 1)) // max(1, visible_n))
            shown = shown[start:start + visible_n]
        more_left = start > 0
        more_right = start + len(shown) < n

        if widths is None:
            widths = [cw] * len(shown)
        x = 0
        for i, status in enumerate(shown):
            self.hit_cols.append((status, x, x + widths[i] - 1))
            x += widths[i] + 1
        built = [
            self._column_lines(
                s,
                grouped.get(s, []),
                widths[i],
                height,
                now,
                more_left=more_left and i == 0,
                more_right=more_right and i == len(shown) - 1,
            )
            for i, s in enumerate(shown)
        ]
        columns = [b[0] for b in built]
        for i, (_lines, card_rows) in enumerate(built):
            x0 = self.hit_cols[i][1]
            x1 = self.hit_cols[i][2]
            for y_in_col, task_id in card_rows:
                if y_in_col < height:
                    self.hit_cards.setdefault(self.body_top + y_in_col, []).append(
                        (x0, x1, task_id)
                    )
        rows = []
        for y in range(height):
            segs = []
            for i, col in enumerate(columns):
                if i:
                    segs.append(("|", A_BORDER))
                segs.extend(col[y])
            used = sum(disp_len(t) for t, _ in segs)
            if used < width:
                segs.append((" " * (width - used), A_PLAIN))
            rows.append(segs)
        return rows

    def _column_lines(self, status, tasks, cw, height, now, more_left=False, more_right=False):
        total = len(tasks)
        hidden = 0
        if status in CLOSED_STATUSES and tasks:
            cap = int(getattr(self.args, "history_cap", HISTORY_CAP) or HISTORY_CAP)
            ordered = sorted(tasks, key=lambda t: (t.get("completed_at") or 0), reverse=True)
            if len(ordered) > cap:
                visible = ordered[:cap]
                if self.selected_id and self.selected_id not in {t["id"] for t in visible}:
                    for t in ordered[cap:]:
                        if t["id"] == self.selected_id:
                            visible = [t] + visible[:-1]
                            break
                hidden = len(ordered) - len(visible)
                tasks = visible
        label = status if cw >= len(status) + 6 else STATUS_TAG[status]
        if more_left:
            label = "<" + label
        if more_right:
            label = label + ">"
        lines = [
            [(fit(" %s %d " % (label, total), cw), A_STATUS[status])],
            [("-" * cw, A_BORDER)],
        ]
        inner = max(0, height - 2)
        cards = [self._card(t, cw, now) for t in tasks]

        idx = next(
            (i for i, c in enumerate(cards) if c["task"]["id"] == self.selected_id), None
        )
        off = self.col_offsets.get(status, 0)
        off = max(0, min(off, max(0, len(cards) - 1)))
        if idx is not None:
            if idx < off:
                off = idx
            else:
                while off < idx and sum(c["height"] for c in cards[off:idx + 1]) > inner:
                    off += 1
        self.col_offsets[status] = off

        visible = cards[off:]
        # A blank line between cards reads much better, but density wins when
        # the column is full: add the separators only if everything still fits.
        sep_h = 1 if visible and sum(c["height"] for c in visible) + len(visible) - 1 <= inner else 0
        used = 0
        body = []
        shown_cards = 0
        card_rows = []
        for i, card in enumerate(visible):
            extra = sep_h if i else 0
            if used + extra + card["height"] > inner:
                break
            if extra:
                body.append([(" " * cw, A_PLAIN)])
            start_row = len(lines) + len(body)
            for k in range(card["height"]):
                card_rows.append((start_row + k, card["task"]["id"]))
            body.extend(card["lines"])
            used += extra + card["height"]
            shown_cards += 1
        if not body and inner:
            body.append([(fit(" (empty)", cw), A_DIM)])
        remaining = total - (off + shown_cards)
        if remaining > 0 and len(body) + 2 <= inner:
            body.append([(clip(" +%d older" % remaining, cw), A_DIM)])
        lines.extend(body)
        while len(lines) < height:
            lines.append([(" " * cw, A_PLAIN)])
        return lines[:height], card_rows

    def _card_signal(self, task, now):
        """Why this card is not moving, in at most six cells.

        Returns (text, severity). Empty text means nothing to flag. Four of the
        thirteen blocked cards carry no failure text at all, and ten open cards
        are gated by unfinished parents -- without this the board shows none of
        that and every one has to be opened.
        """
        status = task["status"]
        if status == "blocked":
            kind = (task.get("block_kind") or "").strip().lower()
            short = {
                "capability": "!cap",
                "needs_input": "!need",
                "dependency": "!dep",
                "transient": "!trns",
            }.get(kind, "!block")
            return short, "bad"
        waits = self.parent_waits.get(task["id"], 0)
        if waits:
            return "dep%d" % waits, "warn"
        if status == "running":
            run = self.live_runs.get(task["id"]) or {}
            hb = run.get("last_heartbeat_at") or task.get("last_heartbeat_at")
            if hb:
                age = max(0, now - hb)
                if age > 120:
                    return "!hb " + human_age(age), "bad"
                if age < 60:
                    return "hb%ds" % age, None
                if age < 3600:
                    return "hb%dm" % (age // 60), None
                return "hb%dh" % (age // 3600), None
        return "", None

    def _card(self, task, cw, now):
        """One card: an id/age/signal line, then up to two title lines."""
        selected = task["id"] == self.selected_id
        signal, severity = self._card_signal(task, now)
        if selected:
            attr = A_SELECTED
        elif severity == "bad":
            attr = A_BAD
        elif severity == "warn":
            attr = A_WARN
        else:
            attr = A_PLAIN
        marker = ">" if selected else " "
        idw = max(6, min(11, cw - 6))
        head = marker + clip(task["id"], idw)
        if signal and disp_len(head) + 1 + disp_len(signal) <= cw - 1:
            head = head + " " + clip(signal, cw)
        age = human_age(now - (task.get("created_at") or now)) if cw >= 24 else ""
        if age and disp_len(head) + 1 + disp_len(age) <= cw - 1:
            gap = max(1, cw - 1 - disp_len(head) - disp_len(age))
            head = head + " " * gap + age
        lines = [[(fit(head, cw), attr)]]
        title_attr = A_SELECTED if selected else A_PLAIN
        title_w = max(1, cw - 2)
        for chunk in self._wrap(task.get("title") or "", title_w)[:2]:
            lines.append([(fit("  " + chunk, cw), title_attr)])
        return {"task": task, "height": len(lines), "lines": lines}

    def _strip_rows(self, height, width, now):
        """The detail strip under the board."""
        if not self.detail:
            return [[(fit(" no task selected", width), A_DIM)]]
        lines = self._detail_lines(width, now, compact=height < 20)
        max_off = max(0, len(lines) - height)
        self.detail_offset = max(0, min(self.detail_offset, max_off))
        out = []
        for i in range(height):
            idx = self.detail_offset + i
            if idx >= len(lines):
                out.append([])
                continue
            used = 0
            row = []
            for text, attr in lines[idx]:
                if used >= width:
                    break
                text = clip(text, width - used)
                row.append((text, attr))
                used += disp_len(text)
            out.append(row)
        return out

    def _list_frame(self, height, width, now):
        rows = [[] for _ in range(height)]
        if not self.rows:
            rows[0] = [(fit("no tasks match the current filter", width), A_DIM)]
            return rows

        sel_idx = self.selected_index()
        if sel_idx is not None:
            if sel_idx < self.list_offset:
                self.list_offset = sel_idx
            elif sel_idx >= self.list_offset + height:
                self.list_offset = sel_idx - height + 1
        self.list_offset = max(0, min(self.list_offset, max(0, len(self.rows) - 1)))

        for slot in range(height):
            idx = self.list_offset + slot
            if idx >= len(self.rows):
                break
            row = self.rows[idx]
            if row["kind"] == "group":
                rows[slot] = self._group_row(row, width)
            else:
                rows[slot] = self._task_row(row["task"], width, now, idx == sel_idx)
                self.hit_cards.setdefault(self.body_top + slot, []).append(
                    (0, width - 1, row["task"]["id"])
                )
        return rows

    def _group_row(self, row, width):
        status = row["status"]
        label = " %s (%d) " % (status, row["count"])
        rest = max(0, width - disp_len(label))
        return [(label, A_STATUS[status]), ("-" * rest, A_BORDER)]

    def _task_row(self, task, width, now, selected):
        attr = A_SELECTED if selected else A_PLAIN
        marker = ">" if selected else " "
        tag = STATUS_TAG.get(task["status"], "?")
        prio = task.get("priority") or 0
        assignee = clip(task.get("assignee") or "-", 13)
        head = "%s %s %s %3d %s " % (
            marker,
            clip(task["id"], 11),
            tag,
            int(prio) if str(prio).lstrip("-").isdigit() else 0,
            fit(assignee, 13),
        )
        extra = ""
        if task["status"] == "running" and task.get("last_heartbeat_at"):
            extra = "  hb " + human_age(now - task["last_heartbeat_at"])
        elif task["status"] == "blocked" and task.get("block_kind"):
            extra = "  " + task["block_kind"]
        title_w = max(0, width - disp_len(head))
        room = max(0, title_w - disp_len(extra))
        body = fit(clip(task.get("title") or "", room), room) + extra
        return [(head, attr), (fit(body, title_w), attr)]

    # -- detail ------------------------------------------------------------
    def _detail_frame(self, height, dw, now):
        sep = [[("|", A_BORDER)] for _ in range(height)]
        out = [[] for _ in range(height)]
        if not self.detail:
            out[0] = [(fit(" no task selected", dw), A_DIM)]
            return sep, out
        lines = self._detail_lines(dw, now)
        max_off = max(0, len(lines) - height)
        self.detail_offset = max(0, min(self.detail_offset, max_off))
        for slot in range(height):
            idx = self.detail_offset + slot
            if idx >= len(lines):
                break
            # Consume the row width segment by segment. Fitting each segment to
            # the full width would push every segment after the first off-row.
            used = 0
            row = []
            for text, attr in lines[idx]:
                if used >= dw:
                    break
                text = clip(text, dw - used)
                row.append((text, attr))
                used += disp_len(text)
            out[slot] = row
        return sep, out

    def _wrap(self, text, width):
        """Word wrap on display cells; long tokens are hard-split."""
        text = sanitize(text)
        if width <= 1:
            return [clip(text, width)]
        words = text.split(" ")
        lines, cur, cur_w = [], "", 0
        for word in words:
            ww = disp_len(word)
            while ww > width:
                head = clip(word, width)
                if cur:
                    lines.append(cur)
                    cur, cur_w = "", 0
                lines.append(head)
                word = word[len(head):]
                ww = disp_len(word)
            if not cur:
                cur, cur_w = word, ww
            elif cur_w + 1 + ww <= width:
                cur += " " + word
                cur_w += 1 + ww
            else:
                lines.append(cur)
                cur, cur_w = word, ww
        lines.append(cur)
        return lines

    def _detail_lines(self, width, now, compact=False):
        """Detail rows. compact=True drops the low-value lines (workspace path,
        attachments, dependency lists, the rule) so the strip under the board
        has room for actual content."""
        d = self.detail
        t = d["task"]
        lines = []

        def push(segs):
            lines.append(segs)

        head = clip("%s  [%s]" % (t["id"], t["status"]), width)
        prio = "prio %s" % (t.get("priority") or 0)
        gap = max(0, width - disp_len(head) - disp_len(prio))
        push([(head, A_STATUS.get(t["status"], A_PLAIN)), (" " * gap, A_PLAIN), (prio, A_DIM)])
        for chunk in self._wrap(t.get("title") or "", width):
            push([(chunk, A_TITLE)])
        push([])

        assignee = clip(t.get("assignee") or "-", max(4, min(24, width // 3)))
        left = fit("assignee " + assignee, max(0, min(width, 34)))
        right = fit(
            "created %s  age %s" % (human_clock(t.get("created_at")), human_age(now - (t.get("created_at") or now))),
            max(0, width - disp_len(left)),
        )
        push([(left, A_PLAIN), (right, A_DIM)])

        run = d["runs"][0] if d["runs"] else None
        if run:
            status = run["status"]
            if status == "running":
                attr = A_OK
            elif status in ("done",):
                attr = A_DIM
            elif status in ("failed", "crashed", "timed_out", "spawn_failed", "gave_up"):
                attr = A_BAD
            else:
                attr = A_WARN
            started = run.get("started_at") or now
            elapsed = (run.get("ended_at") or now) - started
            bits = "  by %s  elapsed %s" % (run.get("profile") or "-", human_age(elapsed))
            if status == "running" and run.get("last_heartbeat_at"):
                bits += "  hb %s ago" % human_age(now - run["last_heartbeat_at"])
            if run.get("max_runtime_seconds"):
                bits += "  limit %s" % human_age(run["max_runtime_seconds"])
            head = fit("run #%s %s" % (run["id"], status), min(width, 26))
            push([(head, attr), (clip(bits, max(0, width - disp_len(head))), A_DIM)])
            if run.get("outcome") and run["outcome"] != status:
                push([(clip("outcome %s" % run["outcome"], width), A_DIM)])
            if run.get("error"):
                for chunk in self._wrap("error: " + run["error"], width):
                    push([(chunk, A_BAD)])

        if t.get("worker_pid"):
            alive = _pid_alive(t["worker_pid"])
            head = fit("worker pid %s %s" % (t["worker_pid"], "alive" if alive else "gone"), min(width, 30))
            tail = ""
            if t.get("last_heartbeat_at"):
                tail = "hb %s ago" % human_age(now - t["last_heartbeat_at"])
            push([(head, A_OK if alive else A_DIM), (clip(tail, max(0, width - disp_len(head))), A_DIM)])

        if t.get("claim_expires"):
            remaining = t["claim_expires"] - now
            text = "claim " + ("expires in " + human_age(remaining) if remaining > 0 else "expired " + human_age(-remaining))
            push([(clip(text, width), A_DIM if remaining > 0 else A_WARN)])

        if t.get("status") == "blocked":
            kind = t.get("block_kind") or "untyped"
            head = fit("blocked: %s  recurrences %s" % (kind, t.get("block_recurrences") or 0), min(width, 42))
            tail = "fails %s" % (t.get("consecutive_failures") or 0)
            push([(head, A_BAD), (clip(tail, max(0, width - disp_len(head))), A_DIM)])
        if t.get("last_failure_error"):
            for chunk in self._wrap("last failure: " + t["last_failure_error"], width):
                push([(chunk, A_WARN)])
        if t.get("scheduled_due_at"):
            push([(clip("scheduled for %s" % human_clock(t["scheduled_due_at"]), width), A_INFO)])
        if t.get("model_override"):
            push([(clip("model %s (%s)" % (t["model_override"], t.get("provider_override") or "profile"), width), A_DIM)])
        if t.get("workspace_path") and not compact:
            push([(clip(t["workspace_path"], width), A_DIM)])
        if d["attachments"] and not compact:
            names = ", ".join("%s (%s)" % (a["filename"], _bytes(a["size"])) for a in d["attachments"])
            push([(clip("attachments " + names, width), A_DIM)])

        deps_p, deps_c = d["parents"], d["children"]
        if (deps_p or deps_c) and not compact:
            push([])
            if deps_p:
                push([(clip("parents: %s" % ", ".join("%s(%s)" % (p["id"], p["status"]) for p in deps_p), width), A_DIM)])
            if deps_c:
                push([(clip("children: %s" % ", ".join("%s(%s)" % (c["id"], c["status"]) for c in deps_c), width), A_DIM)])

        if not compact:
            push([("-" * width, A_BORDER)])

        body_lines = self._body_lines(width)
        tabs = [
            ("body", len(body_lines)),
            ("events", len(d["events"])),
            ("comments", len(d["comments"])),
        ]
        tab_segs = []
        used = 0
        for name, n in tabs:
            active = name == self.detail_tab
            label = " %s(%d) " % (name, n)
            if used + len(label) > width:
                break
            tab_segs.append((label, A_HEADER if active else A_DIM))
            used += len(label)
        push(tab_segs)
        push([])

        tw = min(width, 100)
        if self.detail_tab == "body":
            lines.extend([[(chunk, A_PLAIN)] for chunk in self._body_lines(tw)])
        elif self.detail_tab == "events":
            for ev in d["events"]:
                kind = ev["kind"]
                attr = A_DIM
                if kind in ("blocked", "crashed", "gave_up", "timed_out", "spawn_failed", "worker_lost"):
                    attr = A_BAD
                elif kind in ("completed", "worker_result", "promoted", "unblocked"):
                    attr = A_OK
                elif kind in ("review_requested", "changes_requested", "claimed", "spawned", "created"):
                    attr = A_INFO
                stamp = human_time(ev["created_at"])
                head = "%s %s " % (stamp, fit(kind, 18))
                payload = _event_summary(ev.get("payload"))
                push([(head, attr), (clip(payload, max(0, tw - disp_len(head))), A_PLAIN)])
        else:
            if not d["comments"]:
                push([("(no comments)", A_DIM)])
            for cm in d["comments"]:
                meta = clip("%s  %s" % (human_clock(cm["created_at"]), cm["author"]), width)
                push([(meta, A_INFO)])
                for chunk in self._wrap(cm["body"] or "", max(1, tw - 2)):
                    push([("  " + chunk, A_PLAIN)])
                push([])
        return lines

    def _body_lines(self, width):
        body = (self.detail or {}).get("task", {}).get("body") or "(no body)"
        out = []
        for raw in body.splitlines():
            raw = raw.rstrip()
            if not raw.strip():
                out.append("")
                continue
            for chunk in self._wrap(raw, width):
                out.append(chunk)
        return out

    def _footer_row(self, w):
        if self.compose_mode:
            keys = " [Enter] post comment   [Esc] cancel   [Ctrl-U] clear   (runs: hermes kanban comment)"
            return [(fit(keys, w), A_INFO)]
        if self.search_mode:
            return [(fit("search: %s   [Enter] apply   [Esc] cancel" % self.search_buf, w), A_INFO)]
        if self.show_help:
            return [(fit(" any key closes help ", w), A_DIM)]
        if self.mode(w) == "board":
            if self.focus == "detail":
                keys = " scrolling detail | Tab back to the board  j/k scroll  z hide  ? help  q quit"
            else:
                keys = (
                    " h/l column  j/k card  Tab detail  b list  c comment  s status  "
                    "a assignee  / search  d history  z detail  ? help  q quit"
                )
            return [(fit(keys, w), A_DIM)]
        if self.show_detail and w < 82:
            # One pane at a time: say which one is showing and how to swap.
            where = "detail" if self.focus == "detail" else "list"
            keys = " %s | Tab swaps list/detail  1-3 tab  z hide  b column view  ? help  q quit" % where
            return [(fit(keys, w), A_DIM)]
        keys = (
            " j/k move  Tab pane  1-3 tab  b columns  c comment  s status  a assignee  "
            "/ search  d history  z detail  r refresh  ? help  q quit"
        )
        return [(fit(keys, w), A_DIM)]

    # -- help --------------------------------------------------------------
    def help_lines(self, width):
        rows = [
            [],
            [(fit(" kanban tui %s -- read-only board view " % VERSION, width), A_TITLE)],
            [(fit(" db   %s" % self.store.db_path, width), A_DIM)],
            [(fit(" open %s   board %s" % (self.store.mode, self.store.board_label), width), A_DIM)],
        ]
        if self.terminfo_note:
            rows.append([(fit(" term " + self.terminfo_note, width), A_WARN)])
        entries = [
            ("navigation -- board view (status columns)", [
                ("h / l", "previous / next column (skips empty ones)"),
                ("j / k", "down / up inside the current column"),
                ("g / G", "first / last card in the column"),
                ("Tab, Enter", "focus the detail strip"),
                ("1 / 2 / 3", "detail tab: body / events / comments"),
                ("z", "show or hide the detail strip"),
                ("b", "switch to the list view"),
            ]),
            ("navigation -- list view", [
                ("j / k", "next / previous task"),
                ("g / G", "first / last task"),
                ("Tab, Enter", "switch focus between list and detail"),
                ("1 / 2 / 3", "detail tab: body / events / comments"),
                ("z", "show or hide the detail pane"),
                ("b", "switch to the board view"),
            ]),
            ("either view", [
                ("Space / u", "page down / page up"),
                ("c", "comment on the selected card"),
            ]),
            ("filters", [
                ("s / S", "next / previous status filter"),
                ("a / A", "next / previous assignee filter"),
                ("/", "search id, title, assignee (Enter apply, Esc cancel)"),
                ("d", "show or hide done + archived history"),
                ("r", "refresh now"),
                ("p", "pause or resume auto refresh"),
                ("q", "quit"),
            ]),
        ]
        for title, items in entries:
            rows.append([])
            rows.append([("  " + title, A_HEADER)])
            for key, desc in items:
                rows.append([("   " + fit(key, 15), A_KEY), (clip(desc, max(0, width - 19)), A_PLAIN)])
        rows.append([])
        if self.can_write():
            rows.append([(clip("  c posts a comment through: hermes kanban comment <id> --author %s <text>"
                               % (getattr(self.args, "author", None) or "kanban-tui"), width), A_INFO)])
            rows.append([(clip("  the view itself never writes to kanban.db; run with --no-write to disable.",
                               width), A_DIM)])
        else:
            rows.append([(clip("  writes disabled (--no-write): comment action is unavailable.", width), A_WARN)])
        rows.append([(clip("  no claim, unblock, complete, reassign or dispatch here: those stay in the CLI.",
                           width), A_WARN)])
        if self.herdr.enabled:
            rows.append([(clip("  herdr pane title: %s" % (self.herdr.last_title or "-"), width), A_DIM)])
            if self.herdr.last_error:
                rows.append([(clip("  herdr error: " + self.herdr.last_error, width), A_WARN)])
        return rows


def _bytes(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d%s" % (n, unit)) if unit == "B" else ("%.1f%s" % (n, unit))
        n /= 1024.0
    return "%d" % n


def _event_summary(payload):
    if not payload:
        return ""
    try:
        obj = json.loads(payload)
    except (ValueError, TypeError):
        return sanitize(str(payload))[:200]
    if not isinstance(obj, dict):
        return sanitize(str(obj))[:200]
    keep = []
    for key in ("summary", "reason", "error", "message", "outcome", "status", "detail", "title", "note"):
        val = obj.get(key)
        if val:
            keep.append("%s=%s" % (key, val))
    if keep:
        return " ".join(keep)
    return ", ".join("%s=%s" % (k, v) for k, v in list(obj.items())[:4])


def _pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# --------------------------------------------------------------------------
# key handling
# --------------------------------------------------------------------------
def _normalize_key(ch):
    if not isinstance(ch, int):
        return ch
    mapping = {
        curses.KEY_UP: "k",
        curses.KEY_DOWN: "j",
        curses.KEY_LEFT: "h",
        curses.KEY_RIGHT: "l",
        curses.KEY_HOME: "g",
        curses.KEY_END: "G",
        curses.KEY_NPAGE: " ",
        curses.KEY_PPAGE: "b",
        curses.KEY_ENTER: "\n",
    }
    if ch == curses.KEY_RESIZE:
        return "resize"
    return mapping.get(ch)


def handle_key(self, ch):
    """Translate one key event into a state change. Returns 'quit' or None."""
    ch = _normalize_key(ch)
    if ch is None or ch == "resize":
        return None

    if self.show_help:
        self.show_help = False
        return None

    if self.compose_mode:
        if ch in ("\n", "\r"):
            self.submit_comment()
        elif ch == "\x1b":
            self.compose_mode = False
            self.compose_buf = ""
            self.set_message("comment cancelled")
        elif ch in ("\x7f", "\b") or ch == curses.KEY_BACKSPACE:
            self.compose_buf = self.compose_buf[:-1]
        elif ch == "\x15":
            self.compose_buf = ""
        elif isinstance(ch, str) and ch.isprintable():
            if len(self.compose_buf) < 2000:
                self.compose_buf += ch
        return None

    if self.search_mode:
        if ch in ("\n", "\r"):
            self.search_mode = False
            self.query = self.search_buf
            self._rebuild()
            self.set_message("search: %s" % (self.query or "cleared"))
        elif ch == "\x1b":
            self.search_mode = False
            self.search_buf = ""
            self.set_message("search cancelled")
        elif ch in ("\x7f", "\b") or ch == curses.KEY_BACKSPACE:
            self.search_buf = self.search_buf[:-1]
        elif isinstance(ch, str) and ch.isprintable():
            self.search_buf += ch
        return None

    if ch in ("q", "Q"):
        return "quit"
    if ch == "?":
        self.show_help = True
    elif ch == "j":
        self._move(1)
    elif ch == "k":
        self._move(-1)
    elif ch == " ":
        self._move(10)
    elif ch == "u":
        self._move(-10)
    elif ch == "g":
        if self.focus == "detail":
            self.detail_offset = 0
        elif self.mode() == "board":
            self.select_in_column(-10 ** 6)
        else:
            self.select_first()
    elif ch == "G":
        if self.focus == "detail":
            self.detail_offset = 10 ** 6
        elif self.mode() == "board":
            self.select_in_column(10 ** 6)
        else:
            self.select_last()
    elif ch == "b":
        self.mode_override = "list" if self.mode() == "board" else "board"
        self.focus = "list"
        self.detail_offset = 0
        self.set_message("view: %s" % self.mode_override)
    elif ch in ("\t", "\n", "\r"):
        self.focus = "detail" if self.focus == "list" else "list"
        self.detail_offset = 0
    elif ch == "l":
        if self.mode() == "board":
            if self.focus == "detail":
                self.focus = "list"
            else:
                self.select_column(1)
        else:
            self.focus = "detail"
            self.detail_offset = 0
    elif ch == "h":
        if self.mode() == "board" and self.focus == "detail":
            self.focus = "list"
        elif self.mode() == "board":
            self.select_column(-1)
        else:
            self.focus = "list"
    elif ch in ("1", "2", "3"):
        self.detail_tab = {"1": "body", "2": "events", "3": "comments"}[ch]
        self.detail_offset = 0
        self.focus = "detail"
        self.show_detail = True
    elif ch == "s":
        self.cycle_status(1)
    elif ch == "S":
        self.cycle_status(-1)
    elif ch == "a":
        self.cycle_assignee(1)
    elif ch == "A":
        self.cycle_assignee(-1)
    elif ch == "c":
        if not self.can_write():
            self.set_message("comment disabled (--no-write)")
        elif not self.selected_id:
            self.set_message("no task selected")
        else:
            self.compose_mode = True
            self.compose_buf = ""
            self.compose_task = self.selected_id
    elif ch == "/":
        self.search_mode = True
        self.search_buf = self.query
    elif ch == "d":
        self.toggle_closed()
    elif ch == "z":
        self.show_detail = not self.show_detail
        self.set_message("detail pane %s" % ("shown" if self.show_detail else "hidden"))
    elif ch == "r":
        self.refresh(force=True)
        self.set_message("refreshed %s" % human_time(self.last_refresh))
    elif ch == "p":
        self.paused = not self.paused
        self.set_message("auto refresh %s" % ("paused" if self.paused else "resumed"))
    return None


def _move(self, delta):
    if self.focus == "detail":
        self.detail_offset = max(0, self.detail_offset + delta)
    elif self.mode() == "board":
        self.select_in_column(delta)
    else:
        self.select_relative(delta)


App.handle_key = handle_key
App._move = _move


# --------------------------------------------------------------------------
# curses terminal
# --------------------------------------------------------------------------
def ensure_terminal(stream=None):
    """Pick a TERM whose terminfo entry actually exists.

    Ghostty reports TERM=xterm-ghostty, and that entry is absent from a stock
    ncurses-term package, which makes curses die with 'setupterm: could not
    find terminal'. Ghostty is xterm-256color compatible, so fall back to that
    and report why.
    """
    fd = (stream or sys.stdout).fileno()
    wanted = (os.environ.get("TERM") or "").strip()
    candidates = []
    if wanted:
        candidates.append(wanted)
    for extra in ("xterm-256color", "screen-256color", "xterm", "ansi"):
        if extra not in candidates:
            candidates.append(extra)
    errors = []
    for term in candidates:
        try:
            curses.setupterm(term=term, fd=fd)
        except (curses.error, OSError) as exc:
            errors.append("%s(%s)" % (term, exc))
            continue
        os.environ["TERM"] = term
        note = ""
        if wanted and term != wanted:
            note = "%s has no terminfo entry here; using %s" % (wanted, term)
        return term, note
    raise SystemExit(
        "kanban tui: no usable terminfo entry (tried %s). Install ncurses-term or set TERM=xterm-256color."
        % ", ".join(errors or candidates)
    )


def _init_colors():
    """Colour pairs for statuses and chrome. Safe on 8-colour terminals."""
    try:
        if not curses.has_colors():
            return
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        palette = {
            "triage": curses.COLOR_MAGENTA,
            "todo": curses.COLOR_BLUE,
            "scheduled": curses.COLOR_YELLOW,
            "ready": curses.COLOR_CYAN,
            "running": curses.COLOR_GREEN,
            "review": curses.COLOR_MAGENTA,
            "blocked": curses.COLOR_RED,
            "done": curses.COLOR_WHITE,
            "archived": curses.COLOR_WHITE,
        }
        for status in STATUS_ORDER:
            try:
                curses.init_pair(A_STATUS[status], palette[status], bg)
            except curses.error:
                pass
        for pair, fg, b in (
            (A_HEADER, curses.COLOR_BLACK, curses.COLOR_CYAN),
            (A_DIM, curses.COLOR_WHITE, bg),
            (A_TITLE, curses.COLOR_WHITE, bg),
            (A_OK, curses.COLOR_GREEN, bg),
            (A_WARN, curses.COLOR_YELLOW, bg),
            (A_BAD, curses.COLOR_RED, bg),
            (A_INFO, curses.COLOR_CYAN, bg),
            (A_KEY, curses.COLOR_BLUE, bg),
            (A_BORDER, curses.COLOR_BLUE, bg),
        ):
            try:
                curses.init_pair(pair, fg, b)
            except curses.error:
                pass
    except curses.error:  # pragma: no cover
        pass


def _attr(slot):
    if slot == A_PLAIN:
        return curses.A_NORMAL
    if slot == A_SELECTED:
        return curses.A_REVERSE
    if slot == A_HEADER:
        return curses.color_pair(slot) | curses.A_BOLD
    return curses.color_pair(slot)


def paint(screen, frame):
    """Draw a frame, rewriting only the rows that changed.

    A full erase + repaint on every tick flickers inside a herdr pane, and
    the flicker shows up as scrollback noise. Diffing rows keeps a steady
    board almost silent.
    """
    if screen.last is None:
        try:
            screen.scr.erase()
        except curses.error:
            pass
        changed = range(len(frame))
    else:
        changed = [
            i for i, row in enumerate(frame)
            if i >= len(screen.last) or row != screen.last[i]
        ]
        if len(changed) > len(frame) * 0.6:
            try:
                screen.scr.erase()
            except curses.error:
                pass
            changed = range(len(frame))
    for y in changed:
        screen.line(y, [(text, _attr(slot)) for text, slot in frame[y]])
    screen.last = [list(row) for row in frame]
    try:
        screen.scr.refresh()
    except curses.error:
        pass


def run_interactive(app):
    if curses is None:
        sys.stderr.write(NO_CURSES + "\n")
        return 1
    term, note = ensure_terminal()
    app.terminfo_note = note
    os.environ.setdefault("ESCDELAY", "25")
    mouse = bool(getattr(app.args, "mouse", False))
    stdscr = curses.initscr()
    try:
        curses.noecho()
        curses.cbreak()
        stdscr.keypad(True)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        _init_colors()
        if mouse:
            try:
                curses.mousemask(curses.ALL_MOUSE_EVENTS)
                curses.mouseinterval(150)
            except curses.error:
                mouse = False
        stdscr.nodelay(True)
        screen = CursesScreen(stdscr)
        last_draw = 0.0
        last_size = (screen.h, screen.w)
        dirty = True
        while True:
            now = time.time()
            if not app.paused and now - app.last_refresh >= app.interval:
                app.refresh()
                dirty = True
            if dirty or now - last_draw > 0.5:
                screen.resize()
                if (screen.h, screen.w) != last_size:
                    # A resize invalidates every painted cell.
                    screen.last = None
                    last_size = (screen.h, screen.w)
                frame = app.render(screen.h, screen.w)
                try:
                    paint(screen, frame)
                except curses.error:
                    pass
                last_draw = time.time()
                dirty = False
            try:
                ch = stdscr.get_wch()
            except curses.error:
                ch = None
            except KeyboardInterrupt:
                break
            if ch is None:
                time.sleep(0.04)
                continue
            if mouse and ch == curses.KEY_MOUSE:
                try:
                    _id, mx, my, _z, bstate = curses.getmouse()
                except curses.error:
                    continue
                # Left click, and also right click: herdr can forward
                # right-click to a pane app (`pane input --right-click pane`)
                # while still keeping its own mouse UI, which is the only way to
                # get pointer selection without turning mouse capture off.
                click_bits = (
                    curses.BUTTON1_CLICKED | curses.BUTTON1_RELEASED | curses.BUTTON1_PRESSED
                    | curses.BUTTON3_CLICKED | curses.BUTTON3_RELEASED | curses.BUTTON3_PRESSED
                )
                if bstate & click_bits:
                    app.click(mx, my)
                if bstate & curses.BUTTON1_DOUBLE_CLICKED:
                    app.click(mx, my)
                    app.focus = "detail"
                elif bstate & (curses.BUTTON4_PRESSED | getattr(curses, "BUTTON4_CLICKED", 0)):
                    app.wheel(-3)
                elif bstate & (curses.BUTTON5_PRESSED | getattr(curses, "BUTTON5_CLICKED", 0)):
                    app.wheel(3)
                dirty = True
                continue
            if app.handle_key(ch) == "quit":
                break
            dirty = True
        return 0
    finally:
        try:
            app.herdr.clear()
        except Exception:
            pass
        try:
            if mouse:
                curses.mousemask(0)
        except curses.error:
            pass
        try:
            stdscr.keypad(False)
            curses.nocbreak()
            curses.echo()
            try:
                curses.curs_set(1)
            except curses.error:
                pass
            curses.endwin()
        except curses.error:
            pass


# --------------------------------------------------------------------------
# snapshot mode
# --------------------------------------------------------------------------
def build_json(app):
    """Machine-readable board, for Fleet/HQ/watchdogs.

    Same filters as the UI, same read-only connection; nothing here is inferred
    from rendered text.
    """
    now = time.time()
    tasks = []
    for t in app.filtered():
        created = t.get("created_at") or now
        run = app.live_runs.get(t["id"])
        entry = {
            "id": t["id"],
            "title": t.get("title"),
            "status": t.get("status"),
            "assignee": t.get("assignee"),
            "priority": t.get("priority") or 0,
            "created_at": t.get("created_at"),
            "age_seconds": int(max(0, now - created)),
            "started_at": t.get("started_at"),
            "completed_at": t.get("completed_at"),
            "open_parents": app.parent_waits.get(t["id"], 0),
            "scheduled_due_at": t.get("scheduled_due_at"),
            "model_override": t.get("model_override"),
            "provider_override": t.get("provider_override"),
            "workspace_path": t.get("workspace_path"),
        }
        if t.get("status") == "blocked":
            entry["block"] = {
                "kind": t.get("block_kind"),
                "recurrences": t.get("block_recurrences") or 0,
                "consecutive_failures": t.get("consecutive_failures") or 0,
                "last_failure_error": t.get("last_failure_error"),
            }
        if run:
            started = run.get("started_at") or now
            entry["current_run"] = {
                "id": run["id"],
                "status": run["status"],
                "profile": run.get("profile"),
                "started_at": run.get("started_at"),
                "elapsed_seconds": int(max(0, (run.get("ended_at") or now) - started)),
                "max_runtime_seconds": run.get("max_runtime_seconds"),
                "heartbeat_age_seconds": (
                    int(max(0, now - run["last_heartbeat_at"]))
                    if run.get("last_heartbeat_at") else None
                ),
            }
        signal, severity = app._card_signal(t, now)
        if signal:
            entry["signal"] = signal
            entry["severity"] = severity or "info"
        tasks.append(entry)

    return {
        "schema": "hermes-kanban-tui/1",
        "generated_at": int(now),
        "board": app.store.board_label,
        "db": app.store.db_path,
        "filters": {
            "status": app.status_filter or None,
            "assignee": app.assignee_filter or None,
            "query": app.query or None,
            "include_closed": bool(app.include_closed),
        },
        "counts": app.counts,
        "task_count": len(tasks),
        "tasks": tasks,
    }


def render_text(app, width, height, now=None):
    screen = TextScreen(width, height)
    for y, segs in enumerate(app.render(height, width, now=now)):
        screen.line(y, segs)
    return screen.render()


def parse_size(text):
    m = re.fullmatch(r"\s*(\d+)\s*[xX]\s*(\d+)\s*", text or "")
    if not m:
        raise SystemExit("kanban tui: size must be WxH, e.g. 120x40")
    return int(m.group(1)), int(m.group(2))


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        prog="kanban-tui",
        description="Read-only Hermes Kanban board for the terminal (herdr and Ghostty safe).",
    )
    p.add_argument("--db", help="path to kanban.db (default: HERMES_KANBAN_DB, else current board)")
    p.add_argument("--board", help="board slug (non-default boards live under kanban/boards/<slug>/)")
    p.add_argument("--status", default="", help="start filtered to one status")
    p.add_argument("--assignee", default="", help="start filtered to one assignee")
    p.add_argument("--query", default="", help="start with a search string")
    p.add_argument("--all", action="store_true", help="include done and archived tasks")
    p.add_argument("--interval", type=float, default=2.0, help="auto-refresh seconds (default 2)")
    p.add_argument("--no-herdr", action="store_true", help="do not report pane metadata to herdr")
    p.add_argument("--mouse", action="store_true",
                   help="enable mouse: click a card to select it, wheel to scroll")
    p.add_argument("--no-write", action="store_true",
                   help="disable the comment action entirely (read-only)")
    p.add_argument("--author", metavar="NAME",
                   help="author name recorded on comments (default kanban-tui)")
    p.add_argument("--print", nargs="?", const="120x40", metavar="WxH",
                   help="render one frame as text and exit (default 120x40)")
    p.add_argument("--json", action="store_true",
                   help="print the filtered board as JSON and exit")
    p.add_argument("--history-cap", type=int, default=HISTORY_CAP, metavar="N",
                   help="cards to load per done/archived column (default %d)" % HISTORY_CAP)
    p.add_argument("--size", metavar="WxH", help="frame size for --print")
    p.add_argument("--where", action="store_true",
                   help="print what the view resolved (db, terminfo, herdr, install) and exit")
    p.add_argument("--pane", action="store_true",
                   help="open the view in a fresh herdr workspace")
    p.add_argument("--version", action="version", version="kanban tui %s" % VERSION)
    return p


def _terminfo_state(name):
    """True/False, or None when infocmp is not installed to ask."""
    if not shutil.which("infocmp"):
        return None
    return subprocess.run(["infocmp", name], capture_output=True).returncode == 0


def where_report(path, board):
    """Everything a user needs to see why the view is looking where it is."""
    state = _terminfo_state("xterm-ghostty")
    if state is None:
        terminfo = "unknown (no infocmp)"
    elif state:
        terminfo = "xterm-ghostty present"
    else:
        terminfo = "xterm-ghostty missing (view falls back to xterm-256color)"
    in_pane = os.environ.get("HERDR_ENV") == "1"
    rows = [
        ("kanban-tui", "%s" % VERSION),
        ("python", "%s (%s)" % (sys.version.split()[0], sys.executable)),
        ("installed", os.path.abspath(__file__)),
        ("db", path),
        ("exists", "yes" if os.path.exists(path) else "no"),
        ("size", "%d bytes" % os.path.getsize(path) if os.path.exists(path) else "-"),
        ("board", board or "default"),
        ("terminfo", terminfo),
        ("herdr", "pane %s" % os.environ.get("HERDR_PANE_ID", "?") if in_pane
         else "not in a herdr pane"),
    ]
    width = max(len(k) for k, _ in rows)
    return "\n".join("%-*s  %s" % (width, k, v) for k, v in rows)


def open_in_pane(forward):
    """Create a herdr workspace and run this same view in its root pane."""
    herdr = shutil.which("herdr")
    if not herdr:
        sys.stderr.write("kanban-tui: --pane needs the herdr CLI on PATH\n")
        return 1
    created = subprocess.run(
        [herdr, "workspace", "create", "--label", "kanban",
         "--cwd", os.path.expanduser("~"), "--no-focus"],
        capture_output=True, text=True,
    )
    if created.returncode != 0:
        sys.stderr.write("kanban-tui: herdr workspace create failed: %s\n"
                         % (created.stderr.strip() or "no output"))
        return 1
    try:
        pane = json.loads(created.stdout)["result"]["root_pane"]["pane_id"]
    except (ValueError, KeyError):
        sys.stderr.write("kanban-tui: could not read the pane id from herdr\n")
        return 1
    cmd = " ".join(shlex.quote(part) for part in
                   [sys.executable, os.path.abspath(__file__)] + forward)
    # Drop back to a shell when the view exits, otherwise quitting would close
    # the pane and take the workspace with it.
    subprocess.run([herdr, "pane", "run", pane,
                    "TERM=xterm-256color %s; exec bash -l" % cmd])
    sys.stdout.write("kanban-tui: opened in herdr workspace pane %s\n" % pane)
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    pane = "--pane" in argv
    forward = [a for a in argv if a != "--pane"]
    args = build_parser().parse_args(argv)
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    if args.where:
        # Resolve without connecting: a missing or unreadable board is exactly
        # the case where someone runs --where.
        path = KanbanStore._resolve_path(args.db, args.board)
        sys.stdout.write(where_report(path, args.board or "default") + "\n")
        return 0
    store = KanbanStore(args.db, args.board)
    try:
        if pane:
            return open_in_pane(forward)
        app = App(store, args)
        if args.json:
            sys.stdout.write(json.dumps(build_json(app), indent=2, sort_keys=False) + "\n")
            return 0
        if args.print is not None:
            width, height = parse_size(args.size or args.print)
            sys.stdout.write(render_text(app, width, height) + "\n")
            return 0
        return run_interactive(app)
    finally:
        store.close()


# The console-script entry point (pyproject: kanban-tui = "kanban_tui:cli_main").
cli_main = main


if __name__ == "__main__":
    sys.exit(cli_main())
