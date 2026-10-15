//! The EE's COP1 — the R5900's single-precision FPU — as the reference emulator (PCSX2) performs it, so a
//! ported formula reproduces the original's recorded values bit for bit.
//!
//! The EE FPU is *not* an IEEE 754 unit. A port that uses the host's `f32` arithmetic will agree with the
//! original on most inputs and disagree on a few parts in 10^7, which is exactly enough to send a physics
//! step, an AI decision or an RNG stream down a different branch twenty frames later. The differences:
//!
//! * **Round toward zero, always.** There is no round mode bit a game can change: every `add.s`, `sub.s`,
//!   `mul.s`, `madd.s` result is truncated ("chop"), never rounded to nearest. Error accumulates in one
//!   direction, which is why a chopping port and a rounding one drift apart instead of jittering around
//!   each other.
//! * **The adder pre-aligns badly.** Before adding, the smaller operand is shifted right to line the
//!   exponents up, and the EE keeps only **one guard bit**: with an exponent difference `d` (1..24) the
//!   low `d - 1` mantissa bits of the smaller operand are simply dropped, and at `d >= 25` the whole
//!   operand disappears (its sign survives). Both operands then add exactly. So `1.0 + 1e-8 == 1.0`, and
//!   `0.95 - 0.25 * 0.05000001` lands on a different `f32` than IEEE gives.
//! * **No infinities and no NaNs.** The exponent field never reaches 255 in a result: an overflow
//!   saturates to `±1.7014118e38` (`0x7f7fffff`). An operand *read* with exponent 255 is taken as that
//!   same maximum, so a bit pattern a game stored as "infinity" behaves as a big finite number
//!   ([`operand`]).
//! * **No denormals.** A denormal operand reads as `±0` and a denormal result is flushed to `±0`
//!   (sign preserved).
//! * **Divide and square root round to nearest**, unlike everything else: in the reference emulator the
//!   divider has its own rounding, and `x / 0` yields `±max` rather than a trap or an infinity.
//! * **`madd.s` is not fused.** The product is rounded (chopped) to `f32` first and then goes through the
//!   same badly-aligning adder. `madd(acc, a, b)` is therefore *exactly* `add(acc, mul(a, b))` — it is a
//!   single instruction, not a single rounding.
//!
//! Everything here is total: no operation panics, and no operation can produce a NaN or an infinity, just
//! as the hardware cannot.
//!
//! The model is written in `f64` host arithmetic on purpose. Both operands of an aligned add are exact in
//! `f64`, and a 24×24-bit product is exact in `f64`, so the `f64` intermediate is the *exact* real result
//! and the only rounding that happens is the one the EE performs. That makes the model host-independent:
//! `f64` `+`, `-`, `*`, `/` and `sqrt` are specified exactly by IEEE 754 and give the same bits on x86_64,
//! aarch64 and wasm32.
#![deny(clippy::float_arithmetic)]

use crate::libm::sqrtf;

/// The largest magnitude the EE FPU can represent; it saturates here instead of producing an infinity.
pub const MAX: f32 = f32::MAX;

/// The negative of [`MAX`]. Spelled as a constant rather than `-MAX` so that the modules in this crate
/// can keep `float_arithmetic` denied: even a unary minus is arithmetic to the lint, and the lint is
/// worth more than the two characters.
pub const NEG_MAX: f32 = f32::from_bits(0xff7f_ffff);

/// Flush a denormal to zero, keeping its sign (what the EE does on both read and write).
#[inline]
fn daz(x: f32) -> f32 {
    if x.is_subnormal() { if x.is_sign_negative() { -0.0 } else { 0.0 } } else { x }
}

/// How the FPU reads a 32-bit pattern as an operand: exponent 255 (what other machines call Inf or NaN)
/// is the saturated maximum, a denormal is zero, everything else is itself.
///
/// An interpreter or a RAM reader should push every value through this before using it; the arithmetic
/// below applies it anyway, so a stray `f32::INFINITY` from host code cannot leak into a result.
#[inline]
pub fn operand(bits: u32) -> f32 {
    match bits & 0x7f80_0000 {
        0x7f80_0000 => f32::from_bits((bits & 0x8000_0000) | 0x7f7f_ffff),
        0 => f32::from_bits(bits & 0x8000_0000),
        _ => f32::from_bits(bits),
    }
}

/// An operand as the FPU sees it: denormals zeroed, Inf/NaN saturated to `±max`.
#[inline]
fn ee(x: f32) -> f32 {
    if x.is_finite() { daz(x) } else { operand(x.to_bits()) }
}

