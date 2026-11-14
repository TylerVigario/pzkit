#!/usr/bin/env python3
"""
pzbench -- parse Project Zomboid / Viewpoint DebugLog telemetry into a comparable
per-pass cost profile, so shader-pack settings can be tuned from measurement rather
than from guesswork.

Viewpoint emits a stat line every few seconds carrying per-pass GPU and render-CPU
costs, main-thread breakdown, VRAM and heap. That is everything a benchmark needs;
this just turns it into numbers you can compare between runs.

Usage:
    python pzbench.py [--from-load] <DebugLog.txt> [<DebugLog.txt> ...]

Two runs are compared pairwise against the first. A pztail capture, a raw DebugLog
and console.txt all work: Viewpoint writes the same lines to each.

Warmup is reported separately from steady state on purpose. Entering first person
builds the whole deferred pipeline and compiles shader variants -- the frame that
hung the game was a warmup frame of 2678 ms. Mixing that into a median hides both
the stall and the real frame cost.

HITCHES are reported from Viewpoint's own "slow frame" lines, by attribution and
not by median. Each line splits the frame into `ours` (Viewpoint's own work) and
`the rest` (on the main thread the game's update and the collector's pauses; on the
render thread the game's own drawing, or waiting for the main thread). That split
is what separates the two hitch classes in README.md: a large `ours` is the mod's
work and its settings are the lever, a small `ours` with a large rest is not
Viewpoint at all. --from-load drops everything before the first exit from
GameLoadingState, so loading stalls and startup errors stay out of it.
Script exceptions are split at that same point and listed by source. Startup is
dominated by noise -- a mod without an optional folder logs one visitFileFailed per
missing path, 238 of 242 in one session -- so the source list is what to read, not
the count.
"""

import re
import sys
import statistics
from collections import Counter
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

# "slow frame on the main thread: 626.2 ms, ours 538.7 (chunk cache 56.4: 15 builds;
#  ...; by part: chunk cache 55.9, lights 290.6, ...), the rest 87.5 (...)"
RE_HITCH = {
    "main": re.compile(r"slow frame on the main thread: ([\d.]+) ms, ours ([\d.]+) "
                       r"\(([^()\n]*)\), the rest ([\d.]+)"),
    # "slow frame on the render thread: 65.1 ms, our passes 27.5 (floors 4.9 shadow
    #  14.5 ...), the rest 37.6 (...)"
    "render": re.compile(r"slow frame on the render thread: ([\d.]+) ms, our passes ([\d.]+) "
                         r"\(([^()\n]*)\), the rest ([\d.]+)"),
}
RE_HITCH_TOTAL = {"main": RE_SLOWMAIN, "render": RE_SLOWRENDER}
RE_EXCEPTION = re.compile(r"> ([^>\n]+?)> Exception thrown")
RE_CLOCK = re.compile(r" st:([\d,]+)>")
LOADED = "STATE: exit zombie.gameStates.GameLoadingState"


def _hitch_parts(thread, inner):
    """Named parts of one slow frame, as {name: ms}."""
    if thread == "render":
        return {k: float(v) for k, v in re.findall(r"([A-Za-z+]+)\s+([\d.]+)", inner)}
    i = inner.find("by part:")
    out = {}
    for item in (inner[i + len("by part:"):].split(",") if i >= 0 else []):
        m = re.match(r"\s*(.+?)\s+([\d.]+)\s*$", item)   # "none over 1 ms" never matches
        if m:
            out[m.group(1)] = float(m.group(2))
    return out


def parse_hitches(text):
    """Every slow frame, with Viewpoint's split of it where the line carries one."""
    slow = {"main": [], "render": []}
    for line in text.splitlines():
        if "slow frame on the" not in line:
            continue
        for thread in slow:
            m = RE_HITCH[thread].search(line)
            if m:
                slow[thread].append({"total": float(m.group(1)), "ours": float(m.group(2)),
                                     "rest": float(m.group(4)),
                                     "parts": _hitch_parts(thread, m.group(3))})
                break
            m = RE_HITCH_TOTAL[thread].search(line)
            if m:   # a line in some other shape: count it, without a split
                slow[thread].append({"total": float(m.group(1)), "ours": None,
                                     "rest": None, "parts": {}})
                break
    return slow


