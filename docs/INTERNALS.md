# Internals — the contract between the kit's modules

Read this before changing anything in `ps2kit/`. It is short on purpose: the kit is a set of small
modules that only talk to each other through the interfaces below.

## The three rules

1. **Nothing game-specific outside `kit.toml`.** No address, serial, file name or offset is hard-coded in
   `ps2kit/`. If a module needs one, it comes from `config.cfg()`. A kit checkout works on any PS2 game the
   moment its `kit.toml` is filled in.
2. **Nothing OS-specific outside `ps2kit/platform_.py`.** Paths, locks, process checks, screenshots, the PINE
   socket type and the pad backend all come from there. This is what makes this edition possible: it *is*
   the Linux kit with that one module rewritten (plus the pad's transport, the launcher, and `runner.py`'s
   choice of terminal multiplexer). `platform_.py` here still carries both branches — `plat.WINDOWS`,
   `plat.LINUX`, `plat.MAC` — so a fix made in one edition can be read across into the other. If you find
   yourself writing `if plat.WINDOWS` in any other module, the function belongs in `platform_.py`
   instead; `tests/test_platform_win.py` asserts that for `emu.py`.
3. **Nothing ever floods the caller.** Every command's success output fits on a screen; long output is
   capped and a path to the full copy is printed instead. The reader is usually an AI agent whose whole
   conversation is re-read every turn, so a 16 MB `cat` is not a nuisance, it is the end of the session.

## Module map

| Module | Owns | Must expose |
|---|---|---|
| `config.py` | `kit.toml` | `cfg()`, `ROOT`, `Config`, `problems()`, `num()` |
| `platform_.py` | the OS | `Lock`, `rt()`, `instance()`, `pine_endpoint()`, `pcsx2_config_dir()`, `copies_root()`, `pcsx2_cmd()`, `pids_named()`, `alive()`, `terminate()`, `spawn_detached()`, `screenshot_window()`, `screenshot_tools()`, `wait_for_file()`, `pad_backend()`, `pad_available()`, `py_cmd()`, `quote_args()`, `link()`, `children()`, `ghidra_headless()`, `ghidra_user_dirs()` |
| `disc.py` | ISO 9660 | `Disc` (open/list/read/extract), `system_cnf()`, `cmd_extract(argv)` |
| `elf.py` | PS2 ELFs | `Elf` (read), `write_elf()` (build one for an overlay), `scan()`, `elf_path()`, `cmd_elf(argv)` |
| `ghidra.py` | headless Ghidra | `analyse()`, `postscript()`, `ee_extension()`, `project_programs()`, `cmd_ghidra(argv)` |
| `decomp.py` | the dumps | `func()`, `xrefs()`, `similar()`, `disasm()`, `data()`, `strings()`, `cmd_fn/cmd_xref/cmd_dis/cmd_data(argv)` |
| `pine.py` | live RAM | `Pine` class, `cmd_pine(argv)` |
| `pad.py` | virtual pad | `serve()`, `send()`, `cmd_pad(argv)` |
| `emu.py` | PCSX2 itself | `start()`, `stop()`, `status()`, `shot()`, `copy_config()`, `state_path()`, `state_ram()`, `cmd_pcsx2/cmd_shot/cmd_state(argv)` |
| `probe.py` | finding things | `find_vsync()`, `find_changed()`, `chase()`, `cmd_probe(argv)` |
| `record.py` | captures | `Watch` (a spec), `record()`, `cmd_record(argv)` |
| `oracle.py` | the Rust oracle | `cmd_oracle(argv)` |
| `plan.py` | the task list | `main(argv)` plus the functions `next_task/brief/tick/block/add/move/note` |
| `journal.py` | notes | `cmd_journal(argv)` |
| `checks.py` | tests, commits | `cmd_check(argv)`, `cmd_done(argv)` |
| `runner.py` | unattended runs | `cmd_run(argv)` |
| `setup.py` | first-time setup | `main(argv)`, `doctor(argv)`, `symbols(argv)` |

Every `cmd_*(argv)` takes the argument list after the subcommand, parses it with
`ps2kit.cli.parser(name, doc)`, and returns an exit code (`None` = 0). They are the *only* functions
`cli.py` calls.

## Shared conventions

**Addresses.** Any place a user can type an address accepts `1004c0`, `0x1004c0` or `@name`
(a `[symbols]` entry). Use `cfg().sym(s)` to resolve, never `int(s, 16)` directly.