/// Round a real value (held exactly in an `f64`) to `f32` the way every EE FPU result is rounded: toward
/// zero, denormals flushed, overflow saturated to `±max`.
///
/// This is also how the EE's `cvt.s.d`-style double-to-float conversions round, so a game's own soft-float
/// double constants come out of it unchanged.
pub fn chop(x: f64) -> f32 {
    if !x.is_finite() {
        return if x.is_sign_negative() { NEG_MAX } else { MAX };
    }
    // The host rounds to nearest; if that rounded *away* from zero, step back one representable value.
    #[allow(clippy::float_arithmetic)]
    let r = {
        let r = x as f32;
        if (r as f64).abs() > x.abs() { f32::from_bits(r.to_bits().wrapping_sub(1)) } else { r }
    };
    daz(r)
}

/// The EE adder's alignment step: mask the mantissa bits the smaller operand loses, keeping one guard bit.
///
/// With an exponent difference of `d`, the smaller operand keeps all but its low `d - 1` mantissa bits,
/// and at `d >= 25` nothing but its sign is left. The two results add exactly in `f64`.
fn align(a: f32, b: f32) -> (f32, f32) {
    let (ba, bb) = (a.to_bits(), b.to_bits());
    let (ea, eb) = (((ba >> 23) & 0xff) as i32, ((bb >> 23) & 0xff) as i32);
    let mask = |bits: u32, d: i32| if d >= 25 { bits & 0x8000_0000 } else { bits & !((1u32 << (d - 1)) - 1) };
    match ea - eb {
        d if d > 0 => (a, f32::from_bits(mask(bb, d))),
        d if d < 0 => (f32::from_bits(mask(ba, -d)), b),
        _ => (a, b),
    }
}

/// `add.s`: align, add exactly, chop.
#[allow(clippy::float_arithmetic)] // the model itself, in exact host arithmetic
pub fn add(a: f32, b: f32) -> f32 {
    let (a, b) = align(ee(a), ee(b));
    chop(a as f64 + b as f64)
}

/// `sub.s`: the adder with the second operand's sign flipped (a sign flip is exact, so this is the
/// instruction, not an approximation of it).
pub fn sub(a: f32, b: f32) -> f32 {
    add(a, neg(b))
}

/// `mul.s`: the 24×24-bit product is exact in `f64`, so the only rounding is the chop.
#[allow(clippy::float_arithmetic)] // the model itself, in exact host arithmetic
pub fn mul(a: f32, b: f32) -> f32 {
    chop(ee(a) as f64 * ee(b) as f64)
}

/// `div.s`: round to nearest (the divider's own mode), `x / 0` gives `±max`.
#[allow(clippy::float_arithmetic)] // the model itself; f32 division is exact per IEEE
pub fn div(a: f32, b: f32) -> f32 {
    let (a, b) = (ee(a), ee(b));
    if b == 0.0 {
        return if a.is_sign_negative() != b.is_sign_negative() { NEG_MAX } else { MAX };
    }
    let r = a / b;
    if r.is_finite() { daz(r) } else if r.is_sign_negative() { NEG_MAX } else { MAX }
}

/// `sqrt.s` of `|x|` (the instruction ignores the sign), round to nearest.
pub fn sqrt(x: f32) -> f32 {
    daz(sqrtf(abs(ee(x))))
}

/// `rsqrt.s`: `a / sqrt(b)`, a divide and a square root, both round-to-nearest. `b == 0` gives `±max`
/// with the sign of `a / b`.
pub fn rsqrt(a: f32, b: f32) -> f32 {
    let (a, b) = (ee(a), ee(b));
    if b == 0.0 {
        return if a.is_sign_negative() != b.is_sign_negative() { NEG_MAX } else { MAX };
    }
    div(a, sqrt(b))
}

/// `madd.s`: `acc + a·b`, the product chopped to `f32` first and then put through the aligning adder.
/// Not a fused multiply-add: two roundings, in this order.
pub fn madd(acc: f32, a: f32, b: f32) -> f32 {
    add(acc, mul(a, b))
}

/// `msub.s`: `acc − a·b`, same two roundings as [`madd`].
pub fn msub(acc: f32, a: f32, b: f32) -> f32 {
    sub(acc, mul(a, b))
}

/// `madda.s`: the value `madd.s` would produce, destined for the accumulator register rather than an
/// `f`-register. The arithmetic is identical; the instructions differ only in where they write.
pub fn madda(acc: f32, a: f32, b: f32) -> f32 {
    madd(acc, a, b)
}

