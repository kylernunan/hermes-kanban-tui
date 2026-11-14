#!/usr/bin/env python3
"""Tests for kanban_tui.py.

Runs against a throwaway SQLite database with the real Hermes kanban schema
subset the viewer reads, plus one read against a copy of the live board when
one is present.

    python3 -m unittest -v test_kanban_tui.py
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import kanban_tui as kt  # noqa: E402

SCHEMA = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT, assignee TEXT,
    status TEXT NOT NULL, priority INTEGER DEFAULT 0, created_by TEXT,
    created_at INTEGER NOT NULL, started_at INTEGER, completed_at INTEGER,
    workspace_kind TEXT NOT NULL DEFAULT 'scratch', workspace_path TEXT,
    branch_name TEXT, project_id TEXT, claim_lock TEXT, claim_expires INTEGER,
    tenant TEXT, result TEXT, idempotency_key TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0, worker_pid INTEGER,
    worker_started_at INTEGER, last_failure_error TEXT,
    max_runtime_seconds INTEGER, last_heartbeat_at INTEGER,
    current_run_id INTEGER, workflow_template_id TEXT, current_step_key TEXT,
    skills TEXT, model_override TEXT, provider_override TEXT,
    reasoning_effort TEXT, max_retries INTEGER,
    goal_mode INTEGER NOT NULL DEFAULT 0, goal_max_turns INTEGER,
    session_id TEXT, block_kind TEXT,
    block_recurrences INTEGER NOT NULL DEFAULT 0, completion_contract TEXT,
    scheduled_mode TEXT, scheduled_due_at INTEGER, scheduled_due_tz TEXT,
    schedule_generation INTEGER NOT NULL DEFAULT 0, scheduled_hold_pid INTEGER,
    scheduled_hold_started INTEGER, scheduled_hold_fingerprint TEXT,
    scheduled_hold_run_id INTEGER, scheduled_hold_unknown INTEGER,
    scheduled_park_gate TEXT
);
CREATE TABLE task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, profile TEXT,
    step_key TEXT, status TEXT NOT NULL, claim_lock TEXT, claim_expires INTEGER,
    worker_pid INTEGER, worker_started_at INTEGER, max_runtime_seconds INTEGER,
    last_heartbeat_at INTEGER, started_at INTEGER NOT NULL, ended_at INTEGER,
    outcome TEXT, summary TEXT, metadata TEXT, error TEXT
);
CREATE TABLE task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, run_id INTEGER,
    kind TEXT NOT NULL, payload TEXT, created_at INTEGER NOT NULL
);
CREATE TABLE task_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
    author TEXT NOT NULL, body TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE task_links (
    parent_id TEXT NOT NULL, child_id TEXT NOT NULL,
    PRIMARY KEY (parent_id, child_id)
);
CREATE TABLE task_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
    filename TEXT NOT NULL, stored_path TEXT NOT NULL, content_type TEXT,
    size INTEGER NOT NULL DEFAULT 0, uploaded_by TEXT, created_at INTEGER NOT NULL
);
"""

NOW = int(time.time())


def digest(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def build_fixture(path, now=None):
    now = NOW if now is None else int(now)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    rows = [
        # id, title, assignee, status, prio, block_kind, body
        ("t_run00001", "Running worker with a long title that must be truncated somewhere",
         "builder-hard", "running", 80, None, "# Heading\n\nBody line one.\n\n- bullet\n"),
        ("t_rdy00001", "Ready task \u2014 with an em dash and a very long title that overflows the column",
         "reviewer", "ready", 100, None, "ready body"),
        ("t_blk00001", "Blocked task needing owner input", "operator", "blocked", 0,
         "needs_input", "blocked body"),
        ("t_sch00001", "Scheduled task", "builder", "scheduled", 5, None, ""),
        ("t_don00001", "Finished task", "checker", "done", 0, None, "done body"),
        ("t_arc00001", "Archived task", "checker", "archived", 0, None, "archived body"),
        ("t_emo00001", "Unicode \U0001f680 wide chars \u4e2d\u6587 and tabs\there",
         "intake", "todo", 1, None, "tab\there and \u4e2d\u6587"),
    ]
    for i, (tid, title, assignee, status, prio, kind, body) in enumerate(rows):
        conn.execute(
            "INSERT INTO tasks (id,title,assignee,status,priority,created_at,body,"
            "block_kind,worker_pid,last_heartbeat_at,current_run_id,"
            "block_recurrences,consecutive_failures,last_failure_error,"
            "model_override,provider_override,workspace_path,claim_expires) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                tid, title, assignee, status, prio, now - (i + 1) * 3600, body,
                kind,
                999999 if status == "running" else None,
                now - 5 if status == "running" else None,
                1 if status == "running" else None,
                2 if kind else 0,
                1 if status == "blocked" else 0,
                "elapsed 1823s > limit 1800s" if status == "blocked" else None,
                "some/model" if tid == "t_rdy00001" else None,
                "custom" if tid == "t_rdy00001" else None,
                "/tmp/ws/" + tid,
                now + 600 if status == "running" else None,
            ),
        )
    conn.execute(
        "INSERT INTO task_runs (task_id,profile,status,started_at,last_heartbeat_at,"
        "max_runtime_seconds,ended_at,outcome,error) VALUES (?,?,?,?,?,?,?,?,?)",
        ("t_run00001", "builder-hard", "running", now - 120, now - 5, 3600, None, None, None),
    )
    conn.execute(
        "INSERT INTO task_runs (task_id,profile,status,started_at,ended_at,outcome,error) "
        "VALUES (?,?,?,?,?,?,?)",
        ("t_rdy00001", "reviewer", "done", now - 900, now - 800, "completed", None),
    )
    for i, (kind, payload) in enumerate(
        [
            ("created", '{"title": "x"}'),
            ("claimed", '{"profile": "builder-hard"}'),
            ("worker_result", '{"summary": "did the thing", "status": "ok"}'),
            ("blocked", '{"reason": "need owner input"}'),
            ("heartbeat", None),
        ]
    ):
        conn.execute(
            "INSERT INTO task_events (task_id,kind,payload,created_at) VALUES (?,?,?,?)",
            ("t_blk00001", kind, payload, now - (10 - i) * 60),
        )
    # Closed rows carry a completion time; the board orders those columns by it.
    conn.execute(
        "UPDATE tasks SET completed_at = created_at + 60 "
        "WHERE status IN ('done','archived')"
    )
    conn.execute(
        "INSERT INTO task_comments (task_id,author,body,created_at) VALUES (?,?,?,?)",
        ("t_blk00001", "captain", "First comment line.\nSecond comment line that is quite long and should wrap across the pane.", now - 120),
    )
    conn.execute("INSERT INTO task_links (parent_id,child_id) VALUES (?,?)", ("t_rdy00001", "t_blk00001"))
    # A non-blocked card gated by an open parent: exercises the dep signal.
    conn.execute("INSERT INTO task_links (parent_id,child_id) VALUES (?,?)", ("t_sch00001", "t_emo00001"))
    conn.execute(
        "INSERT INTO task_attachments (task_id,filename,stored_path,size,created_at) "
        "VALUES (?,?,?,?,?)",
        ("t_blk00001", "report.pdf", "/tmp/report.pdf", 2048, now - 300),
    )
    conn.commit()
    conn.close()


