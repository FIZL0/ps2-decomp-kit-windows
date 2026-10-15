"""Talk to a running PCSX2 over PINE — the kit's only window into the live game.

Why this module exists: the decompilation tells you what the code *does*; only the running game tells you
what it actually computed this frame. Every capture, every "does my port agree with the original" check and
every address hunt goes through this one client, so the awkward parts are solved once, here:

* **The serial guard.** PINE will happily read whatever disc is booted. A capture made against the wrong
  region or revision is not an error, it is *silently wrong data* that you will trust for a week. So every
  connection compares PCSX2's game id to `[game] serial` in kit.toml and refuses to go on otherwise.
  `--any-serial` / `Pine(check=False)` is there for deliberate cross-version comparisons.
* **The lock.** One PINE client at a time: a second client connecting while a recorder polls makes both hang
  (PCSX2 serves one socket). `platform_.Lock('pcsx2')` is taken on construction and held until the process
  exits, so tools queue instead of corrupting each other's reads.
* **Nothing spins forever.** The socket has a timeout (a frozen emulator must fail, not hang) and
  `next_frame()` gives up if the frame counter stops moving — otherwise a recorder whose scene has ended sits
  on the lock until someone notices.
* **Loud failure.** Importing this module installs a `sys.excepthook` that turns any crash into one line
  starting `CAPTURE FAILED:`. Half-written capture files that look complete have cost more time than any
  other bug in this kind of work.

Reads race the EE: PCSX2 keeps running while it answers, so a multi-word read can straddle a game tick. Use
`settle(regions, frame)` for anything that has to be self-consistent, and `read_regions()` to get a whole
struct in one round trip.
"""
from __future__ import annotations

import os
import socket
import struct
import sys
import threading
import time
from pathlib import Path

from ps2kit import platform_ as plat
from ps2kit.config import cfg, num

# PINE opcodes (PCSX2's IPC protocol). A message is <u32 total size><op><args...>; a reply is
# <u32 total size><u8 result><payload...>. Several ops may be packed into one message: same framing,
# one result byte, payloads concatenated — that is what read_regions() exploits.
MSG_READ = {1: 0x00, 2: 0x01, 4: 0x02, 8: 0x03}
MSG_WRITE = {1: 0x04, 2: 0x05, 4: 0x06, 8: 0x07}
MSG_VERSION, MSG_SAVE, MSG_LOAD = 0x08, 0x09, 0x0A
MSG_TITLE, MSG_ID, MSG_UUID, MSG_GAMEVER, MSG_STATUS = 0x0B, 0x0C, 0x0D, 0x0E, 0x0F
RESULT_OK = 0

# One PINE message carries a bounded request and reply (PCSX2 drops oversized ones). 4096 read64 ops is a
# 20 KB request for a 32 KB reply — comfortably inside any version's limit, and still only 8 round trips
# per megabyte.
OPS_PER_MSG = 4096

STALL = float(os.environ.get('KIT_FRAME_STALL') or 10)   # s without a frame tick = something ended
FRAME_SETTLE = float(os.environ.get('KIT_FRAME_DELAY') or 0.004)  # s after the tick, before reading

TYPES = {'u8': 1, 'u16': 2, 'u32': 4, 'u64': 8, 'i8': 1, 'i16': 2, 'i32': 4, 'i64': 8,
         'f32': 4, 'f64': 8, 'hex': 1}
_FMT = {'u8': 'B', 'u16': 'H', 'u32': 'I', 'u64': 'Q', 'i8': 'b', 'i16': 'h', 'i32': 'i', 'i64': 'q',
        'f32': 'f', 'f64': 'd'}


# --- loud failure ------------------------------------------------------------

def _loud(kind, err, tb):
    if issubclass(kind, KeyboardInterrupt):
        return sys.__excepthook__(kind, err, tb)
    sys.exit(f'CAPTURE FAILED: {kind.__name__}: {err} — anything this run wrote is incomplete.\n'
             'Is PCSX2 up with PINE on (kit pcsx2 start) and still responding?')


