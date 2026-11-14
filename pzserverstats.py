# /// script
# requires-python = ">=3.11"
# dependencies = ["psutil>=5.9"]
# ///
"""pzserverstats -- once a minute, record Project Zomboid server + client process health.

Writes captures/serverstats_<date>.csv (one row per minute per process found):
  time, role (server|client), pid, cpu_pct (of one core, averaged over the minute),
  rss_mib, private_mib, threads, and for the server JVM (via jcmd, read-only attach):
  heap_used_mib, heap_committed_mib, gc_young_count, gc_young_ms, gc_old_count, gc_old_ms,
  safepoint_ms.

Run:  uv run --script pzserverstats.py          (uvw for no console window)
Nothing is written while the game is not running.
"""
import csv
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

import psutil

HERE = Path(__file__).resolve().parent
CAPTURES = HERE / "captures"
INTERVAL = 60
JCMD = None
for cand in sorted(Path(r"C:\Program Files\Eclipse Adoptium").glob("jdk-*/bin/jcmd.exe"), reverse=True):
    JCMD = str(cand)
    break

FIELDS = ["time", "role", "pid", "cpu_pct", "rss_mib", "private_mib", "threads",
          "heap_used_mib", "heap_committed_mib", "gc_young_count", "gc_young_ms",
          "gc_old_count", "gc_old_ms", "safepoint_ms", "safepoints", "safepoint_sync_ms"]

NOWIN = 0x08000000  # CREATE_NO_WINDOW


def find():
    out = {}
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            name = (p.info["name"] or "").lower()
            cmd = " ".join(p.info["cmdline"] or [])
        except (psutil.Error, OSError):
            continue
        if name == "java.exe" and "zombie.network.GameServer" in cmd:
            out["server"] = p
        elif name == "projectzomboid64.exe":
            out["client"] = p
    return out


def jcmd(pid, *args):
    if not JCMD:
        return ""
    try:
        r = subprocess.run([JCMD, str(pid), *args], capture_output=True, text=True,
                           timeout=20, creationflags=NOWIN)
        return r.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def jvm_stats(pid):
    s = {}
    heap = jcmd(pid, "GC.heap_info")
    # G1: "garbage-first heap   total reserved 16777216K, committed 16777216K, used 6691934K"
    m = re.search(r"committed (\d+)K, used (\d+)K", heap) or re.search(r"total (\d+)K, used (\d+)K", heap)
    if m:
        s["heap_committed_mib"] = round(int(m.group(1)) / 1024)
        s["heap_used_mib"] = round(int(m.group(2)) / 1024)
    perf = jcmd(pid, "PerfCounter.print")
    freq = re.search(r"sun\.os\.hrt\.frequency=(\d+)", perf)
    f = int(freq.group(1)) if freq else 0

    def counter(name):
        mm = re.search(re.escape(name) + r"=(\d+)", perf)
        return int(mm.group(1)) if mm else None

    def ms(ticks):
        return round(ticks * 1000 / f, 1) if (ticks is not None and f) else None

    s["gc_young_count"] = counter("sun.gc.collector.0.invocations")
    s["gc_young_ms"] = ms(counter("sun.gc.collector.0.time"))
    s["gc_old_count"] = counter("sun.gc.collector.1.invocations")
    s["gc_old_ms"] = ms(counter("sun.gc.collector.1.time"))
    s["safepoint_ms"] = ms(counter("sun.rt.safepointTime"))
    s["safepoints"] = counter("sun.rt.safepoints")
    s["safepoint_sync_ms"] = ms(counter("sun.rt.safepointSyncTime"))
    return s


def main():
    CAPTURES.mkdir(exist_ok=True)
    last = {}  # pid -> (cpu seconds, wall time)
    while True:
        procs = find()
        if procs:
            path = CAPTURES / f"serverstats_{datetime.now():%Y-%m-%d}.csv"
            new = not path.exists()
            with open(path, "a", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=FIELDS)
                if new:
                    w.writeheader()
                for role, p in procs.items():
                    try:
                        t = p.cpu_times()
                        cpu_s = t.user + t.system
                        mem = p.memory_info()
                        row = {"time": f"{datetime.now():%Y-%m-%d %H:%M:%S}", "role": role,
                               "pid": p.pid, "rss_mib": round(mem.rss / 2**20),
                               "private_mib": round(getattr(mem, "private", 0) / 2**20),
                               "threads": p.num_threads()}
                    except (psutil.Error, OSError):
                        continue
                    now = time.monotonic()
                    if p.pid in last:
                        c0, t0 = last[p.pid]
                        row["cpu_pct"] = round(100 * (cpu_s - c0) / max(now - t0, 1e-6))
                    last[p.pid] = (cpu_s, now)
                    if role == "server":
                        row.update(jvm_stats(p.pid))
                    w.writerow(row)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
