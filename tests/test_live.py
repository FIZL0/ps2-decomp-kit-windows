"""What the live layer promises, proved against a fake emulator this file builds itself.

The live layer is everything that talks to a running PCSX2: the PINE client, the virtual pad, the RAM
search and the launcher. It is the half of the kit that cannot be checked by reading its output, because
the output *looks* right either way — a capture made against the wrong disc, a struct read that straddled
a frame tick, a `select` that quietly loaded a save state, a launch whose ini no longer has PINE on. Each
of those produces plausible numbers and costs a day. So the properties here are the invariants that make
live data trustworthy at all:

* `pine` frames every op the way PCSX2 expects, batches a block read into as few round trips as the
  protocol allows, refuses a disc that is not the one in kit.toml, and *stops* instead of spinning when
  the frame counter dies. Nothing here may hang: a hung client holds the lock on the only emulator.
* `pad` turns a text script into button events, counts durations in milliseconds unless told frames, and
  refuses `select` — PCSX2's own hotkeys are Select plus a shoulder button, so sending it saves or loads
  a state behind the script's back.
* `probe` picks the real frame counter out of a region full of things that also climb, and narrows a value
  hunt only by what the passes actually prove.
* `emu` writes PINE into the ini on every launch, keeps the user's own emulator and a kit copy apart in
  both argv and environment, finds a save state by slot, and reads a frozen RAM image out of one.

The emulator here is a thread on a local socket speaking PINE, with words in its RAM that climb at rates
this file chooses. Nothing reaches the real PCSX2, a real pad device, the network, or any file outside one
temp directory; no game data, no real serial, no real address.

Run: python3 tests/test_live.py        (V=1 for tracebacks)
"""
from __future__ import annotations

import io
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

WORK = Path(tempfile.mkdtemp(prefix='ps2kit-live-'))
os.environ['XDG_RUNTIME_DIR'] = str(WORK / 'run')           # POSIX: locks, fifos and the PINE socket
os.environ['LOCALAPPDATA'] = str(WORK)                      # Windows: the same things, under WORK\ps2kit
(WORK / 'run').mkdir(parents=True, exist_ok=True)
os.environ.pop('KIT_PCSX2', None)                            # the user's own emulator, unless a test says so
os.environ.pop('KIT_PCSX2_LOCKED', None)
os.environ.pop('KIT_PCSX2_MAX_HOLD', None)

KIT = Path(__file__).resolve().parents[1]                     # the repo root, for a probe subprocess
sys.path.insert(0, str(KIT))

import ps2kit.config as CFG                                  # noqa: E402 - after the path and the env
from ps2kit import emu as EMU                                # noqa: E402
from ps2kit import pad as PAD                                # noqa: E402
from ps2kit import pine as PINE                              # noqa: E402
from ps2kit import platform_ as PLAT                         # noqa: E402
from ps2kit import probe as PROBE                            # noqa: E402

RUN = PLAT.runtime_dir()      # locks and pad markers, wherever this OS puts them -- inside WORK either way

TESTS: list[tuple[str, object]] = []
PASS, FAIL = [], []


def check(name):
    """Register a test. Definition order is run order, so the cheap socket-free tests fail first."""
    def deco(fn):
        TESTS.append((name, fn))
        return fn
    return deco


def out_of(fn, *a, **kw):
    """(return value, stdout, stderr) — the kit's live tools say what they did, and that is half the API."""
    o, e = io.StringIO(), io.StringIO()
    with redirect_stdout(o), redirect_stderr(e):
        rv = fn(*a, **kw)
    return rv, o.getvalue(), e.getvalue()


def raises(exc, fn, *a, **kw):
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            fn(*a, **kw)
    except exc as e:
        return e
    raise AssertionError(f'{getattr(fn, "__name__", fn)} did not raise {exc.__name__}')


# --- a kit.toml that exists only in memory -----------------------------------

RAM = 0x20_0000          # 2 MiB of fake EE RAM: enough for a region scan, small enough to allocate freely
FRAME = 0x1000           # where the fake game keeps its frame counter


class FakeCfg:
    """Stands in for `config.Config`. Every module binds `cfg` at import, so a test swaps that attribute."""

    name, region, version = 'Synthetic Game', 'US', '1.00'
    ram, instances, pcsx2 = RAM, 4, '/nonexistent/pcsx2-qt'
    pine_slot = 28011
    speed = 1.0

    def __init__(self, root: Path, serial: str = 'SLUS-12345', vsync: int | None = FRAME):
        self.root = root
        self.serial = serial
        self.vsync = vsync
        self.states: dict[str, str] = {'3': 'before the first point', '7': 'match point'}
        self.symbols: dict[str, int] = {'frame': FRAME, 'table': 0x4000, 'base': 0x3000}
        self.wrote: list[tuple[str, str, object]] = []
        root.mkdir(parents=True, exist_ok=True)

    def ctx(self, *parts: str) -> Path:
        p = self.root / 'context'
        for part in parts:
            p = p / part
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def need_vsync(self) -> int:
        if self.vsync is None:
            raise SystemExit('probe vsync first')
        return self.vsync

    def need_iso(self) -> Path:
        iso = self.root / 'disc.iso'
        iso.write_bytes(b'\0' * 16)
        return iso

    def sym(self, s: str) -> int:
        s = str(s)
        if s.startswith('@'):
            return self.symbols[s[1:]]
        return int(s, 16)

    def set(self, table: str, key: str, value) -> None:
        self.wrote.append((table, key, value))


def use_cfg(c: FakeCfg, *mods) -> FakeCfg:
    for mod in (mods or (PINE, PROBE, EMU, CFG)):
        mod.cfg = lambda required=True, _c=c: _c
    return c


# --- the fake emulator -------------------------------------------------------

WIDTH_OF = {op: w for w, op in PINE.MSG_READ.items()}
WRITE_WIDTH = {op: w for w, op in PINE.MSG_WRITE.items()}
STATUS_CODE = {'running': 0, 'paused': 1, 'shutdown': 2}


def _fake_endpoint(tag: int) -> tuple[int, object]:
    """Where a `FakePcsx2` listens: the transport the client really uses on this OS, so the code under
    test takes the same path it takes against PCSX2. A unix socket in the runtime dir; on Windows, which
    has none, loopback TCP on port 0 -- the OS picks a free one and `bind` reads it back."""
    if PLAT.WINDOWS:
        return socket.AF_INET, ('127.0.0.1', 0)
    return socket.AF_UNIX, str(PLAT.runtime_dir() / f'pcsx2.sock.test{tag}')


def _dead_endpoint() -> tuple[int, object]:
    """An address of the right kind with nothing listening on it: a socket path nobody bound, or a
    loopback port that was held just long enough to learn it was free."""
    if PLAT.WINDOWS:
        s = socket.socket(socket.AF_INET)
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
        s.close()
        return socket.AF_INET, ('127.0.0.1', port)
    return socket.AF_UNIX, str(RUN / 'nobody-here.sock')


