//! The path from `kit.toml` to a running function, proved end to end on a disc image this test builds
//! itself.
//!
//! No game data is involved: the ISO 9660 image, the boot ELF and the overlay are a few hundred bytes of
//! structure assembled here, holding three instructions. That is enough to exercise everything between a
//! config file and an answer — the TOML reader, the ISO directory walk, `SYSTEM.CNF`, the serial check,
//! the ELF program headers, overlay placement and the call itself — on any machine, with no disc.
//!
//! Everything runs in one test function on purpose: it sets `KIT_ROOT`, which is process-wide.

use kit_oracle::{Ee, Kit, KitEe, disc::elf_segments, open_disc};
use std::io::Write;

const SECTOR: usize = 2048;

/// One ISO 9660 directory record. Both byte orders, as the format insists.
fn rec(name: &[u8], extent: u32, size: u32, is_dir: bool) -> Vec<u8> {
    let nlen = name.len();
    let mut r = vec![0u8; 33 + nlen + (nlen + 1) % 2];
    r[0] = r.len() as u8;
    r[2..6].copy_from_slice(&extent.to_le_bytes());
    r[6..10].copy_from_slice(&extent.to_be_bytes());
    r[10..14].copy_from_slice(&size.to_le_bytes());
    r[14..18].copy_from_slice(&size.to_be_bytes());
    r[25] = if is_dir { 2 } else { 0 };
    r[28..30].copy_from_slice(&1u16.to_le_bytes());
    r[30..32].copy_from_slice(&1u16.to_be_bytes());
    r[32] = nlen as u8;
    r[33..33 + nlen].copy_from_slice(name);
    r
}

/// A PS2-style ELF: one `PT_LOAD` segment holding `code` at `va`.
fn elf(va: u32, code: &[u8]) -> Vec<u8> {
    let mut e = vec![0u8; 0x34 + 32];
    e[..4].copy_from_slice(b"\x7fELF");
    e[4..7].copy_from_slice(&[1, 1, 1]); // 32-bit, little-endian, version 1
    e[16..18].copy_from_slice(&2u16.to_le_bytes()); // ET_EXEC
    e[18..20].copy_from_slice(&8u16.to_le_bytes()); // EM_MIPS
    e[24..28].copy_from_slice(&va.to_le_bytes()); // entry
    e[28..32].copy_from_slice(&0x34u32.to_le_bytes()); // phoff
    e[0x2a..0x2c].copy_from_slice(&32u16.to_le_bytes()); // phentsize
    e[0x2c..0x2e].copy_from_slice(&1u16.to_le_bytes()); // phnum
    let (off, p) = (0x54u32, 0x34);
    e[p..p + 4].copy_from_slice(&1u32.to_le_bytes()); // PT_LOAD
    e[p + 4..p + 8].copy_from_slice(&off.to_le_bytes());
    e[p + 8..p + 12].copy_from_slice(&va.to_le_bytes());
    e[p + 16..p + 20].copy_from_slice(&(code.len() as u32).to_le_bytes()); // filesz
    e[p + 20..p + 24].copy_from_slice(&(code.len() as u32 + 64).to_le_bytes()); // memsz: some .bss
    e.extend_from_slice(code);
    e
}

/// Lay the files out one per sector and write the image.
fn iso(path: &std::path::Path, files: &[(&str, Vec<u8>)]) {
    // sector 16 PVD, 17 root directory, 18 the ZZ subdirectory, 19+ the files
    let mut img = vec![0u8; 22 * SECTOR];
    img[16 * SECTOR] = 1;
    img[16 * SECTOR + 1..16 * SECTOR + 6].copy_from_slice(b"CD001");
    img[16 * SECTOR + 6] = 1;
    let root = rec(&[0], 17, SECTOR as u32, true);
    img[16 * SECTOR + 156..16 * SECTOR + 156 + root.len()].copy_from_slice(&root);

    let (mut rootdir, mut subdir) = (Vec::new(), Vec::new());
    rootdir.extend(rec(&[0], 17, SECTOR as u32, true));
    rootdir.extend(rec(&[1], 17, SECTOR as u32, true));
    rootdir.extend(rec(b"ZZ", 18, SECTOR as u32, true));
    subdir.extend(rec(&[0], 18, SECTOR as u32, true));
    subdir.extend(rec(&[1], 17, SECTOR as u32, true));
    for (k, (name, data)) in files.iter().enumerate() {
        let sector = 19 + k;
        let at = sector * SECTOR;
        img[at..at + data.len()].copy_from_slice(data);
        let (dir, leaf) = name.rsplit_once('/').unwrap_or(("", name));
        let entry = rec(format!("{leaf};1").as_bytes(), sector as u32, data.len() as u32, false);
        if dir.is_empty() { &mut rootdir } else { &mut subdir }.extend(entry);
    }
    img[17 * SECTOR..17 * SECTOR + rootdir.len()].copy_from_slice(&rootdir);
    img[18 * SECTOR..18 * SECTOR + subdir.len()].copy_from_slice(&subdir);
    std::fs::File::create(path).unwrap().write_all(&img).unwrap();
}