sys.excepthook = _loud   # set on import: every tool that touches live RAM gets it, without remembering to


# --- the lock, held for the life of the process ------------------------------

_lock: plat.Lock | None = None


def take_lock(note: str = '') -> None:
    """Give this process PCSX2 to itself until it exits. Honours KIT_PCSX2_LOCKED (a parent holds it)."""
    global _lock
    if _lock is not None or os.environ.get('KIT_PCSX2_LOCKED'):
        return
    inst = plat.instance()
    _lock = plat.Lock('pcsx2', note or f'another process is using PCSX2{" copy " + inst if inst else ""}')
    _lock.take()
    if cap := int(num(os.environ.get('KIT_PCSX2_MAX_HOLD'), 'KIT_PCSX2_MAX_HOLD') or 0):
        _cap_hold(cap)


def _cap_hold(seconds: int) -> None:
    """Stop this process after `seconds` of holding the lock, so one hung tool can't starve other agents."""
    msg = (f'pine: held PCSX2 for {seconds}s (the KIT_PCSX2_MAX_HOLD cap); stopping. '
           'Split the job into shorter runs.')
    import signal
    if hasattr(signal, 'SIGALRM'):
        signal.signal(signal.SIGALRM, lambda *_: sys.exit(msg))
        signal.alarm(seconds)
        return

    def blow():  # pragma: no cover - Windows; sys.exit in a thread would only end the thread
        print(msg, file=sys.stderr, flush=True)
        os._exit(3)

    t = threading.Timer(seconds, blow)
    t.daemon = True
    t.start()


# --- the client --------------------------------------------------------------

