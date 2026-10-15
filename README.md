# ps2-decomp-kit

A workbench for reverse-engineering a PlayStation 2 game and re-implementing it — **with proof that the
re-implementation matches**.

It is game-agnostic. One file, `kit.toml`, says which game you are working on; everything else works the
same whatever that game is. It is built for long projects worked on a task at a time, including ones where
most of the typing is done by an AI agent, and a lot of its design is about making that cheap and honest.

One entry point: `kit <command>`, which is `kit.cmd` (or `kit.ps1`) in the repository root — `.\kit` from
PowerShell until you put the repository on your `PATH`. Use PowerShell 7 as your shell: the kit's own
output is shell-neutral, but almost everything you will do *around* it needs a shell with pipelines.

```
kit setup                     # find your disc, emulator and Ghidra; write kit.toml
kit extract                   # unpack the disc
kit elf --scan                # work out the program overlays and their load addresses
kit ghidra analyse && kit ghidra dump
kit fn 1000a0                 # one decompiled function, not a 20 MB dump
kit pcsx2 start               # the real game, with live memory access
kit probe vsync --save        # find its frame counter, once
kit record watch/frame.toml --frames 600 --out context/run1.csv
kit check                     # your port's tests, summarised to one line
kit done T3 "port the walker" # test, tick the task, commit
```

## What is actually in here

