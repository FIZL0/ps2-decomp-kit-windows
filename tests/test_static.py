"""What the static layer promises, proved against inputs this file builds itself.

The static layer is everything the kit can do with no emulator attached: read a disc image, build the ELFs
Ghidra analyses, and answer questions out of a Ghidra dump. All three are *silent* when they go wrong —
a 2352-byte rip read as 2048 looks like a corrupt disc, an overlay guessed 0x100 too low decompiles into
plausible nonsense, and a mis-decoded `madda.s` reads as an integer multiply-add. None of that raises an
exception on its own, so the properties here are the ones that would otherwise cost a day:

* `disc` finds the sector stride instead of assuming it, derives the serial from SYSTEM.CNF, and refuses
  the wrong disc and compressed containers by name rather than guessing at them.
* `elf` reads by virtual address (with .bss zero-filled), round-trips what it writes, seeds an IRX with
  its own exports and prologues, and ranks a blob of real MIPS above a blob of noise while deriving the
  load address from the blob's own `jal`s.
* `decomp` streams one function out of a dump it never holds, finds look-alike functions, and decodes the
  R5900-only instructions no general MIPS disassembler knows — the whole reason the kit has its own.
* `ghidra` builds the right argv: `dump` must carry `-noanalysis -readOnly`, because without them it
  silently re-runs the twenty-minute analysis.

Everything is synthesised here: a hand-built ISO 9660 image, a hand-assembled MIPS blob, a fake dump in
Ghidra's own line format. No game data, no real serial, no real address. Nothing reaches the network,
Ghidra or any file outside one temp directory.

Run: python3 tests/test_static.py        (V=1 for tracebacks)
"""
from __future__ import annotations

import io
import os
import random
import shutil
import struct
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ps2kit import decomp as D
from ps2kit import disc as DISC
from ps2kit import elf as E
from ps2kit import ghidra as G

TESTS: list[tuple[str, object]] = []
PASS, FAIL = [], []


def check(name):
    """Register a test. Definition order is run order, so the cheap fixture-free tests fail first."""
    def deco(fn):
        TESTS.append((name, fn))
        return fn
    return deco


WORK = Path(tempfile.mkdtemp(prefix='ps2kit-static-'))


# --- a config that owns nothing but a temp directory -------------------------

