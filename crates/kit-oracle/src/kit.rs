//! `kit.toml`, read from Rust.
//!
//! The kit's rule is that nothing game-specific lives outside `kit.toml`, and that holds on this side of
//! the fence too: no address, serial or file name is compiled into these crates. The Python front end
//! reads the same file with `tomllib`; this is a ~120-line reader for the subset the file uses, which is
//! cheaper than a TOML dependency and keeps the oracle buildable on a bare `cargo` with no network.
//!
//! Supported: `[table]`, `[[array-of-tables]]`, `key = "string" | 123 | 0x7b | 1.5 | true`, `#` comments,
//! and `\\`/`\"`/`\n`/`\t`/`\r`/`\0` escapes in strings. Not supported (and not used by `kit.toml`):
//! dotted keys, inline tables, arrays, multi-line strings, literal `'…'` strings, dates.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

/// One `[[overlay]]`: code the game copies over a fixed address at runtime.
#[derive(Clone, Debug)]
pub struct Overlay {
    /// The name used everywhere else in the kit (`kit fn -p game …`).
    pub name: String,
    /// Path inside the disc image.
    pub file: String,
    /// The address the game's loader copies it to.
    pub load: u32,
    /// Bytes of header to skip before the code, if the file has one.
    pub off: u32,
}

/// Everything in `kit.toml` the Rust side needs.
#[derive(Clone, Debug)]
pub struct Kit {
    /// The repo root: the directory holding `kit.toml`.
    pub root: PathBuf,
    /// `[game] serial`, e.g. `SLUS-20000`. Checked against the disc before anything is loaded.
    pub serial: String,
    /// `[game] iso`, absolute.
    pub iso: PathBuf,
    /// EE RAM size in bytes (`[emu] ram_mb`, 32 MB on a retail console).
    pub ram: usize,
    /// `[boot] elf`: the boot ELF's name on the disc. Empty means "ask `SYSTEM.CNF`".
    pub boot_elf: String,
    pub overlays: Vec<Overlay>,
    /// `[oracle] addresses`: the `name 0xaddr` list, absolute.
    pub oracle_file: PathBuf,
    /// `[paths] context`: the git-ignored working directory, absolute.
    pub context: PathBuf,
}

impl Kit {
    /// Load `kit.toml`, found by walking up from this crate (or from `$KIT_ROOT`).
    ///
    /// The error is a sentence meant to be printed as-is: it says which file is missing and what to do.
    pub fn load() -> Result<Kit, String> {
        let root = root()?;
        let path = root.join("kit.toml");
        let text = std::fs::read_to_string(&path).map_err(|e| match e.kind() {
            std::io::ErrorKind::NotFound => format!(
                "no kit.toml in {} yet\n    cp kit.toml.example kit.toml   # then fill in [game]\n    \
                 kit setup                    # or be walked through it", root.display()),
            _ => format!("{}: {e}", path.display()),
        })?;
        let t = Toml::parse(&text).map_err(|e| format!("kit.toml: {e}"))?;
        let abs = |p: &str, dflt: &str| -> PathBuf {
            let p = if p.is_empty() { dflt } else { p };
            if Path::new(p).is_absolute() { PathBuf::from(p) } else { root.join(p) }
        };
        let iso = t.str_("game.iso");
        if iso.is_empty() {
            return Err("kit.toml: [game] iso is empty — point it at your own disc image".into());
        }
        let mut overlays = Vec::new();
        for i in 0..t.count("overlay") {
            let k = |f: &str| format!("overlay.{i}.{f}");
            overlays.push(Overlay {
                name: t.str_(&k("name")).to_string(),
                file: t.str_(&k("file")).to_string(),
                load: t.num(&k("load")).ok_or_else(|| format!("kit.toml: overlay {i} has no load address"))? as u32,
                off: t.num(&k("off")).unwrap_or(0) as u32,
            });
        }
        Ok(Kit {
            serial: t.str_("game.serial").trim().to_uppercase(),
            iso: abs(iso, ""),
            ram: (t.num("emu.ram_mb").unwrap_or(32) as usize) << 20,
            boot_elf: t.str_("boot.elf").to_string(),
            overlays,
            oracle_file: abs(t.str_("oracle.addresses"), "research/oracle.txt"),
            context: abs(t.str_("paths.context"), "context"),
            root,
        })
    }

