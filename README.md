# pzkit

Measurement tools for **Project Zomboid Build 42** on Windows. They keep the logs the
game deletes, turn the Viewpoint mod's telemetry into benchmarks that can tell a real
change from noise, read each hitch by what caused it, back up single-player saves, and
watch a co-op server's JVM.

They exist because almost every way of getting this wrong is silent. The game wipes
its own log mid-session, rewrites its config files on exit, and caps frame time
without saying so. A counter can reset under you, too. Each tool below is built
around one of those, and the section after the table says which.

Written against Build 42.21 and 42.22. `pztail`, `pzsavebackup`, `pzproc` and
`pzserverstats` work with or without Viewpoint; `pzbench`, `pzauto`, `pzpreset` and
`pzconfig` read or write its files.

## Requirements

- Windows. Several tools ask `tasklist` or PowerShell whether the game is running.
- Python 3.11 or newer. Everything uses the standard library only, except
  `pzserverstats.py`, which declares `psutil` inline so that `uv run --script`
  fetches it.
- For the JVM columns of `pzserverstats.py`, a JDK's `jcmd`. The game does not ship
  one: its bundled `jre64/bin` has `java.exe`, `javaw.exe` and `jfr.exe` among others,
  but no `jcmd` and no `jstat`.
- The game's user folder at `~/Zomboid`, which is the default.

## The tools

```
python pztail.py   [--console] [--server] [--save-backups]   # keep a log the game would wipe
python pzbench.py  [--from-load] <log> [<log> ...]           # profile, hitches, compare runs
python pzauto.py   [--once]                                  # report each finished session
python pzpreset.py --list | --diff P | --apply P             # change settings reproducibly
python pzconfig.py --bench | --play | --show                 # uncapped bench vs play profile
python pzproc.py   [--report CSV]                            # client handles/GDI over a session
python pzsavebackup.py                                       # zip the newest single-player save
uv run --script pzserverstats.py                             # server JVM heap/GC/safepoints
```

Every script explains itself in its docstring, including why it exists and what it
cannot tell you. `--server` and `pzserverstats.py` are for the PC that hosts a co-op
server. Example presets, and how to make your own, are in
[`presets/`](presets/README.md).

## What the game does that these tools work around

### It deletes its own log at 10 MB, mid-session, with no backup

This is not rotation: the earlier part is simply gone. From `zombie/core/logger/ZLogger`
in `projectzomboid.jar` (42.21.0):

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

It fires at **10,241,024 bytes** exactly (integer division: the first length whose
`length/1024` exceeds 10000), then reopens a `PrintStream` on the same path, which
truncates it. A four-hour session can be left with its last six minutes.

**There is nothing to configure.** `s_maxSizeKo` is `static final`, a compile-time
constant, with no system property, launch flag or `debuglog.cfg` key behind it. Cutting
log volume does not get you far either: with Viewpoint running, its own stat line was
82% of the bytes in one measured session, and that line is the data worth keeping.

Two more things about the logs:

- On launch the game moves the **previous** run's logs into `Logs/logs_<timestamp>/`,
  so an older session's log is in a subfolder, not loose in `Logs/`.
- `console.txt` is a separate logger with its own 10 MB budget, so it wipes at a
  different moment.

**`pztail.py`** copies the bytes out as they are written into `captures/`, survives each
truncate (including one that refills past the old position between two polls, which
a 256-byte anchor check catches), and resumes after its own restart rather than
appending the whole log twice. Run it before you play.

### It rewrites its config files when it exits

`options.ini` and Viewpoint's `viewpoint-live.properties` are both written back from
memory on exit, so an edit made while the game runs is lost, and the next benchmark
says the setting did nothing. **`pzpreset.py`** refuses to run while the game is up. It
edits line by line, so the 566 keys keep their order and the file keeps its CRLF
endings and header. It treats an unknown key as an error rather than a silent no-op,
and it backs up every file before writing it.

**`pzconfig.py`** switches between an uncapped bench profile and your play profile. It
edits only `vsync`, `uncappedFPS` and the frame-caps file's `game` key, for the same
reason: a whole-file restore would revert your keybinds and volumes with it.

### `options.ini` stores quality settings two opposite ways

Verified against `media/lua/client/OptionScreens/MainOptions.lua`:

| keys | stored as | so the best setting reads |
|---|---|---|
| `water`, `puddles`, `perfPuddles`, `fogQuality`, `perfSkybox` | combo index, applied as `selected - 1`; the first entry is the best | `0` |
| `maxTextureSize`, `maxVehicleTextureSize` | 1-based, no offset | the highest number |
| `bPerfReflections` | plain boolean | `true` |

Reading those `0`s as "off" sends you looking for headroom in `options.ini`, where
there may be none.

### A frame cap turns frame time into a ceiling

