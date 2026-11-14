# Verification record

Environment: Linux 7.0.0-43-generic, Python 3.14.9, sqlite3 3.53.1,
ncurses 2.2, herdr 0.9.5 (server protocol 22), Ghostty via herdr
(`TERM=xterm-256color`, `COLORTERM=truecolor`, `TERM_PROGRAM=herdr` inside a
pane -- read with `stty size`, 2026-11-12).

Every claim below is a command that was run and its actual output. Nothing here
is inferred from reading the code.

## 1. Unit and fixture tests

    $ ./verify.sh
    --- unit tests: PASS
    --- snapshot at live size: PASS
    --- snapshot with history: PASS
    --- pty: ghostty TERM, real size: PASS
    --- pty: terminfo fallback: PASS
    --- live resize: PASS
    --- live refresh: PASS
    --- terminfo state: PASS
    ALL CHECKS PASSED

    $ python3 -m unittest test_kanban_tui -v
    Ran 122 tests in 5.566s
    OK

Coverage includes wide-character geometry, control-character stripping, the
read-only connection and its hot-WAL fallback, board-path resolution (explicit
db, env pin, current-board file, named board), workflow ordering, every key
binding, filter cycling, search, the herdr bridge (enabled / disabled / error /
clear), and the CLI.
`TestLiveBoard.test_live_snapshot` also runs against the real board:

    OK  hashes ~/.hermes/kanban.db before and after -> identical

## 2. The board is never written

Byte-level, not "no writes in the code":

    $ python3 -m unittest test_kanban_tui.TestCli.test_db_is_byte_identical_after_snapshot
    OK
    $ python3 -m unittest test_kanban_tui.TestStore.test_connection_is_read_only
    OK          # DELETE on the handle raises sqlite3.OperationalError

## 3. Ghostty terminfo: the failure and both defenses

Ghostty defaults to `TERM=xterm-ghostty`. Before the fix:

    $ infocmp xterm-ghostty
    infocmp: couldn't open terminfo file .../x/xterm-ghostty.
    $ python3 -c "import curses; curses.setupterm(term='xterm-ghostty')"
    _curses.error: setupterm: could not find terminal

The stock package ships `ghostty` but not `xterm-ghostty`:

    $ toe -a | grep -i ghostty
    ghostty     Ghostty terminal emulator

Fallback path, proved by pointing TERMINFO at an empty directory:

    $ TERMINFO=/tmp/empty-ti python3 -c "
        import sys; sys.path.insert(0,'.'); import kanban_tui as kt, curses
        try: curses.setupterm(term='xterm-ghostty', fd=sys.stdout.fileno())
        except curses.error as e: print('direct setupterm FAILS:', e)
        print('ensure_terminal chose:', kt.ensure_terminal()[0])"
    direct setupterm FAILS: setupterm: could not find terminal
    ensure_terminal chose: xterm-256color

Same condition through the real TUI in a pty:

    $ TERMINFO=/tmp/empty-ti python3 pty_drive.py --term xterm-ghostty --cols 100 --rows 28 --keys q
    term=xterm-ghostty size=100x28 bytes=3305 exit=0
    saw_alt_screen=True saw_colour=True
    ... board rendered ...

Install path:

    $ ./install-terminfo.sh
    xterm-ghostty    missing
    ghostty          present (source)
    xterm-ghostty    installed in ~/.terminfo
    $ infocmp xterm-ghostty >/dev/null && echo resolvable
    resolvable

## 4. Interactive run in a real pty, at the real size

`pty_drive.py` allocates a pty, sets TIOCSWINSZ, runs the actual TUI, sends
keystrokes, and keeps every byte. `ansi_screen.py` replays those bytes into a
grid, which is what a terminal would have shown.

    $ python3 pty_drive.py --term xterm-ghostty --cols 164 --rows 50 --keys jj2q --raw-out /tmp/kb3.raw
    term=xterm-ghostty size=164x50 bytes=7632 exit=0
    saw_alt_screen=True saw_colour=True

Reconstructed screen (top 12 rows of 50):

    KANBAN  default tri 0 tod 8 sch 7 rdy 5 run 2 rev 0 blk 13        read-only | 2s | 00:11:50
     show open  status all  assignee all   35 listed
    ------------------------------------------------------------------------------------
     todo (8) --------------------------------------------------|t_122da3f2  [todo]   prio 110
    > t_122da3f2 tod 110 default       Captain goal: advance|Captain goal: advance canonical Omni qualification ...
      t_746cf1b4 tod   0 reviewer      Omni native Hermes pi|qualification to verified release decision
    ...