class FakePcsx2:
    """A PINE server: one connection at a time, one reply per message, exactly like PCSX2's.

    What it adds over an echo: words in RAM whose value is a function of elapsed time or of how often they
    have been read, so a test can plant a 60 Hz counter, a 30 Hz one and one that stops climbing — and the
    client has no way to tell them apart except by doing its job.
    """

    def __init__(self, game_id: str = 'SLUS_123.45', status: str = 'running'):
        self.ram = bytearray(RAM)
        self.game_id = game_id
        self.status = status
        self.messages: list[list[int]] = []        # the ops of each message, in order
        self.saved: list[int] = []
        self.loaded: list[int] = []
        self.planted: dict[int, object] = {}       # addr -> f(server) -> u32, refreshed before each message
        self.reads: Counter = Counter()
        self.t0 = time.monotonic()
        self.family, self.addr = _fake_endpoint(id(self))
        self.srv = socket.socket(self.family)
        self.srv.bind(self.addr)
        if self.family == socket.AF_INET:
            self.addr = self.srv.getsockname()         # the loopback port the OS just picked
        self.path = self.addr if isinstance(self.addr, str) else '%s:%d' % self.addr
        self.srv.listen(2)
        self.stop = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.t0

    def poke(self, addr: int, *values: int, width: int = 4) -> None:
        for i, v in enumerate(values):
            self.ram[addr + i * width:addr + (i + 1) * width] = v.to_bytes(width, 'little')

    def peek(self, addr: int, width: int = 4) -> int:
        return int.from_bytes(self.ram[addr:addr + width], 'little')

    def close(self) -> None:
        self.stop = True
        self.srv.close()

    # --- the protocol ----------------------------------------------------
    def _serve(self) -> None:
        while not self.stop:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            # The real PCSX2 answers one socket at a time (which is what the kit's lock is for); this
            # answers each connection on its own thread, so a client that died mid-handshake in one test
            # cannot wedge the next one.
            threading.Thread(target=self._talk, args=(conn,), daemon=True).start()

    def _talk(self, conn) -> None:
            with conn:
                while not self.stop:
                    head = self._recv(conn, 4)
                    if not head:
                        break
                    size = struct.unpack('<I', head)[0]
                    body = self._recv(conn, size - 4)
                    if body is None:
                        break
                    try:
                        payload = self._run(body)
                    except Exception:                       # a refused op is a result byte, not a crash
                        conn.sendall(struct.pack('<IB', 5, 1))
                        continue
                    conn.sendall(struct.pack('<IB', 5 + len(payload), PINE.RESULT_OK) + payload)

    @staticmethod
    def _recv(conn, n: int):
        b = b''
        while len(b) < n:
            c = conn.recv(n - len(b))
            if not c:
                return None if b else b''
            b += c
        return b

    def _refresh(self) -> None:
        for addr, fn in self.planted.items():
            self.ram[addr:addr + 4] = (fn(self) & 0xffffffff).to_bytes(4, 'little')

    def _run(self, body: bytes) -> bytes:
        self._refresh()
        ops, out, i = [], b'', 0
        while i < len(body):
            op = body[i]
            ops.append(op)
            i += 1
            if op in WIDTH_OF:
                w = WIDTH_OF[op]
                addr = struct.unpack_from('<I', body, i)[0]
                i += 4
                self.reads[addr] += 1
                if addr + w > len(self.ram):
                    raise ValueError('out of range')
                out += bytes(self.ram[addr:addr + w])
            elif op in WRITE_WIDTH:
                w = WRITE_WIDTH[op]
                addr = struct.unpack_from('<I', body, i)[0]
                i += 4
                self.ram[addr:addr + w] = body[i:i + w]
                i += w
            elif op in (PINE.MSG_VERSION, PINE.MSG_TITLE, PINE.MSG_ID, PINE.MSG_GAMEVER, PINE.MSG_UUID):
                s = {PINE.MSG_VERSION: '2.9.9-fake', PINE.MSG_TITLE: 'Synthetic Game',
                     PINE.MSG_ID: self.game_id, PINE.MSG_GAMEVER: '1.00',
                     PINE.MSG_UUID: '0000'}[op].encode('latin-1') + b'\0'
                out += struct.pack('<I', len(s)) + s
            elif op == PINE.MSG_STATUS:
                out += struct.pack('<I', STATUS_CODE[self.status])
            elif op in (PINE.MSG_SAVE, PINE.MSG_LOAD):
                (self.saved if op == PINE.MSG_SAVE else self.loaded).append(body[i])
                i += 1
            else:
                raise ValueError(f'unknown op {op:#x}')
        self.messages.append(ops)
        return out


def client(srv: FakePcsx2, **kw) -> PINE.Pine:
    """A Pine pointed at `srv`, with the lock off unless a test is about the lock."""
    kw.setdefault('check', False)
    kw.setdefault('lock', False)
    kw.setdefault('timeout', 5.0)
    real = PLAT.pine_endpoint
    PLAT.pine_endpoint = lambda slot: (srv.family, srv.addr)
    try:
        return PINE.Pine(**kw)
    finally:
        PLAT.pine_endpoint = real


# --- pine.py -----------------------------------------------------------------

@check('a crash during a capture prints one CAPTURE FAILED line, because a truncated file looks finished')
def _():
    assert sys.excepthook is PINE._loud, 'importing pine must install the hook, not ask to be told'
    e = raises(SystemExit, PINE._loud, ValueError, ValueError('short read'), None)
    assert str(e).startswith('CAPTURE FAILED: ValueError: short read')
    assert 'incomplete' in str(e) and 'PINE' in str(e)
    seen = []
    real = sys.__excepthook__
    sys.__excepthook__ = lambda *a: seen.append(a)
    try:
        PINE._loud(KeyboardInterrupt, KeyboardInterrupt(), None)
    finally:
        sys.__excepthook__ = real
    assert len(seen) == 1, 'ctrl-c is the user stopping a capture, not a failed one'


@check('each PCSX2 copy has its own PINE endpoint, so two agents never share one emulator')
def _():
    family, addr = PLAT.pine_endpoint(28011)
    _, addr2 = PLAT.pine_endpoint(28012)
    if PLAT.WINDOWS:
        # No unix sockets there: PCSX2 listens on loopback TCP and the slot *is* the port, so what keeps
        # two copies apart is 28011 + n rather than a socket file each.
        assert (family, addr) == (socket.AF_INET, ('127.0.0.1', 28011))
        assert addr2 == ('127.0.0.1', 28012)
    else:
        assert family == socket.AF_UNIX and str(addr).endswith('/pcsx2.sock')
        assert str(addr).startswith(str(RUN)), 'the runtime dir is respected, not hard-coded'
        assert str(addr2).endswith('/pcsx2.sock.28012')


@check('nothing listening says how to start the emulator instead of raising a socket error')
def _():
    use_cfg(FakeCfg(WORK / 'noserver'))
    real = PLAT.pine_endpoint
    PLAT.pine_endpoint = lambda slot: _dead_endpoint()
    try:
        e = raises(SystemExit, PINE.Pine, check=False, lock=False, timeout=0.5)
    finally:
        PLAT.pine_endpoint = real
    assert 'no PINE server on slot 28011' in str(e) and 'kit pcsx2 start' in str(e)


