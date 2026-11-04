#!/usr/bin/env python3
"""
pzbench -- parse Project Zomboid / Viewpoint DebugLog telemetry into a comparable
per-pass cost profile, so shader-pack settings can be tuned from measurement rather
than from guesswork.

Viewpoint emits a stat line every few seconds carrying per-pass GPU and render-CPU
costs, main-thread breakdown, VRAM and heap. That is everything a benchmark needs;
this just turns it into numbers you can compare between runs.

Usage:
    python pzbench.py <DebugLog.txt> [<DebugLog.txt> ...]

Two runs are compared pairwise against the first.

Warmup is reported separately from steady state on purpose. Entering first person
builds the whole deferred pipeline and compiles shader variants -- the frame that
hung the game was a warmup frame of 2678 ms. Mixing that into a median hides both
the stall and the real frame cost.
"""

import re
import sys
import statistics
from pathlib import Path

PASSES = ["floors", "shadow", "gbuffer", "occlusion", "sky+light", "far",
          "outlines", "glass", "weather", "air", "bloom", "post+taa"]

# "gpu ms floors 0.04 shadow 0.73 ... post+taa 0.68 (5.90)"
def _pass_block(label):
    body = r"[\s\S]*?".join(
        re.escape(p) + r"\s+(-?[\d.]+)" for p in PASSES)
    return re.compile(re.escape(label) + r"\s+" + body + r"\s*\((-?[\d.]+)\)")

RE_GPU = _pass_block("gpu ms")
RE_CPU = _pass_block("render cpu ms")
RE_FRAME = re.compile(r"main thread frame ([\d.]+) ms \((\d+) fps\)")
RE_MAIN = re.compile(r"main ms cache ([\d.]+) far ([\d.]+) snapshot ([\d.]+)")
RE_VRAM = re.compile(r"video memory MiB free (\d+) of (\d+), evictions (\d+) \((\d+) MiB\)")
RE_HEAP = re.compile(r"heap used (\d+) of (\d+)")
RE_COMMIT = re.compile(r"process MiB committed (\d+) \(peak (\d+)\)")
RE_SLOWMAIN = re.compile(r"slow frame on the main thread: ([\d.]+) ms")
RE_SLOWRENDER = re.compile(r"slow frame on the render thread: ([\d.]+) ms")
RE_DRAWS = re.compile(r"draws/frame (\d+)")
RE_REBUILDS = re.compile(r"rebuilds/frame ([\d.]+) meshes (\d+)")


def parse(path):
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    samples = []
    for line in text.splitlines():
        if "gpu ms floors" not in line:
            continue
        g, c = RE_GPU.search(line), RE_CPU.search(line)
        frame = RE_FRAME.search(line)
        if not (g and c and frame):
            continue
        s = {
            "gpu": {p: float(v) for p, v in zip(PASSES, g.groups()[:-1])},
            "gpu_total": float(g.groups()[-1]),
            "cpu": {p: float(v) for p, v in zip(PASSES, c.groups()[:-1])},
            "cpu_total": float(c.groups()[-1]),
            "frame_ms": float(frame.group(1)),
            "fps": int(frame.group(2)),
        }
        for rx, keys in ((RE_MAIN, ("main_cache", "main_far", "main_snapshot")),
                         (RE_VRAM, ("vram_free", "vram_total", "evictions", "evicted_mib")),
                         (RE_HEAP, ("heap_used", "heap_cap")),
                         (RE_COMMIT, ("committed", "committed_peak")),
                         (RE_DRAWS, ("draws",)),
                         (RE_REBUILDS, ("rebuilds", "meshes"))):
            m = rx.search(line)
            if m:
                for k, v in zip(keys, m.groups()):
                    s[k] = float(v)
        samples.append(s)

    slow_main = [float(m) for m in RE_SLOWMAIN.findall(text)]
    slow_render = [float(m) for m in RE_SLOWRENDER.findall(text)]
    return samples, slow_main, slow_render


