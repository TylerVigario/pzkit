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
python pztail.py   [--console]                           # keep a log the game would wipe
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
*with the main thread*, which is the limiter here.

**`-Xmx3072m` → `6144m` is applied and verified.** Applied 2026-11-09, backed up to
`ProjectZomboid64.json.pretune-2026-11-09T090752`; only `-Xmx` was touched. Measured
across three sessions on 2026-11-11:

| per 10 s | `-Xmx3072m` | `-Xmx6144m` |
|---|---|---|
| Shenandoah cycles | median **21**, max 34 | median **0** |
| concurrent GC work | median **8.7 s**, max 12.4 s | median **0 ms** |
| heap used | 2,591 of ~3,000 — pinned at the ceiling | 3,635 of ~5,550 |

8.7 seconds of collector work per 10 seconds of wall clock is roughly a core running
continuously, and it tracked straight into frame time: 7.89 ms at 15 cycles, 11.17 ms
at 24, back to 7.60 ms when it dipped to 21. **The heap was simply smaller than the
working set** — 3,635 MiB cannot fit in a 3,072 MiB ceiling, which is the whole bug
and the one inference that needs no other controls.

**It does not take hour 3 to see.** The old config was already at 15 cycles/10 s inside
its first ten minutes and 21 by minute 20, so a 25-minute session is sufficient. The
"3 cycles when fresh" figure came from too early a sample.

**Confounds that were checked rather than assumed.** MPOptimizer was absent from the
2026-11-04 run and present on 2026-11-11, and it installs its own "Memory & Garbage
Collection Manager" — so the first comparison had two new variables plus
single-player → multiplayer. Removing it changed nothing: GC stayed at median 0 cycles
with the mod gone, so the collector manager contributed nothing and `-Xmx` is the cause.
Separately, the collector itself is **Shenandoah by Viewpoint's edit, not the game's
default** — vanilla selects ZGC on the Windows 10/11 key. The thrash was therefore
Shenandoah's behaviour at a 3 GB ceiling; ZGC at 3072m was never tried and does not
need to be.

**VRAM eviction is a real cost, and it is not the card filling up.** Measured
2026-11-11 across three sessions: eviction events fire at **45–76% card occupancy**,
and **13 of 40 main-thread stalls land within 1.5 s of one — 65× what chance gives**
(tested against 2,000 randomised placements over the same span). The stalls run
130–280 ms with Viewpoint attributing only **1.4 ms** of each to itself; the rest is
the game re-uploading what the driver dumped. Worst observed 265 ms while standing
still.

The mechanism is the Windows GPU memory **budget**, not capacity. Measured during play:
PZ holds 3,933 MB while other processes hold **3,005 MB** — `dwm` alone 1,735 MB. The
manager sizes each process's budget against the others, so PZ is evicted while 6.7 GB
of the card reads "free". The lever is therefore *fewer resident competitors*, not
lower texture settings.

`options.ini` texture levers: `texture2x=true`, `maxTextureSize=4`,
`maxVehicleTextureSize=4`, and `textureCompression=true` — the last one changed by
MPOptimizer rather than chosen. See **Open**.

**The hitches are bursts, not a rate — and the earlier hypothesis was wrong.** 22 slow
main-thread frames in 6m20s, but **17 of them inside one 8-second window**
(19:18:22–30), with 21 of the 31 slow *render*-thread frames in the same window. The
`shadow` GPU pass tripled across it, 0.97 → 1.64 → 2.21 ms, while fps fell to 67.
That is a sudden crowd of shadow casters, not shader variants compiling and not a
texture-streaming stall — this file was four hours into the session, long past any
cold cache. Worst frame 136 ms, well clear of a hang.

**There are two hitch classes and they need separating.** That 8-second burst is one:
a crowd of shadow casters, where the `shadow` pass itself triples and shadow settings
are the lever. The other is the recurring kind measured 2026-11-11 — roughly one every
25–69 seconds, median 177–198 ms, with Viewpoint's own work at 1.4 ms and the cost in
GPU-memory eviction. Shadow settings do nothing for the second kind. **The attribution
in each `slow frame on the main thread` line is what tells them apart:** a large `ours`
is the mod's own work, a small `ours` with a large remainder is the game re-uploading,
the renderer, or the server. Reading the median instead of the attribution is what kept
this confused for a week.

