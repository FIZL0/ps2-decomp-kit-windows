"""Find things in the running game's RAM, so you don't have to be told where they are.

This is the module that turns "a kit someone else configured" into "a kit I can point at any disc". Every
other live tool needs addresses — the frame counter first of all — and reading a 32 MB dump by eye to find
them is the single most tedious hour of starting a new game. So:

* `find_vsync()` finds the game's own per-frame counter by watching which words in RAM climb at the frame
  rate and *how exactly linearly* they do it. Almost every PS2 game has one (its vsync interrupt handler
  bumps it); the kit needs it because wall-clock timing cannot tell you which frame a value belongs to.
  `--save` writes it into kit.toml, once, forever.
* `find_changed()` is the classic value hunt from any cheat-search tool, narrowed across passes: snapshot,
  play, say what happened to the number ("it went down"), repeat until a handful of addresses are left.
  That is how you find the player's X coordinate, the score, the RNG seed, in minutes rather than in a
  disassembly session.
* `chase()` walks a pointer chain live, which is how a struct layout gets mapped: the decompiler shows you
  `*(float *)(ptr + 0x40)`, and this says what is actually there right now.

Everything here reads *only*; nothing writes to the game. Reads come through one PINE connection
(`ps2kit.pine`), so the PCSX2 lock and the serial check apply as usual — a candidate list found against the
wrong disc version is worse than no list at all.

numpy is used when it is importable (a full-RAM pass is ~30x faster); the plain-Python path does the same
work and only prints a hint.
"""
from __future__ import annotations

import json
import struct
import sys
import time
from array import array
from dataclasses import dataclass, field

from ps2kit.config import cfg, num
from ps2kit.pine import OPS_PER_MSG, Pine, TYPES, add_type_flags, show, type_flag

try:
    import numpy as _np
except ImportError:                        # pragma: no cover - optional, see docs/INTERNALS.md
    _np = None

# Where a scan starts by default: the EE kernel and its stacks live under this, and nothing a game counts
# frames with lives there. Scanning it anyway only adds noise and seconds.
SCAN_FROM = 0x0010_0000
CHUNK = OPS_PER_MSG * 8                    # bytes per PINE round trip — one timestamp per chunk, see below
FPS_LO, FPS_HI = 45.0, 65.0                # NTSC 59.94 and PAL 50, with room for a double-bumped counter


def _size(n: int) -> str:
    return f'{n / (1 << 20):.0f} MiB' if n >= (1 << 20) else f'{n >> 10} KiB'


def ee(addr: int, ram: int | None = None) -> int:
    """Fold a PS2 virtual address (0x8…, 0xa…) onto the RAM offset PINE wants. Leaves plain offsets alone."""
    ram = ram if ram is not None else cfg().ram
    return addr & (ram - 1) if addr >= ram else addr


# --- snapshots ---------------------------------------------------------------