def parse(path, from_load=False):
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    loaded_at = text.find(LOADED)
    if from_load and loaded_at > 0:
        text = text[loaded_at:]
        loaded_at = 0
    if loaded_at >= 0:
        exceptions = {"startup": Counter(RE_EXCEPTION.findall(text[:loaded_at])),
                      "in game": Counter(RE_EXCEPTION.findall(text[loaded_at:]))}
    else:
        exceptions = {"whole log, which never finished loading": Counter(RE_EXCEPTION.findall(text))}
    # st: is a millisecond clock with an arbitrary origin, and 0 until it is set:
    # over the 2026-11-11 13:47 capture its change equalled the wall-clock change
    # to the second (9898 s), while its first value was 6h25m. So only a
    # difference of nonzero readings means anything.
    clock = [c for c in (int(x.replace(",", "")) for x in RE_CLOCK.findall(text)) if c > 0]
    span_s = (clock[-1] - clock[0]) / 1000 if len(clock) > 1 else 0
    extra = {"hitches": parse_hitches(text), "exceptions": exceptions,
             "span_s": span_s, "loaded": loaded_at >= 0}
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

    slow_main = [h["total"] for h in extra["hitches"]["main"]]
    slow_render = [h["total"] for h in extra["hitches"]["render"]]
    return samples, slow_main, slow_render, extra


