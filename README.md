# pz-tuning

Measurement-driven tuning for Project Zomboid Build 42 with the Viewpoint
first-person shader stack, on this desktop.

**Why it exists.** Viewpoint's shader pack alone exposes 68 settings, across 566 keys
in `viewpoint-live.properties`. Guessing at that space is hopeless, and the mod's own
telemetry makes guessing unnecessary: it logs **per-pass GPU and render-CPU costs**
every few seconds. These two scripts turn that into a benchmark loop.

```
python pzbench.py  <DebugLog.txt> [<DebugLog.txt> ...]   # profile, and compare runs
python pzpreset.py --list | --diff P | --apply P         # change settings reproducibly
```

Started 2026-11-04 after two `AppHangB1` hangs entering first person. No remote, per
the host convention in `~/src/desktop-admin/CLAUDE.md` — local history so the
reasoning survives, publishing is a separate decision.

---

## The problem it was built for, and how it was actually diagnosed

Reported as "crashes a lot, just crashed going into first person mode".

**It was not crashing.** Windows Application log, Event 1002 `AppHangB1`:
*"ProjectZomboid64.exe stopped interacting with Windows and was closed."* Twice,
13:48 and 14:04 on 2026-11-04, and those are the **only two ever recorded**.

Ruled out by looking, not by reasoning:

| Suspect | Evidence against |
|---|---|
| GPU driver timeout (TDR) | zero `nvlddmkm` / 4101 events in 3 days |
| Java heap OOM | heap 1859 of 2747 MiB, no `hs_err`, no OOM in log |
| VRAM exhaustion | 3.4 GB free at the hang; 7 evictions totalling 96 MiB |
| Hardware | 31.8 GB RAM, RTX 4070 Ti 12 GB |

**The cause.** Both hangs stop dead on a `[TrueWeathers]` shader-injection line,
moments after the iso → first-person transition rebuilds Viewpoint's deferred
pipeline. TrueWeathers had taken over the shader pack and rewritten the preset:

```
[TrueWeathers] shader pack: selected True Weathers & Lighting (was 'default').
               Turn "Use the True Weathers shader pack" off in Mod Options...
[Viewpoint]    trueweathers's preset High: its values changed, written again.
```

That forces a full shader recompile, synchronously, on the main thread. The window
stops pumping messages and Windows closes it after ~5 s. **The game was never
crashing — it was busy.**

Worst pipeline-build frame went **2,678 ms → 349 ms** once the pack was reined in,
and a 25-minute first-person session produced zero hangs.

---

## What the profile says

The baseline that matters is the **controlled** one — standing still, warmup ramp
discarded, 2026-11-04 15:15 and 15:19, reproducible to 0.08 ms:

```
main frame median  7.22 ms  ->  138 fps
gpu median         5.91 ms  ->  would support 169 fps

LIMITER: MAIN THREAD   (1.31 ms of GPU headroom unused, 19x the resolution)
```

Under **actual play** it is 9.36 ms / 107 fps, and at hour 4 of a session that is
still main-thread bound with the GPU unchanged at 5.72 ms. Playing costs ~2.1 ms of
main thread; the GPU does not notice.

Per-pass GPU cost, median, controlled run:

```
sky+light   1.51  25.6%      glass    0.35
gbuffer     1.19  20.1%      far      0.24
shadow      1.02  17.2%      air      0.17
post+taa    0.80  13.6%      bloom    0.06
occlusion   0.49   8.3%      weather  0.04
                             floors   0.03
                             outlines 0.00
```

**Only five passes cost anything.** Everything below `occlusion` is rounding error,
so every setting feeding those passes is free — turn them up.

And the main thread splits as `chunk cache 0.40 + far world 0.08 + snapshot 1.24`
= **1.7 ms of Viewpoint**, leaving **~5.5 ms of base-game simulation**. Roughly 76%
of frame time is the vanilla update loop, which no shader setting touches.

### Which is why the search space collapses

It is not a 68-dimensional optimisation. While the main thread is the limiter, the
**entire GPU-side setting space is irrelevant to fps** — so:

> Spend GPU headroom on looks until the GPU median approaches the main-thread median,
> then stop. One number to walk up, not 2^68 combinations.

