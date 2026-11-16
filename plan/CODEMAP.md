# Code map

Keep this current. It exists so nobody greps the repo to find where something lives.

## The kit

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
- workflow: `plan.py` · `journal.py` · `checks.py` · `runner.py` + `runner.toml` the kit's settings for
  [agent-runner](https://github.com/FIZL0/agent-runner), which runs `kit plan` and `kit run` · `oracle.py` ·
  `setup.py` setup and doctor · `hooks/` the context guards

`watch/` — capture specs (`*.toml`), one per thing you record. `research/` — your own scripts and notes,
committed, no game data. `context/` — everything game-derived, git-ignored.

## The port

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
