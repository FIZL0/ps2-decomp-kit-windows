//! The function oracle: run one of the original game's own functions, on inputs you choose, with no
//! emulator anywhere.
//!
//! # What it is for
//!
//! A port is "probably right" until something proves it. The usual proof is a recording: drive the real
//! game under an emulator, log what a function produced, compare. That works, but a recording only ever
//! contains the inputs the game happened to produce — a few hundred, all from normal play, none of them
//! near the edges where ports actually break.
//!
//! This crate loads the game's code into a small R5900 interpreter and calls the function directly. A test
//! can then do what a recording cannot:
//!
//! ```no_run
//! use ps2_oracle::{Ee, addr};
//! let mut ee = Ee::game().expect("disc");        // boot ELF + overlays, serial checked
//! let f = addr("angle_between").unwrap();        // from research/oracle.txt, never hard-coded
//! for k in 0..10_000u32 {
//!     let (x, y) = (rnd(k), rnd(k ^ 0x9e37));
//!     ee.call(f, &[], &[x, y], 100_000);
//!     assert_eq!(ee.f0().to_bits(), my_port::angle_between(x, y).to_bits(),
//!                "angle_between({x}, {y})");   // first mismatch names its inputs
//! }
//! # fn rnd(_: u32) -> f32 { 0.0 }
//! # mod my_port { pub fn angle_between(_: f32, _: f32) -> f32 { 0.0 } }
//! ```
//!
//! Ten thousand cases take a second or two. Every one of them is bit-exact or the test fails with the
//! inputs that broke it, which is the whole point: you find the disagreement, not the next player.
//!
//! # What it can prove
//!
//! A pure function of its arguments and of RAM. In practice that is most of what a port gets wrong:
//! maths and geometry, table lookups and interpolation, AI decisions, state machines whose state you can
//! load from a RAM dump, scoring and rules, random number generators, string and data decoders.
//!
//! # What it cannot prove, at all
//!
//! Anything whose answer depends on *when* it ran, or on hardware this interpreter does not contain:
//!
//! * **Timing and frame flow** — how many frames a state lasts, what order subsystems update in, what a
//!   function returns on the second call because something else moved in between.
//! * **DMA** — transfers complete instantly or not at all; a function that waits on one will spin until
//!   the instruction budget runs out. Hook it.
//! * **VU1, and VU0 in micro mode** — only VU0's macro-mode instructions (the ones the EE issues inline)
//!   are executed. An uploaded micro program is not run.
//! * **The GS** — nothing is drawn, no register has an effect, and no amount of calling will tell you what
//!   a frame looked like.
//! * **The IOP, the SPU, the BIOS and the kernel** — no syscalls, no threads, no files, no sound, no pad.
//! * **Memory it was never given** — an uninitialised global reads as zero rather than as whatever a
//!   running game had there. That is the commonest cause of a false mismatch; see [`Ee::from_ram`].
//!
//! For all of that, record the real machine instead (`kit record`, `kit pine`). The two techniques are
//! complementary, and the division is sharp: the oracle proves *what* a function computes, a recording
//! proves *when* it is called.
//!
//! # Contracts worth knowing before you start
//!
//! **Unknown instructions panic, with the address and the encoded word.** The interpreter implements what
//! a C compiler emits plus the EE's common extras; it is not complete, and it does not pretend to be. The
//! message names the instruction and where it was, and `kit dis <addr>` shows it in context. The fix is
//! to add the instruction to `src/ee.rs` — usually a dozen lines next to its neighbours — not to work
//! around it.
//!
//! **Addresses come from a file, never from source.** [`addr`] reads `[oracle] addresses` (by default
//! `research/oracle.txt`, a `name 0xaddr` per line, added with `kit oracle add`). Addresses are specific
//! to one build of one game: putting them in a crate makes the crate wrong for every other build, and
//! makes the next person wonder which disc a bare hex number came from. The name is the documentation.
//!
//! **The serial is checked.** [`Ee::game`] refuses a disc whose `SYSTEM.CNF` boot serial is not the one in
//! `kit.toml`. An address from a US build means something else entirely in the European one, and silent
//! nonsense is worse than an error.
//!
//! **Floats go through [`ps2_float`].** The interpreter's arithmetic *is* the model the port under test
//! uses, so a mismatch is always a difference in logic, never a difference in rounding.
//!
//! # Roughly what the interpreter knows
//!
//! * The integer set a C compiler emits: arithmetic and logic (32- and 64-bit), shifts, the branches and
//!   their delay slots including the "likely" forms, `mult`/`div` with the R5900's defined degenerate
//!   cases, the second multiplier (`mult1`, `mfhi1`, …), loads and stores of every width, the unaligned
//!   pairs (`lwl`/`lwr`/`ldl`/`ldr` and their stores), and quadwords (`lq`/`sq`).
//! * COP1 single precision in full, through [`ps2_float::fpu`]: the accumulator forms (`adda.s`,
//!   `madd.s`, …), `rsqrt.s`, `max.s`/`min.s`, the compares and `bc1t`/`bc1f`, `cvt.w.s`/`cvt.s.w`.
//! * VU0 macro mode through [`ps2_float::vu`]: the lane-masked and broadcast add/sub/mul/madd/msub,
//!   the `Q` unit (`vdiv`, `vsqrt`, `vrsqrt`), `vopmula`/`vopmsub`, `vmove`/`vmr32`, `qmfc2`/`qmtc2`,
//!   `lqc2`/`sqc2`.
//! * The MMI instructions that turn up in ordinary compiled code: `pcpyld`/`pcpyud`, `pand`/`por`/
//!   `pxor`/`pnor`, and the `*1` multiplier moves.
//!
//! Everything else — COP0, micro-mode VU0, the cache instructions' effects, syscalls — panics by name.
//! Teaching it one more instruction is a dozen lines in `src/ee.rs`.
//!
//! # Getting state in
//!
//! Three ways, in increasing order of effort:
//!
//! 1. [`Ee::game`] — code from the disc, RAM otherwise zero. Enough for anything that only reads its
//!    arguments and constant tables (which, in a shipped game, is most maths).
//! 2. [`Ee::from_ram`] — a RAM image saved from a running game (`kit state ram 3`), so globals, loaded
//!    data and object pools are all exactly as they were at that moment. No disc needed, and the fastest
//!    way to make a stubborn function behave.
//! 3. Build the state yourself: [`Ee::write`], [`Ee::set_f32`] and friends, with [`Ee::scratch`] for
//!    out-parameters. Verbose, but it is the only way to reach a case the game never reached.

