#!/usr/bin/env python3
"""Tests for the declarative recorder: `python3 tests/test_record.py`.

No pytest, no kit.toml, no emulator. A fake EE RAM and a fake Pine stand in for the real thing, so
every part that used to live in a one-off script — region coalescing, pointer chasing, torn-read
accounting, the CSV and the diff — is checked here instead of being re-debugged per capture.
"""
from __future__ import annotations

import csv
import io
import json
import os
import struct
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ps2kit import record as R                                   # noqa: E402

RAM_MB = 32
RAM = RAM_MB << 20

# --- a fake game in a fake EE RAM -------------------------------------------
# One match object with a ball and four players behind a pointer table, which is the shape that
# half the hand-written capture scripts were walking by hand.
VSYNC = 0x00100000
GLOBALS = 0x00100010
MATCH_PTR = 0x00200000
MATCH = 0x00300000
BALL = 0x00400000
PLAYER = [0x00500000 + i * 0x10000 for i in range(4)]
ANIM = [0x00600000 + i * 0x1000 for i in range(4)]

SYMS = {'vsync': VSYNC, 'globals': GLOBALS, 'match': MATCH_PTR}


class FakeCfg:
    """Just enough of ps2kit.config.Config for the recorder, with no kit.toml on disk."""
    name, serial, region, version = 'Fake Game', 'SLUS-20000', 'US', '1.00'
    pine_slot, speed, ram = 28011, 1.0, RAM
    vsync = VSYNC

    def __init__(self, syms=None):
        self.syms = dict(SYMS if syms is None else syms)

    def sym(self, s):
        s = str(s).strip()
        if s.startswith('@'):
            if s[1:] not in self.syms:
                raise SystemExit(f'no [symbols] entry named {s[1:]!r} in kit.toml')
            return self.syms[s[1:]]
        return int(s, 16) if not s.lower().startswith('0x') else int(s, 16)

    def need_vsync(self):
        return VSYNC

    def ctx(self, *parts):
        p = Path(tempfile.gettempdir(), 'ps2kit-test', *parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p


def use_cfg(syms=None):
    """`record.py` binds `cfg` at import time, so a test can swap it for a fake outright."""
    c = FakeCfg(syms)
    R.cfg = lambda required=True: c
    return c


class FakePine:
    """The documented Pine surface over a bytearray, plus the two things a recorder must survive:
    a frame that ticks mid-read, and a pointer that moves mid-read."""

    def __init__(self, ram: bytearray, frame: int = 1000, tear: set[int] | None = None,
                 move: dict[int, tuple[int, int]] | None = None, per_frame=None):
        self.ram = ram
        self.frame = frame
        self.tear = tear or set()            # frames whose settle() reports a tick (returns None);
        #                                      True = every frame, for the "nothing captured" case
        self.move = move or {}               # frame -> (addr, new pointer) written during settle
        self.per_frame = per_frame           # callback(ram, frame) to animate the fake game
        self.reads = 0                       # PINE messages, so a test can prove batching
        self.states: list[int] = []
        self.w32(VSYNC, frame)

    # -- memory --
    def w32(self, a, v):
        struct.pack_into('<I', self.ram, a, v & 0xffffffff)

    def wf(self, a, *vals):
        struct.pack_into('<%df' % len(vals), self.ram, a, *vals)

    # -- Pine interface --
    def read8(self, a):
        return self.ram[a]

    def read16(self, a):
        return struct.unpack_from('<H', self.ram, a)[0]

    def read32(self, a):
        return struct.unpack_from('<I', self.ram, a)[0]

    def read64(self, a):
        return struct.unpack_from('<Q', self.ram, a)[0]

    def read_block(self, a, n):
        return bytes(self.ram[a:a + n])

    def read_regions(self, regions):
        for a, n in regions:
            assert n % 8 == 0, f'region length {n} is not a multiple of 8 (PINE reads read64s)'
            assert a % 8 == 0, f'region address {a:#x} is not 8-aligned'
            assert 0 <= a and a + n <= len(self.ram), f'region {a:#x}+{n} is outside EE RAM'
        self.reads += 1
        return b''.join(bytes(self.ram[a:a + n]) for a, n in regions)

    def settle(self, regions, frame):
        if self.tear is True or frame in self.tear:
            return None
        if frame in self.move:
            a, v = self.move[frame]
            self.w32(a, v)
        return self.read_regions(regions)

    def next_frame(self, last, delay=0.0):
        self.frame = last + 1
        self.w32(VSYNC, self.frame)
        if self.per_frame:
            self.per_frame(self, self.frame)
        return self.frame

    def load_state(self, n):
        self.states.append(n)

    def save_state(self, n):
        self.states.append(-n)

    def status(self):
        return 'running'

    def game_id(self):
        return 'SLUS-20000'


def fake_game() -> bytearray:
    ram = bytearray(RAM)
    p = FakePine(ram)
    p.w32(MATCH_PTR, MATCH)
    p.w32(MATCH + 0x88, BALL)
    for i, a in enumerate(PLAYER):
        p.w32(MATCH + 0xa8 + 4 * i, a)
        p.w32(a + 0x60, ANIM[i])
        struct.pack_into('<H', ram, ANIM[i] + 0x24, 0x100 + i)
        p.wf(a + 0x3d70, 1.5 + i, 2.5 + i, 3.5 + i, 1.0)
        p.wf(a + 0x12b4, 100.0 - i)
        struct.pack_into('<H', ram, a + 0x3050, 7 + i)
    ram[MATCH + 0x54] = 2
    ram[MATCH + 0x55] = 1
    p.wf(BALL + 0xe0, -1.0, 0.25, 12.0, 1.0)
    p.wf(BALL + 0x130, 0.5, 0.0, -0.5)
    p.w32(GLOBALS, 3)
    p.w32(GLOBALS + 4, 99)
    ram[GLOBALS + 8] = 4
    return ram


# --- the harness ------------------------------------------------------------

TESTS: list[tuple[str, object]] = []
PASS, FAIL = [], []


def check(name):
    """Register a test. Definition order is run order, so the cheap parsing tests fail first."""
    def deco(fn):
        TESTS.append((name, fn))
        return fn
    return deco


def spec(text: str, **kw) -> R.Watch:
    return R.Watch(text=text, **kw)


def tmp(name: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix='ps2kit-record-'))
    return d / name


