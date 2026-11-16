# Mechanics — look up, don't read whole

The detail behind `AGENT.md` and `PLAN.md`. `kit plan "<section>"` prints one section.

## Continue

One task, start to finish (the rules: `AGENT.md` *A task, start to finish*):

```powershell
kit plan next                 # the first open task
kit plan brief T7             # its line, own file, journal, the files it names
kit check                     # tests
kit journal new T7 walker     # the entry, including what you could not verify
kit done T7 "port the walker" # tests, tick, commit, push
```

## Task commands

| Command | Does |
|---|---|
| `kit plan` | the index: sections and how many are open in each |
| `kit plan next` | the first open task |
| `kit plan T7` | that task: its own file if it has one, else its line |
| `kit plan brief T7` | everything you need to start it, in one call |
| `kit plan tick T7` | done → moves it to `plan/DONE.md` |
| `kit plan block T7 "<why + what unblocks it>"` | blocked, with the reason recorded |
| `kit plan unblock T7 [note]` | un-blocked |
| `kit plan add T "text" --files a.rs,dir/** [--after X] [--test name] [@ where]` | a new task, next free ID in that prefix; fields, never prose, say its files and what it waits on |
| `kit plan move T7 top\|before T3\|after T3\|in <section>` | reprioritise |
| `kit plan note T7 "<text>"` | add a line to its file |
| `kit plan newid T` | the next free ID in a prefix |
| `kit plan archive` | sweep finished lines into `plan/DONE.md` (after merges or hand edits) |
| `kit plan "<heading>"` | one section of the plan files, `AGENT.md`, `PLAN.md`, `plan/CODEMAP.md` or this file |

`brief` is the one that matters. It assembles the task's line, its plan file, its journal directory (the
newest entry pasted in, capped), and each file the task names with its length and last few commits. One
call instead of eight, and you start with the same context every time.

`plan/TODO.md` is plain markdown so a human can skim it, but every change goes through the command. Why a
tool and not a file: a task list you read is a task list you read *all of*, every turn, for the rest of the
session. The list is a few hundred lines; `kit plan next` is three.

## Journal states

- One README per task. Newest iteration file names whose move it is: `-FINAL` (done), `-PART` (an agent
  continues), `-READY` (needs the human).
- `kit journal new <ID> <slug>` creates the directory and stubs with the required sections;
  `kit journal state <ID>` answers "whose move is it" and `kit journal finish` closes an entry.
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

## Pushing and merges

Pushing is nobody's manual step: `kit done` pushes the branch it committed on, and a `kit run` pushes
the merge target once that merge has passed the test gate — which is what makes publishing it unattended
defensible, since what goes out has been merged, archived and tested. A merged `task/<ID>` is then dropped
from the remote; if the push could not be made, the branch stays, because until the remote holds the merge
that branch is the only copy of the work it has. A remote that has moved is merged in first, never
rebased, because the run's worktree branches are based on those commits. A push that fails is a logged
line and nothing more: the commit is safe in the local repo and the next one pushes it. `[git] push =
false` turns it all off; `[git] push_merge = false` keeps the task branches going out but leaves the merge
local until a person has read it.

Why never merge by hand: since `kit done` pushes, the branch of a task whose merge went wrong is on the
remote — the one case where a lost branch costs a whole session — and a hand merge races the runner.

**A conflict the runner cannot join goes to an agent session, not to you.** Files where a line is a whole
record (the task list, the journal, the oracle's address list) are joined mechanically; a name given two
addresses in the address list is refused (`kit oracle join`). Anything else gets one session per merge
in the main checkout, which reads both sides, resolves and commits; the runner then runs the tests and
pushes as usual. `[run] fix_merges = false` leaves them for a human.

## PS2 libraries

`ps2-float` (FIZL0/ps2-float: EE FPU, VU0, fdlibm, newlib rand/MT) and `ps2-oracle` (FIZL0/ps2-oracle: the
R5900 interpreter under `crates/kit-oracle`) live in their own repositories, tag-pinned git dependencies
shared with other ports. Agents may fix and extend them when a port needs it (new op, missing function,
wrong rounding): change it there with a test, tag a release, bump the tag in every dependent
(`crates/game-sim`, `crates/kit-oracle`, the other kit, opentennis) and run their tests. Never vendor a copy
here; nothing from crates.io either.

## Unattended runs

```powershell
kit run once                  # one task, then stop
kit run 3                     # three agents at a time, each in its own worktree
kit run --dry-run 3           # what would start, and why the rest wait
kit stop                      # running tasks finish and merge, nothing new starts
```

The loop is [agent-runner](https://github.com/FIZL0/agent-runner) (install: SETUP.md step 1). Each agent
gets a git worktree on a `task/<ID>` branch and its own emulator copy (`KIT_PCSX2=N`).

Each agent also needs a terminal. With Windows Terminal installed, `kit run` opens one `wt.exe` tab per
agent in a window named `agents`; without it, one console window per agent. `kit run once`, one slot and
`--no-tmux` all mean the sequential path: one session at a time in this checkout. Don't close the window
on a run — a session killed mid-commit leaves a task neither ticked nor blocked. See `docs/WORKFLOW.md`
for what to expect from a long run and how to pick up the pieces afterwards, and `kit plan "Running a long
session unattended"` for reading the results.
