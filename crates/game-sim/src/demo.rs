//! A worked example: one function of the synthetic game, ported, and the proof that it matches.
//!
//! `kit demo` assembles a fake PS2 program — see `ps2kit/demo.py`, every byte of which is generated,
//! none of it from any real game. This module is the port of three of its functions, written exactly the
//! way a real one should be: PS2 arithmetic, the original's operation order, named after what it does,
//! with a comment saying where it came from in words. `tests/demo.rs` proves all three against the
//! original's own instructions over ten thousand generated inputs, and shows what happens when the same
//! code is written with the host's `f32` instead.
//!
//! Read `docs/EXAMPLE.md` for the walkthrough that produced it, and delete this module when your own port
//! has something real in it.
#![deny(clippy::float_arithmetic)] // the lint that keeps a bit-exact port bit-exact

use ps2_float::{f, F};

/// The weight table the original reads, as eight floats in its `.data`.
///
/// Read out of the program image with `kit data @weights 0x20 --as f32 -p boot`, not typed from a
/// decompiler's guess at the literals — a table copied by hand is a table with a typo in it. The order
/// matters: the original accumulates them front to back, and chopped addition is not associative, so
/// summing them in any other order gives a different answer.
pub const WEIGHTS: [f32; 8] = [1.0, 0.25, 0.3333333, 7.5e-4, 1024.5, 0.0078125, 3.0e-5, 12.345679];

/// The step table, eight signed ints immediately after the weights.
pub const STEPS: [i32; 8] = [0, 3, 7, 12, 18, 25, 33, 42];

/// Clamp `x` into `[lo, hi]`.
///
/// Three exits and two `slt`s in the original. Ported for completeness: integer code is the part a port
/// gets right by reading it, which is worth knowing because it is *not* how the float code goes.
///
/// One detail that is not obvious from the C-like reading: the original compares with `slt`, which on the
/// R5900 compares the full 64-bit registers. The arguments arrive sign-extended from 32 bits, so for
/// anything a 32-bit caller can pass this is the same as an `i32` comparison — which is why the port is
/// allowed to be this plain, and why the test passes `i32`s and nothing wider.
pub fn clamp_index(x: i32, lo: i32, hi: i32) -> i32 {
    if x < lo {
        lo
    } else if hi < x {
        hi
    } else {
        x
    }
}

/// The step for a gear, clamped to the table.
///
/// The original clamps with a call to the function above and then indexes its table; the clamp is why an
/// out-of-range gear is not a crash. Note what the port does *not* do: it does not range-check and return
/// an `Option`, because the original does not, and a port that is safer than the original is a port that
/// behaves differently.
pub fn step_for_gear(gear: i32) -> i32 {
    STEPS[clamp_index(gear, 0, STEPS.len() as i32 - 1) as usize]
}

/// A weighted blend of `x`, plus `x` squared.
///
/// The original is a loop of eight `mul.s`/`add.s` pairs into a running sum, then `adda.s` to move that
/// sum into the FPU's accumulator and one `madd.s` for the squared term.
///
/// Four things in those two sentences are the whole reason this crate exists:
///
/// * **The sum is chopped at every step.** Eight truncating additions of terms that differ in magnitude
///   by twenty bits (`1024.5 * x` against `3.0e-5 * x`) lose bits that IEEE round-to-nearest would keep.
/// * **The adder keeps one guard bit.** Once the running sum passes `1024.5 * x`, the small terms shift
///   out from under it, and some of them stop contributing at all.
/// * **The order is the original's.** Front to back, one accumulator. Reassociating, vectorising, or
///   using `iter().sum()` all give a different number.
/// * **`madd.s` is not a fused multiply-add.** The product `x * x` is chopped to `f32` *first*, and only
///   then added to the accumulator, through the same short adder. [`F::madd`] models exactly that;
///   Rust's `mul_add` models a true FMA, which the PS2 does not have.
///
/// `tests/demo.rs` holds the consequence: written with host `f32` operators instead, this function
/// disagrees with the original within a few dozen inputs.
pub fn blend_weights(x: F) -> F {
    let mut sum = f(0.0);
    for &w in &WEIGHTS {
        // mul.s then add.s, in that order, into the same register the next term reads.
        sum += F(w) * x;
    }
    // adda.s $f4, $f6 with a zero in $f6 — "ACC = sum" — then madd.s: ACC + chop(x * x).
    sum.madd(x, x)
}

/// The same arithmetic, written the way anyone would write it in Rust if nobody had told them about the
/// PS2's FPU. **It is wrong**, and it is here to be proved wrong.
///
/// Nothing in a port may look like this; the `#![deny(clippy::float_arithmetic)]` at the top of every
/// module is what stops it, and this one function has to opt out of its own crate's rule to exist.
#[allow(clippy::float_arithmetic)]
pub fn blend_weights_in_host_f32(x: f32) -> f32 {
    let mut sum = 0.0f32;
    for &w in &WEIGHTS {
        sum += w * x;
    }
    sum + x * x
}