/// `msuba.s`: [`msub`] into the accumulator.
pub fn msuba(acc: f32, a: f32, b: f32) -> f32 {
    msub(acc, a, b)
}

/// `adda.s` / `suba.s` / `mula.s`: the same arithmetic as [`add`] / [`sub`] / [`mul`], into the
/// accumulator. A very common idiom is `adda.s $f0, x` (accumulator = 0 + x) followed by `madd.s`, which
/// is why [`lerp`] below reads the way it does.
pub fn adda(a: f32, b: f32) -> f32 {
    add(a, b)
}

/// `abs.s`: clears the sign bit, nothing rounds.
pub fn abs(x: f32) -> f32 {
    f32::from_bits(x.to_bits() & 0x7fff_ffff)
}

/// `neg.s`: flips the sign bit, nothing rounds.
pub fn neg(x: f32) -> f32 {
    f32::from_bits(x.to_bits() ^ 0x8000_0000)
}

/// `max.s`: a compare and a select, no rounding.
pub fn max(a: f32, b: f32) -> f32 {
    let (a, b) = (ee(a), ee(b));
    if a >= b { a } else { b }
}

/// `min.s`: a compare and a select, no rounding.
pub fn min(a: f32, b: f32) -> f32 {
    let (a, b) = (ee(a), ee(b));
    if a <= b { a } else { b }
}

/// `cvt.w.s`: float to 32-bit int, truncating, saturating at `i32::MIN` / `i32::MAX` the way the EE does
/// (anything whose exponent is past `2^31` saturates by sign).
pub fn to_i32(x: f32) -> i32 {
    let bits = x.to_bits();
    if bits & 0x7f80_0000 <= 0x4e80_0000 {
        ee(x) as i32
    } else if bits & 0x8000_0000 != 0 {
        i32::MIN
    } else {
        i32::MAX
    }
}

/// `cvt.s.w`: 32-bit int to float, chopped like every other FPU result (values above 2^24 lose their low
/// bits *toward zero*, not to nearest).
pub fn from_i32(v: i32) -> f32 {
    chop(v as f64)
}

/// An unsigned int to float, as a compiler emits it on the EE (convert the halved value, then double it):
/// chopped, like every other FPU result.
pub fn from_u32(v: u32) -> f32 {
    chop(v as f64)
}

/// The `adda.s` / `sub.s` / `madd.s` lerp idiom a PS2 compiler emits for `from + w * (to - from)`.
/// Spelled out so a port can call it where the original has those three instructions.
pub fn lerp(from: f32, to: f32, w: f32) -> f32 {
    madd(adda(0.0, from), w, sub(to, from))
}

/// An EE FPU single.
///
/// `+ - * /` are the FPU's [`add`], [`sub`], [`mul`], [`div`]; unary `-` is `neg.s`; comparisons are the
/// FPU's (plain IEEE order, denormals read as zero). The point of the wrapper is reviewability: a ported
/// expression can be written the way the original's decompilation writes it —
///
/// ```
/// # use ps2_float::F;
/// let (dt, g) = (F(1.0 / 60.0), F(-9.8));
/// let (mut y, mut vy) = (F(1.2), F(4.0));
/// vy = vy + g * dt;          // one mul.s, one add.s, in that order
/// y = y.madd(vy, dt);        // one madd.s
/// # let _ = (y, vy);
/// ```
///
/// — instead of `fpu::add(y, fpu::mul(vy, dt))`, which nobody can check against a decompilation line by
/// line. Rust never reassociates or fuses floating point, and `a + b + c` parses as `(a + b) + c`, so
/// writing the original's operations in the original's order keeps every intermediate rounding.
///
/// `madd` / `msub` name the instruction where the original has one; they are bit for bit `self + a * b`.
/// An `f32` on either side is taken as an FPU operand, so constants and struct fields need no wrapping.
#[derive(Clone, Copy, Debug, Default, PartialEq, PartialOrd)]
#[repr(transparent)]
pub struct F(pub f32);

/// `F(x)`, shorter inside a formula.
pub const fn f(x: f32) -> F {
    F(x)
}

/// Every element of an `f32` array as an [`F`].
pub fn fs<const N: usize>(a: [f32; N]) -> [F; N] {
    a.map(F)
}

/// Every element of an [`F`] array back as `f32`.
pub fn f32s<const N: usize>(a: [F; N]) -> [f32; N] {
    a.map(|x| x.0)
}