def rows(path: Path) -> tuple[list[str], list[dict]]:
    with open(path, newline='', encoding='utf-8') as f:
        r = csv.reader(f)
        head = next(r)
        return head, [dict(zip(head, line)) for line in r]


# --- parsing ----------------------------------------------------------------

@check('a spec parses into fields, types and columns')
def t_parse():
    use_cfg()
    w = spec('''
[watch]
name = "t"
frames = 10
[[field]]
name = "pos"
addr = "@match -> 0x88 -> 0xe0"
type = "f32x4"
[[field]]
name = "phase"
addr = "@match -> 0x54"
type = "u8"
[[field]]
name = "head"
addr = "0x300000"
type = "bytes:16"
''')
    assert w.name == 't' and w.frames == 10, (w.name, w.frames)
    assert [f.name for f in w.fields] == ['pos', 'phase', 'head']
    assert w.columns == ['frame', 'pos.x', 'pos.y', 'pos.z', 'pos.w', 'phase', 'head'], w.columns
    assert w.fields[0].kind.size == 16 and w.fields[2].kind.size == 16


@check('an array is one spec block and N sets of columns')
def t_array():
    use_cfg()
    w = spec('''
[watch]
frames = 1
[[array]]
name = "player"
addr = "@match -> 0xa8"
count = 4
stride = 4
deref = true
[[array.field]]
name = "pos"
off = 0x3d70
type = "f32x4"
[[array.field]]
name = "stamina"
off = 0x12b4
type = "f32"
''')
    assert len(w.fields) == 8, len(w.fields)
    assert w.columns[1:6] == ['player0.pos.x', 'player0.pos.y', 'player0.pos.z', 'player0.pos.w',
                              'player0.stamina'], w.columns[1:6]
    # four pointers -> four groups, each the object behind one table entry
    assert len(w.plan.groups) == 4, [g.chain_text for g in w.plan.groups]


