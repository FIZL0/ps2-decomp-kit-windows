"""PS2 ELFs: read the disc's boot ELF, and *build* one per overlay so Ghidra can see the real addresses.

Why this exists. A PS2 game is almost never one program. The boot ELF (`SLUS_200.00`) is a loader and the
shared engine; the code you actually care about arrives at runtime as an overlay — a headerless blob the
game DMAs over a fixed address (`DATA/GAME.BIN` → 0x200000, a menu overlay and a movie player over the
same region). Hand that blob to Ghidra as a flat binary and it loads at 0 and every `jal`, every pointer
and every jump table points into nothing: the decompilation is unreadable. Hand it over as an ELF whose
one PT_LOAD segment sits at the address the loader uses, together with the boot ELF's own segments, and
Ghidra resolves the cross-overlay calls, the string literals and the globals, and the decompilation reads
like C. That one trick is the difference between a usable project and a wall of `*(int *)0x1234`.

The other half of the problem is *finding* the overlays, which in the reference project took a human
reading the loader. `kit elf --scan` does most of it: a raw MIPS blob is easy to recognise (high density
of valid opcodes, one `addiu $sp, $sp, -N` prologue every few hundred bytes), and because a `jal`'s target
is an absolute address, the targets of a blob's own calls cluster inside the address range it is loaded at.
Pick the base that captures the most of them and you have the load address, which is the only thing
`[[overlay]]` really needs.

`irx_elf` covers the IOP side: `.IRX` modules are R3000 code linked at 0 and relocated at load, so at base
0 the image is just its allocated sections in place. Ghidra has no seeds in a section-less blob, so we
hand it function starts from the module's export table plus the obvious prologues.
"""
from __future__ import annotations

import mmap
import os
import re
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

from ps2kit.config import cfg, ROOT

ET_EXEC, EM_MIPS = 2, 8
PT_LOAD, PT_NULL = 1, 0
SHT_PROGBITS, SHT_SYMTAB, SHT_STRTAB, SHT_NOBITS = 1, 2, 3, 8
SHF_WRITE, SHF_ALLOC, SHF_EXEC = 0x1, 0x2, 0x4
SHN_ABS = 0xfff1
PAGE = 0x1000

# EE RAM, and the same window seen through kseg0/kseg1. An overlay load address outside these is a bug in
# whatever guessed it.
RAM_WINDOWS = ((0x00080000, 0x02000000), (0x80080000, 0x82000000), (0xa0080000, 0xa2000000))


def in_ram(addr: int) -> bool:
    return any(lo <= addr < hi for lo, hi in RAM_WINDOWS)


# --- reading ------------------------------------------------------------------

@dataclass(frozen=True)
class Seg:
    type: int
    off: int
    vaddr: int
    filesz: int
    memsz: int
    flags: int

    @property
    def end(self) -> int:
        return self.vaddr + self.memsz

    def __str__(self) -> str:
        rwx = ''.join(c if self.flags & b else '-' for c, b in (('r', 4), ('w', 2), ('x', 1)))
        kind = {PT_LOAD: 'LOAD', PT_NULL: 'NULL'}.get(self.type, f'0x{self.type:x}')
        return (f'{kind:5} {self.vaddr:#010x}-{self.end:#010x}  file +{self.off:#08x} {self.filesz:#9x}'
                f'  bss {self.memsz - self.filesz:#7x}  {rwx}')


@dataclass(frozen=True)
class Sec:
    name: str
    type: int
    flags: int
    addr: int
    off: int
    size: int
    link: int
    entsize: int


@dataclass(frozen=True)
class Sym:
    name: str
    value: int
    size: int
    kind: int          # STT_*: 2 = FUNC, 1 = OBJECT
    shndx: int


class ElfError(Exception):
    pass


