# PLAN — how work gets done here

`AGENT.md` has the rules. This file has the mechanics: how a task goes from the list to a commit, and
where everything lives so you never have to search for it.

## Continue

"Continue" is one task, start to finish:

```powershell
kit plan next                 # the first open task
kit plan brief T7             # its line, its own file, its journal, the files it names
                                # ... do the work ...
kit check                     # tests
kit journal new T7 walker     # the entry, including what you could not verify
kit done T7 "port the walker" # tests again, tick the task, commit, push
```

Then stop and report what you did and what you did not verify. One task per iteration. Don't merge two
tasks because they look related, and don't skip ahead to a more interesting one.

If a task turns out to be bigger than one sitting, split it before starting: write `plan/<ID>.md` files
for the parts, `kit plan add` them, then take the first.

## The task list is a tool, not a file

Don't read `plan/TODO.md` and don't edit its checkboxes by hand. It is a plain-markdown file so a human
can skim it, but every change goes through the command, which keeps the format consistent and the archive
in step:

| Command | Does |
|---|---|
| `kit plan` | the index: sections and how many are open in each |
| `kit plan next` | the first open task |
| `kit plan T7` | that task: its own file if it has one, else its line |
| `kit plan brief T7` | everything you need to start it, in one call |
| `kit plan tick T7` | done → moves it to `plan/DONE.md` |
| `kit plan block T7 "<why + what unblocks it>"` | blocked, with the reason recorded |
| `kit plan unblock T7 [note]` | un-blocked |
| `kit plan add T "text" [@ where]` | a new task, next free ID in that prefix |
| `kit plan move T7 top\|before T3\|after T3\|in <section>` | reprioritise |
| `kit plan note T7 "<text>"` | add a line to its file |
| `kit plan newid T` | the next free ID in a prefix |
| `kit plan archive` | sweep finished lines into `plan/DONE.md` (after merges or hand edits) |

`brief` is the one that matters. It assembles the task's line, its plan file, its journal directory (the
newest entry pasted in, capped), and each file the task names with its length and last few commits. One
call instead of eight, and you start with the same context every time.

Why a tool and not a file: a task list you read is a task list you read *all of*, every turn, for the
rest of the session. The list is a few hundred lines; `kit plan next` is three.

## The journal

Every task gets an entry in `research/journal/YYYY-MM-DD-<ID>-<slug>/`. This is the project's memory: what you
found, what the original does, which addresses you used, what you measured, and — the important part —
what you could not check.

- One README per task. Newest iteration file names whose move it is: `-FINAL` (done), `-PART` (an agent
  continues), `-READY` (needs the human).
- `kit journal new <ID> <slug>` creates the directory and stubs with the required sections;
  `kit journal state <ID>` answers "whose move is it" and `kit journal finish` closes an entry.
- Captures, screenshots and dumps go in `context/`, **never** in the journal — the journal is committed.
- The last section is always `## Not verified / not 1:1`, with a reason per item, or `none`. If this
  section is missing, `kit done` tells you so.

The journal is why a project like this can survive being picked up months later, or by someone else, or
by an agent with no memory of yesterday. Write it for that reader.

## Wrap-up

```powershell
kit done T7 "port the walker"                  # tests → tick → commit, one call
kit done T7 "partial: tables read" --blocked "needs a save state in the second stage"
```

It runs the test command, runs the float lint if the port has one, refuses to commit if the tests fail,
ticks or blocks the task, commits everything including the journal entry, and pushes the branch it
committed on. One call, so a wrap-up cannot be half-done — the state where the code is committed but the
task is still open is the one that confuses the next session most, and the state where it is committed but
only on this disk is the one that loses work outright.

Pushing your own branch is nobody's manual step: `kit done` pushes the branch you are on when it is a
task's own (an agent's `task/<ID>` from its worktree, a topic branch here), and a `kit run` pushes the
branch of any task whose merge it had to leave for a human. Publishing the **shared** branch stays a
decision somebody makes: neither a wrap-up nor a run pushes it, and a wrap-up in the main checkout prints
the one-line `git push` instead. A merged `task/<ID>` is dropped from the remote, but only once the remote
actually holds the merge — before that, the branch is the only copy of that work the remote has. A remote
that has moved is merged in first, never rebased, because the run's worktree branches are based on those
commits. A push that fails is a logged line and nothing more. Turn it off with `[git] push = false`.