@check('every width reads and writes exactly the bytes the emulator holds, floats by their bit pattern')
def _():
    use_cfg(FakeCfg(WORK / 'widths'))
    srv = FakePcsx2()
    p = client(srv)
    srv.poke(0x2000, 0x0123456789abcdef, width=8)
    assert p.read8(0x2000) == 0xef and p.read16(0x2000) == 0xcdef
    assert p.read32(0x2000) == 0x89abcdef and p.read64(0x2000) == 0x0123456789abcdef
    for width, value in ((1, 0xa5), (2, 0xbeef), (4, 0xdeadbeef), (8, 0xfeedfacecafebabe)):
        p.write(0x2100, value, width)
        assert srv.peek(0x2100, width) == value and p.read(0x2100, width) == value
    p.write32(0x2200, 0x1ff)
    assert p.read32(0x2200) == 0x1ff
    p.write(0x2200, 0x1ff, 1)                       # a wider value is masked to the width, not refused
    assert p.read8(0x2200) == 0xff
    p.write_f32(0x2300, -1.5)
    assert srv.peek(0x2300) == 0xbfc00000 and p.read_f32(0x2300) == -1.5
    srv.close()


@check('a block read is one message of read64s with a byte tail, not one round trip per word')
def _():
    use_cfg(FakeCfg(WORK / 'block'))
    srv = FakePcsx2()
    p = client(srv)
    srv.ram[0x8000:0x8100] = bytes((i * 7 + 3) & 0xff for i in range(0x100))
    srv.messages.clear()
    assert p.read_block(0x8000, 13) == bytes(srv.ram[0x8000:0x800d])
    assert len(srv.messages) == 1, 'thirteen bytes must not cost thirteen round trips'
    assert srv.messages[0] == [PINE.MSG_READ[8]] + [PINE.MSG_READ[1]] * 5
    srv.messages.clear()
    assert p.read_regions([(0x8020, 16), (0x8000, 4), (0x8040, 8)]) == \
        bytes(srv.ram[0x8020:0x8030]) + bytes(srv.ram[0x8000:0x8004]) + bytes(srv.ram[0x8040:0x8048])
    assert len(srv.messages) == 1, 'a whole struct in one round trip is the point of read_regions'
    srv.close()


@check('a read past 4096 ops splits into several messages and comes back in one piece, in order')
def _():
    use_cfg(FakeCfg(WORK / 'batch'))
    srv = FakePcsx2()
    p = client(srv)
    n = PINE.OPS_PER_MSG * 8 + 8                      # one op more than a single message may carry
    srv.ram[0x10000:0x10000 + n] = bytes((i * 31) & 0xff for i in range(n))
    srv.messages.clear()
    got = p.read_block(0x10000, n)
    assert got == bytes(srv.ram[0x10000:0x10000 + n])
    assert [len(m) for m in srv.messages] == [PINE.OPS_PER_MSG, 1]
    assert all(len(m) <= PINE.OPS_PER_MSG for m in srv.messages)
    srv.close()


@check('settle keeps reading until two reads agree, and gives up on the frame rather than on consistency')
def _():
    use_cfg(FakeCfg(WORK / 'settle'))
    srv = FakePcsx2()
    p = client(srv)
    srv.planted[0x5000] = lambda s: min(s.reads[0x5000], 1)   # torn for one read, then steady
    srv.messages.clear()
    buf = p.settle([(0x5000, 8)], 0)
    assert buf is not None and struct.unpack_from('<I', buf)[0] == 1
    assert len(srv.messages) == 5, 'three region reads and two frame checks, no more'
    srv.planted[FRAME] = lambda s: s.reads[FRAME]             # the counter moves under the read
    assert p.settle([(0x6000, 8)], 99) is None, 'a snapshot from two frames is no snapshot'
    srv.close()


@check('the booted disc is checked against kit.toml, and the wrong one is fatal rather than a warning')
def _():
    c = use_cfg(FakeCfg(WORK / 'serial'))
    srv = FakePcsx2(game_id='SLUS_123.45')
    p = client(srv, check=True)
    assert p.game_id() == 'SLUS_123.45', 'the reply loses its length prefix and its NUL'
    assert p.version() == '2.9.9-fake' and p.title() == 'Synthetic Game' and p.status() == 'running'
    srv.close()

    other = FakePcsx2(game_id='SLUS_999.99')
    e = raises(SystemExit, client, other, check=True)
    assert 'wrong disc' in str(e) and 'SLUS_999.99' in str(e) and 'SLUS-12345' in str(e)
    assert '--any-serial' in str(e), 'the escape hatch has to be in the message that blocks you'
    p = client(other, check=False)                            # --any-serial: deliberate, so allowed
    assert p.game_id() == 'SLUS_999.99'
    c.serial = ''
    _, _, err = out_of(client(other).check_serial)
    assert 'serial is empty' in err and 'unchecked' in err
    other.close()


@check('the frame counter is read from kit.toml, and next_frame returns the first frame after it')
def _():
    use_cfg(FakeCfg(WORK / 'frames'))
    srv = FakePcsx2()
    srv.planted[FRAME] = lambda s: s.reads[FRAME]
    p = client(srv)
    assert p.frame() == 0
    assert p.next_frame(0, delay=0) == 1
    assert p.next_frame(1, delay=0) == 2
    assert p.frame(addr=0x7000) == 0, 'an explicit address beats the configured one'
    srv.close()


@check('a frame counter that has stopped ends the run instead of spinning on the PCSX2 lock')
def _():
    use_cfg(FakeCfg(WORK / 'stuck'))
    srv = FakePcsx2()
    srv.poke(FRAME, 7)
    p = client(srv)
    stall = PINE.STALL
    PINE.STALL = 0.05
    try:
        assert p.frame() == 7                                 # the first read only starts the clock
        time.sleep(0.06)
        e = raises(SystemExit, p.frame)
    finally:
        PINE.STALL = stall
    assert 'frame counter stuck at 7' in str(e) and 'flushed' in str(e)
    srv.close()


@check('save and load carry the slot number PCSX2 was asked for')
def _():
    use_cfg(FakeCfg(WORK / 'states'))
    srv = FakePcsx2()
    p = client(srv)
    p.save_state(8)
    p.save_state(9)
    p.load_state(8)
    assert srv.saved == [8, 9] and srv.loaded == [8]
    srv.close()


@check('a value is decoded and shown as the type asked for, and a written one parses back to its bits')
def _():
    assert PINE.decode(struct.pack('<ff', 1.5, -2.5), 'f32') == [1.5, -2.5]
    assert PINE.decode(b'\xff\x7f', 'i16') == [32767] and PINE.decode(b'\xff\x7f', 'u16') == [32767]
    assert PINE.decode(b'\xff\x00', 'hex') == [255, 0]
    assert PINE.show(255, 'u8') == '255 (0xff)' and PINE.show(-1, 'i32') == '-1'
    assert PINE.show(1.5, 'f32') == '1.5' and PINE.show(255, 'hex') == 'ff'
    assert PINE.encode('1.5', 'f32') == (0x3fc00000, 4)
    assert PINE.encode('0x10', 'u32') == (16, 4)
    # A *value* on the command line is decimal unless it says 0x; an *address* is hex either way. The two
    # go through different parsers on purpose, and swapping them silently writes 16 where 10 was meant.
    assert PINE.encode('10', 'u8') == (10, 1)
    assert FakeCfg(WORK / 'parse').sym('10') == 0x10