Selection moved exactly three rows for `jjj` (t_122da3f2 -> t_7545d222 in a
second run), confirming keystrokes reach the app and the cursor follows.

## 5. Inside a real herdr pane

    $ herdr pane run wQ:p1 'cd . && exec ./kanban-tui'
    $ herdr pane read wQ:p1 --source visible --lines 50
    KANBAN  default tri 0 tod 8 sch 7 rdy 5 run 2 rev 0 blk 13 ...
    > t_122da3f2 tod 110 default  Captain goal: advance canonical Omni qualification ...
    ... (48 rows, both panes drawn, footer visible)

Pane metadata round-trip:

    $ herdr pane get wQ:p1
    title: kanban default: 2 running, 5 ready, 13 blocked

    $ herdr pane send-keys wQ:p1 Down Down Down
    $ herdr pane read wQ:p1 --source visible | grep '^>'
    12:> t_3e1b3e67 tod   0 operator      Deploy the reviewed HQ build through fleet-deploy gua|

    $ herdr pane send-keys wQ:p1 q
    $ herdr pane get wQ:p1
    title: None          # cleared on exit, no stale counts in the tab strip

Panes survive quit via the launcher (`--pane` runs the view then `exec bash -l`):

    $ ./kanban-tui --pane
    kanban-tui: opened in herdr workspace pane wR:p1
    $ herdr pane send-keys wR:p1 q
    $ herdr pane read wR:p1 --lines 5
    $      # pane alive, back at a shell

## 5b. Board layout (status columns)

A "kanban" with vertical status sections is a list, not a board. The default
view is now columns, with the detail as a strip underneath so the columns keep
the full height.

    $ ./kanban-tui --print 160x30
     KANBAN  default tri 0 tod 8 sch 7 rdy 5 run 0 rev 1 blk 13 ...
     triage 0             | todo 8               | scheduled 7          | ready 5 ...
     ---------------------|----------------------|----------------------|--------
      (empty)             |>t_122da3f2    18d14h | t_6dfd43a9    12h16m | t_d257db9f
                          |  Captain goal:       |  Scheduler release   |  Implement
                          |  advance canonical   |  prep: rebase        |  durable

Navigation, driven through a real pty at 164x50 (`--keys ll`, then `jj`):

    $ python3 pty_drive.py --term xterm-ghostty --cols 164 --rows 50 --keys ll --raw-out /tmp/board.raw
    term=xterm-ghostty size=164x50 bytes=10698 exit=0
    $ python3 ansi_screen.py --cols 164 --rows 50 --raw-out /tmp/board.raw
     triage 0   | todo 8   | scheduled 7 | ready 5 | running 0 | review 1 | blocked 13
     ...
      (empty)   | t_122da3f2 | t_6dfd43a9 | >t_d257db9f | (empty) | t_3c7ce1ac | t_6e06bc35

`l` moved the cursor from todo to scheduled to ready and the strip followed.
In the live pane (179x50):

    $ herdr pane send-keys wT:p1 l        -> |>t_6dfd43a9   (scheduled)
    $ herdr pane send-keys wT:p1 j j      -> |>t_7d8bd97b   (still scheduled)

Narrowing drops empty columns before it pages:

    $ ./kanban-tui --print 100x24
     todo 8            | scheduled 7       | ready 5           | review 1          | blocked 13

At 160 the same board also shows `triage 0` and `running 0`; at 100 those two
empty columns are dropped so all five occupied ones stay visible. If even that
does not fit, the board pages a window around the cursor and marks the hidden
side with `<` / `>` in the column header.

## 5c. Card signals, history cap, JSON

The board answers "why is this not moving" without opening a card, and the
same data is available to scripts.

    $ ./kanban-tui --print 148x40
     triage 0           | todo 8             | scheduled 7        | ready 5 ...
     -------------------|--------------------|--------------------|--------------
      (empty)           |>t_122da3f2 dep1    | t_6dfd43a9         | t_d257db9f
                        |  Captain goal:     |  Scheduler release |  Implement
     ...                                              t_6e06bc35 !cap

