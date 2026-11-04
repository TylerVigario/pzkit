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

## What the profile says, 2026-11-04

```
main frame median  7.64 ms  ->  131 fps
gpu median         5.77 ms  ->  would support 173 fps

LIMITER: MAIN THREAD   (1.87 ms of GPU headroom unused)
```

Per-pass GPU cost, median:

```
sky+light   1.66  29.8%      glass    0.31
gbuffer     1.22  21.9%      far      0.20
shadow      0.73  13.1%      air      0.15
post+taa    0.68  12.2%      bloom    0.06
occlusion   0.49   8.8%      weather  0.04
                             floors   0.03
                             outlines 0.00
```

**Only five passes cost anything.** Everything below `occlusion` is rounding error,
so every setting feeding those passes is free — turn them up.

And the main thread splits as `chunk cache 0.43 + far world 0.09 + snapshot 1.40`
= **1.9 ms of Viewpoint**, leaving **~5.7 ms of base-game simulation**. Roughly 75%
of frame time is the vanilla update loop, which no shader setting touches.

### Which is why the search space collapses

It is not a 68-dimensional optimisation. While the main thread is the limiter, the
**entire GPU-side setting space is irrelevant to fps** — so:

> Spend GPU headroom on looks until the GPU median approaches the main-thread median,
> then stop. One number to walk up, not 2^68 combinations.

Past that point, fps only moves via base-game main-thread settings (`options.ini`) or
sandbox population.

---

## Things measured and then found wrong

Kept because the wrong version is instructive.

- **`memory.minecraftMiB=6144` is a cap, not a reservation.** The pool reads
  `Minecraft shaders 0/6144` — it holds nothing. Lowering it frees nothing. Viewpoint's
  *entire* VRAM footprint is 182 MiB across all pools; the ~5.9 GB the game uses is
  base-game textures. `textureCompression` is the real lever there.
- **VRAM was never the problem.** 7 evictions / 96 MiB over a whole session is noise,
  and 3.4 GB was free at the hang. It was listed alongside real causes, which was
  padding.
- **GC was never the problem either.** `Shenandoah Pauses 16 in 1 ms`. Raising `-Xmx`
  is hygiene — heap runs 2036–2605 MiB against a 2676 cap, 76–97% full — but it will
  not raise fps, and it was nearly recommended as if it would.
- **"VRAM already in use" is the desktop**, not a leak: `dwm` 1820 MB,
  PhoneExperienceHost 652 MB, msedge 459 MB, steamwebhelper 323 MB, and a tail of tray
  apps. ~2.7 GB resident before the game starts.

---

## Benchmark protocol

Runs are only comparable if the scene is. Weather alone swings `sky+light` hard.

1. `python pzpreset.py --snapshot my-baseline` — always have a way back.
2. `--diff` the preset, then `--apply` it. **Game must be closed.**
3. Launch, go to the *same* spot, same in-game time and weather, enter first person.
4. Stand still ~3 minutes. That yields ~30 stat samples.
5. Quit, then `python pzbench.py <old log> <new log>`.
6. **One change per run.** Two changes at once are unattributable.

Logs live in `~/Zomboid/Logs/`. PZ **rotates the previous run** into
`Logs/logs_<timestamp>/` on launch, so the log you want is often in a subfolder.

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

- **Bench `01` and `02`** — applied, unmeasured.
- **25 slow main-thread frames per 25 min, worst 319 ms.** Well clear of a hang now,
  but a visible hitch. Suspected new shader variants compiling as weather and time
  change; should fade as the cache fills. Unconfirmed.
- **`-Xmx3072m` → `6144m`** in `ProjectZomboid64.json`. Offered, not applied. Note
  Viewpoint manages that file (it keeps a `.viewpoint-backup`) and swapped the
  collector ZGC → Shenandoah; leave the collector alone, it is behaving.
- **`AdvancedAnimator$1.visitFileFailed` threw 11 exceptions at startup.** Unrelated to
  the hang, never investigated.
- **`textureCompression=false`, `texture2x=true`** — real VRAM headroom, no effect on
  the hang. Untested.