    /// The configured overlay with this name.
    pub fn overlay(&self, name: &str) -> Option<&Overlay> {
        self.overlays.iter().find(|o| o.name == name)
    }

    /// A path inside the private, git-ignored working directory.
    pub fn ctx(&self, rel: impl AsRef<Path>) -> PathBuf {
        self.context.join(rel)
    }

    /// Every name in the `[oracle] addresses` file, with its address, in file order.
    ///
    /// Format, one per line: `name [program] 0x1004c0 [note]`, `#` starts a comment. The program
    /// column is optional for a game with one program and wanted for every other, because overlays
    /// share their addresses — an address with no program beside it is not an address. That file is
    /// research notes, so it is the one place an address is written down — never a `const` in the port.
    pub fn addresses(&self) -> Result<Vec<(String, u32)>, String> {
        let text = std::fs::read_to_string(&self.oracle_file)
            .map_err(|e| format!("{}: {e}\nList the functions you want to call, one `name 0xaddr` per line.",
                                 self.oracle_file.display()))?;
        let mut out = Vec::new();
        for (n, line) in text.lines().enumerate() {
            let line = line.split('#').next().unwrap_or("").trim();
            if line.is_empty() {
                continue;
            }
            let mut tok = line.split_whitespace();
            let name = tok.next().unwrap_or("");
            // `name 0xaddr …` or `name program 0xaddr …`: the address is whichever of the next two
            // columns parses as hex, so a file written either way reads the same.
            let hex = |t: &str| u32::from_str_radix(t.trim_start_matches("0x").trim_start_matches("0X"), 16).ok();
            let a = match (tok.next(), tok.next()) {
                (Some(t), _) if hex(t).is_some() => hex(t),
                (Some(_), Some(t)) => hex(t),
                _ => None,
            };
            let a = a.ok_or_else(|| format!("{}:{}: want `name [program] 0xaddr [note]`, got {line:?}",
                                            self.oracle_file.display(), n + 1))?;
            out.push((name.to_string(), a));
        }
        Ok(out)
    }

    /// One function's address by name.
    pub fn addr(&self, name: &str) -> Result<u32, String> {
        let all = self.addresses()?;
        all.iter().find(|(n, _)| n == name).map(|(_, a)| *a).ok_or_else(|| {
            let names: Vec<_> = all.iter().map(|(n, _)| n.as_str()).take(12).collect();
            format!("no function named {name:?} in {}\nknown: {}{}\nAdd it with: kit oracle add {name} 0x…",
                    self.oracle_file.display(), names.join(" "), if all.len() > 12 { " …" } else { "" })
        })
    }
}

/// The directory holding `kit.toml`: `$KIT_ROOT`, or found by walking up from this crate.
pub fn root() -> Result<PathBuf, String> {
    if let Some(r) = std::env::var_os("KIT_ROOT") {
        return Ok(PathBuf::from(r));
    }
    let mut dir: PathBuf = env!("CARGO_MANIFEST_DIR").into();
    for _ in 0..6 {
        if dir.join("kit.toml").exists() {
            return Ok(dir);
        }
        if dir.join("kit.toml.example").exists() {
            return Err(format!(
                "no kit.toml in {} yet\n    cp kit.toml.example kit.toml   # then fill in [game]\n    \
                 kit setup                    # or be walked through it",
                dir.display()));
        }
        if !dir.pop() {
            break;
        }
    }
    Err("no kit.toml found above this crate; set KIT_ROOT to the kit's directory".into())
}

/// The `kit.toml` subset reader. Keys are dotted (`game.iso`, `overlay.0.load`).
pub struct Toml {
    vals: BTreeMap<String, Value>,
    counts: BTreeMap<String, usize>,
}

#[derive(Clone, Debug, PartialEq)]
pub enum Value {
    Str(String),
    Int(i64),
    Float(f64),
    Bool(bool),
}

