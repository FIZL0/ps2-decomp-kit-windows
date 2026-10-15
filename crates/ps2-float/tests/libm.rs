//! `libm` is checked two ways: against a correctly-rounded reference (it must be *accurate*, within a
//! few ulps — a transcription slip in a polynomial shows up immediately), and against itself (fixed
//! digests, so the same bits come out on every host and after every refactor).
//!
//! What it is *not* checked against here is a particular game, because that needs the game: see
//! `ps2-oracle`'s `tests/game.rs`, which runs these against the original's own code over 10k inputs.
// π and π/2 appear here as literals on purpose: these tests check the *game's* constants, and
// `std::f32::consts` is not what the PS2's libm was built with.
#![allow(clippy::approx_constant)]


use ps2_float::libm::*;

/// A deterministic 32-bit xorshift: the same inputs on every platform, no `rand` dependency.
struct Rng(u32);
impl Rng {
    fn next(&mut self) -> u32 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 17;
        self.0 ^= self.0 << 5;
        self.0
    }
    /// A float in `[lo, hi)`.
    fn range(&mut self, lo: f64, hi: f64) -> f32 {
        (lo + (hi - lo) * (self.next() as f64 / 4294967296.0)) as f32
    }
}

/// Worst relative error of `got` against `want` over the sample, in ulps of the result.
fn ulps(got: f32, want: f64) -> f64 {
    if want == 0.0 {
        return (got as f64).abs() / f64::from(f32::MIN_POSITIVE);
    }
    let ulp = f64::from(f32::EPSILON) * want.abs().max(f64::from(f32::MIN_POSITIVE));
    ((got as f64) - want).abs() / ulp
}

fn sweep(name: &str, n: u32, lo: f64, hi: f64, ours: impl Fn(f32) -> f32, exact: impl Fn(f64) -> f64,
         limit: f64) {
    let mut rng = Rng(0x1234_5678);
    let mut worst = (0.0f64, 0.0f32);
    for _ in 0..n {
        let x = rng.range(lo, hi);
        let e = ulps(ours(x), exact(x as f64));
        if e > worst.0 {
            worst = (e, x);
        }
    }
    assert!(worst.0 <= limit, "{name}: {:.1} ulp at x = {:?} ({:#010x}): got {:?}, want {:?}",
            worst.0, worst.1, worst.1.to_bits(), ours(worst.1), exact(worst.1 as f64));
    println!("{name:8} {:>5.2} ulp over {n} inputs in [{lo}, {hi})", worst.0);
}

#[test]
fn sqrt_is_correctly_rounded() {
    // sqrt is specified exactly by IEEE, so our integer algorithm must agree with the host's bit for bit.
    let mut rng = Rng(1);
    for _ in 0..200_000 {
        let x = f32::from_bits(rng.next() & 0x7fff_ffff);
        if !x.is_finite() || x < f32::MIN_POSITIVE {
            continue;
        }
        assert_eq!(sqrtf(x).to_bits(), x.sqrt().to_bits(), "sqrtf({x:?})");
    }
    assert_eq!(sqrtf(0.0), 0.0);
    assert_eq!(sqrtf(-0.0).to_bits(), (-0.0f32).to_bits());
    assert_eq!(sqrtf(-4.0), 2.0, "sqrt.s ignores the sign, and so does a PS2 sqrtf()");
}

#[test]
fn trig_is_accurate() {
    sweep("sinf", 20_000, -3.141_592_6, 3.141_592_6, sinf, f64::sin, 4.0);
    sweep("cosf", 20_000, -3.141_592_6, 3.141_592_6, cosf, f64::cos, 4.0);
    sweep("sinf", 20_000, -100.0, 100.0, sinf, f64::sin, 4.0);
    sweep("cosf", 20_000, -100.0, 100.0, cosf, f64::cos, 4.0);
    sweep("tanf", 20_000, -0.7, 0.7, tanf, f64::tan, 4.0);
    sweep("tanf", 20_000, -100.0, 100.0, tanf, f64::tan, 8.0);
    // the fdlibm reduction tops out at 2^7·π/2; above that we take our own exact path
    sweep("sinf+", 20_000, 200.0, 1.0e7, sinf, f64::sin, 4.0);
    sweep("cosf+", 20_000, 200.0, 1.0e7, cosf, f64::cos, 4.0);
    sweep("sinf+", 5_000, 1.0e20, 1.0e30, sinf, f64::sin, 4.0);
    assert_eq!(sinf(0.0), 0.0);
    assert_eq!(cosf(0.0), 1.0);
    assert_eq!(sinf(-0.0).to_bits(), (-0.0f32).to_bits());
}