**Static side.** An ISO 9660 reader, a PS2 ELF reader and overlay-ELF builder, headless Ghidra driving,
and tools that read the decompilation *without ever loading it whole*: one function by address, a
function's callers and callees, an R5900-aware disassembler, and a table reader. Plus `kit xref
--similar`, which finds the functions shaped most like one you have already ported — port one, then find
its siblings instead of hunting them.

**Live side.** A PINE client for the running game's memory, a virtual controller, screenshots, save-state
RAM extraction, and `kit probe`, which finds game-specific addresses for you: the frame counter, values
that changed between two moments, pointer chains walked hop by hop.

**Proof side.** Four ways to show a port matches, in descending order of how much they cost you:

| Method | Proves | Cost |
|---|---|---|
| **Function oracle** | a pure function, over 10,000+ generated inputs, bit-exact | cheap — no emulator, runs in a unit test |
| **Live capture** | timing, frame flow, anything stateful | a recording per question |
| **Save-state RAM** | tables, static data, struct layouts | one extraction |
| **Replay harness** | the whole thing: same input → same state hash | build it once, it guards everything after |

The oracle is the one worth understanding first. `crates/ps2-oracle` is a small R5900 interpreter that
loads the game's own code from *your* disc at test time and calls one function with arguments you choose.
Your port and the original then run side by side over as many inputs as you like, and a mismatch comes
back with the inputs that caused it. No emulator, no recording, no human watching.

**Arithmetic.** `crates/ps2-float` models the PS2's single-precision arithmetic as PCSX2 performs it —
round-toward-zero, the EE's mantissa alignment with its one guard bit, denormals flushed, divide and
square root rounding differently from add and multiply — plus the VU0 macro ops and the transcendental
functions a PS2 game's own C library computes differently from your host's. Without this, a ported
calculation is *nearly* right, and nearly right compounds into visibly wrong within a few hundred frames.

**Workflow.** A task list that is a tool rather than a file to read (`kit plan next`, `brief`, `tick`,
`block`), a research journal with a mandatory "what I could not verify" section, a test runner that
answers "did the build break or did a test fail" in one line, a one-call wrap-up that tests, ticks and
commits, and unattended runners that keep several agents working through the backlog in separate
worktrees.

**Context discipline.** If an AI agent is doing the work, its whole conversation is re-read every turn, so
a single careless `cat` of a decompilation dump can end a session. The kit ships hooks that block the
expensive mistakes and name the cheap alternative, and every command is built to print a screenful, not a
megabyte. `docs/CONTEXT.md` explains each measure and what it cost to learn.

## Getting started

**[SETUP.md](SETUP.md)** — eight steps, each ending in a command that tells you whether it worked. Read
**[WINDOWS.md](WINDOWS.md)** alongside it: it is one page, and it is where this edition admits what it has
not been able to test.

Then **[docs/WORKFLOW.md](docs/WORKFLOW.md)** — the loop you stay in: find the code, port it, prove it,
write down what you could not prove.

No disc dumped yet, or you would rather see the whole loop before committing an evening to it?
**`kit demo --check`**, then **[docs/EXAMPLE.md](docs/EXAMPLE.md)**: the kit generates a synthetic PS2
program — its own ISO, its own boot ELF, its own overlay, none of it from any real game — and the
walkthrough takes you from "unpack the disc" to "this port is bit-exact over ten thousand inputs", with a
real test and a deliberate failure at the end. It needs Python and a Rust toolchain and nothing else.

`plan/TODO.md` already contains a worked backlog for a brand-new port, in order, from "open the disc" to
"the replay harness passes". `kit plan next` hands you the first one.

## Documentation

| | |
|---|---|
| [SETUP.md](SETUP.md) | prerequisites and first-time setup |
| [WINDOWS.md](WINDOWS.md) | what this edition does differently, and what is untested |
| [AGENT.md](AGENT.md) | the rules an agent (or you) works by |
| [PLAN.md](PLAN.md) | how a task is taken from the list to a commit |
| [docs/WORKFLOW.md](docs/WORKFLOW.md) | the port loop, in detail |
| [docs/EXAMPLE.md](docs/EXAMPLE.md) | the whole loop, worked, on a game the kit generates — start here if you have no disc yet |
| [docs/PROVING.md](docs/PROVING.md) | the four proof methods, and which to use when |
| [docs/PS2.md](docs/PS2.md) | the hardware you need to know about, and why it bites |
| [docs/GHIDRA.md](docs/GHIDRA.md) | decompiling PS2 code well |
| [docs/PCSX2.md](docs/PCSX2.md) | driving the emulator, save states, recordings, copies |
| [docs/CONTEXT.md](docs/CONTEXT.md) | keeping a long agent-run project affordable |
| [docs/INTERNALS.md](docs/INTERNALS.md) | the kit's own module contract |
| [RETROSPECTIVE.md](RETROSPECTIVE.md) | what building this taught, and what is still weak in it |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | the failures that actually happen |
| [watch/README.md](watch/README.md) | the watch-spec format for captures |
| [plan/REFERENCE.md](plan/REFERENCE.md) | quick reference: emulator control, save-state slots, proof methods |
| [plan/HUMAN.md](plan/HUMAN.md) | the things only you can do (dump the disc, make the states) |

## What this does not do

It does not get you a game. You dump your own disc and your own BIOS; nothing here ships, downloads or
contains game data, and the private `context/` directory where everything game-derived lands is
git-ignored so you cannot commit it by accident.

It does not decompile to compilable C or try to produce a matching binary. It is built for the other
approach: understand a function, re-implement it in a language you want to keep, and prove the
re-implementation behaves identically. If you want a byte-matching decompilation, you want a different
toolchain.

It does not emulate the PS2. PCSX2 does that. The kit models only the arithmetic, because that is what a
port has to reproduce exactly, and runs single functions in a small interpreter, because that is what
makes verification cheap enough to do thousands of times.

## Platform

This is the **Windows edition**. Every command, subcommand and flag is the same as the Linux edition's;
what differs is the handful of places that touch the operating system — the launcher (`kit`, not
`./kit`), PINE over a TCP socket instead of a Unix one, the virtual pad through ViGEmBus instead of
`/dev/uinput`, window capture through the Win32 API, and Windows Terminal tabs instead of tmux panes.
[WINDOWS.md](WINDOWS.md) is the one-page list, including what has not been tested on real hardware.

Linux is where the toolkit was developed and is the better-tested of the two: the tools it generalises
were written and used on Linux across a full-length port of one PS2 game, and this edition's
Windows-specific half has not yet been run on Windows at all. If you have a choice of machine, use
Linux. If you do not, read `WINDOWS.md` first — it lists exactly what is still a claim rather than a
fact.

## Licence

MIT — see [LICENSE](LICENSE). The licence covers the kit. It says nothing about any game you point it at;
what you may do with that is between you and the law where you live.