@check('TOML integer offsets are not re-read as hex (0x10 stays 16)')
def t_int_offsets():
    use_cfg()
    w = spec('''
[watch]
frames = 1
[[array]]
name = "shot"
addr = "0x2f0e80"
count = 2
stride = 0x30
[[array.field]]
name = "aim"
off = 0x10
type = "u32"
''')
    offs = sorted(off for g in w.plan.groups for _, off in g.fields)
    assert offs == [0x10, 0x40], [hex(o) for o in offs]      # 0x10 and 0x30+0x10, not 0x16/0x48


@check('a chain means one deref per arrow, canonicalised to object + offset')
def t_chain():
    use_cfg()
    c = R.parse_chain('@match -> 0xa8 -> 0x3fb0', 'x', allow_unresolved=False, unresolved=[]).canon()
    assert c.base == MATCH_PTR and c.steps == (0xa8, 0) and c.off == 0x3fb0, (c.base, c.steps, c.off)
    flat = R.parse_chain('@globals + 0x24', 'x', allow_unresolved=False, unresolved=[])
    assert flat.static and flat.base + flat.off == GLOBALS + 0x24


# --- region coalescing ------------------------------------------------------

@check('adjacent fields coalesce into one region')
def t_coalesce():
    use_cfg()
    # NB the offsets are written in hex: in a spec a bare number is hex (the kit's convention),
    # so "+ 12" would mean 0x12 and the fields would not be adjacent.
    body = '\n'.join(f'[[field]]\nname = "f{i}"\naddr = "0x100000 + {i * 4:#x}"\ntype = "u32"'
                     for i in range(8))
    w = spec('[watch]\nframes = 1\n' + body)
    assert w.regions == [(0x100000, 32)], w.regions         # 8 fields, 1 region
    # and a field on its own, far away, is its own region
    w2 = spec('[watch]\nframes = 1\n' + body +
              '\n[[field]]\nname = "far"\naddr = "0x180000"\ntype = "u32"')
    assert w2.regions == [(0x100000, 32), (0x180000, 8)], w2.regions
    # sub-quad fields in one quadword do not read it twice
    w3 = spec('''
[watch]
frames = 1
[[field]]
name = "a"
addr = "0x100000"
type = "u8"
[[field]]
name = "b"
addr = "0x100001"
type = "u8"
[[field]]
name = "c"
addr = "0x100006"
type = "u16"
''')
    assert w3.regions == [(0x100000, 8)], w3.regions


@check('fields inside one live object share a group and coalesce')
def t_coalesce_dynamic():
    use_cfg()
    w = spec('''
[watch]
frames = 1
[[field]]
name = "pos"
addr = "@match -> 0x88 -> 0xe0"
type = "f32x4"
[[field]]
name = "spin"
addr = "@match -> 0x88 -> 0xf0"
type = "f32x4"
[[field]]
name = "vel"
addr = "@match -> 0x88 -> 0x130"
type = "f32x3"
''')
    assert len(w.plan.groups) == 1, [g.chain_text for g in w.plan.groups]
    g = w.plan.groups[0]
    assert g.chain_text == '@match -> 0x88 -> *', g.chain_text
    # exact extents; the quadword rounding happens once the object's address is known
    assert g.spans == [(0xe0, 0x100), (0x130, 0x13c)], g.spans   # pos+spin merged, vel apart