impl F {
    pub const ZERO: F = F(0.0);
    pub const ONE: F = F(1.0);
    /// The largest magnitude the FPU can hold (its "infinity").
    pub const MAX: F = F(MAX);

    /// The plain `f32` inside.
    pub fn to_f32(self) -> f32 {
        self.0
    }
    /// The raw 32-bit pattern — what a test compares and a mismatch report prints.
    pub fn to_bits(self) -> u32 {
        self.0.to_bits()
    }
    /// A raw pattern as an operand (Inf/NaN saturated, denormals zeroed, as the FPU reads one).
    pub fn from_bits(bits: u32) -> F {
        F(operand(bits))
    }
    /// `madd.s`: `self + a·b`.
    pub fn madd(self, a: impl Into<F>, b: impl Into<F>) -> F {
        F(madd(self.0, a.into().0, b.into().0))
    }
    /// `msub.s`: `self − a·b`.
    pub fn msub(self, a: impl Into<F>, b: impl Into<F>) -> F {
        F(msub(self.0, a.into().0, b.into().0))
    }
    /// `sqrt.s` of `|self|`.
    pub fn sqrt(self) -> F {
        F(sqrt(self.0))
    }
    /// `rsqrt.s`: `self / sqrt(b)`.
    pub fn rsqrt(self, b: impl Into<F>) -> F {
        F(rsqrt(self.0, b.into().0))
    }
    /// `abs.s`.
    pub fn abs(self) -> F {
        F(abs(self.0))
    }
    /// `max.s` / `min.s`: compares only, nothing rounds.
    pub fn max(self, o: impl Into<F>) -> F {
        F(max(self.0, o.into().0))
    }
    pub fn min(self, o: impl Into<F>) -> F {
        F(min(self.0, o.into().0))
    }
    pub fn clamp(self, lo: impl Into<F>, hi: impl Into<F>) -> F {
        self.max(lo).min(hi)
    }
    /// `cvt.w.s`: truncate to `i32`.
    pub fn to_i32(self) -> i32 {
        to_i32(self.0)
    }
    /// The three-instruction lerp idiom: `self + w·(to − self)` (see [`lerp`]).
    pub fn lerp(self, to: impl Into<F>, w: impl Into<F>) -> F {
        F(lerp(self.0, to.into().0, w.into().0))
    }
}

impl From<f32> for F {
    fn from(x: f32) -> F {
        F(x)
    }
}
impl From<i32> for F {
    fn from(x: i32) -> F {
        F(from_i32(x))
    }
}
impl From<F> for f32 {
    fn from(x: F) -> f32 {
        x.0
    }
}
impl PartialEq<f32> for F {
    fn eq(&self, o: &f32) -> bool {
        self.0 == *o
    }
}
impl PartialOrd<f32> for F {
    fn partial_cmp(&self, o: &f32) -> Option<core::cmp::Ordering> {
        self.0.partial_cmp(o)
    }
}
impl PartialEq<F> for f32 {
    fn eq(&self, o: &F) -> bool {
        *self == o.0
    }
}
impl PartialOrd<F> for f32 {
    fn partial_cmp(&self, o: &F) -> Option<core::cmp::Ordering> {
        self.partial_cmp(&o.0)
    }
}

impl core::ops::Neg for F {
    type Output = F;
    fn neg(self) -> F {
        F(neg(self.0))
    }
}

impl core::fmt::Display for F {
    fn fmt(&self, w: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        write!(w, "{}", self.0)
    }
}

macro_rules! fpu_op {
    ($tr:ident $m:ident $as_tr:ident $as_m:ident $op:ident) => {
        impl core::ops::$tr<F> for F {
            type Output = F;
            fn $m(self, o: F) -> F {
                F($op(self.0, o.0))
            }
        }
        impl core::ops::$tr<f32> for F {
            type Output = F;
            fn $m(self, o: f32) -> F {
                F($op(self.0, o))
            }
        }
        impl core::ops::$tr<F> for f32 {
            type Output = F;
            fn $m(self, o: F) -> F {
                F($op(self, o.0))
            }
        }
        impl<T: Into<F>> core::ops::$as_tr<T> for F {
            fn $as_m(&mut self, o: T) {
                *self = F($op(self.0, o.into().0));
            }
        }
    };
}
fpu_op!(Add add AddAssign add_assign add);
fpu_op!(Sub sub SubAssign sub_assign sub);
fpu_op!(Mul mul MulAssign mul_assign mul);
fpu_op!(Div div DivAssign div_assign div);