def _other_process_can_lock() -> bool:
    """Can a *separate* process take the pcsx2 lock right now? The question only means anything across
    processes: a kernel lock belongs to the open file description, so asking inside this one would answer
    about this one."""
    code = ('import sys; sys.path.insert(0, %r)\n'
            'from ps2kit import platform_ as p\n'
            'sys.exit(0 if p.Lock("pcsx2").take(block=False) else 1)\n' % str(KIT))
    r = subprocess.run([sys.executable, '-c', code], env={**os.environ, 'XDG_RUNTIME_DIR': str(WORK / 'run'),
                                            'LOCALAPPDATA': str(WORK)},
                       capture_output=True, text=True, timeout=30)
    assert r.returncode in (0, 1), f'the probe process failed: {r.returncode} {r.stderr.strip()}'
    return r.returncode == 0


@check('the lock is held for the life of the process, so a second tool waits instead of interleaving reads')
def _():
    use_cfg(FakeCfg(WORK / 'lock'))
    srv = FakePcsx2()
    assert not PLAT.Lock('pcsx2').held_by_other(), 'nothing holds it before a client exists'
    assert _other_process_can_lock(), 'and another process could take it'
    client(srv, lock=True)
    assert PINE._lock is not None and PINE._lock.path.parent == RUN
    # The holder must not see *itself* as a foreign holder — `kit pcsx2 status` would report the emulator
    # busy because of its own lock — but no other tool may get in while it reads.
    assert not PLAT.Lock('pcsx2').held_by_other(), 'the holder does not count as another process'
    assert not _other_process_can_lock(), 'a second tool must queue, not interleave its reads'
    # Nothing releases it in normal use — that is the point of "for the life of the process" — so the
    # release path is exercised by hand here, since a crashed tool must not leave PCSX2 locked.
    PINE._lock.release()
    PINE._lock = None
    assert _other_process_can_lock(), 'and the lock is free again as soon as the holder lets go'
    srv.close()


# --- pad.py ------------------------------------------------------------------

class FakePad(PAD.PadBackend):
    """A pad backend with no kernel behind it: /dev/uinput is not available in a test, and the script
    language is what has the bugs anyway."""

    name = 'fake'

    def __init__(self):
        self.events: list[tuple] = []

    def open(self) -> str:
        return '/dev/null'

    def button(self, name: str, down: bool) -> None:
        self.events.append((name, down))

    def axis(self, side: str, x: float, y: float) -> None:
        self.events.append(('axis', side, x, y))

    def pressed(self) -> list[str]:
        return [e[0] for e in self.events if len(e) == 2 and e[1] is True]

    def held(self) -> set[str]:
        down: set[str] = set()
        for e in self.events:
            if len(e) == 2:
                down.add(e[0]) if e[1] else down.discard(e[0])
        return down


class FakeClock:
    """Durations are the point of the script language, so they are recorded rather than slept."""

    def __init__(self):
        self.slept: list[float] = []

    def sleep(self, s: float) -> None:
        self.slept.append(s)

    def monotonic(self) -> float:
        return sum(self.slept)


def script(region: str = 'US'):
    """(Script, backend, clock) with no device and no sleeping, at `region`'s frame rate."""
    c = FakeCfg(WORK / 'pad')
    c.region = region
    use_cfg(c)
    dev, clock = FakePad(), FakeClock()
    PAD.time = clock
    return PAD.Script(dev), dev, clock


@check('press holds a button for the milliseconds asked and then lets it go')
def _():
    s, dev, clock = script()
    _, out, _ = out_of(s.line, 'press cross 120')
    assert dev.events == [('cross', True), ('cross', False)]
    assert clock.slept == [0.120] and out == ''
    out_of(s.line, 'press cross')                 # no duration: the documented 100 ms default
    assert clock.slept[-1] == 0.100
    out_of(s.line, 'dpad right 60')
    assert dev.events[-2:] == [('right', True), ('right', False)]


@check("a duration ending in f counts frames at the disc's own rate, not milliseconds")
def _():
    s, _, clock = script()
    out_of(s.line, 'press cross 6f')
    assert abs(clock.slept[-1] - 6 / 60) < 1e-9, 'an NTSC disc runs at 60'
    out_of(s.line, 'sleep 250ms')
    assert clock.slept[-1] == 0.250
    s, _, clock = script(region='EU')
    out_of(s.line, 'wait 5f')
    assert abs(clock.slept[-1] - 5 / 50) < 1e-9, 'a PAL disc runs at 50, so the same script waits longer'


@check('a press shorter than one frame is pointless, and says so instead of pressing nothing visible')
def _():
    s, _, clock = script()
    _, out, _ = out_of(s.line, 'press cross 8')
    assert 'shorter than one frame' in out and '8f to mean frames' in out
    assert clock.slept == [0.008], 'it still does what it was told'
    _, out, _ = out_of(s.line, 'press cross 40')
    assert out == '', 'a duration over one frame is not worth a word'


@check('semicolons put several commands on one line, which is how a chord is written')
def _():
    s, dev, clock = script()
    out_of(s.line, 'down l1; press cross 100; up l1')
    assert dev.events == [('l1', True), ('cross', True), ('cross', False), ('l1', False)]
    assert clock.slept == [0.100], 'only the press waits; down and up are instant'
    out_of(s.line, '  # a comment line')
    out_of(s.line, '')
    out_of(s.line, 'press circle 50 # trailing comment')
    assert dev.events[-2:] == [('circle', True), ('circle', False)]


@check('release puts every button up and both sticks back to neutral')
def _():
    s, dev, _ = script()
    out_of(s.line, 'stick l 0 -1')
    assert dev.events[-1] == ('axis', 'l', 0.0, -1.0), 'y is positive downwards, as the help says'
    out_of(s.line, 'stick right 0.5 0.25')
    assert dev.events[-1] == ('axis', 'r', 0.5, 0.25)
    dev.events.clear()
    out_of(s.line, 'release')
    ups = [e[0] for e in dev.events if len(e) == 2 and e[1] is False]
    assert set(ups) == set(PAD.BUTTONS) and 'select' not in ups
    assert dev.events[-2:] == [('axis', 'l', 0, 0), ('axis', 'r', 0, 0)]


@check('select is refused and never reaches the device: PCSX2 binds save and load states to it')
def _():
    s, dev, _ = script()
    _, out, _ = out_of(s.line, 'press select 100')
    assert 'refusing `select`' in out and 'save or load a state' in out
    assert dev.events == [], 'not one event, not even the release'
    assert out.count('refusing') == 1, 'once per press, not again on the way up'
    _, out, _ = out_of(s.line, 'down select')
    assert 'refusing `select`' in out and dev.events == []
    _, out, _ = out_of(s.line, 'select')
    assert 'refusing `select`' in out and dev.events == []
    assert 'select' not in PAD.BUTTONS


@check('bad syntax names the line that was wrong and presses nothing')
def _():
    s, dev, clock = script()
    for text, want in (('press', 'bad arguments'),
                       ('press cross soon', 'bad arguments'),
                       ('stick l 0', 'bad arguments'),
                       ('jump cross 100', 'unknown command'),
                       ('press nosuchbutton 100', 'unknown button')):
        _, out, _ = out_of(s.line, text)
        assert want in out, (text, out)
        assert repr(text) in out or text.split()[1] in out, f'{text!r} is not named in {out!r}'
    _, out, _ = out_of(s.line, 'cross')
    assert 'is a button, not a command' in out and 'press cross 100' in out
    assert dev.pressed() == [], 'a line the parser did not understand must not half-execute'
    assert dev.held() == set(), 'a bad duration must not leave a button held down for the whole session'
    # An unknown button still spends its duration (and says so twice, once per edge); what matters is
    # that no event reached the device.
    assert clock.slept == [0.1]