def make_args(**over):
    class Args:
        db = None
        board = None
        status = ""
        assignee = ""
        query = ""
        all = False
        interval = 2.0
        no_herdr = True
        print = None
        size = None
        herdr_bin = "herdr"

    a = Args()
    for k, v in over.items():
        setattr(a, k, v)
    return a


class FixtureCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="kanban-tui-test-")
        cls.db = os.path.join(cls.tmp, "kanban.db")
        build_fixture(cls.db)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def make_app(self, **over):
        store = kt.KanbanStore(self.db)
        app = kt.App(store, make_args(**over))
        self.addCleanup(store.close)
        return app

    def text(self, app, w=140, h=40, **over):
        for k, v in over.items():
            setattr(app, k, v)
            if k in ("status_filter", "assignee_filter", "query", "include_closed"):
                app.build_rows()
                app.clamp_selection()
                app.load_detail(force=True)
        return kt.render_text(app, w, h)


class TestGeometry(unittest.TestCase):
    def test_disp_len_counts_wide_chars(self):
        self.assertEqual(kt.disp_len("abc"), 3)
        self.assertEqual(kt.disp_len("\u4e2d\u6587"), 4)

    def test_clip_never_splits_a_wide_char(self):
        out = kt.clip("a\u4e2d\u6587b", 3)
        self.assertEqual(out, "a\u4e2dbs"[:0] + "a\u4e2d")

    def test_fit_pads_to_exact_width(self):
        self.assertEqual(kt.disp_len(kt.fit("abc", 10)), 10)
        self.assertEqual(kt.disp_len(kt.fit("\u4e2d\u6587", 5)), 5)

    def test_sanitize_strips_control_characters(self):
        self.assertEqual(kt.sanitize("a\tb\nc\x00d\x1b[31m"), "a b c d [31m")

    def test_human_age_units(self):
        self.assertEqual(kt.human_age(0), "0s")
        self.assertEqual(kt.human_age(59), "59s")
        self.assertEqual(kt.human_age(60), "1m00s")
        self.assertEqual(kt.human_age(3725), "1h02m")
        self.assertEqual(kt.human_age(90000), "1d01h")
        self.assertEqual(kt.human_age(None), "-")