## Two index conventions in one options file

`options.ini` stores the graphics quality settings two different ways, and both read as
"at maximum" by opposite arithmetic. Verified 2026-11-11 against
`media/lua/client/OptionScreens/MainOptions.lua`.

| key | stored as | value here | means |
|---|---|---|---|
| `water` | combo index, `selected = v + 1` | `0` | **High** of {High, Medium, Low} |
| `puddles` | same | `0` | **High** |
| `perfPuddles` | same | `0` | **All** of {All, GroundWithRuts, GroundOnly, None} |
| `fogQuality` | same | `0` | **High** of {High, Medium, legacy} |
| `perfSkybox` | same | `0` | **High** of {High, Medium, No} |
| `maxTextureSize` | 1-based, **no offset**: `selected = v` | `4` | **largest** of four |
| `maxVehicleTextureSize` | same | `4` | **largest** |
| `bPerfReflections` | plain bool, no index | `false` | **off** |

The first group applies with `set…(box.selected - 1)` and the first combo entry is the
best one, so a `0` is the *highest* setting. The second group applies with
`set…(box.selected)` and no arithmetic at all, so a `4` is also the highest. Reading a
`0` as "off" inverts the conclusion about where GPU headroom can be spent: it puts the
floor in `options.ini`, where there is none, instead of in Viewpoint's shader pack,
where 13 of 68 keys really are `false`.

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

0. `python pzconfig.py --bench` — take the frame cap off, or steps 5 and 6 measure
   nothing. `--play` puts it back; `--show` says which profile is live. It edits only
   `vsync`, `uncappedFPS` and the caps file's `game` key, in place, because the game
   rewrites `options.ini` from memory on exit and a whole-file restore would quietly
   revert keybinds and volumes along with the cap.
1. `python pzpreset.py --snapshot my-baseline` — always have a way back.
2. `--diff` the preset, then `--apply` it. **Game must be closed.**
3. Launch, go to the *same* spot, same in-game time and weather, enter first person.
4. Stand still **4–5 minutes**, not 2. The first ~90 s is the warmup ramp and gets
   discarded, so a 2-minute run yields only 6–7 usable samples. 4–5 minutes leaves
   ~18–24, and resolution improves as √n — roughly 0.07 ms → 0.04 ms.
5. Quit, then `python pzbench.py <old log> <new log>`.
6. **One change per run.** Two changes at once are unattributable.

**Step 5 is the one that does not get done.** The 2026-11-04 session sat unanalysed for
five days and `02-mainthread` was applied 2026-11-04 and never measured at all — both
logs existed the whole time. So `pzauto.py` does it instead:

```
python pzauto.py            # watch; report each session as it ends
python pzauto.py --once     # report any finished, unreported session now
python pzauto.py --report <capture>
```

A session counts as over only when **all three** hold: no `ProjectZomboid64.exe`, its
capture idle for 90 s, and no report written yet. Each alone is wrong — no process is
also true *before* launch, and a quiet capture is also true in a menu. A failed
`tasklist` is treated as "running", so a broken query cannot produce a mid-session
report. Runs under 12 stat lines are skipped and recorded as skipped, because `pzbench`
will otherwise state a confident median over nothing.

Reports land in `reports/<session>.txt`, each compared against the previous session
that produced one, and `reports/INDEX.md` carries one line per session so the trend is
readable without opening any of them. The index is parsed back out of `pzbench`'s own
output, scoped to that capture's profile block, so it cannot disagree with the report
beside it.

Logs live in `~/Zomboid/Logs/`. Two traps, both of which have already cost time:

- PZ **rotates the previous run** into `Logs/logs_<timestamp>/` on launch, so the log
  you want is usually in a subfolder, not loose in `Logs/`.