pub mod disc;
pub mod ee;
pub mod kit;

pub use disc::Disc;
pub use ee::{DONE, Ee};
pub use kit::Kit;

/// The address recorded under `name` in the `[oracle] addresses` file.
///
/// The error is a printable sentence that lists the names the file does have and the command that adds
/// one, because the usual reason for failure is a typo or a name nobody has looked up yet.
pub fn addr(name: &str) -> Result<u32, String> {
    Kit::load()?.addr(name)
}

/// A machine with the game loaded, or a sentence saying what is missing.
///
/// Tests use this with [`skip`] so that a checkout with no disc still runs green:
///
/// ```no_run
/// # fn t() {
/// let Some(mut ee) = ps2_oracle::game_or_skip() else { return };
/// # }
/// ```
pub fn game_or_skip() -> Option<Ee> {
    match Ee::game() {
        Ok(ee) => Some(ee),
        Err(why) => {
            skip(&why);
            None
        }
    }
}

/// Print why a test is not running and return.
///
/// `cargo test` has no notion of "skipped", so the convention here is: say it out loud (visible with
/// `--nocapture`, and in CI logs), pass, and never pretend a test proved something it did not. Every
/// message names the thing that is missing, so a reader knows whether to care.
pub fn skip(why: &str) {
    println!("SKIPPED: {why}");
}

impl Ee {
    /// Load the game as `kit.toml` describes it: the boot ELF's `PT_LOAD` segments, then every
    /// `[[overlay]]` copied raw to its `load` address — which is what the game's own loader does.
    ///
    /// The disc's serial is checked first. RAM is `[emu] ram_mb` big and otherwise zero; see
    /// [`Ee::from_ram`] when zero is not good enough.
    pub fn game() -> Result<Ee, String> {
        let kit = Kit::load()?;
        let mut disc = Disc::open(&kit)?;
        disc.check_serial(&kit)?;
        let mut ee = Ee::bare(kit.ram);
        // `[boot] elf` wins if it is set; otherwise SYSTEM.CNF says which file the console boots.
        let boot = match kit.boot_elf.as_str() {
            "" => disc.boot2()?.0,
            name => name.to_string(),
        };
        let image = disc.read(&boot)?;
        for (va, bytes) in disc::elf_segments(&image)? {
            ee.load(va, bytes);
        }
        for o in &kit.overlays {
            let data = disc.read(&o.file)?;
            let at = (o.off as usize).min(data.len());
            ee.load(o.load, &data[at..]);
        }
        Ok(ee)
    }

    /// Check that a RAM image holds the same overlay the disc does — in other words, that the dump came
    /// from the build whose addresses you are about to use.
    ///
    /// Opt-in, because [`Ee::from_ram`] deliberately needs no disc. Worth calling once in any test suite
    /// that uses dumps: a save state from another revision loads perfectly happily and then calls the
    /// wrong code, and this is the only cheap way to notice.
    pub fn check_overlay(&self, name: &str) -> Result<(), String> {
        let kit = Kit::load()?;
        let o = kit.overlay(name).ok_or_else(|| format!("no overlay named {name:?} in kit.toml"))?;
        let mut disc = Disc::open(&kit)?;
        let data = disc.read(&o.file)?;
        let at = (o.off as usize).min(data.len());
        let n = (data.len() - at).min(0x1000);
        let want = &data[at..at + n];
        let have: Vec<u8> = (0..n).map(|k| self.u8_at(o.load + k as u32)).collect();
        if have != want {
            return Err(format!("this RAM image holds a different {name} overlay from {}\n\
                                The dump and the addresses must come from the same build.",
                               disc.describe()));
        }
        Ok(())
    }

    /// Load a RAM image saved from a running game — `kit state ram <slot>` writes one, and PCSX2's own
    /// "save EE memory" produces the same thing: a flat dump of EE RAM from address 0.
    ///
    /// This needs no disc and no config, and it is the answer whenever a function reads a global you have
    /// not identified. Two things to watch: *when* the dump was taken (it is a moment, with whatever mode,
    /// level and objects were live then), and *which build* it came from — [`Ee::check_overlay`] answers
    /// the second one.
    pub fn from_ram(path: impl AsRef<std::path::Path>) -> Result<Ee, String> {
        let p = path.as_ref();
        let ram = std::fs::read(p).map_err(|e| format!("cannot read the RAM image {}: {e}", p.display()))?;
        if ram.len() < 0x10_0000 {
            return Err(format!("{} is only {} bytes — that is not an EE RAM image (expected 32 MB; \
                                `kit state ram <slot>` writes one)", p.display(), ram.len()));
        }
        Ok(Ee::with_ram(ram))
    }
}