@check('send with nothing serving says how to start a server, and quiet mode reports it instead')
def _():
    use_cfg(FakeCfg(WORK / 'padsend'))
    where = PAD.transport()
    if PLAT.WINDOWS:
        # A named pipe, not a file: the instance has to be in the name because the namespace is
        # machine-wide, and `serving()` asks the pipe itself rather than looking in a directory.
        assert where == r'\\.\pipe\ps2kit-pad'
    else:
        f = PAD.fifo()
        assert where == str(f) and f.parent == RUN and f.name == 'pad.fifo'
        if f.exists():
            f.unlink()
    assert not PAD.serving(), 'nothing serves this copy until a server is started'
    e = raises(SystemExit, PAD.send, ['press cross 100'])
    assert 'kit pad serve' in str(e) and where in str(e)
    rv, _, err = out_of(PAD.send, ['press cross 100'], quiet=True)
    assert rv is False and 'kit pad serve' in err


@check('a machine that cannot make a pad is told so, rather than a backend pretending to work')
def _():
    real_b, real_a = PLAT.pad_backend, PLAT.pad_available
    PLAT.pad_backend = lambda: 'none'
    PLAT.pad_available = lambda: (False, 'no pad driver here')
    try:
        e = raises(SystemExit, PAD.backend)
        assert 'no virtual pad' in str(e) and 'no pad driver here' in str(e)
        assert set(PAD.BACKENDS) == {'uinput', 'vigem'}
    finally:
        PLAT.pad_backend, PLAT.pad_available = real_b, real_a


# --- probe.py ----------------------------------------------------------------

def endpoint(srv: FakePcsx2):
    """Point `Pine()` (as the CLI builds it) at the fake emulator for the rest of this test."""
    PLAT.pine_endpoint = lambda slot: (srv.family, srv.addr)


@check('a word is rated by its own chunk\'s elapsed time, not the whole pass\'s')
def _():
    words = PROBE.CHUNK // 4
    a = bytearray(PROBE.CHUNK * 2)
    b = bytearray(PROBE.CHUNK * 2)
    struct.pack_into('<I', b, 0x40, 120)                 # chunk 0: +120 over its own 2.0 s
    struct.pack_into('<I', b, PROBE.CHUNK + 0x40, 72)    # chunk 1: +72 over its own 1.2 s
    s0 = PROBE.Snap(0, bytes(a), [0.0, 1.0])
    s1 = PROBE.Snap(0, bytes(b), [2.0, 2.2])
    got = dict(PROBE._rate_filter(s0, s1))
    assert sorted(got) == [0x40, PROBE.CHUNK + 0x40], got
    assert abs(got[0x40] - 60.0) < 0.01 and abs(got[PROBE.CHUNK + 0x40] - 60.0) < 0.01
    assert s0.at(PROBE.CHUNK * 2) == 1.0, 'past the end is the last chunk, not an IndexError'
    struct.pack_into('<I', b, 0x80, 600)                 # 300/s: a timer, not a frame counter
    struct.pack_into('<I', b, 0x84, 2)                   #   1/s: nothing like one either
    assert sorted(dict(PROBE._rate_filter(s0, PROBE.Snap(0, bytes(b), [2.0, 2.2])))) == \
        [0x40, PROBE.CHUNK + 0x40], 'the frame-rate window is the whole filter'


@check('the frame counter wins against a half-rate counter, a stalling one and a random word')
def _():
    import random
    c = use_cfg(FakeCfg(WORK / 'vsync'))
    srv = FakePcsx2()
    rng = random.Random(20261015)
    base = 0x4_0000
    srv.planted[base + 0x100] = lambda s: int(60 * s.elapsed)
    srv.planted[base + 0x200] = lambda s: int(30 * s.elapsed)
    srv.planted[base + 0x300] = lambda s: int(60 * min(s.elapsed, 0.95) + 2 * max(0.0, s.elapsed - 0.95))
    srv.planted[base + 0x400] = lambda s: rng.randrange(0, 1 << 32)
    p = client(srv)
    scored, out, err = out_of(PROBE.find_vsync, p, base, 0x1000, save=True)
    found = [a for a, *_ in scored]
    assert found[0] == base + 0x100, f'{[hex(a) for a in found]}'
    assert 55 <= scored[0][1] <= 65 and scored[0][3] < 0.02
    assert base + 0x200 not in found, 'a counter at half the frame rate is not the frame counter'
    assert base + 0x300 not in found, 'a word that stops climbing is not linear, so it loses'
    assert base + 0x400 not in found, 'a word full of noise never rates as a frame rate'
    assert c.wrote == [('emu', 'vsync_addr', base + 0x100)], c.wrote
    assert f'{base + 0x100:#08x}' in out and 'frames/s' in out
    assert 'only a kit copy can be paused' in err, 'the paused check is skipped out loud, not silently'
    srv.close()


@check('nothing to find, or a paused emulator, says which it was instead of printing an empty list')
def _():
    use_cfg(FakeCfg(WORK / 'vsync2'))
    srv = FakePcsx2()
    p = client(srv)
    e = raises(SystemExit, PROBE.find_vsync, p, 0x5_0000, 0x1000)
    assert 'no word in that region climbs at the frame rate' in str(e) and '--addr 0' in str(e)
    srv.close()
    paused = FakePcsx2(status='paused')
    e = raises(SystemExit, PROBE.find_vsync, client(paused), 0x5_0000, 0x1000)
    assert 'PCSX2 is paused' in str(e) and 'nothing in RAM is moving' in str(e)
    paused.close()


@check('a value hunt narrows only by what each pass proves, and refuses to mix regions or types')
def _():
    use_cfg(FakeCfg(WORK / 'hunt'))
    srv = FakePcsx2()
    p = client(srv)
    base, up, down, same = 0x5_0000, 0x5_0010, 0x5_0020, 0x5_0030
    srv.poke(up, 100)
    srv.poke(down, 100)
    srv.poke(same, 100)
    keep, out, _ = out_of(PROBE.find_changed, p, addr=base, length=0x100, reset=True)
    assert keep == [] and 'remembered' in out and 'change the value in the game' in out

    srv.poke(up, 101)
    srv.poke(down, 99)
    keep, out, _ = out_of(PROBE.find_changed, p, mode='decreased')
    assert keep == [down], [hex(a) for a in keep]
    assert '1 candidate(s) decreased' in out and f'{down:#08x}' in out

    keep, _, _ = out_of(PROBE.find_changed, p, mode='unchanged')
    assert keep == [down], 'a later pass narrows the survivors, it does not start again'

    srv.poke(down, 1234)
    keep, _, _ = out_of(PROBE.find_changed, p, equals='1234')
    assert keep == [down]
    keep, _, _ = out_of(PROBE.find_changed, p, equals='1235')
    assert keep == [], 'an empty list is a real answer: that value is nowhere in the region'

    e = raises(SystemExit, PROBE.find_changed, p, mode='changed', addr=base, length=0x200)
    assert 'the region or type changed' in str(e) and '--reset' in str(e)
    srv.close()