class FakeCfg:
    """Enough of `config.Config` for the static layer, rooted in a temp directory.

    Every module binds `cfg` at import, so `use_cfg` swaps the module attribute and nothing in the kit
    ever looks for a kit.toml. `set()` only records, so a test can assert `--save` without writing config.
    """

    name = 'Synthetic Game'
    region, version = 'US', '1.00'
    ram = 32 << 20
    ghidra_name = 'synth'
    ghidra_timeout = 600
    states: dict = {}

    def __init__(self, root: Path, serial: str = 'SLUS-12345', programs=('boot', 'game')):
        self.root = Path(root)
        self.serial = serial
        self.programs = list(programs)
        self.iso: Path | None = None
        self.ghidra: Path | None = None
        self.symbols: dict[str, int] = {}
        self.wrote: list[tuple] = []

    @property
    def journal(self) -> Path:
        return self.root / 'journal'

    @property
    def ghidra_project(self) -> Path:
        return self.root / 'ghidra-project'

    def ctx(self, *parts) -> Path:
        p = self.root.joinpath('context', *parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def need_iso(self) -> Path:
        if not self.iso:
            raise SystemExit('[game] iso is not set')
        return self.iso

    def need_ghidra(self) -> Path:
        if not self.ghidra:
            raise SystemExit('[ghidra] home is not set')
        return self.ghidra

    def sym(self, s) -> int:
        s = str(s).strip()
        if s.startswith('@'):
            if s[1:] not in self.symbols:
                raise SystemExit(f'no [symbols] entry named {s[1:]!r}')
            return self.symbols[s[1:]]
        return int(s, 16) if not s.lower().startswith('0x') else int(s, 0)

    def set(self, table: str, key: str, value) -> None:
        self.wrote.append((table, key, value))


def use_cfg(c: FakeCfg) -> FakeCfg:
    for mod in (DISC, E, D, G):
        mod.cfg = lambda required=True, _c=c: _c
    return c


def out_of(fn, *a, **kw) -> tuple[object, str, str]:
    o, e = io.StringIO(), io.StringIO()
    with redirect_stdout(o), redirect_stderr(e):
        rv = fn(*a, **kw)
    return rv, o.getvalue(), e.getvalue()


def raises(exc, fn, *a, **kw) -> Exception:
    try:
        fn(*a, **kw)
    except exc as err:
        return err
    raise AssertionError(f'expected {exc.__name__}')


# --- a synthetic ISO 9660 image ----------------------------------------------

def _dirrec(name: bytes, lba: int, size: int, is_dir: bool) -> bytes:
    rec = bytearray(33 + len(name))
    rec[33:] = name
    rec[32] = len(name)
    struct.pack_into('<I', rec, 2, lba)
    struct.pack_into('>I', rec, 6, lba)
    struct.pack_into('<I', rec, 10, size)
    struct.pack_into('>I', rec, 14, size)
    rec[25] = 0x02 if is_dir else 0x00
    struct.pack_into('<H', rec, 28, 1)
    struct.pack_into('>H', rec, 30, 1)
    if len(rec) % 2:
        rec.append(0)
    rec[0] = len(rec)
    return bytes(rec)


def _raw2352(sectors: list[bytes]) -> bytes:
    """Wrap 2048-byte sectors the way a CD ripper does: 12 sync + 4 header + data + 288 bytes of ECC."""
    head = b'\x00' + b'\xff' * 10 + b'\x00' + bytes([0, 0, 0, 1])
    return b''.join(head + s + bytes(288) for s in sectors)


DISC_FILES = {
    'SYSTEM.CNF': b'BOOT2 = cdrom0:\\SLUS_123.45;1\r\nVER = 1.00\r\nVMODE = NTSC\r\n',
    'SLUS_123.45': b'\x7fELF' + b'boot elf, not a real one' * 200,
    'DATA/GAME.BIN': bytes(range(256)) * 12,
    'DATA/LEVEL1.DAT': b'level data' * 100,
}


def build_iso(path: Path, files=None, label: str = 'SYNTHETIC', stride: int = 2048) -> Path:
    """A minimal but genuine ISO 9660 image: PVD at sector 16, a root directory, one subdirectory."""
    files = DISC_FILES if files is None else files
    subs = sorted({k.split('/')[0] for k in files if '/' in k})
    lba = 18
    root_lba, lba = lba, lba + 1
    sub_lba = {}
    for s in subs:
        sub_lba[s], lba = lba, lba + 1
    data_lba, data = {}, {}
    for k in sorted(files):
        data_lba[k] = lba
        data[k] = files[k]
        lba += max(1, (len(files[k]) + 2047) // 2048)
    total = lba

    def dirsector(entries, self_lba, parent_lba):
        buf = _dirrec(b'\x00', self_lba, 2048, True) + _dirrec(b'\x01', parent_lba, 2048, True)
        for name, l, size, isdir in entries:
            buf += _dirrec(name, l, size, isdir)
        assert len(buf) <= 2048, 'the fixture outgrew one directory sector'
        return buf.ljust(2048, b'\x00')

    root = [(s.encode(), sub_lba[s], 2048, True) for s in subs]
    root += [(k.encode() + b';1', data_lba[k], len(data[k]), False) for k in sorted(files) if '/' not in k]
    sectors = [bytes(2048)] * 16
    pvd = bytearray(2048)
    pvd[0], pvd[1:6], pvd[6] = 1, b'CD001', 1
    pvd[8:40] = b'PLAYSTATION'.ljust(32)
    pvd[40:72] = label.encode().ljust(32)
    struct.pack_into('<I', pvd, 80, total)
    struct.pack_into('>I', pvd, 84, total)
    struct.pack_into('<H', pvd, 128, 2048)
    struct.pack_into('>H', pvd, 130, 2048)
    pvd[156:156 + 34] = _dirrec(b'\x00', root_lba, 2048, True)
    sectors.append(bytes(pvd))
    sectors.append((b'\xff' + b'CD001' + b'\x01').ljust(2048, b'\x00'))
    sectors.append(dirsector(root, root_lba, root_lba))
    for s in subs:
        kids = [(k.split('/')[1].encode() + b';1', data_lba[k], len(data[k]), False)
                for k in sorted(files) if k.startswith(s + '/')]
        sectors.append(dirsector(kids, sub_lba[s], root_lba))
    for k in sorted(files):
        blob = data[k]
        sectors += [blob[i:i + 2048].ljust(2048, b'\x00') for i in range(0, max(len(blob), 1), 2048)]
    blobs = b''.join(sectors) if stride == 2048 else _raw2352(sectors)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blobs)
    return path


_ISOS: dict[tuple, Path] = {}


def iso(stride: int = 2048, **kw) -> Path:
    key = (stride, tuple(sorted(kw.items())))
    if key not in _ISOS:
        name = f'synthetic-{stride}{"-" + "-".join(str(v) for v in kw.values()) if kw else ""}.iso'
        _ISOS[key] = build_iso(WORK / 'iso' / name, stride=stride, **kw)
    return _ISOS[key]


# --- disc.py ------------------------------------------------------------------

@check('a disc path is canonical whatever the caller typed: upper case, / separators, no ;1')
def _():
    assert DISC.Disc.key('\\data\\game.bin;1') == 'DATA/GAME.BIN'
    assert DISC.Disc.key('/SYSTEM.CNF;1') == 'SYSTEM.CNF'
    assert DISC.Disc.key('data/') == 'DATA/'


@check("the boot ELF's name is the serial: SLUS_123.45 -> SLUS-12345")
def _():
    assert DISC.serial_of('cdrom0:\\SLUS_123.45;1') == 'SLUS-12345'
    assert DISC.serial_of('cdrom0:\\SLUS-123.45;1') == 'SLUS-12345'
    assert DISC.serial_of('cdrom0:\\SLUS12345;1') == 'SLUS-12345'
    assert DISC.serial_of('cdrom0:\\BOOT.ELF;1') == '', 'homebrew must read as "no serial", not as a name'


@check('the whole directory tree comes back sorted, with sizes, and directories only when asked')
def _():
    with DISC.Disc(iso()) as d:
        assert d.label == 'SYNTHETIC'
        assert d.stride == 2048 and d.data_off == 0
        paths = [e.path for e in d.list()]
        assert paths == ['DATA/GAME.BIN', 'DATA/LEVEL1.DAT', 'SLUS_123.45', 'SYSTEM.CNF'], paths
        assert [e.path for e in d.list(dirs=True)] == ['DATA'] + paths
        sizes = {e.path: e.size for e in d.list()}
        assert sizes['DATA/GAME.BIN'] == len(DISC_FILES['DATA/GAME.BIN'])


@check('a file reads back byte for byte, by any spelling of its path')
def _():
    with DISC.Disc(iso()) as d:
        assert d.read('SYSTEM.CNF') == DISC_FILES['SYSTEM.CNF']
        assert d.read('data/game.bin') == DISC_FILES['DATA/GAME.BIN']
        assert d.read('\\DATA\\GAME.BIN;1') == DISC_FILES['DATA/GAME.BIN']
        assert raises(DISC.DiscError, d.read, 'NOPE.BIN')


@check('SYSTEM.CNF gives the boot ELF, the serial and the VER line')
def _():
    with DISC.Disc(iso()) as d:
        boot = DISC.system_cnf(d)
    assert boot.elf == 'SLUS_123.45' and boot.serial == 'SLUS-12345'
    assert boot.version == '1.00' and boot.raw['VMODE'] == 'NTSC'


@check('--only takes an exact path, a directory prefix and a glob, and names what matched nothing')
def _():
    with DISC.Disc(iso()) as d:
        assert [e.path for e in d.select(['SYSTEM.CNF'])] == ['SYSTEM.CNF']
        assert [e.path for e in d.select(['data/'])] == ['DATA/GAME.BIN', 'DATA/LEVEL1.DAT']
        assert [e.path for e in d.select(['DATA'])] == ['DATA/GAME.BIN', 'DATA/LEVEL1.DAT']
        assert [e.path for e in d.select(['*.DAT'])] == ['DATA/LEVEL1.DAT']
        assert len(d.select(None)) == 4
        err = raises(DISC.DiscError, d.select, ['SYSTEM.CNF', 'MISSING/*'])
        assert 'MISSING/*' in str(err) and 'SYSTEM.CNF' not in str(err)


@check('extraction writes every file, in its own directory, with its own bytes')
def _():
    c = use_cfg(FakeCfg(WORK / 'extract'))
    c.iso = iso()
    dest = WORK / 'extract' / 'out'
    _, text, _ = out_of(DISC.cmd_extract, ['--out', str(dest)])
    assert (dest / 'DATA' / 'GAME.BIN').read_bytes() == DISC_FILES['DATA/GAME.BIN']
    assert (dest / 'SYSTEM.CNF').read_bytes() == DISC_FILES['SYSTEM.CNF']
    assert 'done: 4 files' in text, text


@check('--list prints the volume, the serial and one row per file, and extracts nothing')
def _():
    c = use_cfg(FakeCfg(WORK / 'listing'))
    c.iso = iso()
    rc, text, _ = out_of(DISC.cmd_extract, ['--list'])
    assert rc == 0
    assert "volume 'SYNTHETIC'" in text and 'SLUS-12345' in text
    assert '4 files' in text and 'DATA/GAME.BIN' in text
    assert not (c.root / 'context' / 'iso').exists(), '--list must not write the extraction directory'


@check('the wrong disc is fatal, says both serials, and only --any-serial gets past it')
def _():
    c = use_cfg(FakeCfg(WORK / 'wrongdisc', serial='SLUS-54321'))
    c.iso = iso()
    rc, _, err = out_of(DISC.cmd_extract, ['--list'])
    assert rc == 1
    assert 'SLUS-12345' in err and 'SLUS-54321' in err and '--any-serial' in err
    rc, text, _ = out_of(DISC.cmd_extract, ['--list', '--any-serial'])
    assert rc == 0 and 'DATA/GAME.BIN' in text


@check('a disc that boots something which is not a serial at all is refused separately')
def _():
    files = dict(DISC_FILES)
    files['SYSTEM.CNF'] = b'BOOT2 = cdrom0:\\BOOT.ELF;1\r\n'
    path = build_iso(WORK / 'iso' / 'homebrew.iso', files=files)
    c = use_cfg(FakeCfg(WORK / 'homebrew'))
    c.iso = path
    rc, _, err = out_of(DISC.cmd_extract, ['--list'])
    assert rc == 1 and 'not a retail serial' in err and 'BOOT.ELF' in err
    rc, _, _ = out_of(DISC.cmd_extract, ['--list', '--any-serial'])
    assert rc == 0


@check('a compressed container is refused by its magic, naming the tool that converts it')
def _():
    for magic, tool in ((b'MComprHD', 'chdman'), (b'CISO', 'maxcso'), (b'PK\x03\x04', 'unzip')):
        p = WORK / 'iso' / f'packed-{magic[:4].hex()}.chd'
        p.write_bytes(magic + bytes(0x10000))
        err = str(raises(DISC.DiscError, DISC.Disc, p))
        assert tool in err and 'does not guess' in err, err


@check('a 2352-byte raw rip is read by finding the stride, not by assuming 2048')
def _():
    with DISC.Disc(iso(stride=2352)) as d:
        assert (d.stride, d.data_off) == (2352, 16)
        assert [e.path for e in d.list()] == ['DATA/GAME.BIN', 'DATA/LEVEL1.DAT',
                                              'SLUS_123.45', 'SYSTEM.CNF']
        assert d.read('DATA/GAME.BIN') == DISC_FILES['DATA/GAME.BIN']
        assert DISC.system_cnf(d).serial == 'SLUS-12345'


@check('a file with no volume descriptor at sector 16 says so instead of reading rubbish')
def _():
    p = WORK / 'iso' / 'garbage.iso'
    p.write_bytes(bytes(0x20000))
    err = str(raises(DISC.DiscError, DISC.Disc, p))
    assert 'no ISO 9660 volume descriptor' in err and '2352/16' in err


# --- elf.py -------------------------------------------------------------------

def code_blob(load: int, size: int) -> bytes:
    """A blob that looks like compiled MIPS: stack prologues, self-calls, lui/addiu address pairs.

    Hand-assembled rather than taken from anywhere: `jal` targets all land inside [load, load+size), which
    is the only reason `_implied_base` can recover the load address.
    """
    n = size // 4
    words = []
    for i in range(n):
        if i % 64 == 0:
            words.append(0x27bdffe0)                                        # addiu $sp, $sp, -32
        elif i % 64 == 63:
            words.append(0x03e00008)                                        # jr $ra
        elif i % 7 == 3:
            t = load if i == 3 else load + (((i * 52) % size) & ~3)         # jal, inside this blob
            words.append((3 << 26) | ((t >> 2) & 0x3ffffff))
        elif i % 11 == 5:
            words.append((0x0f << 26) | (8 << 16) | ((load >> 16) & 0xffff))   # lui $t0, hi
        elif i % 11 == 6:
            words.append((0x09 << 26) | (8 << 21) | (8 << 16) | (load & 0xffff))   # addiu $t0, $t0, lo
        else:
            words.append((0x23 << 26) | (29 << 21) | (2 << 16) | ((i * 4) & 0x7fff))  # lw $v0, N($sp)
    return struct.pack(f'<{n}I', *words)


def noise_blob(size: int, seed: int = 20261015) -> bytes:
    return random.Random(seed).randbytes(size)


@check('an ELF built by the kit reads back: header, one PT_LOAD per piece, entry, machine')
def _():
    path = E.write_elf(WORK / 'elf' / 'three.elf',
                       [(0x100040, b'B' * 0x40), (0x100000, b'A' * 0x40), (0x200000, b'\x11' * 16, 48)],
                       entry=0x100000)
    e = E.Elf(path)
    assert e.type == E.ET_EXEC and e.machine == E.EM_MIPS and e.entry == 0x100000
    assert [s.vaddr for s in e.loads] == [0x100000, 0x100040, 0x200000], 'pieces must come out sorted'
    assert [(s.filesz, s.memsz) for s in e.loads] == [(0x40, 0x40), (0x40, 0x40), (16, 48)]
    assert 'EXEC MIPS' in str(e) and '3 segments' in str(e)


@check('read() crosses a segment boundary, zero-fills the .bss tail, and refuses what is not mapped')
def _():
    e = E.Elf(WORK / 'elf' / 'three.elf')
    assert e.read(0x100020, 0x40) == b'A' * 0x20 + b'B' * 0x20
    assert e.read(0x200000, 48) == b'\x11' * 16 + bytes(32)
    assert e.words(0x100000, 2) == (0x41414141, 0x41414141)
    err = str(raises(E.ElfError, e.read, 0x300000, 4))
    assert 'not mapped' in err and '0x100000-0x100040' in err


@check('write_elf refuses pieces that overlap, and nothing at all')
def _():
    err = str(raises(E.ElfError, E.write_elf, WORK / 'elf' / 'bad.elf',
                     [(0x100000, b'x' * 0x40), (0x100020, b'y' * 4)]))
    assert 'overlap' in err
    assert raises(E.ElfError, E.write_elf, WORK / 'elf' / 'bad.elf', [])


@check('symbols survive the round trip and come back as address -> name')
def _():
    path = E.write_elf(WORK / 'elf' / 'syms.elf', [(0x200000, code_blob(0x200000, 0x400))],
                       symbols={0x200000: 'entry_point', 0x200100: 'f_200100'})
    e = E.Elf(path)
    assert e.functions() == {0x200000: 'entry_point', 0x200100: 'f_200100'}
    assert {s.name for s in e.symbols} == {'entry_point', 'f_200100'}
    assert e.read(0x200000, 0x400) == code_blob(0x200000, 0x400), 'the payload must be untouched'


@check('an IRX is seeded with its entry, its exported functions and its prologues')
def _():
    text = bytearray(code_blob(0, 0x400))
    struct.pack_into('<4I', text, 0x200, E.EXPORT_MAGIC, 0x101, 0, 0)
    text[0x20c:0x214] = b'fakelib\x00'
    struct.pack_into('<3I', text, 0x214, 0x100, 0x180, 0)                   # two exports, then the NULL
    struct.pack_into('<2I', text, 0x300, 0x03e00008, 0x27bdffd0)            # jr $ra; addiu $sp, $sp, -48
    iopmod = struct.pack('<4I', 0, 0x100, 0, len(text)) + b'fake_module\x00'
    body = bytes(text) + iopmod
    shoff = 0x34 + len(body)
    head = bytearray(b'\x7fELF\x01\x01\x01' + bytes(9) + struct.pack('<HHIIIIIHHHHHH',
                     1, E.EM_MIPS, 1, 0, 0, shoff, 0, 52, 0, 0, 40, 4, 0))
    secs = [bytes(40),
            struct.pack('<10I', 0, E.SHT_PROGBITS, E.SHF_ALLOC | E.SHF_EXEC, 0, 0x34, len(text), 0, 0, 4, 0),
            struct.pack('<10I', 0, E.IOPMOD, 0, 0, 0x34 + len(text), len(iopmod), 0, 0, 4, 0),
            struct.pack('<10I', 0, E.SHT_NOBITS, E.SHF_ALLOC, len(text), 0, 0x40, 0, 0, 4, 0)]
    src = WORK / 'elf' / 'module.irx'
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(bytes(head) + body + b''.join(secs))
    _, text_out, _ = out_of(E.irx_elf, src, WORK / 'elf' / 'module.elf')
    e = E.Elf(WORK / 'elf' / 'module.elf')
    fns = e.functions()
    assert e.entry == 0x100 and fns[0x100] == 'fake_module_entry'
    assert fns[0x180] == 'fakelib_01', f'the second export must be named for its table: {fns.get(0x180)!r}'
    assert fns[0x300 + 4] == 'f_00304', 'a prologue after jr $ra is a function start'
    assert any(n.startswith('f_') for n in fns.values()) and "module 'fake_module'" in text_out
    assert e.read(0, 4) == bytes(code_blob(0, 4)), 'the image is the allocated sections at their own addresses'


@check('a blob of real MIPS scores as code and its own jals give away the load address')
def _():
    cand = E.scan_blob('DATA/GAME.BIN', code_blob(0x200000, 0x4000), 0x4000)
    assert cand.valid == 1.0 and cand.jals > 32
    assert cand.prologues > 0.4 and cand.load == 0x200000 and cand.hit == 1.0
    assert cand.good and cand.header_at is None
    assert '[[overlay]]' in cand.block() and 'load = "0x200000"' in cand.block()


@check('a loader header word that equals the load address is noticed and commented')
def _():
    blob = struct.pack('<I', 0x200000) + code_blob(0x200000, 0x4000)[4:]
    cand = E.scan_blob('DATA/HEADER.BIN', blob, 0x4000)
    assert cand.header_at == 0 and cand.load == 0x200000
    assert 'off = 0' in cand.block()


@check('a blob of noise is scored, ranked below the code and never called good')
def _():
    noise = E.scan_blob('MOVIE/NOISE.BIN', noise_blob(0x4000), 0x4000)
    code = E.scan_blob('DATA/GAME.BIN', code_blob(0x200000, 0x4000), 0x4000)
    assert not noise.good, 'random bytes must never be offered as an overlay'
    assert noise.prologues < 0.4 and noise.load != 0x200000
    assert sorted([noise, code], key=lambda x: (x.good, x.hit, x.valid), reverse=True)[0] is code


@check('scan ranks the code blob first and skips media, ELFs and anything too small to judge')
def _():
    root = WORK / 'scanroot'
    (root / 'DATA').mkdir(parents=True, exist_ok=True)
    (root / 'MOVIE').mkdir(parents=True, exist_ok=True)
    (root / 'SOUND').mkdir(parents=True, exist_ok=True)
    (root / 'DATA' / 'GAME.BIN').write_bytes(code_blob(0x200000, 0x8000))
    (root / 'MOVIE' / 'NOISE.BIN').write_bytes(noise_blob(0x8000))
    (root / 'SOUND' / 'BGM.VAG').write_bytes(code_blob(0x300000, 0x8000))
    (root / 'DATA' / 'SMALL.BIN').write_bytes(code_blob(0x200000, 0x1000))
    (root / 'DATA' / 'BOOT.ELF').write_bytes(b'\x7fELF' + code_blob(0x200000, 0x8000))
    use_cfg(FakeCfg(WORK / 'scan'))
    cands = E.scan(root)
    assert cands and cands[0].path == 'DATA/GAME.BIN' and cands[0].load == 0x200000
    assert cands[0].good and not any(c.good for c in cands[1:])
    seen = {c.path for c in cands}
    assert 'SOUND/BGM.VAG' not in seen and 'DATA/BOOT.ELF' not in seen and 'DATA/SMALL.BIN' not in seen


@check('a load address is only claimed when enough jals agree on one')
def _():
    assert E._implied_base([0x200100] * 4, 0x4000) == (None, 0.0), 'fewer than 8 targets proves nothing'
    base, hit = E._implied_base([0x200000 + i * 0x40 for i in range(16)] + [0x900000], 0x1000)
    assert base == 0x200000 and 0.5 < hit < 1.0


# --- a synthetic Ghidra dump --------------------------------------------------

SCORE = '''void update_score(int param_1, float param_2)
{
  int iVar1;
  float fVar2;

  iVar1 = *(int *)(param_1 + 0x10);
  fVar2 = (float)iVar1 * param_2;
  if (fVar2 <= 0.0) {
    fVar2 = 0.0;
  }
  while (iVar1 < 6) {
    *(float *)(param_1 + iVar1 * 4 + 0x20) = fVar2;
    iVar1 = iVar1 + 1;
  }
  *(int *)(param_1 + 0x10) = iVar1;
  return;
}
'''

# The same function for another case: different names, different constants, same shape. This is what
# `similar` has to find, because its Rust is the port of the first one with two numbers changed.
SCORE_TWIN = '''void update_score_doubles(int team, float weight)
{
  int count;
  float scaled;

  count = *(int *)(team + 0x18);
  scaled = (float)count * weight;
  if (scaled <= 0.0) {
    scaled = 0.0;
  }
  while (count < 4) {
    *(float *)(team + count * 4 + 0x30) = scaled;
    count = count + 1;
  }
  *(int *)(team + 0x18) = count;
  return;
}
'''

SHADOW = '''void draw_shadow_mesh(undefined4 *param_1)
{
  switch (*param_1) {
  case 0:
    memset(param_1 + 2, 0, 0x40);
    break;
  case 1:
    memcpy(param_1 + 2, param_1 + 0x12, 0x40);
    break;
  default:
    param_1[1] = 0;
  }
  do {
    param_1[3] = (undefined4)sinf((float)param_1[4] * 0.0174532925);
  } while (param_1[3] != 0);
  return;
}
'''

POISON = 'POISON_MARKER'


def _filler(rng, n: int) -> str:
    """A function body unlike the others, so the corpus has enough shapes for tf-idf to mean anything."""
    stmts = [
        '  iVar{0} = *(int *)(param_1 + {1});',
        '  if (iVar{0} == {1}) {{\n    return;\n  }}',
        '  fVar{0} = sqrtf((float)iVar{0} + {1}.0);',
        '  for (iVar{0} = 0; iVar{0} < {1}; iVar{0} = iVar{0} + 1) {{\n    param_1[iVar{0}] = 0;\n  }}',
        '  do {{\n    iVar{0} = iVar{0} - {1};\n  }} while (iVar{0} != 0);',
        '  memcpy(param_1 + {1}, param_2, {1});',
        '  switch (iVar{0}) {{\n  case {1}:\n    break;\n  default:\n    return;\n  }}',
        '  *(float *)(param_1 + {1}) = (float)iVar{0} * 0.5;',
        '  goto LAB_{1};',
        '  while (*(char *)(param_1 + iVar{0}) != 0) {{\n    iVar{0} = iVar{0} + 1;\n  }}',
    ]
    body = [f'void filler_{n:03d}(int *param_1, void *param_2)', '{', '  int iVar0;', '  float fVar0;', '']
    for _ in range(rng.randint(4, 9)):
        body.append(rng.choice(stmts).format(0, rng.choice((2, 4, 8, 0x10, 0x20, 0x40))))
    body += ['  return;', '}', '']
    return '\n'.join(body)


FUNCS = [(0x200100, 'update_score', SCORE), (0x200300, 'update_score_doubles', SCORE_TWIN),
         (0x200500, 'update_score_ported', SCORE_TWIN.replace('doubles', 'ported')),
         (0x200700, 'draw_shadow_mesh', SHADOW)]

XREFS = [(0x200100, 0x200310, 'update_score_doubles', 'CALL'),
         (0x200100, 0x200720, 'draw_shadow_mesh', 'CALL'),
         (0x200700, 0x200140, 'update_score', 'CALL'),
         (0x1f0000, 0x200160, 'update_score', 'WRITE'),
         (0x1f0004, 0x200180, 'update_score', 'READ')]

R5900_WORDS = [
    ((0x11 << 26) | (0x10 << 21) | (8 << 16) | (6 << 11) | (4 << 6) | 0x1c, f'{"madd.s":<9} $f4, $f6, $f8'),
    ((0x11 << 26) | (0x10 << 21) | (10 << 16) | (12 << 11) | 0x1e, f'{"madda.s":<9} $f12, $f10'),
    ((0x11 << 26) | (0x10 << 21) | (6 << 16) | (4 << 11) | (2 << 6) | 0x28, f'{"max.s":<9} $f2, $f4, $f6'),
    ((0x11 << 26) | (0x10 << 21) | (3 << 16) | (2 << 11) | (1 << 6) | 0x16, f'{"rsqrt.s":<9} $f1, $f2, $f3'),
    ((0x1e << 26) | (29 << 21) | (16 << 16) | 0x20, f'{"lq":<9} $s0, 32($sp)'),
    ((0x1f << 26) | (29 << 21) | (17 << 16) | (0x10000 - 16), f'{"sq":<9} $s1, -16($sp)'),
    ((0x1c << 26) | (4 << 21) | (5 << 16) | (6 << 11) | (0x00 << 6) | 0x08,
     f'{"paddw":<9} $a2, $a0, $a1'),
    ((0x1c << 26) | (4 << 21) | (5 << 16) | (6 << 11) | (0x0e << 6) | 0x09,
     f'{"pcpyld":<9} $a2, $a0, $a1'),
    ((0x1c << 26) | (8 << 11) | 0x30, f'{"pmfhl.lw":<9} $t0'),
    ((0x12 << 26) | (1 << 25) | (0xf << 21) | (8 << 16) | (6 << 11) | (4 << 6) | 0x28,
     f'{"vadd.xyzw":<9} $vf4, $vf6, $vf8'),
    ((0x12 << 26) | (1 << 25) | (8 << 21) | (3 << 16) | (2 << 11) | (1 << 6) | 0x02,
     f'{"vaddz.x":<9} $vf1, $vf2, $vf3z'),
    ((0x12 << 26) | (1 << 21) | (8 << 16) | (2 << 11), f'{"qmfc2":<9} $t0, $vf2'),
]

TABLE = b''.join(struct.pack('<IffI', i + 1, 1.5 + i * 2.0, -2.25 + i, 0x80 + i) for i in range(4))
BLURB = b'ps2kit synthetic string\x00'
MIXED = [(3 << 26) | (0x200100 >> 2), 0x27bdffe0,
         (0x11 << 26) | (0x10 << 21) | (8 << 16) | (6 << 11) | (4 << 6) | 0x1c, 0x00000000]


def dump_cfg() -> FakeCfg:
    """The fake dump, function table, xrefs, journal and ELF — built once, reused by every decomp test."""
    c = use_cfg(FakeCfg(WORK / 'decomp'))
    dump = c.ctx('decomp', 'game.c')
    if dump.exists():
        return c
    rng = random.Random(4242)
    rows, text = [], []
    for addr, name, body in FUNCS:
        rows.append((addr, name, len(body)))
        text.append(f'// {addr:08x} {name} size={len(body)}\n{body}\n')
    for i in range(600):
        addr, body = 0x210000 + i * 0x100, _filler(rng, i)
        rows.append((addr, f'filler_{i:03d}', len(body)))
        text.append(f'// {addr:08x} filler_{i:03d} size={len(body)}\n{body}\n')
    # A trailing region that must never be parsed: reading one function may not touch it.
    text.append(f'// 002f0000 poison size=NOT_A_NUMBER\n{POISON}\n')
    text.append('junk that is not a function at all, and not valid C either: }}} ;;;\n' * 4000)
    dump.write_text(''.join(text), encoding='utf-8')
    c.ctx('decomp', 'game.funcs.tsv').write_text(
        ''.join(f'{a:08x}\t{n}\t{s}\tvoid {n}(void)\n' for a, n, s in rows), encoding='utf-8')
    c.ctx('decomp', 'game.xrefs.tsv').write_text(
        ''.join(f'{to:08x}\t{frm:08x}\t{fn}\t{ty}\n' for to, frm, fn, ty in XREFS), encoding='utf-8')
    j = c.journal / '2026-10-15-scoring'
    j.mkdir(parents=True, exist_ok=True)
    (j / 'README-FINAL.md').write_text('Ported 0x200500 (the doubles case) to Rust.\n', encoding='utf-8')
    E.write_elf(c.ctx('elf', 'game.elf'),
                [(0x200000, struct.pack(f'<{len(R5900_WORDS)}I', *[w for w, _ in R5900_WORDS])),
                 (0x210000, TABLE + BLURB),
                 (0x220000, struct.pack(f'<{len(MIXED)}I', *MIXED))])
    return c


# --- decomp.py ----------------------------------------------------------------

@check('the dump header line is the contract: address, name and size, however it is padded')
def _():
    m = D.HEADER.match('// 00200100 update_score size=339\n')
    assert m and int(m.group(1), 16) == 0x200100 and m.group(2) == 'update_score' and m.group(3) == '339'
    assert D.HEADER.match('// 0x200100 update_score') is None, 'Ghidra writes bare hex, not 0x'
    assert int(D.HEADER.match('// 1004c0\n').group(1), 16) == 0x1004c0


@check('the function table comes back keyed by address, with names and sizes')
def _():
    c = dump_cfg()
    table = D.funcs()
    assert table[0x200100].name == 'update_score' and table[0x200100].size == len(SCORE)
    assert len(table) == len(FUNCS) + 600
    assert D.default_program() == 'game', 'the last program is the one you nearly always meant'
    err = str(raises(D.DumpError, D.dump, 'nosuch'))
    assert 'no program named' in err and 'boot, game' in err
    assert str(table[0x200100]).startswith('00200100  update_score')
    assert raises(D.DumpError, D.funcs, 'boot'), 'a program with no dump must say so, not answer empty'
    _ = c


@check('an address, a name, a case-insensitive name or an @symbol all resolve; an ambiguous one refuses')
def _():
    c = dump_cfg()
    c.symbols['score_entry'] = 0x200100
    assert D.resolve('200100') == 0x200100 and D.resolve('0x200100') == 0x200100
    assert D.resolve('update_score') == 0x200100
    assert D.resolve('UPDATE_SCORE') == 0x200100
    assert D.resolve('@score_entry') == 0x200100
    assert D.resolve('shadow_mesh') == 0x200700, 'a unique substring is enough'
    err = str(raises(D.DumpError, D.resolve, 'update_score_'))
    assert 'matches 2 functions' in err and 'update_score_doubles' in err
    assert 'no function called' in str(raises(D.DumpError, D.resolve, 'no_such_function'))


@check('one function comes out whole, by address or by name, and only that function')
def _():
    dump_cfg()
    body = D.func(0x200100)
    assert body.startswith('// 00200100 update_score size=')
    assert body.endswith('}\n\n') and 'update_score_doubles' not in body
    assert body == D.func('update_score')
    assert 'return;' in body and body.count('{') == body.count('}')


@check('a function is streamed out of the dump: the trailing region is never read, let alone parsed')
def _():
    c = dump_cfg()
    size = c.ctx('decomp', 'game.c').stat().st_size
    assert size > 400_000, 'the fixture has to be big enough for "streamed" to mean something'
    read = [0]
    real_open = open

    class Counted:
        def __init__(self, fh):
            self.fh = fh

        def __iter__(self):
            for line in self.fh:
                read[0] += len(line)
                yield line

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.fh.close()

        def __getattr__(self, name):
            return getattr(self.fh, name)

    D.open = lambda *a, **kw: Counted(real_open(*a, **kw))
    try:
        body = D.func(0x200100)
    finally:
        del D.open
    assert POISON not in body
    assert read[0] < size // 10, f'read {read[0]} of {size} bytes to print the first function'


@check('an address inside a function says which function it is inside')
def _():
    dump_cfg()
    err = str(raises(D.DumpError, D.func, 0x200140))
    assert 'no function starts at 0x200140' in err and 'inside update_score at 0x200100' in err


@check('xrefs split into callers and the references made from inside the body, classified')
def _():
    dump_cfg()
    got = D.xrefs(0x200100)
    assert sorted(r.frm for r in got['in']) == [0x200310, 0x200720]
    assert all(r.kind == 'call' for r in got['in'])
    assert sorted((r.to, r.kind) for r in got['out']) == [(0x1f0000, 'write'), (0x1f0004, 'read'),
                                                          (0x200700, 'call')]
    assert D.xrefs('update_score') == got


@check('look-alike functions rank by shape, and a sibling already ported names its journal entry')
def _():
    dump_cfg()
    hits = D.similar(0x200100, n=6)
    top = {f.addr: (score, journal) for score, f, journal in hits[:2]}
    assert set(top) == {0x200300, 0x200500}, 'the two same-shape siblings must come first'
    assert all(score > 0.99 for score, _ in top.values())
    assert 'README-FINAL.md' in top[0x200500][1], 'the ported sibling carries its journal entry'
    assert top[0x200300][1] == '', 'the unported one carries nothing'
    rest = {f.addr: score for score, f, _ in hits[2:]}
    assert all(s < 0.6 for s in rest.values()), rest
    by_addr = {f.addr: score for score, f, _ in D.similar(0x200100, n=800)}
    assert by_addr[0x200700] < 0.1, 'a differently shaped function is a coincidence, not a sibling'


@check('a function the dump does not have refuses by address rather than guessing')
def _():
    dump_cfg()
    assert 'no function starts at 0x999000' in str(raises(D.DumpError, D.similar, 0x999000))


@check('the R5900-only instructions decode here, with no external disassembler involved')
def _():
    dump_cfg()
    insns, tool = D.disasm(0x200000, 0x200000 + 4 * len(R5900_WORDS))
    assert tool is None, 'a range the kit can decode itself must not shell out'
    assert [i.text for i in insns] == [t for _, t in R5900_WORDS]
    assert [i.word for i in insns] == [w for w, _ in R5900_WORDS]
    assert str(insns[0]).startswith('00200000  ') and 'madd.s' in str(insns[0])
    for word, text in R5900_WORDS:
        assert D.r5900(word) == text
    assert D.r5900(0x27bdffe0) is None, 'an ordinary MIPS word is not this module\'s business'


@check('an ordinary MIPS word falls back, and jump targets are worked out and named either way')
def _():
    dump_cfg()
    insns, tool = D.disasm(0x220000, 0x220000 + 4 * len(MIXED))
    assert insns[0].note == '-> 0x200100 update_score'
    assert insns[2].text == f'{"madd.s":<9} $f4, $f6, $f8', 'the R5900 word still comes from our table'
    assert insns[1].text.startswith('.word') or 'addiu' in insns[1].text
    assert tool is None or isinstance(tool, str)


@check('dis refuses an empty range and an address the ELF does not map')
def _():
    dump_cfg()
    assert 'empty range' in str(raises(D.DumpError, D.disasm, 0x200010, 0x200000))
    assert 'not mapped' in str(raises(D.DumpError, D.disasm, 0x900000, 0x900010))


@check("a data range prints offsets from its start, so a table's stride is visible")
def _():
    dump_cfg()
    rows = D.data(0x210000, 0x40, 'u32')
    assert rows[0].startswith('+0x0000  00210000  ') and rows[1].startswith('+0x0010  00210010  ')
    assert len(rows) == 4 and len(rows[0].split()[2:]) == 4
    floats = D.data(0x210000, 0x40, 'f32')
    assert '1.5' in floats[0] and '-2.25' in floats[0]
    assert '3.5' in floats[1] and '-1.25' in floats[1]
    hexed = D.data(0x210000, 0x10, 'hex')
    assert hexed[0].endswith('|')
    assert '01 00 00 00' in hexed[0]
    assert '--as must be one of' in str(raises(D.DumpError, D.data, 0x210000, 4, 'u24'))


@check('strings finds printable runs by address, matched as a substring or a regex')
def _():
    dump_cfg()
    at = 0x210000 + len(TABLE)
    assert (at, 'ps2kit synthetic string') in D.strings('synthetic')
    assert (at, 'ps2kit synthetic string') in D.strings(r'ps2kit\s+\w+')
    assert D.strings('synthetic', min_len=400) == [], 'min_len is a floor on the run, not on the match'
    assert D.strings('nothing matches this') == []


@check('kit fn prints the function with its table row, and --context adds the callers\' call sites')
def _():
    dump_cfg()
    rc, text, _ = out_of(D.cmd_fn, ['update_score'])
    assert rc == 0 and '# game  00200100  update_score' in text and 'while (iVar1 < 6)' in text
    rc, text, _ = out_of(D.cmd_fn, ['200100', '--context', '2'])
    assert rc == 0 and '2 caller(s)' in text
    assert 'update_score_doubles' in text and 'draw_shadow_mesh' in text
    rc, _, err = out_of(D.cmd_fn, ['no_such_function'])
    assert rc == 1 and 'kit fn: no function called' in err


@check('kit xref groups incoming from outgoing, and --similar reports scores and ported siblings')
def _():
    dump_cfg()
    rc, text, _ = out_of(D.cmd_xref, ['update_score'])
    assert rc == 0 and 'called by' in text and 'calls' in text and 'touches' in text
    assert '00200310' in text and '001f0000' in text
    rc, text, _ = out_of(D.cmd_xref, ['200100', '--similar'])
    assert rc == 0 and 'shaped like' in text
    assert '1.00  00200500  update_score_ported' in text and 'ported: ' in text
    assert 'already ported' in text
    rc, text, _ = out_of(D.cmd_xref, ['filler_001'])
    assert rc == 0 and 'no references' in text


@check('kit dis takes a length or an end address and says which decoder produced the lines')
def _():
    dump_cfg()
    rc, text, _ = out_of(D.cmd_dis, ['200000', '--len', '0x30'])
    assert rc == 0 and 'R5900 ops only' in text and 'vadd.xyzw' in text
    assert text.count('\n') == 13, '12 instructions and one header line'
    rc, other, _ = out_of(D.cmd_dis, ['200000', '200030'])
    assert other == text, 'an end address and a length must mean the same thing'


@check('kit data reads a region as ints or floats, and --strings searches the image')
def _():
    dump_cfg()
    rc, text, _ = out_of(D.cmd_data, ['210000', '0x20', '--as', 'f32'])
    assert rc == 0 and '# game  00210000  32 bytes as f32' in text and '1.5' in text
    rc, text, _ = out_of(D.cmd_data, ['--strings', 'synthetic'])
    assert rc == 0 and '1 strings' in text and 'ps2kit synthetic string' in text
    rc, _, err = out_of(D.cmd_data, ['900000', '0x10'])
    assert rc == 1 and 'kit data: ' in err and 'not mapped' in err


# --- ghidra.py ----------------------------------------------------------------

def ghidra_home(root: Path, with_extension: bool = True) -> Path:
    home = root / 'ghidra'
    (home / 'support').mkdir(parents=True, exist_ok=True)
    (home / 'support' / 'analyzeHeadless').write_text('#!/bin/sh\nexit 0\n')
    if with_extension:
        langs = home / 'Ghidra' / 'Extensions' / 'unrelated-directory-name' / 'data' / 'languages'
        langs.mkdir(parents=True, exist_ok=True)
        (langs / 'ee.ldefs').write_text('<language_definitions><language processor="r5900"/>'
                                        '</language_definitions>')
    return home


@check('the r5900 processor is found by its language definition, not by a directory name')
def _():
    home = ghidra_home(WORK / 'ghidra-ok')
    ext = G.ee_extension(home)
    assert ext == home / 'Ghidra' / 'Extensions' / 'unrelated-directory-name', ext


@check('a Ghidra with no r5900 refuses before spending twenty minutes, and says where to unzip it')
def _():
    home = ghidra_home(WORK / 'ghidra-bare', with_extension=False)
    empty = WORK / 'empty-home'
    empty.mkdir(exist_ok=True)
    env = {k: os.environ.get(k) for k in ('HOME', 'XDG_CONFIG_HOME')}
    os.environ['HOME'] = os.environ['XDG_CONFIG_HOME'] = str(empty)
    try:
        assert G.ee_extension(home) is None
        c = use_cfg(FakeCfg(WORK / 'ghidra-bare'))
        c.ghidra = home
        err = str(raises(G.GhidraError, G.need_extension))
        assert 'no r5900 processor' in err and str(home / 'Ghidra' / 'Extensions') in err
        assert G.EXTENSION_URL in err and 'mis-decodes' in err
    finally:
        for k, v in env.items():
            os.environ[k] = v if v is not None else ''


@check('analyse imports one program with the EE processor and the configured timeout')
def _():
    c = use_cfg(FakeCfg(WORK / 'ghidra-run'))
    c.ghidra = ghidra_home(WORK / 'ghidra-run')
    calls = []
    G._run = lambda args, log, what, timeout, quiet=False: calls.append((args, what, timeout, quiet)) or 0
    elf = E.write_elf(c.ctx('elf', 'game.elf'), [(0x200000, code_blob(0x200000, 0x2000))])
    assert G.analyse('game', elf) == 0
    args, what, timeout, _ = calls[0]
    assert args[0] == str(c.ghidra / 'support' / 'analyzeHeadless')
    assert args[1:3] == [str(c.ghidra_project), 'synth']
    assert args[args.index('-processor') + 1] == G.EE
    assert args[args.index('-import') + 1] == str(elf)
    assert args[args.index('-analysisTimeoutPerFile') + 1] == '600' and timeout == 600
    assert args[args.index('-scriptPath') + 1] == str(G.SCRIPTS)
    assert '-overwrite' not in args and 'tens of minutes' in what
    assert c.ghidra_project.is_dir(), 'the project directory is created, not assumed'
    calls.clear()
    G.analyse('game', elf, processor=G.IOP, force=True, timeout=30)
    assert calls[0][0][calls[0][0].index('-processor') + 1] == G.IOP, 'an IOP module is a plain R3000'
    assert '-overwrite' in calls[0][0] and calls[0][2] == 30
    assert 'no ELF for' in str(raises(G.GhidraError, G.analyse, 'game', WORK / 'nope.elf'))


@check('every dump runs -noanalysis -readOnly, once per program: without them it re-analyses')
def _():
    c = use_cfg(FakeCfg(WORK / 'ghidra-run'))
    c.ghidra = ghidra_home(WORK / 'ghidra-run')
    c.ghidra_project.mkdir(parents=True, exist_ok=True)
    calls = []
    G._run = lambda args, log, what, timeout, quiet=False: calls.append(args) or 0
    assert G.postscript('DumpDecomp', ['--out', 'x.c'], quiet=True) == 0
    assert len(calls) == 2, 'one run per program in kit.toml'
    for args, program in zip(calls, c.programs):
        assert '-noanalysis' in args and '-readOnly' in args
        assert args[args.index('-process') + 1] == f'{program}.elf'
        assert args[args.index('-postScript') + 1] == 'DumpDecomp.java'
        assert args[-2:] == ['--out', 'x.c']
    calls.clear()
    assert G.postscript('DumpXrefs', [], programs=['game']) == 0 and len(calls) == 1
    err = str(raises(G.GhidraError, G.postscript, 'NoSuchScript', []))
    assert 'no such script' in err and 'DumpDecomp' in err, err


@check('a dump against a project that was never analysed says so instead of starting one')
def _():
    c = use_cfg(FakeCfg(WORK / 'ghidra-fresh'))
    c.ghidra = ghidra_home(WORK / 'ghidra-fresh')
    err = str(raises(G.GhidraError, G.postscript, 'DumpDecomp', []))
    assert 'no Ghidra project' in err and 'ghidra analyse' in err


@check("the project's programs are read from its own index, with the .prp files as the fallback")
def _():
    c = use_cfg(FakeCfg(WORK / 'ghidra-list'))
    rep = c.ghidra_project / 'synth.rep'
    rep.mkdir(parents=True, exist_ok=True)
    assert G.project_programs() is None, 'an empty project is unknown, not empty'
    (rep / '~index.dat').write_text('1:boot.elf:\n2:game.elf:\n', encoding='utf-8')
    assert G.project_programs() == ['boot.elf', 'game.elf']
    (rep / '~index.dat').unlink()
    (rep / 'idata').mkdir(exist_ok=True)
    (rep / 'idata' / '00000000.prp').write_text('NAME=game.elf\n', encoding='utf-8')
    assert G.project_programs() == ['game.elf']


if __name__ == '__main__':
    print(f'ps2kit static-layer tests  (python {sys.version.split()[0]})')
    for name, fn in TESTS:
        try:
            fn()
        except Exception as e:                   # noqa: BLE001 - a test report, not a library
            FAIL.append((name, e))
            print(f'FAIL {name}: {e.__class__.__name__}: {e}')
            if os.environ.get('V'):
                import traceback
                traceback.print_exc(limit=4)
        else:
            PASS.append(name)
            print(f'ok   {name}')
    shutil.rmtree(WORK, ignore_errors=True)
    print()
    print(f'{len(PASS)} passed, {len(FAIL)} failed')
    for name, e in FAIL:
        print(f'  FAIL {name}: {e}')
    sys.exit(1 if FAIL else 0)