class Elf:
    """A 32-bit little-endian MIPS ELF, read by virtual address.

        e = Elf('context/elf/game.elf')
        e.entry, e.segments, e.sections, e.symbols
        e.segment_for(0x200000)
        e.read(0x200000, 0x40)        # bytes, bss zero-filled

    `read` is what makes `kit data` and `kit dis` work: everything downstream asks for an address,
    never a file offset.
    """

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        if not self.path.exists():
            raise ElfError(f'no such ELF: {self.path}')
        f = open(self.path, 'rb')
        try:
            self._mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        except (ValueError, OSError):            # empty file, or a filesystem that will not mmap
            self._mm = f.read()
        finally:
            f.close()
        d = self._mm
        if len(d) < 52 or d[:4] != b'\x7fELF':
            raise ElfError(f'{self.path.name} is not an ELF (magic {bytes(d[:4])!r})')
        if d[4] != 1 or d[5] != 1:
            raise ElfError(f'{self.path.name} is not 32-bit little-endian — the PS2 is both')
        (self.type, self.machine, _ver, self.entry, phoff, shoff, self.eflags, _ehsize,
         phentsize, phnum, shentsize, shnum, shstrndx) = struct.unpack_from('<HHIIIIIHHHHHH', d, 16)
        # Program header fields in order: type, offset, vaddr, paddr, filesz, memsz, flags, align.
        self.segments = [Seg(t, off, va, fsz, msz, fl) for (t, off, va, _pa, fsz, msz, fl) in
                         (struct.unpack_from('<7I', d, phoff + i * phentsize) for i in range(phnum))] \
            if phoff else []
        self.sections: list[Sec] = []
        if shoff and shnum:
            raws = [struct.unpack_from('<10I', d, shoff + i * shentsize) for i in range(shnum)]
            strtab_off = raws[shstrndx][4] if shstrndx < shnum else 0
            for (nm, ty, fl, ad, of, sz, lk, _info, _al, ent) in raws:
                self.sections.append(Sec(_cstr(d, strtab_off + nm) if strtab_off else '', ty, fl, ad, of,
                                         sz, lk, ent))
        self.loads = [s for s in self.segments if s.type == PT_LOAD and s.memsz]
        self._syms: list[Sym] | None = None

    # --- symbols ---------------------------------------------------------------

    @property
    def symbols(self) -> list[Sym]:
        if getattr(self, '_syms', None) is None:
            out = []
            for sec in self.sections:
                if sec.type != SHT_SYMTAB or not sec.entsize:
                    continue
                strs = self.sections[sec.link].off if sec.link < len(self.sections) else 0
                for i in range(sec.size // sec.entsize):
                    nm, val, size, info, _oth, shndx = struct.unpack_from('<IIIBBH', self._mm,
                                                                          sec.off + i * sec.entsize)
                    name = _cstr(self._mm, strs + nm) if strs else ''
                    if name:
                        out.append(Sym(name, val, size, info & 0xf, shndx))
            self._syms = out
        return self._syms

    def functions(self) -> dict[int, str]:
        """address → name, for the FUNC symbols. Empty for a stripped retail ELF, which is the norm."""
        return {s.value: s.name for s in self.symbols if s.kind == 2 and s.value}

    # --- by address ------------------------------------------------------------

    def segment_for(self, addr: int) -> Seg | None:
        for s in self.loads:
            if s.vaddr <= addr < s.end:
                return s
        return None

    def section_for(self, addr: int) -> Sec | None:
        for s in self.sections:
            if s.flags & SHF_ALLOC and s.addr and s.addr <= addr < s.addr + s.size:
                return s
        return None

    def read(self, addr: int, n: int) -> bytes:
        """`n` bytes from virtual address `addr`. .bss reads as zeroes; a read off the end of the mapped
        range is an error rather than silent zeroes, because a silently-zero table looks like real data."""
        out = bytearray()
        while n > 0:
            seg = self.segment_for(addr)
            if seg is None:
                sec = self.section_for(addr)
                if sec is None:
                    raise ElfError(f'{addr:#x} is not mapped by {self.path.name}'
                                   + (f' (mapped: ' + ', '.join(f'{s.vaddr:#x}-{s.end:#x}' for s in self.loads)
                                      + ')' if self.loads else ''))
                seg = Seg(PT_LOAD, sec.off, sec.addr, 0 if sec.type == SHT_NOBITS else sec.size, sec.size, 4)
            take = min(n, seg.end - addr)
            have = max(0, min(take, seg.vaddr + seg.filesz - addr))
            if have:
                start = seg.off + (addr - seg.vaddr)
                out += self._mm[start:start + have]
            out += bytes(take - have)                     # the .bss tail of this segment
            addr += take
            n -= take
        return bytes(out)

    def words(self, addr: int, count: int) -> tuple[int, ...]:
        return struct.unpack(f'<{count}I', self.read(addr, count * 4))

    def __str__(self) -> str:
        kind = {1: 'REL', 2: 'EXEC', 3: 'DYN'}.get(self.type, str(self.type))
        mach = 'MIPS' if self.machine == EM_MIPS else f'machine {self.machine}'
        return f'{self.path.name}: {kind} {mach} entry {self.entry:#x}, {len(self.segments)} segments, ' \
               f'{len(self.sections)} sections, {len(self.symbols)} symbols'


def _cstr(d, off: int) -> str:
    end = d.find(b'\0', off)
    return bytes(d[off:end if end >= 0 else off]).decode('latin-1')


# --- writing ------------------------------------------------------------------

def write_elf(out: str | os.PathLike, pieces, entry: int = 0, machine: int = EM_MIPS,
              symbols: dict[int, str] | None = None, eflags: int = 0) -> Path:
    """Build a minimal ET_EXEC ELF out of (vaddr, bytes) blobs — one PT_LOAD each.

    `pieces` items are `(vaddr, data)` or `(vaddr, data, memsz)` (memsz > len(data) gives the piece a .bss
    tail). `symbols` is address → name; when given, real sections are emitted too, since Ghidra needs a
    section index to attach a symbol to.

    The trap worth naming: the reference implementation did this by *editing* the boot ELF in place and had
    to blank the section headers, because the original sections described segments it had just nulled out.
    Building a fresh ELF avoids the whole class of problem, so nothing here reuses the input's headers.
    """
    blobs = []
    for p in pieces:
        va, data = p[0], bytes(p[1])
        memsz = p[2] if len(p) > 2 else len(data)
        if data or memsz:
            blobs.append((va, data, max(memsz, len(data))))
    if not blobs:
        raise ElfError('write_elf: nothing to write')
    blobs.sort(key=lambda b: b[0])
    for (a, d, m), (b, _, _) in zip(blobs, blobs[1:]):
        if a + m > b:
            raise ElfError(f'write_elf: pieces overlap: {a:#x}+{m:#x} runs into {b:#x}')

    nsec = (1 + len(blobs) + 3) if symbols else 0          # null + one per blob + symtab/strtab/shstrtab
    ehsize, phentsize, shentsize = 52, 32, 40
    off = ehsize + phentsize * len(blobs)
    laid = []
    for va, data, memsz in blobs:
        off = (off + 15) & ~15
        laid.append((va, data, memsz, off))
        off += len(data)

    sym_blob = strtab = shstr = b''
    sym_off = str_off = shs_off = sh_off = 0
    if symbols:
        names, strtab = [], b'\0'
        for addr in sorted(symbols):
            names.append((addr, len(strtab)))
            strtab += symbols[addr].encode('latin-1') + b'\0'
        sym_blob = bytes(16)                               # index 0 is always the null symbol
        for addr, noff in names:
            shndx = next((i + 1 for i, (va, _d, m, _o) in enumerate(laid) if va <= addr < va + m), SHN_ABS)
            sym_blob += struct.pack('<IIIBBH', noff, addr, 0, 0x12, 0, shndx)  # GLOBAL | FUNC
        shstr = b'\0.symtab\0.strtab\0.shstrtab\0' + b''.join(b'.seg%d\0' % i for i in range(len(laid)))
        sym_off = (off + 3) & ~3
        str_off = sym_off + len(sym_blob)
        shs_off = str_off + len(strtab)
        sh_off = (shs_off + len(shstr) + 3) & ~3
        off = sh_off + shentsize * nsec

    buf = bytearray(off)
    struct.pack_into('<16sHHIIIIIHHHHHH', buf, 0, b'\x7fELF\x01\x01\x01' + bytes(9), ET_EXEC, machine, 1,
                     entry or laid[0][0], ehsize, sh_off, eflags, ehsize, phentsize, len(laid),
                     shentsize, nsec, (nsec - 1) if symbols else 0)
    for i, (va, data, memsz, doff) in enumerate(laid):
        struct.pack_into('<7I', buf, ehsize + i * phentsize, PT_LOAD, doff, va, va, len(data), memsz, 7)
        buf[doff:doff + len(data)] = data
    if symbols:
        buf[sym_off:sym_off + len(sym_blob)] = sym_blob
        buf[str_off:str_off + len(strtab)] = strtab
        buf[shs_off:shs_off + len(shstr)] = shstr
        name_at = lambda s: shstr.find(s.encode() + b'\0')
        rows = [bytes(40)]
        for i, (va, data, memsz, doff) in enumerate(laid):
            ty = SHT_NOBITS if not data else SHT_PROGBITS
            rows.append(struct.pack('<10I', name_at(f'.seg{i}'), ty, SHF_ALLOC | SHF_EXEC | SHF_WRITE,
                                    va, doff, memsz, 0, 0, 16, 0))
        rows.append(struct.pack('<10I', name_at('.symtab'), SHT_SYMTAB, 0, 0, sym_off, len(sym_blob),
                                nsec - 2, 1, 4, 16))
        rows.append(struct.pack('<10I', name_at('.strtab'), SHT_STRTAB, 0, 0, str_off, len(strtab), 0, 0, 1, 0))
        rows.append(struct.pack('<10I', name_at('.shstrtab'), SHT_STRTAB, 0, 0, shs_off, len(shstr), 0, 0, 1, 0))
        buf[sh_off:sh_off + shentsize * nsec] = b''.join(rows)

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(bytes(buf))
    return out


# --- IOP modules --------------------------------------------------------------

IOPMOD = 0x70000080          # SHT_LOPROC + 0x80: the .iopmod section with the module's name, entry and gp
EXPORT_MAGIC = 0x41c00000    # struct irx_export_table { u32 magic; u32 ver; u32 flags; char name[8]; fn*[] }


def irx_elf(src: str | os.PathLike, out: str | os.PathLike) -> Path:
    """`.IRX` (IOP module, R3000) → a plain MIPS ELF with function-start symbols, for its own Ghidra project.

    An IRX is an ELF whose sections are linked at address 0 and relocated when the IOP loader places it. At
    base 0 the relocations add nothing, so the image is simply the allocated sections at their own
    addresses. What is missing is seeds: with no symbols, Ghidra's analyser finds very little in a flat
    blob. So we seed it with
      * the module entry from `.iopmod`,
      * every function the module exports (its `irx_export_table`, which is how other modules call in), and
      * every `jal` target and every `addiu $sp, $sp, -N` that follows a `jr $ra` — i.e. the prologues.
    """
    src, out = Path(src), Path(out)
    d = src.read_bytes()
    if d[:4] != b'\x7fELF':
        raise ElfError(f'{src.name} is not an ELF — a .IRX always is')
    shoff, = struct.unpack_from('<I', d, 0x20)
    shnum, = struct.unpack_from('<H', d, 0x30)
    shentsize, = struct.unpack_from('<H', d, 0x2e)
    img = bytearray()
    entry, text_end, modname = 0, 0, ''
    for i in range(shnum):
        _nm, ty, fl, addr, off, size = struct.unpack_from('<6I', d, shoff + i * (shentsize or 40))
        if ty == IOPMOD:
            entry, = struct.unpack_from('<I', d, off + 4)
            modname = _cstr(d, off + 16)
        if not fl & SHF_ALLOC:
            continue
        if len(img) < addr + size:
            img.extend(bytes(addr + size - len(img)))
        if ty != SHT_NOBITS:
            img[addr:addr + size] = d[off:off + size]
        if fl & SHF_EXEC:
            text_end = max(text_end, addr + size)
    if not text_end:
        raise ElfError(f'{src.name} has no executable section')

    words = struct.unpack_from(f'<{text_end // 4}I', bytes(img))
    syms: dict[int, str] = {}
    if entry:
        syms[entry] = f'{modname or src.stem}_entry'
    for lib, fns in _irx_exports(img, words, text_end):
        for n, addr in fns:
            syms.setdefault(addr, f'{lib}_{n:02d}')
    for i, w in enumerate(words):
        if w >> 26 == 3:                                    # jal: target is absolute in this 256 MB segment
            t = (w & 0x3ffffff) << 2
            if t < text_end:
                syms.setdefault(t, f'f_{t:05x}')
        if (w & 0xffff8000) == 0x27bd8000 and (                # addiu $sp, $sp, -N right after a jr $ra
                (i >= 1 and words[i - 1] == 0x03e00008) or (i >= 2 and words[i - 2] == 0x03e00008)):
            syms.setdefault(i * 4, f'f_{i * 4:05x}')

    write_elf(out, [(0, bytes(img))], entry=entry, symbols=syms)
    print(f'{out.name}: {len(img):#x} bytes, entry {entry:#x}, {len(syms)} function symbols'
          + (f', module {modname!r}' if modname else ''))
    print(f'  kit ghidra analyse --program {out.stem}   # IOP is R3000: MIPS:LE:32:default, not r5900')
    return out


def _irx_exports(img: bytes, words, text_end: int):
    """Find `irx_export_table`s: magic 0x41c00000, an 8-byte library name, then a NULL-terminated fn array.

    This is how one IOP module calls another, so the exports are exactly the entry points worth naming. The
    layout is from the IOP SDK's `irx.h`; we only accept a table whose pointers all land inside .text, which
    is enough to keep a random 0x41c00000 in a data blob from inventing symbols.
    """
    out = []
    for i, w in enumerate(words):
        if w != EXPORT_MAGIC or i * 4 + 20 > text_end:
            continue
        name = bytes(img[i * 4 + 12:i * 4 + 20]).split(b'\0')[0].decode('latin-1', 'replace')
        if not re.fullmatch(r'[\x20-\x7e]{1,8}', name):
            continue
        fns, j = [], i + 5
        while j < len(words) and words[j]:
            if not (0 < words[j] < text_end) or words[j] & 3:
                fns = []
                break
            fns.append((len(fns), words[j]))
            j += 1
        if fns:
            out.append((re.sub(r'\W', '_', name.strip()) or f'lib{i:x}', fns))
    return out


# --- guessing the overlays ----------------------------------------------------

# Primary opcodes (bits 31..26) that appear in real R5900 code. Not a legality check — a density score.
VALID_OP = set(range(0, 19)) | set(range(20, 29)) | {30, 31} | set(range(32, 48)) | \
           {49, 50, 54, 55, 57, 58, 62, 63}
SPECIAL_FUNCT = set(range(0, 4)) | set(range(4, 8)) | {8, 9, 10, 11, 12, 13, 15} | set(range(16, 20)) | \
                set(range(20, 28)) | set(range(32, 48)) | set(range(42, 48)) | {56, 58, 59, 60, 62, 63}
SCAN_BYTES = 2 << 20          # enough of a file to judge it; overlays put code first
MEDIA_EXT = {'.PSS', '.PSS2', '.STR', '.VAG', '.ADS', '.ADP', '.IPU', '.SFX', '.WAV', '.PNG', '.TM2',
             '.TIM2', '.AVI', '.M2V', '.SND', '.XA', '.MPG'}


@dataclass
class Candidate:
    path: str
    size: int
    valid: float          # fraction of words whose opcode looks like MIPS
    prologues: float      # `addiu $sp, $sp, -N` per KB
    jals: int
    load: int | None
    hit: float            # fraction of jal targets inside [load, load+size)
    header_at: int | None # a word early in the file that equals `load` — a loader header naming itself

    @property
    def good(self) -> bool:
        return (self.valid > 0.80 and self.prologues > 0.4 and self.jals > 32
                and self.load is not None and self.hit > 0.80)

    def block(self) -> str:
        name = re.sub(r'\W+', '_', Path(self.path).stem.lower()).strip('_') or 'overlay'
        return (f'[[overlay]]\nname = "{name}"\nfile = "{self.path}"\nload = "{self.load:#x}"'
                + ('\n# off = 0   # the file starts with a loader header; the whole file is copied to `load`'
                   if self.header_at is not None else ''))

    def line(self) -> str:
        load = f'{self.load:#09x}' if self.load is not None else '       ?'
        return (f'  {self.path:<28} {self.size / 1024:7.0f} KB  opcodes {self.valid * 100:3.0f}%  '
                f'prologues {self.prologues:4.1f}/KB  jal {self.jals:5d} in-range {self.hit * 100:3.0f}%  '
                f'load {load}')


def scan_blob(path: str, data: bytes, full_size: int) -> Candidate:
    """Score a file as "raw MIPS code loaded somewhere", and guess where."""
    n = len(data) // 4
    words = struct.unpack_from(f'<{n}I', data) if n else ()
    valid = prologues = 0
    targets = []
    for w in words:
        op = w >> 26
        if op == 0:
            if (w & 63) in SPECIAL_FUNCT or w == 0:
                valid += 1
        elif op in VALID_OP:
            valid += 1
        if op == 3:
            targets.append((w & 0x3ffffff) << 2)
        if (w & 0xffff8000) == 0x27bd8000:
            prologues += 1
    load, hit = _implied_base(targets, full_size)
    header = None
    if load is not None:
        header = next((i * 4 for i in range(min(32, n)) if words[i] == load), None)
    return Candidate(path, full_size, valid / n if n else 0.0,
                     prologues / (len(data) / 1024) if data else 0.0, len(targets), load, hit, header)


def _implied_base(targets: list[int], size: int) -> tuple[int | None, float]:
    """The load address implied by a blob's own `jal`s.

    A `jal` encodes an absolute target (the top 4 bits come from the delay-slot PC, which for a single
    256 MB segment means "as written"). A blob's internal calls therefore all land inside the range it was
    loaded at, so the base is whichever address captures the most of them. Candidates come from the lowest
    targets rounded down — the first function is usually at or near the base.
    """
    ts = sorted(t for t in targets if in_ram(t))
    if len(ts) < 8:
        return None, 0.0
    cands = {t & ~0xff for t in ts[:200]} | {t & ~0xfff for t in ts[:200]}
    best, score = None, 0
    for b in sorted(cands):
        k = sum(1 for t in ts if b <= t < b + size)
        if k > score:                       # ties go to the lowest base: an overlay starts with code
            best, score = b, k
    return best, score / len(targets) if targets else 0.0


def scan(root: Path | None = None, limit: int = 12) -> list[Candidate]:
    """Every file under the extracted disc that looks like a loadable code blob, best first."""
    c = cfg()
    root = Path(root) if root else c.ctx('iso')
    if not root.is_dir():
        raise SystemExit(f'nothing extracted yet at {root}\n    kit extract')
    out = []
    for p in sorted(root.rglob('*')):
        if not p.is_file():
            continue
        size = p.stat().st_size
        if size < 0x2000 or p.suffix.upper() in MEDIA_EXT:
            continue
        with open(p, 'rb') as fh:
            head = fh.read(min(size, SCAN_BYTES))
        if head[:4] == b'\x7fELF':          # an ELF already says where it loads; --info reads it
            continue
        cand = scan_blob(str(p.relative_to(root)).replace(os.sep, '/'), head, size)
        if cand.valid > 0.6 and cand.jals > 8:
            out.append(cand)
    out.sort(key=lambda x: (x.good, x.hit, x.valid), reverse=True)
    return out[:limit]


# --- building the programs ----------------------------------------------------

def disc_bytes(rel: str) -> bytes:
    """A disc file, from `context/iso` if it has been extracted, otherwise straight out of the image."""
    c = cfg()
    p = c.ctx('iso') / rel.replace('\\', '/')
    if p.is_file():
        return p.read_bytes()
    from ps2kit import disc as disc_mod
    with disc_mod.open_disc() as d:
        return d.read(rel)


def boot_name() -> str:
    """The boot ELF's path on the disc: [boot] elf when set, else whatever SYSTEM.CNF says."""
    c = cfg()
    if c.boot_elf:
        return c.boot_elf
    from ps2kit import disc as disc_mod
    with disc_mod.open_disc() as d:
        return disc_mod.system_cnf(d).elf


def elf_path(program: str) -> Path:
    return cfg().ctx('elf', f'{program}.elf')


def build(program: str) -> Path:
    """Write `context/elf/<program>.elf`. `boot` is the disc's ELF verbatim; an overlay is the boot ELF's
    segments plus the overlay blob at its load address, so cross-overlay calls resolve in Ghidra."""
    c = cfg()
    out = elf_path(program)
    boot = disc_bytes(boot_name())
    if boot[:4] != b'\x7fELF':
        # Checked before anything is written: [boot] elf pointing at the wrong file is a common typo, and
        # a boot.elf full of raw MIPS would then be analysed for half an hour before anyone noticed.
        raise ElfError(f'{boot_name()} is not an ELF (starts {boot[:4]!r}).\n'
                       'Set [boot] elf in kit.toml to the file SYSTEM.CNF names (kit extract --list), '
                       'or leave it empty to take it from SYSTEM.CNF.')
    if program == 'boot':
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(boot)
        e = Elf(out)
        print(f'  boot   {out.name:<16} {len(boot) / 1024:7.0f} KB  entry {e.entry:#x}  '
              f'{len(e.loads)} segments')
        return out

    ov = c.overlay(program)
    blob = disc_bytes(ov.file)[ov.off:]
    tmp = cfg().ctx('elf', '.boot.tmp')
    tmp.write_bytes(boot)
    try:
        be = Elf(tmp)
        lo, hi = ov.load, ov.load + len(blob)
        pieces = [(s.vaddr, be.read(s.vaddr, s.filesz), s.memsz) for s in be.loads
                  if not (s.vaddr < hi and lo < s.end)]      # drop the slot this overlay lands in
        dropped = [s for s in be.loads if s.vaddr < hi and lo < s.end]
        pieces.append((ov.load, blob))
        write_elf(out, pieces, entry=be.entry, eflags=be.eflags)
    finally:
        tmp.unlink(missing_ok=True)
    print(f'  {program:<6} {out.name:<16} {len(blob) / 1024:7.0f} KB  at {ov.load:#x} + '
          f'{len(pieces) - 1} boot segment(s)'
          + (f'  (replaced the boot ELF\'s own {", ".join(f"{s.vaddr:#x}" for s in dropped)} segment)'
             if dropped else ''))
    return out


# --- kit elf ----------------------------------------------------------------

DOC = """Build the ELFs Ghidra analyses — one per program in kit.toml.

  kit elf                      boot + every [[overlay]] → context/elf/*.elf
  kit elf --program game       just one
  kit elf --scan               guess the overlays from the extracted disc, print [[overlay]] blocks
  kit elf --irx MODULES/X.IRX  an IOP module as its own ELF (R3000 — its own Ghidra project)
  kit elf --info FILE          headers and segments of an ELF

An overlay ELF contains the boot ELF's segments as well as the overlay, which is the whole point: without
them every call out of the overlay decompiles as a call into nothing."""


def cmd_elf(argv: list[str]) -> int:
    from ps2kit.cli import parser
    ap = parser('elf', DOC)
    ap.add_argument('--program', '-p', metavar='NAME', help='build just this program (boot, or an overlay name)')
    ap.add_argument('--scan', action='store_true', help='guess the overlays from the extracted disc')
    ap.add_argument('--irx', metavar='FILE', help='convert an IOP module to a MIPS ELF')
    ap.add_argument('--info', metavar='FILE', help='print an ELF\'s headers and segments')
    ap.add_argument('--root', metavar='DIR', help='--scan: where the disc was extracted (default context/iso)')
    ap.add_argument('--limit', type=int, default=12, metavar='N', help='--scan: candidates to print')
    args = ap.parse_args(argv)

    try:
        if args.info:
            return _info(args.info)
        if args.irx:
            src = Path(args.irx)
            if not src.exists():
                src = cfg().ctx('iso') / args.irx.replace('\\', '/')
            irx_elf(src, cfg().ctx('elf', f'{src.stem.lower()}.elf'))
            return 0
        if args.scan:
            return _scan_report(args.root, args.limit)
        c = cfg()
        names = [args.program] if args.program else c.programs
        for n in names:
            if n != 'boot':
                c.overlay(n)                      # fails loudly before we read the disc
        print(f'{c.name}: building {len(names)} program(s) into {cfg().ctx("elf")}')
        bad = []
        for n in names:
            # One bad program must not stop the others: a game's boot ELF and its overlays are
            # independent, and "the overlay built, the boot ELF is not an ELF" is a useful thing to learn
            # in one run rather than two.
            try:
                build(n)
            except (ElfError, OSError) as ex:
                bad.append(n)
                print(f'  {n}: {ex}', file=sys.stderr)
        if bad:
            print(f'kit elf: {len(bad)} of {len(names)} failed: {", ".join(bad)}', file=sys.stderr)
            return 1
        if len(c.programs) == 1:
            print('No [[overlay]] blocks in kit.toml — if the game has overlays, find them: kit elf --scan')
        print('next: kit ghidra analyse        # tens of minutes per program, once')
    except (ElfError, OSError) as e:
        print(f'kit elf: {e}', file=sys.stderr)
        return 1
    return 0


def _info(path: str) -> int:
    p = Path(path)
    if not p.exists():
        p = cfg().ctx('iso') / path.replace('\\', '/')
    e = Elf(p)
    print(e)
    for s in e.segments:
        print(' ', s)
    for s in e.sections:
        if s.flags & SHF_ALLOC and s.size:
            print(f'  {s.name:<16} {s.addr:#010x} {s.size:#9x}')
    fns = e.functions()
    if fns:
        print(f'  {len(fns)} function symbols, first {min(fns):#x}, last {max(fns):#x}')
    return 0


def _scan_report(root, limit: int) -> int:
    cands = scan(root, limit)
    good = [c for c in cands if c.good]
    if not cands:
        print('No file under the extracted disc looks like a raw code blob.\n'
              'Either the game has no overlays (common), or its code is packed/compressed — in that case\n'
              'look at what the boot ELF loads: kit xref -p boot <the loader address>.')
        return 0
    print('Files that look like raw MIPS code (opcode density, prologue density, and how many of the')
    print("file's own jal targets land inside [load, load+size) — that is what pins the load address):")
    for c in cands:
        print(('* ' if c.good else '  ') + c.line().lstrip())
    if not good:
        print('\nNothing scored high enough to be sure. The starred rows are the ones to look at by hand;\n'
              'confirm a load address by finding the loader in the boot ELF (kit xref -p boot …).')
        return 0
    print('\nPaste into kit.toml (check the names — they become the dump names and `-p` arguments):\n')
    for c in good:
        print(c.block() + '\n')
    print('Then:  kit elf   &&   kit ghidra analyse')
    return 0


if __name__ == '__main__':
    sys.exit(cmd_elf(sys.argv[1:]))
