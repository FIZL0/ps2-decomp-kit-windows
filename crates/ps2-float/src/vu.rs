//! VU0 in **macro mode** — the vector unit the EE drives directly with `COP2` instructions — as the
//! reference emulator performs it.
//!
//! A PS2 game's vector math (transforms, cross products, normalises, collision inner loops) is usually
//! VU0 macro-mode code sitting in the middle of ordinary C, so a port has to model it as well as the FPU.
//! How it differs from the FPU next door:
//!
//! * **Round toward zero**, like the FPU.
//! * **No alignment quirk.** The VU adder is a plain adder: it lines the operands up with full precision
//!   and then truncates the result. So `vaddx` and `add.s` of the same two numbers can differ — this is
//!   the single most common reason a port that "uses the PS2 float model" still drifts. Use [`crate::fpu`]
//!   for code the compiler put on COP1 and this module for code it put on COP2.
//! * **Nothing but finite numbers.** VU floats have no Inf and no NaN: an overflow *clamps* to
//!   `±1.7014118e38` and a denormal is zero. A bit pattern with exponent 255 read out of memory behaves
//!   as that maximum.
//! * **`vmadd` is two roundings**, a chopped product then a chopped add, exactly like `madd.s`.
//! * **Divide is a separate unit.** `vdiv` / `vsqrt` / `vrsqrt` write the single `Q` register; the result
//!   is chopped and `x / 0` gives `±max`.
//!
//! ## What is modelled, and what is not
//!
//! Modelled: the arithmetic of the macro-mode ops below, the destination field mask (`.xyzw` subsets),
//! the broadcast forms (`vaddx`, `vmulw`, …), the accumulator, `Q`, and `vftoi`/`vitof` fixed-point
//! conversions.
//!
//! **Not** modelled: the status, MAC and clipping flags; the integer register file (`vi0`..`vi15`) and its
//! branches; VU1 and its EFU ops (`esum`, `erleng`, `esin`, …); micro-program control flow; and all
//! pipeline timing — in particular `Q` is available immediately here, whereas the hardware makes a
//! program wait seven cycles (`vwaitq`). A micro program whose results you need cannot be reproduced by
//! this module alone: either port it by hand on top of these ops, or hook it out (see `ps2-oracle`'s
//! `hooks`) and compare against a capture.
#![deny(clippy::float_arithmetic)]

/// The largest magnitude a VU float can hold; results clamp here rather than going infinite.
pub const MAX: f32 = f32::MAX;

/// Four lanes as plain `f32`s, in `x, y, z, w` order.
pub type V4 = [f32; 4];

#[inline]
fn daz(x: f32) -> f32 {
    if x.is_subnormal() { if x.is_sign_negative() { -0.0 } else { 0.0 } } else { x }
}

/// How a VU reads a 32-bit pattern: exponent 255 clamps to `±max`, denormals are zero.
#[inline]
pub fn operand(bits: u32) -> f32 {
    match bits & 0x7f80_0000 {
        0x7f80_0000 => f32::from_bits((bits & 0x8000_0000) | 0x7f7f_ffff),
        0 => f32::from_bits(bits & 0x8000_0000),
        _ => f32::from_bits(bits),
    }
}

#[inline]
fn vu(x: f32) -> f32 {
    if x.is_finite() { daz(x) } else { operand(x.to_bits()) }
}

/// Round toward zero to `f32`, clamping an overflow to `±max`. `x` must be the exact result (or within
/// the right `f32` neighbourhood of it).
fn chop(x: f64) -> f32 {
    if !x.is_finite() {
        return if x.is_sign_negative() { crate::fpu::NEG_MAX } else { MAX };
    }
    #[allow(clippy::float_arithmetic)]
    let r = {
        let r = x as f32;
        if (r as f64).abs() > x.abs() { f32::from_bits(r.to_bits().wrapping_sub(1)) } else { r }
    };
    daz(r)
}

/// `vadd` (one lane): a full-precision add, chopped.
///
/// The exact sum of two `f32`s does not always fit in an `f64` — the exponents can be 270 apart — so the
/// `f64` sum alone would round a far smaller addend away and lose the *direction* of the truncation.
/// The exact rounding error (a TwoSum) decides it: `x` plus a tiny value of the opposite sign truncates
/// to the next `f32` toward zero, not to `x`.
#[allow(clippy::float_arithmetic)] // the model itself, in host arithmetic
pub fn add(a: f32, b: f32) -> f32 {
    let (a, b) = (vu(a) as f64, vu(b) as f64);
    let s = a + b;
    if !s.is_finite() {
        return if s.is_sign_negative() { crate::fpu::NEG_MAX } else { MAX };
    }
    let bb = s - a;
    let e = (a - (s - bb)) + (b - bb); // exact: s + e is the true sum
    let r = s as f32;
    let over = (r as f64 - s) - e; // r minus the true sum
    if over != 0.0 && r != 0.0 && (over > 0.0) == (r > 0.0) {
        daz(f32::from_bits(r.to_bits().wrapping_sub(1)))
    } else {
        daz(r)
    }
}

