# Agent notes

Rules for whoever works here, human or agent. Read this, then the task's brief and the files it names;
look the rest up when needed (`PLAN.md` says where).

A re-implementation of a PlayStation 2 game: same behaviour, better rendering, maintainable code. `kit.toml`
says which game; `kit` is the entry point for everything (`.\kit` from the repository root unless it is on
`PATH`). This is the Windows edition: same commands as the Linux one, a few things done differently underneath
(`WINDOWS.md`). Work in PowerShell 7; examples assume it.

## Hard rules

- **Game data comes from the user's disc at runtime.** Never commit game data, extracted assets, decompiled
  code, dumps, captures or screenshots: they live in git-ignored `context/`. About to `git add` under
  `context/`? Stop.
- **No retail addresses or decompiler names in shipped code** (`FUN_…`, `DAT_…`, bare hex). Describe what
  the code does, not where it lived; addresses go in `research/oracle.txt` and the journal. `kit audit`
  checks, `kit done` gates on it.
- **Simulation ports: spec, then port, in two sessions.** One reads the decompilation and writes the task's
  `SPEC.md` (behaviour in plain words; no code, addresses or offsets); a second writes code and tests from it
  with the decompilation locked (`docs/WORKFLOW.md` *Clean-room ports*).
- **Match the original exactly where it matters.** Simulation (physics, AI, state machines, scoring, timing)
  is ported and proved: no placeholders, no "close enough", no TODO passed off as done.
- **Say what you did not verify.** Every journal entry ends with `## Not verified / not 1:1`: each thing not
  checked or not matched, with its reason, or `none`. The most important habit here: written-down gaps are
  recoverable; silent ones land on someone later with less context.

## What has to be exact

Decide per area once, write it here, stop relitigating. The default:

- **Simulation: bit-exact**, through `ps2-float`'s `F`/`Vf`, never host `f32`; float lint on in those modules.
- **The PS2 libraries are ours** (`ps2-float`, `ps2-oracle`: tag-pinned git dependencies shared with other
  ports): fix them there, with a test and a release, never vendor a copy (`kit plan "PS2 libraries"`).
- **Rendering: looks right** (by eye, or a small tolerance against a screenshot). No replaying GS
  rasterisation, no tasks chasing bit-level render differences: note them, move on.
- **Menus and front-end: yours to design**; the original is a reference, not a spec.
- **Presentation (cameras, cutaways, effects, audio cues): feels right**; must not touch the simulation's RNG
  or timing.
- **Additions (mods, widescreen, upscaled textures) layer on top**: off, the game behaves exactly like the
  original and the replay tests pass bit-exact.

**Proof** — the cheapest that proves it (`kit plan "Which proof"`, `docs/PROVING.md`): pure function →
oracle, ≥10,000 inputs; timing/state → live capture (`kit record`); tables/layouts → save-state RAM image;
the whole → replay harness's per-tick state hash. No proof, or "I read the decompilation carefully", is not done.

## Working cheaply

Every turn re-reads the conversation (`docs/CONTEXT.md`).
- Never read a decompilation dump whole (a hook blocks it): `kit fn <addr>`, `kit xref <addr>`
  (`--similar`: look-alikes; `ported` ones have Rust to copy the shape of), `kit dis`.
- Cap output with `| Select-Object -First 40` / `-Last 40`, `Select-String <pat> <file>` (PowerShell has no
  `head`/`tail`/`grep`; `cmd` no pipeline filters at all). Read files by line range.
- Tests only via `kit check` (one line; build broke vs test failed). While working run only yours
  (`kit check -p <crate> <filter>`): slots are shared; the full suite runs once, in `kit done`.
- `kit plan brief <ID>` is a task's whole starting context. Don't grep for where things live: `plan/CODEMAP.md`.

## The live game

Read `kit plan "Controlling the real game"` before driving it. Always: don't touch the emulator while a
capture runs; one PINE connection at a time (read static values from a save state); never send `select`; multi-step jobs as
one `kit pcsx2 with -- CMD`; `CAPTURE FAILED` = re-record; close yours with `kit pcsx2 stop`, never kill
PCSX2 by image name (`taskkill /IM`) or window title/command line (that matches your own console); if you
must, `tasklist /FI "IMAGENAME eq pcsx2-qt.exe"`, then `taskkill /PID <pid>`.

## A task, start to finish

"Continue": `kit plan next`, `kit plan brief <ID>`, do it completely, `kit journal new <ID> <slug>`,
`kit done <ID> "<message>"` (tests, tick, commit, push this branch). Stop; report what you did and did not
verify. One task at a time, no merging or skipping ahead; too big → split first (`plan/<ID>.md` per part,
`kit plan add`), do the first part.

**Tasks** go through `kit plan` (`PLAN.md`), never by reading `plan/TODO.md` or editing its checkboxes.
**Journal**: `research/journal/YYYY-MM-DD-<ID>-<slug>/`, for a reader with no context: findings, addresses,
measurements, what you could not check. Newest file's state: `-FINAL` done, `-PART` agent continues,
`-READY` needs the human. Captures, screenshots, dumps go in `context/`, never the committed journal.

**Never merge, rebase or resolve a conflict by hand**: it races the runner, which merges when the session
ends and gives a conflict it can't join to a session of its own. A failed push is a logged line, not a
failed task (`kit plan "Pushing and merges"`).

**Stuck? Move on.** A tool hangs, the emulator won't cooperate, the same fix fails twice, it needs the human,
or ~20 minutes pass without progress: stash experiments, write it in the journal, `kit done <ID> "<what is
solid>" --blocked "<why + what unblocks it>"`, take the next task, preferably in another section (obstacles
are often shared within one). A stopped emulator is not a blocker (start it), nor a usage limit (commit
what is solid, wait). Never end a run while open tasks remain; list the blockers at the end. Human-only
things go in `plan/HUMAN.md`, never into code as a guess.
