//! **This is the crate you fill in: the game's own logic, ported.**
//!
//! It starts nearly empty on purpose. What is here is the shape that has survived contact with a real
//! port — the rules that are annoying to adopt later and cheap to adopt now. Delete the examples as you
//! replace them; keep the rules.
//!
//! # The shape
//!
//! ```text
//! crates/game-sim/src/
//!   lib.rs        this: the state type, the tick, the hash
//!   physics.rs    one module per subsystem of the original, named after what it does
//!   player.rs     …not after the file it was found in, and never after an address
//!   rules.rs
//!   replay.rs     the acceptance test: same input, same hash
//! ```
//!
//! Five rules, each of which exists because breaking it costs a week:
//!
//! 1. **No `f32` arithmetic operators anywhere in here.** Every float goes through [`ps2_float`]: `F` for
//!    scalars, `Vf` for VU0 vectors. The PS2's FPU chops where IEEE rounds and saturates where IEEE makes
//!    an infinity, so `a + b * c` in plain Rust is *a different number* from the original's, usually in
//!    the last bit and occasionally not. Every module in this crate starts with
//!    `#![deny(clippy::float_arithmetic)]`, which turns "I forgot" into a build failure.
//! 2. **No `std` transcendentals.** `sinf`, `atan2f`, `powf` and friends come from [`ps2_float::libm`],
//!    which is the algorithm the PS2 SDK shipped. `f32::sin` is accurate to whatever the host's libm
//!    feels like, which differs between x86-64 and aarch64, between glibc versions, and between debug and
//!    release — and each difference is a test that passes on your machine and fails in CI.
//! 3. **No hidden state.** Everything the simulation reads lives in one state value (here [`Sim`]), so a
//!    snapshot is a copy and a replay is a loop. `static mut`, thread locals, lazily-initialised caches
//!    and "I'll just read the clock" all end the same way: a desync nobody can reproduce.
//! 4. **No rendering, no input polling, no files, no time.** This crate is a pure function of (state,
//!    input). The app layer draws it and feeds it. That is what lets a test run ten thousand ticks a
//!    second, and what lets the same code run in a browser or in a netplay rollback.
//! 5. **Every ported function is proved.** `ps2-oracle` calls the original over generated inputs and
//!    compares bit for bit; `replay` proves whole matches. "It looks right" is not a result.
//!
//! # The loop
//!
//! ```
//! use game_sim::{Sim, Input};
//! let mut sim = Sim::new(0x1234_5678);
//! for _ in 0..60 {
//!     sim.tick(Input::default());
//! }
//! assert_eq!(sim.ticks, 60);
//! ```
//!
//! [`Sim::hash`] is the whole state in 64 bits, identical on every platform (see
//! [`state_hash`]). It is the single most useful thing in a port: print it per tick and a divergence
//! from the original — or between two machines — has an exact frame number instead of a vague
//! "sometime in the second rally".

#![forbid(unsafe_code)]
// Float operators are denied per module (see each `mod` below) rather than crate-wide, so that the few
// places that legitimately need host arithmetic — a hash, a test's statistics — can opt out knowingly.

use ps2_float::{F, Vf};

/// One frame of controller input. Replace with the game's own buttons; keep it `Copy` and small, because
/// a replay fixture is a list of these.
#[derive(Clone, Copy, Default, Debug, PartialEq, Eq, Hash)]
pub struct Input {
    pub buttons: u16,
    pub stick: (i8, i8),
}

/// The entire simulation state.
///
/// One value, no pointers, `Clone` for snapshots. When this grows past a screenful, split it into
/// per-subsystem structs that live here — not into globals.
#[derive(Clone, Debug)]
pub struct Sim {
    pub ticks: u64,
    pub rng: u32,
    pub ball: physics::Ball,
}

impl Sim {
    /// A fresh state from a seed. The seed is part of the state and part of a replay fixture: whatever
    /// the original seeded its RNG with (a frame counter, a button press pattern) comes in here, never
    /// from the clock.
    pub fn new(seed: u32) -> Sim {
        // a serve, so the example has something to integrate
        Sim {
            ticks: 0,
            rng: seed,
            ball: physics::Ball { vel: Vf([0.5, 3.0, 0.0, 0.0]), spin: F(1.0), ..Default::default() },
        }
    }

    /// Advance one frame. This is the function whose every detail has to match the original, including
    /// the *order* the subsystems run in — which is nearly always the order the original's main loop
    /// calls them, and nearly never the order that reads nicely.
    pub fn tick(&mut self, _input: Input) {
        self.ball.step();
        self.ticks += 1;
    }

    /// The state in 64 bits. Add a field here the moment you add it to the state, or a replay test will
    /// pass over a divergence it cannot see.
    pub fn hash(&self) -> u64 {
        let mut h = state_hash(&self.ticks.to_le_bytes(), 0);
        h = state_hash(&self.rng.to_le_bytes(), h);
        self.ball.hash(h)
    }
}

/// FNV-1a over bytes, seeded with the running value: integer-only, so the answer is the same on x86-64,
/// aarch64 and wasm32, which is the entire point.
///
/// Floats are hashed as their *bit patterns*, never as values: `0.0 == -0.0` is true, and a port that has
/// drifted to `-0.0` has drifted. Equally, never hash a `f64`, a `HashMap`'s iteration order, or a
/// pointer; all three vary between runs of the same binary.
pub fn state_hash(bytes: &[u8], seed: u64) -> u64 {
    let mut h = if seed == 0 { 0xcbf2_9ce4_8422_2325 } else { seed };
    for &b in bytes {
        h ^= b as u64;
        h = h.wrapping_mul(0x1000_0000_01b3);
    }
    h
}

