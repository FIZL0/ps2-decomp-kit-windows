//! The worked example: the game's own code against a Rust port, ten thousand inputs at a time.
//!
//! Everything here needs *your* disc, so everything here skips with a message when it is not set up yet
//! — a fresh checkout stays green. What it prints when it skips is the instruction for making it run.
//!
//! Copy this file into your own crate's `tests/` and change two things: the name you look up and the
//! Rust function you compare it to.

use ps2_oracle::{Ee, Kit, skip};

/// A tiny xorshift, so the inputs are the same on every machine and a failure is reproducible from the
/// seed alone. (A real random source would make "it failed once in CI" unfixable.)
struct Rng(u64);
impl Rng {
    fn next(&mut self) -> u32 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 7;
        self.0 ^= self.0 << 17;
        (self.0 >> 32) as u32
    }
    /// A float in `-range..range`, including the awkward sizes: this is the whole advantage over a
    /// recording, so do not waste it on 0..1.
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
}

/// Run one single-argument float function both ways and stop at the first disagreement.
///
/// The failure message carries the input, in decimal *and* as a bit pattern, because "0.1 differs" is not
/// something anyone can act on and `0x3dcccccd` is.
fn compare1(ee: &mut Ee, at: u32, name: &str, port: impl Fn(f32) -> f32, n: u32, range: f32) {
    let mut rng = Rng(0x2545_f491_4f6c_dd1d);
    let mut worst = 0u32;
    for k in 0..n {
        let x = if k < 8 {
            [0.0, -0.0, 1.0, -1.0, 0.5, range, -range, f32::from_bits(1)][k as usize]
        } else {
            rng.float(range)
        };
        ee.call(at, &[], &[x], 200_000);
        let (got, want) = (ee.f0(), port(x));
        if got.to_bits() != want.to_bits() {
            // not an immediate failure for the last-bit cases: report, then fail once with a count
            worst += 1;
            assert!(worst > 1 || got.to_bits().abs_diff(want.to_bits()) > 1,
                    "{name}({x} = {:#010x}): the game gives {got} ({:#010x}), the port {want} ({:#010x})",
                    x.to_bits(), got.to_bits(), want.to_bits());
        }
    }
    assert_eq!(worst, 0, "{name}: {worst} of {n} inputs differ from the port");
    println!("{name}: {n} inputs, bit-exact");
}

#[test]
fn the_disc_loads_and_the_code_is_really_there() {
    let kit = match Kit::load() {
        Ok(k) => k,
        Err(why) => return skip(&why),
    };
    let ee = match Ee::game() {
        Ok(ee) => ee,
        Err(why) => return skip(&why),
    };
    // Code, not zeroes: every program the config names must have landed somewhere.
    for o in &kit.overlays {
        let w = ee.u32_at(o.load);
        assert_ne!(w, 0, "overlay {} loaded nothing at {:#x} — check its `file`, `load` and `off`",
                   o.name, o.load);
    }
    // And the stack is inside RAM, which is the other thing that silently breaks a call.
    assert!(ee.stack as usize + 0x1000 < ee.ram.len());
    println!("loaded {} + {} overlay(s), {} MB of RAM", kit.boot_elf, kit.overlays.len(), ee.ram.len() >> 20);
}

#[test]
fn a_ram_image_can_stand_in_for_the_disc() {
    // `kit state ram <slot>` writes one of these next to the save state it came from.
    let dir = match Kit::load() {
        Ok(k) => k.ctx("states"),
        Err(why) => return skip(&why),
    };
    let newest = std::fs::read_dir(&dir)
        .ok()
        .into_iter()
        .flatten()
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.to_string_lossy().ends_with(".eeMemory.bin"))
        .max();
    let Some(p) = newest else {
        return skip(&format!("no RAM image in {} — `kit state ram 3` writes one", dir.display()));
    };
    let ee = Ee::from_ram(&p).expect("a RAM image that exists should load");
    assert_eq!(ee.ram.len() & 0xfffff, 0, "a RAM image is a whole number of megabytes");
    println!("{} loaded, {} MB", p.display(), ee.ram.len() >> 20);
}

#[test]
fn the_games_own_maths_agrees_with_the_port() {
    // The example uses the game's libm, because every PS2 game has one and `ps2-float` already claims to
    // reproduce it — so this test is a real check of a real claim, not a demonstration. Add whichever of
    // these your game has:   kit oracle add sinf 0x1a2b3c
    /// a name in the addresses file, the Rust function it should match, and the range to try
    type Case = (&'static str, fn(f32) -> f32, f32);
    let candidates: [Case; 5] = [
        ("sqrtf", ps2_float::libm::sqrtf, 1.0e9),
        ("sinf", ps2_float::libm::sinf, 100.0),
        ("cosf", ps2_float::libm::cosf, 100.0),
        ("atanf", ps2_float::libm::atanf, 1.0e4),
        ("expf", ps2_float::libm::expf, 60.0),
    ];
    let kit = match Kit::load() {
        Ok(k) => k,
        Err(why) => return skip(&why),
    };
    let known = kit.addresses().unwrap_or_default();
    let found: Vec<_> = candidates.iter().filter(|(n, ..)| known.iter().any(|(k, _)| k == n)).collect();
    if found.is_empty() {
        return skip(&format!("none of {} are in {} — add one with `kit oracle add <name> <addr>` to \
                              turn this test on",
                             candidates.map(|c| c.0).join("/"), kit.oracle_file.display()));
    }
    let mut ee = match Ee::game() {
        Ok(ee) => ee,
        Err(why) => return skip(&why),
    };
    for (name, port, range) in found {
        let at = kit.addr(name).unwrap();
        compare1(&mut ee, at, name, port, 10_000, *range);
    }
}
