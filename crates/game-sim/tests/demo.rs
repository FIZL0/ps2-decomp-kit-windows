//! The port of the synthetic game, proved against the synthetic game.
//!
//! Unlike `crates/ps2-oracle/tests/game.rs`, nothing here needs a disc: `kit demo` generates the
//! program these tests call, so they run on a fresh checkout and in CI with nothing installed but a Rust
//! toolchain and Python. If the demo is not there yet, the first test builds it.
//!
//! Three things are being proved, in rising order of how much they matter:
//!
//! 1. the integer functions match (they do, first try — integer code usually does);
//! 2. the float function matches **bit for bit over ten thousand inputs**, which is the standard this
//!    whole kit exists to make reachable;
//! 3. the same float function written with the host's `f32` **does not** match, and fails within a few
//!    dozen inputs. That is the argument for `ps2-float`, and for every rule in `game-sim`'s header,
//!    reduced to one assertion.
//!
//! The program is generated, not extracted: see `ps2kit/demo.py` and `docs/EXAMPLE.md`. No byte of any
//! real game is involved.

use std::path::{Path, PathBuf};
use std::sync::OnceLock;

use game_sim::demo;
use ps2_float::F;
use ps2_oracle::{skip, Ee, Kit};

/// Point `ps2-oracle` at the generated demo, building it first if it is missing.
///
/// `Kit::load()` finds its config through `$KIT_ROOT`, which is process-wide, and Rust runs tests in
/// threads of one process — so this happens exactly once, inside a `OnceLock`, and every test reads the
/// environment only through this function's return value. (`crates/ps2-oracle/tests/loader.rs` solves the
/// same problem the other way, by having a single test; either is fine, neither is optional.)
fn demo_kit() -> Result<&'static PathBuf, &'static String> {
    static SETUP: OnceLock<Result<PathBuf, String>> = OnceLock::new();
    SETUP.get_or_init(|| {
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..").canonicalize().unwrap();
        let dir = root.join("context/demo");
        if !dir.join("kit.toml").exists() || !dir.join("elf/boot.elf").exists() {
            // `kit demo` is deterministic and takes well under a second, so building it here is
            // cheaper than explaining to a reader why their checkout has a skipping test in it.
            let out = std::process::Command::new("python3")
                .arg(root.join("kit"))
                .args(["demo", "--out"])
                .arg(&dir)
                .output()
                .map_err(|e| format!("could not run `kit demo`: {e} — run it yourself, then retry"))?;
            if !out.status.success() {
                return Err(format!("`kit demo` failed:\n{}", String::from_utf8_lossy(&out.stderr)));
            }
            // The ELFs are what the oracle actually loads; `kit extract` + `kit elf` make them.
            for step in [["extract"].as_slice(), ["elf"].as_slice()] {
                let out = std::process::Command::new("python3")
                    .arg(root.join("kit"))
                    .args(step)
                    .env("KIT_ROOT", &dir)
                    .output()
                    .map_err(|e| format!("could not run `kit {}`: {e}", step[0]))?;
                if !out.status.success() {
                    return Err(format!("`kit {}` failed:\n{}", step[0],
                                       String::from_utf8_lossy(&out.stderr)));
                }
            }
        }
        // SAFETY-adjacent note: this is the one place the environment is touched, before any test has
        // read it, and it is never changed afterwards.
        unsafe { std::env::set_var("KIT_ROOT", &dir) };
        Ok(dir)
    })
    .as_ref()
}

/// A tiny xorshift, so the inputs are identical on every machine and any failure is reproducible from
/// the seed alone.
struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u32 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 7;
        self.0 ^= self.0 << 17;
        (self.0 >> 32) as u32
    }

    /// A float in `-range..range` across several magnitudes. Generated inputs beat a recording precisely
    /// because they go where play never does, so they are not wasted on `0..1`.
    #[allow(clippy::float_arithmetic)] // a test's input generator is not ported code
    fn float(&mut self, range: f32) -> f32 {
        let (a, b) = (self.next(), self.next());
        let m = 1.0 - 2.0 * (a as f32 / u32::MAX as f32);
        let scale = match b % 4 {
            0 => 1.0,
            1 => 1.0 / 1024.0,
            2 => 1024.0,
            _ => 1.0 / 16_777_216.0, // down where the chop rounding shows
        };
        m * range * scale
    }

    /// The fixed inputs first — zero, both signs, the extremes, the smallest nonzero — then random ones.
    /// Half the bugs a port has are at those eight values, and they cost eight iterations to rule out.
    fn input(&mut self, k: u32, range: f32) -> f32 {
        if k < 8 {
            [0.0, -0.0, 1.0, -1.0, 0.5, range, -range, f32::from_bits(1)][k as usize]
        } else {
            self.float(range)
        }
    }
}

