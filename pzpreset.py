#!/usr/bin/env python3
"""
pzpreset -- apply named setting overlays to Project Zomboid's config files so an
A/B benchmark run is "apply, play, parse" instead of clicking through a mod UI.

    python pzpreset.py --list
    python pzpreset.py --diff  01-restore-cheap
    python pzpreset.py --apply 01-restore-cheap
    python pzpreset.py --snapshot my-baseline
    python pzpreset.py --restore backups/viewpoint-live.properties.2026-11-04T1450
    python pzpreset.py --show viewpoint

Three things this is careful about, because all three are silent if you get them
wrong:

1. IT WILL NOT RUN WHILE THE GAME IS RUNNING. Both config files are rewritten by
   the game on exit, so an edit made mid-session is simply lost -- and you would
   conclude the setting did nothing. Viewpoint's own file header says the same:
   "Edit it by hand only with the game closed."

2. CRLF AND KEY ORDER ARE PRESERVED. viewpoint-live.properties has 566 keys and
   CRLF endings. Loading it into a dict and writing it back would reorder
   everything and drop the header comments, which turns every future diff into
   noise. So this does a line-oriented edit and touches nothing else.

3. AN UNKNOWN KEY IS AN ERROR, NOT A NO-OP. A typo in a preset would otherwise
   apply cleanly and change nothing, and the benchmark would read as "that
   setting makes no difference".

Every write is backed up first, timestamped, into backups/.
"""

import argparse
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
PRESETS = HERE / "presets"
BACKUPS = HERE / "backups"

ZOMBOID = Path.home() / "Zomboid"
TARGETS = {
    "viewpoint": ZOMBOID / "viewpoint-live.properties",
    "options": ZOMBOID / "options.ini",
}

RE_KV = re.compile(r"^([A-Za-z][\w.+-]*)=(.*)$")


