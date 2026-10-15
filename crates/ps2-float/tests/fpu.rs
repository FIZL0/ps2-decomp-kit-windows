//! The FPU model's own properties: each test is one deviation from IEEE 754, written so that it fails
//! loudly if someone "fixes" the model into ordinary floating point.
//!
//! These are *properties*, not captures. Proving the model against a particular console means running
//! the game's own code: `cargo test -p ps2-oracle`.

use ps2_float::fpu::{self, F, f};

#[test]
fn every_rounding_is_toward_zero() {
    // 1/3 rounds down in binary; the IEEE nearest result is one ulp above the chopped one.
    assert_eq!(fpu::div(1.0, 3.0), 1.0f32 / 3.0, "divide is the exception: it rounds to nearest");
    // a product whose exact value sits between two floats loses the remainder entirely
    let (a, b) = (1.0000001f32, 1.0000002f32);
    assert!(fpu::mul(a, b) <= (a as f64 * b as f64) as f32);
    let mut worst = 0;
    for i in 0..100_000u32 {
        let x = f32::from_bits(0x3f80_0000 + i * 7);
        let y = f32::from_bits(0x3fc0_0000 + i * 13);
        let exact = x as f64 * y as f64;
        let got = fpu::mul(x, y) as f64;
        assert!(got.abs() <= exact.abs(), "mul({x}, {y}) rounded away from zero");
        worst = worst.max((exact - got).abs().to_bits());
    }
    assert!(worst > 0, "no input was inexact — the test is not exercising the chop");

    // negatives chop toward zero too, i.e. upward
    let n = fpu::mul(-1.0000001, 1.0000002);
    assert!(n >= (-1.0000001f64 * 1.0000002f64) as f32);
}

#[test]
fn chop_only_moves_one_bit_and_only_inward() {
    for i in 0..200_000u64 {
        let x = f64::from_bits(0x3ff0_0000_0000_0000 + i * 0x4_0000_0003);
        let got = fpu::chop(x);
        let near = x as f32;
        assert!(got == near || got.to_bits() == near.to_bits() - 1, "chop({x}) vs nearest {near}");
        assert!((got as f64).abs() <= x.abs(), "chop({x}) grew");
    }
}

#[test]
fn the_adder_keeps_one_guard_bit_and_gives_up_at_25() {
    // The headline symptom: a small addend simply disappears.
    assert_eq!(fpu::add(1.0, 1.0e-8), 1.0);
    assert_eq!(f(1.0) + 1.0e-8, 1.0);

    // Where the host and the EE part company. 1.5·2^-25 is 25 exponents below 1.0, so the adder keeps
    // only its sign: the subtraction is the identity, where the host moves one ulp.
    let d25 = f32::from_bits(0x3340_0000);
    assert_eq!(fpu::sub(1.0, d25), 1.0);
    assert_eq!(1.0f32 - d25, 0.99999994, "the host keeps it");

    // One exponent higher the operand survives, but only its top mantissa bit (the guard bit) does —
    // so the EE's answer is one ulp *above* the host's correctly-rounded one.
    let d24 = f32::from_bits(0x33c0_0000);
    assert_eq!(fpu::sub(1.0, d24), 0.99999994);
    assert_eq!(1.0f32 - d24, 0.9999999, "the host keeps the bits below the guard bit");

    // the mantissa below the guard bit is gone before the add, not rounded into it
    let all_ones = f32::from_bits(0x3380_0000 | 0x007f_ffff); // just under 2·2^-24
    assert_eq!(fpu::sub(1.0, all_ones), fpu::sub(1.0, f32::from_bits(0x3380_0000)));

    // a worked case from a real port: lerp(0.95, 0.9, 0.25) is exactly 0.9375 on the PS2, where plain
    // chopped arithmetic without the alignment step gives 0.93749994.
    assert_eq!(fpu::lerp(0.95, 0.9, 0.25), 0.9375);
}

#[test]
fn madd_is_a_chopped_product_then_an_aligned_add() {
    let (acc, a, b) = (1.0f32, 1.0 / 3.0, 3.000_001);
    assert_eq!(fpu::madd(acc, a, b), fpu::add(acc, fpu::mul(a, b)), "madd must not fuse");
    assert_ne!(fpu::madd(acc, a, b).to_bits(), acc.mul_add(a, b).to_bits(),
               "and it is not the host's FMA either");
    // the accumulate form keeps the same two steps
    assert_eq!(fpu::msub(acc, a, b), fpu::sub(acc, fpu::mul(a, b)));
    assert_eq!(f(acc).madd(a, b), fpu::madd(acc, a, b));
    // a product too small to reach the accumulator vanishes, exactly as a plain add would
    assert_eq!(fpu::madd(1.0, 1.0e-4, 1.0e-5), 1.0);
}

