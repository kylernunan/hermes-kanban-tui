#!/usr/bin/env python3
"""ansi_screen.py -- turn captured pty bytes back into the visible grid.

Small, purpose-built VT interpreter (CUP/CUD/CUU/CUF/CUB, EL, ED, SGR-ignore,
alt-screen ignore). Enough to show what a terminal actually displayed after a
run, which is the evidence needed before claiming a TUI renders correctly.

    python3 ansi_screen.py --cols 164 --rows 50 --raw-out capture.bin
"""
from __future__ import annotations

import argparse
import re

CSI = re.compile(rb"\x1b\[([0-9;?]*)([@-~])")


class Screen:
    def __init__(self, cols, rows):
        self.cols = cols
        self.rows = rows
        self.grid = [[" "] * cols for _ in range(rows)]
        self.x = 0
        self.y = 0

    def _clamp(self):
        self.y = max(0, min(self.y, self.rows - 1))
        self.x = max(0, min(self.x, self.cols - 1))

    def _put(self, ch, width=1):
        if self.y < 0 or self.y >= self.rows:
            return
        if self.x < 0:
            self.x = 0
        if self.x + width > self.cols:
            # wrap
            self.x = 0
            self.y += 1
            if self.y >= self.rows:
                return
        self.grid[self.y][self.x] = ch
        for i in range(1, width):
            if self.x + i < self.cols:
                self.grid[self.y][self.x + i] = ""
        self.x += width

    def feed(self, data):
        i = 0
        n = len(data)
        while i < n:
            m = CSI.match(data, i)
            if m:
                args, final = m.group(1), m.group(2)
                self._csi(args.decode("ascii", "replace"), final.decode("ascii", "replace"))
                i = m.end()
                continue
            b = data[i]
            if b == 0x1B:
                # ESC ( X, ESC ) X, ESC =, ESC >, ESC 7/8, OSC
                if i + 1 < n and data[i + 1:i + 2] == b"]":
                    end = data.find(b"\x07", i)
                    if end == -1:
                        end = data.find(b"\x1b\\", i)
                        i = (end + 2) if end != -1 else n
                    else:
                        i = end + 1
                    continue
                if i + 2 < n and data[i + 1:i + 2] in (b"(", b")"):
                    i += 3
                    continue
                if i + 1 < n and data[i + 1:i + 2] in (b"=", b">", b"7", b"8", b"M", b"H", b"c"):
                    i += 2
                    continue
                i += 1
                continue
            if b == 0x0D:
                self.x = 0
                i += 1
                continue
            if b == 0x0A:
                self.y += 1
                i += 1
                if self.y >= self.rows:
                    self.y = self.rows - 1
                continue
            if b == 0x08:
                self.x = max(0, self.x - 1)
                i += 1
                continue
            if b == 0x07:
                i += 1
                continue
            # decode one UTF-8 character
            for size in (1, 2, 3, 4):
                try:
                    ch = data[i:i + size].decode("utf-8")
                except UnicodeDecodeError:
                    continue
                width = 2 if _wide(ch) else 1
                self._put(ch, width)
                i += size
                break
            else:
                i += 1
        return self

    def _csi(self, args, final):
        self._clamp()
        parts = [p for p in args.lstrip("?").split(";") if p != ""] if args.lstrip("?") else []
        nums = []
        for p in parts:
            try:
                nums.append(int(p))
            except ValueError:
                nums.append(0)
        if final == "H" or final == "f":
            self.y = max(0, (nums[0] if nums else 1) - 1)
            self.x = max(0, (nums[1] if len(nums) > 1 else 1) - 1)
        elif final == "A":
            self.y = max(0, self.y - (nums[0] if nums else 1))
        elif final == "B":
            self.y = min(self.rows - 1, self.y + (nums[0] if nums else 1))
        elif final == "C":
            self.x = min(self.cols - 1, self.x + (nums[0] if nums else 1))
        elif final == "D":
            self.x = max(0, self.x - (nums[0] if nums else 1))
        elif final == "G":
            self.x = max(0, (nums[0] if nums else 1) - 1)
        elif final == "d":
            self.y = max(0, (nums[0] if nums else 1) - 1)
        elif final == "X":
            # ECH: erase N cells from the cursor without moving it. ncurses uses
            # this to blank the gap between two segments of one row, so a replay
            # without it shows the previous frame's characters there.
            count = nums[0] if nums and nums[0] else 1
            for xx in range(self.x, min(self.cols, self.x + count)):
                self.grid[self.y][xx] = " "
        elif final == "P":
            # DCH: delete N cells, shifting the rest of the row left.
            count = nums[0] if nums and nums[0] else 1
            row = self.grid[self.y]
            del row[self.x:self.x + count]
            row.extend([" "] * count)
        elif final == "J":
            self._clamp()
            mode = nums[0] if nums else 0
            if mode == 2 or mode == 3:
                self.grid = [[" "] * self.cols for _ in range(self.rows)]
            elif mode == 0:
                for xx in range(self.x, self.cols):
                    self.grid[self.y][xx] = " "
                for yy in range(self.y + 1, self.rows):
                    self.grid[yy] = [" "] * self.cols
        elif final == "K":
            self._clamp()
            mode = nums[0] if nums else 0
            if mode == 0:
                for xx in range(self.x, self.cols):
                    self.grid[self.y][xx] = " "
            elif mode == 2:
                self.grid[self.y] = [" "] * self.cols
        elif final == "L":
            count = nums[0] if nums else 1
            self._clamp()
            for _ in range(count):
                self.grid.insert(self.y, [" "] * self.cols)
                self.grid.pop()
        elif final == "M":
            count = nums[0] if nums else 1
            self._clamp()
            for _ in range(count):
                self.grid.pop(self.y)
                self.grid.append([" "] * self.cols)

    def render(self):
        return "\n".join("".join(r).rstrip() for r in self.grid)


def _wide(ch):
    import unicodedata

    return unicodedata.east_asian_width(ch) in ("W", "F")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cols", type=int, required=True)
    ap.add_argument("--rows", type=int, required=True)
    ap.add_argument("--raw-out", required=True)
    a = ap.parse_args()
    with open(a.raw_out, "rb") as fh:
        data = fh.read()
    print(Screen(a.cols, a.rows).feed(data).render())


if __name__ == "__main__":
    main()