Signals on the live board: 13 blocked split 6 `!cap`, 3 `!need`, 4 `!block`
(none of those four carry a typed reason), and 10 open cards carry `depN`
because a parent is still open -- `t_7545d222` shows `dep5`. Verified against
SQL:

    $ select block_kind,count(*) from tasks where status='blocked' group by 1
    capability 6 | needs_input 3 | NULL 4
    $ select count(distinct t.id) from tasks t join task_links l on l.child_id=t.id
      join tasks p on p.id=l.parent_id where t.status not in ('done','archived')
      and p.status not in ('done','archived')
    10

History cap:

    $ ./kanban-tui --all --print 179x34 | head -5
     triage 0 | ... | done 860 | archived 57
     (the header keeps the true total; the column loads 25 newest by completion)

Tests pin both halves: `TestHistoryCap.test_header_shows_the_true_total` (40
synthetic done rows + 1 fixture -> "done 41"), `test_cap_limits_loaded_cards`,
`test_older_marker_when_there_is_room`, and
`test_selected_card_outside_the_cap_stays_visible`.

JSON:

    $ ./kanban-tui --json --status blocked | head -20
    {
      "schema": "hermes-kanban-tui/1",
      "generated_at": 1794598301,
      "board": "default",
      "db": "~/.hermes/kanban.db",
      "filters": { "status": "blocked", "assignee": null, "query": null,
                   "include_closed": false },
      "counts": { "archived": 57, "blocked": 13, "done": 860, "ready": 5,
                  "review": 1, "scheduled": 7, "todo": 8 },
      "task_count": 13,
      "tasks": [ { "id": "t_6e06bc35", "signal": "!cap", "severity": "bad",
                   "open_parents": 0,
                   "block": { "kind": "capability", "recurrences": 1, ... } } ]
    }

`TestJson` covers schema, filters, block evidence, open-parent counts, the live
run object, and that a `--json --all` run leaves the db byte-identical.

Cost after adding the two board queries (`open_parent_waits`, `running_runs`):

    refresh():  12.6 / 11.9 / 10.7 ms   (was ~3.5 ms)
    render 179x50:  4.9 ms
    build_json:     0.2 ms (15 KB for 34 open tasks)

Still under 1% duty cycle at a 2s interval.

## 5d. Column widths follow load, and whole frames are pinned

Equal columns gave the empty `running` column the same 24 cells as `blocked`
with 13 cards. Widths are now shared by load with a 16-cell floor and a 40-cell
ceiling:

    $ python3 -c "import kanban_tui as kt; print(kt._shared_widths([0,8,7,5,0,1,13], 154))"
    [17, 26, 24, 22, 17, 17, 31]     # sums to 154 = 160 - 6 separators

    $ ./kanban-tui --print 160x30 | sed -n '4,5p'
     triage 0        | todo 8                   | scheduled 7             | ready 5 ...
     -----------------|--------------------------|-------------------------|---------

Tests: `TestProportionalWidths` covers the sum-never-exceeds-available
invariant, busy columns getting more, the 40-cell ceiling, and the degenerate
case where the floor does not fit.

Whole frames are stored under `tests/` and compared verbatim (clock and absolute
dates normalised, `App.render(now=...)` pinned):

    $ KANBAN_TUI_UPDATE_GOLDEN=1 python3 -m unittest test_kanban_tui.TestGoldenFrame
    Ran 3 tests - OK
    $ ls tests/
    golden-board-100x24.txt  golden-board-160x40.txt  golden-list-70x26.txt

The check was proved able to fail: changing one separator in the stored board
frame produced

    FAIL: test_board_160x40  layout drift in golden-board-160x40.txt

and restoring the file returned it to OK, so the comparison is live rather than
a file nobody reads.

## 5e. Mouse (opt-in)

Off by default; `--mouse` enables `curses.mousemask(ALL_MOUSE_EVENTS)`.
`mouse_probe.py` proves it end to end in a real pty: it renders the board,
finds the blocked column and a card row, writes the mouse report a terminal
would send, and requires the cursor to move to that card.

    $ python3 mouse_probe.py
    cursor before click: t_122da3f2
    clicked at (135, 5) in the blocked column
    cursor after SGR  click: t_6e06bc35
    cursor after X10  click: t_0485492d
    mouse click selects a card: OK

ncurses accepted both encodings. Coordinate mapping is unit-tested separately
(`TestMouse`): the hit map covers every rendered card, column spans never
overlap, a click on empty column space picks that column's first card, a click
outside the board changes nothing, and the wheel scrolls the detail when it has
focus instead of moving the selection.

## 5f. Comment action

