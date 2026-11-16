#!/usr/bin/env python3
"""
pzmodprof -- read Mod Profiler's log lines back into a ranking of which mods' Lua took
the time, across a whole session.

    python pzmodprof.py <log> [<log> ...]          # a pztail capture, console.txt or a DebugLog
    python pzmodprof.py --top 15 <log>

WHAT IT READS
-------------
Mod Profiler is a Project Zomboid mod, kept in its own repo. It writes "[ModProf]"
lines: one per reading window, with the top mods under it and each mod's top functions
under that. It also writes a line for any HITCH (a gap between ticks, with the Lua that
ran inside it), any SLOW call, its start-up, and its calibration.

A window line ends "in N calls (M timed)" when the mod samples busy handlers. A line
from before sampling, with no "(M timed)", is read too.

The game's DebugLog adds a full stop to the end of every message, so one trailing "."
is stripped before a line is parsed.

WHAT IT CANNOT TELL YOU
-----------------------
In the default "anomaly" log mode, only unusual windows are written: a low tick rate,
Lua above a threshold, or a hitch. Totals are then over the WINDOWS THAT WERE LOGGED,
which are the bad ones. That is exactly the right sample for "what was heavy when it
went wrong", and the wrong one for "what does a normal minute cost". Set log=always in
ModProfiler.ini, or tick "Write every reading to the log" in its options, for the second.

Each window lists only its top mods and functions, so a mod that is never in a top list
does not appear here, however steady its cost. The panel in the game shows them all.
"""

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

TAG = re.compile(r"\[ModProf\] ?(.*)$")
WINDOW = re.compile(r"window ([\d.]+)s \((.+?), clock (\w+)\): (\d+) ticks \(([\d.]+)/s\), longest gap (\d+) ms, "
                    r"(\d+) hitch\(es\), Lua ([\d.]+)% = ([\d.]+) ms in (\d+) calls"
                    r"(?: \((\d+) timed\))?(?:, profiler ~([\d.]+) ms)?")
MOD = re.compile(r"mod #(\d+)\s+([\d.]+) ms\s+([\d.]+)%\s+(\d+) calls\s+max\s+([\d.]+) ms\s+(.+) \[(.+)\]$")
FN = re.compile(r"fn\s+([\d.]+) ms\s+([\d.]+)%\s+(\d+) calls\s+max\s+([\d.]+) ms\s+(\S+) (\S+):(\d+)$")
HITCH = re.compile(r"HITCH (\d+) ms between ticks; Lua in it ([\d.]+) ms(?:; slowest ([\d.]+) ms (.+))?$")
SLOW = re.compile(r"SLOW ([\d.]+) ms in (.+)$")
INIT = re.compile(r"v([\d.]+) init on (.+?): (.+?); clock (\w+); wrapping (\d+) events")
CAL = re.compile(r"calibration: ([\d.]+) us per timed call")


def parse(paths):
    s = {"windows": 0, "span": 0.0, "lua": 0.0, "overhead": 0.0, "rates": [], "calls": 0, "timed": 0,
         "sampled_windows": 0, "sides": set(), "clocks": set(),
         "mods": defaultdict(lambda: {"ms": 0.0, "calls": 0, "max": 0.0, "seen": 0, "name": ""}),
         "fns": defaultdict(lambda: {"ms": 0.0, "calls": 0, "max": 0.0, "seen": 0}), "hitches": [],
         "slow": defaultdict(list), "init": [], "cal": []}
    current_mod = None
    for path in paths:
        for raw in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
            m = TAG.search(raw)
            if not m:
                continue
            body = m.group(1).rstrip()
            if body.endswith(".") and not body.endswith(".."):
                body = body[:-1]
            if (w := WINDOW.search(body)):
                s["windows"] += 1
                s["span"] += float(w.group(1)) * 1000
                s["lua"] += float(w.group(9))
                s["calls"] += int(w.group(10))
                if w.group(11) is not None:
                    s["timed"] += int(w.group(11)); s["sampled_windows"] += 1
                s["overhead"] += float(w.group(12) or 0)
                s["rates"].append(float(w.group(5)))
                s["sides"].add(w.group(2)); s["clocks"].add(w.group(3))
                current_mod = None
            elif (r := MOD.search(body)):
                key = r.group(7)
                e = s["mods"][key]
                e["name"] = r.group(6); e["ms"] += float(r.group(2)); e["calls"] += int(r.group(4))
                e["max"] = max(e["max"], float(r.group(5))); e["seen"] += 1
                current_mod = key
            elif (f := FN.search(body)) and current_mod:
                e = s["fns"][(current_mod, f.group(5), f.group(6), int(f.group(7)))]
                e["ms"] += float(f.group(1)); e["calls"] += int(f.group(3))
                e["max"] = max(e["max"], float(f.group(4))); e["seen"] += 1
            elif (h := HITCH.search(body)):
                s["hitches"].append((int(h.group(1)), float(h.group(2)), h.group(4)))
            elif (sl := SLOW.search(body)):
                s["slow"][sl.group(2)].append(float(sl.group(1)))
            elif (i := INIT.search(body)):
                s["init"].append(f"v{i.group(1)} on {i.group(2)}, clock {i.group(4)}, {i.group(5)} events")
            elif (c := CAL.search(body)):
                s["cal"].append(float(c.group(1)))
    return s


