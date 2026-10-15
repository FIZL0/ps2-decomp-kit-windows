# What building this taught, and what could be better

This kit was generalised out of a working single-game project: a Rust/Bevy re-implementation of one PS2
game, about eighteen months of mostly agent-driven work, with a tool directory that had grown to fit it
exactly. The job was to keep what was load-bearing, drop what was accidental, and make the rest apply to
any PS2 game.

Doing that exposed a lot about which parts of a project like that were really carrying the weight — and
the answer was not the parts that looked impressive. This is the honest version: what transferred, what
turned out to be a mistake, and what is still weak in the result.

---

## Part 1 — What actually mattered in the original

### The context measures were the product

Going in, the assumption was that the valuable artefacts were the reverse-engineering tools: the ISO
reader, the PINE client, the Ghidra scripts, the R5900 interpreter. They are good, but they are
reproducible. Anyone competent writes an ISO reader in a day.

The parts that were genuinely hard-won, and that nobody would get right first time, were all about
*spending attention*:

- the per-function readers, so a 20 MB dump is an index rather than a document
- the hook that blocks the expensive read **and names the cheap alternative**
- `brief <ID>` collapsing eight reads into one
- `done <ID>` collapsing a five-step wrap-up into one
- `check` reducing a build log to "the build broke" or "a test failed"
- the task list being a *command*, not a file, because a file gets read in full

None of these are clever. Each exists because its absence cost a session. The generalisation that matters
most here is not "tools for PS2 games", it is "a way of working on something far larger than the working
set you can hold".

### The mandatory "what I did not verify" section

The original required every journal entry to end with a list of everything the author could not check,
each with a reason, or the word `none`. This is the single highest-value process rule in the whole
project, and it is three lines of convention.

The reason is that in a port, verified and unverified work are indistinguishable in a diff. Both compile.
Both look plausible. Both pass review. The failure mode of a long port is not bad code, it is forty
small silent corners accumulated at one or two a week, each invisible. Written down, those forty corners
are a work plan; unwritten, they are a game that is subtly wrong forever, and no amount of later
diligence recovers them because nobody knows where to look.

### Four proof methods, used in cost order

The original had converged on something worth stating explicitly, which it never had: there are four ways
to prove a port matches, they differ in cost by two orders of magnitude, and most wasted effort is using
an expensive one where a cheap one would do.

The function oracle — a small interpreter running the game's own code so a unit test can compare a port
against it over ten thousand generated inputs — is dramatically underrated. It needs no emulator, no
recording and no human; it finds the branch that only fires at exactly zero; and it tells you the inputs
that diverged. In the original it arrived late, after many things had been verified the expensive way by
recording them. Building it early would have paid for itself several times over, which is why it is now
task five in the default backlog.

### "If you get stuck, move on"

An explicit rule: if a tool hangs, the same fix fails twice, or twenty minutes pass without progress,
write down what happened, mark the task blocked with a reason, and take a different one.

This is partly about morale and mostly about economics. An agent that retries one obstacle for an hour
produces no work *and* fills the session with failed attempts that every subsequent turn re-reads. The
rule converts a stall into a recorded result. "Blocked, with a reason" is a perfectly good outcome; it
is how the project discovered which problems genuinely needed a human.

### The things that bite are never the interesting things

The original's accumulated warnings were nearly all operational, not intellectual:

- a `-f` process pattern matches the searching shell itself, so `pkill -f foo` can kill its own caller
- PCSX2 rewrites its ini on exit, so settings must be written immediately before launch
- `select` on a virtual pad is half of every emulator hotkey
- PINE serves one connection, so a second client wedges both
- a globbed `rm` prompts for confirmation even in permissive modes, and an unattended run sits on it
- build directories reach hundreds of gigabytes across parallel worktrees
- a capture with one missing frame looks perfectly fine and is worthless

Every one of those cost real time once. They are the actual content of experience in this kind of work,
and they are the reason `docs/TROUBLESHOOTING.md` is organised by symptom rather than by subsystem.

---

## Part 2 — What the original got wrong