Past that point, fps only moves via base-game main-thread settings (`options.ini`),
sandbox population, or **the JVM** — which is neither of the two config files and was
the lever this framing originally missed. See the hour-4 table.

---

## What four hours of play does, which no short run shows

From the last 6m20s of the 2026-11-04 15:23 session (3h58m in), against the fresh
2-minute controlled runs. **This is where the remaining levers are.**

| | fresh (2 min in) | hour 4 | |
|---|---|---|---|
| main frame median | 7.22 ms (138 fps) | 9.36 ms (107 fps) | −22%, and real (3.5× resolution) |
| GPU median | 5.91 ms | 5.72 ms | unchanged — **still not the limiter** |
| Shenandoah **cycles** / 10 s | 3 | **21** | **7× the collection rate** |
| Shenandoah pauses / 10 s | 12 in 1 ms | 84 in 6 ms | still trivial as *pause* time |
| heap | 2335 / 3072 MiB | 2696 / **3067** MiB | cap ratcheted to the `-Xmx` ceiling |
| VRAM used | 7727 / 12282 MiB | **11448** / 12282 MiB | 834 MiB free; first evictions appear |
| commit charge | 13.8 GB | 18.5 GB | fine on 31.8 GB |

**The GC finding is the strongest lever on the board, and it corrects an earlier
call.** `-Xmx` was written off here as "hygiene, it will not raise fps", on the
strength of `Shenandoah Pauses 16 in 1 ms` from a 25-minute run. That reading of
*pause* time was right and the conclusion was wrong: the measurement was simply taken
too early in a session to see the problem. At hour 4 the live set is ~2.7 GB against a
3.07 GB cap, leaving ~370 MB of allocation headroom, so Shenandoah runs a **full cycle
every ~480 ms, continuously**. Pause time stays negligible because Shenandoah is
concurrent — but concurrent means its marking and evacuation threads compete for CPU
*with the main thread*, which is the limiter here. Raising `-Xmx` is now the
best-supported single change, and the loop can check it: play to hour 3+ and read the
cycle rate.

**VRAM is at 93% by hour 4**, up 3.7 GB from fresh, with the first evictions (4,
58 MiB) in the final sample. `options.ini` has every texture lever at maximum —
`textureCompression=false`, `texture2x=true`, `maxTextureSize=4`,
`maxVehicleTextureSize=4`. Stated carefully: **58 MiB of eviction is not yet a
measured cost**, it is a ceiling being approached. It is a reason to keep watching,
not yet a reason to give up texture quality.

**The hitches are bursts, not a rate — and the earlier hypothesis was wrong.** 22 slow
main-thread frames in 6m20s, but **17 of them inside one 8-second window**
(19:18:22–30), with 21 of the 31 slow *render*-thread frames in the same window. The
`shadow` GPU pass tripled across it, 0.97 → 1.64 → 2.21 ms, while fps fell to 67.
That is a sudden crowd of shadow casters, not shader variants compiling and not a
texture-streaming stall — this file was four hours into the session, long past any
cold cache. Worst frame 136 ms, well clear of a hang. **So shadow settings are the
lever for the hitching specifically**, which is a different question from median fps
and needs a different instrument than a median.

## Things measured and then found wrong

Kept because the wrong version is instructive.

- **`memory.minecraftMiB=6144` is a cap, not a reservation.** The pool reads
  `Minecraft shaders 0/6144` — it holds nothing. Lowering it frees nothing. Viewpoint's
  *entire* VRAM footprint is 182 MiB across all pools; the ~5.9 GB the game uses is
  base-game textures. `textureCompression` is the real lever there.
- **VRAM was never the problem.** 7 evictions / 96 MiB over a whole session is noise,
  and 3.4 GB was free at the hang. It was listed alongside real causes, which was
  padding.
- **"GC was never the problem either" — withdrawn 2026-11-04.** That read
  `Shenandoah Pauses 16 in 1 ms` off a 25-minute run and concluded `-Xmx` was hygiene
  that "will not raise fps". The pause figure was correct; the conclusion was not. A
  four-hour session shows **21 cycles per 10 s against 3 when fresh**, with the heap
  pinned at the `-Xmx` ceiling. See the hour-4 table above. The error was reading a
  steady-state property off a session that had not reached steady state — the same
  mistake, in a different guise, as counting the warmup ramp as noise.