# --- validation -------------------------------------------------------------

@check('validation errors name the line and say what was expected')
def t_errors():
    use_cfg()
    bad = [
        ('[watch]\nframes = 1\n[[field]]\nname = "x"\naddr = "0x100000"\ntype = "u33"\n',
         ':6', 'unknown type'),
        ('[watch]\nframes = 1\n[[field]]\nname = "x"\naddr = "zzz"\ntype = "u32"\n',
         ':5', 'not an address'),
        ('[watch]\nframes = 1\n[[field]]\nname = "x"\naddr = "0x3000000"\ntype = "u32"\n',
         ':4', 'outside EE RAM'),
        ('[watch]\nframes = 1\n[[field]]\nname = "x"\ntype = "u32"\n', ':3', 'no addr'),
        ('[watch]\nframes = 1\nevery = 0\n[[field]]\nname = "x"\naddr = "0x100000"\n',
         ':3', 'positive'),
        ('[watch]\nframes = 1\n[[array]]\nname = "a"\naddr = "0x100000"\ncount = 4\n'
         '[[array.field]]\nname = "x"\noff = 0\n', ':3', 'no stride'),
        ('[watch]\nframes = 1\n[[field]]\nname = "x"\naddr = "0x100000"\n'
         '[[field]]\nname = "x"\naddr = "0x100004"\n', ':7', 'duplicate column'),
        ('[watch]\nframes = 1\nwhen = "nosuch == 1"\n[[field]]\nname = "x"\naddr = "0x100000"\n',
         ':3', 'neither a number'),
        ('[wacth]\nframes = 1\n', ':1', 'unknown table'),
        ('[watch]\nfarmes = 1\n', ':2', 'unknown key'),
        ('[watch]\nframes = 1\n', '', 'nothing to record'),
    ]
    for text, line, want in bad:
        try:
            spec(text)
        except R.SpecError as e:
            msg = str(e)
            assert want in msg, f'{want!r} not in {msg!r}'
            assert line in msg, f'expected line {line!r} in {msg!r}'
        else:
            raise AssertionError(f'no SpecError for {text!r}')


@check('an unknown @symbol is fatal for a capture but survivable in --dry-run')
def t_unresolved():
    use_cfg()
    text = '[watch]\nframes = 1\n[[field]]\nname = "x"\naddr = "@nope"\n'
    try:
        spec(text)
    except R.SpecError as e:
        assert 'nope' in str(e), e
    else:
        raise AssertionError('expected a SpecError')
    w = spec(text, allow_unresolved=True)
    assert w.unresolved == ['@nope'], w.unresolved
    assert 'UNRESOLVED' in w.describe()


# --- the capture loop -------------------------------------------------------

BALL_SPEC = '''
[watch]
name = "ball"
frames = 5
[[field]]
name = "phase"
addr = "@match -> 0x54"
type = "u8"
[[field]]
name = "pos"
addr = "@match -> 0x88 -> 0xe0"
type = "f32x4"
[[array]]
name = "player"
addr = "@match -> 0xa8"
count = 4
stride = 4
deref = true
[[array.field]]
name = "pos"
off = 0x3d70
type = "f32x3"
[[array.field]]
name = "anim"
off = "0x60 -> 0x24"
type = "u16"
'''


@check('pointer chains resolve every frame and land in the CSV')
def t_capture():
    use_cfg()
    ram = fake_game()
    p = FakePine(ram)
    w = spec(BALL_SPEC)
    out = tmp('ball.csv')
    res = R.record(w, out, pine=p, log=lambda s: None)
    assert res.frames == 5, res.frames
    head, data = rows(out)
    assert head == w.columns
    assert [int(r['frame']) for r in data] == [1001, 1002, 1003, 1004, 1005]
    r = data[0]
    assert r['phase'] == '2'
    assert (float(r['pos.x']), float(r['pos.z'])) == (-1.0, 12.0), r
    assert float(r['player2.pos.y']) == 4.5, r['player2.pos.y']
    assert r['player3.anim'] == '259', r['player3.anim']      # 0x103, three derefs deep
    side = json.loads(Path(str(out) + '.json').read_text())
    assert side['capture']['frames'] == 5 and side['capture']['skipped'] == 0
    assert side['game']['serial'] == 'SLUS-20000'
    assert side['spec']['text'].strip().startswith('[watch]')
    assert side['capture']['frame_first'] == 1001 and side['capture']['frame_last'] == 1005


