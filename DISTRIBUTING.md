# Releasing kanban-tui

One module, one wheel, no dependencies. The whole release is: bump the version,
build, prove the built artifact, tag, publish.

## What a release must contain

    kanban_tui.py        the program
    LICENSE              MIT
    README.md            install + usage + layout
    pyproject.toml       metadata, console script
    MANIFEST.in          what the sdist carries

The wheel contains `kanban_tui.py` and nothing else. If a release changes that,
something is wrong with `[tool.setuptools] py-modules`.

## Version

`VERSION` in `kanban_tui.py` is the single source; `pyproject.toml` reads it
through `[tool.setuptools.dynamic]`, so there is nothing to keep in sync. The
console script prints it:

    kanban-tui --version

## Build

    rm -rf dist build *.egg-info
    uv build --wheel --sdist        # or: python3 -m build

## Prove the artifact before publishing

A wheel that installs is not the same as a wheel that works. This is the check
that caught `--where` existing only in the shell launcher:

    S=$(mktemp -d)
    python3 -m venv $S/venv
    $S/venv/bin/pip install --no-index --no-deps dist/*.whl
    cd /tmp                                    # not the source tree
    $S/venv/bin/python -c "import kanban_tui; print(kanban_tui.__file__)"
    $S/venv/bin/kanban-tui --version
    $S/venv/bin/kanban-tui --db /path/to/kanban.db --print 120x40 | head
    $S/venv/bin/kanban-tui --db /path/to/kanban.db --json | head -c 200

The `--where` line must name the site-packages copy, not the checkout. Then run
the suite against the installed bytes, with the source tree off `sys.path`:

    cd $S && cp <repo>/{test_kanban_tui.py,ansi_screen.py,pty_drive.py} .
    mkdir tests && cp <repo>/tests/*.txt tests/
    ln -s $S/venv/lib/python*/site-packages/kanban_tui.py kanban_tui.py
    $S/venv/bin/python -m unittest test_kanban_tui

Passing that means the sdist and wheel are complete: no test depends on a file
that was left out of the package.

## Tag and publish

    git tag -a v$(kanban-tui --version | awk '{print $3}') -m "kanban-tui $(...)"
    git push origin main --tags
    uv publish dist/*                # or: python3 -m twine upload dist/*

Publishing is a credential action: it needs the owner's account and explicit go.

## Checklist

1. `./verify.sh` is green (10 stages, includes live-board read and comment probe).
2. `kanban-tui --version` matches the tag you are about to cut.
3. The wheel contains only `kanban_tui.py` plus metadata.
4. The fresh-venv proof above passes, including the suite run from `$S`.
5. No secrets, hostnames, tailnet addresses, or personal absolute paths in the
   tree: `grep -rn -E "k-server|/home/|100\.[0-9]+\.[0-9]+\." --include='*.py'
   --include='*.md' .`
6. `docs/VERIFICATION.md` still describes this version's evidence.

## Files that are development-only

`verify.sh`, the `*_probe.py` helpers, `pty_drive.py`, `ansi_screen.py`,
`test_kanban_tui.py`, `tests/`, and `docs/` ship in the sdist for people who want
to re-run the proof, and are never imported by the installed program.