**Where the config is.** `ps2kit.config.CONFIG` is `kit.toml` in the repository root, unless
`$KIT_CONFIG` (a file, or a directory holding one) or `$KIT_ROOT` (a directory) says otherwise —
`crates/kit-oracle` already honoured `$KIT_ROOT`, so both halves of the kit now agree on it. Only the
config's *location* moves: `ROOT` stays the repository, because it also anchors `crates/`, `plan/` and
the `kit` script. Two users: `kit demo`, which writes a synthetic game's config into `context/demo/`
rather than touching the user's, and tests that need a config of their own. A config reached this way
should hold absolute paths, since Python resolves relative ones against `ROOT` and the Rust crates
resolve them against `$KIT_ROOT`.

**The private directory.** Anything derived from the disc or the running game goes under
`cfg().ctx(...)` — `context/` by default, git-ignored. Call `cfg().ctx('decomp', 'boot.c')`; it creates the
parent. Nothing derived from the game is ever written into a tracked path. A module that writes elsewhere
is a bug.

**Programs.** A game is one boot ELF plus zero or more overlays. `cfg().programs` gives their dump names
(`boot`, then each `[[overlay]] name`). Every static tool takes `-p/--program NAME` defaulting to the last
overlay if there is one, else `boot` — that is nearly always the gameplay code.

**Locks.** One process talks to one PCSX2 at a time: take `platform_.Lock('pcsx2')`. Honour
`KIT_PCSX2_LOCKED=1` in the environment (a parent already holds it for a multi-step job). A tool that
polls the emulator must give the lock up when it exits, including on a crash.

**Frames.** `Pine.next_frame(last)` is the only way to wait for a frame. It polls the configured counter
and bails out with a clear message if the counter stops moving (the game paused, or the thing you were
recording ended) rather than spinning forever holding the lock.

**Failure.** A capture tool that fails must say so in a way nobody can mistake for partial success:
print `CAPTURE FAILED: <what> — anything this run wrote is incomplete` and exit non-zero. Half-written
CSVs that look fine have cost more time than any other class of bug in this kind of work.

**Imports.** Modules import `from ps2kit.config import cfg` and `from ps2kit import platform_ as plat`.
Nothing imports a sibling except the leaves — `config`, `platform_`, `disc`, `elf`, `pine`, `pad` — and
`elf` itself imports only `config` and `disc`. `cli` is imported by nobody at module level: a `cmd_*`
pulls `parser` in from `ps2kit.cli` inside the function, which is also what keeps `--help` working with
no `kit.toml` present.

## Dependencies

The kit runs on a stock Python 3.11+ with no packages at all for everything static (disc, ELF,
decompilation, plan, journal, checks). Optional extras, each needed by exactly one thing:

| Package | Needed for |
|---|---|
| `vgamepad` + the ViGEmBus driver | the virtual pad (`evdev` is the Linux edition's equivalent) |
| `numpy` | nothing required; `probe` is faster with it and falls back without |
| a Rust toolchain | the `crates/` port target and the function oracle |
| Ghidra + the EmotionEngine extension | decompiling at all |
| PCSX2 2.x + your own BIOS | anything live |

Import optional things inside the function that needs them, with a message that names the fix.

## Tests

Plain scripts, no framework, run with the interpreter: they have to work on a checkout with no packages
and no `kit.toml`.

| Script | Covers |
|---|---|
| `py -3 tests\test_plan.py` | the task list and the runner's task picker |
| `py -3 tests\test_static.py` | the static layer against inputs it builds itself: a disc image, a MIPS blob, a Ghidra dump |
| `py -3 tests\test_live.py` | the live layer against a fake emulator it builds itself: PINE framing, the pad language, RAM search, launch |
| `py -3 tests\test_record.py` | the watch-spec parser, region coalescing, torn-frame handling |
| `py -3 tests\test_push.py` | the push step: a moved remote merged in, a conflicting one refused, the switches |
| `py -3 tests\test_platform_win.py` | this edition's OS layer: paths, the lock, the PINE endpoint, `-cfgpath` copies, pad rows, process handling, `wt.exe` argv and its shim end to end |
| `py -3 ps2kit\hooks\guard.py --self-test` | the context guard's decisions |
| `cargo test --workspace` | the oracle's kit layer (`kit-oracle`) and your own port; `ps2-float` and `ps2-oracle` are tested in their own repositories |

What they deliberately do not cover: anything that needs PCSX2, Ghidra, a disc or a real pad. Those are
checked by `kit doctor` and by using them.

`test_live.py` builds its fake emulator on the transport this OS really uses — loopback TCP here, a unix
socket on the Linux edition — and its pad test asks `pad.transport()` rather than looking for a FIFO, so
the suite proves the Windows paths on Windows instead of failing on things that do not exist there. That
is the one place where the two editions' test files legitimately differ.
