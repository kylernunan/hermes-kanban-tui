# hermes-kanban-tui

A read-only terminal view of the Hermes Kanban board, built to run inside
[herdr](https://herdr.dev) panes in Ghostty. Install it as
`hermes-kanban-tui`; the command it puts on your PATH is `kanban-tui`.

[![PyPI](https://img.shields.io/pypi/v/hermes-kanban-tui.svg)](https://pypi.org/project/hermes-kanban-tui/)
[![python](https://img.shields.io/pypi/pyversions/hermes-kanban-tui.svg)](https://pypi.org/project/hermes-kanban-tui/)

Not affiliated with Nous Research. It reads the board their `hermes` CLI writes
and never writes to it beyond the one comment action described below.

    kanban-tui                      # current board, status columns
    kanban-tui --all                # include done + archived history
    kanban-tui --status blocked     # start filtered
    kanban-tui --pane               # open in a fresh herdr workspace
    kanban-tui --json               # machine-readable board, then exit
    kanban-tui --print 160x48       # one frame as text, then exit
    kanban-tui --where              # print what it resolved, then exit
    kanban-tui --mouse              # click to select, wheel to scroll
    kanban-tui --no-write           # disable the comment action

Keyboard first, no build step, no dependency on Linux or macOS. Read-only: it
never writes to the board.

## Install

No dependencies, one module. Pick whichever you already use:

    pipx install hermes-kanban-tui          # isolated, puts kanban-tui on PATH
    uv tool install hermes-kanban-tui
    pip install --user hermes-kanban-tui

From a checkout, without installing anything:

    git clone https://github.com/kylernunan/hermes-kanban-tui
    cd hermes-kanban-tui
    ./kanban-tui --where

Requires Python 3.10+. `curses` ships with Python on Linux and macOS; on Windows
pip pulls in `windows-curses` for you, and if it is ever missing the interactive
view says so instead of crashing while `--print`, `--json` and `--where` keep
working.

CI runs the full 129-test suite on Linux, macOS and Windows (`windows-latest`,
Python 3.14), plus a wheel-install job and the snapshot modes on Windows. What is
still untested on Windows is the interactive curses view itself: nothing has driven
it in a real Windows console, so treat that as unproven rather than supported. The
pane-title and mouse features assume a Unix terminal either way.
It reads the board the Hermes CLI reads: `~/.hermes/kanban.db`, `HERMES_HOME`,
`HERMES_KANBAN_HOME`, or `--db`, in that order of specificity. `--where` prints
exactly what it resolved before you look at a blank screen.

Installed or checked out, the flags are identical: the shell script only finds
the module and picks an interpreter, every option lives in `kanban_tui.py`.

## Why it exists

`hermes kanban list` is a good script surface and a poor dashboard: it prints
one status at a time, one screen at a time, and nothing refreshes. The board
also carries evidence the CLI summaries drop -- which run a card is on, when it
last sent a heartbeat, how close it is to its runtime limit, why it blocked, and
which cards are gated by unfinished parents. This view shows all of it on one
screen, answers "what needs me now" per card, and leaves the board untouched.

## Read-only by design, with one comment path

The viewer opens `kanban.db` with `mode=ro` (falling back to `query_only=1` on a
second handle if a hot WAL refuses a read-only open) and issues only `SELECT`s.
The single exception is the comment action, and even that does not write SQL: it
runs `hermes kanban comment` as a subprocess, so the CLI remains the only writer
on the board.

There is no claim, unblock, complete, reassign, promote or dispatch path in the
code. `--no-write` removes the comment action as well, for a display that must
not mutate anything.

Proved by test: `TestCli.test_db_is_byte_identical_after_snapshot` and
`TestLiveBoard.test_live_snapshot` hash `kanban.db` before and after a run and
require the bytes to match.

## What is on screen

Two layouts over the same selection. Wide panes get the board; narrow panes get
the list. `b` switches between them at any width.

    BOARD (default at >= 96 cols and >= 18 rows)

    row 0     board name, per-status counts, refresh interval, clock
    row 1     filters: open/history, status, assignee, search, listed count
    columns   one column per status, left to right in workflow order:
              triage, todo, scheduled, ready, running, review, blocked
              (+ done, archived with 'd'), each headed by its count
    cards     id, a why-it-is-not-moving signal, age, and up to two wrapped
              title lines
    strip     the selected card underneath: run, heartbeat, claim, block kind,
              failure, then body / events / comments

    LIST (narrow panes, or 'b')

    left      the same tasks as one scrolling list grouped by status
    right     the same detail, side by side instead of underneath

Column widths follow load: every column keeps a 16-cell floor, the remaining
cells are shared in proportion to how many cards each holds, and no column takes
more than 40 so one busy status cannot swallow the board. On the live board that
gives blocked (13 cards) 31 cells and the empty running column 17, instead of 24
each.

Columns stay in a fixed workflow order so a card moving status moves sideways
rather than shuffling the board. An empty column still shows its count; when the
pane is too narrow for all of them the empty ones are dropped first, and only
then does the board page a window that follows the cursor (`<` / `>` in the
column headers mark the hidden side).

Done and archived hold 917 rows between them, which no column can show. Those
two columns load the most recent `--history-cap` cards (default 25), ordered by
completion time, and the header keeps the true total ("done 860"). When a spare
row exists the column ends with "+N older". A selected card that falls outside
the loaded window is pulled back into view rather than silently disappearing.

### The signal on a card

Six cells that answer "why is this not moving", so a card never needs opening to
be triaged:

    !cap      blocked on a capability wall (no access, no credential)
    !need     blocked needing an owner decision
    !dep      blocked on a dependency that is itself unfinished
    !trns     blocked on a transient failure
    !block    blocked with no typed reason
    depN      N parents are still open, so nothing can run it yet
    hbNNs     running, heartbeat this recent
    !hb ...   running, heartbeat is stale (over 120s)

The card's id line is coloured by severity: red for blocked or a stale
heartbeat, yellow for a dependency wait. A selected card keeps the reverse
cursor style so the position is never ambiguous.

On this board that surfaces real state: 13 blocked cards split 6 `!cap`,
3 `!need`, 4 `!block`, and 10 open cards that are silently waiting on open
parents (one on five).

Detail tabs:

    1  body       markdown-ish body, wrapped to at most 100 columns so prose
                  stays readable in a very wide pane
    2  events     the task_events stream with payload summarised
    3  comments   full comment thread

## Keys

    j k            move between tasks / down and up the current column
    h l            board only: previous / next column (skips empty ones)
    g G            first / last card in the column
    Space u        page down / page up
    Tab Enter      switch focus between the board (or list) and the detail
    1 2 3          detail tab: body / events / comments (also focuses detail)
    z              show or hide the detail strip / pane
    b              switch between the board and the list view
    s S            cycle status filter forward / back
    a A            cycle assignee filter forward / back
    /              search id, title, assignee (Enter apply, Esc cancel)
    d              show or hide done + archived
    r              refresh now
    p              pause / resume auto refresh
    ?              help
    q              quit

## Commenting (the only write)

    c              comment on the selected card
    Enter          post          Esc cancel        Ctrl-U clear

The compose line shows the target card, and posting runs exactly one command:

    hermes kanban comment <task_id> --author kanban-tui <text>

The text is passed as a single argv element, so a semicolon, backtick or `$(...)`
in a comment is stored literally rather than interpreted. The view itself never
writes SQL: `kanban.db` is opened read-only and the only mutation goes through
the CLI, which keeps one writer on the board. After a successful post the view
refreshes and switches the detail to the comments tab.

A failed post keeps your text in the compose line and reports the error, so it
can be retried rather than retyped. `--no-write` removes the action entirely;
`--author NAME` changes the recorded author.

Deliberately absent: claim, unblock, complete, reassign, promote and dispatch.
Those change what the dispatcher picks up, so they stay in the CLI where the
confirmation lives.

## Mouse (opt-in, and gated by herdr)

    kanban-tui --mouse

Click a card to select it, click empty space in a column to jump to its first
card, right-click does the same, double-click selects and focuses the detail,
wheel moves the selection or scrolls the detail when it has focus. Off by
default: a pane that captures the mouse steals drag-select and the terminal's
own scrollback, so you choose it per run.

Inside herdr there is a second gate that is not ours: herdr is mouse-native and
captures the mouse for its own UI (`ui.mouse_capture`, default `true`), so a
plain click selects herdr panes and drags borders instead of reaching the app.
Herdr's docs are explicit about the switch:

    # ~/.config/herdr/config.toml
    [ui]
    mouse_capture = false

Turning it off gives the pane the mouse and gives up herdr's click-to-focus,
drag-borders, right-click menus and drag-select copy. The narrower option is
per-pane right-click forwarding, which keeps herdr's left-click UI:

    herdr pane input <pane> --right-click pane

Both are herdr settings, not this program's. The app side is proved by
`herdr_mouse_probe.py`, which delivers a click through herdr's own terminal API
and requires the cursor to move.

## herdr and Ghostty

**Ghostty's terminfo.** Ghostty sets `TERM=xterm-ghostty`. The stock
`ncurses-term` package on this box ships a `ghostty` entry but **not**
`xterm-ghostty`, so ncurses programs die with `setupterm: could not find
terminal`. Two independent defenses:

1. `kanban_tui.py` probes terminfo at startup and falls back to
   `xterm-256color` when the exported name has no entry, reporting the
   substitution in the help screen. Verified by running with an empty
   `TERMINFO` directory (see `docs/VERIFICATION.md`).
2. `install-terminfo.sh` installs a real `xterm-ghostty` entry into
   `~/.terminfo` by rewriting the header of the shipped `ghostty` entry.
   User-local, no sudo, and `--check` reports state without changing anything.

**Pane title.** Inside a herdr pane (`HERDR_ENV=1` and `HERDR_PANE_ID` set) the
view publishes `kanban <board>: N running, N ready, N blocked` as pane metadata
through `herdr pane report-metadata`, so the workspace tab strip carries crew
state while the pane is in the background. The title is cleared on exit -- a
quit board must not keep advertising counts. Any herdr error is recorded and
shown in the help screen; it never takes the board down. `--no-herdr` disables
it entirely.

**Flicker.** `paint()` diffs rows against the previous frame and rewrites only
what changed, so a steady board writes almost nothing to the pane instead of
repainting 48 rows every two seconds.

## Snapshot mode

Two non-interactive modes, no TTY needed:

    ./kanban-tui --print 160x48          # one frame as text
    ./kanban-tui --json                  # the filtered board as JSON
    ./kanban-tui --json --status blocked # machine-readable, scripted

`--json` emits `{"schema": "hermes-kanban-tui/1", generated_at, board, db,
filters, counts, task_count, tasks[]}`. Each task carries id, title, status,
assignee, priority, timestamps, age, `open_parents`, `workspace_path`, the
`block` object (kind, recurrences, consecutive failures, last failure) when
blocked, `current_run` (id, profile, status, elapsed, runtime limit, heartbeat
age) when running, and the same `signal` / `severity` the card shows. Filters
apply. It never writes.

That is the surface Fleet, HQ or a watchdog can consume without parsing column
text.

Output is a fixed-size character grid: exactly H lines, no line wider than W
display cells. That invariant is asserted across eight sizes in
`TestRendering.test_frame_matches_requested_size`. Narrow-layout tests cover the
30-81 column tier: Tab swaps panes, all three detail tabs are reachable, and no
row overflows at 70x30, 49x38 or 30x12.

## Layout tiers

The frame degrades instead of breaking:

    >= 96 cols and >= 18 rows   board: status columns + detail strip
    <  96 cols or <  18 rows    list; >= 82 cols keeps list and detail side by side
    30-81 cols                  list with one pane at a time: Tab swaps
                                list <-> detail, 1/2/3 jumps to a detail tab
    <  30 cols                  list only
    <  12 rows                  filter line dropped
    <  10 rows                  single-line footer

Board columns need ~16 cells each, which is why the board starts at 96 for the
seven open statuses. `b` overrides the automatic choice in either direction.

The one-pane list tier exists because a herdr pane split two ways is often ~49
columns wide, and a detail pane that cannot be reached is the same as no detail
pane. Verified at 49x38 live, and down to 20x5 overall.

## Files

    kanban_tui.py         the viewer (stdlib only, no install)
    kanban-tui            launcher: board resolution, --pane, --where
    install-terminfo.sh   add the xterm-ghostty terminfo entry
    test_kanban_tui.py    122 tests, fixture board + live-board smoke test
    pty_drive.py          run the TUI in a real pty and capture every byte
    ansi_screen.py        turn those bytes back into the visible grid
    resize_probe.py       SIGWINCH at four sizes, report any overflow
    refresh_probe.py      insert into a board copy, prove live refresh works
    mouse_probe.py        click a card in a real pty, prove the cursor moves
    herdr_mouse_probe.py  click through herdr's terminal API, inside a pane
    comment_probe.py      type a comment into a board copy, read it back
    verify.sh             every check above, PASS/FAIL per stage
    tests/golden-*.txt    stored frames: board 160x40, board 100x24, list 70x26
    docs/VERIFICATION.md  the commands run and what they returned
    docs/screen-148x40.txt  a captured board frame, for diffing a layout change

## Tests

    ./verify.sh            # unit tests + every probe, PASS/FAIL per stage
    ./verify.sh --quick    # unit tests only
    python3 -m unittest test_kanban_tui -v

Fixture tests cover geometry (wide characters, control characters, exact
padding), the read-only guarantee, ordering, every key binding, filter cycling,
search, the herdr bridge's enabled/disabled/error paths, and the CLI. The
live-board test runs only when `~/.hermes/kanban.db` exists.

`mouse_probe.py` does the same for pointer input: it locates a card in the
rendered frame, writes a mouse report into the pty the way a terminal would, and
requires the selection to move to that card. It tries SGR (1006) and the legacy
X10 encoding; ncurses accepted both here.

`pty_drive.py` plus `ansi_screen.py` are the end-to-end proof: a real pty, a
real window size, real keystrokes, and the resulting screen reconstructed from
the captured bytes. Unit tests cannot see a curses crash on startup; that pair
can.

`tests/golden-*.txt` are whole frames at three geometries (board 160x40, board
100x24 where empty columns drop, list 70x26). They pin the picture, not just the
facts: a change that keeps every field but moves it on screen still fails. The
clock and absolute dates are normalised out, and `App.render(now=...)` takes an
injectable clock so a frame is reproducible. After an intended layout change:

    KANBAN_TUI_UPDATE_GOLDEN=1 python3 -m unittest test_kanban_tui.TestGoldenFrame

and read the diff before committing to it.

## Notes

- `~/.hermes/kanban.db` is the `default` board. Named boards live at
  `~/.hermes/kanban/boards/<slug>/kanban.db`; `--board <slug>` resolves them.
  `~/.hermes/kanban/kanban.db` exists but is a zero-byte file -- the viewer
  refuses it rather than showing an empty board.
- Auto-refresh is 2s by default (`--interval`). One tick is three query sets
  (counts, open tasks, parent-wait/live-run maps) and runs in ~12 ms on a 23 MB
  board with 950 tasks.
- The elapsed clock on a finished run is measured to `ended_at`, so a completed
  run does not keep ageing.