@check('a float hunt compares with a tolerance, and a word that is not a float at all is dropped')
def _():
    use_cfg(FakeCfg(WORK / 'floats'))
    srv = FakePcsx2()
    p = client(srv)
    base, moved, nan = 0x6_0000, 0x6_0010, 0x6_0020
    srv.poke(moved, struct.unpack('<I', struct.pack('<f', 1.5))[0])
    out_of(PROBE.find_changed, p, addr=base, length=0x100, kind='f32', reset=True)
    srv.poke(moved, struct.unpack('<I', struct.pack('<f', 2.25))[0])
    srv.poke(nan, 0x7fc00000)
    keep, _, _ = out_of(PROBE.find_changed, p, mode='changed', kind='f32')
    assert keep == [moved], 'NaN bits are uninitialised memory, not a value that changed'
    keep, _, _ = out_of(PROBE.find_changed, p, equals='2.2500001', kind='f32')
    assert keep == [moved], 'floats are compared within --tol, never for equality'
    keep, _, _ = out_of(PROBE.find_changed, p, equals='2.26', kind='f32', tol=1e-6)
    assert keep == [], 'the tolerance is relative and small, not a window anything lands in'
    srv.close()


@check('--near searches a window centred on a known address instead of all of RAM')
def _():
    import json
    c = use_cfg(FakeCfg(WORK / 'near'))
    srv = FakePcsx2()
    endpoint(srv)
    out_of(PROBE.cmd_probe, ['find', '--reset', '--near', '@table', '--window', '0x100'])
    state = json.loads(c.ctx(PROBE.STATE).read_text())
    assert state['addr'] == hex(c.symbols['table'] - 0x80) and state['len'] == '0x100'
    assert state['kind'] == 'u32' and state['cands'] is None
    assert c.ctx(PROBE.SNAPFILE).stat().st_size == 0x100
    srv.close()


@check('a pointer chain is walked hop by hop, and a hop out of RAM says the chain broke before it')
def _():
    use_cfg(FakeCfg(WORK / 'chase'))
    srv = FakePcsx2()
    p = client(srv)
    srv.poke(0x3000, 0x0000_4000)
    srv.poke(0x4010, 0x0000_5000)
    srv.poke(0x5040, struct.unpack('<I', struct.pack('<f', -12.5))[0])
    rows, out, _ = out_of(PROBE.chase, p, 0x8000_3000, [0x10, 0x40])
    assert rows == [(0x3000, 0x4000), (0x4010, 0x5000), (0x5040, 0xc1480000)]
    assert 'looks like a pointer' in out and 'as f32 -12.5' in out
    assert '(+0x40)' in out, 'every hop names the offset that produced it'

    srv.poke(0x3000, RAM - 0x10)
    rows, out, _ = out_of(PROBE.chase, p, 0x3000, [0x20])
    assert len(rows) == 1 and 'outside EE RAM' in out and 'broke at the hop before' in out
    srv.close()


@check('a virtual address is folded onto the RAM offset PINE wants, and a plain offset is left alone')
def _():
    assert PROBE.ee(0x8000_3000, RAM) == 0x3000 and PROBE.ee(0xa000_3000, RAM) == 0x3000
    assert PROBE.ee(0x3000, RAM) == 0x3000
    assert PROBE.ee(0x0020_0000, RAM) == 0, 'RAM mirrors itself above its size'


@check('probe wrote says where the answer is, because PINE has no watchpoint op to ask')
def _():
    use_cfg(FakeCfg(WORK / 'wrote'))
    rv, _, err = out_of(PROBE.cmd_probe, ['wrote', '@table'])
    assert rv == 2, 'an honest "I cannot" is not a success'
    assert 'no watchpoint op' in err and 'Open Debugger' in err and 'kit fn' in err


# --- emu.py ------------------------------------------------------------------

INI = """[UI]
MainWindowGeometry = AdnQywADAAAA
ConfirmShutdown = true
EnablePINE = false

[EmuCore]
EnableCheats = false

[Folders]
Savestates = sstates
Bios = bios

[SPU2/Output]
Backend = Cubeb
"""

LAUNCHED: list[tuple[list[str], object, dict]] = []
ALIVE: set[int] = set()


def emu_env(inst: str = '') -> FakeCfg:
    """Patch out every way `emu` could touch the real machine: no PCSX2, no pad, no spawning."""
    c = use_cfg(FakeCfg(WORK / 'emu'))
    os.environ.pop('KIT_PCSX2', None)
    if inst:
        os.environ['KIT_PCSX2'] = inst
    PLAT.pcsx2_config_dir = lambda n='': (WORK / 'emu' / 'cfg' / (f's{n}' if n else 'own') / 'PCSX2')
    PLAT.copies_root = lambda: WORK / 'emu' / 'cfg'
    PLAT.pcsx2_cmd = lambda cmd: ['/fake/bin/pcsx2-qt']
    PLAT.spawn_detached = lambda argv, log=None, env=None: (LAUNCHED.append((argv, log, env or {}))
                                                            or 31337)
    PLAT.alive = lambda pid: pid in ALIVE
    PLAT.proc_name = lambda pid: 'pcsx2-qt'
    PLAT.pad_available = lambda: (True, '')
    LAUNCHED.clear()
    own = PLAT.pcsx2_config_dir('') / 'inis'
    own.mkdir(parents=True, exist_ok=True)
    if not (own / 'PCSX2.ini').exists():
        (own / 'PCSX2.ini').write_text(INI)
    return c


@check('a key already in the ini is edited where it is, since PCSX2 moves keys between sections')
def _():
    emu_env()
    ini = EMU.ini_path()
    EMU._ini_set(ini, 'EmuCore', 'EnablePINE', 'true')     # it lives under [UI] in this file
    text = ini.read_text()
    assert text.count('EnablePINE') == 1, 'a second copy in another section is what PCSX2 reads last'
    assert text.index('EnablePINE') < text.index('[EmuCore]')
    assert EMU._ini_get(ini, 'EnablePINE') == 'true' and EMU._ini_get(ini, 'Backend') == 'Cubeb'
    assert 'MainWindowGeometry = AdnQywADAAAA' in text, 'everything else is left byte for byte'

    EMU._ini_set(ini, 'EmuCore', 'PINESlot', '28011')      # new key, existing section
    lines = ini.read_text().splitlines()
    assert lines.index('[EmuCore]') < lines.index('PINESlot = 28011') < lines.index('[Folders]')
    EMU._ini_set(ini, 'Hotkeys', 'TogglePause', 'SDL-0/Guide')   # new key, no such section
    assert ini.read_text().endswith('[Hotkeys]\nTogglePause = SDL-0/Guide\n')
    EMU._ini_del(ini, 'TogglePause')
    assert 'TogglePause' not in ini.read_text() and EMU._ini_get(ini, 'TogglePause', 'gone') == 'gone'

    e = raises(SystemExit, EMU._ini_set, WORK / 'emu' / 'nothing' / 'PCSX2.ini', 'UI', 'K', 'v')
    assert 'no PCSX2 ini at' in str(e) and 'Start PCSX2 once' in str(e)


@check('PINE is written into the ini on every launch, because PCSX2 rewrites it when it exits')
def _():
    c = emu_env()
    ini = EMU.ini_path()
    out_of(EMU.start)
    assert EMU._ini_get(ini, 'EnablePINE') == 'true' and EMU._ini_get(ini, 'PINESlot') == '28011'
    EMU._ini_del(ini, 'EnablePINE')                        # what PCSX2 does to it on the way out
    assert EMU._ini_get(ini, 'EnablePINE') == ''
    out_of(EMU.start)
    assert EMU._ini_get(ini, 'EnablePINE') == 'true', 'setting it once, by hand, is not enough'


