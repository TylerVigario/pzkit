#!/usr/bin/env python3
"""
pztail -- keep a COMPLETE Project Zomboid DebugLog, which the game itself will not.

WHY THIS EXISTS
---------------
PZ caps each log file at ~10 MB and, on reaching it, DELETES THE CONTENTS AND
STARTS OVER. Not rotation -- there is no backup, the earlier part is simply gone.
Read out of zombie/core/logger/ZLogger in projectzomboid.jar (build 42.21.0):

    static final long s_maxSizeKo = 10000;

    void checkSizeUnsafe() {
        if (this.file.length() / 1024L > s_maxSizeKo)
            recreateLogFileUnsafe();
    }

    void recreateLogFileUnsafe() {
        this.outputStreams.file.close();
        this.file = new File(LoggerManager.getLogFilePath(this.name));
        this.outputStreams.file = new PrintStream(this.file);  // truncates
    }

So it fires at 10,241,024 bytes exactly (integer division: the first length whose
length/1024 exceeds 10000), reopens a PrintStream on the SAME path, and that
truncates. `s_maxSizeKo` is `static final` -- a compile-time constant, with no
system property, no command-line flag and no debuglog.cfg key behind it. There is
nothing to configure.

Measured consequence, 2026-11-04: a 3h58m session left 214 lines covering its last
6m20s. Everything before 19:15:17 was destroyed while the game was still running.

Reducing log volume is not a way out either. In steady state **82% of the bytes are
Viewpoint's own stat line** -- 4.8 KB per sample, which is precisely the data worth
keeping. Silencing every other channel via debuglog.cfg buys 18%, moving the cap
from 4.7 to 5.7 hours. And Viewpoint has no key to trim the line: all 566 keys in
viewpoint-live.properties were checked.

Hence: copy the bytes out as they are written, and survive the truncate.

    python pztail.py                 # follow the live session, Ctrl-C to stop
    python pztail.py --once          # one catch-up pass, then exit
    python pztail.py --console       # also follow console.txt (its own 10 MB budget)

Captures land in captures/<logname>.full.txt and are append-only. pzbench.py reads
them directly -- it keys off lines containing "gpu ms floors" and ignores the seam
markers this writes.
"""

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
CAPTURES = HERE / "captures"
ZOMBOID = Path.home() / "Zomboid"
LOGS = ZOMBOID / "Logs"

# PZ fires at length/1024 > 10000, i.e. the first length >= 10001*1024.
PZ_TRUNCATE_AT = 10001 * 1024

# How much trailing context to re-verify each poll. If the bytes at
# offset-ANCHOR no longer match what we read last time, the file underneath us
# is not the one we were reading -- treat it as a reset rather than appending
# into the middle of a fresh file and silently corrupting the capture.
ANCHOR = 256


def newest_loose_log():
    """The live session's DebugLog. PZ moves the PREVIOUS session's logs into
    Logs/logs_<timestamp>/ at launch, so the live one is the loose file."""
    files = [p for p in LOGS.glob("*_DebugLog.txt") if p.is_file()]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