- **"VRAM already in use" is the desktop**, not a leak: `dwm` 1820 MB,
  PhoneExperienceHost 652 MB, msedge 459 MB, steamwebhelper 323 MB, and a tail of tray
  apps. ~2.7 GB resident before the game starts.

---

## Benchmark protocol

Runs are only comparable if the scene is. Weather alone swings `sky+light` hard.

### Step zero: measure the noise floor — done, 2026-11-04

Two 2-minute runs on an **identical** config, standing still in first person, at
15:15 and 15:19. This is the measurement the whole method rests on, and it answered
the question in the method's favour:

```
run A   median 7.22 ms   p10-p90 band 0.17 ms   n=6    resolution 0.07 ms
run B   median 7.31 ms   p10-p90 band 0.25 ms   n=7    resolution 0.09 ms

delta of medians 0.08 ms, against a resolution of 0.09 ms  ->  NOT A RESULT
```

Two independent runs of the same config land **0.08 ms apart** and the tool correctly
declines to call that a difference. Against **~1.4 ms of GPU headroom**, that is a
15:1 signal-to-noise ratio. **The loop works. Single settings are measurable.**

#### Two things that were wrong before this was measured

**The band was never 1.46 ms — that figure was a warmup ramp counted as noise.**
Frame time falls monotonically for ~60–90 s after the pipeline builds:

```
22.49 -> 8.37 -> 8.05 -> 7.69 -> 7.49 -> 8.07 -> 7.38 -> 7.44 -> 7.23 -> 7.22 -> 7.21
|------------ ramp, ~60 s, discard -----------|------------ plateau -------------|
```

with render CPU descending 13.5 → 5.6 → 5.4 → 5.3 alongside it. Including the ramp
reports a 1.16 ms band; excluding it reports **0.17 ms** — an eightfold overstatement,
which is what made real changes look unresolvable. `split_warmup` caught only the
single pipeline-build sample (render CPU 3294 ms) and let the ramp through as steady
state.

**The resolution of the benchmark is not the spread of its samples.** The comparison
is between *medians*, and a median of n samples is steadier than one sample by
roughly √n. Comparing a median delta against the sample band was too strict by that
factor, and it is what produced the "1.3:1, marginal" verdict. `resolution()` now
reports `band/√n`, which is about a 2σ threshold on the median — and the controlled
pair above confirms it empirically.

> **Retracted: "do not bench `01-restore-cheap`, it is under the floor."** The floor
> is 0.07–0.09 ms on a controlled run, not ~1.5 ms. A 0.5 ms change is five times the
> resolution and perfectly measurable. The only honest part of that advice was the
> last clause — the question it really answers is whether the lens flares look better
> to you, and that is still a matter for the eye.

**Scene composition is a separate, real, and irreducible variance source.** Under
actual play the band is 3.79 ms (n=39) even with weather constant. That is why runs
are made standing still; it is not the same thing as the warmup ramp, and conflating
the two is what caused the error above.

### The loop

1. `python pzpreset.py --snapshot my-baseline` — always have a way back.
2. `--diff` the preset, then `--apply` it. **Game must be closed.**
3. Launch, go to the *same* spot, same in-game time and weather, enter first person.
4. Stand still **4–5 minutes**, not 2. The first ~90 s is the warmup ramp and gets
   discarded, so a 2-minute run yields only 6–7 usable samples. 4–5 minutes leaves
   ~18–24, and resolution improves as √n — roughly 0.07 ms → 0.04 ms.
5. Quit, then `python pzbench.py <old log> <new log>`.
6. **One change per run.** Two changes at once are unattributable.

Logs live in `~/Zomboid/Logs/`. Two traps, both of which have already cost time:

- PZ **rotates the previous run** into `Logs/logs_<timestamp>/` on launch, so the log
  you want is usually in a subfolder, not loose in `Logs/`.