The only write path, and it is a subprocess rather than SQL. Proved against a
copy of the live board so the real board is untouched:

    $ python3 comment_probe.py
    target task: t_122da3f2
    copy: comments on that task 10 -> 11
    view reported success: True
    live board bytes unchanged: True

    comment action: OK

The probe points the view at a `.backup` copy with `--db` and exports
`HERMES_KANBAN_DB` to the same copy so the CLI writes there too, types through
the real TUI in a pty, then reads the row count back with SQL and re-hashes the
live board.

`TestCommentAction` pins the rest: `c` opens compose on the selected card, Esc
cancels without running anything, Enter runs
`hermes kanban comment <id> --author kanban-tui <text>` with the body as a single
argv element (`a; rm -rf / && echo $(id)` arrives intact and unsplit), a failing
CLI keeps the text and reports the error, an empty body posts nothing, a missing
`hermes` binary is reported rather than raised, and `--no-write` refuses the
action.

Two board goldens changed for this feature (footer gained "c comment"); the diff
was read before regenerating and the list golden was untouched.

## 5g. Clicks inside herdr

Herdr captures the mouse for its own UI by default, so the first question is
whether a click can reach a pane app at all. It can, two ways.

**Through herdr's own terminal API** (`terminal session control` +
`terminal.mouse`), which delivers an event encoded for the app's mouse mode:

    $ python3 herdr_mouse_probe.py w11:p1
    control viewport 120x40, cursor before: t_122da3f2
    clicking blocked column at cell (106, 5)
    button: left
    cursor after:  t_6e06bc35
    herdr-injected click selects a card: OK

    $ PROBE_BUTTON=right python3 herdr_mouse_probe.py w11:p1
    cursor before: t_6e06bc35 ... cursor after: t_e908e065
    herdr-injected click selects a card: OK

The probe reads the controller's own 120x40 frame stream and derives the click
cell from that render, because the control viewport is not the pane's size
(191x50 here) and coordinates from `pane read` land outside it.

**For a human click**, herdr's `ui.mouse_capture` (default true) decides. Its
docs: "If you prefer keyboard-only control, or you want Herdr to stop capturing
mouse input, disable mouse capture: mouse_capture = false". Per-pane
`herdr pane input <pane> --right-click pane` forwards right-click to a
mouse-reporting app while keeping herdr's left-click UI. Both are herdr
settings; the app side is the probe above.

## 6. Live resize

    $ python3 resize_probe.py
    captured 19030 bytes across 4 resizes
      at 160x46    segment=7432   rows_seen=46 widest=160 overflows=False board_drawn=True
      at 110x30    segment=3679   rows_seen=30 widest=110 overflows=False board_drawn=True
      at 70x22     segment=1840   rows_seen=22 widest=70  overflows=False board_drawn=True
      at 150x40    segment=6079   rows_seen=40 widest=150 overflows=False board_drawn=True
    resize handling: OK

No segment produced a line wider than the pane, and the board was redrawn at
every geometry. 70x22 exercises the narrow tier where the detail pane drops.

## 7. Size degradation

The live herdr pane was 49x38 (`herdr api snapshot` -> `rect/width = 49`,
`viewport_rows = 38`), which is the common case when herdr splits a terminal two
ways. That size is too narrow for two columns, so the view uses one pane at a
time and Tab swaps list <-> detail. Verified live, not just in tests:

    $ herdr pane read wS:p1 --source visible --lines 40 | head -4
     KANBAN  default tri 0  read-only | 2s | 06:32:49
     show open  status all  assignee all   34 listed
    -------------------------------------------------
     todo (8) ---------------------------------------

    $ herdr pane send-keys wS:p1 Tab
    $ herdr pane read wS:p1 --source visible --lines 40 | head -5
     KANBAN  default tri 0  read-only | 2s | 06:32:56
     show open  status all  assignee all   34 listed
    -------------------------------------------------
    t_122da3f2  [todo]                       prio 110
    Captain goal: advance canonical Omni

    $ herdr pane send-keys wS:p1 Tab      # back to the list
    > t_122da3f2 tod 110 default       Captain goal:

Tiers:

    >= 82 cols   list + detail side by side
    30-81 cols   one pane at a time (Tab swaps; footer names the active pane)
    <  30 cols   list only
    <  12 rows   filter line dropped
    <  10 rows   single-line footer

Degenerate sizes still hold the invariant:

    $ ./kanban-tui --print 40x10   -> header, list, footer (filter line dropped)
    $ ./kanban-tui --print 30x8    -> header, one separator, list, footer
    $ ./kanban-tui --print 20x5    -> header, separator, list, footer
    $ ./kanban-tui --print 60x20   -> single pane; list until Tab