impl Toml {
    pub fn parse(text: &str) -> Result<Toml, String> {
        let mut t = Toml { vals: BTreeMap::new(), counts: BTreeMap::new() };
        let mut prefix = String::new();
        for (n, raw) in text.lines().enumerate() {
            let line = strip_comment(raw).trim();
            if line.is_empty() {
                continue;
            }
            let at = |what: &str| format!("line {}: {what}", n + 1);
            if let Some(name) = line.strip_prefix("[[").and_then(|l| l.strip_suffix("]]")) {
                let name = name.trim();
                let i = t.counts.entry(name.to_string()).or_insert(0);
                prefix = format!("{name}.{i}.");
                *i += 1;
            } else if let Some(name) = line.strip_prefix('[').and_then(|l| l.strip_suffix(']')) {
                prefix = format!("{}.", name.trim());
            } else {
                let (key, val) = line.split_once('=').ok_or_else(|| at("not `key = value`"))?;
                let key = key.trim().trim_matches('"');
                if key.is_empty() {
                    return Err(at("empty key"));
                }
                t.vals.insert(format!("{prefix}{key}"), value(val.trim(), raw).map_err(|e| at(&e))?);
            }
        }
        Ok(t)
    }

    pub fn get(&self, key: &str) -> Option<&Value> {
        self.vals.get(key)
    }
    /// A string value, or `""` when it is missing or another type.
    pub fn str_(&self, key: &str) -> &str {
        match self.vals.get(key) {
            Some(Value::Str(s)) => s,
            _ => "",
        }
    }
    /// A number, whether it was written `32`, `"0x200000"` or `0x200000`.
    pub fn num(&self, key: &str) -> Option<i64> {
        match self.vals.get(key)? {
            Value::Int(i) => Some(*i),
            Value::Float(f) => Some(*f as i64),
            Value::Str(s) => parse_num(s),
            Value::Bool(_) => None,
        }
    }
    /// How many `[[name]]` blocks the file has.
    pub fn count(&self, name: &str) -> usize {
        self.counts.get(name).copied().unwrap_or(0)
    }
}

/// Cut an unquoted `#` comment off a line, leaving `#` inside strings alone.
fn strip_comment(line: &str) -> &str {
    let (mut quoted, mut esc) = (false, false);
    for (i, c) in line.char_indices() {
        match c {
            _ if esc => esc = false,
            '\\' if quoted => esc = true,
            '"' => quoted = !quoted,
            '#' if !quoted => return &line[..i],
            _ => {}
        }
    }
    line
}

fn value(s: &str, raw: &str) -> Result<Value, String> {
    if let Some(rest) = s.strip_prefix('"') {
        // the comment stripper left the closing quote in place
        let end = rest.rfind('"').ok_or_else(|| format!("unterminated string in {raw:?}"))?;
        let mut out = String::new();
        let mut it = rest[..end].chars();
        while let Some(c) = it.next() {
            out.push(match c {
                '\\' => match it.next() {
                    Some('n') => '\n',
                    Some('t') => '\t',
                    Some('r') => '\r',
                    Some('0') => '\0',
                    Some(other) => other, // \\ and \" land here, and so does anything exotic
                    None => break,
                },
                c => c,
            });
        }
        return Ok(Value::Str(out));
    }
    match s {
        "true" => return Ok(Value::Bool(true)),
        "false" => return Ok(Value::Bool(false)),
        _ => {}
    }
    if let Some(i) = parse_num(s) {
        return Ok(Value::Int(i));
    }
    s.parse::<f64>().map(Value::Float).map_err(|_| format!("cannot read {s:?} as a value"))
}

fn parse_num(s: &str) -> Option<i64> {
    let s = s.trim().replace('_', "");
    let (sign, s) = match s.strip_prefix('-') {
        Some(r) => (-1, r.to_string()),
        None => (1, s),
    };
    let v = match s.strip_prefix("0x").or_else(|| s.strip_prefix("0X")) {
        Some(h) => i64::from_str_radix(h, 16).ok()?,
        None => s.parse::<i64>().ok()?,
    };
    Some(sign * v)
}