- **`DebugLog.txt` is truncated, not appended indefinitely.** The 3h58m session of
  2026-11-04 15:23 left a 230 KB file of **214 lines**, beginning mid-session at
  19:15:17 with frame counter `f:1619662`. A 4-minute run leaves 1.4 MB. So a long
  session yields only its last few minutes of telemetry. That is still the most
  valuable few minutes — it shows the state *after* hours of play — but there is no
  way to see the trajectory that got there, and nothing warns you.

`pzbench.py` separates **pipeline-build** frames from **steady state** on purpose. The
frame that hung the game was a build frame of 2,678 ms; folding that into a median
hides both the stall and the real frame cost.

## Safety built into `pzpreset.py`

Three failure modes, all silent if unguarded:

1. **Refuses to run while the game is running.** Both files are rewritten on exit, so
   a mid-session edit is lost — and you would conclude the setting did nothing.
   Viewpoint's own file header says the same: *"Edit it by hand only with the game
   closed."*
2. **Preserves CRLF and key order.** Load-and-dump would reorder 566 keys and drop the
   header, making every future diff useless. Edits are line-oriented.
3. **An unknown key is a hard error.** A typo would otherwise apply cleanly, change
   nothing, and read as "that setting makes no difference".

Every write backs up first to `backups/`, timestamped; `--restore` puts one back
(and backs up the current file on the way).

## Presets

| Preset | Target | What |
|---|---|---|
| `00-baseline-*` | both | snapshot taken before any change — the way back |
| `01-restore-cheap` | viewpoint | spend the headroom: lens flare, far shadows, headlight shadows, occlusion medium |
| `02-mainthread` | options | `maxActiveRagdolls` 60→20, `uiRenderFPS` 120→60 |
| `03-sky-and-light` | viewpoint | **reserve.** `bounceLight`, `cloudSteps` — both land in the most expensive pass |

`01` and `02` applied 2026-11-04 14:50; both verified to have changed exactly their
intended lines and nothing else. Not yet benched.

## Open

In priority order, and the first one is now well ahead of the rest.

1. **`-Xmx3072m` → `6144m`** — **applied 2026-11-09**, backed up to
   `ProjectZomboid64.json.pretune-2026-11-09T090752`, JSON re-validated, game closed
   at the time. The hour-4 table is the case: 21 GC cycles per 10 s against 3 when
   fresh, heap pinned at the ceiling, on a host where the main thread is the limiter.
   Only `-Xmx` was touched — Viewpoint manages that file (it keeps a
   `.viewpoint-backup`) and swapped the collector ZGC → Shenandoah; **the collector
   was left alone**, it is behaving. **Unverified.** The test is to play to hour 3+
   and read the cycle rate off the stat line; a 4-minute run cannot see it, because
   the problem only exists once the heap has filled. If Viewpoint or a Steam file
   verification rewrites the file, this reverts silently — re-check the value before
   concluding the change did nothing.
2. **Bench `02-mainthread`** (applied 2026-11-04, never measured). Main-thread side, so
   it can actually move the limiter. Use the 4–5 minute protocol.
3. **Then spend the GPU headroom**, ~1.4 ms of it, one key at a time — `03-sky-and-light`
   first, since both its keys land in the most expensive pass. Now genuinely
   measurable at 15:1 signal-to-noise.
4. **The hitching needs a different instrument.** It is bursty (17 of 22 slow frames in
   one 8-second window) and correlated with the `shadow` pass tripling, so a median
   cannot see it. Wanted: a per-burst report keyed off the `frames over 50 ms` counter
   that already appears in every stat line.
5. **`textureCompression=false`, `texture2x=true`** — VRAM reached 93% by hour 4 with
   the first evictions. Watch it; do not trade texture quality for it yet.

Lower priority, unchanged:

- **No clean before/after exists for `01`/`02`.** They were applied immediately after
  the only profiled run, so the pre-change state was never captured under control.
  Recoverable with `--apply 00-baseline-viewpoint` / `00-baseline-options` if a true A
  is ever wanted; probably not worth it now that the baseline is re-measured.
- **`AdvancedAnimator$1.visitFileFailed` threw 11 exceptions at startup.** Unrelated to
  the hang, never investigated.