class Follower:
    """Follows one file, appending everything it ever contains to one capture."""

    def __init__(self, src, dest):
        self.src = src
        self.dest = dest
        self.offset = 0
        self.anchor = b""
        self.resets = 0
        self.copied = 0
        dest.parent.mkdir(exist_ok=True)

    def _note(self, text):
        with open(self.dest, "ab") as out:
            out.write(f"\n=== pztail: {text} ===\n".encode("utf-8"))

    def _reset(self, why, size):
        self.resets += 1
        was = self.offset
        self._note(f"{datetime.now():%Y-%m-%d %H:%M:%S} {why} "
                   f"(was at {was:,} B, file now {size:,} B) "
                   f"-- reset #{self.resets}, capture continues")
        self.offset = 0
        self.anchor = b""
        print(f"  [{datetime.now():%H:%M:%S}] {why} after {was:,} B "
              f"-- {self.src.name} restarted, capture intact", flush=True)

    def poll(self):
        """Copy whatever is new. Returns bytes copied this pass."""
        try:
            size = self.src.stat().st_size
        except FileNotFoundError:
            # recreateLogFile closes and reopens; the path can blink out.
            return 0

        if size < self.offset:
            self._reset("log truncated by the game", size)
        elif self.anchor and size > self.offset:
            # Same length or longer, but is it the same file? Check the anchor.
            try:
                with open(self.src, "rb") as f:
                    back = min(ANCHOR, self.offset)
                    f.seek(self.offset - back)
                    if f.read(back) != self.anchor:
                        self._reset("log replaced underneath us", size)
            except OSError:
                return 0

        if size <= self.offset:
            return 0

        try:
            with open(self.src, "rb") as f:
                f.seek(self.offset)
                chunk = f.read(size - self.offset)
        except OSError:
            return 0
        if not chunk:
            return 0

        with open(self.dest, "ab") as out:
            out.write(chunk)
        self.offset += len(chunk)
        self.copied += len(chunk)
        self.anchor = chunk[-ANCHOR:] if len(chunk) >= ANCHOR else (self.anchor + chunk)[-ANCHOR:]
        return len(chunk)

    def headroom(self):
        """How far the live file is from the game's own truncate point."""
        try:
            size = self.src.stat().st_size
        except OSError:
            return None
        return PZ_TRUNCATE_AT - size


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true",
                    help="single catch-up pass over the current log, then exit")
    ap.add_argument("--console", action="store_true",
                    help="also follow ~/Zomboid/console.txt (separate 10 MB budget)")
    ap.add_argument("--interval", type=float, default=2.0,
                    help="poll seconds (default 2; the log grows ~35 KB/min, so this "
                         "is three orders of magnitude faster than it needs to be)")
    a = ap.parse_args()

    src = newest_loose_log()
    if src is None:
        sys.exit(f"no *_DebugLog.txt in {LOGS} -- has the game ever been run?")

    followers = [Follower(src, CAPTURES / (src.stem + ".full.txt"))]
    if a.console:
        c = ZOMBOID / "console.txt"
        if c.exists():
            followers.append(Follower(c, CAPTURES / f"{src.stem}.console.full.txt"))
        else:
            print(f"  no {c}, skipping --console")

    for f in followers:
        print(f"  following {f.src}")
        print(f"       into {f.dest}")
    if a.once:
        for f in followers:
            f.poll()
            print(f"  {f.copied:,} B copied from {f.src.name}")
        return 0

    print(f"  polling every {a.interval:g}s. Ctrl-C to stop.\n")
    watched = src
    try:
        while True:
            for f in followers:
                f.poll()

            # A new session rotates the old log away and starts a new loose file.
            now = newest_loose_log()
            if now is not None and now != watched:
                print(f"\n  new session: {now.name}", flush=True)
                watched = now
                followers = [Follower(now, CAPTURES / (now.stem + ".full.txt"))]
                if a.console and (ZOMBOID / "console.txt").exists():
                    followers.append(Follower(ZOMBOID / "console.txt",
                                              CAPTURES / f"{now.stem}.console.full.txt"))
                for f in followers:
                    print(f"  following {f.src.name} -> {f.dest.name}", flush=True)

            head = followers[0].headroom()
            if head is not None and head < 512 * 1024:
                print(f"  [{datetime.now():%H:%M:%S}] game is {head:,} B from its own "
                      f"truncate point -- capture has it", flush=True)
            time.sleep(a.interval)
    except KeyboardInterrupt:
        print()
        for f in followers:
            print(f"  {f.dest}")
            print(f"    {f.copied:,} B captured, {f.resets} truncation(s) survived")
    return 0


if __name__ == "__main__":
    sys.exit(main())
