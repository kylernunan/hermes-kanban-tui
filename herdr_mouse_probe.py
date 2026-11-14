#!/usr/bin/env python3
"""herdr_mouse_probe.py -- click a card through herdr's own terminal API.

Herdr captures the mouse for its own UI by default (`ui.mouse_capture = true`),
so a human click never reaches a pane app. Its socket API can still deliver one:
`herdr terminal session control <pane>` accepts `terminal.mouse` events and
encodes them for the application's mouse mode, dropping them when the app has
not enabled mouse reporting.

That makes the app's mouse path testable inside herdr itself, independently of
whether the user's own clicks are being captured.

The controller opens its own viewport (120x40 unless --cols/--rows are given),
so the click coordinates are derived from the frames it streams, not from
`pane read`.

    python3 herdr_mouse_probe.py w0:p1
"""
from __future__ import annotations

import base64
import json
import os
import re
import select
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ansi_screen import Screen  # noqa: E402


def cursor_task(text):
    for line in text.split("\n"):
        m = re.search(r">\s*(t_[0-9a-z]+)", line)
        if m:
            return m.group(1)
    return None


def find_card(text, want="blocked", avoid=None):
    """A card cell in the named column, using the rendered frame itself."""
    lines = text.split("\n")
    header_i = next(
        (i for i, l in enumerate(lines) if "todo" in l and "blocked" in l), None
    )
    if header_i is None:
        return None
    spans = [
        (m.group(1), m.start())
        for m in re.finditer(
            r"(triage|todo|scheduled|ready|running|review|blocked)\s+\d+", lines[header_i]
        )
    ]
    for status, start in spans:
        if want and status != want:
            continue
        for i in range(header_i + 2, min(len(lines), header_i + 30)):
            line = lines[i]
            if start >= len(line):
                continue
            m = re.search(r"t_[0-9a-z]+", line[start:start + 14])
            if m and not (avoid and avoid.startswith(m.group(0))):
                return status, start + 2, i
    return None


def read_frame(proc, screen, timeout):
    """Feed one streamed frame into the screen; returns the screen or None."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r, _, _ = select.select([proc.stdout], [], [], 0.3)
        if not r:
            continue
        line = proc.stdout.readline()
        if not line:
            return None
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("type") != "terminal.frame":
            continue
        if screen is None:
            screen = Screen(msg["width"], msg["height"])
        screen.feed(base64.b64decode(msg["bytes"]))
        return screen
    return None


def main():
    pane = sys.argv[1] if len(sys.argv) > 1 else "w0:p1"
    proc = subprocess.Popen(
        ["herdr", "terminal", "session", "control", pane],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )

    screen = None
    text = ""
    for _ in range(12):
        screen = read_frame(proc, screen, 2.0)
        if screen is None:
            break
        text = screen.render()
        if cursor_task(text):
            break
    if not text:
        print("FAILED: no frame received from the control stream")
        return 1

    was = cursor_task(text)
    # Clicking the card that is already selected proves nothing, so pick a
    # different one: the probe must observe a change.
    target = find_card(text, want="blocked", avoid=was) or find_card(text, want=None, avoid=was)
    if not target:
        print("FAILED: no blocked card in the %dx%d control viewport" % (screen.cols, screen.rows))
        return 1
    status, x, y = target
    print("control viewport %dx%d, cursor before: %s" % (screen.cols, screen.rows, was))
    print("clicking %s column at cell (%d, %d)" % (status, x, y))

    button = os.environ.get("PROBE_BUTTON", "left")
    print("button: %s" % button)
    for action in ("down", "up"):
        proc.stdin.write(json.dumps({
            "type": "terminal.mouse", "action": action, "button": button,
            "column": x, "row": y,
        }) + "\n")
        proc.stdin.flush()
        time.sleep(0.3)

    now = None
    for _ in range(12):
        screen = read_frame(proc, screen, 1.5)
        if screen is None:
            break
        now = cursor_task(screen.render())
        if now and now != was:
            break

    proc.stdin.close()
    try:
        proc.wait(timeout=6)
    except subprocess.TimeoutExpired:
        proc.kill()

    print("cursor after:  %s" % now)
    ok = bool(now) and now != was
    print("\nherdr-injected click selects a card: %s" % ("OK" if ok else "FAILED"))
    if not ok:
        print("---- frame tail ----")
        print("\n".join(screen.render().split("\n")[-8:]))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