fn load() -> Option<(Kit, Ee)> {
    match demo_kit() {
        Err(why) => {
            skip(why);
            None
        }
        Ok(_) => match (Kit::load(), Ee::game()) {
            (Ok(kit), Ok(ee)) => Some((kit, ee)),
            (Err(why), _) | (_, Err(why)) => {
                skip(&why);
                None
            }
        },
    }
}

#[test]
fn the_generated_game_is_really_there() {
    let Some((kit, ee)) = load() else { return };
    assert_eq!(kit.serial, "SLUS-90001", "the demo's own made-up serial");
    for name in ["clamp_index", "step_for_gear", "blend_weights", "demo_main", "overlay_mix"] {
        let at = kit.addr(name).unwrap_or_else(|why| panic!("{why}"));
        assert_ne!(ee.u32_at(at), 0, "{name} at {at:#x} is zeroes, not instructions");
    }
    // `Ee::game()` reads the programs straight out of the disc image: the boot file named by
    // SYSTEM.CNF, then each overlay copied to its `load` address with its header skipped.
    println!("{}: boot + {} overlay(s), {} MB of RAM, 5 named functions",
             kit.serial, kit.overlays.len(), ee.ram.len() >> 20);
}

#[test]
fn the_integer_functions_match() {
    let Some((kit, mut ee)) = load() else { return };
    let (clamp, step) = (kit.addr("clamp_index").unwrap(), kit.addr("step_for_gear").unwrap());
    let mut rng = Rng(0x9e37_79b9_7f4a_7c15);

    for k in 0..10_000u32 {
        // Interesting values first: the table's own bounds, then arbitrary ones.
        let (x, lo, hi) = match k {
            0..=10 => (k as i32 - 2, 0, 7),
            _ => (rng.next() as i32 >> (k % 24), -(rng.next() as i32 >> 20), rng.next() as i32 >> 20),
        };
        // `slt` compares 64-bit registers, so arguments go in sign-extended, exactly as a real caller's
        // `addiu`/`lw` would leave them. Passing `x as u64` instead is the classic way to get a port that
        // "matches" for positive numbers only.
        ee.call(clamp, &[x as i64 as u64, lo as i64 as u64, hi as i64 as u64], &[], 10_000);
        assert_eq!(ee.v0() as i32, demo::clamp_index(x, lo, hi),
                   "clamp_index({x}, {lo}, {hi}) after {k} inputs");

        let gear = if k < 12 { k as i32 - 2 } else { rng.next() as i32 >> (k % 28) };
        ee.call(step, &[gear as i64 as u64], &[], 10_000);
        assert_eq!(ee.v0() as i32, demo::step_for_gear(gear), "step_for_gear({gear}) after {k} inputs");
    }
    println!("clamp_index, step_for_gear: 10000 inputs each, exact");
}