`TestRendering.test_frame_matches_requested_size` asserts exactly H lines and
no row wider than W at 160x45, 120x40, 100x30, 84x24, 80x20, 60x16, 40x10,
20x4.

## 8. Cost

Live board, 945 tasks total, 39 open rows, 23 MB db:

    cold start (process spawn to exit):   162 / 168 / 150 ms  (3 runs)
    refresh() on the open set:            3.8 / 3.3 / 3.5 ms
    render() at 160x48:                   4.37 ms/frame

A steady board writes ~40 bytes per tick to the pane because `paint()` diffs
rows; 48 rows cost ~7.4 KB on the first paint only.

## 9. Live refresh against a changing board

The board copy is created with `sqlite3 .backup`, so the live board is never
touched. The probe opens that copy in a pty, inserts one task from a second
connection, and sends no keystroke.

    $ python3 refresh_probe.py
    board copy: ~/.hermes/cache/scratch/kanban-refresh-qq6b5x3t/kanban.db
    todo in db       before=11 after=12
    todo in header   before=11 after=12
    new task visible in the running view: id=True title=True

    header before: KANBAN  default tri 0 tod 11 sch 7 rdy 6 run 2 rev 0 blk 13 ...
    header after:  KANBAN  default tri 0 tod 12 sch 7 rdy 6 run 2 rev 0 blk 13 ...

## 10. Board resolution

    $ env -u HERMES_KANBAN_BOARD HERMES_KANBAN_HOME=/tmp/kbhome ./kanban-tui --where
    db        /tmp/kbhome/kanban/boards/crew/kanban.db
    exists    True

    $ env -u HERMES_KANBAN_BOARD HERMES_KANBAN_HOME=/tmp/kbhome ./kanban-tui --print 80x6
     KANBAN  crew tri 0 tod 11 sch 7 rdy 6 run 2 rev 0     read-only | 2s | 00:32:20

`HERMES_KANBAN_BOARD` outranks the `kanban/current` file, which is the same
precedence the Hermes kanban CLI uses; the test fixture pins the env var to
`default`, so the current-file path is exercised with `env -u`.

## 11. Hot-WAL fallback

`TestWalFallback.test_falls_back_to_query_only_handle` forces the read-only URI
open to raise and asserts the viewer retries with `query_only=1`, reports
`mode == "query_only"`, and still reads counts.
`test_query_only_still_cannot_write` asserts an `UPDATE` on that handle raises.

## 12. Findings fixed during verification

- The detail pane originally fitted each row segment to the full column width,
  pushing every segment after the first off-row. Caught by the first pty
  screenshot, fixed by consuming width segment by segment.
- A finished run kept ageing its elapsed clock (`now - started_at`); it now
  measures to `ended_at`.
- The pane title survived quit and kept advertising stale counts. `clear()`
  now removes it on exit; proved in section 5.
- `ansi_screen.py` crashed on `EL`/`ED` when the cursor had run past the last
  row of a shorter grid. Bounds-clamped; the probe now reports per-geometry
  overflow instead of dying.
- Pressing `2` while the detail pane was hidden set the tab but showed nothing;
  numbered tab keys now force the detail pane back on.
- **The detail pane was unreachable in a narrow pane.** Below 84 columns the
  view dropped the detail column entirely, so `1/2/3` and `Tab` set state
  nothing displayed -- and the live herdr pane is 49 columns. Narrow panes now
  swap one pane at a time, with the footer naming the active one.
- **The default layout was a list, not a board.** Statuses were vertical
  sections, which reads as a filtered list. Wide panes now show status columns
  with the detail as a strip underneath; `b` switches back to the list, which
  remains the layout for panes under 96 columns.
- **Paging hid the column that mattered.** With seven statuses at 100 columns
  the window started at triage, so `blocked` (13 cards) fell off the right edge.
  Empty columns are now dropped before any paging happens.
- **A replay-tool artifact was nearly misread as an app bug.** Rows showed
  `t_122da3f2  [todo]-------prio 110`. The raw bytes contain `\x1b[23X` (ECH)
  where the gap is: ncurses blanks the span without writing spaces. The
  renderer was correct and `ansi_screen.py` simply lacked ECH (now implemented,
  with DCH alongside). Snapshot text and pty replay agree after the fix.
