# REFERENCE — background, not rules

The rules are in `PLAN.md` and `AGENT.md`; the backlog is `plan/TODO.md`. This file is the things you look
*up*: how to drive the emulator, what the save states hold, which proof suits which code, and what is known
to be missing. Where `docs/` already explains something, this points at it rather than repeating it — a
second copy of an explanation is a second copy to get out of date.

| Looking for | Read |
|---|---|
| How a task is done, start to finish | `docs/WORKFLOW.md` |
| Proving a port against the original | `docs/PROVING.md` |
| PCSX2, PINE, states, the pad | `docs/PCSX2.md` |
| Ghidra, dumps, finding functions | `docs/GHIDRA.md` |
| PS2 hardware: EE, VU, GS, the FPU | `docs/PS2.md` |
| What lives in `context/` | `docs/CONTEXT.md` |
| When a tool misbehaves | `docs/TROUBLESHOOTING.md` |
| How the kit's own modules fit together | `docs/INTERNALS.md` |

## Controlling the real game

Everything live goes through `kit`, never through PCSX2's own UI, because the kit's copies have their own
config, their own PINE slot and their own lock:

    kit pcsx2 start            launch this copy and boot the ISO
    kit pcsx2 status           is it up, what is it running, which PINE slot
    kit pcsx2 stop             close this copy (waits for anything mid-use)
    kit pcsx2 with -- CMD…     hold the emulator for a whole multi-step job
    kit pine save 9 / load 9   scratch save states
    kit pine read <addr> …     read RAM; `watch` follows a value per frame
    kit pad send "press cross 120"   press buttons on the running game (`kit pad serve` first)
    kit shot out.png           screenshot of the emulator window
    kit record …               a per-frame capture to CSV (see `docs/PCSX2.md`)

Two rules that cost real time when broken:

- **One PCSX2 per agent.** `$env:KIT_PCSX2 = N` selects copy N and every kit tool follows it — config
  directory (`-cfgpath`), PINE port (`pine_slot + N`), pad pipe, lock. `kit run` sets it per slot; set it
  per terminal tab, not in your profile. Two agents on one copy will load states into each other's game
  and both will conclude the game is haunted.
- **Never kill PCSX2 by image name.** `taskkill /IM pcsx2-qt.exe` takes out the other agents' copies and
  the user's own session; a `/FI "WINDOWTITLE eq …"` filter can take out the console that ran it (see the
  comment in `ps2kit/hooks/guard.py`). `kit pcsx2 stop` ends yours. If you really need a pid,
  `tasklist /FI "IMAGENAME eq pcsx2-qt.exe"` then `taskkill /PID <pid>`.

A multi-step job (load a state, press buttons, record, read RAM) must run as **one** command under
`kit pcsx2 with -- …` so it holds the lock for the whole job (`kit pcsx2 with -- pwsh -File .\job.ps1`
for anything longer than one line); a sequence of separate calls can be
interleaved with another agent's and the capture will be nonsense. Each call also honours
`KIT_PCSX2_LOCKED=1` so nothing deadlocks against its own parent.

Launching is never a blocker: if nothing is running, start it. "I could not capture because the emulator was
not up" is a missing `kit pcsx2 start`, not a reason to stop.

## What each save state holds

Fill this in as the states are made (`plan/HUMAN.md` asks for them) — a state nobody can describe gets
reloaded by three different agents who each work out what it is.

| Slot | What it holds | Whose |
|---|---|---|
| 3 | title screen | the human's: load only, never overwrite |
| 4 | first frame of play | the human's: load only, never overwrite |
| 5 | mid-action | the human's: load only, never overwrite |
| 8, 9 | scratch | anyone's; assume they will be overwritten |

Note per state: the characters/options chosen, which controller ports are in use, and the frame of the
vsync counter it was saved on. A recording is only usable with the state it was recorded from, so keep them
named in pairs under `context/recordings/`.

## Which proof for which code

`docs/PROVING.md` is the long version. The short one:

| The code is… | Prove it with |
|---|---|
| a pure function of its arguments and RAM (math, a table lookup, a decision) | the **oracle** — ≥10k generated inputs, bit-exact |
| per-frame behaviour over time (a state machine, movement, timing) | a **capture** of the real game, compared per frame |
| one-off structure (a table layout, a loader) | a **save state** dump parsed against the documented layout |
| a whole subsystem under real input | the **replay harness** and the state hash |
| rendering | a **screenshot** beside `kit shot`, by eye |

Oracle runs are cheap; captures are not. If a function can be proved with the oracle, do that rather than
spending an hour on the emulator — and if it cannot, say in the journal why not, because that sentence is
usually where the next person starts.

## Known gaps

Keep this list short and honest: things the port does not do, or does differently, with the reason. It is
the first thing to read when something behaves oddly, and the first thing to update when a gap closes.
Every journal entry's `## Not verified / not 1:1` section feeds it; promote anything that will outlive its
task.

- Nothing yet — the port has not started. (When you add one: what differs, why, and what would close it.)

## Running a long session unattended

`kit run` keeps N agents going through `plan/TODO.md` (see `docs/WORKFLOW.md` → *Long and unattended
runs*, and the module docstring in `ps2kit/runner.py` for the picking rules). What matters when reading the
results in the morning:

- Each task got **one** attempt. A task that comes back blocked has a reason on its line.
- A session that only hit the usage limit was discarded, not counted — its task is still open.
- A branch that could not be merged is still there as `task/<ID>`, with the reason in `context/notes/run.log`.
- `context\notes\slotN.log` is slot N's session output; `kit plan` shows what landed.
- Each slot gets a Windows Terminal tab if `wt.exe` is installed, its own console window if not, and the
  run falls back to one sequential session if neither is available. Closing the window ends the sessions
  in it.

The hooks in `.claude/settings.json` are what keep those sessions cheap: `ps2kit/hooks/guard.py` refuses the
expensive mistakes (reading a dump whole, polling a background task, a kill that matches on command line
or window title and so matches the caller) and
`ps2kit\hooks\inbox.py` (both wired up in `.claude\settings.json`, run with `py -3`) hands over anything you wrote into `context/inbox.md` while they were working —
write `- [ ] <ticket>` lines there any time, and the next agent to finish a tool call picks them up.
