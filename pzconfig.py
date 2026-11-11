#!/usr/bin/env python3
"""
pzconfig -- flip the client between a PLAY profile and a BENCH profile.

WHY THIS EXISTS
---------------
Every client frame-time number this repo collected after the heap fix was the
refresh interval, not a measurement. The display runs at 165 Hz and the ceiling
is set three times over: `vsync=true` and `frameRate=165` in options.ini, and
viewpoint-frame-caps.properties carrying no `game` line, which its own header
says means "the monitor's refresh rate".

Measured 2026-11-11 over three sessions: minimum main-thread frame time was
6.06 ms in all three, to the hundredth, with 38-70% of samples inside a 0.1 ms
bucket at that floor and NOT ONE sample below it. 165 Hz is 6.0606 ms. That is a
clamp, not a distribution. pzbench's cap() now detects it and refuses to compare
two ceilings; this script is the other half -- it takes the ceiling away.

It is not a play profile. Uncapped means the GPU runs flat out for frames nobody
sees, which costs heat, power and VRAM pressure. Bench, then come back.

WHY SURGICAL EDITS AND NOT A FILE SWAP
--------------------------------------
Project Zomboid rewrites options.ini from its own memory when it exits, so a
saved copy of the file goes stale the moment you change anything in-game. A
profile that restored whole files would quietly revert keybinds, volumes and
window size along with the cap. So this touches ONLY the keys below, in place,
preserving every other line and the file's order.

  options.ini                       vsync, uncappedFPS
  viewpoint-frame-caps.properties   game

`frameRate` is deliberately left alone: with uncappedFPS it is not consulted,
and leaving it means `--play` restores exactly the number that was there.

THE CHECK THAT MATTERS
----------------------
After a bench run, confirm the uncap actually took:

    python pzbench.py captures/<the new capture>

If it still prints "CAPPED RUN. Hard floor at 6.06 ms", the cap did not come
off -- the uncap is unverified until a frame comes in under 6.06 ms. Viewpoint
owns the Display pane, so it may normalise or ignore a hand-written value; the
honest test is the floor moving, not this script reporting success.

    python pzconfig.py --show
    python pzconfig.py --bench
    python pzconfig.py --play
"""

import argparse
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ZOMBOID = Path.home() / "Zomboid"
OPTIONS = ZOMBOID / "options.ini"
CAPS = ZOMBOID / "viewpoint-frame-caps.properties"
GAME_EXE = "ProjectZomboid64.exe"

# profile -> {file: {key: value}}.  None as a value means "remove the key",
# which for the caps file is how you say "follow the monitor's refresh rate".
PROFILES = {
    "play": {
        OPTIONS: {"vsync": "true", "uncappedFPS": "false"},
        CAPS: {"game": None},
    },
    "bench": {
        OPTIONS: {"vsync": "false", "uncappedFPS": "true"},
        CAPS: {"game": "uncapped"},
    },
}

WATCH = {OPTIONS: ("vsync", "uncappedFPS", "frameRate"), CAPS: ("game", "menu")}


def game_running():
    """The game rewrites these files on exit, so editing them mid-session loses
    the edit without saying so. Unknowable counts as running -- the same rule
    pzauto uses, for the same reason."""
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {GAME_EXE}"],
                             capture_output=True, text=True, timeout=20)
        return GAME_EXE.lower() in out.stdout.lower()
    except Exception:
        return True


def read_keys(path, keys):
    found = {k: None for k in keys}
    if not path.exists():
        return found
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        if k.strip() in found:
            found[k.strip()] = v.strip()
    return found


def apply(path, changes, dry=False):
    """Set or remove keys in place. Returns a list of (key, old, new) actually
    changed. Preserves comments, blank lines and the order of everything else."""
    if not path.exists():
        print(f"  !! {path} does not exist -- skipped")
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    done, moves = set(), []

    out = []
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k, _, v = s.partition("=")
            k, v = k.strip(), v.strip()
            if k in changes:
                want = changes[k]
                done.add(k)
                if want is None:
                    moves.append((k, v, "(removed)"))
                    continue                      # drop the line entirely
                if v != want:
                    moves.append((k, v, want))
                    out.append(f"{k}={want}")
                    continue
                out.append(line)                  # already right, keep verbatim
                continue
        out.append(line)

    # Keys that were absent and now need a value get appended.
    for k, want in changes.items():
        if k not in done and want is not None:
            moves.append((k, "(absent)", want))
            out.append(f"{k}={want}")

    if moves and not dry:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.pzconfig-backup-{stamp}")
        shutil.copy2(path, backup)
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
        print(f"  backup -> {backup.name}")
    return moves


def show():
    print(f"  {'file':<34}{'key':<14}{'value'}")
    for path, keys in WATCH.items():
        vals = read_keys(path, keys)
        for k in keys:
            v = vals[k]
            note = ""
            if path is CAPS and k == "game" and v is None:
                note = "   <- absent = the monitor's refresh rate"
            print(f"  {path.name:<34}{k:<14}{v if v is not None else '(absent)'}{note}")
    state = infer()
    print(f"\n  profile: {state}")
    if state == "play":
        print("  Frame time is clamped. pzbench will say CAPPED RUN and refuse to compare medians.")
    elif state == "bench":
        print("  Uncapped. Not a play profile -- the GPU runs flat out for frames nobody sees.")


def infer():
    for name, spec in PROFILES.items():
        if all(read_keys(path, changes.keys()) == {k: v for k, v in changes.items()}
               for path, changes in spec.items()):
            return name
    return "mixed (neither profile exactly)"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--bench", action="store_true", help="vsync off, frame cap off")
    g.add_argument("--play", action="store_true", help="vsync on, cap at refresh rate")
    g.add_argument("--show", action="store_true", help="print the current state")
    ap.add_argument("--dry-run", action="store_true", help="say what would change")
    ap.add_argument("--force", action="store_true",
                    help="edit even while the game is running (it will clobber you on exit)")
    a = ap.parse_args()

    if a.show:
        show()
        return 0

    which = "bench" if a.bench else "play"
    if game_running() and not a.force:
        print(f"  {GAME_EXE} is running (or could not be queried).")
        print("  It rewrites these files from memory when it exits, so the edit would be")
        print("  silently lost. Close the game first, or pass --force if you know better.")
        return 1

    print(f"  -> {which} profile{'  (dry run)' if a.dry_run else ''}")
    total = 0
    for path, changes in PROFILES[which].items():
        moves = apply(path, changes, dry=a.dry_run)
        for k, old, new in moves:
            print(f"  {path.name}: {k}  {old} -> {new}")
        total += len(moves)
    if not total:
        print("  already in that profile, nothing to change")
    elif which == "bench":
        print("\n  Now launch, settle, and confirm the cap actually came off:")
        print("    python pzbench.py captures/<the new capture>")
        print("  A floor still at 6.06 ms means the uncap did NOT take.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