#[test]
fn denormals_are_zero_coming_in_and_going_out() {
    let d = f32::from_bits(0x0040_0000); // 5.9e-39, a denormal
    assert!(d != 0.0, "the host can hold it");
    assert_eq!(fpu::operand(d.to_bits()), 0.0, "the FPU reads it as zero");
    assert_eq!(fpu::add(d, d), 0.0);
    assert_eq!(fpu::mul(1.0, d), 0.0);
    assert_eq!(fpu::add(1.0, d), 1.0);
    // a product that would underflow into the denormal range comes out as exactly zero
    assert_eq!(fpu::mul(1.0e-30, 1.0e-20), 0.0);
    assert_eq!(fpu::mul(-1.0e-30, 1.0e-20).to_bits(), (-0.0f32).to_bits(), "and keeps its sign");
    // ±0 is preserved
    assert_eq!(fpu::operand(0x8000_0000).to_bits(), (-0.0f32).to_bits());
}

#[test]
fn there_are_no_infinities_and_no_nans() {
    assert_eq!(fpu::mul(f32::MAX, 2.0), fpu::MAX, "overflow saturates");
    assert_eq!(fpu::mul(-f32::MAX, 2.0), -fpu::MAX);
    assert_eq!(fpu::add(f32::MAX, f32::MAX), fpu::MAX);
    assert_eq!(fpu::div(1.0, 0.0), fpu::MAX, "the divider only looks at the signs");
    assert_eq!(fpu::div(-1.0, 0.0), -fpu::MAX);
    assert_eq!(fpu::div(0.0, 0.0), fpu::MAX, "0/0 is +max, not a NaN");
    assert_eq!(fpu::div(0.0, -0.0), -fpu::MAX);
    // an Inf or NaN bit pattern (from a save state, a file, a host computation) reads as ±max
    assert_eq!(fpu::operand(0x7f80_0000), fpu::MAX);
    assert_eq!(fpu::operand(0xff80_0000), -fpu::MAX);
    assert_eq!(fpu::operand(0x7fc0_0000), fpu::MAX);
    assert!(fpu::MAX.is_finite() && !fpu::MAX.is_nan(), "nothing this model returns is non-finite");
}

#[test]
fn divide_and_root_round_to_nearest() {
    // a different unit, a different rounding mode: these agree with the host bit for bit
    for i in 1..50_000u32 {
        let a = f32::from_bits(0x3f80_0000 + i * 11);
        let b = f32::from_bits(0x4000_0000 + i * 7);
        assert_eq!(fpu::div(a, b).to_bits(), (a / b).to_bits(), "div({a}, {b})");
        assert_eq!(fpu::sqrt(b).to_bits(), b.sqrt().to_bits());
    }
    assert_eq!(fpu::sqrt(-4.0), 2.0, "sqrt.s ignores the sign");
    assert_eq!(fpu::rsqrt(1.0, 4.0), 0.5);
}

#[test]
fn the_wrapper_reads_like_the_decompilation() {
    // one mul.s, one add.s, in that order — and nothing Rust does may change that
    let (y, vy, dt) = (f(10.0), f(-1.0 / 3.0), f(1.0 / 60.0));
    assert_eq!((y + vy * dt).to_bits(), fpu::add(10.0, fpu::mul(-1.0 / 3.0, 1.0 / 60.0)).to_bits());
    // f32 on either side, in any position
    assert_eq!((1.0 + f(2.0) * 3.0).to_f32(), 7.0);
    assert_eq!((f(1.0) - 0.5) / 2.0, F(0.25));
    let mut a = f(1.0);
    a += 1.0e-8;
    assert_eq!(a, 1.0, "even through +=");
    assert_eq!(-f(2.0), F(-2.0));
    assert!(f(1.0) < 2.0 && 0.5 < f(1.0) && f(1.0) == 1.0);
    assert_eq!(f(-3.0).abs().max(2.0).min(f(10.0)), F(3.0));
    assert_eq!(f(2.5).to_i32(), 2, "cvt.w.s truncates");
    assert_eq!(f(-2.5).to_i32(), -2);
    assert_eq!(F::from(3), F(3.0));
    assert_eq!(f32::from(f(3.0)), 3.0);
}

#[test]
fn conversions_match_the_instructions() {
    assert_eq!(fpu::to_i32(2.9), 2);
    assert_eq!(fpu::to_i32(-2.9), -2);
    assert_eq!(fpu::from_i32(1 << 24), 16777216.0);
    assert_eq!(fpu::from_i32(-1), -1.0);
    // (float)(unsigned) is a compiler sequence, not an instruction; it chops like everything else
    assert_eq!(fpu::from_u32(0xffff_ffff), 4294967040.0);
    assert_eq!(fpu::from_u32(1 << 31), 2147483648.0);
    // cvt.w.s saturates rather than wrapping, like the hardware
    assert_eq!(fpu::to_i32(1.0e30), i32::MAX);
    assert_eq!(fpu::to_i32(-1.0e30), i32::MIN);
}