class Pine:
    """A connected PINE client. One per process; constructing it takes the PCSX2 lock.

        p = Pine()
        p.read32(cfg().sym('@player_x'))
        f = p.next_frame(p.frame())
        p.settle([(base, 0x80)], f)
    """

    def __init__(self, check: bool = True, slot: int | None = None, timeout: float = 10.0,
                 lock: bool = True):
        c = cfg()
        self.slot = slot if slot is not None else c.pine_slot + int(plat.instance() or 0)
        if lock:
            take_lock()
        family, addr = plat.pine_endpoint(self.slot)
        self.s = socket.socket(family)
        plat.tune_socket(self.s, family)   # TCP needs Nagle off, or a per-frame poll crawls
        self.s.settimeout(timeout)   # a frozen emulator must fail, not hang holding the lock
        try:
            self.s.connect(addr)
        except OSError as e:
            where = addr if isinstance(addr, str) else ':'.join(str(x) for x in addr)
            raise SystemExit(f'no PINE server on slot {self.slot} ({where}): {e}\n'
                             'Start the emulator with PINE on:  kit pcsx2 start')
        self._vs: int | None = None
        self._vs_t = 0.0
        if check:
            self.check_serial()

    # --- framing ---------------------------------------------------------
    def _recv(self, n: int) -> bytes:
        b = b''
        while len(b) < n:
            c = self.s.recv(n - len(b))
            if not c:
                raise ConnectionError('PINE closed the connection (did PCSX2 exit?)')
            b += c
        return b

    def _send(self, ops: bytes) -> bytes:
        self.s.sendall(struct.pack('<I', 4 + len(ops)) + ops)
        size, res = struct.unpack('<IB', self._recv(5))
        body = self._recv(size - 5)
        if res != RESULT_OK:
            raise RuntimeError(f'PINE refused the request (result {res}; op {ops[0]:#x}) — '
                               'wrong address, or nothing booted?')
        return body

    def _call(self, op: int, args: bytes = b'') -> bytes:
        return self._send(bytes([op]) + args)

    def _str(self, op: int) -> str:
        return self._call(op)[4:].rstrip(b'\0').decode('latin-1')   # <u32 len><chars...>

    # --- scalars ---------------------------------------------------------
    def read(self, addr: int, width: int) -> int:
        return int.from_bytes(self._call(MSG_READ[width], struct.pack('<I', addr)), 'little')

    def write(self, addr: int, value: int, width: int) -> None:
        self._call(MSG_WRITE[width], struct.pack('<I', addr) + (value & (2 ** (width * 8) - 1))
                   .to_bytes(width, 'little'))

    def read8(self, a: int) -> int:
        return self.read(a, 1)

    def read16(self, a: int) -> int:
        return self.read(a, 2)

    def read32(self, a: int) -> int:
        return self.read(a, 4)

    def read64(self, a: int) -> int:
        return self.read(a, 8)

    def write8(self, a: int, v: int) -> None:
        self.write(a, v, 1)

    def write16(self, a: int, v: int) -> None:
        self.write(a, v, 2)

    def write32(self, a: int, v: int) -> None:
        self.write(a, v, 4)

    def write64(self, a: int, v: int) -> None:
        self.write(a, v, 8)

    def read_f32(self, a: int) -> float:
        return struct.unpack('<f', self.read(a, 4).to_bytes(4, 'little'))[0]

    def write_f32(self, a: int, v: float) -> None:
        self.write(a, struct.unpack('<I', struct.pack('<f', v))[0], 4)

    # --- blocks ----------------------------------------------------------
    def read_block(self, addr: int, n: int) -> bytes:
        """`n` bytes from `addr`, as few PINE messages as the size limit allows."""
        return self.read_regions([(addr, n)])

    def read_regions(self, regions: list[tuple[int, int]]) -> bytes:
        """Several (addr, length) blocks, their bytes concatenated, batched into one message each 4096 ops.

        One round trip per batch is what makes per-frame struct captures possible at all: the same read done
        word by word takes thousands of round trips and the frame has long since ticked.
        """
        ops: list[bytes] = []
        for addr, n in regions:
            a, left = addr, n
            while left >= 8:          # read64 is the cheapest byte-per-op; the tail falls back to read8
                ops.append(struct.pack('<BI', MSG_READ[8], a))
                a, left = a + 8, left - 8
            while left:
                ops.append(struct.pack('<BI', MSG_READ[1], a))
                a, left = a + 1, left - 1
        out = []
        for i in range(0, len(ops), OPS_PER_MSG):
            out.append(self._send(b''.join(ops[i:i + OPS_PER_MSG])))
        return b''.join(out)

    def settle(self, regions: list[tuple[int, int]], frame: int) -> bytes | None:
        """`read_regions` until two reads agree inside frame `frame`; None if the frame ticks first.

        PCSX2 does not stop the EE to answer PINE, so a long read can span a game tick and come back with
        half of one frame and half of the next. Two identical reads inside the same frame is the cheap
        proof that what you have is one consistent snapshot. None means "skip this frame" — at full speed
        a heavy region can tick on every attempt, and waiting for the same frame again would never return.
        """
        a = self.read_regions(regions)
        while True:
            b = self.read_regions(regions)
            if self.frame() != frame:
                return None
            if a == b:
                return a
            a = b

    # --- frames ----------------------------------------------------------
    def frame(self, addr: int | None = None) -> int:
        """The game's own per-frame counter ([emu] vsync_addr; `kit probe vsync` finds it)."""
        v = self.read32(addr if addr is not None else cfg().need_vsync())
        self._watch(v)
        return v

    def next_frame(self, last: int, delay: float = FRAME_SETTLE, addr: int | None = None) -> int:
        """The first frame after `last`: poll the counter, then wait `delay` for the game's tick to finish.

        The delay matters: a read taken the instant the counter moves still sees last frame's values in
        anything the game writes after its own vsync handler.
        """
        a = addr if addr is not None else cfg().need_vsync()
        while (v := self.read32(a)) == last:
            self._watch(v)
        self._watch(v)
        time.sleep(delay)
        return v

    def _watch(self, v: int) -> None:
        """Bail out if the counter stops. Every recorder polls it; when the scene ends (or someone pauses
        PCSX2) it never ticks again and the recorder would spin forever holding the lock."""
        now = time.monotonic()
        if v != self._vs:
            self._vs, self._vs_t = v, now
        elif now - self._vs_t > STALL:
            if self.resume():
                self._vs_t = now
                return
            sys.exit(f'pine: frame counter stuck at {v} for {STALL:g}s — the game is paused or whatever you '
                     'were recording has ended. Stopping; what was written so far is flushed.')

    # --- emulator state --------------------------------------------------
    def version(self) -> str:
        return self._str(MSG_VERSION)

    def title(self) -> str:
        return self._str(MSG_TITLE)

    def game_id(self) -> str:
        return self._str(MSG_ID)

    def game_version(self) -> str:
        return self._str(MSG_GAMEVER)

    def status(self) -> str:
        return ['running', 'paused', 'shutdown'][struct.unpack('<I', self._call(MSG_STATUS))[0]]

    def save_state(self, slot: int) -> None:
        self._call(MSG_SAVE, bytes([slot]))

    def load_state(self, slot: int) -> None:
        self._call(MSG_LOAD, bytes([slot]))

    def check_serial(self) -> None:
        """Refuse to read a different disc than kit.toml describes. The kit's single most valuable check."""
        want, got = cfg().serial, self.game_id()
        if not want:
            print('pine: [game] serial is empty in kit.toml — reading whatever is booted, unchecked',
                  file=sys.stderr)
            return
        if _plain(got) != _plain(want):
            raise SystemExit(f'wrong disc: PCSX2 has {got or "nothing"} booted, kit.toml says {want}.\n'
                             'A capture against another version is silently wrong data, so this is fatal.\n'
                             'Boot the right image (kit pcsx2 start), or pass --any-serial on purpose.')

    # --- pausing a kit copy ----------------------------------------------
    # A kit-managed copy binds TogglePause to its virtual pad's Guide button (see emu.copy_config), which is
    # the only way to pause it without a window to click. The user's own emulator is never touched.
    def pause(self) -> bool:
        return self._toggle('paused')

    def resume(self) -> bool:
        return self._toggle('running')

    def _toggle(self, want: str) -> bool:
        if not plat.instance() or self.status() == want or self.status() == 'shutdown':
            return False
        from ps2kit import pad
        pad.send(['press guide 80'], quiet=True)
        for _ in range(30):
            if self.status() == want:
                print(f'pine: PCSX2 is now {want}', file=sys.stderr, flush=True)
                return True
            time.sleep(0.1)
        print(f'pine: the pad Guide button did not make PCSX2 {want} (is `kit pad serve` up for this copy?)',
              file=sys.stderr)
        return False


