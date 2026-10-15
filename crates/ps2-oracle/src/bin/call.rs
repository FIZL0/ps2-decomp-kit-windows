//! `kit oracle` lives here: call one of the game's functions from a shell and print what came back.
//!
//! This is for looking, not for proving — a dozen calls by hand while you work out what a function does,
//! then a test in Rust that makes ten thousand. Usage (`kit oracle` passes these through):
//!
//! ```text
//! call <name|addr> [--ints 1,2] [--floats 0.5,-3] [--ram FILE] [--budget N] [--read ADDR:LEN]
//! ```
//!
//! Output is a few lines whatever happens, including on a panic: the point of the interpreter is that a
//! failure names the instruction and the address, and that is worth more than a backtrace.

use ps2_oracle::{Ee, Kit};

fn main() -> std::process::ExitCode {
    match run() {
        Ok(()) => std::process::ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("{e}");
            std::process::ExitCode::FAILURE
        }
    }
}

fn num(s: &str) -> Result<u64, String> {
    let s = s.trim().replace('_', "");
    let (neg, s) = match s.strip_prefix('-') {
        Some(rest) => (true, rest.to_string()),
        None => (false, s),
    };
    let v = match s.strip_prefix("0x").or_else(|| s.strip_prefix("0X")) {
        Some(h) => u64::from_str_radix(h, 16),
        None => s.parse::<u64>(),
    }
    .map_err(|_| format!("not a number: {s:?}"))?;
    Ok(if neg { (v as i64).wrapping_neg() as u64 } else { v })
}

/// A bit pattern as a float, or `-` when it cannot plausibly be one. An integer or a pointer read as a
/// float is a denormal with forty digits of nothing, and printing those costs more than it tells.
fn as_float(bits: u32) -> String {
    let e = bits >> 23 & 0xff;
    if bits == 0 || (0x61..=0xbb).contains(&e) { format!("{}", f32::from_bits(bits)) } else { "-".into() }
}

fn list(s: &str) -> Vec<String> {
    s.split(',').map(|p| p.trim().to_string()).filter(|p| !p.is_empty()).collect()
}

fn run() -> Result<(), String> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.is_empty() || args[0] == "-h" || args[0] == "--help" {
        println!("call <name|addr> [--ints 1,2] [--floats 0.5,-3] [--ram FILE] [--budget N] \
                  [--read ADDR:LEN]");
        return Ok(());
    }
    let mut what = String::new();
    let (mut ints, mut floats, mut reads) = (Vec::new(), Vec::new(), Vec::new());
    let (mut ram, mut budget) = (None, 2_000_000u64);
    let mut it = args.iter();
    while let Some(a) = it.next() {
        let mut next = |name: &str| it.next().cloned().ok_or(format!("{name} needs a value"));
        match a.as_str() {
            "--ints" => for v in list(&next("--ints")?) {
                ints.push(num(&v)?);
            },
            "--floats" => for v in list(&next("--floats")?) {
                floats.push(v.parse::<f32>().map_err(|_| format!("not a float: {v:?}"))?);
            },
            "--ram" => ram = Some(next("--ram")?),
            "--budget" => budget = num(&next("--budget")?)?,
            "--read" => {
                let spec = next("--read")?;
                let (a, n) = spec.split_once(':').ok_or("--read wants ADDR:LEN")?;
                reads.push((num(a)? as u32, num(n)? as usize));
            }
            other if other.starts_with('-') => return Err(format!("unknown option {other}")),
            other => what = other.to_string(),
        }
    }
    if what.is_empty() {
        return Err("which function? give a name from the addresses file, or an address".into());
    }

    // A name goes through the addresses file; a bare number is taken as written (and said so, because an
    // address with no name in the file is a note nobody else can read).
    let kit = Kit::load();
    let (at, from) = match num(&what) {
        Ok(a) if what.chars().next().is_some_and(|c| c.is_ascii_digit()) => (a as u32, "given".to_string()),
        _ => {
            let kit = kit.as_ref().map_err(|e| e.clone())?;
            (kit.addr(&what)?, format!("{}", kit.oracle_file.display()))
        }
    };

    let mut ee = match &ram {
        Some(p) => Ee::from_ram(p)?,
        None => Ee::game()?,
    };
    println!("{what} {at:#010x}  ({from}{})", match &ram {
        Some(p) => format!(", RAM from {p}"),
        None => String::new(),
    });

    let args_shown = ints.iter().map(|v| format!("{v:#x}")).chain(floats.iter().map(|v| v.to_string()))
        .collect::<Vec<_>>().join(", ");
    let out = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        ee.call(at, &ints, &floats, budget);
    }));
    if let Err(p) = out {
        let msg = p.downcast_ref::<String>().cloned()
            .or_else(|| p.downcast_ref::<&str>().map(|s| s.to_string()))
            .unwrap_or_else(|| "panicked".into());
        return Err(format!("call({args_shown}) stopped after {} instructions:\n{msg}", ee.steps));
    }

    println!("args    {args_shown}");
    println!("steps   {}", ee.steps);
    let v0 = ee.v0();
    // the float reading only when the bit pattern could plausibly be one: an integer return value read
    // as a float is a denormal with forty digits, and printing it helps nobody
    let f = as_float(v0 as u32);
    println!("v0      {:#x}  {}{}", v0, v0 as i64, if f == "-" { String::new() } else { format!("  (as a float: {f})") });
    println!("f0      {}  {:#010x}", ee.f0(), ee.f0().to_bits());
    let ranges = ee.written_ranges();
    for (a, b) in ranges.iter().take(8) {
        let n = (b - a).min(32) as usize;
        let bytes: Vec<String> = (0..n).map(|k| format!("{:02x}", ee.u8_at(a + k as u32))).collect();
        println!("wrote   {a:#010x}..{b:#010x}  {}{}", bytes.join(" "),
                 if (b - a) as usize > n { " …" } else { "" });
    }
    if ranges.len() > 8 {
        println!("wrote   … and {} more ranges", ranges.len() - 8);
    }
    for (a, n) in reads {
        let words: Vec<String> = (0..n.div_ceil(4).min(16))
            .map(|k| format!("{:#010x}", ee.u32_at(a + 4 * k as u32))).collect();
        let floats: Vec<String> = (0..n.div_ceil(4).min(16))
            .map(|k| as_float(ee.u32_at(a + 4 * k as u32))).collect();
        println!("read    {a:#010x}  {}", words.join(" "));
        if floats.iter().any(|f| f != "-") {
            println!("              as floats: {}", floats.join(" "));
        }
    }
    Ok(())
}
