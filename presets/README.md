# Presets

Example overlays for `pzpreset.py`, from one PC's tuning of Build 42 with the
Viewpoint mod and its True Weathers shader pack. They form a ladder: apply one, bench
it, then decide on the next. Each file's header says what it is for and what was
measured when it was written.

**The method carries over to another PC. The numbers in the headers may not.** They
belong to the machine they were measured on, and where a header says "on this
machine" it means that one.

Before you apply anything:

```
python pzpreset.py --snapshot my-baseline   # your own way back, taken first
python pzpreset.py --diff 01-restore-cheap  # what it would change, key by key
```

An unknown key is a hard error, not a silent no-op, so a preset written against a
different Viewpoint version fails loudly rather than appearing to do nothing. Every
write is backed up to `backups/` first.

| Preset | Target | What it does |
|---|---|---|
| `01-restore-cheap` | viewpoint | spends GPU headroom on passes that each cost under 0.5 ms |
| `02-mainthread` | options | base-game main-thread levers: `maxActiveRagdolls` 20, `uiRenderFPS` 60 |
| `03-sky-and-light` | viewpoint | held in reserve: both keys land in the most expensive pass |
| `04-high-options` | options | `bPerfReflections=true`: on the PC these came from, the one vanilla quality option not already at maximum |
| `04-high-viewpoint` | viewpoint | deliberately over budget, to be scaled back from the per-pass table |
| `05-fidelity-viewpoint` | viewpoint | visual draw distance, which costs main-thread time |
| `06-interactivity-viewpoint` | viewpoint | how far out the world simulates, not just draws |

Apply `01` and `02` separately: one changes GPU work and the other main-thread work,
and with both in one run neither result can be attributed.