def split_warmup(samples):
    """Split a run into pipeline-build, warmup ramp, and steady state.

    There are TWO warmup effects and conflating them wrecked the first round of
    measurement here. Measured 2026-11-04 on two identical 2-minute runs:

      build  one sample, render CPU 3294 ms -- the deferred pipeline being
             constructed on entry to first person. Obvious, already caught.
      ramp   the NEXT ~90 seconds. Frame time falls monotonically
             8.37 -> 8.05 -> 7.69 -> 7.49 -> 7.38 -> 7.22 and then plateaus,
             with render CPU falling 13.5 -> 5.6 -> 5.4 -> 5.3 alongside it.
             This is NOT noise, it is a settling curve, and it was inflating
             the reported p10-p90 band from 0.17 ms to 1.16 ms -- an eightfold
             overstatement of the noise floor, which made real changes look
             unmeasurable.

    So: drop the build samples, then drop leading samples until frame time
    stops sitting above the plateau. The plateau is taken from the back half of
    what is left, which is the part that is definitionally settled.
    """
    if not samples:
        return [], [], []
    cpu = sorted(s["cpu_total"] for s in samples)
    med = cpu[len(cpu) // 2]
    build, rest = [], []
    for s in samples:
        # fps 0 or gpu_total 0 means the frame never completed -- the GPU timers
        # report nothing, so the sample is a stall, not a measurement.
        stalled = s["fps"] == 0 or s["gpu_total"] <= 0.0
        if stalled or s["cpu_total"] > max(10 * med, 100.0):
            build.append(s)
        else:
            rest.append(s)

    if len(rest) < 6:
        return build, [], rest  # too short to tell a ramp from the plateau

    half = len(rest) // 2
    ceiling = pct([s["frame_ms"] for s in rest[half:]], 0.90)

    # Cut to the LAST sample in the first half that is still above the plateau,
    # not the first one that dips below it. A settling run is not monotone -- it
    # bounces on the way down -- and stopping at the first dip leaves most of
    # the ramp in place. That asymmetry measured two identical runs over
    # different windows and manufactured a 0.15 ms difference between them.
    cut = 0
    for i in range(half):
        if rest[i]["frame_ms"] > ceiling:
            cut = i + 1

    # A ramp has to actually DESCEND. Without this guard the rule happily
    # trimmed 19 of 39 samples off a mid-session play log whose leading samples
    # were simply a busier scene -- a prefix running 8.33 -> 12.78 ms, trimmed
    # as "warmup". That is not settling, it is cherry-picking the quiet half,
    # and it moved the reported median by 0.5 ms in the flattering direction.
    if cut > 1 and rest[0]["frame_ms"] <= rest[cut - 1]["frame_ms"]:
        cut = 0
    return build, rest[:cut], rest[cut:]


def pct(vals, p):
    if not vals:
        return 0.0
    v = sorted(vals)
    i = min(len(v) - 1, max(0, int(round(p * (len(v) - 1)))))
    return v[i]


def stat(vals):
    if not vals:
        return (0.0, 0.0, 0.0)
    v = sorted(vals)
    return (statistics.median(v), v[int(len(v) * 0.95) - 1] if len(v) > 1 else v[0], max(v))


def spread(samples):
    """The p10-p90 band of main frame time, in ms -- how much one SAMPLE varies.

    This is not the resolving power of the benchmark. See resolution(): the
    comparison is between medians, and a median of n samples is far steadier
    than a single sample. Comparing a median delta against this band was the
    original mistake here and it declared a real 0.5 ms change unmeasurable."""
    f = [s["frame_ms"] for s in samples]
    return pct(f, 0.10), pct(f, 0.90)


def resolution(samples):
    """Smallest median shift this run can actually resolve, in ms.

    band/sqrt(n), which is roughly a 2-sigma threshold on the median:
    p10-p90 spans ~2.56 sigma, and the standard error of a median is
    ~1.25*sigma/sqrt(n), so band/sqrt(n) ~= 2.05 * SE. Deliberately the
    conservative side of exact.

    Checked against reality: two identical controlled runs, n=6 each, bands
    0.17 and 0.45 ms -> resolution 0.18 ms. Their medians came out 7.22 and
    7.24 ms, 0.02 ms apart. The threshold holds, and it puts a 0.5 ms change
    comfortably inside reach."""
    lo, hi = spread(samples)
    n = max(len(samples), 1)
    return (hi - lo) / (n ** 0.5)


def cap(samples):
    """Detect a frame-rate ceiling, returning (floor_ms, share_on_it, hz) or None.

    A capped run is not a distribution with a low tail -- it has a HARD FLOOR
    that many samples sit exactly on and none go below. Measured 2026-11-11 over
    three sessions at 165 Hz with vsync on: minimum frame time was 6.06 ms in all
    three, to the hundredth, with 38-70% of samples inside a 0.1 ms bucket at the
    floor and not one sample underneath it.

    It matters because every frame-time number is then reading a clamp. The median
    IS the cap, so comparing medians has no power; and the LIMITER verdict reads
    MAIN THREAD because the main thread is the one that blocks on the present --
    about 4 of the 6.07 ms are spent waiting rather than working. Three sessions
    were compared on their medians before anyone noticed."""
    f = sorted(s["frame_ms"] for s in samples)
    if len(f) < 8:
        return None
    floor = f[0]
    if floor <= 0:
        return None
    on_floor = sum(1 for x in f if x <= floor * 1.004)   # a single 0.4% bucket
    share = on_floor / len(f)
    ratio = statistics.median(f) / floor
    # What this detects is narrower than "a cap exists": it is "THE CAP IS THE
    # LIMITER", which is exactly the condition under which a median comparison
    # is void. A capped run that spends most of its time missing the cap fails
    # these tests and should -- its median is then a real measurement of load.
    #
    # Thresholds measured against five sessions rather than chosen. The first
    # cut used share >= 0.10 and ratio <= 1.10 and produced a FALSE POSITIVE on
    # the 13-47 run, calling a 4.55 ms fast tail a "220 Hz cap":
    #
    #   session   floor   on floor   median/floor   capped?
    #   11-04     7.18       0%          1.30       no
    #   08-56     6.06      47.3%        1.016      yes, 165 Hz
    #   10-31     6.06      69.8%        1.007      yes, 165 Hz
    #   11-09     6.06      37.6%        1.040      yes, 165 Hz
    #   13-47     4.55      15.2%        1.084      NO -- uncapped, 1.78 ms band
    #
    # A real cap piles the distribution against the floor: a third or more of
    # samples on it and a median within 5% of it. Both margins are wide here
    # (37.6% vs 15.2%, 1.040 vs 1.084). With a hundredth-of-a-millisecond log
    # and a few dozen samples, some will land on the minimum by chance, so the
    # minimum being hit more than once proves nothing on its own.
    if share >= 0.30 and ratio <= 1.05:
        return floor, share, 1000.0 / floor
    return None


def miss(samples, floor_ms, factor):
    """Share of frames (%) that came in above floor_ms * factor.

    What to compare on when a run is capped. The median cannot move; this can,
    and does. Across the three capped sessions the medians spanned 0.20 ms while
    the share over 1.07x the cap ran 5.8% / 30.5% / 36.6%."""
    f = [s["frame_ms"] for s in samples]
    if not f:
        return 0.0
    return 100.0 * sum(1 for x in f if x > floor_ms * factor) / len(f)


def profile(path, from_load=False):
    samples, slow_main, slow_render, extra = parse(path, from_load)
    build, ramp, steady = split_warmup(samples)
    return {
        "path": Path(path).name,
        "samples": len(samples),
        "steady": steady, "warmup": build, "ramp": ramp,
        "slow_main": slow_main, "slow_render": slow_render,
        "from_load": from_load, **extra,
    }


def report(p):
    steady, warm, ramp = p["steady"], p["warmup"], p["ramp"]
    print("=" * 78)
    print(p["path"])
    print("=" * 78)
    print(f"  stat samples {p['samples']}  (steady {len(steady)}, "
          f"warmup ramp {len(ramp)}, pipeline-build {len(warm)})")
    if not steady:
        print("  no steady-state samples -- the run never settled.")
        return
    if ramp:
        shape = (f"{ramp[0]['frame_ms']:.2f} -> {ramp[-1]['frame_ms']:.2f} ms"
                 if len(ramp) > 1 else f"{ramp[0]['frame_ms']:.2f} ms")
        print(f"  discarded a {len(ramp) * 10}s warmup ramp ({shape})."
              f"  Measure AFTER it, not through it.")
    if len(steady) < 12:
        print(f"  !! only {len(steady)} settled samples (~{len(steady) * 10}s). Stand still")
        print("     longer -- resolving power improves as sqrt(n).")

    fmed, f95, fmax = stat([s["frame_ms"] for s in steady])
    gmed, g95, gmax = stat([s["gpu_total"] for s in steady])
    print()
    print(f"  {'':<14}{'median':>10}{'p95':>10}{'worst':>10}")
    fps_main = f"-> {1000/fmed:.0f} fps median" if fmed > 0 else ""
    fps_gpu = f"-> {1000/gmed:.0f} fps if GPU-bound" if gmed > 0 else "-> no GPU timings"
    print(f"  {'main frame ms':<14}{fmed:>10.2f}{f95:>10.2f}{fmax:>10.2f}   {fps_main}")
    print(f"  {'gpu ms':<14}{gmed:>10.2f}{g95:>10.2f}{gmax:>10.2f}   {fps_gpu}")
    lo, hi = spread(steady)
    band = hi - lo
    res = resolution(steady)
    fps_lo, fps_hi = (1000 / hi if hi else 0), (1000 / lo if lo else 0)
    print(f"\n  sample spread: p10-p90 frame time {lo:.2f}-{hi:.2f} ms"
          f"  ({band:.2f} ms band, {fps_lo:.0f}-{fps_hi:.0f} fps)")
    print(f"  RESOLUTION: {res:.2f} ms  -- the smallest median shift n={len(steady)} can show")

    ceiling = cap(steady)
    if ceiling:
        floor_ms, share, hz = ceiling
        print(f"\n  !! CAPPED RUN. Hard floor at {floor_ms:.2f} ms ({hz:.0f} Hz): "
              f"{share*100:.0f}% of samples sit on it, none below.")
        print("     The frame-time median above is that ceiling, not a measurement, and")
        print("     comparing it against another run tests nothing. To measure frame time:")
        print("     python pzconfig.py --bench      (vsync off, frame cap off)")
        print("     What still carries signal while capped:")
        print(f"       off the cap       {miss(steady, floor_ms, 1.07):>5.1f}%"
              f" of frames over {floor_ms*1.07:.2f} ms")
        print(f"       missed outright   {miss(steady, floor_ms, 1.50):>5.1f}%"
              f" of frames over {floor_ms*1.50:.2f} ms")
        print("     Per-pass GPU ms are GPU timer queries and stay valid under a cap. So do")
        print("     eviction counts, the heap, and the slow-frame log.")

    if gmed > 0:
        limiter = "MAIN THREAD" if fmed > gmed else "GPU"
        head = abs(fmed - gmed)
        if ceiling and limiter == "MAIN THREAD":
            # Not main-thread WORK: the main thread is just what blocks on the swap.
            print(f"\n  LIMITER: THE PRESENT. Frame time is pinned at the {ceiling[0]:.2f} ms cap")
            print("     and the main thread is merely the thread that waits for it.")
            print(f"  -> the GPU median of {gmed:.2f} ms is {head:.2f} ms under the cap. That gap is")
            print("     real and spendable, but it is a LOWER BOUND on the headroom rather than")
            print("     the headroom itself -- uncap to find out how much there actually is.")
        else:
            print(f"  LIMITER: {limiter}   (headroom on the other side: {head:.2f} ms)")
            if limiter == "MAIN THREAD":
                print("  -> GPU-side quality cuts buy no fps here. Spend the headroom instead.")
            if head < res:
                print(f"  !! the {head:.2f} ms headroom is below the {res:.2f} ms resolution.")
                print("     Nothing on the GPU side is worth benching -- judge it by eye.")
            else:
                print(f"  -> headroom is {head / res:.1f}x the resolution, so spending it is measurable.")

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
        # Viewpoint's eviction count is per stat window, not a running total: it
        # falls as well as rises. Runs of equal values (2,2,2,2,2) suggest windows
        # longer than the sample interval, so a sum would count one event several
        # times. Neither the sum nor the max is a session total; say which this is.
        ev = max(s.get("evictions", 0) for s in steady)
        evm = max(s.get("evicted_mib", 0) for s in steady)
        print(f"\n  VRAM {used:.0f} / {last['vram_total']:.0f} MiB used"
              f"   evictions: at most {ev:.0f} in one stat window ({evm:.0f} MiB)")
    if "heap_cap" in last:
        print(f"  heap {last['heap_used']:.0f} / {last['heap_cap']:.0f} MiB"
              f"   process committed {last.get('committed', 0):.0f} MiB")

    if warm:
        w = max(s["cpu_total"] for s in warm)
        print(f"\n  PIPELINE BUILD: {len(warm)} sample(s), worst render CPU {w:.0f} ms")


BUCKETS = ((100, "50-100"), (250, "100-250"), (500, "250-500"), (float("inf"), "500+"))
REST = {"main": "the game's update and the collector",
        "render": "the game's own drawing, or waiting on the main thread"}


def report_hitches(p):
    """Slow frames by attribution, and script exceptions by phase. Printed even
    when the run never settled: a session that only stutters is still worth reading."""
    hitches, exc = p["hitches"], p["exceptions"]
    if not (hitches["main"] or hitches["render"] or any(exc.values())):
        return
    where = "from the end of loading" if p["from_load"] and p["loaded"] else "whole log"
    span = f", over {p['span_s'] / 60:.0f} min" if p["span_s"] else ""
    print(f"\n  HITCHES ({where}{span})")
    for thread in ("main", "render"):
        frames = hitches[thread]
        if not frames:
            print(f"  slow {thread}-thread frames: none")
            continue
        totals = [h["total"] for h in frames]
        b = Counter(next(label for lim, label in BUCKETS if t < lim) for t in totals)
        rate = f", one every {p['span_s'] / len(frames):.0f} s" if p["span_s"] else ""
        print(f"  slow {thread}-thread frames: {len(frames)}{rate}, worst {max(totals):.0f} ms   "
              + "  ".join(f"[{label} ms] {b[label]}" for _, label in BUCKETS if b[label]))
        split = [h for h in frames if h["ours"] is not None]
        if split:
            ours_big = sum(1 for h in split if h["ours"] >= h["rest"])
            print(f"    Viewpoint's own work is the larger part of {ours_big}, the rest "
                  f"({REST[thread]}) of {len(split) - ours_big}")
            print(f"    median: ours {statistics.median(h['ours'] for h in split):.1f} ms, "
                  f"the rest {statistics.median(h['rest'] for h in split):.1f} ms")
        parts, named = Counter(), Counter()
        for h in frames:
            for k, v in h["parts"].items():
                parts[k] += v
                named[k] += 1
        if parts:
            print("    Viewpoint's parts, total ms over these frames: "
                  + ", ".join(f"{k} {v:.0f} ({named[k]}x)" for k, v in parts.most_common(6)))
    # A frame over ~5000 ms of unresponsiveness is what Windows closes as AppHangB1.
    worst = max(p["slow_main"] + p["slow_render"] + [0])
    if worst > 2000:
        print(f"  !! {worst:.0f} ms stall -- Windows closes a window unresponsive for ~5 s")

    counts = {phase: sum(c.values()) for phase, c in exc.items()}
    if any(counts.values()):
        print("  script exceptions: " + ", ".join(f"{phase} {n}" for phase, n in counts.items()))
        for phase, c in exc.items():
            if c:
                print(f"    {phase}: " + ", ".join(f"{src} {n}" for src, n in c.most_common(5))
                      + (f", and {len(c) - 5} more sources" if len(c) > 5 else ""))


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
        delta = (vb - va) / va * 100 if va else 0
        print(f"  {label:<14}{va:>10.2f}{vb:>10.2f}{delta:>11.1f}%")

    # The comparison is between MEDIANS, so the threshold is the resolution of
    # the weaker run, not its sample spread. Using the spread here was wrong and
    # too strict by a factor of sqrt(n) -- see resolution().
    ca, cb = cap(sa), cap(sb)
    if ca or cb:
        print("\n  !! CAPPED. The frame-time medians above are ceilings, so a comparison")
        print("     between them tests nothing -- whatever changed, the cap did not move.")
        for label, s, c in (("before", sa, ca), ("after", sb, cb)):
            if c:
                print(f"       {label:<7} cap {c[0]:.2f} ms   off the cap {miss(s, c[0], 1.07):>5.1f}%"
                      f"   missed outright {miss(s, c[0], 1.50):>5.1f}%")
            else:
                print(f"       {label:<7} not capped")
        if ca and cb:
            d = miss(sb, cb[0], 1.07) - miss(sa, ca[0], 1.07)
            print(f"     off-the-cap share moved {d:+.1f} percentage points.")
        print("     Trust that only if both runs saw the same scene under the same load: it")
        print("     is a tail statistic, and the tail is where load lives. For a clean")
        print("     frame-time comparison, uncap both runs -- python pzconfig.py --bench")
        print("     The per-pass GPU table below is unaffected and is the better read here.")

    res = max(resolution(sa), resolution(sb))
    moved = abs(med(sb, lambda x: x["frame_ms"]) - med(sa, lambda x: x["frame_ms"]))
    print(f"\n  frame-time change {moved:.2f} ms against a resolution of {res:.2f} ms")
    if moved < res:
        print("  !! NOT A RESULT. The shift is below what these runs can resolve. Either")
        print("     the change does nothing, or you need longer runs (resolution improves")
        print("     as sqrt(n)) or a harder-controlled scene.")
        if ca or cb:
            print("     Here it is none of those: a capped run cannot show a frame-time")
            print("     change at all, however large the change really was.")
    else:
        print(f"  -> {moved / res:.1f}x the resolution, so this is a real difference.")

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
    args = sys.argv[1:]
    from_load = "--from-load" in args
    paths = [a for a in args if a != "--from-load"]
    if not paths:
        print(__doc__)
        return 1
    profiles = [profile(p, from_load) for p in paths]
    for p in profiles:
        report(p)
        report_hitches(p)
        print()
    for p in profiles[1:]:
        compare(profiles[0], p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