/// An example subsystem, and the only thing in this crate that does any arithmetic. Written the way a
/// ported function should be: PS2 floats, the original's operation order, and a comment saying where it
/// came from in words — never an address or a decompiler's name for it.
pub mod physics {
    #![deny(clippy::float_arithmetic)] // the lint that keeps a bit-exact port bit-exact

    use super::*;

    /// Gravity, in units per frame squared, as the original's data table holds it.
    const GRAVITY: f32 = -0.15;
    /// How much of its spin a ball keeps each frame.
    const SPIN_DECAY: f32 = 0.98;

    /// A thing that flies. Two vectors and nothing else; the original's object is bigger, and the rest of
    /// it belongs to whichever subsystem actually reads it.
    #[derive(Clone, Copy, Debug, Default)]
    pub struct Ball {
        pub pos: Vf,
        pub vel: Vf,
        pub spin: F,
    }

    impl Ball {
        /// One frame of free flight: `vel += gravity`, then `pos += vel`.
        ///
        /// Note the order and the lack of a `dt`: a PS2 game's integration step is one frame, and
        /// "improving" this to `pos += vel * dt` changes every trajectory in the game.
        pub fn step(&mut self) {
            // Vectors through `Vf` (VU0's adder), scalars through `F` (the EE FPU's). They are not the
            // same model — see `ps2_float` — so which one a value goes through is part of the port.
            self.vel = self.vel.add(Vf([0.0, GRAVITY, 0.0, 0.0]));
            self.pos = self.pos.add(self.vel);
            self.spin *= SPIN_DECAY;
        }

        /// Hashed as bit patterns, in a fixed order.
        pub fn hash(&self, seed: u64) -> u64 {
            let mut h = seed;
            for v in [self.pos, self.vel] {
                for b in v.to_bits() {
                    h = state_hash(&b.to_le_bytes(), h);
                }
            }
            state_hash(&self.spin.to_bits().to_le_bytes(), h)
        }
    }
}

/// The acceptance test for the whole port: same input, same state, frame by frame.
///
/// **Stub — this is where a real port's hours go, and it is worth them.** The idea, which has caught more
/// bugs in practice than any unit test:
///
/// 1. Record a session on the real game: the input every frame (`kit record`), plus whatever state you
///    can read per frame (positions, scores, an RNG word) and the frame it was read on.
/// 2. Write it to a fixture file next to the test — *not* into git if it contains game data.
/// 3. Feed the same inputs to [`Sim`] and compare at every tick, not just at the end. The first
///    disagreement is the bug; everything after it is noise.
/// 4. Keep one fixture that is quick (a few seconds of play, run on every `kit check`) and the long
///    ones behind `#[ignore]`, with a line in the doc comment saying how to run them.
///
/// Once it passes, it is also a regression test for every refactor afterwards, and — if the hash is in
/// it — a cross-platform test for free: run the same fixture on wasm32 and aarch64 and the hashes must
/// match exactly.
pub mod replay {
    use super::{Input, Sim};

    /// One recorded session.
    #[derive(Clone, Debug, Default)]
    pub struct Fixture {
        /// What the original was seeded with.
        pub seed: u32,
        /// One entry per frame.
        pub inputs: Vec<Input>,
        /// Optional: the expected [`Sim::hash`] per frame. Empty means "only the end state is checked",
        /// which is how a fixture starts life before the hashes are trustworthy.
        pub hashes: Vec<u64>,
    }

    /// Replay a fixture and return the first tick whose hash disagrees, if any.
    pub fn verify(f: &Fixture) -> Result<Sim, (u64, u64, u64)> {
        let mut sim = Sim::new(f.seed);
        for (k, &input) in f.inputs.iter().enumerate() {
            sim.tick(input);
            if let Some(&want) = f.hashes.get(k) {
                let got = sim.hash();
                if got != want {
                    return Err((sim.ticks, got, want));
                }
            }
        }
        Ok(sim)
    }
}

/// The worked example from `docs/EXAMPLE.md`: three functions of the synthetic program that `kit demo`
/// generates, ported, with `tests/demo.rs` proving them bit-exact against the original's instructions.
/// Delete it once your own port has something real in it.
pub mod demo;

#[cfg(test)]
mod tests {
    use super::*;

    /// The hash is a constant of the code, not of the machine: this value must be the same on x86-64,
    /// aarch64 and wasm32. When a change to the state type changes it, that is expected — update it. When
    /// a *platform* changes it, something in here is using host arithmetic, and that is the bug this test
    /// exists to catch.
    #[test]
    fn a_hundred_ticks_hash_the_same_everywhere() {
        let mut sim = Sim::new(1);
        for _ in 0..100 {
            sim.tick(Input::default());
        }
        assert_eq!(sim.hash(), 0x481b_a2a2_d10e_b072, "{:?}", sim.ball);
    }

    #[test]
    fn a_replay_reports_the_first_frame_that_differs() {
        let mut sim = Sim::new(7);
        let inputs = vec![Input::default(); 10];
        let hashes: Vec<u64> = inputs.iter().map(|&i| {
            sim.tick(i);
            sim.hash()
        }).collect();
        let good = replay::Fixture { seed: 7, inputs: inputs.clone(), hashes: hashes.clone() };
        assert!(replay::verify(&good).is_ok());

        let mut bad = good.clone();
        bad.hashes[4] ^= 1;
        let (tick, _, _) = replay::verify(&bad).expect_err("a changed hash must be noticed");
        assert_eq!(tick, 5, "the fifth tick is the one that differs");
    }
}