def _plain(serial: str) -> str:
    return ''.join(ch for ch in serial.upper() if ch.isalnum())


# --- value formatting --------------------------------------------------------

def decode(buf: bytes, kind: str) -> list:
    """Bytes → a list of values of `kind` ('hex' gives one int per byte)."""
    if kind == 'hex':
        return list(buf)
    w, f = TYPES[kind], _FMT[kind]
    return [struct.unpack_from('<' + f, buf, i)[0] for i in range(0, len(buf) - w + 1, w)]


def show(v, kind: str) -> str:
    if kind in ('f32', 'f64'):
        return f'{v:.9g}'
    if kind == 'hex':
        return f'{v:02x}'
    w = TYPES[kind]
    return f'{v}' if kind.startswith('i') else f'{v} ({v:#0{2 + w * 2}x})'


def encode(text: str, kind: str) -> tuple[int, int]:
    """A command-line value → (integer to write, width in bytes), floats via their bit pattern."""
    if kind == 'f32':
        return struct.unpack('<I', struct.pack('<f', float(text)))[0], 4
    if kind == 'f64':
        return struct.unpack('<Q', struct.pack('<d', float(text)))[0], 8
    return num(text, 'value'), TYPES[kind]


def add_type_flags(ap) -> None:
    """`--f32` / `--u32 5` as a shorthand for `--as f32` on any kit command that takes a value type.

    Hidden from --help on purpose: eleven near-identical lines would bury the flags that need reading.
    The DOC string above each command mentions the shorthand instead.
    """
    import argparse
    for t in sorted(TYPES):
        ap.add_argument(f'--{t}', dest=f'v_{t}', nargs='?', const='', metavar='VALUE',
                        help=argparse.SUPPRESS)


