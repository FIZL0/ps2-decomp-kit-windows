# Agent notes

Rules for whoever does the work here, human or agent. Short on purpose: read this, then the task's own
file, then the files the task names. Nothing else.

This is a re-implementation of a PlayStation 2 game: same behaviour, better rendering, maintainable code.
`kit.toml` says which game. `kit` is the entry point for everything (`.\kit` from the repository root,
unless it is on `PATH`). This is the Windows edition: same commands as the Linux one, a few things done
differently underneath (`WINDOWS.md`). Work in PowerShell 7 — these pages' examples assume it.

## The non-negotiable ones

- **Game data is read from the user's own disc at runtime.** Never commit game data, extracted assets,
  decompiled code, dumps, captures or screenshots. Those live in `context/`, which is git-ignored. If you
  are about to `git add` something under `context/`, stop.
- **No retail addresses or decompiler-generated names in shipped code.** No `FUN_00123456`, no
  `DAT_00456789`, no bare hex addresses in the port. Describe the behaviour instead: a comment saying
  *what* a function does is worth more than one saying where it lived. Addresses belong in
  `research/oracle.txt` and in journal entries — research notes, not product. `kit audit` checks this
  and `kit done` gates on it, so you find out before the commit rather than after the release.
- **Match the original exactly, where it matters.** Simulation behaviour — physics, AI, state machines,
  scoring, timing — is ported, not approximated, and proved against the original. No placeholders, no
  "close enough", no TODO left behind as if it were finished work.
- **Say what you did not verify.** Every journal entry ends with a `## Not verified / not 1:1` section
  listing everything you could not check against the original or could not match exactly, each with its
  reason, or the single word `none`. This is the most important habit in the project. A port where the
  gaps are written down is recoverable; one where they are silent is not, and the person who finds out is
  always someone later with less context than you.

## What has to be exact, and what does not

Decide this per area once, write it here, and then stop relitigating it. A sensible default:

- **Simulation: bit-exact.** Floating-point results must match the original bit for bit, which means
  going through `ps2-float`'s `F`/`Vf` types rather than host `f32`. Enable the float lint in those
  modules so a stray host-float operation cannot creep in.
- **The PS2 libraries are ours.** `ps2-float` (FIZL0/ps2-float: EE FPU, VU0, fdlibm, newlib rand/MT) and
  `ps2-oracle` (FIZL0/ps2-oracle: the R5900 interpreter under `crates/kit-oracle`) live in their own
  repositories, tag-pinned git dependencies shared with other ports. Agents may fix and extend them when a
  port needs it (new op, missing function, wrong rounding): change it there with a test, tag a release,
  bump the tag in every dependent (`crates/game-sim`, `crates/kit-oracle`, the other kit, opentennis) and
  run their tests. Never vendor a copy here; nothing from crates.io either.
- **Rendering: looks right.** Match by eye or within a small tolerance against a screenshot. Do not
  replay the GS's rasterisation to close the last few pixels, and do not open follow-up tasks to chase
  bit-level render differences — note them and move on.
- **Menus and front-end: yours to design.** The original is a reference, not a spec.
- **Presentation: feels right.** Cameras, cutaways, effects, audio cues — match by eye. Just check they
  do not touch the simulation's RNG or its timing.
- **Additions are layered on top.** Mods, widescreen, upscaled textures: with them off, the game must
  still behave exactly like the original, and the replay tests must still pass bit-exact.

## Proving it

Pick the cheapest method that actually proves the thing (`docs/PROVING.md` has the detail):

- a pure function of its arguments and RAM → **the oracle**, over ≥10,000 generated inputs. No emulator,
  runs as a unit test, catches the cases you would never have thought to record.
- timing, frame flow, anything stateful → **a live capture** (`kit record`) compared against the port.
- tables, static data, struct layouts → **a save-state RAM image**. Cheap, and it does not disturb a
  running capture the way a second PINE connection does.
- the whole thing → **the replay harness**: same input and seed in, same per-tick state hash out.