@check('the whole sample is one batched read (plus one per deref level)')
def t_batched():
    use_cfg()
    p = FakePine(fake_game())
    w = spec(BALL_SPEC)
    R.record(w, tmp('b.csv'), pine=p, log=lambda s: None)
    # 5 frames x (3 deref levels + 1 data read); a hand-written script would do ~20 reads a frame.
    assert p.reads == 5 * 4, p.reads


@check('a torn read is skipped, counted and logged — never interpolated')
def t_torn():
    use_cfg()
    p = FakePine(fake_game(), tear={1002, 1003})
    w = spec(BALL_SPEC.replace('frames = 5', 'frames = 4'))
    msgs = []
    out = tmp('torn.csv')
    res = R.record(w, out, pine=p, log=msgs.append)
    assert res.frames == 4 and res.skipped == 2, (res.frames, res.skipped)
    assert [int(r['frame']) for r in rows(out)[1]] == [1001, 1004, 1005, 1006]
    assert any('ticked mid-read' in m for m in msgs), msgs
    assert res.suspect and 'faster than' in res.summary()


@check('a pointer that moves mid-read invalidates the sample')
def t_pointer_moved():
    use_cfg()
    ram = fake_game()
    # on frame 1002 the match pointer is swapped while the batch is in flight
    p = FakePine(ram, move={1002: (MATCH_PTR, MATCH + 0x1000)})
    w = spec(BALL_SPEC.replace('frames = 5', 'frames = 3'))
    msgs = []
    res = R.record(w, tmp('moved.csv'), pine=p, log=msgs.append)
    assert res.skipped == 1, res.skipped
    assert any('changed mid-read' in m for m in msgs), msgs


@check('a null pointer blanks its cells; a required null ends the capture')
def t_null():
    use_cfg()
    ram = fake_game()
    struct.pack_into('<I', ram, MATCH + 0xa8 + 4 * 2, 0)       # player 2 is gone
    p = FakePine(ram)
    out = tmp('null.csv')
    res = R.record(spec(BALL_SPEC), out, pine=p, log=lambda s: None)
    assert res.frames == 5 and res.blanked == 5, (res.frames, res.blanked)
    _, data = rows(out)
    assert data[0]['player2.pos.x'] == '' and data[0]['player2.anim'] == ''
    assert data[0]['player1.pos.x'] == '2.5'
    assert any('null pointer chain' in x for x in res.warnings), res.warnings

    # the same null, but on a required field: a clean stop with the reason recorded
    req = BALL_SPEC.replace('addr = "@match -> 0x88 -> 0xe0"\ntype = "f32x4"',
                            'addr = "@match -> 0x88 -> 0xe0"\ntype = "f32x4"\nrequired = true')
    ram2 = fake_game()
    p2 = FakePine(ram2, per_frame=lambda fp, fr: fp.w32(MATCH + 0x88, 0) if fr == 1003 else None)
    out2 = tmp('req.csv')
    res2 = R.record(spec(req.replace('frames = 5', 'frames = 50')), out2, pine=p2,
                    log=lambda s: None)
    assert res2.frames == 2, res2.frames
    assert "required field 'pos' went null" in res2.ended, res2.ended


