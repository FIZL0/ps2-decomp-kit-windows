# PLAN — how work gets done here

`AGENT.md` has the rules. Everything else is looked up, one section at a time, with `kit plan "<heading>"`:

- `plan/MECHANICS.md` — continue step by step, the task-command table, journal states, wrap-up, pushing
  and merges, the PS2 libraries, unattended runs
- `plan/CODEMAP.md` — where everything lives (read it instead of grepping; add a line per new module)
- `plan/REFERENCE.md` — driving the emulator, save states, which proof for which code, known gaps
- `plan/HUMAN.md` — what only the human can do; `docs/` — the long versions

## Tasks

`kit plan` (index) · `next` · `<ID>` · `brief <ID>` · `tick` · `block <ID> "<why + what unblocks>"` ·
`unblock` · `add T "text" --files a.rs,dir/** [--after X] [--test name]` (files and waits are fields, never
prose) · `move` · `note` · `newid` · `archive`. Details: `kit plan "Task commands"`.

## Code map

`plan/CODEMAP.md`.