/// `vsub` (one lane).
pub fn sub(a: f32, b: f32) -> f32 {
    add(a, f32::from_bits(b.to_bits() ^ 0x8000_0000))
}

/// `vmul` (one lane): the 24×24-bit product is exact in `f64`, so the only rounding is the chop.
#[allow(clippy::float_arithmetic)] // the model itself, in exact host arithmetic
pub fn mul(a: f32, b: f32) -> f32 {
    chop(vu(a) as f64 * vu(b) as f64)
}

/// `vmadd` (one lane): `acc + a·b`, product chopped first, then the add chopped.
pub fn madd(acc: f32, a: f32, b: f32) -> f32 {
    add(acc, mul(a, b))
}

/// `vmsub` (one lane): `acc − a·b`.
pub fn msub(acc: f32, a: f32, b: f32) -> f32 {
    sub(acc, mul(a, b))
}

/// `vdiv` into `Q`, chopped; `x / 0` gives `±max`.
#[allow(clippy::float_arithmetic)] // the model itself; f32 division is exact per IEEE
pub fn div(a: f32, b: f32) -> f32 {
    let (a, b) = (vu(a), vu(b));
    if b == 0.0 {
        return if a.is_sign_negative() != b.is_sign_negative() { crate::fpu::NEG_MAX } else { MAX };
    }
    let r = a / b; // nearest; step toward zero when it rounded away
    if !r.is_finite() {
        return if r.is_sign_negative() { crate::fpu::NEG_MAX } else { MAX };
    }
    let over = ((r as f64) * (b as f64)).abs() > (a as f64).abs();
    daz(if over && r != 0.0 { f32::from_bits(r.to_bits().wrapping_sub(1)) } else { r })
}

/// `vsqrt` into `Q`: the square root of `|x|`, chopped.
pub fn sqrt(x: f32) -> f32 {
    chop(crate::libm::sqrtf(f32::from_bits(vu(x).to_bits() & 0x7fff_ffff)) as f64)
}

/// `vrsqrt` into `Q`: `a / sqrt(|b|)`, chopped.
pub fn rsqrt(a: f32, b: f32) -> f32 {
    div(a, sqrt(b))
}

/// Which lane a broadcast op reads (`vaddx`, `vmuly`, `vmaddw`, …).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Field {
    X = 0,
    Y = 1,
    Z = 2,
    W = 3,
}

/// A destination field mask: which lanes an op writes (`vadd.xz`, `vmul.xyz`, …).
///
/// The bits are the instruction's own: `x` is the high bit, `w` the low one. Lanes outside the mask keep
/// their old contents, in the destination register *and* in the accumulator — a detail that matters when
/// a `.xyz` op is followed by something reading `w`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Dest(pub u8);

impl Dest {
    pub const X: Dest = Dest(0b1000);
    pub const Y: Dest = Dest(0b0100);
    pub const Z: Dest = Dest(0b0010);
    pub const W: Dest = Dest(0b0001);
    pub const XY: Dest = Dest(0b1100);
    pub const XYZ: Dest = Dest(0b1110);
    pub const XYZW: Dest = Dest(0b1111);
    pub const XZ: Dest = Dest(0b1010);
    pub const YZW: Dest = Dest(0b0111);

    /// Does this mask write lane `i` (0 = x … 3 = w)?
    pub fn has(self, i: usize) -> bool {
        self.0 >> (3 - i) & 1 == 1
    }
    /// The mask as the letters an assembly listing prints (`"xyz"`, `"w"`, …).
    pub fn name(self) -> &'static str {
        const NAMES: [&str; 16] = [
            "", "w", "z", "zw", "y", "yw", "yz", "yzw", "x", "xw", "xz", "xzw", "xy", "xyw", "xyz", "xyzw",
        ];
        NAMES[(self.0 & 0xf) as usize]
    }
}