def type_flag(a) -> tuple[str | None, str | None]:
    """-> (kind, the value given with it), either of which may be None."""
    for t in sorted(TYPES):
        v = getattr(a, f'v_{t}', None)
        if v is not None:
            return t, (v or None)
    return None, None


# --- CLI ---------------------------------------------------------------------

DOC = """read and write the live game's RAM over PCSX2's PINE socket.

  kit pine                             version, disc, status and the frame counter
  kit pine read @player_x --as f32     one value by [symbols] name (--f32 means the same)
  kit pine read 1004c0 4               four u32s from an address
  kit pine read 1004c0 --len 64        64 bytes, dumped on screen
  kit pine write @flags 1 --as u8
  kit pine block 1c0000 0x200 --out context/probe/dump.bin
  kit pine watch @score @frame --frames 120
  kit pine save 8   ·   kit pine load 8      (slots 8/9 are the kit's scratch slots)
  kit pine info

Addresses: hex, 0x-hex, or @name from [symbols] in kit.toml."""


SUBS = ['read', 'write', 'block', 'watch', 'save', 'load', 'info']


def cmd_pine(argv: list[str]) -> int | None:
    from ps2kit.cli import parser
    # `kit pine 1004c0 4` means `read`: typing the subcommand for the commonest case is pure friction.
    if argv and argv[0] not in SUBS and not argv[0].startswith('-'):
        argv = ['read'] + argv
    ap = parser('pine', DOC)
    ap.add_argument('cmd', nargs='?', default='info', choices=SUBS)
    ap.add_argument('args', nargs='*', help='addresses, counts, values — see above')
    ap.add_argument('--as', dest='kind', default='u32', choices=sorted(TYPES),
                    help='value type (default u32)')
    add_type_flags(ap)
    ap.add_argument('--len', dest='length', metavar='N',
                    help='read: a length in bytes instead of a count of values')
    ap.add_argument('--out', metavar='FILE', help='write bytes/rows to a file instead of the terminal')
    ap.add_argument('--frames', type=int, default=60, help='how many frames `watch` samples (default 60)')
    ap.add_argument('--any-serial', action='store_true',
                    help='skip the kit.toml serial check (deliberate cross-version work only)')
    a = ap.parse_args(argv)
    kflag, inline = type_flag(a)
    a.kind = k = kflag or a.kind
    # Nothing above touched kit.toml, so --help works in a bare checkout. From here on, it is needed.
    p = Pine(check=not a.any_serial)

    if a.cmd == 'info':
        return _info(p)
    if a.cmd in ('save', 'load'):
        if not a.args:
            raise SystemExit(f'kit pine {a.cmd} N — which save-state slot? (8 and 9 are scratch)')
        slot = int(a.args[0])
        (p.save_state if a.cmd == 'save' else p.load_state)(slot)
        print(f'{a.cmd}d state slot {slot}')
        return None
    if a.cmd == 'read':
        if not a.args:
            raise SystemExit('kit pine read ADDR [count] [--as u32]')
        addr = cfg().sym(a.args[0])
        if a.length:
            return _dump(p, addr, num(a.length, '--len'), k, a.out)
        count = int(num(a.args[1], 'count') or 1) if len(a.args) > 1 else 1
        buf = p.read_block(addr, TYPES[k] * count)
        for i, v in enumerate(decode(buf, k)):
            print(f'{addr + i * TYPES[k]:08x}  {show(v, k)}')
        return None
    if a.cmd == 'write':
        # `write ADDR VALUE --as u32` and `write ADDR --u32 VALUE` are the same thing.
        text = a.args[1] if len(a.args) > 1 else inline
        if not a.args or text is None:
            raise SystemExit('kit pine write ADDR VALUE [--as u32]')
        addr = cfg().sym(a.args[0])
        value, width = encode(text, k)
        p.write(addr, value, width)
        print(f'{addr:08x}  <- {show(decode(p.read_block(addr, width), k)[0], k)}')
        return None
    if a.cmd == 'block':
        if len(a.args) < 2:
            raise SystemExit('kit pine block ADDR LEN [--out FILE]')
        addr, n = cfg().sym(a.args[0]), num(a.args[1], 'length')
        buf = p.read_block(addr, n)
        out = Path(a.out) if a.out else cfg().ctx('pine', f'{addr:08x}-{n:x}.bin')
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(buf)
        print(f'{len(buf)} bytes from {addr:08x} -> {out}')
        return None
    if a.cmd == 'watch':
        return watch(p, a.args, kind=k, frames=a.frames, out=a.out)
    return None