These are not criticisms of the people who built it. They are the predictable shape of what happens when
a project grows for eighteen months without anyone being able to stop and refactor the tooling.

### One-off scripts bred until nobody could tell which were right

About forty-five near-identical capture scripts. Well over a hundred single-use analysis scripts. Each
one written because adapting an existing one looked harder than starting fresh — which, individually, it
was.

The result: no two captures were taken the same way, several scripts had the same bug fixed in some
copies and not others, and the only way to know whether a given script still worked was to run it. The
cost was invisible, spread over every capture anyone ever took.

The fix in the kit is `kit record` with a declarative watch spec: a new capture is a ten-line TOML file
naming the fields, their types and any pointer chains, and the recorder does the coalescing, batching,
frame-gap detection and failure reporting once, for everyone. The lesson generalises past this project:
**when the same program keeps getting rewritten with small variations, the variations are data.**

### Game-specific knowledge was scattered everywhere

Addresses, offsets, file names and layout assumptions lived in dozens of files. That is natural in a
single-game project and it is exactly what makes it a single-game project. Pulling it all into one
`kit.toml` was most of the generalisation work, and the discipline it imposes is now rule 1 in
`docs/INTERNALS.md`: nothing game-specific outside that file.

The same was true of OS assumptions — socket paths, process checks, screenshot commands, lock
primitives — which is now rule 2 and one module. That is what makes the Windows edition a change to one
file rather than a fork.

### The examples carried the old game with them

The generalisation moved every address *the code uses* into `kit.toml`, and I checked that. What I did
not check for a long time was the addresses the **prose** uses. The first draft of this kit shipped seven
real function addresses from the project it grew out of, in thirty-odd places: `README.md`, seven
documentation pages, seven module docstrings, a Rust doc comment, two Ghidra scripts, the example config,
a watch spec and a test. Every one of them was an innocent-looking example argument — `kit fn <that
address>` — and that game's own disc directory name was in a fixture besides. The kit's own first
non-negotiable rule says no retail address appears in what it ships, and the kit broke it on line 15 of
its own README.

Finding them was mechanical once I thought of it: take every hex literal in the new repository, take the
old project's address file, intersect. Seven hits, five minutes. Not thinking of it lasted much longer.

The lesson is not "be careful with examples". It is that a rule nobody can check is a rule that holds
until the first hurry, and that the cheapest place to put the check is where the work ends. So the kit now
has `kit audit`, and `kit done` gates on it: every file about to be committed is grepped for the
addresses in the project's own address file and for Ghidra's `FUN_…`/`DAT_…` names. It only recognises
addresses the project wrote down — guessing which hex literals are addresses would flag every bitmask —
which means an address you never named can still slip through, and the example addresses in these docs are
now in the demo program's own range, where nothing real can live. Both of those are exactly the kind of
gap this file exists to say out loud.

### Per-game setup was a week of undocumented archaeology

Two things in particular: finding the frame counter, and working out which blobs on the disc are
overlays and what address each loads at. Both were done once, by hand, by someone who then knew the
answer and had no reason to write down the method.

For a kit aimed at *any* game, that is the single biggest barrier to the first useful day, so both became
commands: `kit probe vsync --save` samples RAM for words advancing at the frame rate, verifies the
candidates and writes the winner into the config; `kit elf --scan` infers each blob's load address from
its own internal references and prints a config block to paste. Neither is magic and both can be wrong,
but turning a week of archaeology into a command that is right most of the time is the difference between
a kit someone else can use and one only its author can.

### The rules were right but repeated

The original's instructions said the same things in three places, and the three copies had drifted. Where
rules are loaded into every session, duplication is not just untidy — it is a per-turn cost and a
correctness hazard, because a rule that exists twice gets updated once. The kit has one home per rule and
cross-references instead of restating, imperfectly.

---

## Part 3 — What is weak in the kit itself

Written now, while it is still honest, rather than discovered by the next person.

### It is unproven against a second game

This is the big one. The kit was generalised from exactly one project, so every generalisation is a
hypothesis. The parts most likely to be wrong, in order:

- **`kit elf --scan`** assumes overlays are stored as raw code blobs in ordinary files. Plenty of games
  pack them in a proprietary archive, compressed, or load them in pieces. For those, the scan finds
  nothing and the first real task is reverse-engineering an archive format — which the kit does not help
  with at all.
- **`kit probe vsync`** assumes a word in main RAM advancing once per frame. Common, not universal:
  some games keep their frame count in a struct reached by a pointer, or count fields rather than frames,
  or derive timing from a hardware timer the heuristic will not see.
- **`kit.toml`'s shape** fits a game that is one boot ELF plus a handful of overlays. A game with dozens
  of dynamically loaded modules, or one that relocates code, will strain it.
- **Disc formats.** ISO 9660 is handled. A game with a custom filesystem on the disc, or data in the gap
  between the filesystem and the tracks, is not.

Nothing here is unfixable; all of it is unvalidated. The second game to be ported with this kit will find
most of it, and `docs/INTERNALS.md` exists so that the fixes land in the right single place.

### The oracle covers only the instructions one game used

`ps2-oracle` (now its own repository, FIZL0/ps2-oracle) interprets the R5900 subset the original project needed. Another game will hit
unimplemented instructions immediately — MMI ops, unusual VU0 sequences, anything touching the
scratchpad in a way the original did not.

This is designed for: an unknown instruction panics naming the instruction and the address, adding one is
a few lines, and each addition is permanent. It is still friction on day one, and there is no table
anywhere of what *is* implemented, which is the first thing someone will want. That table should be
generated from the interpreter rather than written by hand, and it is not.

### The arithmetic model is a model

`ps2-float` (FIZL0/ps2-float) reproduces the EE FPU as PCSX2 implements it, which is itself a careful
reverse-engineering of the hardware rather than the hardware. Where PCSX2 is subtly wrong, this kit is
wrong in the same way — and a port verified against PCSX2 will match PCSX2, not a PS2. For the purpose
(a faithful re-implementation, verified against an emulator everyone has) that is the right trade. It is
worth knowing that the floor on fidelity is the emulator's, not the hardware's.

### The pad is wall-clock, and that is a real limitation

Virtual-controller input is timed in milliseconds, not frames, so no sequence driven by it is exactly
reproducible. The kit's answer is to lean on save states that run themselves, which works and is good
advice, but it means anything requiring frame-exact input has to go through PCSX2's own input recording,
which the kit does not integrate. That integration is the most obvious missing feature.

### The hooks are tied to one agent harness

The context guards ship as Claude Code hooks. The *ideas* are portable — block the expensive read, name
the cheap alternative — but the wiring is not, and a user of a different harness gets the tools without
the guard rails, which is the configuration most likely to end a session expensively. The guard logic is
in one module and could be exposed as a wrapper command for other harnesses. It is not.

### The worked example is a game that does not exist

This was the gap I expected to leave open. The strongest possible documentation for a kit like this is
one fully worked example — a real function, found, read, ported, proved, with every command and its
output — and the kit cannot ship that, because the output would contain game code.

The way out was to generate the game. `kit demo` assembles a synthetic PS2 program: an ISO with a boot
ELF of four hand-written R5900 functions, a raw overlay with a load address to discover, a symbol table,
a second disc with the wrong serial, and a `kit.toml` of its own. `docs/EXAMPLE.md` then walks the whole
loop on it — extract, `elf --scan`, `dis`, `data`, `oracle`, a Rust port proved bit-exact over 10,000
inputs, and the same port rewritten with host `f32` so you can watch the proof catch it two ulps in.
`kit demo --check` runs all twelve steps and prints pass/fail per step, in about a second, with no
disc, no emulator, no BIOS and no network. It is also the integration test the kit otherwise lacked:
twelve commands in sequence, against real bytes, with an assertion at the end of each.