@check('start / stop / when gate the rows, and cost no extra reads')
def t_conditions():
    use_cfg()
    ram = fake_game()
    ram[MATCH + 0x54] = 0

    def animate(fp, fr):
        fp.ram[MATCH + 0x54] = {1003: 2, 1008: 5}.get(fr, fp.ram[MATCH + 0x54])
        fp.ram[MATCH + 0x55] = 0 if fr in (1005, 1006) else 1

    p = FakePine(ram, per_frame=animate)
    w = spec('''
[watch]
frames = 50
start = "@match -> 0x54:u8 >= 2"
stop  = "@match -> 0x54:u8 == 5"
when  = "live == 1"
[[field]]
name = "live"
addr = "@match -> 0x55"
type = "u8"
''')
    out = tmp('cond.csv')
    res = R.record(w, out, pine=p, log=lambda s: None)
    frames = [int(r['frame']) for r in rows(out)[1]]
    assert frames == [1003, 1004, 1007], frames      # armed at 1003, 1005/1006 gated out, stop 1008
    assert 'stop' in res.ended, res.ended
    assert 'live' in w.columns and len(w.columns) == 2    # the condition address stayed hidden
    assert p.reads == 8 * 2, p.reads                 # 8 frames x (1 deref level + 1 data read)


@check('a counter gap is reported as dropped frames')
def t_gaps():
    use_cfg()

    class Skippy(FakePine):
        def next_frame(self, last, delay=0.0):
            step = 4 if last == 1002 else 1
            return super().next_frame(last + step - 1, delay)

    p = Skippy(fake_game())
    res = R.record(spec(BALL_SPEC.replace('frames = 5', 'frames = 3')), tmp('gap.csv'),
                   pine=p, log=lambda s: None)
    assert (res.gaps, res.dropped) == (1, 3), (res.gaps, res.dropped)


@check('every = N samples every Nth frame')
def t_every():
    use_cfg()
    p = FakePine(fake_game())
    w = spec(BALL_SPEC.replace('frames = 5', 'frames = 3\nevery = 3'))
    out = tmp('every.csv')
    R.record(w, out, pine=p, log=lambda s: None)
    assert [int(r['frame']) for r in rows(out)[1]] == [1001, 1004, 1007]


@check('setup loads a state, waits warmup frames and plays nothing it was not asked to')
def t_setup():
    use_cfg()
    p = FakePine(fake_game())
    w = spec('''
[watch]
frames = 2
[setup]
state = 5
warmup = 10
settle_ms = 0
[[field]]
name = "phase"
addr = "@match -> 0x54"
type = "u8"
''')
    out = tmp('setup.csv')
    R.record(w, out, pine=p, log=lambda s: None)
    assert p.states == [5], p.states
    assert [int(r['frame']) for r in rows(out)[1]] == [1011, 1012]
    side = json.loads(Path(str(out) + '.json').read_text())
    assert side['setup'] == {'state': 5, 'pad': None, 'warmup': 10}, side['setup']


@check('binary output is packed records with a layout sidecar')
def t_binary():
    use_cfg()
    p = FakePine(fake_game())
    w = spec(BALL_SPEC)
    out = tmp('ball.bin')
    res = R.record(w, out, pine=p, binary=True, log=lambda s: None)
    side = json.loads(Path(str(out) + '.json').read_text())
    n = side['record_bytes']
    assert n == 8 + 1 + 16 + 4 * (12 + 2), n
    raw = out.read_bytes()
    assert len(raw) == n * res.frames, (len(raw), n, res.frames)
    frame, live = struct.unpack_from('<II', raw, 0)
    assert frame == 1001 and live == (1 << len(w.plan.groups)) - 1, (frame, live)
    pos = next(e for e in side['layout'] if e['name'] == 'pos')
    assert struct.unpack_from('<4f', raw, pos['offset']) == (-1.0, 0.25, 12.0, 1.0)


