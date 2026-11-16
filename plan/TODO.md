# TODO — open tasks, in order

The rules are in `PLAN.md`; this is only the backlog. Read it with `kit plan` (headings and ids),
`kit plan next` (the next task to take) and `kit plan brief <ID>` (everything you need for one task:
the line, its long form, its journal and the files, with line counts and recent commits). Never open this
file whole, and never edit a checkbox by hand — `kit plan tick <ID>`, `kit plan block <ID> "<why>"`,
`kit plan unblock <ID>`, `kit plan move <ID> …`, `kit plan add <PREFIX> "<text>"`.

One line is one task, finishable in one session:

    `- [ ] **ID** what it has to do | files: a.rs, dir/** | after: X | test: name | journal: slug`

`files:` is what the task touches: the runner keeps two agents off the same code by it (and off what running
and finished-unmerged tasks have touched), so keep it honest and specific. `after:` ids it waits for. `test:`
names the test that will prove it; a task with no `test:` either is not a port task or is not thought through
yet. `journal:` is the journal directory under `research/journal/`. Only the fields count: prose is never read
for files or ids. `kit plan add P "text" --files a.rs,dir/** --after X --test name` writes them.
`- [x]` done (it moves to `plan/DONE.md`), `- [~]` blocked with the reason on the line, `(in X)` is a subtask of X.

**Ids are permanent.** They end up in journal directories, commit messages, test names and code comments.
If a task comes back, reopen it under its own id; never renumber, and never give an old id to new work.

Prefixes: `D` disc · `R` decompilation · `C` the frame and captures · `F` arithmetic · `O` oracle ·
`A` acceptance harness · `I` input · `S` simulation · `V` rendering · `K` the kit itself.

### Next (in this order)

The first four phases are tooling and feel like a detour from the port. They are the difference between a
port that converges and one that is 90 % done forever: every task after them is proved with what they
build. Do them in order; inside a later phase, tasks that touch different files can go in any order.