@dataclass
class Snap:
    """A region of RAM, read in `CHUNK`-sized round trips, with the time each chunk was read.

    The per-chunk timestamp is the whole trick behind `find_vsync`: a full-RAM pass takes a second or two,
    so the word at the end of the region was read a second later than the word at the start. Dividing a
    word's delta by the *region's* elapsed time would then mis-rate every candidate; dividing by its own
    chunk's elapsed time is exact enough to separate a 60 Hz counter from a 59 Hz one.
    """
    addr: int
    data: bytes
    times: list[float] = field(default_factory=list)

    def at(self, off: int) -> float:
        return self.times[min(off // CHUNK, len(self.times) - 1)]


def snapshot(p: Pine, addr: int, length: int) -> Snap:
    buf = bytearray()
    times: list[float] = []
    for off in range(0, length, CHUNK):
        times.append(time.monotonic())
        buf += p.read_block(addr + off, min(CHUNK, length - off))
    return Snap(addr, bytes(buf), times)


def _u32(buf: bytes):
    """The buffer as little-endian u32s: a numpy view, or an `array` (byteswapped on a big-endian host)."""
    if _np is not None:
        return _np.frombuffer(buf, dtype='<u4')
    a = array('I')
    a.frombytes(buf[:len(buf) & ~3])
    if sys.byteorder == 'big':             # pragma: no cover
        a.byteswap()
    return a


# --- the frame counter -------------------------------------------------------

def find_vsync(p: Pine | None = None, addr: int | None = None, length: int | None = None,
               top: int = 8, save: bool = False) -> list[tuple[int, float, int, float]]:
    """Find the game's per-frame counter. Returns [(address, frames/s, value, error), …], best first.

    How: three snapshots over two different intervals. A real frame counter increases monotonically at the
    display rate and is *linear* — value(t) = a + b·t with no wobble. Anything driven by a timer that the
    game only samples (or a two-u16 pair that looks like a counter) fails the linearity check across two
    different intervals, which is why the second interval is deliberately a different length.
    """
    c = cfg()
    p = p or Pine()
    if p.status() == 'paused':
        raise SystemExit('PCSX2 is paused — nothing in RAM is moving, so there is nothing to find.\n'
                         'Let the game run (a title screen or attract loop is ideal) and retry.')
    a0 = ee(addr if addr is not None else SCAN_FROM)
    n = length if length is not None else c.ram - a0
    n &= ~7
    if _np is None:
        print('probe: numpy is not installed — this works but takes a few times longer '
              '(pip install numpy)', file=sys.stderr)
    print(f'probe: watching {_size(n)} at {a0:#08x} over ~2 s …', file=sys.stderr)
    s0 = snapshot(p, a0, n)
    time.sleep(0.9)
    s1 = snapshot(p, a0, n)
    cands = _rate_filter(s0, s1)
    if not cands:
        raise SystemExit('probe: no word in that region climbs at the frame rate.\n'
                         'Is the game actually running (not in a menu that idles)? Try a wider region:\n'
                         '    kit probe vsync --addr 0 --len 0x2000000')
    time.sleep(0.37)                        # a *different* interval: a linear counter does not care
    s2 = snapshot(p, a0, n)
    scored = []
    for off, rate in cands:
        v0, v1, v2 = (_word(s, off) for s in (s0, s1, s2))
        dt1, dt2 = s1.at(off) - s0.at(off), s2.at(off) - s0.at(off)
        if dt1 <= 0 or dt2 <= 0:
            continue
        d1, d2 = (v1 - v0) & 0xffffffff, (v2 - v0) & 0xffffffff
        if d2 < d1:                         # it went backwards, or wrapped at something other than 2^32
            continue
        b = d1 / dt1
        err = abs(d2 - b * dt2) / max(1.0, b * dt2)
        if not (FPS_LO <= b <= FPS_HI) or err > 0.05:
            continue
        scored.append((a0 + off, b, v2, err))
    scored.sort(key=lambda r: (r[3], r[0]))
    scored = _reject_while_paused(p, scored)
    if not scored:
        raise SystemExit('probe: candidates climbed at the right rate but none of them linearly — '
                         'rerun with the game left alone for a few seconds (no input, no loading).')
    for a, rate, v, err in scored[:top]:
        print(f'{a:#08x}  {rate:6.2f} frames/s  now {v:>12}  error {err * 100:5.2f}%')
    if len(scored) > top:
        print(f'… and {len(scored) - top} more (counters are usually mirrored in several places)')
    best = scored[0][0]
    if save:
        cfg().set('emu', 'vsync_addr', best)
        print(f'kit.toml: [emu] vsync_addr = {best:#x}')
    else:
        print(f'\nkeep it:  kit probe vsync --save      (would write {best:#x})')
    return scored


def _word(s: Snap, off: int) -> int:
    return struct.unpack_from('<I', s.data, off)[0]


def _rate_filter(s0: Snap, s1: Snap) -> list[tuple[int, float]]:
    """Word offsets whose delta between two snapshots works out to roughly a frame rate."""
    a, b = _u32(s0.data), _u32(s1.data)
    words_per_chunk = CHUNK // 4
    out: list[tuple[int, float]] = []
    if _np is not None:
        d = (b.astype('int64') - a.astype('int64')) & 0xffffffff
        dt = _np.repeat(_np.array(s1.times) - _np.array(s0.times), words_per_chunk)[:len(d)]
        rate = d / _np.maximum(dt, 1e-6)
        hit = _np.nonzero((rate >= FPS_LO) & (rate <= FPS_HI))[0]
        return [(int(i) * 4, float(rate[i])) for i in hit]
    for i in range(len(a)):
        d = (b[i] - a[i]) & 0xffffffff
        if not d or d > 200:                # cheap pre-filter: ~2 s at any frame rate is well under 200
            continue
        dt = s1.times[min(i // words_per_chunk, len(s1.times) - 1)] - \
            s0.times[min(i // words_per_chunk, len(s0.times) - 1)]
        if dt > 0 and FPS_LO <= d / dt <= FPS_HI:
            out.append((i * 4, d / dt))
    return out


def _reject_while_paused(p: Pine, scored: list) -> list:
    """Drop anything that keeps moving with the EE halted — it isn't the game counting frames.

    Only possible on a kit copy, whose pause is bound to the virtual pad; the user's own emulator is never
    paused from here, so the check is skipped with a word about it.
    """
    if not scored or not p.pause():
        print('probe: (skipped the paused check — only a kit copy can be paused from here)', file=sys.stderr)
        return scored
    try:
        first = {a: p.read32(a) for a, *_ in scored}
        time.sleep(0.5)
        keep = [r for r in scored if p.read32(r[0]) == first[r[0]]]
        if len(keep) != len(scored):
            print(f'probe: dropped {len(scored) - len(keep)} candidate(s) that moved while paused',
                  file=sys.stderr)
        return keep
    finally:
        p.resume()


# --- the value hunt ----------------------------------------------------------

STATE = 'probe/candidates.json'
SNAPFILE = 'probe/snapshot.bin'


def find_changed(p: Pine | None = None, mode: str | None = None, equals: str | None = None,
                 kind: str = 'u32', addr: int | None = None, length: int | None = None,
                 reset: bool = False, tol: float = 1e-4, limit: int = 40) -> list[int]:
    """Narrow a candidate list by what happened to the value. Returns the surviving addresses.

    Each call reads the region, compares it to the snapshot the last call left behind, keeps the candidates
    that match (`mode` one of changed/unchanged/increased/decreased, and/or `equals` an exact value now),
    and saves the new snapshot. Run it once with `reset=True`, do something in the game, run it again with
    what you know; three or four passes usually leave a single address.
    """
    c = cfg()
    p = p or Pine()
    state_path, snap_path = c.ctx(STATE), c.ctx(SNAPFILE)
    prev = json.loads(state_path.read_text()) if state_path.exists() and not reset else {}
    a0 = ee(addr if addr is not None else num(prev.get('addr')) or SCAN_FROM)
    n = length if length is not None else (num(prev.get('len')) or c.ram - a0)
    n &= ~7
    kind = prev.get('kind', kind) if prev and not reset else kind
    width = TYPES[kind]
    if prev and (num(prev.get('addr')) != a0 or num(prev.get('len')) != n or prev.get('kind') != kind):
        raise SystemExit('probe find: the region or type changed since the last pass, so the stored '
                         'candidates mean nothing. Start over with --reset.')

    now = snapshot(p, a0, n)
    old = snap_path.read_bytes() if (prev and snap_path.exists()) else None
    cands = [int(x, 16) for x in prev.get('cands', [])] if prev.get('cands') is not None else None

    if old is None and mode:
        raise SystemExit('probe find: no earlier snapshot to compare with.\n'
                         'Take one first:   kit probe find --reset        (then play, then ask again)')

    def remember(keep: list[int] | None) -> None:
        # cands = null means "every address in the region is still a candidate"; an empty list means the
        # hunt is over. Writing [] for the opening pass would quietly end every later pass with 0 hits.
        snap_path.write_bytes(now.data)
        state_path.write_text(json.dumps(
            {'addr': f'{a0:#x}', 'len': f'{n:#x}', 'kind': kind, 'when': time.strftime('%F %T'),
             'cands': None if keep is None or len(keep) > 200_000 else [f'{a:#x}' for a in keep]}, indent=0))

    if cands is None and not mode and equals is None:
        remember(None)
        print(f'remembered {_size(n)} at {a0:#x} as {kind}. Now change the value in the game, then:\n'
              '    kit probe find --changed      (or --increased / --decreased / --equals V)')
        return []

    keep = _narrow(old, now.data, a0, width, kind, mode, equals, tol, cands)
    remember(keep)
    what = ' '.join(filter(None, [mode or '', f'== {equals}' if equals else '']))
    print(f'{len(keep)} candidate(s) {what or "(first pass)"}  [{kind} in {a0:#x}+{n:#x}]')
    for a in keep[:limit]:
        v = _val(now.data, a - a0, kind)
        print(f'  {a:#08x}  {show(v, kind)}')
    if len(keep) > limit:
        print(f'  … {len(keep) - limit} more in {state_path}')
    if len(keep) > 1:
        print('Next: do something in the game that changes it, then\n'
              '      kit probe find --decreased     (or --increased / --changed / --unchanged)')
    return keep


def _val(buf: bytes, off: int, kind: str):
    from ps2kit.pine import decode
    return decode(buf[off:off + TYPES[kind]], kind)[0]


def _narrow(old: bytes | None, new: bytes, a0: int, width: int, kind: str, mode: str | None,
            equals: str | None, tol: float, cands: list[int] | None) -> list[int]:
    target = None
    if equals is not None:
        target = float(equals) if kind in ('f32', 'f64') else num(equals, 'value')

    def ok(off: int) -> bool:
        if mode:
            o, nn = _val(old, off, kind), _val(new, off, kind)
            if kind in ('f32', 'f64') and (o != o or nn != nn):
                return False                # NaN: a float field that isn't one, or uninitialised memory
            if mode == 'changed' and not o != nn:
                return False
            if mode == 'unchanged' and o != nn:
                return False
            if mode == 'increased' and not nn > o:
                return False
            if mode == 'decreased' and not nn < o:
                return False
        if target is not None:
            v = _val(new, off, kind)
            if kind in ('f32', 'f64'):
                if v != v or abs(v - target) > tol * max(1.0, abs(target)):
                    return False
            elif v != target:
                return False
        return True

    if cands is not None:
        return [a for a in cands if ok(a - a0)]
    if _np is not None and kind == 'u32' and mode and target is None:
        # The only pass that is big enough to care: whole-region, word by word.
        o, nn = _u32(old), _u32(new)
        oi, ni = o.astype('int64'), nn.astype('int64')
        hit = {'changed': oi != ni, 'unchanged': oi == ni,
               'increased': ni > oi, 'decreased': ni < oi}[mode]
        return [a0 + int(i) * 4 for i in _np.nonzero(hit)[0]]
    if cands is None:
        print(f'probe: checking every {kind} in the region one at a time'
              + ('' if _np is not None else ' (pip install numpy makes this ~30x faster)') + ' …',
              file=sys.stderr)
    return [a0 + off for off in range(0, len(new) - width + 1, width) if ok(off)]


# --- pointer chains ----------------------------------------------------------

def chase(p: Pine, addr: int, offsets: list[int]) -> list[tuple[int, int]]:
    """Follow a pointer chain, printing every hop: `addr` → *(addr) + off0 → … Returns [(read_at, value)].

    What this is for: the decompilation says `*(int *)(*(int *)(base + 4) + 0x30)`. Typing that out as hops
    shows you which link is null, which one points outside RAM (so your base is wrong) and what the leaf
    value actually is, without rebuilding anything.
    """
    ram = cfg().ram
    rows = []
    at = ee(addr, ram)
    v = p.read32(at)
    rows.append((at, v))
    print(f'  {at:#08x} -> {v:#010x}{_note(v, ram)}')
    for off in offsets:
        at = ee(v, ram) + off
        if at + 4 > ram:
            print(f'  {at:#010x} is outside EE RAM — the chain broke at the hop before this one')
            break
        v = p.read32(at)
        rows.append((at, v))
        print(f'  {at:#08x} (+{off:#x}) -> {v:#010x}{_note(v, ram)}')
    return rows


def _note(v: int, ram: int) -> str:
    f = struct.unpack('<f', struct.pack('<I', v))[0]
    bits = []
    # Under the first 64 KiB is EE kernel space: a small number there is a count or a flag, not a pointer.
    if 0x10000 < ee(v, ram) < ram and (v & 3) == 0:
        bits.append('looks like a pointer')
    if f == f and 1e-6 < abs(f) < 1e7:
        bits.append(f'as f32 {f:.6g}')
    return f'   ({"; ".join(bits)})' if bits else ''


# --- CLI ---------------------------------------------------------------------

DOC = """find things in the live game's RAM: the frame counter, a changing value, a pointer chain.

  kit probe vsync --save               find the per-frame counter and write it to kit.toml
  kit probe find --reset               first pass: remember all of RAM
  kit probe find --equals 100          … or only the words holding 100 right now
  kit probe find --f32 1.5 --near @cam  … floats near a known address (--f32 = --as f32 --equals)
      (play until the number changes, then)
  kit probe find --decreased           narrow; repeat with --increased/--changed/--unchanged
  kit probe chase 1c4420 0x10 0x40     follow *(…)+0x10, then +0x40
  kit probe scan @player 0x80 --as f32
  kit probe watch @score --frames 120  one line per frame, to see what the value does

Each `find` pass keeps its candidate list under context/probe/, so passes build on each other.
Addresses may be hex, 0x-hex or @name from [symbols]."""


def cmd_probe(argv: list[str]) -> int | None:
    from ps2kit.cli import parser
    ap = parser('probe', DOC)
    ap.add_argument('cmd', nargs='?', default='vsync',
                    choices=['vsync', 'find', 'chase', 'scan', 'watch', 'wrote'])
    ap.add_argument('args', nargs='*', help='ADDR [OFFSET…] for chase, ADDR LEN for scan')
    ap.add_argument('--save', action='store_true', help='vsync: write the winner to kit.toml')
    ap.add_argument('--as', dest='kind', default='u32', choices=sorted(TYPES), help='value type')
    ap.add_argument('--addr', help='start of the region to search (default 0x100000)')
    ap.add_argument('--len', dest='length', help='how many bytes to search (default: to the end of RAM)')
    ap.add_argument('--equals', metavar='V', help='find: only addresses holding V right now')
    add_type_flags(ap)
    ap.add_argument('--near', metavar='A', help='search a window around this address instead of all of RAM')
    ap.add_argument('--window', metavar='N', default='0x10000',
                    help='how wide --near is (default 0x10000, centred)')
    ap.add_argument('--frames', type=int, default=60, help='watch: how many frames to sample')
    ap.add_argument('--tol', type=float, default=1e-4, help='find: relative tolerance for float --equals')
    for m in ('changed', 'unchanged', 'increased', 'decreased'):
        ap.add_argument(f'--{m}', dest='mode', action='store_const', const=m,
                        help=f'find: keep addresses whose value {m} since the last pass')
    ap.add_argument('--reset', action='store_true', help='find: forget the stored candidates and start over')
    ap.add_argument('--top', type=int, default=8, help='how many results to print (default 8)')
    ap.add_argument('--any-serial', action='store_true', help='skip the kit.toml serial check')
    a = ap.parse_args(argv)
    kflag, inline = type_flag(a)
    a.kind = kflag or a.kind
    a.equals = a.equals if a.equals is not None else inline
    addr = cfg().sym(a.addr) if a.addr else None
    length = num(a.length, '--len')
    if a.near:                              # a window around a known address beats a 32 MB pass
        win = num(a.window, '--window') or 0x10000
        centre = ee(cfg().sym(a.near))
        addr, length = max(0, centre - win // 2), win
    if a.cmd == 'wrote':
        return _wrote(a)
    p = Pine(check=not a.any_serial)

    if a.cmd == 'vsync':
        find_vsync(p, addr, length, top=a.top, save=a.save)
        return None
    if a.cmd == 'find':
        find_changed(p, mode=a.mode, equals=a.equals, kind=a.kind, addr=addr, length=length,
                     reset=a.reset, tol=a.tol, limit=a.top * 5)
        return None
    if a.cmd == 'watch':
        from ps2kit.pine import watch
        watch(p, a.args, kind=a.kind, frames=a.frames)
        return None
    if a.cmd == 'chase':
        if not a.args:
            raise SystemExit('kit probe chase ADDR OFFSET…')
        chase(p, cfg().sym(a.args[0]), [num(x, 'offset') for x in a.args[1:]])
        return None
    return _scan(p, a)


def _wrote(a) -> int:
    """`probe wrote ADDR` — which code last stored to an address.

    PINE cannot answer this: it reads and writes memory, and has no breakpoint or watchpoint op in any
    released PCSX2. Rather than pretend, say where the answer actually lives.
    """
    addr = cfg().sym(a.args[0]) if a.args else 0
    print(f'probe wrote: PCSX2\'s PINE protocol has no watchpoint op, so the kit cannot tell you what '
          f'wrote {addr:#x}.\n'
          '  In PCSX2: Debug > Open Debugger, right-click Memory > Add breakpoint, Write, '
          f'address {addr:#x}; it halts on the storing instruction and the register window shows who.\n'
          '  Then `kit fn <that address>` to read the function it lands in.', file=sys.stderr)
    return 2


def _scan(p: Pine, a) -> None:
    """Dump a region, typed or as hex. Capped on screen; the whole thing goes to a file (INTERNALS rule 3)."""
    if len(a.args) < 2:
        raise SystemExit('kit probe scan ADDR LEN [--as u32|f32|hex]')
    from ps2kit.pine import decode
    addr, n = ee(cfg().sym(a.args[0])), num(a.args[1], 'length')
    buf = p.read_block(addr, n)
    width = TYPES[a.kind]
    per = 16 if a.kind == 'hex' else max(1, 16 // width)
    lines = []
    for i in range(0, len(buf), width * per):
        vals = decode(buf[i:i + width * per], a.kind)
        if a.kind == 'hex':
            txt = ' '.join(f'{v:02x}' for v in vals)
            txt += '  ' + ''.join(chr(v) if 32 <= v < 127 else '.' for v in vals)
        elif a.kind in ('f32', 'f64'):
            txt = ' '.join(f'{v:>14.6g}' for v in vals)
        else:
            txt = ' '.join(f'{v:>{width * 2}x}' for v in vals)
        lines.append(f'{addr + i:08x}  {txt}')
    out = cfg().ctx('probe', f'scan-{addr:08x}.txt')
    out.write_text('\n'.join(lines) + '\n')
    for l in lines[:48]:
        print(l)
    if len(lines) > 48:
        print(f'… {len(lines) - 48} more lines in {out}')