@check('odd addresses and odd pointers still read as 8-aligned regions')
def t_alignment():
    """PINE's batched read is a list of read64s: every region must start 8-aligned and be a
    multiple of 8 long. Rounding a field's offset against its own base gets that wrong the moment
    the base is odd — FakePine.read_regions asserts it, which is how this was caught."""
    use_cfg()
    ram = fake_game()
    PTR = 0x00700000
    struct.pack_into('<I', ram, PTR, BALL + 0xc)          # 4-aligned, not 8
    struct.pack_into('<I', ram, BALL + 0xc + 5, 0xcafef00d)
    ram[0x101001] = 0x5a                                  # clear of the frame counter at @vsync
    struct.pack_into('<H', ram, 0x101006, 0x1234)
    w = spec("""
[watch]
frames = 2
[[field]]
name = "odd_static"
addr = "0x101001"
type = "u8"
[[field]]
name = "odd_tail"
addr = "0x101006"
type = "u16"
[[field]]
name = "odd_live"
addr = "0x700000 -> 0x5"
type = "u32"
fmt  = "hex"
""")
    for lo, n in w.regions:
        assert lo % 8 == 0 and n % 8 == 0, (lo, n)
    out = tmp('odd.csv')
    R.record(w, out, pine=FakePine(ram), log=lambda s: None)
    r = rows(out)[1][0]
    assert (r['odd_static'], r['odd_tail'], r['odd_live']) == ('90', '4660', '0xcafef00d'), r


@check('a capture that got nothing fails loudly instead of leaving a plausible file')
def t_nothing():
    use_cfg()
    p = FakePine(fake_game(), tear=True)
    try:
        R.record(spec(BALL_SPEC.replace('frames = 5', 'frames = 2\nseconds = 0.1')),
                 tmp('empty.csv'), pine=p, log=lambda s: None)
    except R.CaptureFailed as e:
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(buf):   # the failure line goes to stderr
            e.report()
        assert 'no frames captured' in str(e), e
        assert buf.getvalue().startswith('CAPTURE FAILED: '), buf.getvalue()
        assert 'incomplete' in buf.getvalue(), buf.getvalue()
    else:
        raise AssertionError('expected CaptureFailed')


@check('--append refuses a file with different columns')
def t_append():
    use_cfg()
    p = FakePine(fake_game())
    out = tmp('app.csv')
    R.record(spec(BALL_SPEC.replace('frames = 5', 'frames = 2')), out, pine=p, log=lambda s: None)
    R.record(spec(BALL_SPEC.replace('frames = 5', 'frames = 2')), out, pine=FakePine(fake_game()),
             append=True, log=lambda s: None)
    assert len(rows(out)[1]) == 4, rows(out)[1]
    other = spec('[watch]\nframes = 1\n[[field]]\nname = "q"\naddr = "0x100000"\n')
    try:
        R.record(other, out, pine=FakePine(fake_game()), append=True, log=lambda s: None)
    except R.CaptureFailed as e:
        assert 'different columns' in str(e), e
    else:
        raise AssertionError('expected CaptureFailed on mismatched --append')


# --- diff -------------------------------------------------------------------

def write_csv(path: Path, head: list[str], data: list[list]) -> Path:
    with open(path, 'w', newline='', encoding='utf-8') as f:
        wr = csv.writer(f)
        wr.writerow(head)
        wr.writerows(data)
    return path


