//! The interpreter, proved against hand-assembled machine code.
//!
//! These tests need no disc, no emulator and no config: the programs are half a dozen instructions each,
//! written out as encoded words, so they also serve as a worked example of what the oracle expects a
//! function to look like (arguments in `a0…`/`$f12…`, result in `v0`/`$f0`, return through `jr $ra`).
//!
//! When one of these fails, the interpreter is wrong — not your port.

use ps2_float::fpu;
use ps2_oracle::Ee;

/// Where the test programs are assembled. Any address inside RAM does; this one is clear of the stack.
const CODE: u32 = 0x0010_0000;

// --- the world's smallest MIPS assembler ------------------------------------------------------------

/// An I-type word: `op rt, rs, imm`.
const fn i(op: u32, rs: u32, rt: u32, imm: i16) -> u32 {
    op << 26 | rs << 21 | rt << 16 | imm as u16 as u32
}
/// An R-type word: `fun rd, rs, rt` (shift amount `sa`).
const fn r(rs: u32, rt: u32, rd: u32, sa: u32, fun: u32) -> u32 {
    rs << 21 | rt << 16 | rd << 11 | sa << 6 | fun
}
/// A COP1 single-precision word: `fun fd, fs, ft`.
const fn c1(ft: u32, fs: u32, fd: u32, fun: u32) -> u32 {
    0x11 << 26 | 0x10 << 21 | ft << 16 | fs << 11 | fd << 6 | fun
}
/// An MMI word (the EE's 128-bit integer group): `fun/sa rd, rs, rt`.
const fn mmi(rs: u32, rt: u32, rd: u32, sa: u32, fun: u32) -> u32 {
    0x1c << 26 | rs << 21 | rt << 16 | rd << 11 | sa << 6 | fun
}
const JR_RA: u32 = r(31, 0, 0, 0, 8);
const NOP: u32 = 0;

/// Assemble a program at [`CODE`] and return a machine ready to call it.
fn prog(words: &[u32]) -> Ee {
    let mut ee = Ee::bare(2 << 20);
    let bytes: Vec<u8> = words.iter().flat_map(|w| w.to_le_bytes()).collect();
    ee.load(CODE, &bytes);
    ee
}

// --- the tests --------------------------------------------------------------------------------------

#[test]
fn a_loop_runs_with_its_delay_slots() {
    // sum(n) = n + (n-1) + … + 1, which exercises both branch forms and the instruction after them.
    let mut ee = prog(&[
        r(0, 0, 2, 0, 0x21),  // addu  v0, zero, zero
        i(4, 4, 0, 5),        // beq   a0, zero, +5  (to the jr)
        NOP,                  //   delay slot
        r(2, 4, 2, 0, 0x21),  // addu  v0, v0, a0      <- body
        i(9, 4, 4, -1),       // addiu a0, a0, -1
        i(5, 4, 0, -3),       // bne   a0, zero, -3    (back to the body)
        NOP,                  //   delay slot
        JR_RA,
        NOP,
    ]);
    for n in [0u64, 1, 2, 10, 100] {
        ee.call(CODE, &[n], &[], 10_000);
        assert_eq!(ee.v0(), n * (n + 1) / 2, "sum({n})");
    }
    // The budget is what a test notices instead of hanging.
    assert!(ee.steps > 300, "100 iterations should take hundreds of instructions, not {}", ee.steps);
}

#[test]
fn a_delay_slot_runs_even_when_the_branch_is_taken() {
    // The classic trap when reading a decompilation: `beq` jumps, and the instruction *after* it has
    // already been executed. A port that reads the listing top to bottom gets this wrong.
    let mut ee = prog(&[
        i(9, 0, 2, 1),   // addiu v0, zero, 1
        i(4, 0, 0, 2),   // beq   zero, zero, +2   (always taken, skips the addiu below)
        i(9, 2, 2, 10),  // addiu v0, v0, 10       <- delay slot: still runs
        i(9, 2, 2, 100), // addiu v0, v0, 100      <- jumped over
        JR_RA,
        NOP,
    ]);
    ee.call(CODE, &[], &[], 100);
    assert_eq!(ee.v0(), 11);

    // A "likely" branch is the exception: not taken, its delay slot is skipped.
    let mut ee = prog(&[
        i(9, 0, 2, 1),    // addiu v0, zero, 1
        i(0x15, 0, 0, 2), // bnel  zero, zero, +2   (never taken)
        i(9, 2, 2, 10),   // addiu v0, v0, 10       <- nullified
        i(9, 2, 2, 100),  // addiu v0, v0, 100
        JR_RA,
        NOP,
    ]);
    ee.call(CODE, &[], &[], 100);
    assert_eq!(ee.v0(), 101);
}