def game_running():
    """True if Project Zomboid is up. Checked with tasklist so this works without
    PowerShell and without elevation."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq ProjectZomboid64.exe", "/NH"],
            capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return None  # unknown -- caller decides
    return "ProjectZomboid64.exe" in out


def read_lines(path):
    """Raw text plus the line ending in use, so we can write it back unchanged."""
    raw = path.read_bytes()
    nl = "\r\n" if b"\r\n" in raw else "\n"
    return raw.decode("utf-8", errors="replace").split(nl), nl


def current_values(path):
    lines, _ = read_lines(path)
    out = {}
    for line in lines:
        m = RE_KV.match(line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def load_preset(name):
    """A preset is 'target: key=value' lines; blank lines and # comments ignored.
    'target' is a key of TARGETS and may be set once as a header for the file."""
    path = PRESETS / (name if name.endswith(".preset") else name + ".preset")
    if not path.exists():
        sys.exit(f"no such preset: {path}")

    target = None
    changes = {}
    notes = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            notes.append(s.lstrip("# ").rstrip())
            continue
        if s.startswith("target:"):
            target = s.split(":", 1)[1].strip()
            if target not in TARGETS:
                sys.exit(f"{path}:{lineno}: unknown target '{target}' "
                         f"(expected one of {', '.join(TARGETS)})")
            continue
        m = RE_KV.match(s)
        if not m:
            sys.exit(f"{path}:{lineno}: not a key=value line: {s!r}")
        changes[m.group(1)] = m.group(2)

    if target is None:
        sys.exit(f"{path}: no 'target:' line -- say which file this preset edits")
    return target, changes, notes


def plan(target, changes):
    path = TARGETS[target]
    if not path.exists():
        sys.exit(f"target file missing: {path}")
    have = current_values(path)

    unknown = [k for k in changes if k not in have]
    if unknown:
        sys.exit("these keys do not exist in %s, so applying them would silently\n"
                 "do nothing. Fix the preset:\n  %s"
                 % (path.name, "\n  ".join(unknown)))

    deltas = [(k, have[k], v) for k, v in changes.items() if have[k] != v]
    already = [(k, v) for k, v in changes.items() if have[k] == v]
    return path, deltas, already


def backup(path):
    BACKUPS.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%dT%H%M%S")
    dest = BACKUPS / f"{path.name}.{stamp}"
    shutil.copy2(path, dest)
    return dest


def apply(path, deltas):
    lines, nl = read_lines(path)
    wanted = {k: v for k, _, v in deltas}
    done = set()
    for i, line in enumerate(lines):
        m = RE_KV.match(line)
        if m and m.group(1) in wanted:
            lines[i] = f"{m.group(1)}={wanted[m.group(1)]}"
            done.add(m.group(1))
    missed = set(wanted) - done
    if missed:
        sys.exit(f"internal error: did not find {sorted(missed)} on a second pass")
    path.write_bytes(nl.join(lines).encode("utf-8"))


def cmd_list():
    if not PRESETS.exists() or not any(PRESETS.glob("*.preset")):
        print("no presets in", PRESETS)
        return
    for p in sorted(PRESETS.glob("*.preset")):
        target, changes, notes = load_preset(p.stem)
        head = notes[0] if notes else ""
        print(f"  {p.stem:<26} {target:<10} {len(changes):>3} key(s)   {head}")


def cmd_diff(name, do_apply):
    target, changes, notes = load_preset(name)
    path, deltas, noop = plan(target, changes)

    print("=" * 74)
    print(f"{name}  ->  {path}")
    for n in notes:
        print(f"  # {n}")
    print("=" * 74)

    if not deltas:
        print("  nothing to change -- already in this state.")
        return

    width = max(len(k) for k, _, _ in deltas)
    for k, old, new in sorted(deltas):
        print(f"  {k:<{width}}  {old}  ->  {new}")
    if noop:
        print(f"\n  ({len(noop)} key(s) already at the target value, left alone)")

    if not do_apply:
        print("\n  dry run. Re-run with --apply to write it.")
        return

    running = game_running()
    if running:
        sys.exit("\nREFUSING: Project Zomboid is running. It rewrites these files on\n"
                 "exit, so this edit would be lost and the benchmark would be wrong.\n"
                 "Close the game and run again.")
    if running is None:
        print("\n  WARNING: could not determine whether the game is running.")

    dest = backup(path)
    apply(path, deltas)
    print(f"\n  backed up to {dest}")
    print(f"  wrote {len(deltas)} change(s) to {path.name}")
    print("\n  Now: launch, go to the same spot in first person, stand still ~3 min,")
    print("  quit, then run pzbench.py on the new DebugLog.")


def cmd_snapshot(name):
    """Capture the current value of every key any preset mentions, as a new preset
    you can always return to."""
    keys = {}
    for p in sorted(PRESETS.glob("*.preset")):
        target, changes, _ = load_preset(p.stem)
        keys.setdefault(target, set()).update(changes)
    if not keys:
        sys.exit("no presets to take the key list from")

    for target, ks in keys.items():
        have = current_values(TARGETS[target])
        out = PRESETS / f"{name}-{target}.preset"
        body = [f"# snapshot of {target} taken {datetime.now():%Y-%m-%d %H:%M}",
                f"target: {target}", ""]
        body += [f"{k}={have[k]}" for k in sorted(ks) if k in have]
        out.write_text("\n".join(body) + "\n", encoding="utf-8")
        print(f"  wrote {out}  ({len(ks)} key(s))")


def cmd_show(target):
    if target not in TARGETS:
        sys.exit(f"unknown target '{target}' (expected one of {', '.join(TARGETS)})")
    path = TARGETS[target]
    have = current_values(path)
    print(f"{path}  ({len(have)} keys)")
    for k in sorted(have):
        print(f"  {k}={have[k]}")


def cmd_restore(which):
    src = Path(which)
    if not src.exists():
        src = BACKUPS / which
    if not src.exists():
        sys.exit(f"no such backup: {which}")
    name = src.name.rsplit(".", 1)[0]
    dest = next((p for p in TARGETS.values() if p.name == name), None)
    if dest is None:
        sys.exit(f"cannot tell which target '{src.name}' belongs to")
    if game_running():
        sys.exit("REFUSING: Project Zomboid is running.")
    again = backup(dest)
    shutil.copy2(src, dest)
    print(f"  current file saved to {again}")
    print(f"  restored {src} -> {dest}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--list", action="store_true", help="list available presets")
    g.add_argument("--diff", metavar="PRESET", help="show what a preset would change")
    g.add_argument("--apply", metavar="PRESET", help="apply a preset (backs up first)")
    g.add_argument("--snapshot", metavar="NAME", help="save current values as a preset")
    g.add_argument("--show", metavar="TARGET", help="print a config file's keys")
    g.add_argument("--restore", metavar="BACKUP", help="restore a backup")
    a = ap.parse_args()

    if a.list:
        cmd_list()
    elif a.diff:
        cmd_diff(a.diff, do_apply=False)
    elif a.apply:
        cmd_diff(a.apply, do_apply=True)
    elif a.snapshot:
        cmd_snapshot(a.snapshot)
    elif a.show:
        cmd_show(a.show)
    elif a.restore:
        cmd_restore(a.restore)
    return 0


if __name__ == "__main__":
    sys.exit(main())