@check('--diff finds the first divergence, with its frame and both values')
def t_diff():
    use_cfg()
    d = Path(tempfile.mkdtemp(prefix='ps2kit-diff-'))
    head = ['frame', 'x', 'y']
    a = write_csv(d / 'a.csv', head, [[1, 1.5, 2.0], [2, 1.75, 2.0], [3, 2.0, 2.0]])
    b = write_csv(d / 'b.csv', head, [[1, 1.5, 2.0], [2, 1.75, 9.0], [3, 2.5, 2.0]])
    out = io.StringIO()
    rc = R.diff(a, b, 0.0, log=lambda s: print(s, file=out))
    text = out.getvalue()
    assert rc == 1
    assert "first divergence at frame 2, column 'y'" in text, text
    assert '    A  2.0' in text and '    B  9.0' in text, text
    assert '2 of 2 columns differ' in text, text

    out2 = io.StringIO()
    assert R.diff(a, a, 0.0, log=lambda s: print(s, file=out2)) == 0
    assert 'IDENTICAL' in out2.getvalue(), out2.getvalue()

    # a tolerance for the render-ish columns; exact stays exact
    c = write_csv(d / 'c.csv', head, [[1, 1.5000001, 2.0], [2, 1.75, 2.0], [3, 2.0, 2.0]])
    assert R.diff(a, c, 0.0, log=lambda s: None) == 1
    assert R.diff(a, c, 1e-5, log=lambda s: None) == 0

    # shape differences are reported, not papered over
    short = write_csv(d / 's.csv', head, [[1, 1.5, 2.0]])
    out3 = io.StringIO()
    assert R.diff(a, short, 0.0, log=lambda s: print(s, file=out3)) == 1
    assert 'rows only in A: 2' in out3.getvalue(), out3.getvalue()


@check('a capture diffs clean against itself, and dirty against a nudged copy')
def t_diff_capture():
    use_cfg()
    p = FakePine(fake_game())
    a = tmp('cap-a.csv')
    R.record(spec(BALL_SPEC), a, pine=p, log=lambda s: None)
    ram = fake_game()
    struct.pack_into('<f', ram, BALL + 0xe0, -1.0000001)       # one bit of the ball's x
    b = Path(str(a).replace('cap-a', 'cap-b'))
    R.record(spec(BALL_SPEC), b, pine=FakePine(ram), log=lambda s: None)
    assert R.diff(a, a, 0.0, log=lambda s: None) == 0
    out = io.StringIO()
    assert R.diff(a, b, 0.0, log=lambda s: print(s, file=out)) == 1
    assert "column 'pos.x'" in out.getvalue(), out.getvalue()
    assert R.diff(a, b, 1e-5, log=lambda s: None) == 0         # ...and a tolerance hides it


@check('kit record --diff works through cmd_record')
def t_cmd_diff():
    use_cfg()
    d = Path(tempfile.mkdtemp(prefix='ps2kit-cmd-'))
    head = ['frame', 'x']
    a = write_csv(d / 'a.csv', head, [[1, 1], [2, 2]])
    b = write_csv(d / 'b.csv', head, [[1, 1], [2, 3]])
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = R.cmd_record(['--diff', str(a), str(b)])
    assert rc == 1 and 'DIFFER' in buf.getvalue(), (rc, buf.getvalue())
    buf2 = io.StringIO()
    with redirect_stdout(buf2):
        assert R.cmd_record(['--diff', str(a), str(a)]) == 0
    assert 'IDENTICAL' in buf2.getvalue()


@check('--dry-run prints a plan for the shipped specs with no kit.toml and no emulator')
def t_dry_run():
    # deliberately *not* using the fake config: this is the "first five minutes" path, where
    # ps2kit.config has no kit.toml to read and every @symbol is unknown.
    import ps2kit.config as C
    R.cfg = C.cfg
    for name in ('example.toml', 'frame.toml', 'struct.toml'):
        path = Path(__file__).resolve().parent.parent / 'watch' / name
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = R.cmd_record(['--dry-run', str(path)])
        text = buf.getvalue()
        assert rc == 0, (name, rc, text)
        assert 'bytes/frame' in text and 'group' in text, (name, text)
    use_cfg()


if __name__ == '__main__':
    print(f'ps2kit.record tests  (python {sys.version.split()[0]})')
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
    print()
    print(f'{len(PASS)} passed, {len(FAIL)} failed')
    for name, e in FAIL:
        print(f'  FAIL {name}: {e}')
    sys.exit(1 if FAIL else 0)
