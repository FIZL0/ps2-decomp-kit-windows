//! PlayStation 2 floating-point arithmetic, bit for bit.
//!
//! A re-implementation of a PS2 game is a guessing game until its arithmetic matches. The EE's FPU is
//! *not* IEEE 754, so a port that uses the host's `f32` drifts from the original: not visibly on frame
//! one, but a few ulps per operation, compounding through a physics integrator until a ball lands on the
//! other side of a line and every decision after that is different. This crate is the model that makes
//! the drift zero.
//!
//! ```
//! use ps2_float::{F, f};
//!
//! // the original: pos = pos + vel * dt;  (mul.s then add.s)
//! let (mut pos, vel, dt) = (f(0.0), f(1.0 / 3.0), f(1.0 / 60.0));
//! pos = pos + vel * dt;
//!
//! // the original: pos = pos + vel * dt;  compiled to one madd.s — a *different* value
//! let other = f(0.0).madd(vel, dt);
//! assert_eq!(pos.to_bits(), other.to_bits());  // same here; not for every input
//! ```
//!
//! # What is wrong with the EE's FPU
//!
//! Six deviations from IEEE 754 single precision, each of which changes results:
//!
//! 1. **One rounding mode: toward zero.** Every `add.s`, `sub.s`, `mul.s`, `madd.s` truncates. No
//!    round-to-nearest, no rounding-mode bits to set. A sum of 1000 positive terms is systematically
//!    *low*, not randomly off by half an ulp either way; this alone is why a naive port's trajectories
//!    sag.
//! 2. **A short adder.** Before an add or subtract the smaller operand is shifted right to line the
//!    exponents up, but the hardware keeps **one** guard bit, not the full width. At an exponent
//!    difference of 25 or more the smaller operand contributes nothing at all: `1.0 + 1e-8 == 1.0`
//!    exactly. See [`fpu::align`].
//! 3. **No infinities and no NaNs.** The largest exponent is just another number. An overflow saturates
//!    to `±1.7014118e38` ([`fpu::MAX`]), `1.0/0.0` is `+MAX`, and `0.0/0.0` is `+MAX` too (the divider
//!    takes the sign of the operands and nothing else). Code that leans on `f32::NAN` or `is_infinite`
//!    is describing hardware the PS2 does not have.
//! 4. **No denormals.** They are flushed to zero on the way in *and* on the way out, so the smallest
//!    nonzero magnitude is `1.1754944e-38`. This is why a decaying value snaps to exactly zero on the
//!    PS2 and crawls on a PC.
//! 5. **`madd.s` is not FMA.** It chops the product, then adds that to the accumulator through the same
//!    short adder. Writing it as Rust's `mul_add` (a true fused multiply-add) is wrong in both
//!    directions; writing it as `a + b * c` is wrong only when the compiler used `madd.s`, which it did
//!    constantly. The decompilation tells you which — model it with [`F::madd`] when it is one.
//! 6. **Divide and square root round to nearest**, unlike everything else, because they come out of a
//!    different unit. `rsqrt.s` is `a / sqrt(b)` with one rounding.
//!
//! VU0, which games use through macro mode (`vmulax`, `vmadd` …) for vectors, matrices and normalisation,
//! chops like the FPU but has a *full-width* adder and clamps instead of saturating. See [`vu`] — mixing
//! the two models up is the most common reason a port that "uses the PS2 float model" still drifts.
//!
//! # The three pieces
//!
//! | Module | Model |
//! |---|---|
//! | [`fpu`] | EE COP1 scalar single precision, and the [`F`] wrapper type |
//! | [`vu`] | VU0 macro mode: [`Vf`] 4-lane vectors, field masks, clamping |
//! | [`libm`] | `sinf`/`powf`/… the way the game's own C library computes them |
//!
//! # Using it in a port
//!
//! Write the ported expression the way the original's decompilation reads, with [`F`] instead of `f32`:
//!
//! ```
//! use ps2_float::{F, f};
//! fn step(y: F, vy: F, dt: F) -> (F, F) {
//!     let g = f(-9.8);
//!     (y + vy * dt, vy + g * dt)   // two mul.s, two add.s, in the original's order
//! }
//! # let _ = step(f(1.0), f(0.0), f(0.016666668));
//! ```
//!
//! Rust never reassociates or contracts floating point, so the operations you write are the operations
//! that happen, in order — which is the whole reason this reads as plainly as it does.
//!
//! **Then lock the host's floats out.** Add this at the top of every module of the port that does sim
//! arithmetic:
//!
//! ```ignore
//! #![deny(clippy::float_arithmetic)]   // crate root
//! // or, per module:
//! #[deny(clippy::float_arithmetic)]
//! mod ball { /* … */ }
//! ```
//!
//! `clippy::float_arithmetic` rejects every `+ - * / %` on a bare `f32`/`f64`, so a stray host-float
//! expression cannot creep back in during a refactor — operations on [`F`] and [`Vf`] are method calls
//! on a struct and pass the lint. Where the port genuinely needs a host float (a rendering path, a
//! debug print), opt out at that one spot with `#[allow(clippy::float_arithmetic)]` and say why in a
//! comment. Run it as `cargo clippy --workspace -- -D warnings`.
//!
//! The matching rule for transcendentals: call [`libm`], never `f32::sin` and friends, because `std`'s
//! accuracy is the platform's and a test that passes on your machine will fail on CI or on an arm64
//! laptop. [`libm`]'s docs explain this at length.
//!
//! # What this crate cannot do for you
//!
//! It gives you the arithmetic, not the answers. The game's *constants* (polynomial coefficients, tuning
//! tables) still have to come out of its executable, its `libm` may be a slightly different vintage from
//! the one here, and the only way to know is to run the original's own code: see `ps2-oracle` and
//! `kit oracle`.
//!
//! It also models no exception flags, no FPU status/condition bits beyond the compare flag an
//! interpreter needs, and no timing.

#![no_std]
#![forbid(unsafe_code)]

pub mod fpu;
pub mod libm;
pub mod vu;

pub use fpu::{F, f, f32s, fs};
pub use vu::{Dest, Field, Lane, Lanes, V4, Vf, lanes, v4};