@check("the user's own emulator boots the disc after a -- and ignores every kit pad")
def _():
    c = emu_env()
    _, out, _ = out_of(EMU.start, ['-fullscreen'])
    argv, log, env = LAUNCHED[-1]
    assert argv == ['/fake/bin/pcsx2-qt', '-fullscreen', '--', str(c.need_iso())]
    if PLAT.WINDOWS:
        # Nothing to filter on: every ViGEm pad presents Microsoft's own 360 VID/PID, so the user's
        # emulator can bind a kit pad. WINDOWS.md states that as a limitation rather than hiding it.
        assert not [k for k in env if k.startswith('SDL_GAMECONTROLLER')]
    else:
        assert env['SDL_GAMECONTROLLER_IGNORE_DEVICES'] == \
            '0x1209/0x5001,0x1209/0x5002,0x1209/0x5003,0x1209/0x5004'
        assert 'SDL_GAMECONTROLLER_IGNORE_DEVICES_EXCEPT' not in env, 'their pad 1 stays their pad 1'
    assert 'XDG_CONFIG_HOME' not in env, 'their own config dir is theirs'
    assert 'PINE slot 28011' in out and Path(log).name == 'pcsx20.log'
    assert EMU._pid_file().read_text().strip() == '31337' and EMU._pid_file().name == 'pcsx2.pid'

    ALIVE.add(31337)
    try:
        _, out, _ = out_of(EMU.start)
        assert 'already running (pid 31337)' in out and len(LAUNCHED) == 1, 'a second launch is a no-op'
    finally:
        ALIVE.discard(31337)


@check('a kit copy gets its own config dir, its own PINE slot and only its own pad')
def _():
    c = emu_env(inst='2')
    try:
        e = raises(SystemExit, EMU.start)
        assert 'no PCSX2 copy 2 yet' in str(e) and 'kit pcsx2 copy 2' in str(e)
        _, out, _ = out_of(EMU.copy_config, 2)
        assert 'PINE slot 28013' in out and 'muted' in out
        EMU._pad_pid_file().write_text('4242\n')
        ALIVE.add(4242)
        _, out, _ = out_of(EMU.start)
        argv, log, env = LAUNCHED[-1]
        if PLAT.WINDOWS:
            # No XDG_CONFIG_HOME there, so the copy is pinned with PCSX2's own -cfgpath, and its pad by
            # device index in the ini (`pad_ini_rows`) rather than by any environment filter.
            assert argv == ['/fake/bin/pcsx2-qt', '-cfgpath', str(PLAT.pcsx2_config_dir('2')),
                            '--', str(c.need_iso())]
            assert not [k for k in env if k.startswith('SDL_GAMECONTROLLER')]
        else:
            assert argv == ['/fake/bin/pcsx2-qt', '--', str(c.need_iso())]
            assert env['XDG_CONFIG_HOME'] == str(WORK / 'emu' / 'cfg' / 's2')
            assert env['SDL_GAMECONTROLLER_IGNORE_DEVICES_EXCEPT'] == '0x1209/0x5002'
            assert 'SDL_GAMECONTROLLER_IGNORE_DEVICES' not in env, 'a copy takes its own pad, no other'
        assert 'PINE slot 28013' in out and Path(log).name == 'pcsx22.log'
        assert EMU._pid_file().name == 'pcsx22.pid', 'one pid file per copy, or stop kills the wrong one'
    finally:
        ALIVE.discard(4242)
        os.environ.pop('KIT_PCSX2', None)


@check("a copy's save states and memory cards are its own, written as absolute paths")
def _():
    emu_env(inst='3')
    try:
        src = PLAT.pcsx2_config_dir('')
        (src / 'cache').mkdir(parents=True, exist_ok=True)
        (src / 'cache' / 'huge.bin').write_bytes(b'\0' * 1024)
        (src / 'memcards').mkdir(parents=True, exist_ok=True)
        (src / 'memcards' / 'Mcd001.ps2').write_bytes(b'card')
        (src / 'savestates').mkdir(parents=True, exist_ok=True)
        (src / 'savestates' / 'SLUS12345 (A1B2C3D4).00.p2s').write_bytes(b'')
        dst, out, _ = out_of(EMU.copy_config, 3)
    finally:
        os.environ.pop('KIT_PCSX2', None)
    assert dst == PLAT.pcsx2_config_dir('3') and '1 save state(s) copied in' in out
    ini = EMU.ini_path('3')
    assert EMU._ini_get(ini, 'Savestates') == str(dst / 'savestates'), \
        'a relative path here would share — and overwrite — the user\'s own slots'
    assert EMU._ini_get(ini, 'MemoryCards') == str(dst / 'memcards')
    assert EMU._ini_get(ini, 'OutputMuted') == 'true' and EMU._ini_get(ini, 'Keyboard') == 'false'
    pause = dict(((sec, key), val) for sec, key, val in PLAT.pad_ini_rows(3))[('Hotkeys', 'TogglePause')]
    assert EMU._ini_get(ini, 'PINESlot') == '28014' and EMU._ini_get(ini, 'TogglePause') == pause
    assert (dst / 'memcards' / 'Mcd001.ps2').read_bytes() == b'card', 'progress is copied in, not shared'
    assert not (dst / 'cache').exists(), 'the shader cache is gigabytes and belongs to no copy'
    for sub in ('savestates', 'memcards', 'snaps', 'logs'):
        assert (dst / sub).is_dir()
    assert 'FrameAdvance' not in ini.read_text(), 'frame advance on a pad button pauses a copy by accident'
    real = PLAT.pcsx2_config_dir
    PLAT.pcsx2_config_dir = lambda n='': WORK / 'emu' / 'no-pcsx2-here'
    try:
        e = raises(SystemExit, EMU.copy_config, 4)
    finally:
        PLAT.pcsx2_config_dir = real
    assert 'no PCSX2 config to copy from' in str(e) and 'Run PCSX2 once yourself' in str(e)


@check('the emulator this instance started is tracked by pid file and checked by name')
def _():
    emu_env()
    EMU._pid_file().write_text('99999\n')
    assert EMU.running() is None, 'a dead pid is nobody'
    ALIVE.add(99999)
    real = PLAT.proc_name
    PLAT.proc_name = lambda pid: 'python3'
    try:
        assert EMU.running() is None, 'pids are reused; the name is what proves it is the emulator'
        PLAT.proc_name = lambda pid: 'pcsx2-qt'
        assert EMU.running() == 99999
    finally:
        PLAT.proc_name = real
        ALIVE.discard(99999)
    EMU._pid_file().write_text('not a number\n')
    assert EMU.running() is None