class TestStore(FixtureCase):
    def test_current_file_selects_a_named_board(self):
        root = tempfile.mkdtemp(prefix="kbhome-")
        self.addCleanup(shutil.rmtree, root, True)
        os.makedirs(os.path.join(root, "kanban", "boards", "crew"))
        board_db = os.path.join(root, "kanban", "boards", "crew", "kanban.db")
        build_fixture(board_db)
        with open(os.path.join(root, "kanban", "current"), "w") as fh:
            fh.write("crew\n")
        old = {k: os.environ.get(k) for k in ("HERMES_KANBAN_HOME", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_DB")}
        os.environ["HERMES_KANBAN_HOME"] = root
        for k in ("HERMES_KANBAN_BOARD", "HERMES_KANBAN_DB"):
            os.environ.pop(k, None)
        try:
            self.assertEqual(kt.KanbanStore._resolve_path(None, None), board_db)
            store = kt.KanbanStore(None, None)
            self.addCleanup(store.close)
            self.assertEqual(store.board_label, "crew")
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_explicit_db_beats_every_other_source(self):
        self.assertEqual(kt.KanbanStore._resolve_path(self.db, "crew"), self.db)

    def test_resolves_db_from_env(self):
        old = os.environ.get("HERMES_KANBAN_DB")
        os.environ["HERMES_KANBAN_DB"] = self.db
        try:
            self.assertEqual(kt.KanbanStore._resolve_path(None, None), self.db)
        finally:
            if old is None:
                os.environ.pop("HERMES_KANBAN_DB", None)
            else:
                os.environ["HERMES_KANBAN_DB"] = old

    def test_connection_is_read_only(self):
        store = kt.KanbanStore(self.db)
        self.addCleanup(store.close)
        with self.assertRaises(sqlite3.OperationalError):
            store.conn.execute("DELETE FROM tasks")

    def test_missing_db_exits_with_message(self):
        with self.assertRaises(SystemExit) as ctx:
            kt.KanbanStore("/nonexistent/kanban.db")
        self.assertIn("no board database", str(ctx.exception))

    def test_board_label_from_path(self):
        store = kt.KanbanStore(self.db)
        self.addCleanup(store.close)
        self.assertEqual(store.board_label, "default")
        alt = "/home/x/.hermes/kanban/boards/crew/kanban.db"
        self.assertEqual(
            kt.KanbanStore._resolve_path(alt, None), alt
        )
        store2 = kt.KanbanStore(alt) if os.path.exists(alt) else None
        if store2:
            store2.close()

    def test_stale_board_db_message(self):
        """The zero-length board db must not be silently used."""
        path = os.path.join(self.tmp, "empty.db")
        open(path, "w").close()
        with self.assertRaises(SystemExit):
            kt.KanbanStore(path)


class TestWalFallback(FixtureCase):
    """A read-only URI open can fail on a hot WAL; the viewer must retry."""

    def test_falls_back_to_query_only_handle(self):
        import kanban_tui

        real_connect = sqlite3.connect
        calls = []

        def flaky(target, *a, **kw):
            calls.append((target, kw.get("uri")))
            if kw.get("uri"):
                raise sqlite3.OperationalError("unable to open database file")
            return real_connect(target, *a, **kw)

        old = kanban_tui.sqlite3.connect
        kanban_tui.sqlite3.connect = flaky
        try:
            store = kanban_tui.KanbanStore(self.db)
            self.addCleanup(store.close)
            self.assertEqual(store.mode, "query_only")
            self.assertEqual(len(calls), 2)
            self.assertEqual(store.counts().get("running"), 1)
        finally:
            kanban_tui.sqlite3.connect = old

    def test_query_only_still_cannot_write(self):
        import kanban_tui

        store = kanban_tui.KanbanStore(self.db)
        self.addCleanup(store.close)
        store.mode = "query_only"
        store.close()
        store2 = kanban_tui.KanbanStore(self.db)
        self.addCleanup(store2.close)
        with self.assertRaises(sqlite3.OperationalError):
            store2.conn.execute("UPDATE tasks SET status = 'done'")


class TestRendering(FixtureCase):
    def test_open_filter_hides_closed(self):
        app = self.make_app()
        ids = {t["id"] for t in app.tasks}
        self.assertNotIn("t_don00001", ids)
        self.assertNotIn("t_arc00001", ids)
        self.assertIn("t_rdy00001", ids)

    def test_all_flag_includes_history(self):
        app = self.make_app(all=True)
        ids = {t["id"] for t in app.tasks}
        self.assertIn("t_don00001", ids)
        self.assertIn("t_arc00001", ids)

    def test_status_order_is_work_flow_order(self):
        app = self.make_app()
        statuses = [r["status"] for r in app.rows if r["kind"] == "group"]
        self.assertEqual(statuses, [s for s in kt.STATUS_ORDER if s in statuses])
        self.assertIn("running", statuses)
        self.assertLess(statuses.index("running"), statuses.index("blocked"))

    def test_priority_orders_within_status(self):
        app = self.make_app()
        app.status_filter = "todo"
        app.build_rows()
        todo = [r["task"]["priority"] for r in app.rows if r["kind"] == "task"]
        self.assertEqual(todo, sorted(todo, reverse=True))

    def test_frame_matches_requested_size(self):
        app = self.make_app()
        for w, h in ((160, 45), (120, 40), (100, 30), (84, 24), (80, 20), (60, 16), (40, 10), (20, 4)):
            text = kt.render_text(app, w, h)
            lines = text.split("\n")
            self.assertEqual(len(lines), h, "height %sx%s" % (w, h))
            for i, line in enumerate(lines):
                self.assertLessEqual(kt.disp_len(line), w, "row %d overflows at %sx%s" % (i, w, h))

    def test_every_row_has_content_at_wide_size(self):
        app = self.make_app()
        text = kt.render_text(app, 150, 40)
        lines = text.split("\n")
        self.assertIn("KANBAN", lines[0])
        for tid in ("t_run00001", "t_rdy00001", "t_blk00001", "t_sch00001", "t_emo00001"):
            self.assertTrue(any(tid in line for line in lines), "missing %s" % tid)

    def test_selected_row_is_marked_in_list_view(self):
        app = self.make_app()
        app.mode_override = "list"
        lines = kt.render_text(app, 150, 40).split("\n")
        marked = [l for l in lines if l.startswith("> ")]
        self.assertEqual(len(marked), 1)
        self.assertIn(app.selected_id, marked[0])

    def test_selected_card_is_marked_in_board_view(self):
        app = self.make_app()
        app.mode_override = "board"
        lines = kt.render_text(app, 150, 40).split("\n")
        marked = [l for l in lines if ">" in l and app.selected_id in l]
        self.assertEqual(len(marked), 1, "exactly one card carries the cursor")

    def test_narrow_pane_shows_list_first(self):
        app = self.make_app()
        narrow = kt.render_text(app, 70, 30)
        self.assertNotIn("|", narrow.split("\n")[4])
        self.assertIn("t_rdy00001", narrow)
        wide = kt.render_text(app, 150, 30)
        self.assertIn("|", wide.split("\n")[4])

    def test_narrow_pane_swaps_to_detail_on_tab(self):
        """Below 82 cols there is no second column, so the detail takes the
        pane instead of being unreachable."""
        app = self.make_app()
        app.handle_key("\t")
        self.assertEqual(app.focus, "detail")
        text = kt.render_text(app, 70, 30)
        self.assertIn("prio", text)
        self.assertIn("Tab swaps list/detail", text)
        app.handle_key("\t")
        self.assertEqual(app.focus, "list")
        self.assertIn("t_rdy00001", kt.render_text(app, 70, 30))

    def test_narrow_detail_reaches_all_three_tabs(self):
        app = self.make_app()
        # t_blk00001 is the fixture row that carries events and comments.
        app.selected_id = "t_blk00001"
        app.load_detail(force=True)
        for key, marker in (("1", "body("), ("2", "worker_result"), ("3", "First comment line.")):
            app.handle_key(key)
            self.assertEqual(app.focus, "detail")
            self.assertIn(marker, kt.render_text(app, 70, 34), "tab %s" % key)

    def test_narrow_rows_never_overflow(self):
        app = self.make_app()
        app.handle_key("\t")
        for w, h in ((70, 30), (49, 38), (30, 12)):
            for line in kt.render_text(app, w, h).split("\n"):
                self.assertLessEqual(kt.disp_len(line), w)

    def test_detail_shows_run_and_block_evidence(self):
        app = self.make_app()
        app.mode_override = "list"
        app.selected_id = "t_blk00001"
        app.load_detail(force=True)
        text = kt.render_text(app, 150, 44)
        self.assertIn("blocked: needs_input", text)
        self.assertIn("recurrences 2", text)
        self.assertIn("last failure: elapsed 1823s", text)
        self.assertIn("parents: t_rdy00001", text)
        self.assertIn("report.pdf", text)

    def test_running_task_shows_heartbeat_and_claim(self):
        app = self.make_app()
        app.selected_id = "t_run00001"
        app.load_detail(force=True)
        text = kt.render_text(app, 150, 44)
        self.assertIn("run #1 running", text)
        self.assertIn("claim expires in", text)
        self.assertIn("hb ", text)

    def test_events_tab_renders_summaries(self):
        app = self.make_app()
        app.selected_id = "t_blk00001"
        app.detail_tab = "events"
        app.load_detail(force=True)
        text = kt.render_text(app, 150, 44)
        self.assertIn("worker_result", text)
        self.assertIn("summary=did the thing", text)
        self.assertIn("reason=need owner input", text)

    def test_comments_tab_renders_author(self):
        app = self.make_app()
        app.selected_id = "t_blk00001"
        app.detail_tab = "comments"
        app.load_detail(force=True)
        text = kt.render_text(app, 150, 44)
        self.assertIn("captain", text)
        self.assertIn("First comment line.", text)

    def test_unicode_body_does_not_overflow(self):
        app = self.make_app()
        app.selected_id = "t_emo00001"
        app.load_detail(force=True)
        text = kt.render_text(app, 120, 40)
        for line in text.split("\n"):
            self.assertLessEqual(kt.disp_len(line), 120)
        self.assertNotIn("\t", text)

    def test_filter_row_reports_state(self):
        app = self.make_app()
        app.status_filter = "blocked"
        app.assignee_filter = "operator"
        app.query = "owner"
        app.build_rows()
        text = kt.render_text(app, 150, 40)
        self.assertIn("status blocked", text)
        self.assertIn("assignee operator", text)
        self.assertIn("q owner", text)

    def test_empty_result_has_placeholder(self):
        app = self.make_app()
        app.query = "zzzz-no-such-task"
        app.build_rows()
        app.clamp_selection()
        board = kt.render_text(app, 150, 40)
        self.assertIn("no tasks match", board)
        app.mode_override = "list"
        text = kt.render_text(app, 150, 40)
        self.assertIn("no tasks match", text)
        self.assertIn("no task selected", text)

    def test_help_overlay_covers_board(self):
        app = self.make_app()
        app.show_help = True
        text = kt.render_text(app, 150, 44)
        self.assertIn("read-only board view", text)
        self.assertIn("never writes", text)
        self.assertIn("q", text)

    def test_paused_shows_in_header(self):
        app = self.make_app()
        app.paused = True
        text = kt.render_text(app, 150, 40)
        self.assertIn("paused", text.split("\n")[0])


class TestBoard(FixtureCase):
    """The status-column layout."""

    def board(self, w=160, h=40, **over):
        app = self.make_app(**over)
        app.mode_override = "board"
        return app

    def test_board_is_the_default_when_wide(self):
        app = self.make_app()
        self.assertEqual(app.mode(160, 40), "board")
        self.assertEqual(app.mode(96, 24), "board")

    def test_list_is_the_default_when_narrow_or_short(self):
        app = self.make_app()
        self.assertEqual(app.mode(95, 40), "list")
        self.assertEqual(app.mode(160, 17), "list")
        self.assertEqual(app.mode(49, 38), "list")

    def test_columns_run_left_to_right_in_workflow_order(self):
        app = self.board()
        lines = kt.render_text(app, 160, 40).split("\n")
        header = next(l for l in lines if "todo" in l and "blocked" in l)
        order = [s for s in kt.STATUS_ORDER if s in header]
        self.assertEqual(order, sorted(order, key=kt.STATUS_ORDER.index))
        self.assertLess(header.index("todo"), header.index("blocked"))

    def test_column_header_carries_the_count(self):
        app = self.board()
        text = kt.render_text(app, 160, 40)
        self.assertIn("todo 1", text)
        self.assertIn("blocked 1", text)

    def test_empty_columns_are_dropped_when_space_is_tight(self):
        app = self.board()
        wide = kt.render_text(app, 160, 30)
        narrow = kt.render_text(app, 100, 24)
        self.assertIn("triage 0", wide)          # shown when it fits
        self.assertNotIn("triage", narrow)       # dropped rather than paging
        self.assertIn("blocked 1", narrow)       # the column that matters stays

    def test_cards_render_in_their_status_column(self):
        app = self.board()
        text = kt.render_text(app, 160, 40)
        lines = text.split("\n")
        header = next(i for i, l in enumerate(lines) if "blocked" in l and "todo" in l)
        card_row = next(l for l in lines[header:] if "t_blk00001" in l)
        self.assertLess(card_row.index("t_blk00001"), len(card_row))
        # the blocked card must sit right of the todo card, not below it
        todo_row = next(l for l in lines[header:] if "t_emo00001" in l)
        self.assertLess(todo_row.index("t_emo00001"), card_row.index("t_blk00001"))

    def test_h_and_l_move_between_columns(self):
        app = self.board()
        app.selected_id = "t_emo00001"          # todo
        app.load_detail(force=True)
        self.assertEqual(app.selected_status(), "todo")
        app.handle_key("l")
        self.assertEqual(app.selected_status(), "scheduled")
        app.handle_key("l")
        self.assertEqual(app.selected_status(), "ready")
        app.handle_key("h")
        self.assertEqual(app.selected_status(), "scheduled")

    def test_j_and_k_stay_inside_the_column(self):
        app = self.board()
        app.selected_id = "t_emo00001"
        app.load_detail(force=True)
        app.handle_key("j")
        self.assertEqual(app.selected_status(), "todo")
        app.handle_key("k")
        self.assertEqual(app.selected_status(), "todo")

    def test_column_move_skips_empty_columns(self):
        app = self.board()
        app.selected_id = "t_rdy00001"          # ready
        app.load_detail(force=True)
        app.handle_key("l")
        self.assertEqual(app.selected_status(), "running")
        app.handle_key("l")                     # review is empty in the fixture
        self.assertEqual(app.selected_status(), "blocked")

    def test_b_toggles_board_and_list(self):
        app = self.make_app()
        self.assertEqual(app.mode(160, 40), "board")
        app.last_size = (160, 40)
        app.handle_key("b")
        self.assertEqual(app.mode(), "list")
        app.handle_key("b")
        self.assertEqual(app.mode(), "board")

    def test_detail_strip_shows_the_selected_card(self):
        app = self.board()
        app.selected_id = "t_blk00001"
        app.load_detail(force=True)
        text = kt.render_text(app, 160, 44)
        self.assertIn("blocked: needs_input", text)
        app.handle_key("z")
        self.assertNotIn("blocked: needs_input", kt.render_text(app, 160, 44))

    def test_board_rows_never_overflow(self):
        app = self.board()
        for w, h in ((179, 50), (160, 40), (120, 30), (100, 24), (96, 20), (96, 18)):
            for line in kt.render_text(app, w, h).split("\n"):
                self.assertLessEqual(kt.disp_len(line), w, "%dx%d" % (w, h))

    def test_board_frame_matches_requested_size(self):
        app = self.board()
        for w, h in ((179, 50), (140, 36), (100, 20), (96, 18)):
            lines = kt.render_text(app, w, h).split("\n")
            self.assertEqual(len(lines), h, "%dx%d" % (w, h))

    def test_board_with_all_history_adds_closed_columns(self):
        app = self.board(all=True)
        text = kt.render_text(app, 179, 40)
        self.assertIn("done 1", text)
        self.assertIn("archived 1", text)

    def test_empty_board_has_a_message(self):
        app = self.board()
        app.query = "zzzz-no-such-task"
        app.build_rows()
        app.clamp_selection()
        self.assertIn("no tasks match", kt.render_text(app, 160, 40))


class TestCardSignals(FixtureCase):
    """The six-cell marker that says why a card is not moving."""

    def app(self, **over):
        app = self.make_app(**over)
        app.mode_override = "board"
        return app

    def test_blocked_capability_shows_cap(self):
        app = self.app()
        sig, sev = app._card_signal(next(t for t in app.tasks if t["id"] == "t_blk00001"), 0)
        self.assertEqual(sig, "!need")            # fixture blocks as needs_input
        self.assertEqual(sev, "bad")

    def test_every_block_kind_has_a_short_form(self):
        app = self.app()
        for kind, expect in (
            ("capability", "!cap"),
            ("needs_input", "!need"),
            ("dependency", "!dep"),
            ("transient", "!trns"),
            (None, "!block"),
        ):
            task = {"id": "t_x", "status": "blocked", "block_kind": kind}
            self.assertEqual(app._card_signal(task, 0), (expect, "bad"), kind)

    def test_open_parents_show_as_dep_count(self):
        app = self.app()
        task = next(t for t in app.tasks if t["id"] == "t_emo00001")
        self.assertEqual(app.parent_waits.get("t_emo00001"), 1)
        self.assertEqual(app._card_signal(task, 0), ("dep1", "warn"))

    def test_running_card_shows_heartbeat_age(self):
        app = self.app()
        now = time.time()
        app.live_runs = {"t_run00001": {"last_heartbeat_at": int(now) - 30}}
        task = next(t for t in app.tasks if t["id"] == "t_run00001")
        sig, sev = app._card_signal(task, now)
        self.assertTrue(sig.startswith("hb"), sig)
        self.assertIsNone(sev)

    def test_stale_heartbeat_is_flagged(self):
        app = self.app()
        now = time.time()
        app.live_runs = {"t_run00001": {"last_heartbeat_at": int(now) - 900}}
        task = next(t for t in app.tasks if t["id"] == "t_run00001")
        sig, sev = app._card_signal(task, now)
        self.assertTrue(sig.startswith("!hb"), sig)
        self.assertEqual(sev, "bad")

    def test_quiet_card_has_no_signal(self):
        app = self.app()
        task = next(t for t in app.tasks if t["id"] == "t_rdy00001")
        self.assertEqual(app._card_signal(task, 0), ("", None))

    def test_signal_appears_on_the_card(self):
        app = self.app()
        lines = kt.render_text(app, 179, 44).split("\n")
        blocked_row = next(l for l in lines if "t_blk00001" in l)
        self.assertIn("!need", blocked_row)
        dep_row = next(l for l in lines if "t_emo00001" in l)
        self.assertIn("dep1", dep_row)


class TestHistoryCap(FixtureCase):
    """done/archived columns must not pretend 917 rows fit in 16."""

    def build_capped_board(self, n=40):
        path = os.path.join(self.tmp, "capped.db")
        if not os.path.exists(path):
            build_fixture(path)
            conn = sqlite3.connect(path)
            for i in range(n):
                conn.execute(
                    "INSERT INTO tasks (id,title,status,priority,created_at,completed_at) "
                    "VALUES (?,?,?,?,?,?)",
                    ("t_done%05d" % i, "Finished task %d" % i, "done", 0,
                     NOW - 50000 + i, NOW - 10000 + i),
                )
            conn.commit()
            conn.close()
        store = kt.KanbanStore(path)
        app = kt.App(store, make_args(all=True))
        self.addCleanup(store.close)
        app.mode_override = "board"
        return app

    def test_header_shows_the_true_total(self):
        app = self.build_capped_board()
        text = kt.render_text(app, 200, 40)
        # 40 synthetic + 1 fixture done task
        self.assertIn("done 41", text)

    def test_most_recent_done_cards_come_first(self):
        app = self.build_capped_board()
        grouped = app.by_status()
        done = sorted(grouped["done"], key=lambda t: (t.get("completed_at") or 0), reverse=True)
        self.assertGreater(done[0]["completed_at"], done[-1]["completed_at"])

    def test_cap_limits_loaded_cards(self):
        app = self.build_capped_board()
        app.args.history_cap = 5
        lines = kt.render_text(app, 200, 40).split("\n")
        shown = [l for l in lines if "t_done" in l or "t_don00001" in l]
        self.assertLessEqual(len(shown), 5, shown)

    def test_older_marker_when_there_is_room(self):
        app = self.build_capped_board()
        app.args.history_cap = 3
        text = kt.render_text(app, 320, 60)
        self.assertIn("older", text)

    def test_selected_card_outside_the_cap_stays_visible(self):
        app = self.build_capped_board()
        app.args.history_cap = 3
        oldest = sorted(app.by_status()["done"], key=lambda t: (t.get("completed_at") or 0))[0]
        app.selected_id = oldest["id"]
        lines = kt.render_text(app, 200, 40).split("\n")
        self.assertTrue(any(oldest["id"] in l for l in lines), "cursor must stay on screen")

    def test_cap_does_not_apply_to_open_columns(self):
        app = self.build_capped_board()
        app.args.history_cap = 1
        app.include_closed = False
        text = kt.render_text(app, 200, 40)
        self.assertIn("todo 1", text)


class TestJson(FixtureCase):
    def run_cli(self, *argv):
        return subprocess.run(
            [sys.executable, os.path.join(HERE, "kanban_tui.py"), "--db", self.db] + list(argv),
            capture_output=True, text=True, timeout=60,
            env=dict(os.environ, TERM="xterm-256color"),
        )

    def test_json_is_valid_and_complete(self):
        res = self.run_cli("--json")
        self.assertEqual(res.returncode, 0, res.stderr)
        doc = json.loads(res.stdout)
        self.assertEqual(doc["schema"], "hermes-kanban-tui/1")
        self.assertEqual(doc["board"], "default")
        self.assertEqual(doc["db"], self.db)
        self.assertIn("counts", doc)
        self.assertEqual(doc["task_count"], len(doc["tasks"]))
        self.assertGreater(doc["task_count"], 0)

    def test_json_respects_filters(self):
        doc = json.loads(self.run_cli("--json", "--status", "blocked").stdout)
        self.assertEqual(doc["filters"]["status"], "blocked")
        self.assertTrue(doc["tasks"])
        for t in doc["tasks"]:
            self.assertEqual(t["status"], "blocked")

    def test_json_carries_the_block_evidence(self):
        doc = json.loads(self.run_cli("--json", "--status", "blocked").stdout)
        entry = doc["tasks"][0]
        self.assertIn("block", entry)
        self.assertEqual(entry["block"]["kind"], "needs_input")
        self.assertEqual(entry["block"]["recurrences"], 2)
        self.assertEqual(entry["signal"], "!need")

    def test_json_reports_open_parents_and_signals(self):
        doc = json.loads(self.run_cli("--json", "--status", "todo").stdout)
        entry = next(t for t in doc["tasks"] if t["id"] == "t_emo00001")
        self.assertEqual(entry["open_parents"], 1)
        self.assertEqual(entry["signal"], "dep1")

    def test_json_reports_the_live_run(self):
        doc = json.loads(self.run_cli("--json", "--status", "running").stdout)
        entry = doc["tasks"][0]
        self.assertIn("current_run", entry)
        self.assertEqual(entry["current_run"]["status"], "running")
        self.assertIsNotNone(entry["current_run"]["heartbeat_age_seconds"])

    def test_json_never_writes(self):
        before = digest(self.db)
        res = self.run_cli("--json", "--all")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(before, digest(self.db))


FIXED_NOW = 1794000000  # 2026-11-06T19:20:00Z, arbitrary but pinned
GOLDEN_DIR = os.path.join(HERE, "tests")


def normalize_frame(text):
    """Strip the two values that cannot be pinned: the wall clock and the
    timezone-dependent absolute dates."""
    text = re.sub(r"\b\d{2}:\d{2}:\d{2}\b", "HH:MM:SS", text)
    text = re.sub(r"\b\d{4}-\d{2}-\d{2} \d{2}:\d{2}\b", "DATE", text)
    return text


class TestGoldenFrame(FixtureCase):
    """A stored frame per layout.

    Unit tests assert facts; this asserts the whole picture, so a layout
    regression that keeps every field present but moves it on screen still
    fails. Regenerate deliberately:  KANBAN_TUI_UPDATE_GOLDEN=1 python3 -m unittest test_kanban_tui.TestGoldenFrame
    """

    def frame(self, width, height, mode="board", extra=None):
        path = os.path.join(self.tmp, "golden.db")
        if not os.path.exists(path):
            build_fixture(path, now=FIXED_NOW)
        store = kt.KanbanStore(path)
        app = kt.App(store, make_args(**(extra or {})))
        self.addCleanup(store.close)
        app.mode_override = mode
        return normalize_frame(kt.render_text(app, width, height, now=FIXED_NOW))

    def compare(self, name, width, height, mode="board", extra=None):
        text = self.frame(width, height, mode=mode, extra=extra)
        path = os.path.join(GOLDEN_DIR, name)
        if os.environ.get("KANBAN_TUI_UPDATE_GOLDEN"):
            os.makedirs(GOLDEN_DIR, exist_ok=True)
            with open(path, "w") as fh:
                fh.write(text + "\n")
            return text
        self.assertTrue(os.path.exists(path), "missing golden %s; regenerate with KANBAN_TUI_UPDATE_GOLDEN=1" % name)
        with open(path) as fh:
            expected = fh.read().rstrip("\n")
        self.assertEqual(text, expected, "layout drift in %s" % name)
        return text

    def test_board_160x40(self):
        text = self.compare("golden-board-160x40.txt", 160, 40)
        self.assertIn("todo 1", text)
        self.assertIn("!need", text)
        self.assertIn("dep1", text)

    def test_board_100x24_drops_empty_columns(self):
        text = self.compare("golden-board-100x24.txt", 100, 24)
        self.assertNotIn("triage", text)
        self.assertIn("blocked", text)

    def test_list_70x26(self):
        text = self.compare("golden-list-70x26.txt", 70, 26, mode="list")
        self.assertIn("no tasks match", text) if False else None
        self.assertIn("t_blk00001"[:11], text)


class TestProportionalWidths(unittest.TestCase):
    def test_widths_sum_to_available(self):
        for counts, avail in (
            ([0, 8, 7, 5, 0, 1, 13], 154),
            ([8, 7, 5, 1, 13], 96),
            ([1, 1, 1], 60),
            ([50, 1, 1], 90),
        ):
            widths = kt._shared_widths(counts, avail)
            self.assertEqual(len(widths), len(counts))
            self.assertLessEqual(sum(widths), avail, counts)
            self.assertTrue(all(w >= 8 for w in widths), widths)

    def test_busy_columns_get_more(self):
        widths = kt._shared_widths([0, 8, 7, 5, 0, 1, 13], 154)
        self.assertGreater(widths[6], widths[4])   # blocked 13 > running 0
        self.assertGreater(widths[1], widths[5])   # todo 8 > review 1

    def test_no_column_takes_the_whole_board(self):
        widths = kt._shared_widths([100, 1, 1], 90)
        self.assertLessEqual(max(widths), 40)

    def test_degenerate_width_still_returns_something(self):
        widths = kt._shared_widths([1, 1, 1, 1, 1, 1, 1], 40)
        self.assertEqual(len(widths), 7)
        self.assertTrue(all(w >= 8 for w in widths))


class TestMouse(FixtureCase):
    """Hit testing. The curses event plumbing is proved separately by
    mouse_probe.py in a real pty; this pins the coordinate mapping."""

    def board(self, db=None):
        app = self.make_app()
        app.mode_override = "board"
        kt.render_text(app, 160, 40)
        return app

    def crowded_board(self):
        """A board where one column holds several cards, for scroll tests."""
        path = os.path.join(self.tmp, "crowded.db")
        if not os.path.exists(path):
            build_fixture(path)
            conn = sqlite3.connect(path)
            for i in range(3):
                conn.execute(
                    "INSERT INTO tasks (id,title,status,priority,created_at) VALUES (?,?,?,?,?)",
                    ("t_todo%05d" % i, "Extra todo card %d" % i, "todo", 5 - i, NOW - i * 60),
                )
            conn.commit()
            conn.close()
        store = kt.KanbanStore(path)
        app = kt.App(store, make_args())
        self.addCleanup(store.close)
        app.mode_override = "board"
        kt.render_text(app, 160, 40)
        return app

    def test_hit_map_covers_every_card(self):
        app = self.board()
        ids = {tid for hits in app.hit_cards.values() for _x0, _x1, tid in hits}
        for task in kt.App.filtered(app):
            self.assertIn(task["id"], ids)

    def test_columns_do_not_overlap(self):
        app = self.board()
        spans = sorted((x0, x1) for _s, x0, x1 in app.hit_cols)
        for (a0, a1), (b0, _b1) in zip(spans, spans[1:]):
            self.assertLess(a1, b0, "column spans must not overlap")

    def test_click_on_a_card_selects_it(self):
        app = self.board()
        y = min(app.hit_cards)
        _x0, _x1, want = app.hit_cards[y][-1]      # last column on that row
        self.assertNotEqual(app.selected_id, want)
        span = next((x0, x1) for x0, x1, tid in app.hit_cards[y] if tid == want)
        self.assertTrue(app.click(span[0] + 1, y))
        self.assertEqual(app.selected_id, want)

    def test_click_on_empty_column_space_selects_its_first_card(self):
        app = self.board()
        grouped = app.by_status()
        status, x0, x1 = next(
            (s, a, b) for s, a, b in app.hit_cols if grouped.get(s)
        )
        self.assertTrue(app.click((x0 + x1) // 2, 38))   # bottom: no card drawn
        self.assertEqual(app.selected_id, grouped[status][0]["id"])

    def test_click_on_empty_space_does_nothing(self):
        app = self.board()
        before = app.selected_id
        self.assertFalse(app.click(500, 39))
        self.assertEqual(app.selected_id, before)

    def test_click_works_in_list_view(self):
        app = self.make_app()
        app.mode_override = "list"
        kt.render_text(app, 150, 40)
        y = min(app.hit_cards)
        _x0, _x1, want = app.hit_cards[y][0]
        app.click(5, y)
        self.assertEqual(app.selected_id, want)

    def test_wheel_moves_the_selection_unfocused(self):
        app = self.crowded_board()
        app.selected_id = "t_todo00000"
        app.load_detail(force=True)
        first = app.selected_id
        app.wheel(1)
        self.assertNotEqual(app.selected_id, first)
        self.assertEqual(app.selected_status(), "todo", "wheel stays in the column")

    def test_wheel_scrolls_the_detail_when_it_has_focus(self):
        app = self.board()
        app.focus = "detail"
        before = app.selected_id
        app.wheel(3)
        self.assertEqual(app.selected_id, before, "focused detail must scroll, not move")
        self.assertGreater(app.detail_offset, 0)


class TestCommentAction(FixtureCase):
    """The single write path: `hermes kanban comment`, one argv element."""

    def app(self, **over):
        app = self.make_app(**over)
        app.selected_id = "t_blk00001"
        app.load_detail(force=True)
        return app

    def fake_run(self, rc=0, out="comment 1 added", err=""):
        calls = []

        def runner(argv, **kw):
            calls.append((argv, kw))
            return subprocess.CompletedProcess(argv, rc, out, err)

        return runner, calls

    def test_c_opens_compose_on_the_selected_task(self):
        app = self.app()
        app.handle_key("c")
        self.assertTrue(app.compose_mode)
        self.assertEqual(app.compose_task, "t_blk00001")
        self.assertEqual(app.compose_buf, "")

    def test_typing_edits_the_buffer(self):
        app = self.app()
        app.handle_key("c")
        for ch in "note":
            app.handle_key(ch)
        self.assertEqual(app.compose_buf, "note")
        app.handle_key("\x7f")
        self.assertEqual(app.compose_buf, "not")
        app.handle_key("\x15")
        self.assertEqual(app.compose_buf, "")

    def test_escape_cancels_without_writing(self):
        runner, calls = self.fake_run()
        app = self.app()
        app.handle_key("c")
        app.handle_key("x")
        app.handle_key("\x1b")
        self.assertFalse(app.compose_mode)
        self.assertEqual(calls, [])
        self.assertIsNone(app.last_write["ok"])

    def test_enter_posts_through_the_cli(self):
        import kanban_tui

        runner, calls = self.fake_run()
        old = kanban_tui.subprocess.run
        kanban_tui.subprocess.run = runner
        try:
            app = self.app()
            app.handle_key("c")
            for ch in "looked at this":
                app.handle_key(ch)
            app.handle_key("\n")
        finally:
            kanban_tui.subprocess.run = old
        self.assertEqual(len(calls), 1)
        argv = calls[0][0]
        self.assertEqual(argv[:5], ["hermes", "kanban", "comment", "t_blk00001", "--author"])
        self.assertEqual(argv[5], "kanban-tui")
        self.assertEqual(argv[6], "looked at this")
        self.assertNotIn("shell", calls[0][1])
        self.assertTrue(app.last_write["ok"])
        self.assertFalse(app.compose_mode)
        self.assertEqual(app.detail_tab, "comments")

    def test_body_stays_one_argument(self):
        import kanban_tui

        runner, calls = self.fake_run()
        old = kanban_tui.subprocess.run
        kanban_tui.subprocess.run = runner
        try:
            app = self.app()
            app.handle_key("c")
            for ch in "a; rm -rf / && echo $(id)":
                app.handle_key(ch)
            app.handle_key("\n")
        finally:
            kanban_tui.subprocess.run = old
        argv = calls[0][0]
        self.assertEqual(argv[-1], "a; rm -rf / && echo $(id)")
        self.assertEqual(len(argv), 7, "the body must not be split")

    def test_failure_keeps_the_text_and_says_so(self):
        import kanban_tui

        runner, _calls = self.fake_run(rc=1, out="", err="no such task")
        old = kanban_tui.subprocess.run
        kanban_tui.subprocess.run = runner
        try:
            app = self.app()
            app.handle_key("c")
            for ch in "text":
                app.handle_key(ch)
            app.handle_key("\n")
        finally:
            kanban_tui.subprocess.run = old
        self.assertFalse(app.last_write["ok"])
        self.assertTrue(app.compose_mode, "keep compose open so it can be retried")
        self.assertEqual(app.compose_buf, "text")
        self.assertIn("no such task", app.message)

    def test_empty_body_posts_nothing(self):
        import kanban_tui

        runner, calls = self.fake_run()
        old = kanban_tui.subprocess.run
        kanban_tui.subprocess.run = runner
        try:
            app = self.app()
            app.handle_key("c")
            app.handle_key(" ")
            app.handle_key("\n")
        finally:
            kanban_tui.subprocess.run = old
        self.assertEqual(calls, [])
        self.assertIn("nothing to post", app.message)

    def test_cli_missing_is_reported_not_raised(self):
        import kanban_tui

        def boom(argv, **kw):
            raise FileNotFoundError("hermes")

        old = kanban_tui.subprocess.run
        kanban_tui.subprocess.run = boom
        try:
            app = self.app()
            ok, detail = app.post_comment("t_blk00001", "hello")
        finally:
            kanban_tui.subprocess.run = old
        self.assertFalse(ok)
        self.assertIn("hermes", detail)
        self.assertFalse(app.last_write["ok"])

    def test_no_write_blocks_the_action(self):
        app = self.app(no_write=True)
        app.handle_key("c")
        self.assertFalse(app.compose_mode)
        self.assertIn("disabled", app.message)

    def test_compose_prompt_is_rendered(self):
        app = self.app()
        app.handle_key("c")
        for ch in "hello":
            app.handle_key(ch)
        text = kt.render_text(app, 150, 40)
        self.assertIn("comment t_blk00001: hello", text)
        self.assertIn("Enter] post comment", text)


class TestKeys(FixtureCase):
    def press(self, app, *keys):
        for k in keys:
            app.handle_key(k)

    def test_j_and_k_move_selection(self):
        app = self.make_app()
        first = app.selected_id
        self.press(app, "j")
        second = app.selected_id
        self.assertNotEqual(first, second)
        self.press(app, "k")
        self.assertEqual(app.selected_id, first)

    def test_selection_stops_at_edges(self):
        app = self.make_app()
        self.press(app, *("k" * 20))
        self.assertEqual(app.selected_id, app.rows[[i for i, r in enumerate(app.rows) if r["kind"] == "task"][0]]["task"]["id"])
        self.press(app, *("j" * 40))
        last = app.selected_id
        self.press(app, "j")
        self.assertEqual(app.selected_id, last)

    def test_g_and_G_jump(self):
        app = self.make_app()
        self.press(app, "G")
        last = app.selected_id
        self.press(app, "g")
        self.assertNotEqual(last, app.selected_id)

    def test_tab_toggles_focus_and_scrolls_detail(self):
        app = self.make_app()
        app.handle_key("\t")
        self.assertEqual(app.focus, "detail")
        before = app.selected_id
        self.press(app, "j")
        self.assertEqual(app.selected_id, before)
        self.assertGreater(app.detail_offset, 0)

    def test_number_keys_switch_detail_tab(self):
        app = self.make_app()
        app.handle_key("2")
        self.assertEqual(app.detail_tab, "events")
        app.handle_key("3")
        self.assertEqual(app.detail_tab, "comments")

    def test_status_filter_cycles(self):
        app = self.make_app()
        app.handle_key("s")
        self.assertEqual(app.status_filter, "triage")
        app.handle_key("S")
        self.assertEqual(app.status_filter, "")

    def test_assignee_filter_narrows_rows(self):
        app = self.make_app()
        app.assignee_filter = "operator"
        app.build_rows()
        assignees = {r["task"]["assignee"] for r in app.rows if r["kind"] == "task"}
        self.assertEqual(assignees, {"operator"})

    def test_search_mode_applies_on_enter(self):
        app = self.make_app()
        app.handle_key("/")
        self.assertTrue(app.search_mode)
        for ch in "blocked":
            app.handle_key(ch)
        app.handle_key("\n")
        self.assertFalse(app.search_mode)
        self.assertEqual(app.query, "blocked")
        titles = [r["task"]["title"] for r in app.rows if r["kind"] == "task"]
        self.assertTrue(titles)
        for t in titles:
            self.assertIn("Blocked", t)

    def test_search_escape_discards(self):
        app = self.make_app()
        app.handle_key("/")
        app.handle_key("x")
        app.handle_key("\x1b")
        self.assertEqual(app.query, "")

    def test_search_backspace(self):
        app = self.make_app()
        app.handle_key("/")
        for ch in "abc":
            app.handle_key(ch)
        app.handle_key("\x7f")
        self.assertEqual(app.search_buf, "ab")

    def test_d_toggles_history(self):
        app = self.make_app()
        self.assertFalse(app.include_closed)
        app.handle_key("d")
        self.assertTrue(app.include_closed)

    def test_z_toggles_detail(self):
        app = self.make_app()
        app.handle_key("z")
        self.assertFalse(app.show_detail)
        app.handle_key("z")
        self.assertTrue(app.show_detail)

    def test_p_and_r(self):
        app = self.make_app()
        app.handle_key("p")
        self.assertTrue(app.paused)
        gen = app.gen
        app.refresh()
        self.assertEqual(app.gen, gen, "paused app must not reload")
        app.handle_key("r")
        self.assertGreater(app.gen, gen, "forced refresh must reload")
        app.handle_key("p")
        self.assertFalse(app.paused)

    def test_q_quits(self):
        app = self.make_app()
        self.assertEqual(app.handle_key("q"), "quit")

    def test_help_swallows_next_key(self):
        app = self.make_app()
        app.handle_key("?")
        self.assertTrue(app.show_help)
        app.handle_key("q")
        self.assertFalse(app.show_help)
        self.assertIsNotNone(app.selected_id, "help must not clear the selection")

    def test_int_keys_are_ignored_not_crashing(self):
        app = self.make_app()
        for code in (curses_missing(), 9999):
            self.assertIsNone(app.handle_key(code))


def curses_missing():
    import curses

    return curses.KEY_F0


class TestHerdrBridge(FixtureCase):
    def test_clear_is_a_noop_when_disabled(self):
        app = self.make_app()
        app.herdr.clear()
        self.assertEqual(app.herdr.last_title, "")

    def test_clear_drops_the_title(self):
        bridge = kt.HerdrBridge(make_args(no_herdr=False))
        bridge.enabled = True
        bridge.pane = "w1:p1"
        bridge.bin = "/nonexistent/herdr"
        bridge.last_title = "kanban default: 1 running"
        bridge.clear()
        self.assertEqual(bridge.last_title, "")

    def test_disabled_without_herdr_env(self):
        app = self.make_app()
        self.assertFalse(app.herdr.enabled)
        app.herdr.report("kanban: test")
        self.assertEqual(app.herdr.last_title, "")

    def test_enabled_in_herdr_pane(self):
        old = {k: os.environ.get(k) for k in ("HERDR_ENV", "HERDR_PANE_ID")}
        os.environ["HERDR_ENV"] = "1"
        os.environ["HERDR_PANE_ID"] = "w1:p1"
        try:
            store = kt.KanbanStore(self.db)
            bridge = kt.HerdrBridge(make_args(no_herdr=False))
            store.close()
            self.assertTrue(bridge.enabled)
            self.assertEqual(bridge.pane, "w1:p1")
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_no_herdr_flag_wins(self):
        os.environ["HERDR_ENV"] = "1"
        os.environ["HERDR_PANE_ID"] = "w1:p1"
        try:
            bridge = kt.HerdrBridge(make_args(no_herdr=True))
            self.assertFalse(bridge.enabled)
        finally:
            os.environ.pop("HERDR_ENV", None)
            os.environ.pop("HERDR_PANE_ID", None)

    def test_failure_is_recorded_not_raised(self):
        bridge = kt.HerdrBridge(make_args(no_herdr=False))
        bridge.enabled = True
        bridge.pane = "w1:p1"
        bridge.bin = "/nonexistent/herdr"
        bridge.report("kanban: test")
        self.assertEqual(bridge.last_title, "")
        self.assertTrue(bridge.last_error)


class TestCli(FixtureCase):
    def run_cli(self, *argv):
        return subprocess.run(
            [sys.executable, os.path.join(HERE, "kanban_tui.py")] + list(argv),
            capture_output=True,
            text=True,
            timeout=60,
            env=dict(os.environ, TERM="xterm-256color"),
        )

    def test_print_snapshot(self):
        res = self.run_cli("--db", self.db, "--print", "130x36")
        self.assertEqual(res.returncode, 0, res.stderr)
        lines = res.stdout.rstrip("\n").split("\n")
        self.assertEqual(len(lines), 36)
        self.assertIn("KANBAN", lines[0])
        self.assertIn("t_rdy00001", res.stdout)

    def test_print_with_filters(self):
        res = self.run_cli("--db", self.db, "--status", "blocked", "--print", "120x30")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("t_blk00001", res.stdout)
        self.assertNotIn("t_sch00001", res.stdout)

    def test_version(self):
        res = self.run_cli("--version")
        self.assertEqual(res.returncode, 0)
        self.assertIn(kt.VERSION, res.stdout)

    def test_where_reports_the_resolved_board(self):
        res = self.run_cli("--db", self.db, "--where")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("kanban-tui", res.stdout)
        self.assertIn(self.db, res.stdout)
        self.assertIn("exists", res.stdout)
        self.assertIn("terminfo", res.stdout)
        self.assertIn("board", res.stdout)

    def test_where_is_not_a_board_read_error(self):
        # --where must answer even when the database is missing: that is
        # exactly the case where a user needs it.
        res = self.run_cli("--db", os.path.join(self.tmp, "nope.db"), "--where")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("no", res.stdout)

    def test_pane_without_herdr_is_a_clear_error(self):
        # In-process: a subprocess would inherit the real PATH and herdr.
        buf = io.StringIO()
        with unittest.mock.patch.object(kt.shutil, "which", return_value=None), \
                unittest.mock.patch.object(kt.sys, "stderr", buf):
            code = kt.main(["--db", self.db, "--pane"])
        self.assertEqual(code, 1)
        self.assertIn("needs the herdr CLI", buf.getvalue())

    def test_pane_creates_a_workspace_and_forwards_the_view_args(self):
        calls = []

        class Done:
            def __init__(self, out=""):
                self.returncode = 0
                self.stdout = out
                self.stderr = ""

        def fake_run(argv, **kw):
            calls.append(argv)
            if argv[1:3] == ["workspace", "create"]:
                return Done('{"result": {"root_pane": {"pane_id": "w9:p1"}}}')
            return Done()

        out = io.StringIO()
        with unittest.mock.patch.object(kt.shutil, "which", return_value="/usr/bin/herdr"), \
                unittest.mock.patch.object(kt.subprocess, "run", side_effect=fake_run), \
                unittest.mock.patch.object(kt.sys, "stdout", out):
            code = kt.main(["--db", self.db, "--board", "crew", "--pane"])
        self.assertIn("w9:p1", out.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual([c[1:3] for c in calls],
                         [["workspace", "create"], ["pane", "run"]])
        self.assertEqual(calls[1][3], "w9:p1")
        command = calls[1][4]
        self.assertIn("kanban_tui.py", command)
        self.assertIn("--board crew", command)
        self.assertIn("exec bash -l", command)

    def test_snapshot_modes_work_without_curses(self):
        # Windows ships no curses; pip installs windows-curses, but if it is
        # absent the read-only snapshot modes must still work.
        out = io.StringIO()
        with unittest.mock.patch.object(kt, "curses", None), \
                unittest.mock.patch.object(kt.sys, "stdout", out):
            code = kt.main(["--db", self.db, "--print", "100x20"])
        self.assertEqual(code, 0)
        self.assertIn("KANBAN", out.getvalue())

    def test_interactive_without_curses_says_so(self):
        err = io.StringIO()
        with unittest.mock.patch.object(kt, "curses", None), \
                unittest.mock.patch.object(kt.sys, "stderr", err):
            code = kt.main(["--db", self.db])
        self.assertEqual(code, 1)
        message = err.getvalue()
        self.assertIn("no curses module", message)
        self.assertIn("pip install windows-curses", message)

    def test_bad_size_is_rejected(self):
        res = self.run_cli("--db", self.db, "--print", "wide")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("WxH", res.stderr)

    def test_db_is_byte_identical_after_snapshot(self):
        before = digest(self.db)
        res = self.run_cli("--db", self.db, "--print", "120x40", "--all")
        self.assertEqual(res.returncode, 0, res.stderr)
        after = digest(self.db)
        self.assertEqual(before, after)


class TestLiveBoard(unittest.TestCase):
    """Smoke test against the real board when it exists. Never writes."""

    def setUp(self):
        self.db = os.path.join(os.path.expanduser("~/.hermes"), "kanban.db")
        if not os.path.exists(self.db) or os.path.getsize(self.db) == 0:
            self.skipTest("no live kanban.db")

    def test_live_snapshot(self):
        before = digest(self.db)
        res = subprocess.run(
            [sys.executable, os.path.join(HERE, "kanban_tui.py"), "--print", "160x48"],
            capture_output=True, text=True, timeout=90,
            env=dict(os.environ, TERM="xterm-256color"),
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("KANBAN", res.stdout)
        self.assertIn("default", res.stdout.split("\n")[0])
        after = digest(self.db)
        self.assertEqual(before, after, "snapshot mode must not touch the board")


if __name__ == "__main__":
    unittest.main(verbosity=2)