A port with no proof is not done. A port whose proof is "I read the decompilation carefully" is not done
either — that is how you get something that works for two hundred frames.

## Spending context carefully

The whole conversation is re-read every turn, so what you read is what you pay for, every turn, for the
rest of the session. `docs/CONTEXT.md` explains each measure; the short version:

- **Never read a decompilation dump whole.** `kit fn <addr>` for one function, `kit xref <addr>` for
  its neighbours, `kit xref <addr> --similar` to find look-alikes (a `ported` mark means there is Rust
  to copy the shape of), `kit dis` for instructions. A hook blocks the whole-file read.
- Cap long output: `| Select-Object -First 40`, `| Select-Object -Last 40`, `Select-String <pat> <file>`
  (`cmd` has no pipeline filters at all, and PowerShell has no `head`, `tail` or `grep` — the aliases you
  reach for do not exist). Read files by line range. Never dump a whole log.
- `kit check` for tests, not the raw build command — it reduces a build log to one line, and it
  distinguishes "the build broke" from "a test failed", which is the only thing you need to know.
- `kit plan brief <ID>` gives you a task's entire starting context in one call: its line, its own file,
  its journal, the files it names and their recent commits. Use it instead of gathering that yourself.
- Don't grep the repo to find where things live. `PLAN.md`'s code map says.

## Working on the live game

`docs/PCSX2.md` has the detail. The parts that bite:

- **One PINE connection at a time.** A second client while a recorder is polling hangs both. To read
  static values during a capture, read them from a save state instead.
- **Don't touch the emulator while a capture is running.**
- **Never send `select`** on the virtual pad — PCSX2's hotkeys are Select plus a shoulder button.
- Drive multi-step jobs (load state → send input → record → screenshot) as one `kit pcsx2 with -- CMD`
  so you hold the emulator for the whole job and release it afterwards.
- `CAPTURE FAILED` from any tool means the output file is incomplete. Fix the cause and re-record; do not
  use it.
- Close the emulator copy you started when you are done with it: `kit pcsx2 stop`. Never kill it by
  image name (`taskkill /IM pcsx2-qt.exe`) — that is the user's own session too. If you must, find the
  pid with `tasklist /FI "IMAGENAME eq pcsx2-qt.exe"` and `taskkill /PID <pid>`. Don't match on window
  title or command line: that pattern matches the console you typed it in.

## One task at a time

"Continue" means: `kit plan next`, read `kit plan brief <ID>`, do that task completely, write the
journal entry, then `kit done <ID> "<message>"` — which runs the tests, ticks the task, commits and
pushes the branch you are on, in one call. Then stop and report. Don't merge tasks, don't skip ahead. If a task turns out to be too big,
split it into sub-tasks first (`kit plan add`), then do the first one.

**Never merge, rebase or resolve a conflict by hand.** `kit done` commits and pushes the branch it is
on, so the branch of a task whose merge went wrong is on the remote — the one case where a lost branch
costs a whole session. The runner merges your branch when your session ends; a conflict it cannot join
mechanically goes to **a session of its own** that reads both sides and finishes the merge, and then the
runner runs the tests and pushes the merge target. So leave merges and conflicts alone: doing one by hand
races the runner. If a push cannot be made it is one logged line, never a failed task — the commit is
already safe in the local repo and the next one pushes it. `[git] push` is the switch for all of it,
`[git] push_merge = false` for the stricter policy where the merge waits for a person to read it.

## If you get stuck, move on

Never loop on one obstacle. If a tool hangs, the emulator will not cooperate, the same fix fails twice,
something genuinely needs the human, or about twenty minutes pass with no progress:

1. write what happened and what you tried in the task's journal entry,
2. `kit done <ID> "<what is solid>" --blocked "<why + what would unblock it>"`,
3. start the next open task — preferably in a different area.

"The emulator is not running" is not a blocker; start it. A usage limit is not a blocker either; commit
what is solid and wait. Things only a human can do go in `plan/HUMAN.md`, never into the code as a guess.