#[test]
fn arithmetic_is_32_bit_sign_extended_into_64_bit_registers() {
    let mut ee = prog(&[
        r(4, 5, 2, 0, 0x21), // addu v0, a0, a1   (32-bit add, sign-extended)
        JR_RA,
        NOP,
    ]);
    ee.call(CODE, &[0x7fff_ffff, 1], &[], 100);
    assert_eq!(ee.v0(), 0xffff_ffff_8000_0000, "the 32-bit sum sign-extends through the whole register");

    let mut ee = prog(&[
        r(4, 5, 2, 0, 0x2d), // daddu v0, a0, a1  (the 64-bit form does not)
        JR_RA,
        NOP,
    ]);
    ee.call(CODE, &[0x7fff_ffff, 1], &[], 100);
    assert_eq!(ee.v0(), 0x8000_0000);
}

#[test]
fn multiply_and_divide_match_the_r5900s_definitions() {
    let mut ee = prog(&[
        r(4, 5, 0, 0, 0x1a), // div   a0, a1
        r(0, 0, 2, 0, 0x12), // mflo  v0
        r(0, 0, 3, 0, 0x10), // mfhi  v1
        JR_RA,
        NOP,
    ]);
    ee.call(CODE, &[(-7i64) as u64, 2], &[], 100);
    assert_eq!(ee.v0() as i64, -3, "integer division truncates toward zero");
    assert_eq!(ee.r(3) as i64, -1, "and the remainder keeps the dividend's sign");

    // No trap on divide by zero: the quotient is defined, which is why a port must not `assert!(d != 0)`
    // where the original happily carried on.
    ee.call(CODE, &[5, 0], &[], 100);
    assert_eq!(ee.v0() as i64, -1);
    assert_eq!(ee.r(3), 5, "the dividend is left in hi");
}

#[test]
fn loads_and_stores_are_tracked_so_out_parameters_can_be_checked() {
    let mut ee = prog(&[
        i(0x2b, 4, 5, 0),    // sw a1, 0(a0)
        i(0x29, 4, 5, 4),    // sh a1, 4(a0)
        i(0x23, 4, 2, 0),    // lw v0, 0(a0)
        JR_RA,
        NOP,
    ]);
    let p = ee.scratch(0);
    ee.call(CODE, &[p as u64, 0x1234_abcd], &[], 100);
    assert_eq!(ee.v0() as u32, 0x1234_abcd, "what was stored reads back");
    assert_eq!(ee.u32_at(p), 0x1234_abcd);
    assert_eq!(ee.u16_at(p + 4), 0xabcd, "the halfword store keeps the low half");
    assert_eq!(ee.written_ranges(), vec![(p, p + 6)],
               "the write log is how a test asserts on a function's output parameters");

    // and nothing outside what it wrote
    assert_eq!(ee.u32_at(p + 8), 0);
}

#[test]
fn unaligned_loads_and_quadwords_work() {
    let mut ee = prog(&[
        i(0x22, 4, 2, 5),  // lwl v0, 5(a0)
        i(0x26, 4, 2, 2),  // lwr v0, 2(a0)   (the pair a compiler emits for a word at a+2)
        JR_RA,
        NOP,
    ]);
    let p = ee.scratch(0);
    ee.write(p, 8, 0x0807_0605_0403_0201); // bytes 01 02 03 04 05 06 07 08
    ee.call(CODE, &[p as u64], &[], 100);
    assert_eq!(ee.v0() as u32, 0x0605_0403, "the little-endian word starting at p+2");

    // lq/sq move 16 bytes at once, aligned down — how a compiler copies a struct.
    let mut ee = prog(&[
        i(0x1e, 4, 8, 0), // lq t0, 0(a0)
        i(0x1f, 5, 8, 0), // sq t0, 0(a1)
        JR_RA,
        NOP,
    ]);
    let (a, b) = (ee.scratch(0), ee.scratch(1));
    for k in 0..4 {
        ee.set_u32(a + 4 * k, 0x1000_0000 + k);
    }
    ee.call(CODE, &[a as u64, b as u64], &[], 100);
    for k in 0..4 {
        assert_eq!(ee.u32_at(b + 4 * k), 0x1000_0000 + k);
    }
}

