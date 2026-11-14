#!/usr/bin/env python3
"""
pzlinkcheck -- find the calls in Java mods that a game update has broken, before they
crash.

    python pzlinkcheck.py                      # the Java mods the last game loaded
    python pzlinkcheck.py --console <log>      # ... read from another console.txt or DebugLog
    python pzlinkcheck.py <jar> [<jar> ...]    # these jars
    python pzlinkcheck.py --game <dir> ...     # the game is not in Steam's default folder

WHY THIS EXISTS
---------------
A Java mod (loaded through ZombieBuddy) is compiled against one build of the game. Each
call it makes is recorded in its jar by exact name AND exact type signature. When an
update changes either one, for example a method that used to return one list type now
returning another, the jar still loads and nothing complains. The JVM only looks the call
up the first time that line runs. Then it throws NoSuchMethodError, NoSuchFieldError or
NoClassDefFoundError, and if the mod does not catch it, whatever called the mod goes down
with it.

Build 42.22 did exactly this. It removed zombie.util.list.PZArrayList, and
IsoGridSquare.getObjects() now returns java.util.List. A Viewpoint add-on compiled before
that made Viewpoint disable itself the moment its 3D view built its first chunk.

WHAT IT CHECKS
--------------
Every field and method reference each jar makes into a class it does not define itself.
The reference is checked against the game's projectzomboid.jar, ZombieBuddy.jar, and every
other jar in the same run, so a Viewpoint add-on calling into Viewpoint is checked too.
For each one:
- the class must exist (if its package exists, a missing class is a removal, not a typo);
- the member must exist with that exact descriptor, declared or inherited;
- no class named in the descriptor may be missing.
When the name exists with a different descriptor, the new one is printed: that is a
signature change, and usually the mod only needs recompiling.

Verified 2026-11-14 on 42.22.2. It reported the call that was crashing Viewpoint. It also
reported the missing classes and methods that True Ballistics' own log names when it
disables itself. Only then were its other results trusted.

WHAT IT CANNOT TELL YOU
-----------------------
- Whether a broken reference ever runs, or is caught when it does. A broken call inside a
  try/catch costs one feature and gets logged, so grep the console for the mod's name.
  An uncaught one crashes the caller. Read the mod's log lines to tell the two apart.
- ZombieBuddy patch targets. Those are named in annotations as strings, not as
  references. ZombieBuddy logs its own "[!] Method ... not found" for them.
- Anything reached by reflection, or called from Lua.
- Members inherited from a JDK class other than java.lang.Object. The JDK is not indexed,
  so these are counted as "not verifiable here" and not reported as broken.

Standard library only.
"""
import argparse
import os
import re
import struct
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

STEAM_GAME = Path(r"C:\Program Files (x86)\Steam\steamapps\common\ProjectZomboid")
CONSOLE = Path.home() / "Zomboid" / "console.txt"
OBJECT_METHODS = {"getClass", "hashCode", "equals", "clone", "toString",
                  "notify", "notifyAll", "wait", "finalize"}


def parse(data):
    """The parts of a class file this needs: name, supertypes, members, references out."""
    off = 8
    (n,) = struct.unpack_from(">H", data, off); off += 2
    cp = [None] * n
    i = 1
    while i < n:
        tag = data[off]; off += 1
        if tag == 1:                                    # Utf8
            (ln,) = struct.unpack_from(">H", data, off); off += 2
            cp[i] = (1, data[off:off + ln].decode("utf-8", "replace")); off += ln
        elif tag in (3, 4):                             # Integer, Float
            off += 4
        elif tag in (5, 6):                             # Long, Double take two slots
            off += 8; i += 1
        elif tag in (7, 8, 16, 19, 20):                 # Class, String, MethodType, Module, Package
            cp[i] = (tag, struct.unpack_from(">H", data, off)[0]); off += 2
        elif tag in (9, 10, 11, 12, 17, 18):            # Field/Method/InterfaceMethod ref, NameAndType, (Invoke)Dynamic
            cp[i] = (tag,) + struct.unpack_from(">HH", data, off); off += 4
        elif tag == 15:                                 # MethodHandle
            off += 3
        else:
            raise ValueError("constant pool tag %d" % tag)
        i += 1
    utf = lambda k: cp[k][1]
    cls = lambda k: utf(cp[k][1])
    _, this, sup = struct.unpack_from(">HHH", data, off); off += 6
    (ni,) = struct.unpack_from(">H", data, off); off += 2
    ifaces = [cls(struct.unpack_from(">H", data, off + 2 * j)[0]) for j in range(ni)]
    off += 2 * ni

    def members():
        nonlocal off
        (count,) = struct.unpack_from(">H", data, off); off += 2
        out = set()
        for _ in range(count):
            _, name, desc, attrs = struct.unpack_from(">HHHH", data, off); off += 8
            out.add((utf(name), utf(desc)))
            for _ in range(attrs):
                (ln,) = struct.unpack_from(">I", data, off + 2); off += 6 + ln
        return out

    fields, methods = members(), members()
    refs = []
    for e in cp:
        if e and e[0] in (9, 10, 11):
            nt = cp[e[2]]
            refs.append(("field" if e[0] == 9 else "method", cls(e[1]), utf(nt[1]), utf(nt[2])))
    return {"name": cls(this), "super": cls(sup) if sup else None, "ifaces": ifaces,
            "fields": fields, "methods": methods, "refs": refs}


