#!/usr/bin/env python3
"""
pzauto -- turn a finished session into a benchmark report without being asked.

WHY THIS EXISTS
---------------
Everything in this repo was already automatic except the part that produces the
answer. pztail.py captures continuously, pzbench.py can compare two runs -- but
somebody had to notice the game had closed, remember which two captures to pass,
and read the output. In practice that meant the 2026-11-04 session sat unanalysed
for five days and the 02-mainthread bench was applied 2026-11-04 and never
measured at all. The data was there the whole time.

So: watch for a session ENDING, and write the report then.

WHAT COUNTS AS ENDED
--------------------
Three conditions, all required, because any one alone is wrong:

  no game process      ProjectZomboid64.exe absent. Alone this is wrong -- it is
                       also true in the seconds before launch, and the capture
                       for a session that never started is not worth a report.
  capture gone quiet   the .full.txt has not grown for QUIET_S. Alone this is
                       wrong too -- a player can stand in a menu, and pztail
                       writes nothing while the game writes nothing.
  no report yet        reports/<stem>.txt absent, so a session is reported once.

A session shorter than MIN_SAMPLES stat lines is skipped rather than reported:
pzbench needs a settled plateau, and a two-minute log produces a confident number
from nothing. The skip is recorded so it is not retried forever.

WHAT IT COMPARES AGAINST
------------------------
The previous session that produced a report, by capture mtime. That is the
honest default -- "did anything change since last time" -- and it is also the
comparison nobody runs by hand. Pass --no-compare for a solo profile.

    python pzauto.py                 # watch, report on each session end
    python pzauto.py --once          # report any unreported finished session, exit
    python pzauto.py --report <file> # force a report for one capture
"""

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
CAPTURES = HERE / "captures"
REPORTS = HERE / "reports"
PZBENCH = HERE / "pzbench.py"
STATE = REPORTS / ".pzauto-state.json"

QUIET_S = 90          # capture must be idle this long before a session counts as over
POLL_S = 30
MIN_SAMPLES = 12      # pzbench warns below this; do not dignify it with a report
GAME_EXE = "ProjectZomboid64.exe"


def game_running():
    """True if the game process exists. Uses tasklist so it needs no extra deps."""
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {GAME_EXE}"],
                             capture_output=True, text=True, timeout=20)
        return GAME_EXE.lower() in out.stdout.lower()
    except Exception:
        # Unknowable is not the same as absent. Say "running" so we never write a
        # report mid-session on the strength of a failed query -- the same lesson
        # as an unelevated process query returning zero and being believed.
        return True


def load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {"skipped": [], "reported": []}


def save_state(st):
    REPORTS.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(st, indent=2), encoding="utf-8")


def captures():
    """Session captures, oldest first, excluding the console side-files."""
    out = [p for p in CAPTURES.glob("*_DebugLog.full.txt") if p.is_file()]
    return sorted(out, key=lambda p: p.stat().st_mtime)