#[test]
fn inverse_trig_is_accurate() {
    sweep("atanf", 20_000, -1.0e6, 1.0e6, atanf, f64::atan, 4.0);
    sweep("atanf", 20_000, -2.0, 2.0, atanf, f64::atan, 4.0);
    sweep("asinf", 20_000, -1.0, 1.0, asinf, f64::asin, 4.0);
    sweep("acosf", 20_000, -1.0, 1.0, acosf, f64::acos, 6.0);
    let mut rng = Rng(7);
    let mut worst = 0.0f64;
    for _ in 0..20_000 {
        let (y, x) = (rng.range(-10.0, 10.0), rng.range(-10.0, 10.0));
        let e = ulps(atan2f(y, x), (y as f64).atan2(x as f64));
        worst = worst.max(e);
        assert!(e <= 4.0, "atan2f({y:?}, {x:?}): {e:.1} ulp");
    }
    println!("atan2f   {worst:>5.2} ulp over 20000 inputs");
    // the quadrant rules, which are all special cases in fdlibm
    assert_eq!(atan2f(0.0, 1.0), 0.0);
    assert_eq!(atan2f(0.0, -1.0), 3.1415927);
    assert_eq!(atan2f(-0.0, -1.0), -3.1415927);
    assert_eq!(atan2f(1.0, 0.0), 1.5707964);
    assert_eq!(atan2f(-1.0, 0.0), -1.5707964);
    assert_eq!(atan2f(-1.0, -0.0), -1.5707964);
    assert_eq!(atan2f(1.0e-30, -1.0), 3.1415927, "a tiny y with x < 0 still gives π");
    assert_eq!(acosf(1.0), 0.0);
    assert_eq!(asinf(0.0), 0.0);
}

#[test]
fn exp_log_pow_are_accurate() {
    sweep("expf", 20_000, -80.0, 80.0, expf, f64::exp, 4.0);
    sweep("expf", 20_000, -0.3, 0.3, expf, f64::exp, 4.0);
    sweep("logf", 20_000, 1.0e-20, 1.0e20, logf, f64::ln, 4.0);
    sweep("logf", 20_000, 0.9, 1.1, logf, f64::ln, 4.0);
    assert_eq!(logf(1.0), 0.0);
    assert_eq!(expf(0.0), 1.0);

    let mut rng = Rng(99);
    let mut worst = (0.0f64, 0.0f32, 0.0f32);
    for _ in 0..40_000 {
        let x = rng.range(0.01, 100.0);
        let y = rng.range(-8.0, 8.0);
        let want = (x as f64).powf(y as f64);
        if !(f64::from(f32::MIN_POSITIVE)..f64::from(f32::MAX)).contains(&want.abs()) {
            continue;
        }
        let e = ulps(powf(x, y), want);
        if e > worst.0 {
            worst = (e, x, y);
        }
    }
    assert!(worst.0 <= 6.0, "powf({:?}, {:?}): {:.1} ulp — got {:?}, want {:?}", worst.1, worst.2, worst.0,
            powf(worst.1, worst.2), (worst.1 as f64).powf(worst.2 as f64));
    println!("powf     {:>5.2} ulp over 40000 inputs", worst.0);
    // the exact cases fdlibm shortcuts, which a port leans on
    assert_eq!(powf(2.0, 0.0), 1.0);
    assert_eq!(powf(0.0, 0.0), 1.0);
    assert_eq!(powf(3.0, 1.0), 3.0);
    assert_eq!(powf(3.0, 2.0), 9.0);
    assert_eq!(powf(4.0, 0.5), 2.0);
    assert_eq!(powf(2.0, 10.0), 1024.0);
    assert_eq!(powf(-2.0, 3.0), -8.0);
    assert_eq!(powf(-2.0, 2.0), 4.0);
    assert_eq!(powf(0.0, 3.0), 0.0);
}

#[test]
fn saturates_where_the_ee_cannot_hold_the_answer() {
    // The EE has no infinity: the biggest float is just a number, and over/underflow stop there.
    assert_eq!(expf(200.0), f32::MAX);
    assert_eq!(expf(-200.0), 0.0);
    assert_eq!(powf(10.0, 60.0), f32::MAX);
    assert_eq!(powf(10.0, -60.0), 0.0);
    assert_eq!(logf(0.0), -f32::MAX);
    assert_eq!(sinf(f32::MAX), sinf(f32::from_bits(0x7f80_0000)), "an Inf pattern reads as ±max");
}

/// A digest over a fixed input set. If this changes, *some* result changed — which is either a bug or a
/// deliberate match to a game's own libm, and either way the commit must say which.
#[test]
fn results_are_identical_on_every_platform() {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    let mut feed = |x: f32| {
        h ^= u64::from(x.to_bits());
        h = h.wrapping_mul(0x1000_0000_01b3);
    };
    let mut rng = Rng(0xdead_beef);
    for _ in 0..2000 {
        let x = rng.range(-20.0, 20.0);
        for v in [sinf(x), cosf(x), tanf(x), atanf(x), expf(x), logf(x.abs() + 1.0e-6), sqrtf(x.abs())] {
            feed(v);
        }
        let u = rng.range(-1.0, 1.0);
        for v in [asinf(u), acosf(u), atan2f(x, u), powf(x.abs() + 0.001, u * 6.0)] {
            feed(v);
        }
    }
    assert_eq!(h, DIGEST, "libm results changed (or differ on this platform): {h:#018x}");
}
const DIGEST: u64 = 0x9da9_a6e5_3818_e896;