#[test]
fn the_quadword_moves_take_the_halves_the_manual_says() {
    // pcpyld/pcpyud are how compiled EE code shuffles 128-bit values, and the two halves are easy to
    // get backwards — so they are pinned here rather than read twice.
    let mut ee = prog(&[
        i(0x1e, 4, 8, 0),           // lq     t0, 0(a0)
        i(0x1e, 5, 9, 0),           // lq     t1, 0(a1)
        mmi(8, 9, 10, 0x0e, 0x09),  // pcpyld t2, t0, t1
        i(0x1f, 6, 10, 0),          // sq     t2, 0(a2)
        mmi(8, 9, 11, 0x0e, 0x29),  // pcpyud t3, t0, t1
        i(0x1f, 6, 11, 16),         // sq     t3, 16(a2)
        JR_RA,
        NOP,
    ]);
    let (a, b, out) = (ee.scratch(0), ee.scratch(1), ee.scratch(2));
    for k in 0..4u32 {
        ee.set_u32(a + 4 * k, k);
        ee.set_u32(b + 4 * k, 4 + k);
    }
    ee.call(CODE, &[a as u64, b as u64, out as u64], &[], 100);
    let got: Vec<u32> = (0..8).map(|k| ee.u32_at(out + 4 * k)).collect();
    assert_eq!(got[..4], [4, 5, 0, 1], "pcpyld: low half from rt, high half from rs's low half");
    assert_eq!(got[4..], [2, 3, 6, 7], "pcpyud: low half from rs's high half, high half from rt's");
}

#[test]
fn the_fpu_goes_through_the_ps2_float_model() {
    let mut ee = prog(&[
        c1(13, 12, 0, 2),  // mul.s  $f0, $f12, $f13
        c1(14, 0, 0, 0),   // add.s  $f0, $f0,  $f14
        JR_RA,
        NOP,
    ]);
    // the same thing with the accumulator, which is *not* a fused multiply-add
    let mut madd = prog(&[
        c1(14, 14, 0, 0x18), // adda.s acc, $f14, $f14  -> 2c, just to load the accumulator
        c1(13, 12, 0, 0x1c), // madd.s $f0, $f12, $f13
        JR_RA,
        NOP,
    ]);
    for (a, b, c) in [(1.1f32, 2.2f32, 3.3f32), (0.1, 0.1, 0.1), (1e20, 1e20, -1.0), (-7.5, 0.333, 12.0)] {
        ee.call(CODE, &[], &[a, b, c], 100);
        assert_eq!(ee.f0().to_bits(), fpu::add(fpu::mul(a, b), c).to_bits(), "mul.s then add.s of {a} {b} {c}");

        madd.call(CODE, &[], &[a, b, c], 100);
        assert_eq!(madd.f0().to_bits(), fpu::madd(fpu::add(c, c), a, b).to_bits(),
                   "madda.s/madd.s of {a} {b} {c}");
    }
    // ±max rather than an infinity, exactly as the hardware does it
    ee.call(CODE, &[], &[3.0e38, 3.0e38, 0.0], 100);
    assert_eq!(ee.f0(), f32::from_bits(0x7f7f_ffff));
}

#[test]
fn float_compares_branch_on_the_single_condition_flag() {
    let mut ee = prog(&[
        c1(13, 12, 0, 0x34), // c.lt.s $f12, $f13
        0x4501_0002,         // bc1t   +2
        i(9, 0, 2, 0),       //   delay slot: v0 = 0
        i(9, 0, 2, 1),       // v0 = 1   (skipped when f12 < f13)
        JR_RA,
        NOP,
    ]);
    for (a, b, want) in [(1.0f32, 2.0f32, 0u64), (2.0, 1.0, 1), (1.0, 1.0, 1)] {
        ee.call(CODE, &[], &[a, b], 100);
        assert_eq!(ee.v0(), want, "c.lt.s {a} {b}");
    }
}

