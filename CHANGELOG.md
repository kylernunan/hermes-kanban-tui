# Changelog

Versions follow semver: a change to the JSON contract or the keyboard map is a
minor bump, a read-only guarantee change would be major.

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