- **PZ wipes its own log at ~10 MB, mid-session, with no backup.** Not rotation.
  Read out of `zombie/core/logger/ZLogger` in `projectzomboid.jar` (42.21.0):

  ```java
  static final long s_maxSizeKo = 10000;

  void checkSizeUnsafe() {
      if (this.file.length() / 1024L > s_maxSizeKo) recreateLogFileUnsafe();
  }
  void recreateLogFileUnsafe() {
      this.outputStreams.file.close();
      this.file = new File(LoggerManager.getLogFilePath(this.name));
      this.outputStreams.file = new PrintStream(this.file);   // truncates
  }
  ```

  It fires at **10,241,024 bytes** exactly — integer division, so the first length
  whose `length/1024` exceeds 10000 — then reopens a `PrintStream` on the *same
  path*, which truncates it. Everything written earlier is destroyed while the game
  is still running. The 3h58m session of 2026-11-04 15:23 left 214 lines covering its
  last 6m20s; the first 3h52m were gone.

  **There is nothing to configure.** `s_maxSizeKo` is `static final` — a compile-time
  constant, with no system property, no launch flag and no `debuglog.cfg` key behind
  it. `console.txt` is a separate `ZLogger` with its own 10 MB budget, so it wipes at
  a different moment and by luck covered 811 stat lines of that session against
  DebugLog's 39 — useful, but not a fix.

  **Cutting log volume is not a way out either.** In steady state **82% of the bytes
  are Viewpoint's own stat line** — 4.8 KB per sample, which is exactly the data
  worth keeping. Silencing every other channel buys 18%, moving the cap from 4.7 to
  5.7 hours, and Viewpoint has no key to trim the line (all 566 checked).

  So **`pztail.py` copies the bytes out as they are written** and survives the
  truncate. Run it before a long session:

  ```
  python pztail.py              # follow the live log, Ctrl-C to stop
  python pztail.py --console    # also follow console.txt
  python pztail.py --once       # one catch-up pass over what is there now
  ```

  Captures are append-only in `captures/`, and `pzbench.py` reads them directly. The
  truncate detection is tested two ways — a plain truncate, and a truncate that
  refills *past* the old offset inside one poll interval, which a size comparison
  alone would miss and append into the middle of a fresh file. A 256-byte anchor
  check catches that one.

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

## What this harness cannot see, and the day it mattered

**Every metric in here is client-side. None of them can see a server.** On 2026-11-11
a multiplayer session was unplayable — actions not registering, a car that stopped
dead, a death to a horde — while this harness reported the best numbers it had ever
produced: main frame 6.07 ms, 165 fps, zero slow frames, zero evictions, GC idle. The
client was flawless and the game was broken.

The server's simulation tick had collapsed from ~10/s to **0.2/s**, about 2.5 seconds
per tick, for twelve minutes. `pzbench` cannot detect that and never will; a clean
report is not evidence that the game plays well.

What the client log *can* contribute is the trigger and the corroboration:

| per minute | collapse (2026-11-11 09:39) |
|---|---|
| `remembered chunks` | 499 → 14,010 over 25 min |
| `zombies loaded` | 29 → 196 → 410 (cap) |
| `EquipPacket.set get invalid arguments` | 51 |
| `player is not found` | 34 |
| main thread frame | **unchanged at 6.07 ms** |

`remembered chunks` is the useful column and it had never been read before: the cause
was the **rate new map area is entered**, not zombie population (sandbox was Normal).
The per-minute sync-error counts are the client-side fingerprint of a stalled server,
and they are the right thing to grep for when the game feels wrong but the frame time
does not move.

Root cause was found server-side by thread dump — a mod's `OnTick` handler draining an
uncapped queue of loaded containers — and fixed by capping it. The after: **7.65 ticks/s
minimum under five times the loading rate**, with `EquipPacket` failures and
`player is not found` at **zero** across a 20-minute run. The residual hitch under
continuous fast travel is vanilla Java chunk loading and is not tunable.

**The rule this leaves behind:** when the symptom is *delay* rather than *stutter*,
measure the other end first. Stutter is a frame-time problem and lives here. Delay is
a simulation-rate problem and does not.

## The client frame time was the refresh interval, not a measurement

Found 2026-11-11, after three sessions had already been compared on it.

