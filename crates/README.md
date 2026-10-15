# crates/ — the Rust side: arithmetic, the oracle, your port

Three crates, no dependencies on anything outside this directory (and none on crates.io at all — see
*Dependencies* below).

| Crate | What it is | Do you edit it? |
|---|---|---|
| `ps2-float` | The PS2's arithmetic: EE FPU, VU0, and the SDK's libm | No. It is a model of the hardware. |
| `ps2-oracle` | An R5900 interpreter that calls the *original game's* functions | Only to teach it an instruction it does not know yet. |
| `game-sim` | **Your port.** Starts as a skeleton with the rules baked in | Yes. This is the work. |

```
cargo test --workspace      # everything; disc-dependent tests skip with a message saying what is missing
cargo test -p ps2-float     # the arithmetic model's own properties, no setup needed
kit check                 # the same thing, summarised to a few lines (what an agent should run)
```

## Why a float crate at all

The Emotion Engine's FPU is not IEEE 754. It chops instead of rounding, it throws away the low bits of
the smaller addend, it has no infinities and no NaNs, and it flushes denormals to zero. `a + b * c` in
plain Rust is therefore *a different number* from the same expression on a PS2 — usually in the last
bit, and occasionally, after a few hundred frames of feedback, not in the last bit at all.

So: in `game-sim`, floats go through `ps2_float::F` (scalars) and `ps2_float::Vf` (VU0 vectors), the
modules carry `#![deny(clippy::float_arithmetic)]`, and transcendentals come from `ps2_float::libm`
rather than `std` — because `f32::sin` is accurate to whatever the host's libm feels like, which differs
between x86-64 and aarch64 and between glibc versions. The crate docs explain each deviation and what it
looks like when you get it wrong.

## Why an interpreter

Because "my port matches a recording of normal play" is a weak claim. The oracle loads the boot ELF and
the overlays off your disc, runs one function to its `jr $ra`, and hands you the result — in
microseconds, with no emulator. A test can then make ten thousand calls with inputs the game never
produced, and compare bit for bit:

```rust
let mut ee = Ee::game()?;                       // disc serial checked first
for _ in 0..10_000 {
    let x = rng.float(1.0e4);                   // including the awkward sizes
    ee.call(addr("atanf")?, &[], &[x], 100_000);
    assert_eq!(ee.f0().to_bits(), my_port::atanf(x).to_bits(), "atanf({x})");
}
```

Addresses live in the `[oracle] addresses` file (`kit oracle add <name> <addr>`), never in Rust
source: an address belongs to one build of one game, and the name is the documentation. From a shell:

```
kit oracle list
kit oracle add ball_speed 0x1001c0   "ball speed in km/h (a0 = ball)"
kit oracle ball_speed --ints 0x1f4a20 --ram 3
```

It can prove what a function *computes*. It cannot prove timing, frame order, DMA, VU1 or the GS —
nothing that depends on *when* it ran. Record the real machine for those (`kit record`), and read
`crates/ps2-oracle/src/lib.rs`'s header before trusting any of it.

An instruction the interpreter does not implement panics with the address and the encoded word. That is
a feature: add the instruction (a dozen lines in `src/ee.rs`, next to its neighbours) rather than working
around it, and `kit dis <addr>` will show you what it was.

## Where to start on `game-sim`

1. Read `src/lib.rs`. It is mostly comments: the five rules, the shape, and a worked example you delete.
2. Port one small function. Name the module after what the code *does*, never after an address or a
   decompiler's `FUN_…`.
3. Prove it with the oracle over ≥10,000 generated inputs (`crates/ps2-oracle/tests/game.rs` is a
   copyable template).
4. Then build the replay harness (`game-sim/src/replay.rs`, a documented stub today). Same inputs, same
   state hash, frame by frame — it catches the bugs unit tests cannot see, and it keeps catching them
   through every refactor afterwards.

## Dependencies

There are none, deliberately. The whole Rust side builds on a bare `cargo` with no network, nothing to
audit and nothing to break in five years — the arithmetic is a few hundred lines of integer bit work and
`kit.toml` is read by a ~120-line TOML subset parser in `ps2-oracle/src/kit.rs`. If you add a dependency,
put the reason in a comment next to it in the manifest, as the existing (empty) `[dependencies]` sections
do.

`ps2-float` is `#![no_std]` and `#![forbid(unsafe_code)]`, so a port can run in a browser (wasm32) or
anywhere else without surprises, and the test suite checks that its results are identical on every
platform.