`vsync`, `frameRate`, a `viewpoint-frame-caps.properties` with no `game` line (which
its own header says means the monitor's refresh rate), and Viewpoint's
`graphics.frameGeneration` all pin frame time. Frame generation was seen holding one
PC flat at 33.33 ms. A capped run shows a hard floor, with no sample below
`1000 / refresh rate` ms, and its median is that ceiling, not a measurement. Two
capped runs compared against each other test nothing.

**`pzbench.py`** detects a floor and refuses to compare two ceilings. What it still
reports under a cap is what a cap cannot clamp:
- per-pass GPU ms, which come from GPU timer queries
- evictions, the heap and the slow-frame log
- the share of frames that miss the cap

`python pzconfig.py --bench` takes the cap off. The proof that it worked is the floor
moving, not either script saying so.

### The first minute and a half is not steady state

Entering first person builds Viewpoint's deferred pipeline, which costs one frame
measured in seconds (2,678 ms in one case). Then frame time falls for another 60 to 90
seconds before it settles.
Counting that ramp as noise once overstated the noise band eightfold (1.16 ms against
0.17 ms), which made real changes look unmeasurable. `pzbench` drops both, and it
judges a change against `band / √n`, the resolution of a median of n samples, rather
than against the spread of single samples.

### A hitch has to be read by its attribution, not its size

Viewpoint logs every slow frame with its own split:

```
slow frame on the main thread: 626.2 ms, ours 538.7 (... by part: lights 290.6, ...), the rest 87.5
```

A large `ours` is Viewpoint's own work, so its settings are the lever. A small `ours`
with a large `rest` is not Viewpoint at all: on the main thread that means the game's
update and the garbage collector, and on the render thread the game's own drawing or a
wait on the main thread. A median of slow frames mixes those two classes and explains
neither. The `HITCHES` block of `pzbench` counts how many fall on each side, names the
parts, buckets the sizes and gives a rate.

Script exceptions are split at the first exit from `GameLoadingState`. Before it they
are dominated by noise: a mod without an optional folder logs one `visitFileFailed`
per missing path, 238 of 242 in one session. `--from-load` leaves that phase out
altogether.

### Three log fields that mislead if read naively

- **`st:` is a millisecond clock with an arbitrary origin, and reads `0` until it is
  set.** Across one 165-minute capture, wall time minus `st` was constant to ±2 ms
  once the `st:0` lines were dropped, and every one of those came first. Keep the
  zeros in a fit and you get a spread of thousands of seconds.
- **Viewpoint's `evictions N (M MiB)` is per stat window, not a running total.** It
  falls as well as rises, and runs of equal values suggest windows longer than the
  sample interval. So neither the sum nor the maximum is a session total, and
  `pzbench` reports the maximum as exactly that: the most in one window.
- **On the client, GC counters are misleading.** Windows 10 and later get the
  Shenandoah collector by default on 42.22, and `jstat` then reports young-GC 0 with
  every cycle counted as full GC. `pzserverstats` reads JVM counters from the server
  only.

### The client cannot see the server

Every client-side metric here can look perfect while a co-op game is unplayable.
That happened once: the server's simulation tick fell from about 10 a second to 0.2
for twelve minutes, with the client at 165 fps and no slow frames. When the symptom
is **delay**, with actions that do not register or a car that stops dead, measure the
server first. **Stutter** is a frame-time problem and the client tools see it; delay
is a simulation-rate problem and they cannot. The client log's per-minute counts of
`EquipPacket.set get invalid arguments` and `player is not found` were the clue from
that side. On the hosting PC, `pztail.py --server` keeps the server's own DebugLog,
and `pzserverstats.py` records its heap, collections and safepoints.

### A power cut can zero a save

The operating system had not written a save's last chunk files when the power went,
and they came back full of zeros. **`pzsavebackup.py`** waits for a clean exit
(`GameThread exited.` in `console.txt`) and for the newest single-player save to stop
changing, then zips it, keeping the last five. Run it by hand, or let
`pztail.py --save-backups` do it after every session. Multiplayer saves are skipped.

## Benchmarking a setting

0. `python pzconfig.py --bench`: take the cap off, or steps 5 and 6 measure nothing.
1. `python pzpreset.py --snapshot my-baseline`: always have a way back.
2. `--diff` the preset, then `--apply` it, with the game closed.
3. Launch and go to the **same** spot, at the same in-game time and weather. Enter
   first person.
4. Stand still for **4 to 5 minutes**. About 90 seconds of that is discarded, and
   resolution improves as √n.
5. Quit, then `python pzbench.py <before> <after>`. It says **NOT A RESULT** when a
   difference is inside what the runs can resolve.
6. **One change per run.** Two changes at once cannot be attributed.

Step 5 is the one that does not get done, so `pzauto.py` does it: it reports each
session once the game has exited, its capture has been idle for 90 s, and no report
exists. Each condition alone is wrong. No process is also true before launch, and a
quiet capture is also true in a menu. It skips runs too short to measure rather than
reporting a confident median over nothing, and keeps one line per session in
`reports/INDEX.md`.

## Your data stays on your PC

`captures/`, `reports/`, `backups/` and `save-backups/` are written next to the scripts
and are all gitignored. Keep it that way. Raw logs carry player and character names,
server names, LAN addresses and Windows user paths, and the backups are copies of your
config and your saves.

## License

[AGPL-3.0](LICENSE).