#[test]
fn vu0_macro_mode_runs_a_cross_product() {
    // vopmula/vopmsub: the two instructions a PS2 compiler emits for a cross product. Written out here
    // in full because this is the idiom most likely to be hiding in a physics routine.
    let mut ee = prog(&[
        i(0x36, 4, 4, 0),  // lqc2  vf4, 0(a0)
        i(0x36, 5, 5, 0),  // lqc2  vf5, 0(a1)
        0x4bc5_22fe,       // vopmula.xyz ACC, vf4, vf5
        0x4bc4_286e,       // vopmsub.xyz vf1, vf5, vf4
        i(0x3e, 6, 1, 0),  // sqc2  vf1, 0(a2)
        JR_RA,
        NOP,
    ]);
    let (a, b, out) = (ee.scratch(0), ee.scratch(1), ee.scratch(2));
    let (u, v) = ([1.5f32, -2.0, 0.25, 0.0], [3.0f32, 0.5, -1.0, 0.0]);
    for k in 0..4 {
        ee.set_f32(a + 4 * k as u32, u[k]);
        ee.set_f32(b + 4 * k as u32, v[k]);
    }
    ee.call(CODE, &[a as u64, b as u64, out as u64], &[], 100);
    let want = ps2_float::vu::cross(u, v);
    for (k, lane) in want.iter().take(3).enumerate() {
        assert_eq!(ee.f32_at(out + 4 * k as u32).to_bits(), lane.to_bits(), "lane {k}");
    }
}

#[test]
fn a_hook_replaces_a_function_the_interpreter_should_not_run() {
    // `jal` to a routine that would need hardware (or that you have not identified yet); the hook stands
    // in for it. This is also how you trace: a hook that only records its arguments.
    let callee = CODE + 0x100;
    let mut ee = prog(&[
        i(9, 29, 29, -16),           // addiu sp, sp, -16
        i(0x3f, 29, 31, 0),          // sd    ra, 0(sp)
        3 << 26 | callee >> 2,       // jal   callee
        NOP,
        i(9, 2, 2, 1),               // addiu v0, v0, 1   (whatever the callee returned, plus one)
        i(0x37, 29, 31, 0),          // ld    ra, 0(sp)
        JR_RA,
        i(9, 29, 29, 16),            // addiu sp, sp, 16  (delay slot)
    ]);
    let seen = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    let log = seen.clone();
    ee.hooks.insert(callee, Box::new(move |e| {
        log.lock().unwrap().push(e.r(4));
        e.set(2, 41);
    }));
    ee.call(CODE, &[7], &[], 100);
    assert_eq!(ee.v0(), 42);
    assert_eq!(*seen.lock().unwrap(), vec![7], "the hook saw the argument it was passed");
}

#[test]
fn an_unknown_instruction_says_what_and_where() {
    let bad = 0x4c00_0000u32; // COP3: not an EE instruction at all
    let mut ee = prog(&[i(9, 0, 2, 1), bad, JR_RA, NOP]);
    let msg = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| ee.call(CODE, &[], &[], 100)))
        .expect_err("an unimplemented instruction must stop the call, not be skipped");
    let msg = msg.downcast_ref::<String>().expect("panic payload").clone();
    assert!(msg.contains("0x4c000000"), "the message must carry the encoded word: {msg}");
    assert!(msg.contains(&format!("{:#08x}", CODE + 4)), "and the address: {msg}");
    assert!(msg.contains("ee.rs"), "and where to fix it: {msg}");
}

#[test]
fn the_budget_stops_a_function_that_waits_for_hardware() {
    let mut ee = prog(&[i(4, 0, 0, -1), NOP]); // beq zero, zero, -1: a DMA wait with nothing to wait for
    let msg = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| ee.call(CODE, &[], &[], 500)))
        .expect_err("an endless loop must hit the budget");
    let msg = msg.downcast_ref::<String>().unwrap().clone();
    assert!(msg.contains("500 instructions"), "{msg}");
    assert!(msg.contains("hook"), "the message should point at the fix: {msg}");
}

#[test]
fn a_bad_pointer_argument_is_caught_rather_than_read_as_rubbish() {
    let mut ee = prog(&[i(0x23, 4, 2, 0), JR_RA, NOP]); // lw v0, 0(a0)
    let msg = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| ee.call(CODE, &[0xdead_beef], &[], 100)))
        .expect_err("a wild pointer must not silently return zero");
    assert!(msg.downcast_ref::<String>().unwrap().contains("0xdeadbeef"));
}
