# Workflow — the loop you stay in

Setup happens once (`SETUP.md`). After that the work is the same shape every time, whichever part of the
game you are on:

> **find it → read it → port it → prove it → write down what you could not prove**

This page walks one task through all five, with the commands.

---

## 0. Take a task

```powershell
kit plan next
kit plan brief T12
```

`brief` gives you the task's line, its own plan file, its journal directory and the files it names — the
same starting context every time, in one call. Read that, then the files it points at. Resist going
further afield; the code map in `PLAN.md` exists so you never have to search.

---

## 1. Find the code

You know what you want in game terms ("how does the walk speed ramp up?"). You need its address.

**From a value you can see.** If the thing is visible in RAM — a position, a timer, a state number —
start from the value:

```powershell
kit probe find --reset                       # pass 1: remember all of RAM
kit probe find --f32 1.5 --near @cam         # ... or just floats near something you know
kit probe find --decreased                   # play a bit, then narrow, repeatedly
kit probe watch 0x3f2a10 --frames 120 --as f32   # how a candidate changes frame to frame
kit probe chase 1c4420 0x10 0x40             # follow a pointer chain hop by hop
```

`find` keeps its candidate list between passes, so narrowing is `--changed` / `--unchanged` /
`--increased` / `--decreased` until a handful are left — the classic value search, which is still the
fastest way to locate a number you can see on screen.

**From a string.** Games leave debug strings, file names and format strings behind. Find the string in
the dump, then find who references it:

```powershell
kit data --strings walk                      # printable runs matching "walk", with addresses
kit xref 0x4c1f20                            # who points at that address
```

**From a sibling.** The best one. If you have already ported something similar, the original's author
almost certainly wrote the rest the same way:

```powershell
kit xref 1000a0 --similar
```

This ranks functions by how much their decompiled shape resembles the one you name, and marks the ones
that already have a port. "Port one, then find its siblings" turns a week of hunting into an afternoon.

**From the call graph.** Once you have one foothold, walk out from it:

```powershell
kit xref 1000a0                              # callers and callees, one line each
```

---

## 2. Read it

```powershell
kit fn 1000a0                                # the decompiled function, by itself
kit dis 1000a0                               # its instructions, when the C is misleading
```

Never open the dumps whole — see `docs/CONTEXT.md` for why, and a hook will stop you anyway.

Read the disassembly whenever floats are involved. Ghidra's output for the PS2 hides exactly the things
that matter: `madd.s` becomes a multiply and an add (it is not — see `docs/PS2.md`), `max.s`/`min.s` can
appear as comparisons, and the decompiler sometimes rewrites a chain of arithmetic in a mathematically
equivalent but bitwise different order. If your port is "nearly right", this is usually why.

Write what the function does, in your own words, in the journal as you go. A paragraph of plain English
per function is the single highest-value artefact of this whole process, and the one you cannot
regenerate later.

---

## Clean-room ports: spec, then port

An agent that reads a function and writes its port in the same session is copying, however it renames
things, and a port that reproduces the original's quirks bit for bit looks exactly like a copy. Nothing
here is a legal opinion, but the usual mitigation is cheap with agents: split the two jobs.

1. **Spec session.** Read the decompilation and write `SPEC.md` in the task's own journal directory: what
   the function does, in your own words; each constant by what it means (and its value); every quirk and
   edge case, because a quirk is behaviour. No code, no addresses, no decompiler names, no offsets or field
   order. End it with `## Open questions` (`none` when there are none), then
   `kit done <ID> "<msg>" --spec`. That commits the spec without tests or a tick; the task stays open.
2. **Port session.** The next brief sees the spec with no open questions and says *Port phase*: it pastes
   the spec in and, in a runner worktree, writes `.cleanroom`, which makes the guard hook refuse the dumps,
   Ghidra and `kit fn/xref/dis` for that session. Write the code from the spec, prove it with the oracle
   and the recordings as usual. Something the spec does not answer goes under its `## Open questions`, then
   `--spec` again, and a spec session answers it.

`kit audit` refuses a `SPEC.md` with an address, offset or decompiler name in it, and any note with a
pasted line of decompiled C. The runner does not count a `--spec` ending as the night's attempt, so the
port phase can run once the spec is merged. In the main checkout the lock is not written (another session
may be researching there): the brief's *Port phase* is the rule, nothing enforces it.

Keep a `CREDITS.md` that says how the port is made and what that does and does not prove
(template: [CREDITS.template.md](CREDITS.template.md)).

---

## 3. Port it

Rules in `AGENT.md`; the ones that come up in every task:

- Simulation arithmetic goes through `ps2-float`, not host `f32`. Enable the float lint in those modules.
- Name things for what they do, not where they were. No `FUN_…`, no addresses in the port. The address
  belongs in `research/oracle.txt` next to a name, and in the journal.
- Match the original's *behaviour*, not its shape: your own structure and names, no dead branches or
  decompiler artefacts. Do not "improve" the algorithm either. An improvement is a behaviour change you
  have not noticed yet.
- Keep the original's evaluation order. `(a + b) + c` and `a + (b + c)` are different numbers on this
  machine.

---

## 4. Prove it

Pick the cheapest method that actually proves this thing — full detail in `docs/PROVING.md`:

```powershell
# a pure function: compare against the game's own code over 10k+ inputs
kit oracle run walk_ramp --inputs 20000

# stateful or timing: record the real game, compare your port's trace against it
kit record watch/player.toml --state 5 --frames 600 --out context/t12/run1.csv
kit record --diff context/t12/run1.csv target/t12-port.csv
kit check --test replay_t12

# a table or a struct layout: pull it out of a save state, no emulator needed
kit state extract 4 --addr @player_table --len 0x400 --as f32
```

Then the test goes in the repo and stays there. A proof you ran once and deleted is not a proof; the next
change to that code is exactly when you want it to run again.

If you genuinely cannot prove something — the behaviour needs a scenario you cannot reach, the function
touches hardware the oracle does not model, the difference is in the renderer and the rules say by-eye is
enough — that is fine. It goes in the journal's last section. What is not fine is a port that is silent
about which parts were checked.

---

## 5. Write it down, then wrap up

```powershell
kit journal new T12 walk-ramp   # directory + stubs with the required sections
# ... fill it in ...
kit done T12 "port the walk speed ramp"
```

`done` runs the tests, ticks the task, commits, and pushes the branch it committed on. If the tests fail
it refuses, and says whether the build broke or a test failed. A push that cannot be made is one line of
output and nothing worse: the commit is already safe here, and the next wrap-up pushes it.
`[git] push = false` turns it off.

It also runs `kit audit` before it commits: every file about to go in is checked for the addresses in
your address file and for Ghidra's `FUN_…`/`DAT_…` placeholder names. Those belong in `research/` and in
journal entries, not in the port — and a rule like that only holds if something checks it. If a hit is
deliberate, `--skip-audit` and say why in the journal.

Report: what you did, how it is proved, what you did not verify. Then stop. The next task is a new
sitting with fresh context, and that is deliberate.

---

## Long and unattended runs

```powershell
kit run once                 # one task, then stop
kit run 3                    # three at a time, each in its own git worktree (mode d: different files)
kit run 3 --mode p           # ... strictly in TODO.md order, files may overlap
kit run --dry-run 3          # what would start now, and why the rest wait
kit stop                     # running tasks finish and merge, nothing new starts
```

The loop is [agent-runner](https://github.com/FIZL0/agent-runner) (install: SETUP.md step 1); its README
covers picking, merging and the idle Stop hook. The kit's settings for it are in `ps2kit/runner.toml`; a
`[agent-runner]` table in `kit.toml` overrides them. What to expect:

- It runs until stopped (only `once` exits by itself). With nothing to start it logs `run idle` and keeps
  re-reading `plan/TODO.md` on the shared branch. A script waiting on a run watches the log
  (`context/notes/run.log`) for `run idle`, not for the process to exit. `kit stop` from another terminal
  stops it gently; Ctrl-C stops it at once.
- It needs `git worktree` and a terminal per agent. With Windows Terminal it opens one tab per slot in a
  window named `agents`; without `wt.exe` each slot gets its own console window, and it says so. A slot
  count of 1, `kit run once` and `--no-tmux` take the sequential path: one session at a time, in this
  checkout.
- Each agent works on `task/<ID>` in its own worktree and build directory, with its own emulator copy
  (`KIT_PCSX2=N`), closed when the session ends. The runner merges each finished branch, runs `kit check`
  on the merge and pushes it.
- Conflicts in files where a line is a whole record (the task list, the journal, the oracle's address
  list) are joined mechanically; the address list refuses a name given two different addresses. Any other
  conflict goes to an agent session in the main checkout (transcript in `context/notes/fixer.log`); one it
  cannot resolve is left for you, with the reason in `context/notes/ledger.md`. `[run] fix_merges = false`
  turns the sessions off.
- Blocked is a result, not a failure: the reason is recorded and the next session can pick it up. A block
  with `--after X` reopens when X lands.
- Build directories grow fast. `kit check` cleans the oldest when the total passes the cap in `kit.toml`.
- Afterwards: `kit plan` to see what moved, and read the journal entries before the diffs.
- The merged `task/<ID>` is dropped from the remote, so `git ls-remote --heads origin` lists the branches
  whose merge did not land. `[git] push_merge = false` keeps merges local until you have read them.
- Closing the Windows Terminal window kills the tabs in it, and with them any session mid-commit. Use
  `kit stop` instead.

If you run more than one emulator copy, each agent needs its own: `$env:KIT_PCSX2 = 2` gives it its own
config directory (PCSX2 launched with `-cfgpath`), its own PINE port, its own pad and its own lock, so
two agents cannot drive the same window. `kit run` sets it per slot; set it per tab if you are driving
copies by hand, never in your PowerShell profile.

---

## The order to do a new port in

If you are starting from nothing, `plan/TODO.md` already contains this as a worked backlog. The shape of
it, and the reasoning:

1. **Disc, ELF, overlays, decompile.** Nothing else is possible until the code is readable at the right
   addresses.
2. **The arithmetic model, with tests.** Before any ported math. If you port physics first and the float
   model second, you will not know which of the two is wrong.
3. **The frame counter and one capture.** The moment you can record the real game you can answer
   questions instead of guessing at them.
4. **The replay harness and the state hash, on an almost empty simulation.** Counter-intuitive, but this
   is the highest-leverage early task: from here on, every port either keeps the hashes matching or
   does not, and you find out in seconds.
5. **The oracle, against one simple function.** Prove the interpreter works while the thing it is testing
   is trivial. Then it is a tool rather than a suspect.
6. **Then the game.** Fixed-point first — the main loop's structure, the tick order, the RNG — then
   motion, then whatever this game is about.

The first five are tooling and feel like a detour. They are the difference between a port that converges
and one that is 90% done forever.
