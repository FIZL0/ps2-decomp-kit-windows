"""Read a PS2 disc image without any external tool.

Why this exists: every PS2 project starts by unpacking the disc, and the usual advice (`7z x game.iso`)
silently does the wrong thing often enough to matter — it mangles `;1` version suffixes, it cannot read a
2352-byte raw rip at all, and it will happily unpack *the wrong region's disc* into the directory every
address you later write down is relative to. Extracting PAL instead of NTSC and then spending a day on
addresses that cannot match is the single most expensive beginner mistake in this work, so `kit extract`
reads `SYSTEM.CNF`, derives the serial, and refuses when it disagrees with `kit.toml`.

ISO 9660, the parts a PS2 disc actually uses:

* 2048-byte logical sectors; the Primary Volume Descriptor is at sector 16 and starts `\\x01CD001`.
* The PVD's root directory record is at offset 156; LBA and size are "both-endian" pairs, little-endian
  half first (so a 4-byte LE read at +2 and +10 of the record).
* A directory is a list of variable-length records. A zero length byte means "no more records in this
  sector" — records never straddle a sector — so skip to the next sector boundary rather than stopping.
* Names are upper-case with a `;1` version suffix. The kit's canonical path form drops the suffix, upper-cases,
  and joins with `/`: `DATA/GAME.BIN`.
* Joliet (a secondary descriptor with an escape sequence, UCS-2 names) exists on a few discs. We never need
  it — PS2 games open files by their 8.3 names — so we read the primary tree and simply ignore the Joliet
  tree instead of tripping over it.

Raw rips: a `.bin` from a CD ripper has 2352-byte sectors (12 sync + 4 header + data [+ ECC]), and a few
tools add a 96-byte subchannel on top (2448). Rather than failing on those, `Disc` finds the stride and the
offset of the user data inside a sector by looking for the PVD at every plausible (stride, offset) pair.
"""
from __future__ import annotations

import fnmatch
import os
import re
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

from ps2kit.config import cfg

SECTOR = 2048

# (stride, offset of user data in a sector). Mode 1 data starts after 12 sync + 4 header bytes; Mode 2
# Form 1 adds an 8-byte subheader. 2448 = 2352 + a 96-byte subchannel some rippers append.
LAYOUTS = ((2048, 0), (2352, 16), (2352, 24), (2448, 16), (2448, 24))

# Containers we cannot read and must not guess at: a half-decompressed image looks like a corrupt disc and
# wastes an afternoon. Say which tool converts it and stop.
COMPRESSED = {
    b'MComprHD': ('CHD', 'chdman extractcd -i game.chd -o game.cue -ob game.bin'),
    b'CISO':     ('CSO', 'maxcso --decompress game.cso'),
    b'ZISO':     ('ZSO', 'maxcso --decompress game.zso'),
    b'\x1f\x8b': ('gzip', 'gunzip game.iso.gz'),
    b'7z\xbc\xaf': ('7z', '7z x game.7z'),
    b'PK\x03\x04': ('zip', 'unzip game.zip'),
}


@dataclass(frozen=True)
class Entry:
    """One name in the disc's directory tree. `lba` is in 2048-byte logical sectors, whatever the rip's stride."""
    path: str
    lba: int
    size: int
    is_dir: bool = False

    def __str__(self) -> str:
        return f'{self.lba:8d}  {self.size:10d}  {self.path}' + ('/' if self.is_dir else '')


class DiscError(Exception):
    pass