def classes(jar):
    with zipfile.ZipFile(jar) as z:
        for n in z.namelist():
            if n.endswith(".class") and not n.startswith("META-INF/"):
                try:
                    yield parse(z.read(n))
                except Exception as ex:
                    print("  (could not read %s in %s: %s)" % (n, os.path.basename(jar), ex))


class Index:
    def __init__(self):
        self.classes, self.packages = {}, set()

    def add(self, jar):
        for c in classes(jar):
            self.classes.setdefault(c["name"], c)
            self.packages.add(c["name"].rsplit("/", 1)[0])

    def ours(self, name):
        """A class this index should hold: its package is one the index knows."""
        return "/" in name and name.rsplit("/", 1)[0] in self.packages

    def missing_in(self, desc):
        return [t for t in re.findall(r"L([^;]+);", desc) if t not in self.classes and self.ours(t)]

    def resolve(self, owner, kind, name, desc, seen=None):
        """True found; False absent; None the search left the index through a JDK class."""
        seen = seen if seen is not None else set()
        if owner in seen:
            return False
        seen.add(owner)
        c = self.classes.get(owner)
        if c is None:
            return None
        if (name, desc) in (c["fields"] if kind == "field" else c["methods"]):
            return True
        outside = False
        for s in ([c["super"]] if c["super"] else []) + c["ifaces"]:
            if s == "java/lang/Object":
                if kind == "method" and name in OBJECT_METHODS:
                    return True
                continue
            r = self.resolve(s, kind, name, desc, seen)
            if r:
                return True
            if r is None:
                outside = True
        return None if outside else False


def check(jar, index):
    own = {c["name"]: c for c in classes(jar)}
    broken, checked, unverifiable = defaultdict(set), 0, 0
    for c in own.values():
        user = c["name"].rsplit("/", 1)[-1]
        for kind, owner, name, desc in c["refs"]:
            if owner.startswith("[") or owner in own:
                continue
            if owner not in index.classes:
                if index.ours(owner):
                    broken[("class", owner, "", "")].add(user)
                continue
            checked += 1
            gone = index.missing_in(desc)
            if gone:
                broken[(kind, owner, name, desc + "   (names missing class " + ", ".join(gone) + ")")].add(user)
                continue
            r = index.resolve(owner, kind, name, desc)
            if r is None:
                unverifiable += 1
            elif r is False:
                pool = index.classes[owner]["fields" if kind == "field" else "methods"]
                now = sorted(d for n, d in pool if n == name)
                note = "   now: " + ", ".join(now) if now else "   (no member of that name)"
                broken[(kind, owner, name, desc + note)].add(user)
    return checked, unverifiable, broken


def jars_from_console(path):
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    seen = []
    for m in re.finditer(r"added to classpath: (.+?\.jar)", text):
        p = m.group(1).strip()
        if p not in seen:
            seen.append(p)
    return seen


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[1].strip())
    ap.add_argument("jars", nargs="*", help="jars to check (default: the ones the last game loaded)")
    ap.add_argument("--console", default=str(CONSOLE), help="console.txt or DebugLog to read loaded jars from")
    ap.add_argument("--game", default=str(STEAM_GAME), help="the game's install folder")
    a = ap.parse_args()

    jars = a.jars or jars_from_console(a.console)
    if not jars:
        sys.exit("no jars given, and none named in %s (ZombieBuddy logs 'added to classpath:')" % a.console)
    game = Path(a.game)
    index = Index()
    for base in ("projectzomboid.jar", "ZombieBuddy.jar"):
        if (game / base).exists():
            index.add(str(game / base))
    if "zombie/iso" not in index.packages:
        sys.exit("projectzomboid.jar not found under %s; pass --game" % game)
    present = [j for j in jars if os.path.exists(j)]
    for j in jars:
        if j not in present:
            print("  (gone since that session: %s)" % j)
    for j in present:
        index.add(j)

    total = 0
    for j in present:
        checked, unverifiable, broken = check(j, index)
        extra = ", %d not verifiable here" % unverifiable if unverifiable else ""
        print("%-44s %5d references checked, %d broken%s" % (os.path.basename(j), checked, len(broken), extra))
        for (kind, owner, name, desc), users in sorted(broken.items()):
            what = owner if kind == "class" else "%s.%s %s" % (owner, name, desc)
            print("    %-6s %s" % (kind, what))
            print("           used by: %s" % ", ".join(sorted(users)))
        total += len(broken)
    print("%d broken reference(s) in %d jar(s)" % (total, len(present)))
    sys.exit(1 if total else 0)


if __name__ == "__main__":
    main()