## Code map

Keep this current. It exists so nobody greps the repo to find where something lives.

### The kit

`ps2kit/` — the tooling. All of it generic; see `docs/INTERNALS.md` for the contract.

- `config.py` reads `kit.toml` (the only game-specific file) · `platform_.py` the whole OS surface
  (the only OS-specific file: processes, sockets, window capture, terminals) · `cli.py` dispatch
- the launcher: `kit.cmd` and `kit.ps1` in the repository root, either of which is `kit <command>`.
  `pad.py` is the second place with a Windows branch, because the virtual controller is a driver rather
  than a device file. Those three are the whole Windows surface; everything else is shared with the
  Linux edition, byte for byte — see `WINDOWS.md`.
- static: `disc.py` ISO 9660 · `elf.py` PS2 ELFs and overlay ELFs · `ghidra.py` headless analysis ·
  `decomp.py` read the dumps one function at a time
- live: `pine.py` PCSX2 memory · `pad.py` virtual controller · `emu.py` launch/stop/screenshot/states ·
  `probe.py` find addresses · `record.py` declarative captures
- workflow: `plan.py` · `journal.py` · `checks.py` · `runner.py` unattended runs · `oracle.py` ·
  `setup.py` setup and doctor · `hooks/` the context guards

`watch/` — capture specs (`*.toml`), one per thing you record. `research/` — your own scripts and notes,
committed, no game data. `context/` — everything game-derived, git-ignored.

### The port

`crates/` by default, but this is yours to shape. What comes with the kit:

- `ps2-float` (FIZL0/ps2-float, a tag-pinned git dependency) — the PS2's arithmetic: EE FPU
  add/sub/mul/div/sqrt/madd, VU0 macro ops, the C library's transcendentals, newlib `rand()`/MT19937.
  Everything in the simulation goes through this.
- `ps2-oracle` (FIZL0/ps2-oracle, likewise) — a small R5900 interpreter that runs one function from the
  game's own code so a test can compare your port against it over thousands of inputs. `crates/kit-oracle`
  wires it to `kit.toml` (disc, serial, overlays, address list) and is what tests and `kit oracle` use.
- `game-sim` — where your port goes. Starts nearly empty, with the replay harness and the state hash,
  because those two are what keep everything after this honest.

As you add modules, list them here with one line each, the way `ps2kit/` is listed above. A code map that
is a sentence per file is worth more than a directory listing, and it is the cheapest context in the
project.

## Unattended runs

```powershell
kit run once                  # one task, then stop
kit run 3                     # three agents at a time, each in its own worktree
kit run 3 --mode d            # ... picking tasks that touch different files
```

Each agent gets a git worktree on a `task/<ID>` branch, so three can build and test without fighting over
one target directory. Tasks are picked to avoid working on the same files.

Each agent also needs a terminal. With Windows Terminal installed, `kit run` opens one `wt.exe` tab per
agent in a single window; without it, one console window per agent. `kit run once`, one slot and
`--no-tmux` all mean the sequential path: one session at a time in this checkout. Don't close the window
on a run — a session killed mid-commit leaves a task neither ticked nor blocked. See `docs/WORKFLOW.md`
for what to expect from a long run and how to pick up the pieces afterwards.

## When blocked

Stalled means: a tool hangs, the emulator will not cooperate, the same fix fails twice, something needs a
human, or twenty minutes pass with nothing to show. Then: stash the experiments, write what happened in
the journal, `kit done <ID> "<what is solid>" --blocked "<why + what unblocks it>"`, and take the next
open task — preferably in a different section, since the obstacle is often shared within one.

Never end a run while open tasks remain. At the end, list the blockers. Human-only items go in
`plan/HUMAN.md` — never into the code as a guess.