- [ ] **D0** Do the loop once on a game that does not exist: `kit demo --check` (12 steps, under a second, no disc) and read `docs/EXAMPLE.md` alongside it. You are learning what each command's output looks like before any of it depends on your own disc; the walkthrough's closing section says where the synthetic program is kinder than a real one | files: docs/EXAMPLE.md, docs/PROVING.md | test: kit demo --check` prints 12/12 | journal: d0-the-demo
- [ ] **D1** Fill in `kit.toml` for this game and make `kit doctor` pass: serial, region and version from the disc's `SYSTEM.CNF` (`kit extract --only SYSTEM.CNF`), the ISO path, the PCSX2 command, the BIOS it finds. Record in the journal which dump you are working from (serial, build date, size, hash) — every address in this repo is only true of that one disc | files: kit.toml, docs/CONTEXT.md | test: kit doctor` clean | journal: d1-the-disc
- [ ] **D2** List and extract the disc: `kit extract --list` shows the whole tree with sizes, `kit extract <path>` writes into `context/iso/`. Note which files are archives (one container holding many), which look like code (ELF magic, or a plausible overlay size), and which are plain data | files: ps2kit/disc.py | test: the extracted boot ELF's size matches the directory record | journal: d1-the-disc
- [ ] **D3** The boot ELF: `kit elf` on it — segments, entry point, where the code lands. Put `[boot] elf` and, if the program headers are unusual, `[boot] load` in `kit.toml`. Say in the journal what the address ranges are: code, data, BSS, and the gap where overlays go | files: kit.toml, docs/CONTEXT.md | test: kit elf` prints segments that cover the entry point | journal: d3-boot-elf
- [ ] **D4** Overlays: `kit elf --scan` guesses which disc files are code loaded over a fixed address; confirm each by looking for a jump table or a string that belongs to that part of the game, and add an `[[overlay]]` block per confirmed one. Most PS2 games have at least a menu overlay and a gameplay overlay, and the gameplay one is where you will spend the whole port | files: kit.toml | test: an overlay dumps with sensible function boundaries at its `load | journal: d3-boot-elf
- [ ] **D5** The archive format, if D2 found containers: the header layout (count, offsets, sizes, names or hashes), enough to list and extract members. Write it up with a hexdump in the journal before writing any code; formats are usually obvious once you can see three entries side by side | files: crates/game-sim/src/**, docs/CONTEXT.md | test: every member of one archive extracts and the sizes add up to the file | journal: d5-archives

### Phase 2 — a decompilation you can read

- [ ] **R1**  Ghidra: `kit ghidra` on the boot ELF and each overlay, with the EmotionEngine extension, producing `context/decomp/<program>.c`. Check the dumps are at the right addresses (a function in the overlay dump should sit inside the overlay's load range) and note how long a full analysis takes on this machine | files: kit.toml, ghidra_scripts/** | after: D4 | test: kit fn <addr>` prints one function from each program | journal: r1-decompile
- [ ] **R2**  The PS2 library functions Ghidra cannot name: `printf`/`sprintf`, `memcpy`, `malloc`, the SIF/IOP calls, `scePad*`, `sceGs*`. Name them in `[symbols]` so every later dump reads in terms of them instead of `FUN_…`. Finding `printf` first is worth hours: its call sites are labelled strings that say what the surrounding code is | files: kit.toml, research/oracle.txt | after: R1 | test: kit fn @printf` resolves | journal: r2-symbols
- [ ] **R3**  Find the main loop: the function that runs forever, waits for vsync and calls a handful of per-frame things. Work out its structure — what runs before the sim, what runs after, where rendering is kicked off — and write it down as a numbered tick order. Everything in the port hangs off this order | files: docs/CONTEXT.md | after: R1 | test: none (notes) | journal: r3-main-loop
- [ ] **R4**  The game's state machine: the mode variable the main loop switches on (title, menu, loading, in-game, replay, results) and the function per mode. Name them in `[symbols]`. This is the map you will navigate by for the rest of the port | files: kit.toml | after: R3 | test: kit xref @main_loop` lists one handler per mode | journal: r3-main-loop
- [ ] **R5**  The RNG: find it (look for a 32-bit multiply by a constant and a stored seed, cross-referenced from everywhere), port it, and prove it with the oracle over 10k calls from a dumped seed. A wrong RNG makes every later proof noisy, and it is a 20-line function | files: crates/game-sim/src/rng.rs, research/oracle.txt | after: R1 | test: crates/game-sim/tests/rng.rs | journal: r5-rng
- [ ] **R6**  The actor/entity table: where live objects are kept in RAM (base address, stride, how many), and the first few fields of one — position, velocity, state, type. Dump the same table across a handful of frames from a save state so the stride and the fields are not guesses | files: research/oracle.txt, docs/CONTEXT.md | after: C3 | test: a dumped table from a state parses with the documented stride | journal: r6-actor-table

### Phase 3 — the frame, and your first capture

- [ ] **C1**  Get PCSX2 running the game under the kit: `kit pcsx2 start`, `kit pcsx2 status`, `kit shot` for a screenshot, each on a kit-managed copy with its own config. Confirm `KIT_PCSX2=2` gives a second, independent copy | files: kit.toml, docs/PCSX2.md | after: D1 | test: kit shot` writes a non-black PNG of the title screen | journal: c1-emulator
- [ ] **C2**  The vsync counter: `kit probe vsync` finds a word in RAM that goes up by one per frame; confirm it stalls when the game pauses and put it in `[emu] vsync_addr`. Every capture and every timing comparison is counted in this one number | files: kit.toml | after: C1 | test: kit pine watch <addr>` shows +1 per frame for 300 frames | journal: c1-emulator
- [ ] **C3**  Save states for the places you will keep coming back to (a human makes these: see `plan/HUMAN.md`) — the title screen, the first in-game frame, mid-action — and a note of what each one holds. Document which slots are the human's (never overwritten) and which are scratch | files: plan/HUMAN.md, plan/REFERENCE.md | after: C1 | test: none | journal: c3-save-states
- [ ] **C4**  First recording: `kit record` with a watch list (a few addresses from R6) over a few hundred frames from a save state, to a CSV in `context/captures/`. Check it yourself for the failure that costs the most time — a short or stalled capture that looks fine — and confirm the tool shouts `CAPTURE FAILED` when you kill the emulator mid-run | files: ps2kit/record.py | after: C3 | test: the CSV has one row per frame with no vsync gaps | journal: c4-first-capture
- [ ] **C5**  The pad: `kit pad` presses a button on the running game and the game reacts. Then find where the game reads the pad in RAM (the bitmask per pad, and whether it keeps "pressed this frame" separately) | files: kit.toml, docs/PCSX2.md | after: C1 | test: a scripted button press changes the menu selection in a capture | journal: c5-pad

### Phase 4 — the arithmetic, before any ported math

- [ ] **F1** The EE FPU (our `ps2-float` library, FIZL0/ps2-float — already ported; check it against this game, fix it there with a test and a tagged release, bump the tag here): add, sub, mul, div, sqrt with the PS2's rules — round toward zero, no denormals (flushed), no NaN or infinity (clamped to the largest magnitude), and the smaller addend masked to the exponent difference minus one guard bit. Port physics before this and you will not know which of the two is wrong | files: src/fpu.rs | test: ps2-float tests/fpu.rs | journal: f1-ee-fpu
- [ ] **F2** `madd.s`/`msub.s` and the accumulator: the product is truncated before the add, so a multiply-add is *not* a multiply then an add. Most of a PS2 game's vector math goes through these, so a wrong one shows up as a tiny drift that takes days to find (ps2-float has them; check, and fix there if this game disagrees) | files: src/fpu.rs | test: ps2-float tests/fpu.rs | journal: f1-ee-fpu
- [ ] **F3** VU0 macro mode: the ops the game actually uses (`vmul`, `vadd`, `vmadd`, `vmula`, `vdiv`, `vsqrt`, `vrsqrt`, the clipping and min/max ones), with VU0's own rounding and its flag behaviour. Take the list from the disassembly rather than implementing the whole ISA; add what ps2-float lacks there (test, tagged release, tag bumped here) | files: src/vu.rs | test: ps2-float tests/vu.rs | journal: f3-vu0
- [ ] **F4**  The game's own transcendentals: whatever `sinf`, `cosf`, `atan2f`, `sqrtf` and `powf` the disc links. They are rarely the standard library's, and a port that calls the host's `sin` will differ in the last bits of every angle. ps2-float's `libm` is the SDK's fdlibm (`libm::sce` one game's variants): check them against the oracle over the full input range, and port the disc's own into ps2-float where they differ | files: src/libm.rs, research/oracle.txt | after: O1 | test: crates/kit-oracle/tests/game.rs | journal: f4-libm
- [ ] **F5**  The matrix and quaternion helpers the game uses for bones and cameras, built on F1–F3 with the same op order as the original (the order changes the last bits) | files: crates/game-sim/src/math.rs | after: F3 | test: crates/game-sim/tests/math.rs | journal: f5-matrix

### Phase 5 — the first proven function, and the harness

- [ ] **O1**  The oracle, against one trivial function: `kit oracle` interprets the real code from a RAM dump and calls it with your arguments. Prove it on something with an obvious answer first (a clamp, a distance, a table lookup) so the interpreter is a tool rather than a suspect. Add any unknown opcode it panics on to our ps2-oracle library (FIZL0/ps2-oracle: a test, a tagged release, the tag bumped in crates/kit-oracle/Cargo.toml) | files: crates/kit-oracle/**, research/oracle.txt | after: R1 | test: crates/kit-oracle/tests/basics.rs | journal: o1-oracle
- [ ] **O2**  Oracle coverage for the arithmetic of F1–F4: run each ported op against the real code over ≥10k generated inputs, including the awkward ones (zero, denormals, the largest magnitudes, equal exponents, exponent differences of 24 and over); a disagreement is fixed in ps2-float and released | files: crates/kit-oracle/tests/**, research/oracle.txt | after: O1 | test: crates/kit-oracle/tests/float.rs | journal: o2-float-oracle
- [ ] **A1**  The state hash: one function that walks the whole simulation state and hashes it, per tick, with a dump mode that says which field first differed. On an almost empty simulation this feels pointless; it is the highest-leverage task in the project, because from here on every port either keeps the hashes matching or does not, and you find out in seconds | files: crates/game-sim/src/hash.rs | after: F1 | test: crates/game-sim/tests/hash.rs | journal: a1-state-hash
- [ ] **A2**  The replay harness: a fixture of recorded inputs plus the initial state, run through the simulation, compared tick by tick against the captured hashes or watch values. Make a one-line failure (`tick 413: player.vel.x 1.5 vs 1.500001`), not a diff of two files | files: crates/game-sim/src/replay.rs, crates/game-sim/tests/replay.rs | after: A1 | test: crates/game-sim/tests/replay.rs | journal: a2-replay-harness
- [ ] **A3**  Make the hash platform-independent and prove it: the same fixture hashes identically on x86-64 and on aarch64 (and on wasm32 if this port will run in a browser). Transcendentals go through the game's own `libm`, never the host's | files: crates/game-sim/tests/platforms.rs | after: A2 | test: crates/game-sim/tests/platforms.rs | journal: a3-platforms

### Phase 6 — input, the game's own way

- [ ] **I1**  The input layer: pads mapped to the game's own bitmask, with "held", "pressed this frame" and "released" exactly as the original keeps them (including the frame it latches them, which decides whether a press on the frame of a state change counts) | files: crates/game-sim/src/input.rs | after: C5 | test: crates/game-sim/tests/input.rs | journal: i1-input
- [ ] **I2**  Analogue sticks: the original's dead zone, clamping and any curve, in its arithmetic. An approximated stick curve quietly changes every movement in the game | files: crates/game-sim/src/input.rs | after: I1 | test: crates/game-sim/tests/input.rs (oracle) | journal: i1-input
- [ ] **I3**  Input recording and playback in the port: a fixture of pad frames drives a run, so a human-played sequence from the real game can be replayed against the port (this is what A2's fixtures are made of) | files: crates/game-sim/src/replay.rs | after: I1 | test: crates/game-sim/tests/replay.rs | journal: i3-input-fixtures

### Phase 7 — one subsystem, end to end

Pick the subsystem this game is *about* (the thing the player does every second) and take it all the way:
ported, oracle-checked, replay-proved. One finished subsystem teaches more about the codebase than five
half-ported ones, and it is the first time the port is worth running.

- [ ] **S1**  The fixed update: the tick order from R3, as the top-level step of the simulation — what updates before what, where input is read, where the RNG is drawn from, what happens on the frames the game is loading or paused | files: crates/game-sim/src/lib.rs | after: R3 | test: crates/game-sim/tests/tick.rs | journal: s1-fixed-update
- [ ] **S2**  Actor lifetime: how actors are created, freed and iterated (order matters: it decides RNG draw order and collision resolution order) | files: crates/game-sim/src/actor.rs | after: S1 | test: crates/game-sim/tests/actor.rs | journal: s2-actors
- [ ] **S3**  Movement: the player's (or main actor's) acceleration, friction, maximum speed and turning, in the original's arithmetic and its update order. Prove each piece with the oracle, then the whole with a short replay fixture | files: crates/game-sim/src/move.rs | after: S2 | test: crates/game-sim/tests/move.rs | journal: s3-movement
- [ ] **S4**  Collision against the world: the original's shape test and its sweep, including how it resolves a hit and how many iterations it allows. Port the test it uses, not a better one — a more accurate collision is a different game | files: crates/game-sim/src/collide.rs | after: S1 | test: crates/game-sim/tests/collide.rs | journal: s4-collision
- [ ] **S5**  The state machine of the main actor (idle, move, the game's own actions): states, transitions, the frame counts, and which transitions can happen on the same frame | files: crates/game-sim/src/state.rs | after: S1 | test: crates/game-sim/tests/state.rs | journal: s5-states
- [ ] **S6**  Animation driving: which motion plays per state, its frame advance and blending, to the frame. Looks are flexible, timing is not: a motion that is one frame out moves the action frames with it | files: crates/game-sim/src/motion.rs | after: S5 | test: crates/game-sim/tests/motion.rs | journal: s6-motion
- [ ] **S7**  The camera for normal play: its target, its lag and its limits. It does not have to be bit-exact unless it feeds the simulation — check whether it does before deciding | files: crates/game-sim/src/camera.rs | after: S1 | test: crates/game-sim/tests/camera.rs | journal: s7-camera
- [ ] **S8**  The acceptance run: a full recorded sequence of real play (several minutes, from a human recording) replays tick-exact against the port. This is the point where the port stops being a collection of functions. Expect it to fail first on the smallest thing — that is what A1's field-level diff is for | files: crates/game-sim/tests/replay.rs | after: A2 | test: crates/game-sim/tests/replay.rs | journal: s8-acceptance

### Phase 8 — showing it

- [ ] **V1**  Load the game's models and textures well enough to draw one actor and the world it stands in, from the user's disc at runtime. Rendering only has to look right, so stop at "recognisably the same scene" and move on | files: crates/game-sim/src/** | after: D5 | test: a screenshot comparison by eye against `kit shot | journal: v1-first-draw
- [ ] **V2**  Side-by-side screenshots: the same moment in the original and in the port, from the same save state, in `context/shots/`, with the differences listed. Do this once a subsystem lands, not once at the end → context/shots/ | after: V1 | test: none (by eye) | journal: v2-side-by-side

### Stretch

Worth doing, not worth blocking on. Nothing here is picked up by an unattended run.

- [ ] **K1** A `kit probe` recipe per kind of value you have had to hunt for (a counter, a score, a position, a flag) — written down as recipes rather than rediscovered | files: docs/PROVING.md | test: none | journal: k1-probe-recipes
- [ ] **A4** A fuzzer over the replay harness: random input fixtures, looking for a tick where the port and a recording diverge in a way the handwritten fixtures never hit | files: crates/game-sim/tests/fuzz.rs | test: crates/game-sim/tests/fuzz.rs | journal: a4-fuzz
- [ ] **S9** The rest of the game modes from R4 (menus, loading, results): the front end does not have to match the original, so design it as you like — but the sim-facing parts of each mode (what it seeds, what it carries between matches) do | files: crates/game-sim/src/mode.rs | test: crates/game-sim/tests/mode.rs | journal: s9-modes
- [ ] **V3** Widescreen and high resolution as a layer on top: with it off, the game must draw exactly as the original did → render crate | test: none (by eye) | journal: v3-widescreen