def split_warmup(samples):
    """Steady state = samples after the pipeline settles. A 0-fps sample, or one
    whose render CPU is an order of magnitude over the median, is construction."""
    if not samples:
        return [], []
    cpu = sorted(s["cpu_total"] for s in samples)
    med = cpu[len(cpu) // 2]
    warm, steady = [], []
    for s in samples:
        # fps 0 or gpu_total 0 means the frame never completed -- the GPU timers
        # report nothing, so the sample is a stall, not a measurement.
        stalled = s["fps"] == 0 or s["gpu_total"] <= 0.0
        if stalled or s["cpu_total"] > max(10 * med, 100.0):
            warm.append(s)
        else:
            steady.append(s)
    return warm, steady


def stat(vals):
    if not vals:
        return (0.0, 0.0, 0.0)
    v = sorted(vals)
    return (statistics.median(v), v[int(len(v) * 0.95) - 1] if len(v) > 1 else v[0], max(v))


def profile(path):
    samples, slow_main, slow_render = parse(path)
    warm, steady = split_warmup(samples)
    return {
        "path": Path(path).name,
        "samples": len(samples), "warm": len(warm), "steady": len(steady),
        "steady": steady, "warmup": warm,
        "slow_main": slow_main, "slow_render": slow_render,
    }


def report(p):
    steady, warm = p["steady"], p["warmup"]
    print("=" * 78)
    print(p["path"])
    print("=" * 78)
    print(f"  stat samples {p['samples']}  (steady {len(steady)}, pipeline-build {len(warm)})")
    if not steady:
        print("  no steady-state samples -- the run never settled.")
        return

    fmed, f95, fmax = stat([s["frame_ms"] for s in steady])
    gmed, g95, gmax = stat([s["gpu_total"] for s in steady])
    print()
    print(f"  {'':<14}{'median':>10}{'p95':>10}{'worst':>10}")
    fps_main = f"-> {1000/fmed:.0f} fps median" if fmed > 0 else ""
    fps_gpu = f"-> {1000/gmed:.0f} fps if GPU-bound" if gmed > 0 else "-> no GPU timings"
    print(f"  {'main frame ms':<14}{fmed:>10.2f}{f95:>10.2f}{fmax:>10.2f}   {fps_main}")
    print(f"  {'gpu ms':<14}{gmed:>10.2f}{g95:>10.2f}{gmax:>10.2f}   {fps_gpu}")
    if gmed > 0:
        limiter = "MAIN THREAD" if fmed > gmed else "GPU"
        print(f"\n  LIMITER: {limiter}   (headroom on the other side: {abs(fmed-gmed):.2f} ms)")
        if limiter == "MAIN THREAD":
            print("  -> GPU-side quality cuts buy no fps here. Spend the headroom instead.")

    print(f"\n  per-pass GPU ms (median), most expensive first:")
    tot = sum(statistics.median([s["gpu"][x] for s in steady]) for x in PASSES) or 1.0
    rows = sorted(((x, statistics.median([s["gpu"][x] for s in steady])) for x in PASSES),
                  key=lambda r: -r[1])
    for name, v in rows:
        bar = "#" * int(round(v / max(rows[0][1], 1e-9) * 34))
        print(f"    {name:<11}{v:>7.2f}  {v/tot*100:>5.1f}%  {bar}")

    cpu_rows = sorted(((x, statistics.median([s["cpu"][x] for s in steady])) for x in PASSES),
                      key=lambda r: -r[1])
    print(f"\n  per-pass render-CPU ms (median), top 5:")
    for name, v in cpu_rows[:5]:
        print(f"    {name:<11}{v:>7.2f}")

    if any("main_cache" in s for s in steady):
        mc = statistics.median([s.get("main_cache", 0) for s in steady])
        mf = statistics.median([s.get("main_far", 0) for s in steady])
        ms = statistics.median([s.get("main_snapshot", 0) for s in steady])
        print(f"\n  main-thread breakdown ms: chunk cache {mc:.2f}  far world {mf:.2f}  snapshot {ms:.2f}")

    last = steady[-1]
    if "vram_total" in last:
        used = last["vram_total"] - last["vram_free"]
        ev = max(s.get("evictions", 0) for s in steady)
        evm = max(s.get("evicted_mib", 0) for s in steady)
        print(f"\n  VRAM {used:.0f} / {last['vram_total']:.0f} MiB used"
              f"   evictions {ev:.0f} ({evm:.0f} MiB)")
    if "heap_cap" in last:
        print(f"  heap {last['heap_used']:.0f} / {last['heap_cap']:.0f} MiB"
              f"   process committed {last.get('committed', 0):.0f} MiB")

    if warm:
        w = max(s["cpu_total"] for s in warm)
        print(f"\n  PIPELINE BUILD: {len(warm)} sample(s), worst render CPU {w:.0f} ms")
    if p["slow_main"]:
        print(f"  slow main-thread frames logged: {len(p['slow_main'])}"
              f"  worst {max(p['slow_main']):.1f} ms")
    if p["slow_render"]:
        print(f"  slow render-thread frames logged: {len(p['slow_render'])}"
              f"  worst {max(p['slow_render']):.1f} ms")
    # A frame over ~5000 ms of unresponsiveness is what Windows closes as AppHangB1.
    worst = max(p["slow_main"] + p["slow_render"] + [0])
    if worst > 2000:
        print(f"  !! {worst:.0f} ms stall -- Windows closes a window unresponsive for ~5 s")


def compare(a, b):
    sa, sb = a["steady"], b["steady"]
    if not (sa and sb):
        return
    print("\n" + "=" * 78)
    print(f"DELTA: {b['path']}  vs  {a['path']}")
    print("=" * 78)

    def med(s, f):
        return statistics.median([f(x) for x in s])

    print(f"  {'':<14}{'before':>10}{'after':>10}{'change':>12}")
    for label, fn in (("main frame ms", lambda x: x["frame_ms"]),
                      ("gpu ms", lambda x: x["gpu_total"]),
                      ("fps", lambda x: x["fps"])):
        va, vb = med(sa, fn), med(sb, fn)
        pct = (vb - va) / va * 100 if va else 0
        print(f"  {label:<14}{va:>10.2f}{vb:>10.2f}{pct:>11.1f}%")

    print(f"\n  per-pass GPU ms change (biggest saving first):")
    rows = []
    for x in PASSES:
        va, vb = med(sa, lambda s: s["gpu"][x]), med(sb, lambda s: s["gpu"][x])
        rows.append((x, va, vb, vb - va))
    for name, va, vb, d in sorted(rows, key=lambda r: r[3]):
        if abs(d) < 0.005:
            continue
        print(f"    {name:<11}{va:>7.2f} -> {vb:>6.2f}   {d:+7.2f}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    profiles = [profile(p) for p in sys.argv[1:]]
    for p in profiles:
        report(p)
        print()
    for p in profiles[1:]:
        compare(profiles[0], p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