/// A VU0 vector register: four lanes, `x y z w`.
///
/// Arithmetic is per lane with the functions above. The ops that take an accumulator take it as their
/// first argument, because that is the order the instruction reads: `vmadd fd, fs, ft` is
/// `Vf::madd(acc, fs, ft)`.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
#[repr(transparent)]
pub struct Vf(pub V4);

impl Vf {
    pub const ZERO: Vf = Vf([0.0; 4]);
    /// `vf0`, which is hard-wired to `(0, 0, 0, 1)`.
    pub const VF0: Vf = Vf([0.0, 0.0, 0.0, 1.0]);

    pub const fn new(x: f32, y: f32, z: f32, w: f32) -> Vf {
        Vf([x, y, z, w])
    }
    pub const fn splat(v: f32) -> Vf {
        Vf([v; 4])
    }
    pub fn x(self) -> f32 {
        self.0[0]
    }
    pub fn y(self) -> f32 {
        self.0[1]
    }
    pub fn z(self) -> f32 {
        self.0[2]
    }
    pub fn w(self) -> f32 {
        self.0[3]
    }
    pub fn lane(self, f: Field) -> f32 {
        self.0[f as usize]
    }
    /// One lane in all four: the operand a broadcast op (`vaddx`, `vmulw`, …) sees.
    pub fn bc(self, f: Field) -> Vf {
        Vf::splat(self.lane(f))
    }
    /// The four lanes as raw patterns (how they sit in the register file and in memory).
    pub fn to_bits(self) -> [u32; 4] {
        self.0.map(f32::to_bits)
    }
    /// Four raw patterns as a register, each read the way a VU reads one ([`operand`]).
    pub fn from_bits(b: [u32; 4]) -> Vf {
        Vf(b.map(operand))
    }

    /// Write `v`'s lanes into `self` where `dest` says to, leaving the rest alone.
    pub fn merge(self, dest: Dest, v: Vf) -> Vf {
        Vf(core::array::from_fn(|i| if dest.has(i) { v.0[i] } else { self.0[i] }))
    }

    /// `vadd` / `vsub` / `vmul`, lane by lane. Pair them with [`Vf::bc`] for the broadcast forms:
    /// `a.mul(b.bc(Field::X))` is `vmulx`.
    #[allow(clippy::should_implement_trait)] // named after the instruction; the operators live on `Lane`
    pub fn add(self, o: Vf) -> Vf {
        Vf(core::array::from_fn(|i| add(self.0[i], o.0[i])))
    }
    #[allow(clippy::should_implement_trait)] // named after the instruction, not after `std::ops`
    pub fn sub(self, o: Vf) -> Vf {
        Vf(core::array::from_fn(|i| sub(self.0[i], o.0[i])))
    }
    #[allow(clippy::should_implement_trait)] // named after the instruction, not after `std::ops`
    pub fn mul(self, o: Vf) -> Vf {
        Vf(core::array::from_fn(|i| mul(self.0[i], o.0[i])))
    }
    /// `vmadd` / `vmsub`: `acc ± a·b`, lane by lane.
    pub fn madd(acc: Vf, a: Vf, b: Vf) -> Vf {
        Vf(core::array::from_fn(|i| madd(acc.0[i], a.0[i], b.0[i])))
    }
    pub fn msub(acc: Vf, a: Vf, b: Vf) -> Vf {
        Vf(core::array::from_fn(|i| msub(acc.0[i], a.0[i], b.0[i])))
    }
    /// `vmulq` / `vaddq` / `vsubq`: every lane against the `Q` register.
    pub fn mul_q(self, q: f32) -> Vf {
        Vf(core::array::from_fn(|i| mul(self.0[i], q)))
    }
    pub fn add_q(self, q: f32) -> Vf {
        Vf(core::array::from_fn(|i| add(self.0[i], q)))
    }

    /// `vopmula`: the first half of a cross product, into the accumulator —
    /// `acc.x = a.y·b.z`, `acc.y = a.z·b.x`, `acc.z = a.x·b.y` (`w` untouched).
    pub fn opmula(a: Vf, b: Vf) -> Vf {
        Vf([mul(a.0[1], b.0[2]), mul(a.0[2], b.0[0]), mul(a.0[0], b.0[1]), 0.0])
    }
    /// `vopmsub`: the second half — the same lane pattern as [`Vf::opmula`], subtracted from the
    /// accumulator. The cross product comes out because the compiler *swaps the operands* between the
    /// two instructions:
    ///
    /// ```text
    ///     vopmula.xyz ACC, vf1, vf2      ; ACC.x = v1.y·v2.z …
    ///     vopmsub.xyz vf3, vf2, vf1      ; vf3.x = ACC.x − v2.y·v1.z …  = (v1 × v2).x
    /// ```
    ///
    /// so port the pair as the original writes it, operands and all, rather than reaching for [`cross`].
    pub fn opmsub(acc: Vf, a: Vf, b: Vf) -> Vf {
        Vf([msub(acc.0[0], a.0[1], b.0[2]), msub(acc.0[1], a.0[2], b.0[0]), msub(acc.0[2], a.0[0], b.0[1]), acc.0[3]])
    }

