# Needs the human

Work no agent can do: it needs the disc, the hardware, an account, a controller in someone's hands, or a
decision about what this port should be.

**Agents add items here instead of guessing.** If a task cannot be finished without one of these, write the
item (what is needed, what it unblocks, and exactly how to produce it), block the task with
`kit plan block <ID> "needs <item> — plan/HUMAN.md"`, and move on. A guess in `kit.toml` or in a ported
constant is far more expensive than a blocked task: it looks like work and it is wrong.

Write each item so it can be done without re-reading the whole project: the slot or file name to save to,
how long it takes, and what to do if it goes wrong.

## Before anything works (Windows)

Four of these need an administrator, which is why they are here rather than in a task: an agent running
unattended cannot answer a UAC prompt, and a run that stops on one is a wasted session.

- [ ] **Dump the disc you own** to `game.iso` in the repo root (never committed; `.gitignore` has it). Note
      the serial, the build date and the file's SHA-256 in the journal — every address in this repo is only
      true of that one dump, and a different region's disc is a different game.
- [ ] **A PS2 BIOS from your own console**, in `%USERPROFILE%\Documents\PCSX2\bios\`. The kit will not
      download one and cannot run anything live without it.
- [ ] **PCSX2 2.x installed**, with the full path to `pcsx2-qt.exe` in `kit.toml` `[emu] pcsx2` (an
      absolute path, not a name on `PATH`), and able to boot the ISO by hand once before any agent drives
      it. Approve the Windows Firewall prompt on that first run, for private networks — PINE listens on a
      local TCP port and the kit cannot reach it otherwise.
- [ ] **A JDK 21 or newer** (`winget install Microsoft.OpenJDK.21`), because Ghidra's Windows package does
      not include one and will not start without it.
- [ ] **Ghidra with the EmotionEngine processor extension**, installed somewhere you own (`C:\ghidra`, not
      `C:\Program Files`), path in `kit.toml` `[ghidra] home`. Without it there is no decompilation and most
      of the backlog is impossible.
- [ ] **The ViGEmBus driver** (admin, and SmartScreen will warn about it) if anything is to press buttons:
      it is a kernel driver, so no agent can install it. `pip install vgamepad` afterwards. Optional —
      everything static works without it.
- [ ] **Long paths enabled** (admin): the `LongPathsEnabled` registry value plus
      `git config --global core.longpaths true`. `SETUP.md` has both commands. Ghidra project files and
      worktree build directories go past 260 characters, and the failures when they do are confusing
      rather than obvious.

## Save states and recordings

Agents can load states and replay recordings, but only a person can play the game to the interesting
moment. Save states go in PCSX2's slots; by convention slots 3, 4 and 5 are the human's and are never
overwritten, 8 and 9 are scratch for agents.

- [ ] **The title screen** (slot 3) and **the first frame of actual play** (slot 4), with a line here saying
      what each holds: characters, difficulty, any options that change the simulation.
- [ ] **A mid-action state** (slot 5) in the middle of the thing this game is about, so a capture does not
      have to start from a loading screen.
- [ ] **One longer recording of real play** (a few minutes, PCSX2 input recording) for the acceptance test:
      start from the slot-4 state, play normally, include whatever is rare (a failure, a special move, a
      mode change). Save it under `context/recordings/` and note which state it starts from and which
      controller ports were used — a recording whose save state is lost is worthless.

## Decisions only you can make

- [ ] **What has to be exact, and what only has to look right.** The default in `AGENT.md` is: simulation
      exact, rendering by eye, front end free. Confirm or change it per area, once, and write it there so
      nobody relitigates it per task.
- [ ] **What this port is for** (a faithful remaster, a mod platform, a research record). It changes which
      tasks are worth doing, and in which order.