class Disc:
    """An ISO 9660 image, opened for reading.

        with Disc('game.iso') as d:
            d.list()                      # [Entry, …] files only; dirs=True for directories too
            d.read('SYSTEM.CNF')          # bytes
            d.extract(Path('context/iso'))

    Paths are case-insensitive and may use `/` or `\\`; a leading separator and a `;1` suffix are ignored.
    """

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        if not self.path.exists():
            raise DiscError(f'disc image not found: {self.path}')
        self._f = open(self.path, 'rb')
        head = self._f.read(8)
        for magic, (kind, fix) in COMPRESSED.items():
            if head.startswith(magic):
                self.close()
                raise DiscError(f'{self.path.name} is a {kind} container, not a raw disc image.\n'
                                f'Convert it first:  {fix}\n'
                                'The kit deliberately does not guess at compressed images.')
        self.stride, self.data_off = self._detect()
        self.pvd = self._sector(16)
        self.label = self.pvd[40:72].decode('latin-1').strip()
        self._entries: list[Entry] | None = None

    # --- plumbing ------------------------------------------------------------

    def _detect(self) -> tuple[int, int]:
        size = self.path.stat().st_size
        for stride, off in LAYOUTS:
            pos = 16 * stride + off
            if pos + SECTOR > size:
                continue
            self._f.seek(pos)
            buf = self._f.read(6)
            if len(buf) == 6 and buf[1:6] == b'CD001' and buf[0] in (1, 2):
                return stride, off
        self.close()
        raise DiscError(f'{self.path.name} has no ISO 9660 volume descriptor at sector 16 '
                        f'(tried {", ".join(f"{s}/{o}" for s, o in LAYOUTS)} byte sector layouts).\n'
                        'Not a PS2 disc image, or a damaged dump.')

    def _sector(self, lba: int) -> bytes:
        self._f.seek(lba * self.stride + self.data_off)
        buf = self._f.read(SECTOR)
        if len(buf) < SECTOR:
            buf += bytes(SECTOR - len(buf))  # a dump truncated at the last sector is common and harmless
        return buf

    def _stream(self, lba: int, size: int, chunk_sectors: int = 256):
        """Yield a file's bytes in chunks. Never slurps: a PS2 disc has 200 MB FMV files on it."""
        left, s = size, lba
        while left > 0:
            n = min(chunk_sectors, (left + SECTOR - 1) // SECTOR)
            buf = b''.join(self._sector(s + i) for i in range(n))
            take = min(left, len(buf))
            yield buf[:take]
            left -= take
            s += n

    # --- the directory tree --------------------------------------------------

    @staticmethod
    def key(path: str) -> str:
        """Canonical form of a disc path: upper-case, `/` separators, no leading slash, no `;1`."""
        p = str(path).replace('\\', '/').lstrip('/')
        return '/'.join(seg.split(';')[0] for seg in p.split('/')).upper()

    def _walk(self, prefix: str, lba: int, size: int, depth: int, out: list[Entry]) -> None:
        if depth > 16:
            raise DiscError('directory nesting deeper than 16 — refusing to follow a loop')
        data = b''.join(self._stream(lba, size))
        p = 0
        while p < len(data):
            rec_len = data[p]
            if rec_len == 0:                      # padding to the end of this sector
                p = (p // SECTOR + 1) * SECTOR
                continue
            rec = data[p:p + rec_len]
            if len(rec) < 33:
                break
            name_len = rec[32]
            raw = rec[33:33 + name_len]
            p += rec_len
            if raw in (b'\x00', b'\x01'):         # '.' and '..'
                continue
            name = raw.decode('latin-1').split(';')[0].upper()
            path = f'{prefix}/{name}' if prefix else name
            e_lba, e_size = struct.unpack_from('<I', rec, 2)[0], struct.unpack_from('<I', rec, 10)[0]
            if rec[25] & 0x02:
                out.append(Entry(path, e_lba, e_size, True))
                self._walk(path, e_lba, e_size, depth + 1, out)
            else:
                out.append(Entry(path, e_lba, e_size, False))

    def list(self, dirs: bool = False) -> list[Entry]:
        """Every file on the disc, sorted by path. Cached; cheap to call twice."""
        if self._entries is None:
            root = self.pvd[156:156 + 34]
            out: list[Entry] = []
            self._walk('', struct.unpack_from('<I', root, 2)[0], struct.unpack_from('<I', root, 10)[0], 0, out)
            self._entries = sorted(out, key=lambda e: e.path)
        return [e for e in self._entries if dirs or not e.is_dir]

    def find(self, path: str) -> Entry:
        k = self.key(path)
        for e in self.list():
            if e.path == k:
                return e
        raise DiscError(f'{path} is not on this disc (kit extract --list shows what is)')

    def read(self, path: str) -> bytes:
        """One file, whole. Fine for an ELF or a config; use `stream()` for anything big."""
        e = self.find(path)
        return b''.join(self._stream(e.lba, e.size))

    def stream(self, path: str):
        e = self.find(path)
        return self._stream(e.lba, e.size)

    def extract(self, dest: str | os.PathLike, subset: list[str] | None = None,
                progress=None) -> tuple[int, int]:
        """Write the disc's files under `dest`, keeping the directory layout. Returns (files, bytes).

        `subset` entries match a full path, a directory prefix, or a glob (`*.IRX`, `DATA/*`).
        """
        dest = Path(dest)
        want = self.select(subset)
        total = 0
        for i, e in enumerate(want):
            out = dest / e.path
            out.parent.mkdir(parents=True, exist_ok=True)
            with open(out, 'wb') as fh:
                for chunk in self._stream(e.lba, e.size):
                    fh.write(chunk)
            total += e.size
            if progress:
                progress(i + 1, len(want), e)
        return len(want), total

    def select(self, subset: list[str] | None) -> list[Entry]:
        """The files `subset` names (see `extract`); everything when it is None or empty."""
        files = self.list()
        if not subset:
            return files
        pats = [self.key(s) for s in subset]
        out, unmatched = [], set(pats)
        for e in files:
            for pat in pats:
                if e.path == pat or e.path.startswith(pat.rstrip('/') + '/') or fnmatch.fnmatch(e.path, pat):
                    out.append(e)
                    unmatched.discard(pat)
                    break
        if unmatched:
            raise DiscError('not on this disc: ' + ', '.join(sorted(unmatched)))
        return out

    def close(self) -> None:
        f = getattr(self, '_f', None)
        if f and not f.closed:
            f.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


# --- SYSTEM.CNF ---------------------------------------------------------------

@dataclass
class Boot:
    """What `SYSTEM.CNF` says: which ELF the PS2 runs, and therefore which disc this is."""
    elf: str                  # disc path of the boot ELF, canonical form: 'SLUS_200.00'
    serial: str               # 'SLUS-20000'
    version: str              # the VER= line, '' when absent
    raw: dict

    def __str__(self) -> str:
        return f'{self.serial or "(no serial)"}  boot {self.elf}' + (f'  ver {self.version}' if self.version else '')


def serial_of(boot_spec: str) -> str:
    """`cdrom0:\\SLUS_200.00;1` → `SLUS-20000`.

    The serial printed on the disc is the boot ELF's name with the underscore turned into a hyphen and the
    dot dropped. This is how `kit setup` learns the serial without the user typing it, and how `extract`
    catches a disc from the wrong region before it costs anybody a day.
    """
    name = Disc.key(boot_spec).split(':')[-1]
    name = name.rsplit('/', 1)[-1]
    m = re.fullmatch(r'([A-Z]{4})[-_]?(\d{3})\.?(\d{2})', name)
    # '' rather than the raw name when it is not a serial at all: a homebrew or prototype disc boots
    # BOOT.ELF or similar, and "this disc is BOOT.ELF" would read like a region mismatch when it is not.
    return f'{m.group(1)}-{m.group(2)}{m.group(3)}' if m else ''


def system_cnf(disc: Disc) -> Boot:
    """Parse SYSTEM.CNF off an open disc. Keys are `BOOT2`, `VER`, `VMODE` (and whatever else is there)."""
    try:
        text = disc.read('SYSTEM.CNF').decode('latin-1')
    except DiscError:
        raise DiscError('no SYSTEM.CNF on this image — a PS2 disc always has one, so this is either a '
                        'PS1 disc, a data disc, or a bad dump')
    raw = {}
    for line in text.replace('\r', '\n').split('\n'):
        if '=' in line and not line.lstrip().startswith(('#', ';')):
            k, v = line.split('=', 1)
            raw[k.strip().upper()] = v.strip()
    spec = raw.get('BOOT2') or raw.get('BOOT') or ''
    if not spec:
        raise DiscError(f'SYSTEM.CNF has no BOOT2 line (keys: {", ".join(raw) or "none"})')
    return Boot(elf=Disc.key(spec).split(':')[-1].lstrip('/'), serial=serial_of(spec),
                version=raw.get('VER', ''), raw=raw)


def open_disc(path=None) -> Disc:
    """The configured disc image (or `path`), with the config's error messages."""
    return Disc(Path(path) if path else cfg().need_iso())


# --- kit extract ------------------------------------------------------------

DOC = """Unpack the disc image into the private context/ directory.

  kit extract                        everything, into context/iso
  kit extract --list                 just show what is on the disc
  kit extract --only DATA/ --only SYSTEM.CNF
  kit extract --only '*.IRX'         globs, directory prefixes and exact paths all work

The serial in SYSTEM.CNF is checked against [game] serial in kit.toml and a mismatch is fatal: every
address you write down afterwards depends on having extracted the right region's disc."""


def cmd_extract(argv: list[str]) -> int:
    ap = _parser()
    args = ap.parse_args(argv)
    c = cfg()
    disc_path = Path(args.iso) if args.iso else c.need_iso()
    out = Path(args.out) if args.out else c.ctx('iso')

    try:
        with Disc(disc_path) as d:
            boot = _check_serial(d, c, args.any_serial)
            files = d.select(args.only)
            if args.list:
                return _print_listing(d, files, args.limit, c)
            print(f'{disc_path.name}  ({d.stride}-byte sectors)  {boot}')
            print(f'extracting {len(files)} file(s), {_mb(sum(e.size for e in files))} → {_rel(out, c)}')
            n, total = d.extract(out, args.only, progress=_ticker(len(files)))
            sys.stderr.write('\r' + ' ' * 60 + '\r')
            print(f'done: {n} files, {_mb(total)}')
            big = sorted(files, key=lambda e: -e.size)[:5]
            print('largest: ' + ', '.join(f'{e.path} {_mb(e.size)}' for e in big))
            print(f'next: kit elf            # build the Ghidra inputs (--scan guesses the overlays)')
    except DiscError as e:
        print(f'kit extract: {e}', file=sys.stderr)
        return 1
    return 0


def _parser():
    """Argument parsing that never touches kit.toml, so `--help` works before the game is configured."""
    from ps2kit.cli import parser
    ap = parser('extract', DOC)
    ap.add_argument('--only', action='append', metavar='PATH',
                    help='extract just these (repeatable): exact path, directory prefix, or glob')
    ap.add_argument('--list', action='store_true', help='list the disc instead of extracting')
    ap.add_argument('--out', metavar='DIR', help='destination (default: the context/iso directory)')
    ap.add_argument('--iso', metavar='FILE', help='a different image than [game] iso')
    ap.add_argument('--any-serial', action='store_true',
                    help="don't check SYSTEM.CNF's serial against kit.toml (you had better be sure)")
    ap.add_argument('--limit', type=int, default=60, metavar='N',
                    help='rows to print with --list before writing the rest to a file (default 60)')
    return ap


def _check_serial(d: Disc, c, any_serial: bool) -> Boot:
    boot = system_cnf(d)
    want = c.serial.replace('_', '-').upper()
    if want and not boot.serial and not any_serial:
        raise DiscError(f'this image boots {boot.elf}, which is not a retail serial, but kit.toml says\n'
                        f'[game] serial = {want!r}.  image: {d.path}\n'
                        'If this really is the disc you mean (a homebrew or prototype image), pass '
                        '--any-serial.')
    if want and boot.serial != want and not any_serial:
        raise DiscError(f'this disc is {boot.serial}, but kit.toml says [game] serial = {want!r}.\n'
                        f'  image: {d.path}\n'
                        'Extracting the wrong region leaves every address in your notes wrong. Either point\n'
                        '[game] iso at the right dump, fix [game] serial, or pass --any-serial if you mean it.')
    return boot


def _print_listing(d: Disc, files: list[Entry], limit: int, c) -> int:
    print(f'{d.path.name}  ({d.stride}-byte sectors)  volume {d.label!r}  {system_cnf(d)}')
    print(f'{len(files)} files, {_mb(sum(e.size for e in files))}')
    print('     LBA        bytes  path')
    for e in files[:limit]:
        print(e)
    if len(files) > limit:
        index = c.ctx('iso.index.txt')
        index.write_text('\n'.join(str(e) for e in d.list(dirs=True)) + '\n', encoding='utf-8')
        print(f'… {len(files) - limit} more (--limit N, or read the full listing: {_rel(index, c)})')
    return 0


def _ticker(total: int):
    """A one-line progress counter, and only on a terminal: in a log or an agent's transcript a
    per-file line for 1500 files is exactly the flood this kit is built to avoid."""
    if not sys.stderr.isatty():
        return None
    def tick(i, _n, e):
        if i % 25 == 0 or i == total:
            sys.stderr.write(f'\r  {i}/{total}  {e.path[:48]:<48}')
            sys.stderr.flush()
    return tick


def _mb(n: int) -> str:
    return f'{n / 1048576:.1f} MB' if n >= 1048576 else f'{n / 1024:.0f} KB' if n >= 1024 else f'{n} B'


def _rel(p: Path, c) -> str:
    from ps2kit.config import ROOT
    try:
        return str(Path(p).resolve().relative_to(ROOT))
    except ValueError:
        return str(p)


if __name__ == '__main__':
    sys.exit(cmd_extract(sys.argv[1:]))