    /// `vftoi0/4/12/15`: multiply by `2^frac`, truncate toward zero, saturate at the `i32` ends. The
    /// result lives in the same register file, so hardware keeps the pattern in a float register —
    /// [`Vf::ftoi_bits`] models that, this returns the integers themselves.
    pub fn ftoi(self, frac: u32) -> [i32; 4] {
        #[allow(clippy::float_arithmetic)]
        let scale = (1u64 << frac) as f64;
        core::array::from_fn(|i| {
            #[allow(clippy::float_arithmetic)]
            let v = vu(self.0[i]) as f64 * scale;
            if v >= i32::MAX as f64 {
                i32::MAX
            } else if v <= i32::MIN as f64 {
                i32::MIN
            } else {
                v as i32
            }
        })
    }
    /// `vitof0/4/12/15`: each lane's *bit pattern* read as an `i32`, divided by `2^frac`, chopped.
    pub fn itof_bits(self, frac: u32) -> Vf {
        Vf::itof(self.to_bits().map(|b| b as i32), frac)
    }
    /// Integers to floats, divided by `2^frac`, chopped.
    pub fn itof(v: [i32; 4], frac: u32) -> Vf {
        #[allow(clippy::float_arithmetic)]
        let scale = (1u64 << frac) as f64;
        #[allow(clippy::float_arithmetic)]
        Vf(v.map(|i| chop(i as f64 / scale)))
    }
    /// [`Vf::ftoi`] left in the register file: the lanes hold the integers' bit patterns.
    pub fn ftoi_bits(self, frac: u32) -> Vf {
        Vf(self.ftoi(frac).map(|i| f32::from_bits(i as u32)))
    }
}

impl core::ops::Index<usize> for Vf {
    type Output = f32;
    fn index(&self, i: usize) -> &f32 {
        &self.0[i]
    }
}
impl core::ops::IndexMut<usize> for Vf {
    fn index_mut(&mut self, i: usize) -> &mut f32 {
        &mut self.0[i]
    }
}
impl From<V4> for Vf {
    fn from(v: V4) -> Vf {
        Vf(v)
    }
}
impl From<Vf> for V4 {
    fn from(v: Vf) -> V4 {
        v.0
    }
}
impl core::ops::Add for Vf {
    type Output = Vf;
    fn add(self, o: Vf) -> Vf {
        Vf::add(self, o)
    }
}
impl core::ops::Sub for Vf {
    type Output = Vf;
    fn sub(self, o: Vf) -> Vf {
        Vf::sub(self, o)
    }
}
impl core::ops::Mul for Vf {
    type Output = Vf;
    fn mul(self, o: Vf) -> Vf {
        Vf::mul(self, o)
    }
}

/// One VU0 lane, for porting a micro program or a macro-mode run by hand.
///
/// `+ - * /` are the VU's [`add`], [`sub`], [`mul`] and the `Q` [`div`], so the ported code keeps the
/// original's order (`a + b + c` is `(a + b) + c`) and reads like its listing. The same idea as
/// [`crate::F`], one unit over.
#[derive(Clone, Copy, Debug, Default, PartialEq, PartialOrd)]
#[repr(transparent)]
pub struct Lane(pub f32);

/// A register's four lanes as [`Lane`]s.
pub type Lanes = [Lane; 4];

/// Every lane of `v` as a [`Lane`].
pub fn lanes(v: V4) -> Lanes {
    v.map(Lane)
}

/// Every [`Lane`] back as an `f32`.
pub fn v4(v: Lanes) -> V4 {
    v.map(|x| x.0)
}