#[test]
fn a_config_a_disc_and_a_call() {
    let root = std::env::temp_dir().join(format!("kit-oracle-loader-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    std::fs::create_dir_all(&root).unwrap();

    // `addiu v0, a0, 1; jr ra; nop` — the boot program's one function, at 0x100000.
    let boot_code: Vec<u8> = [0x2482_0001u32, 0x03e0_0008, 0]
        .iter().flat_map(|w| w.to_le_bytes()).collect();
    // the overlay: 0x100 bytes of header, then `addiu v0, a0, -1; jr ra; nop` at 0x180000
    let mut ovl = vec![0xaau8; 0x100];
    ovl.extend([0x2482_ffffu32, 0x03e0_0008, 0].iter().flat_map(|w| w.to_le_bytes()));

    iso(&root.join("fake.iso"), &[
        ("SYSTEM.CNF", b"BOOT2 = cdrom0:\\SLUS_200.00;1\r\nVER = 1.00\r\n".to_vec()),
        ("SLUS_200.00", elf(0x10_0000, &boot_code)),
        ("ZZ/OVL.BIN", ovl),
    ]);
    std::fs::write(root.join("addr.txt"),
                   "# one per line\ninc 0x100000\ndec 180000\n").unwrap();
    let toml = |serial: &str| format!(
        "[game]\nname = \"Test Game\"\nserial = \"{serial}\"\niso = \"fake.iso\"\n\n\
         [emu]\nram_mb = 2\n\n[boot]\nelf = \"\"\n\n\
         [[overlay]]\nname = \"game\"\nfile = \"ZZ/OVL.BIN\"\nload = \"0x180000\"\noff = 0x100\n\n\
         [oracle]\naddresses = \"addr.txt\"\n");
    std::fs::write(root.join("kit.toml"), toml("SLUS-20000")).unwrap();
    unsafe { std::env::set_var("KIT_ROOT", &root) };

    // --- the config -------------------------------------------------------------------------------
    let kit = Kit::load().expect("the config should load");
    assert_eq!(kit.serial, "SLUS-20000");
    assert_eq!(kit.ram, 2 << 20, "[emu] ram_mb is read, not assumed");
    assert_eq!(kit.overlays.len(), 1);
    assert_eq!((kit.overlays[0].load, kit.overlays[0].off), (0x18_0000, 0x100),
               "hex and decimal numbers both parse");
    assert_eq!(kit.addr("inc").unwrap(), 0x10_0000);
    assert_eq!(kit.addr("dec").unwrap(), 0x18_0000, "a bare hex address needs no 0x");
    let e = kit.addr("nosuch").unwrap_err();
    assert!(e.contains("inc") && e.contains("oracle add"), "an unknown name should help: {e}");

    // --- the disc ---------------------------------------------------------------------------------
    let mut disc = open_disc(&kit).expect("the image should open");
    assert_eq!(disc.boot2().unwrap(), ("SLUS_200.00".into(), "SLUS-20000".into()));
    assert_eq!(disc.check_serial(&kit.serial).unwrap(), "SLUS-20000");
    assert_eq!(disc.read("zz/ovl.bin").unwrap().len(), 0x10c,
               "the directory walk finds subdirectories, and case does not matter");
    let segs = elf_segments(&disc.read("SLUS_200.00").unwrap()).unwrap().len();
    assert_eq!(segs, 1);

    // --- the call ---------------------------------------------------------------------------------
    let mut ee = Ee::game().expect("a loaded game");
    ee.call(kit.addr("inc").unwrap(), &[41], &[], 100);
    assert_eq!(ee.v0(), 42, "the boot ELF's segment really ran");
    ee.call(kit.addr("dec").unwrap(), &[43], &[], 100);
    assert_eq!(ee.v0(), 42, "and so did the overlay, past its 0x100-byte header");

    // --- the refusals -----------------------------------------------------------------------------
    std::fs::write(root.join("kit.toml"), toml("SLUS-99999")).unwrap();
    let e = Ee::game().unwrap_err();
    assert!(e.contains("SLUS-20000") && e.contains("SLUS-99999"),
            "a disc that is not the configured build must be refused by name: {e}");

    std::fs::write(root.join("kit.toml"), toml("SLUS-20000").replace("fake.iso", "gone.iso")).unwrap();
    let e = Ee::game().unwrap_err();
    assert!(e.contains("gone.iso") && e.contains("extract"), "a missing image must say so: {e}");

    let _ = std::fs::remove_dir_all(&root);
    unsafe { std::env::remove_var("KIT_ROOT") };
}
