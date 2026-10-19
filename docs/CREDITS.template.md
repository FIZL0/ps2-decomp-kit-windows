# Credits and how <your port> is made

Copy this to the repository root as `CREDITS.md` and fill it in. Say what is true, including the limits.

## The game

<Port> is a fan port of *<Game>* (PlayStation 2, <year>, <developer> / <publisher>). None of the game is in
this repository or in its builds: the game reads its data from the player's own disc at runtime.

## Built on

| Project | Licence | What we use |
|---|---|---|
| [ps2-float](https://github.com/FIZL0/ps2-float), [ps2-oracle](https://github.com/FIZL0/ps2-oracle) | MIT / Apache-2.0 | PS2 float arithmetic; the interpreter the tests compare against |
| ... | | |

Plus the crates in `Cargo.lock`, each under its own licence. Patched copies of dependencies keep their
upstream licence files.

## Tools

[PCSX2](https://pcsx2.net) runs the original for recordings; [Ghidra](https://ghidra-sre.org) reads the game
program locally. Neither is shipped.

## How it's made

- **Who wrote it.** <AI agents (which, through what), working from a task list; what the maintainer does.>
- **What it matches, and how we know.** <What is ported exactly and how it is checked: recordings of the
  original, the function oracle. What is your own design.>
- **What stays local.** Decompiler output, memory dumps, recordings and screenshots stay on the
  maintainer's machine (`context/`, git-ignored).
- **Checks.** `kit audit` on every commit: no addresses or decompiler names in the code or the docs, no
  decompiled code in the notes. Simulation ports since <date> are written in two sessions (spec, then port
  with the decompilation locked; docs/WORKFLOW.md *Clean-room ports*).
- **What that doesn't prove.** The audit shows those patterns are absent, nothing more. The two-session
  split is a working practice on one machine, not a sandbox<, and ports before <date> were written with
  the decompilation open>. None of this is a legal opinion.