impl Lane {
    pub const ZERO: Lane = Lane(0.0);
    pub fn to_f32(self) -> f32 {
        self.0
    }
    /// `vmadd`: `self + a·b`.
    pub fn madd(self, a: impl Into<Lane>, b: impl Into<Lane>) -> Lane {
        Lane(madd(self.0, a.into().0, b.into().0))
    }
    /// `vmsub`: `self − a·b`.
    pub fn msub(self, a: impl Into<Lane>, b: impl Into<Lane>) -> Lane {
        Lane(msub(self.0, a.into().0, b.into().0))
    }
    /// `vsqrt` of `|self|`.
    pub fn sqrt(self) -> Lane {
        Lane(sqrt(self.0))
    }
}

impl From<f32> for Lane {
    fn from(x: f32) -> Lane {
        Lane(x)
    }
}
impl From<Lane> for f32 {
    fn from(x: Lane) -> f32 {
        x.0
    }
}
impl PartialEq<f32> for Lane {
    fn eq(&self, o: &f32) -> bool {
        self.0 == *o
    }
}
impl PartialOrd<f32> for Lane {
    fn partial_cmp(&self, o: &f32) -> Option<core::cmp::Ordering> {
        self.0.partial_cmp(o)
    }
}
impl core::ops::Neg for Lane {
    type Output = Lane;
    fn neg(self) -> Lane {
        Lane(f32::from_bits(self.0.to_bits() ^ 0x8000_0000))
    }
}

macro_rules! vu_op {
    ($tr:ident $m:ident $as_tr:ident $as_m:ident $op:ident) => {
        impl core::ops::$tr<Lane> for Lane {
            type Output = Lane;
            fn $m(self, o: Lane) -> Lane {
                Lane($op(self.0, o.0))
            }
        }
        impl core::ops::$tr<f32> for Lane {
            type Output = Lane;
            fn $m(self, o: f32) -> Lane {
                Lane($op(self.0, o))
            }
        }
        impl core::ops::$tr<Lane> for f32 {
            type Output = Lane;
            fn $m(self, o: Lane) -> Lane {
                Lane($op(self, o.0))
            }
        }
        impl<T: Into<Lane>> core::ops::$as_tr<T> for Lane {
            fn $as_m(&mut self, o: T) {
                *self = Lane($op(self.0, o.into().0));
            }
        }
    };
}
vu_op!(Add add AddAssign add_assign add);
vu_op!(Sub sub SubAssign sub_assign sub);
vu_op!(Mul mul MulAssign mul_assign mul);
vu_op!(Div div DivAssign div_assign div);

// --- idioms every PS2 game's VU0 code contains -----------------------------------------------------
// These are the shapes a compiler emits again and again. They are here as a starting point and as
// documentation of the instruction order; when a function's listing does it differently, port *that*.

/// `vmul.xyz` then `vaddy.x` then `vaddz.x`: `(a.x·b.x + a.y·b.y) + a.z·b.z`, in that order.
pub fn dot3(a: V4, b: V4) -> f32 {
    let (a, b) = (lanes(a), lanes(b));
    (a[0] * b[0] + a[1] * b[1] + a[2] * b[2]).0
}

/// Normalise `xyz` (`w` becomes 0) the way a VU0 routine does it: `Q = sqrt(len²)`, `Q = 1/Q`, `v·Q`.
pub fn normalize(v: V4) -> V4 {
    let l = Lane(sqrt(dot3(v, v)));
    let q = Lane(div(1.0, l.0));
    let v = lanes(v);
    v4([v[0] * q, v[1] * q, v[2] * q, Lane::ZERO])
}

/// `vopmula` + `vopmsub`: `a × b` with `w = 0`.
pub fn cross(a: V4, b: V4) -> V4 {
    let (va, vb) = (Vf(a), Vf(b));
    let mut r = Vf::opmsub(Vf::opmula(va, vb), vb, va);
    r.0[3] = 0.0;
    r.0
}

/// Row-vector transform, `vmulax` / `vmadday` / `vmaddaz` / `vmaddw`:
/// `((r0·v.x + r1·v.y) + r2·v.z) + r3·v.w` per component.
pub fn transform(m: &[V4; 4], v: V4) -> V4 {
    let (m, v) = (m.map(lanes), lanes(v));
    v4(core::array::from_fn(|k| (m[0][k] * v[0]).madd(m[1][k], v[1]).madd(m[2][k], v[2]).madd(m[3][k], v[3])))
}

/// `vmulax` then `vmaddx`: `a·t + b·(1 − t)` per component.
pub fn lerp(a: V4, b: V4, t: f32) -> V4 {
    let (a, b, t) = (lanes(a), lanes(b), Lane(t));
    let u = Lane::ZERO + 1.0 - t;
    v4(core::array::from_fn(|k| (a[k] * t).madd(b[k], u)))
}