def _dump(p: Pine, addr: int, n: int, kind: str, out: str | None) -> None:
    """A region on screen, capped; with --out the bytes go to a file as well (INTERNALS rule 3)."""
    buf = p.read_block(addr, n)
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(buf)
    width = TYPES[kind]
    per = 16 if kind == 'hex' else max(1, 16 // width)
    rows = []
    for i in range(0, len(buf) - width + 1, width * per):
        vals = decode(buf[i:i + width * per], kind)
        if kind == 'hex':
            txt = ' '.join(f'{v:02x}' for v in vals)
            txt += '  ' + ''.join(chr(v) if 32 <= v < 127 else '.' for v in vals)
        else:
            txt = ' '.join(f'{show(v, kind).split(" ")[0]:>12}' for v in vals)
        rows.append(f'{addr + i:08x}  {txt}')
    for r in rows[:32]:
        print(r)
    if len(rows) > 32:
        print(f'... {len(rows) - 32} more lines; --out FILE writes all {len(buf)} bytes')
    if out:
        print(f'({len(buf)} bytes -> {out})')


def _info(p: Pine) -> None:
    c = cfg()
    print(f'PCSX2    {p.version()}   [{p.status()}]')
    print(f'disc     {p.game_id()} {p.game_version()}  {p.title()}')
    print(f'kit.toml {c.serial or "(no serial set)"}  slot {p.slot}')
    if c.vsync is None:
        print('frame    [emu] vsync_addr not set — kit probe vsync --save')
    else:
        f0 = p.read32(c.vsync)
        time.sleep(0.5)
        print(f'frame    {c.vsync:#x} = {f0} -> {p.read32(c.vsync)} after 0.5 s')


def watch(p: Pine, names: list[str], kind: str = 'u32', frames: int = 60,
          out: str | None = None) -> None:
    """One line per frame for each address. Long runs go to a CSV rather than into the caller's context
    (INTERNALS rule 3). `names` are whatever `cfg().sym` takes, so `@player_x` works."""
    if not names:
        raise SystemExit('watch needs at least one address: ADDR... [--frames N]')
    addrs = [cfg().sym(s) for s in names]
    width = TYPES[kind]
    regions = [(x, width) for x in addrs]   # one PINE message per frame, however many addresses
    rows: list[str] = []
    f = p.frame()
    for _ in range(max(1, frames)):
        f = p.next_frame(f)
        buf = p.read_regions(regions)
        vals = [decode(buf[i * width:(i + 1) * width], kind)[0] for i in range(len(addrs))]
        rows.append(f'{f},' + ','.join(f'{v:.9g}' if isinstance(v, float) else str(v) for v in vals))
    head = 'frame,' + ','.join(str(s).lstrip('@') for s in names)
    out = Path(out) if out else (cfg().ctx('pine', 'watch.csv') if len(rows) > 40 else None)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text('\n'.join([head] + rows) + '\n')
    print(head)
    for r in (rows if len(rows) <= 40 else rows[:20] + [f'... {len(rows) - 40} frames omitted ...'] + rows[-20:]):
        print(r)
    if out:
        print(f'({len(rows)} frames -> {out})')
