# research/

Your own notes and scripts. **Committed** — which means nothing game-derived may be in here: no extracted
assets, no decompiled code, no RAM dumps, no captures, no screenshots. All of that goes in `context/`,
which is git-ignored. If you are about to `git add` something from a disc, stop.

What does belong here:

| | |
|---|---|
| `journal/` | one directory per task: what you found, what you measured, what you could not verify |
| `oracle.txt` | names → addresses, so no address appears in the port's source |
| `*.py`, `*.sh` | your own throwaway analysis scripts |
| notes | anything you worked out and want the next session to know |

`research/oracle.txt` is the one file here that other tools read. One entry per line:

```
# name              program   address     notes
walk_ramp           gameplay  0x001000a0  speed ramp; args (speed, stick) in f12/f13
shot_table          gameplay  0x004c1000  48 records of 0x40 bytes

The program column is optional (a game with no overlays has only one program) and worth writing for
every game that has overlays: they share their addresses, so an address with no program beside it is
not an address.
```

Addresses and names are research notes, not game data, so this file is committed. `kit oracle list`
reads it and `kit oracle add <name> <addr>` appends to it; `kit.toml`'s `[symbols]` is the same idea for addresses that
tools need (`@name` works anywhere an address is accepted).

---

## Rebuilding `context/` from scratch

`context/` is disposable. Anyone with the same disc can regenerate all of it, which is why none of it is
committed. In order:

```sh
kit setup                 # kit.toml: game, emulator, Ghidra
kit extract               # the disc's files              → context/iso
kit elf --scan            # work out the overlay layout    (paste into kit.toml)
kit elf                   # one ELF per program            → context/elf
kit ghidra analyse        # SLOW, once per program         → context/ghidra
kit ghidra dump           # the dumps, seconds             → context/decomp
kit probe vsync --save    # the frame counter              (writes kit.toml)
```

Only the save states cannot be rebuilt — they are made by playing. Keep your own copies of slots 3, 4 and
5 somewhere outside the repository, and record what each one holds in `kit.toml`'s `[states]`.

---

## The journal

`research/journal/YYYY-MM-DD-<ID>-<slug>/`, one directory per task. `kit journal new <ID> <slug>`
creates it with the required skeleton; `kit journal state <ID>` says whose move it is.

- One README per task. The newest iteration file says whose move it is: `-FINAL` (finished), `-PART` (an
  agent continues), `-READY` (needs the human).
- Prose and numbers only. Captures and images live in `context/`; reference them by path.
- The last section is always `## Not verified / not 1:1` — every gap with its reason, or the single word
  `none`. `kit done` checks it is there.

Write it for someone with no memory of today, because that is who reads it: a different person, or you in
three months, or a fresh session. The paragraph of plain English explaining what a function does is the
one artefact you cannot regenerate from the disc.

---

## Pitfalls

Collected the hard way. Most of them have a tool that avoids them; these are the ones that bite anyway.

**Never read a decompilation dump whole.** 10–30 MB. `kit fn`, `kit xref`, `kit dis`, `kit data`.
A hook blocks it, and the hook exists because the mistake is one keystroke away.

**An address without its program name is useless.** Overlays share addresses. Write the overlay next to
every address, in the journal and in `oracle.txt`.

**Mirrored addresses.** The same RAM appears at `0x0…`, `0x2…` and `0xa…`. Mask the top bits before
comparing.

**One PINE connection at a time.** A second client while a recorder polls hangs both. Read static values
from a save state instead.

**Never send `select` to the pad.** It is half of every PCSX2 hotkey.

**`CAPTURE FAILED` means the file is rubbish.** Do not analyse it. Fix the cause, record again.

**Region and revision matter.** A PAL disc can have different physics and a different frame rate. Captures
from the wrong release look completely normal.

**Ghidra's C lies about floats.** `madd.s` is not multiply-then-add; `max.s` is not a branch; arithmetic
gets reassociated. Read the instructions for anything whose numbers must match.

**Keep the original's order of operations.** On this FPU, `(a+b)+c` and `a+(b+c)` are different numbers.

**Don't write a new script when a spec will do.** New capture → a watch spec in `watch/`, not a new
program. The project this kit came from accumulated forty-five near-identical capture scripts before
anyone noticed, and by then nobody could say which were still correct.

**Don't delete with a wildcard.** Globbed `rm` triggers confirmation prompts even in permissive modes, and
an unattended run will sit on one until it times out. Write each run to a fresh directory instead.

**Build directories grow without limit.** Tens of gigabytes per worktree. `kit check` caps and cleans;
check the cap in `kit.toml` before a long parallel run.