The display is **165 Hz**. The ceiling was set three times over: `vsync=true` and
`frameRate=165` in `options.ini`, and `viewpoint-frame-caps.properties` carrying no
`game` line, which its own header says means "the monitor's refresh rate".

| session | min | median | p90−p10 | on the 6.06 ms floor | over 6.50 ms |
|---|---|---|---|---|---|
| 11-04, before the heap fix | 7.18 | 9.36 | **5.07 ms** | 0% | 100% |
| 11-11 08-56 | **6.06** | 6.16 | 1.73 ms | 47.3% | 30.5% |
| 11-11 10-31 | **6.06** | 6.10 | **0.27 ms** | 69.8% | 5.8% |
| 11-11 11-09 | **6.06** | 6.30 | 1.50 ms | 37.6% | 36.6% |

**Not one sample below 6.06 ms in any session after the heap fix**, in all three to the
hundredth, where 165 Hz is 6.0606 ms. That is a hard floor, not the fast end of a
distribution. At the floor the accounted main-thread work is `cache 0.46 far 0.09
snapshot 1.50` ≈ 2.05 ms of a 6.07 ms frame, with `GPU 0.63 frames behind (at most 1)`:
the main thread spends about 4 ms waiting on the present rather than working.

Three consequences, all of which had already been acted on:

- **`LIMITER: MAIN THREAD` on those runs means *limited by the present*.** The main
  thread is simply the thread that blocks on the swap. It is not a statement about
  main-thread work.
- **The PZO comparison was "no change visible at the cap", not "no change".** The
  verdict was right; the reason given for it was weaker than stated. A median that
  cannot move is not evidence that nothing moved.
- **The heap result is untouched.** The 11-04 session had no samples anywhere near the
  cap and a 5.07 ms band, and the verification rested on GC cycle counts and a 3,635 MiB
  settled heap — neither of which is clamped.

What survives a cap, and is therefore still worth reading from the three sessions: the
**per-pass GPU ms** (GPU timer queries), eviction counts, the heap, the slow-frame log,
and the **share of frames that miss the cap** — which ran 30.5% / 5.8% / 36.6% while the
medians spanned 0.20 ms.

`pzbench.cap()` now detects this and refuses to compare two ceilings; `pzconfig.py`
takes the ceiling off. The check that the uncap worked is the floor moving below
6.06 ms, not either script reporting success.

**The same trap, caught earlier in someone else's log and missed here.** It was flagged
to the host lane on 2026-11-11 — their frame time was pinned flat at 33.33 ms by
Viewpoint's `graphics.frameGeneration`, making the significance test meaningless — while
this machine's own log was clamped in a milder form the whole time. A flat signal is
easier to see in a log you are reading for the first time than in one you have been
quoting for a week.

## Open

In priority order.

0. **Uncap first — `python pzconfig.py --bench`.** Prerequisite for 1 and 2, not a
   separate task. Capped, both land inside a 0.10 ms window and read as nothing.
1. **Bench `02-mainthread`** (applied 2026-11-04, never measured). Main-thread side, so
   it can actually move the limiter. Use the 4–5 minute protocol, uncapped — a
   main-thread change is exactly what a frame-time cap hides.