What it still is not: a real game. The generated program ships a symbol table, so the hardest part of a
real port — identifying the function at all — is skipped. Its functions are small, pure and leaf-ish,
where real ones take structs by pointer, mutate globals and run for hundreds of instructions of
compiler output. Its overlay is padded with identical filler functions purely so `elf --scan` has
density to score, which is why the scan's confidence numbers come out implausibly clean. And its weight
table was chosen to make the EE/IEEE divergence loud; a real function may diverge on one input in a
million, which is far harder to notice and is the actual reason the generated 10,000-input sweep matters.
`docs/EXAMPLE.md` says all of that in its own closing section rather than letting the example flatter
itself, but a reader who does the demo and expects their own game to feel like it will be disappointed
on the first afternoon.

A walkthrough against a real homebrew ELF would still be better, and is still unbuilt.

### Untested paths

The pure logic is covered: `tests/test_static.py` (47 properties) builds its own ISO 9660 image, its own
MIPS blob and its own Ghidra dump, and `tests/test_live.py` (43) builds a fake PCSX2 that speaks PINE over
a socket; `test_record.py` (25) and `test_plan.py` cover the recorder and the workflow. Writing those
found two real bugs that review had not: a bad duration in a pad script pressed the button *before*
parsing the duration, so `press cross soon` left Cross held down for the rest of the session, and the
guard's command patterns broke the moment the launcher was spelled differently.

What is still unproven is everything behind a device or an install: the `/dev/uinput` and ViGEm pad
backends themselves (the script language and the backend dispatch are tested, the device is not), Ghidra
actually running — the decompilation tests consume a dump written in Ghidra's format, which is the
contract, not proof Ghidra emits it — the screenshot paths, `probe`'s numpy fast paths (numpy is absent
here, and the plain-Python paths are the tested ones), `emu`'s py7zr and `7z` save-state branches, and
a real disc: the ISO tests use a hand-built image with no Joliet, no multi-extent files and only one of
the five sector strides exercised against actual bytes.

Those are ports of code that worked in the original, which is good evidence and not proof. Expect to fix
small things in the first hour, and `kit doctor` is the thing to run when one of them surprises you.

### This edition has never run on Windows

The Windows-specific code here was written on a Linux machine. Its pure parts are under test —
`tests/test_platform_win.py` forces the Windows branch and checks the paths, the lock, the PINE endpoint,
the `-cfgpath` copies, process handling and the `wt.exe` shim end to end — and the four shared suites pass,
but no command in this repository has been executed against a real PCSX2 on a real Windows install.
`WINDOWS.md` lists every claim a real machine still has to confirm, and the deal is: when you are the
first to run one, fix the code if it is wrong and then fix the line that promised it.

Two differences are limitations rather than translations, and no amount of code fixes them: a ViGEm pad is
an XInput device, XInput has four user slots, so four emulator copies is the ceiling when they need input;
and every ViGEm pad reports Microsoft's own vendor and product id, so the VID/PID pinning that keeps copy
*N* on pad *N* on Linux has nothing to match here.

Keeping this as a second repository is itself a weakness: the two editions drift, and the drift is
invisible. Everything outside `ps2kit/platform_.py`, `pad.py` and the launcher is meant to be identical in
both, so a fix made in one should be read across into the other — `diff -r` between the two checkouts is
the cheapest way to see what has not been.

---

## Part 4 — If someone were to do this again

1. **Build the proof tools before the port.** The arithmetic model, the replay harness with a
   platform-independent state hash, and the oracle — on a simulation that does almost nothing. It feels
   like a detour and it is the difference between a port that converges and one that is ninety per cent
   done forever.
2. **Write the config file first, even for one game.** Every address and file name you inline costs ten
   times as much to extract later, and the discipline of "it goes in the config" is free at the start.
3. **Treat repeated scripts as a missing data format.** The second time you copy a script and change two
   constants, stop and write the spec-driven version. Forty-five is what happens if you do not.
4. **Make the cheap path the easy path, mechanically.** Documentation that says "don't read the whole
   dump" is a suggestion; a hook that refuses and prints the right command is a design. Any rule you
   actually care about should be enforced by something that also tells you what to do instead.
5. **Require the unverified list from the first task.** Not at the end, when forty gaps already exist and
   nobody remembers which they were.
6. **Make stopping cheap.** A blocked task with a written reason is a good outcome. A project where
   stopping feels like failure is a project that burns days on one obstacle and records nothing about it.
