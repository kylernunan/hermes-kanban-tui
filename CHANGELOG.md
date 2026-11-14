# Changelog

Versions follow semver: a change to the JSON contract or the keyboard map is a
minor bump, a read-only guarantee change would be major.

## 1.0.2

- Windows: `--print` and `--json` no longer die with `UnicodeEncodeError` when
  output goes to a pipe or file under a non-UTF-8 locale. A card with an emoji
  was enough to crash it. Output is now forced to UTF-8, replacing characters a
  stream cannot represent instead of raising.

      $ PYTHONIOENCODING=ascii kanban-tui --db board.db --print 110x24
      UnicodeEncodeError: 'ascii' codec can't encode character '\U0001f680'   # 1.0.1
      exit=0                                                                  # 1.0.2

- A board path is split on both `/` and `\` when deriving the board label, so a
  board under `kanban/boards/<slug>/` is named correctly whichever separator the
  path arrived with.
- Tests read and write the golden frames as UTF-8, so a cp1252 default locale no
  longer turns them into mojibake. 129 tests.
- CI gains a `windows-latest` job: pip install, the curses shim, the fixture
  board, every non-interactive mode, and the full suite.

## 1.0.1

- Windows: a Python without `curses` no longer dies on import with a traceback.
  `--print`, `--json`, `--where` and `--version` now work there, and the
  interactive view explains itself instead of crashing. `windows-curses` is
  declared for `sys_platform == 'win32'`, so pip installs it on Windows.
  The Windows interactive path is untested; the graceful path is tested.

## 1.0.0

First public release.

- Board layout: status columns with proportional widths, detail strip beneath,
  list layout for narrow panes, `b` toggles.
- Per-card signals: `!cap`, `!need`, `!dep`, `!trns`, `!block`, `depN`, `hbNNs`,
  `!hb <age>`, coloured by severity.
- Detail tabs: body, events, comments (`1`/`2`/`3`), paginated.
- Comment action (`c`), the only write in the program, posted through
  `hermes kanban comment` so the CLI stays the single writer.
- Opt-in mouse (`--mouse`): click a card, click a column, right-click, double
  click for detail, wheel to move or scroll.
- Non-interactive surfaces: `--print WxH`, `--json`, `--where`.
- herdr integration: pane title publishes per-status counts, `--pane` opens a
  workspace, `--no-herdr` turns the reporting off.
- Packaging: installable as `hermes-kanban-tui` with a `kanban-tui` script; all
  flags implemented in the module, so an installed copy matches a checkout.