@check('a slot number finds this game\'s newest state in that slot, and any state as the fallback')
def _():
    c = emu_env()
    folder = PLAT.pcsx2_config_dir('') / 'sstates'
    folder.mkdir(parents=True, exist_ok=True)
    for name, when in (('SLUS12345 (A1B2C3D4).03.p2s', 1_700_000_000),
                       ('SLUS12345 (E5F6A7B8).03.p2s', 1_700_009_999),
                       ('OTHERGAME (11223344).05.p2s', 1_700_009_999),
                       ('SLUS12345 (A1B2C3D4).07.p2s', 1_700_000_500)):
        f = folder / name
        f.write_bytes(b'')
        os.utime(f, (when, when))
    assert EMU.state_path('3').name == 'SLUS12345 (E5F6A7B8).03.p2s', 'two crcs: the newest one wins'
    assert EMU.state_path(5).name == 'OTHERGAME (11223344).05.p2s', 'another game, but it is that slot'
    assert EMU.state_path('somewhere/else.p2s') == Path('somewhere/else.p2s')
    e = raises(SystemExit, EMU.state_path, '9')
    assert 'no save state in slot 9' in str(e) and 'SLUS12345' in str(e) and 'kit pine save' in str(e)
    slots = EMU.state_slots()
    assert [s for s, *_ in slots] == [3, 3, 5, 7]
    assert dict((s, note) for s, _, note in slots)[7] == 'match point', 'the kit.toml note comes with it'


def p2s(path: Path, size: int = 0x2000, member: str = 'eeMemory.bin') -> bytes:
    """A save state is a zip; PCSX2 puts EE RAM in it as one flat image from physical address 0."""
    ram = bytes((i * 11 + 5) & 0xff for i in range(size))
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('PCSX2 Savestate', b'\0' * 32)
        z.writestr(member, ram)
    return ram


@check('a region comes out of a save state by virtual address, and the RAM image is unpacked once')
def _():
    c = emu_env()
    state = WORK / 'emu' / 'slot.p2s'
    ram = p2s(state)
    assert [n for n, _ in EMU.state_list(state)] == ['PCSX2 Savestate', 'eeMemory.bin']

    out = EMU.state_extract(state, 0x8000_1000, 0x40)
    assert out.read_bytes() == ram[0x1000:0x1040]
    assert EMU.state_extract(state, 0xa000_1000, 0x40).read_bytes() == ram[0x1000:0x1040]
    assert EMU.state_extract(state, 0x1000, 0x40).read_bytes() == ram[0x1000:0x1040]

    cache = c.ctx('states', f'{state.stem}.eeMemory.bin')
    assert cache.exists() and cache.stat().st_size == len(ram)
    marked = bytearray(cache.read_bytes())
    marked[0] = 0xaa
    cache.write_bytes(bytes(marked))
    EMU.state_extract(state, 0, 4)
    assert cache.read_bytes()[0] == 0xaa, 'unpacking 32 MiB per lookup is the thing being avoided'
    os.utime(state, None)                                  # a newer state must beat the cache
    EMU.state_extract(state, 0, 4)
    assert cache.read_bytes()[0] == ram[0]

    e = raises(SystemExit, EMU.state_extract, state, 0x1ff0, 0x40)
    assert 'runs past the end' in str(e) and 'wrong address' in str(e)
    e = raises(SystemExit, EMU.state_extract, state, 0x0200_1000, 4)
    assert 'runs past the end' in str(e), 'only kseg0/kseg1 are folded; a stray address is not aliased'


def _has_7zip() -> bool:
    """Whether anything here can read a 7z: it decides which error an unreadable state can honestly give."""
    if EMU._sevenzip():
        return True
    try:
        import py7zr                                            # noqa: F401
    except ImportError:
        return False
    return True


@check('a state that is not a PCSX2 one says what it holds instead of writing an empty image')
def _():
    emu_env()
    other = WORK / 'emu' / 'wrong.p2s'
    p2s(other, member='GS/renderer.bin')
    e = raises(SystemExit, EMU.state_ram, other)
    assert 'has no eeMemory.bin' in str(e) and 'GS/renderer.bin' in str(e)
    plain = WORK / 'emu' / 'notazip.p2s'
    plain.write_bytes(b'not an archive at all')
    e = raises(SystemExit, EMU.state_ram, plain)
    assert 'cannot open notazip.p2s' in str(e) and 'neither a zip nor' in str(e)
    if _has_7zip():         # a backend is installed, so the file itself is the problem, not the machine
        assert 'neither a zip nor a 7-Zip archive' in str(e)
        assert 'eeMemory.bin' not in str(e), 'an unreadable file is not a state that holds nothing'
    else:                   # nothing can read 7z here, so the error has to say what to install
        assert 'neither a zip nor readable without 7-Zip' in str(e) and 'py7zr' in str(e)


# --- the live CLI surface ----------------------------------------------------

@check('kit pine reads, writes and dumps a block through one client, and says what it did')
def _():
    c = use_cfg(FakeCfg(WORK / 'cli'))
    srv = FakePcsx2()
    endpoint(srv)
    srv.poke(0x4000, 0x11111111, 0x22222222)
    rv, out, _ = out_of(PINE.cmd_pine, ['read', '@table', '2'])
    assert rv is None and '00004000  286331153 (0x11111111)' in out and '00004004' in out
    rv, out, _ = out_of(PINE.cmd_pine, ['4000', '--len', '16', '--as', 'hex'])
    assert '00004000  11 11 11 11 22 22 22 22' in out, 'a bare address means read, the commonest case'
    out_of(PINE.cmd_pine, ['write', '@table', '-1.5', '--as', 'f32'])
    assert srv.peek(0x4000) == 0xbfc00000
    out_of(PINE.cmd_pine, ['write', '@table', '--u8', '7'])
    assert srv.peek(0x4000, 1) == 7, '--u8 V and --as u8 V are the same thing'
    rv, out, _ = out_of(PINE.cmd_pine, ['block', '4000', '0x20'])
    dumped = c.ctx('pine', '00004000-20.bin')
    assert dumped.read_bytes() == bytes(srv.ram[0x4000:0x4020]) and str(dumped) in out
    e = raises(SystemExit, PINE.cmd_pine, ['write', '@table'])
    assert 'kit pine write ADDR VALUE' in str(e)
    srv.close()


@check('kit pine watch samples one line per frame, and a long run goes to a CSV not the terminal')
def _():
    c = use_cfg(FakeCfg(WORK / 'cli2'))
    srv = FakePcsx2()
    srv.planted[FRAME] = lambda s: s.reads[FRAME]
    srv.poke(0x4000, 0x2a)
    p = client(srv)
    _, out, _ = out_of(PINE.watch, p, ['@frame', '@table'], frames=3)
    assert out.splitlines()[0] == 'frame,frame,table'
    assert len(out.splitlines()) == 4 and out.strip().endswith(',42')
    _, out, _ = out_of(PINE.watch, p, ['@table'], frames=45)
    assert 'frames omitted' in out and str(c.ctx('pine', 'watch.csv')) in out
    assert len(c.ctx('pine', 'watch.csv').read_text().splitlines()) == 46
    srv.close()


@check('kit pine info reports the emulator, the disc, kit.toml and whether frames are moving')
def _():
    use_cfg(FakeCfg(WORK / 'cli3'))
    srv = FakePcsx2()
    srv.planted[FRAME] = lambda s: int(60 * s.elapsed)
    p = client(srv)
    _, out, _ = out_of(PINE._info, p)
    assert '2.9.9-fake' in out and '[running]' in out and 'SLUS_123.45' in out and 'SLUS-12345' in out
    assert 'slot 28011' in out
    first, second = out.splitlines()[-1].split(' = ')[1].split(' -> ')
    assert int(second.split()[0]) > int(first), 'a stuck counter is the thing this line is for'
    srv.close()


if __name__ == '__main__':
    print(f'ps2kit live-layer tests  (python {sys.version.split()[0]})')
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