def sample_count(path):
    """Stat lines pzbench will actually use."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    return text.count("gpu ms floors")


def report_path(cap):
    return REPORTS / (cap.name.replace(".full.txt", "") + ".txt")


def previous_reported(cap, st):
    """The newest capture older than `cap` that we have reported on."""
    done = set(st.get("reported", []))
    older = [p for p in captures()
             if p.stat().st_mtime < cap.stat().st_mtime and p.name in done]
    return older[-1] if older else None


def write_report(cap, st, compare=True):
    REPORTS.mkdir(exist_ok=True)
    prev = previous_reported(cap, st) if compare else None
    args = [sys.executable, "-I", str(PZBENCH)]
    args += [str(prev), str(cap)] if prev else [str(cap)]
    try:
        run = subprocess.run(args, capture_output=True, text=True, timeout=600)
    except Exception as e:
        print(f"  pzbench failed on {cap.name}: {e}", flush=True)
        return False

    dest = report_path(cap)
    header = [
        f"# {cap.name}",
        f"# written {datetime.now():%Y-%m-%d %H:%M:%S} by pzauto",
        f"# compared against {prev.name}" if prev else "# no earlier reported session to compare against",
        f"# stat lines {sample_count(cap)}",
        "",
    ]
    dest.write_text("\n".join(header) + run.stdout + ("\n" + run.stderr if run.stderr.strip() else ""),
                    encoding="utf-8")
    index_line(cap, run.stdout, prev)
    print(f"  report -> {dest}", flush=True)
    return True


def index_line(cap, out, prev):
    """One line per session in reports/INDEX.md, so a trend is visible without
    opening fifteen reports. Parsed back out of pzbench's own output rather than
    recomputed, so the index cannot disagree with the report beside it.

    Scoped to THIS capture's own profile block. pzbench prints a profile for each
    file it is given, headed by the filename, and then the delta -- so taking the
    first "main frame ms" in the whole output silently reports the BEFORE run's
    numbers under the after run's name. Caught by 2026-11-11_08-56 appearing with
    9.36 ms, which is the 11-04 session's median."""
    lines = out.splitlines()
    start, end = 0, len(lines)
    for i, line in enumerate(lines):
        if line.strip() == cap.name:
            start = i
            break
    for i in range(start + 1, len(lines)):
        # the next profile header, or the delta section, ends this block
        if lines[i].startswith("DELTA:") or lines[i].strip().endswith(".full.txt"):
            if i > start + 2:
                end = i
                break
    block = lines[start:end]

    frame = gpu = "?"
    for line in block:
        s = line.split()
        if line.strip().startswith("main frame ms") and len(s) >= 4:
            frame = s[3]
        elif line.strip().startswith("gpu ms") and len(s) >= 3:
            gpu = s[2]
    limiter = "?"
    for line in block:
        if "LIMITER:" in line:
            # Stop at a bracket OR a full stop: the capped verdict is a
            # sentence, not a one-word label, and the whole thing in a
            # table cell makes the index unreadable.
            limiter = re.split(r"[(.]", line.split("LIMITER:")[1])[0].strip()
    verdict = ""
    if "NOT A RESULT" in out:
        verdict = "no measurable change"
    elif "x the resolution, so this is a real difference" in out:
        for line in out.splitlines():
            if "the resolution, so this is a real difference" in line:
                verdict = "CHANGED " + line.strip().lstrip("-> ").split("x the")[0] + "x res"
                break

    idx = REPORTS / "INDEX.md"
    if not idx.exists():
        idx.write_text("| session | frame ms | gpu ms | limiter | vs previous |\n"
                       "|---|---|---|---|---|\n", encoding="utf-8")
    with open(idx, "a", encoding="utf-8") as f:
        f.write(f"| {cap.name.replace('_DebugLog.full.txt','')} | {frame} | {gpu} | "
                f"{limiter} | {verdict or ('vs ' + prev.name.replace('_DebugLog.full.txt','') if prev else '-')} |\n")


def sweep(st, compare=True, force_quiet=False):
    """Report on any finished, unreported session. Returns how many it wrote."""
    written = 0
    if game_running() and not force_quiet:
        return 0
    now = time.time()
    for cap in captures():
        if cap.name in st.get("reported", []) or cap.name in st.get("skipped", []):
            continue
        if report_path(cap).exists():
            st.setdefault("reported", []).append(cap.name)
            continue
        if not force_quiet and now - cap.stat().st_mtime < QUIET_S:
            continue          # still being written, or only just stopped
        n = sample_count(cap)
        if n < MIN_SAMPLES:
            print(f"  skipping {cap.name}: {n} stat lines, too short to bench", flush=True)
            st.setdefault("skipped", []).append(cap.name)
            save_state(st)
            continue
        if write_report(cap, st, compare=compare):
            st.setdefault("reported", []).append(cap.name)
            save_state(st)
            written += 1
    return written


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="one sweep, then exit")
    ap.add_argument("--report", metavar="CAPTURE", help="force a report for one capture")
    ap.add_argument("--no-compare", action="store_true", help="profile only, no delta")
    ap.add_argument("--interval", type=float, default=POLL_S, help=f"poll seconds (default {POLL_S})")
    a = ap.parse_args()

    st = load_state()
    REPORTS.mkdir(exist_ok=True)

    if a.report:
        cap = Path(a.report)
        if not cap.is_absolute():
            cap = CAPTURES / cap.name
        if not cap.exists():
            sys.exit(f"no such capture: {cap}")
        ok = write_report(cap, st, compare=not a.no_compare)
        if ok:
            st.setdefault("reported", []).append(cap.name)
            save_state(st)
        return 0 if ok else 1

    if a.once:
        n = sweep(st, compare=not a.no_compare, force_quiet=True)
        print(f"  {n} report(s) written")
        return 0

    print(f"  watching {CAPTURES}")
    print(f"  reports  {REPORTS}")
    print(f"  a session is 'over' when {GAME_EXE} is gone and its capture has been "
          f"idle {QUIET_S}s\n", flush=True)
    while True:
        try:
            sweep(st, compare=not a.no_compare)
        except Exception as e:
            print(f"  sweep error (continuing): {e}", flush=True)
        time.sleep(a.interval)


if __name__ == "__main__":
    sys.exit(main())