def report(s, top):
    if not s["windows"] and not s["hitches"]:
        print("  no Mod Profiler window or hitch lines found.")
        return 1
    print(f"  Mod Profiler: {s['windows']} logged window(s) covering {s['span'] / 60000:.1f} min, "
          f"side {', '.join(sorted(s['sides'])) or '?'}, clock {', '.join(sorted(s['clocks'])) or '?'}")
    for line in s["init"][:3]:
        print(f"  init: {line}")
    if s["cal"]:
        print(f"  profiler cost: {s['cal'][-1]:.2f} us per timed call"
              + (f", ~{s['overhead']:.0f} ms over the logged windows" if s["overhead"] else ""))
    if s["windows"]:
        rates = sorted(s["rates"])
        print(f"  Lua in logged windows: {s['lua']:.0f} ms, {100 * s['lua'] / s['span']:.2f}% of their time;"
              f" tick rate min {rates[0]:.2f}/s, median {rates[len(rates) // 2]:.2f}/s")
    if s["sampled_windows"] and s["calls"]:
        print(f"  sampled: {s['timed']} of {s['calls']} calls timed ({100 * s['timed'] / s['calls']:.0f}%);"
              " totals are weighted, so they still count every call")
    span = s["span"] or 1
    mods = sorted(s["mods"].items(), key=lambda kv: -kv[1]["ms"])
    if mods:
        print(f"\n  mods, by Lua time over the logged windows (top {top}):")
        for key, e in mods[:top]:
            label = e["name"] if e["name"] == key else f"{e['name']} [{key}]"
            print(f"    {e['ms']:9.1f} ms {100 * e['ms'] / span:6.2f}%  {e['calls']:9d} calls  max {e['max']:7.1f} ms"
                  f"  in {e['seen']:3d} window(s)  {label}")
    fns = sorted(s["fns"].items(), key=lambda kv: -kv[1]["ms"])
    if fns:
        print(f"\n  functions (top {top}):")
        for (mod, event, file, line), e in fns[:top]:
            print(f"    {e['ms']:9.1f} ms {100 * e['ms'] / span:6.2f}%  {e['calls']:9d} calls  max {e['max']:7.1f} ms"
                  f"  {mod}  {event} {file}:{line}")
    if s["hitches"]:
        hs = sorted(s["hitches"], key=lambda h: -h[0])
        print(f"\n  hitches: {len(hs)}, worst {hs[0][0]} ms")
        for gap, lua, who in hs[:5]:
            share = f"{100 * lua / gap:3.0f}% Lua" if gap else ""
            print(f"    {gap:6d} ms  Lua {lua:7.1f} ms {share}" + (f"  slowest: {who}" if who else ""))
    if s["slow"]:
        print("\n  slow single calls:")
        for label, v in sorted(s["slow"].items(), key=lambda kv: -max(kv[1]))[:top]:
            print(f"    {len(v):4d}x  worst {max(v):7.1f} ms  {label}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logs", nargs="+")
    ap.add_argument("--top", type=int, default=10)
    a = ap.parse_args()
    return report(parse(a.logs), a.top)


if __name__ == "__main__":
    sys.exit(main())
