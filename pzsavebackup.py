"""pzsavebackup -- zip the single-player save after every clean game exit.

Used by `pztail.py --save-backups` and runnable by hand:
    python pzsavebackup.py            back up the most recently played single-player save now

Why: a power cut on 2026-11-13 left chunk files of the last save full of zeros (the OS had not
written them yet), resetting part of the base. A copy taken right after each clean exit means a
crash or power cut costs at most one session.

How: when ~/Zomboid/console.txt ends a session with "GameThread exited." (a clean quit), wait
until the most recently written save under ~/Zomboid/Saves/<mode>/<save> has been quiet for
QUIET_S seconds, then zip it to save-backups/<mode>_<save>/<timestamp>.zip beside this script, keeping
the newest KEEP. Multiplayer saves are skipped (the co-op server keeps its own startup backups).
"""
import json
import os
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

ZOMBOID = Path.home() / "Zomboid"
SAVES = ZOMBOID / "Saves"
CONSOLE = ZOMBOID / "console.txt"
HERE = Path(__file__).resolve().parent
OUT = HERE / "save-backups"
STATE = OUT / ".state.json"
KEEP = 5
QUIET_S = 15
EXIT_LINE = b"GameThread exited."


def log(msg):
    """pztail runs under pythonw (no console), so also keep a small log file."""
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(f"  [save-backup] {msg}", flush=True)
    try:
        OUT.mkdir(parents=True, exist_ok=True)
        with open(OUT / "backup.log", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def newest_sp_save():
    """(mode, save dir) of the single-player save written most recently, or None."""
    best = None
    if not SAVES.is_dir():
        return None
    for mode in SAVES.iterdir():
        if not mode.is_dir() or mode.name.lower() == "multiplayer":
            continue
        for save in mode.iterdir():
            marker = save / "map_t.bin"
            if save.is_dir() and marker.is_file():
                t = marker.stat().st_mtime
                if best is None or t > best[0]:
                    best = (t, mode.name, save)
    return (best[1], best[2]) if best else None


def newest_write(folder):
    newest = 0.0
    for root, _dirs, files in os.walk(folder):
        for n in files:
            try:
                newest = max(newest, os.stat(os.path.join(root, n)).st_mtime)
            except OSError:
                pass
    return newest


def backup(mode, save, why="manual"):
    dest_dir = OUT / f"{mode}_{save.name}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    tmp = dest_dir / f"{stamp}.zip.part"
    final = dest_dir / f"{stamp}.zip"
    files = 0
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for root, _dirs, names in os.walk(save):
            for n in names:
                p = Path(root) / n
                try:
                    z.write(p, p.relative_to(save.parent))
                    files += 1
                except OSError as e:
                    log(f"skipped {p}: {e}")
    with open(tmp, "rb+") as fh:
        os.fsync(fh.fileno())  # make sure the copy itself is really on disk
    tmp.replace(final)
    old = sorted(dest_dir.glob("*.zip"))[:-KEEP]
    for o in old:
        o.unlink(missing_ok=True)
    log(f"{why}: {mode}/{save.name} -> {final.name} ({files} files, {final.stat().st_size / 1e6:.1f} MB; "
        f"{len(old)} old backup(s) removed, newest {KEEP} kept)")
    return final


class SaveBackup:
    """Polled from pztail's loop. Backs up once per clean exit."""

    def __init__(self):
        self.pending_since = None
        self.state = {}
        # a backup cut off by a shutdown leaves a .zip.part; the next clean exit (or this start-up's
        # catch-up, if the last exit was never backed up) makes a complete one
        for part in OUT.glob("*/*.zip.part"):
            try:
                part.unlink()
                log(f"removed unfinished backup {part.parent.name}/{part.name}")
            except OSError:
                pass
        try:
            self.state = json.loads(STATE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass

    def _save_state(self):
        OUT.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(self.state), encoding="utf-8")

    def poll(self):
        try:
            st = CONSOLE.stat()
        except OSError:
            return
        key = f"{st.st_mtime_ns}:{st.st_size}"
        if self.pending_since is None:
            if key == self.state.get("done"):
                return
            with open(CONSOLE, "rb") as fh:
                fh.seek(max(0, st.st_size - 4096))
                tail = fh.read()
            if EXIT_LINE not in tail:
                return
            self.pending_since = time.time()
            self.pending_key = key
        found = newest_sp_save()
        if found is None:
            self.state["done"] = self.pending_key
            self._save_state()
            self.pending_since = None
            return
        mode, save = found
        written = newest_write(save)
        if time.time() - written < QUIET_S:
            return  # still being written
        tag = f"{mode}/{save.name}"
        if self.state.get("written", {}).get(tag) == written:
            log(f"{tag} unchanged since its last backup (a co-op session?): skipped")
        else:
            try:
                backup(mode, save, why="after clean exit")
                self.state.setdefault("written", {})[tag] = written
            except Exception as e:  # never take pztail down
                log(f"FAILED: {e!r}")
        self.state["done"] = self.pending_key
        self._save_state()
        self.pending_since = None


if __name__ == "__main__":
    found = newest_sp_save()
    if not found:
        sys.exit("no single-player save found")
    backup(*found)
