#!/usr/bin/env python3
"""
pzproc -- sample the game process once a minute, so "it gets worse the longer you
play" can be tested against something other than the stall log itself.

WHY THIS EXISTS
---------------
Client main-thread stalls grow monotonically across a session: median 88 ms in the
first ten minutes, 253 ms after twenty, 321 ms at sixty-three. Nothing Viewpoint
reports moves with them -- eleven counters sit at 1.00-1.13x in stall windows
against all others -- and the Java heap is flat (-1% across the session, GC cycles
median 0). So the growth is outside the JVM heap and outside Viewpoint.

The equivalent check on a second PC, the one hosting the co-op server, found no
leak: private bytes 18.4-21.0 GB with no trend, threads steady at 151-153. That
makes the same measurement on a client worth having, and comparable.

A first reading on one client after 96 minutes: 38,680 handles, 115 threads,
15,658 MB private, 7,083 MB working set. The handle count is the one that looks
like a rate rather than a level -- roughly 400/min if it started near zero -- but a
single sample cannot show a trend, which is the whole reason for this file.

WHAT IT CANNOT TELL YOU
-----------------------
Correlation with session time is not causation, and everything here grows with
session time by construction. The discriminating run is standing still for ~30
minutes: elapsed time advances while explored area does not. Collected during
continuous travel, `chunks` (+249%), `map layouts` (+315%) and `elapsed` are
perfectly collinear -- rho 1.000 -- so this harness cannot separate them and
neither can this script.

    python pzproc.py                    # sample every 60s until the game exits
    python pzproc.py --interval 30
    python pzproc.py --report <csv>     # summarise, with per-sample rates
"""

import argparse
import csv
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "captures"
GAME = "ProjectZomboid64"

FIELDS = ["clock", "elapsed_min", "threads", "handles", "private_mb",
          "workingset_mb", "virtual_mb", "gdi", "user"]

PS = r'''
$p = Get-Process ProjectZomboid64 -ErrorAction SilentlyContinue
if (-not $p) { "GONE"; exit }
$t = Add-Type -MemberDefinition '[DllImport("user32.dll")] public static extern int GetGuiResources(IntPtr h, int f);' -Name GR -PassThru
$vals = @($p.Threads.Count, $p.HandleCount,
          [int]($p.PrivateMemorySize64/1MB), [int]($p.WorkingSet64/1MB),
          [int]($p.VirtualMemorySize64/1MB),
          $t::GetGuiResources($p.Handle,0), $t::GetGuiResources($p.Handle,1))
($vals -join ',')
'''


def sample():
    """One reading, or None if the game is gone. Returns raw strings."""
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", PS],
                           capture_output=True, text=True, timeout=30)
    except Exception as e:
        print(f"  sample failed: {e}", flush=True)
        return None
    out = r.stdout.strip().splitlines()
    if not out or out[-1].strip() == "GONE":
        return None
    parts = [x.strip().replace(",", "") for x in out[-1].split(",")]
    return parts if len(parts) == 7 else None


def watch(interval):
    OUT.mkdir(exist_ok=True)
    dest = OUT / f"{datetime.now():%Y-%m-%d_%H-%M}_process.csv"
    t0 = None
    with open(dest, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(FIELDS)
        print(f"  writing {dest}", flush=True)
        while True:
            s = sample()
            if s is None:
                print("  game gone -- stopping", flush=True)
                return 0
            now = time.time()
            if t0 is None:
                t0 = now
            # PS emits threads,handles,private,ws,virtual,gdi,user -- seven fields
            row = [datetime.now().strftime("%H:%M:%S"), f"{(now - t0)/60:.1f}"] + s[:7]
            w.writerow(row)
            fh.flush()
            print("  " + "  ".join(f"{k}={v}" for k, v in zip(FIELDS, row)), flush=True)
            time.sleep(interval)


def report(path):
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    if len(rows) < 2:
        sys.exit("need at least two samples")
    a, b = rows[0], rows[-1]
    span = float(b["elapsed_min"]) - float(a["elapsed_min"])
    print(f"  {len(rows)} samples over {span:.1f} min\n")
    print(f"  {'metric':<16}{'first':>12}{'last':>12}{'change':>11}{'per min':>11}")
    for k in FIELDS[2:]:
        try:
            x, y = float(a[k]), float(b[k])
        except (ValueError, KeyError):
            continue
        ch = "-" if x == 0 else f"{(y - x) / x * 100:+.0f}%"
        print(f"  {k:<16}{x:>12.0f}{y:>12.0f}{ch:>11}{(y - x)/span if span else 0:>11.1f}")
    print("\n  A metric that grows while the stalls grow is a CANDIDATE, not a cause:")
    print("  everything here grows with session time by construction. Only the")
    print("  stand-still run separates elapsed time from explored area.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--interval", type=float, default=60.0)
    ap.add_argument("--report", metavar="CSV")
    a = ap.parse_args()
    if a.report:
        return report(a.report)
    return watch(a.interval)


if __name__ == "__main__":
    sys.exit(main())