/// The standard: the port and the original agree **in every bit** over ten thousand inputs, including
/// the magnitudes play never reaches.
///
/// This is the test to copy for a real function. Note what it does not do: it does not compare with a
/// tolerance, it does not average an error, and it does not stop at the inputs that look sensible.
#[test]
fn the_float_function_is_bit_exact_over_ten_thousand_inputs() {
    let Some((kit, mut ee)) = load() else { return };
    let at = kit.addr("blend_weights").unwrap();
    let mut rng = Rng(0x2545_f491_4f6c_dd1d);
    const N: u32 = 10_000;

    for k in 0..N {
        let x = rng.input(k, 4096.0);
        ee.call(at, &[], &[x], 10_000);
        let (got, want) = (ee.f0(), demo::blend_weights(F(x)).to_f32());
        // `assert!` rather than `assert_eq!` on purpose: the message below is the whole bug report —
        // the input, both answers, and both bit patterns — and `assert_eq!` would bury it under the two
        // bit patterns printed as decimal integers.
        assert!(got.to_bits() == want.to_bits(),
                "blend_weights({x} = {:#010x}) differs at input {k} of {N}\n  \
                 the game gives {got} ({:#010x})\n  the port gives  {want} ({:#010x})",
                x.to_bits(), got.to_bits(), want.to_bits());
    }
    println!("blend_weights: {N} inputs, bit-exact");
}

/// **The point of this entire repository, as one assertion.**
///
/// `demo::blend_weights_in_host_f32` is the same arithmetic, in the same order, written with Rust's `+`
/// and `*` — the port anyone would write on their first day. It is not a worse approximation; it is a
/// different function. This test finds the first input where it disagrees with the hardware and asserts
/// that there *is* one, early.
///
/// Why it matters more than it looks: the first disagreement is in the last bit or two. Nothing on screen
/// changes, no test of "roughly right" notices, and the port ships. Then the error feeds a physics
/// integrator, and sixty frames later a ball lands on the other side of a line, and the AI's decision is
/// different, and the rally has no relationship to the original's. Bit-exactness is not perfectionism;
/// it is the only error bound that does not grow.
#[test]
fn the_same_function_in_host_f32_is_a_different_function() {
    let Some((kit, mut ee)) = load() else { return };
    let at = kit.addr("blend_weights").unwrap();
    let mut rng = Rng(0x2545_f491_4f6c_dd1d);

    let mut first = None;
    let mut differed = 0;
    for k in 0..10_000u32 {
        let x = rng.input(k, 4096.0);
        ee.call(at, &[], &[x], 10_000);
        let (game, host) = (ee.f0(), demo::blend_weights_in_host_f32(x));
        if game.to_bits() != host.to_bits() {
            differed += 1;
            first.get_or_insert((k, x, game, host));
        }
    }

    let (k, x, game, host) = first.expect(
        "host f32 agreed on all 10000 inputs — if this ever happens, the demo's weights have stopped \
         exercising the EE's chopping and the example no longer proves anything",
    );
    println!("host f32 first differs at input {k}: blend_weights({x} = {:#010x})\n  \
              the game gives {game} ({:#010x})\n  host f32 gives {host} ({:#010x})\n  \
              {differed} of 10000 inputs differ ({:.0}%)",
             x.to_bits(), game.to_bits(), host.to_bits(), differed as f64 / 100.0);
    assert!(k < 64, "host f32 first differed only at input {k}; the example wants it obvious");
    assert!(differed > 1_000, "only {differed} of 10000 inputs differ — expected most of them");
}

/// The overlay's function, for the same reason the demo has an overlay at all: a port has to prove code
/// that was never in the boot ELF, loaded at an address only the loader knows.
///
/// It is also the one that uses the FPU accumulator twice (`mula.s` then `madd.s`), which is the shape no
/// host-arithmetic port can express at all.
#[test]
fn an_overlays_function_can_be_proved_too() {
    let Some((kit, mut ee)) = load() else { return };
    let at = kit.addr("overlay_mix").unwrap();
    let mut rng = Rng(0xdead_beef_1234_5678);

    for k in 0..10_000u32 {
        let (x, y) = (rng.input(k, 1024.0), rng.input(k.wrapping_add(3), 1024.0));
        ee.call(at, &[], &[x, y], 10_000);
        // mula.s $f12, c0  →  ACC = chop(x * 0.6);  madd.s $f0, $f13, c1  →  ACC + chop(y * 0.4)
        let want = (F(x) * 0.6f32).madd(F(y), 0.4f32);
        assert_eq!(ee.f0().to_bits(), want.to_bits(),
                   "overlay_mix({x}, {y}) after {k} inputs: {} vs {want}", ee.f0());
    }
    println!("overlay_mix: 10000 inputs, bit-exact");
}