2. **Spend the GPU headroom — at least 1.5 ms of it, and probably more.** GPU median
   runs **4.51–4.73 ms** against the 6.06 ms cap. That gap is real and spendable, but it
   is a **lower bound**: the frame time it is measured against is a ceiling, so the true
   headroom is unknown until a run is uncapped. A headroom-to-resolution ratio taken
   against a capped frame time divides one clamped number by another and means nothing,
   however large it comes out.

   **The vanilla options cannot spend it — they are already at maximum.** See
   *Two index conventions in one options file* below; the 0s in `options.ini` mean High,
   not off. The one vanilla graphics option genuinely turned down is
   `bPerfReflections=false`.

   **Viewpoint's shader pack is where the floor actually is.** 13 of its 68
   `pack.trueweathers.*` keys are `false`, and all six keys that `01-restore-cheap` and
   `03-sky-and-light` target still hold their 2026-11-04 baseline values — so
   `01-restore-cheap` was written and never applied, and the headroom has never been
   spent at all.

   **This needs no A/B** — the stat line attributes GPU ms per pass, so one run prices
   every feature at once. Per-pass costs are GPU timer queries and stay valid under a cap,
   which makes this the one open item that could have been worked the whole time. Top
   passes measured: `gbuffer` 1.29 ms (28.5%), `sky+light` 1.10 ms (24.2%), `post+taa`
   0.59 ms; `air`, `weather` and `outlines` sit at 0.00 ms. **A key does not necessarily
   map to the pass that shares its name**: the `occlusion` pass already costs 0.45 ms
   while `pack.trueweathers.occlusion` is `false`, so that pass is the base pack's own
   work.

   `04-high-viewpoint` + `04-high-options` go deliberately over budget — fidelity keys on,
   taste keys (`motionBlur`, `filmGrain`, `depthOfField`, `nebula`, `fftWaves`) left off —
   so the per-pass table can be read and then scaled down. Going high first is the method:
   cost is only attributable to a setting that is actually costing something, whereas from
   the floor every addition reads as zero until the budget is crossed and then they all
   read at once.

   One unverified value in that preset: `occlusionQuality=medium`. `low` is live and
   `medium` is carried from `01-restore-cheap`, but the accepted set is not documented in
   any file on disk. If the occlusion pass does not move, suspect a rejected value before
   suspecting the setting.
3. **Reduce what competes for the GPU memory budget.** Measured 2026-11-11 during play:
   PZ holds 3,933 MB and **other processes hold 3,005 MB** — `dwm` 1,735 MB, then
   Steam/Epic/Battle.net/Samsung Magician/Edge/Code/terminals. Evictions fire across
   **45–76% card occupancy, median 67%** (8,262 of 12,282 MiB) because Windows sizes each
   process's budget against the others, not because 12 GB is full. Quote the range or
   the median, not the low end — at 45% occupancy the card looks almost empty, which
   makes the budget mechanism sound stranger than it is.

   **Ruled out 2026-11-11 on Tyler's call, and the arithmetic agrees.** The available
   win is ~490 MB against roughly 4 GB of headroom, about 12%, in exchange for changing
   how the machine gets used while playing. That is not a trade worth making, and a
   tuning lever that requires closing the launchers you actually use is not a lever. The
   eviction-stall problem stands open; this was the wrong instrument for it. The baseline
   of **0.21–0.32 eviction events per minute** remains the number any future attempt
   measures against.
4. **`textureCompression=true` — keep it, deliberately.** It arrived as residue, not a
   decision: the pre-PZO value was `false`, MPOptimizer enforced `Enabled` mid-session on
   2026-11-11 at 09:43, and the change outlived the mod. But compression lowers VRAM
   pressure, and VRAM eviction is the measured stall source above, so the value is right
   even though nobody chose it. Resolved as a keep rather than reverted as residue — the
   right value and a trustworthy provenance are different things, and only the first one
   affects frame time. It still must be declared whenever a run is compared against
   anything from before 2026-11-11.

**Ruled out, so it does not get proposed again: Viewpoint's own memory pools.**
`model packs` peaks at 122/128 and sits ≥95% full in 30% of frames, which looks like
eviction pressure. It is not: evictions land in those frames at **×0.88**, slightly
*less* than chance over 519 samples. Raising the pool caps would cost VRAM for nothing.
The first cut of that test read 65 of 65 evictions "in a frame where a pool was full"
and was vacuous — `sound files` sits at 122/128 in **100%** of frames, so the predicate
was always true. A condition that is never false cannot be evidence.

Lower priority, unchanged:

- **No clean before/after exists for `01`/`02`.** They were applied immediately after
  the only profiled run, so the pre-change state was never captured under control.
  Recoverable with `--apply 00-baseline-viewpoint` / `00-baseline-options` if a true A
  is ever wanted; probably not worth it now that the baseline is re-measured.
- **`AdvancedAnimator$1.visitFileFailed` threw 11 exceptions at startup.** Unrelated to
  the hang, never investigated.
