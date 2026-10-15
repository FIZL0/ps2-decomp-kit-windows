"""Frame-by-frame captures of the running game, driven by a declarative *watch spec*.

Why a spec instead of a script
------------------------------
The project this kit was generalized from accumulated 55 hand-written capture scripts
(`record_ball.py`, `record_camera.py`, `record_ai_rally.py`, …). Every one of them was a copy of the
last with different addresses, and every one of them re-implemented, slightly differently, the same
four things that are actually hard:

1. **One batched read per frame.** The EE keeps running while PINE reads. Forty separate `read32`
   calls take longer than a frame, so the samples come from several different frames and the capture
   is quietly wrong. Everything must go out as one message, which means the fields have to be
   coalesced into as few contiguous blocks as possible — tedious by hand, trivial for a planner.
2. **Torn reads.** Even one batch can straddle a frame boundary. The only honest answer is to read
   twice, keep the sample when the two agree inside one frame, and *skip and count* the frame when
   they do not. Half the hand-written scripts forgot the counting, so nobody could tell a clean
   capture from a shredded one.
3. **Pointer chains.** Live objects move. `*(*(gm+0xa8)+0x3d70)` has to be re-resolved every frame,
   the derefs want batching too, and a null in the middle must leave a hole in the CSV rather than
   abort an otherwise good 20-minute run.
4. **Provenance.** The single most expensive recurring failure in that project was a capture file
   nobody could reconstruct: which save state, which pad input, which build, which frames. A capture
   with no origin is not evidence, it is a 40 MB rumour.

So: the user writes what they want sampled, the kit does the above. One spec file per question,
checked into the repo next to the test that consumes it, and `--dry-run` to debug it without even
starting the emulator.

    kit record watch/ball.toml --state 5 --frames 600 --out context/capture/ball.csv
    kit record --dry-run watch/ball.toml
    kit record --diff context/capture/ball.csv target/port-ball.csv

The format is documented in `watch/README.md`; `watch/example.toml` is a commented tutorial.
"""
from __future__ import annotations

import csv
import hashlib
import importlib
import json
import os
import re
import struct
import sys
import time
import tomllib
from bisect import bisect_right
from dataclasses import dataclass, field as dcfield
from datetime import datetime, timezone
from pathlib import Path

from ps2kit import platform_ as plat
from ps2kit.config import cfg, num, ROOT

# PINE's batch read is a pile of read64 ops, so every region must start 8-aligned and be a multiple
# of 8 bytes long. The planner widens spans to suit; it is not optional, and getting it wrong in a
# hand-written script produced an off-by-4 that looked like a physics bug for a day.
QUAD = 8

# A sample is only worth keeping if the two reads agreed. Log the first few skips, then just count:
# an agent's context is not a dumping ground (docs/INTERNALS.md rule 3).
MAX_SKIP_LOGS = 10
PROGRESS_EVERY = 300          # frames between progress lines
SIDECAR_EVERY = 120           # frames between provenance rewrites, so a kill leaves honest counts
SUSPECT_SKIP_RATIO = 0.02     # above this, say the capture is suspect and why


class CaptureFailed(Exception):
    """Anything that makes the output untrustworthy. Printed with the kit's one unmistakable prefix."""

    def __init__(self, what: str, wrote: bool = True):
        super().__init__(what)
        self.what = what
        self.wrote = wrote

    def report(self) -> None:
        tail = 'anything this run wrote is incomplete' if self.wrote else 'nothing was written'
        print(f'CAPTURE FAILED: {self.what} — {tail}', file=sys.stderr, flush=True)


# --- types -------------------------------------------------------------------

@dataclass(frozen=True)
class Kind:
    name: str
    size: int
    fmt: str                      # struct format, '' for bytes:N
    cols: tuple[str, ...]         # column suffixes; () = one column named after the field
    float_: bool = False


_MAT = tuple(f'm{r}{c}' for r in range(4) for c in range(4))
KINDS: dict[str, Kind] = {
    'u8': Kind('u8', 1, '<B', ()), 'u16': Kind('u16', 2, '<H', ()),
    'u32': Kind('u32', 4, '<I', ()), 'u64': Kind('u64', 8, '<Q', ()),
    's8': Kind('s8', 1, '<b', ()), 's16': Kind('s16', 2, '<h', ()), 's32': Kind('s32', 4, '<i', ()),
    'f32': Kind('f32', 4, '<f', (), True),
    'f32x3': Kind('f32x3', 12, '<3f', ('x', 'y', 'z'), True),
    'f32x4': Kind('f32x4', 16, '<4f', ('x', 'y', 'z', 'w'), True),
    'mat4': Kind('mat4', 64, '<16f', _MAT, True),
}


def _kind(text: str, where: str) -> Kind:
    t = str(text).strip()
    if t in KINDS:
        return KINDS[t]
    if m := re.fullmatch(r'bytes:(\d+)', t):
        n = int(m.group(1))
        if n < 1 or n > 4096:
            raise SpecError(where, f'bytes:{n} is out of range (1..4096)')
        return Kind(t, n, '', ())
    raise SpecError(where, f'unknown type {t!r}; expected one of '
                           f'{" ".join(KINDS)} or bytes:N')


# --- spec errors, with the line they came from -------------------------------

class SpecError(Exception):
    def __init__(self, where: str, what: str):
        super().__init__(f'{where}: {what}')
        self.where, self.what = where, what


class _Lines:
    """Maps a spec key to its line number. `tomllib` throws the positions away, so find them in the
    text: track the current table header, count repeats of array-of-table headers, and remember
    where each key inside it was assigned. Good enough to put a caret on the user's mistake, which
    is the whole point of a declarative format."""

    def __init__(self, text: str, path: str):
        self.path = path
        self.at: dict[tuple[str, int, str], int] = {}
        self.header: dict[tuple[str, int], int] = {}
        counts: dict[str, int] = {}
        table, idx = '', 0
        for n, line in enumerate(text.splitlines(), 1):
            s = line.split('#', 1)[0].strip()
            if m := re.fullmatch(r'\[\[\s*([\w.\-]+)\s*\]\]', s):
                table = m.group(1)
                idx = counts.get(table, 0)
                counts[table] = idx + 1
                self.header[(table, idx)] = n
            elif m := re.fullmatch(r'\[\s*([\w.\-]+)\s*\]', s):
                table, idx = m.group(1), 0
                self.header[(table, idx)] = n
            elif m := re.match(r'([\w\-]+)\s*=', s):
                self.at.setdefault((table, idx, m.group(1)), n)

    def where(self, table: str = '', idx: int = 0, key: str = '') -> str:
        n = self.at.get((table, idx, key)) or self.header.get((table, idx))
        return f'{self.path}:{n}' if n else f'{self.path}'


# --- addresses and pointer chains --------------------------------------------

@dataclass
class Chain:
    """Where a value lives: a base, then zero or more "deref and add" steps, then a final offset.

        a = base
        for step in steps:  a = read32(a) + step
        address = a + off

    So `@match -> 0xa8 -> 0x3fb0` means: read the pointer at @match, add 0xa8, read *that* pointer,
    add 0x3fb0 — one indirection per arrow, which is how a symbol that holds a pointer to a live
    object (`@match`) reaches a field inside it. A chain with no arrows is a plain address, resolved
    once at plan time; everything with an arrow is re-resolved every frame, because objects move.
    """
    text: str
    base: int | None                  # None when the symbol could not be resolved (dry-run only)
    base_text: str = ''
    steps: tuple[int, ...] = ()
    off: int = 0                      # added after the last step (array element / field offset)

    @property
    def node(self) -> tuple:
        """Identity of the pointer-trie node this chain hangs off. Chains sharing a prefix share
        their pointer reads, so ten fields inside one object cost one deref, not ten."""
        return (self.base, self.base_text, self.steps)

    @property
    def static(self) -> bool:
        return not self.steps and self.base is not None

    def canon(self) -> Chain:
        """Push the last arrow's offset into `off`, so the trie node is *the object a pointer points
        at* rather than one address inside it.

        `@match -> 0x54` and `@match -> 0x100` then hang off the same node — the match object — and
        their reads coalesce into one region instead of two groups that happen to land next to each
        other. The hand-written scripts got this for free by writing `gm = read32(GM_PTR)` once at
        the top; a declarative spec has to work it out.
        """
        if not self.steps:
            return self
        return Chain(self.text, self.base, self.base_text, self.steps[:-1] + (0,),
                     self.off + self.steps[-1])

    def __str__(self) -> str:
        s = self.base_text or (f'{self.base:#x}' if self.base is not None else '?')
        for st in self.steps:
            s += f' -> {st:#x}'
        if self.off:
            s += f' + {self.off:#x}'
        return s


NODE_BASE, NODE_TEXT, NODE_STEPS = 0, 1, 2


def node_parent(n: tuple) -> tuple:
    return (n[NODE_BASE], n[NODE_TEXT], n[NODE_STEPS][:-1])


def node_label(n: tuple) -> str:
    """How a trie node is named in --dry-run output, warnings and the sidecar. A canonical node ends
    in a 0 step — "the object this pointer points at" — which reads better as `-> *`."""
    steps = list(n[NODE_STEPS])
    base = n[NODE_TEXT] or (f'{n[NODE_BASE]:#x}' if n[NODE_BASE] is not None else '?')
    tail = ' -> *' if steps and steps[-1] == 0 else ''
    if tail:
        steps = steps[:-1]
    return base + ''.join(f' -> {st:#x}' for st in steps) + tail


_ADDR_RE = re.compile(r'^(@[\w.\-]+|0[xX][0-9a-fA-F_]+|[0-9a-fA-F_]+)$')


def _resolve(tok, where: str, allow_unresolved: bool, unresolved: list[str]) -> int | None:
    """'@name', '0x1004c0' or '1004c0' → an address. Always via cfg().sym, never int(s, 16):
    kit.toml's [symbols] is what keeps a watch spec readable a year later."""
    if isinstance(tok, int):
        return tok                    # `addr = 0x1004c0` written as a TOML integer
    tok = str(tok).strip()
    if not _ADDR_RE.match(tok):
        raise SpecError(where, f'{tok!r} is not an address; expected 0x…, bare hex, or @symbol')
    try:
        return cfg(required=False).sym(tok)
    except SystemExit as e:
        if not allow_unresolved:
            raise SpecError(where, f'{str(e).strip()} (needed by {tok})')
        unresolved.append(tok)
        return None


def parse_chain(text: str, where: str, *, allow_unresolved: bool,
                unresolved: list[str]) -> Chain:
    """`"@match -> 0xa8"` → a Chain. The first token is an absolute address or an @symbol, which may
    carry plain `+ n` offsets (`"@globals + 0x24"`); each `-> n` after it is "read the pointer here,
    add n"."""
    if isinstance(text, int):
        return Chain(f'{text:#x}', text, '', (), 0)
    parts = [p.strip() for p in str(text).split('->')]
    head = [t.strip() for t in parts[0].split('+')]
    if not head[0]:
        raise SpecError(where, f'empty address in {text!r}')
    base = _resolve(head[0], where, allow_unresolved, unresolved)
    lead = sum(_off(t, where) for t in head[1:])
    steps = tuple(_sum(p, where) for p in parts[1:])
    label = head[0] if head[0].startswith('@') else ''
    if steps:                         # the first deref happens at base + lead
        return Chain(str(text), None if base is None else base + lead, label, steps, 0)
    return Chain(str(text), base, label, (), lead)


def parse_rel(text: str, where: str) -> tuple[int, tuple[int, ...]]:
    """An offset *relative to something already resolved* (an array element, a struct base):

        off = "0x3d70"          →  (0x3d70, ())        element + 0x3d70
        off = "0x5c -> 0x24"    →  (0x5c, (0x24,))     read the pointer at element+0x5c, add 0x24

    Same rule as an absolute chain, only the starting point differs.
    """
    if isinstance(text, int):
        return text, ()
    parts = [p.strip() for p in str(text).split('->')]
    if not parts[0]:
        raise SpecError(where, f'empty offset in {text!r}')
    return _sum(parts[0], where), tuple(_sum(p, where) for p in parts[1:])


def compose(c: Chain, first: int, steps: tuple[int, ...], text: str) -> Chain:
    """Hang a relative offset (from `parse_rel`) off the end of a resolved chain.

    The chain model can only add an offset straight after a deref, so when the relative part derefs
    again, the pending offset is folded into the previous step (or into the base when there is no
    previous step). Getting this fold wrong is how a hand-written script ends up reading a player
    object through the *ball*'s pointer.
    """
    add = c.off + first
    if not steps:
        return Chain(text, c.base, c.base_text, c.steps, add)
    if c.steps:
        return Chain(text, c.base, c.base_text, c.steps[:-1] + (c.steps[-1] + add,) + steps, 0)
    return Chain(text, None if c.base is None else c.base + add, c.base_text, steps, 0)


def _sum(tok, where: str) -> int:
    """`0x60`, `0x60 + 4`, `0x60 - 8` — offsets add up, which saves a user computing struct maths in
    their head and getting it wrong in a way nothing can detect later."""
    if isinstance(tok, int):
        return tok
    tok = str(tok).strip()
    terms = [t.strip() for t in re.findall(r'[-+]?[^-+]+', tok)]   # hex digits never hold + or -
    if not terms:
        raise SpecError(where, f'{tok!r} is not an offset')
    return sum(_off(t[1:] if t.startswith('+') else t, where) for t in terms)


def _off(tok, where: str) -> int:
    # TOML already parsed `off = 0x10` into the int 16. Re-rendering it and reading it back as bare
    # hex turns 0x10 into 0x16 — a 6-byte shift that reads neighbouring fields and looks like a
    # plausible value. Numbers stay numbers; only strings get the bare-hex convention.
    if isinstance(tok, int):
        return tok
    tok = str(tok).strip()
    neg = tok.startswith('-')
    if neg:
        tok = tok[1:].strip()
    if not re.fullmatch(r'(0[xX])?[0-9a-fA-F_]+', tok):
        raise SpecError(where, f'{tok!r} is not an offset; expected a number like 0xa8 or 168')
    v = num(tok if tok.lower().startswith('0x') else '0x' + tok, 'offset')
    return -v if neg else v


def ee_ok(addr: int, size: int, ram: int) -> bool:
    """Is [addr, addr+size) inside EE RAM? kseg0/kseg1 addresses are masked down first — the
    decompiler shows 0x80xxxxxx, PINE wants the physical offset, and that mismatch has eaten whole
    afternoons."""
    a = addr & 0x1fffffff if addr >= 0x80000000 else addr
    return 0 <= a and a + size <= ram


def ee_addr(addr: int) -> int:
    return addr & 0x1fffffff if addr >= 0x80000000 else addr


# --- fields ------------------------------------------------------------------

@dataclass(eq=False)                 # identity, not value: fields are dict keys in the capture loop
class Field:
    name: str
    chain: Chain
    kind: Kind
    scale: float = 1.0
    fmt: str = ''
    required: bool = False        # a null chain ends the capture cleanly instead of blanking a cell
    hidden: bool = False          # pulled in by a condition, not written to the output
    where: str = ''

    @property
    def columns(self) -> list[str]:
        return [f'{self.name}.{c}' for c in self.kind.cols] or [self.name]

    def decode(self, blob: bytes, at: int) -> list:
        if not self.kind.fmt:
            return [blob[at:at + self.kind.size].hex()]
        vals = list(struct.unpack_from(self.kind.fmt, blob, at))
        if self.scale != 1.0:
            vals = [v * self.scale for v in vals]
        return vals

    def pack(self, vals: list) -> bytes:
        if not self.kind.fmt:
            raw = bytes.fromhex(vals[0]) if vals else b''
            return raw.ljust(self.kind.size, b'\0')[:self.kind.size]
        if not vals:
            return b'\0' * self.kind.size
        return struct.pack(self.kind.fmt, *vals)

    def render(self, vals: list) -> list[str]:
        if not vals:
            return [''] * len(self.columns)
        if not self.kind.fmt:
            return [vals[0]]
        out = []
        for v in vals:
            if self.fmt == 'hex':
                out.append(f'0x{int(v):x}')
            elif self.fmt:
                out.append(self.fmt % v)
            elif self.kind.float_ or isinstance(v, float):
                out.append(repr(float(v)))      # round-trips: a bit-exact proof needs every bit
            else:
                out.append(str(v))
        return out


# --- conditions --------------------------------------------------------------
# Deliberately tiny: `A op B`, joined by `and` / `or`, where an operand is a declared field, an
# address (which becomes a hidden field, so it rides along in the same batch and costs nothing),
# or a number. Anything richer belongs in the analysis step, not in the capture loop.

_OPS = {'==': lambda a, b: a == b, '!=': lambda a, b: a != b, '<': lambda a, b: a < b,
        '<=': lambda a, b: a <= b, '>': lambda a, b: a > b, '>=': lambda a, b: a >= b}
# The `>` of a pointer chain's `->` is not a comparison: `@match -> 0x54:u8 >= 2` must split
# at the `>=`, not inside the arrow, or the condition silently compares nonsense.
_OP_RE = re.compile(r'(==|!=|<=|>=|(?<!-)<|(?<!-)>)')


@dataclass
class Term:
    kind: str                     # 'col' | 'lit'
    col: str = ''
    value: float = 0.0


@dataclass
class Cond:
    text: str
    groups: list[list[tuple[Term, str, Term]]]   # or-of-ands

    def eval(self, row: dict) -> bool | None:
        """True/False, or None when something it needs was not sampled (a null pointer chain)."""
        any_unknown = False
        for ands in self.groups:
            ok = True
            for lhs, op, rhs in ands:
                a, b = _term(lhs, row), _term(rhs, row)
                if a is None or b is None:
                    any_unknown, ok = True, False
                    break
                if not (_OPS[op](a, b) if op else a != 0):
                    ok = False
                    break
            if ok:
                return True
        return None if any_unknown else False


def _term(t: Term, row: dict):
    if t.kind == 'lit':
        return t.value
    v = row.get(t.col, None)
    if v == '' or v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# --- the spec ----------------------------------------------------------------

@dataclass
class Group:
    """One pointer-trie node that has fields hanging off it, plus the aligned spans to read."""
    node: tuple
    base: int | None
    steps: tuple[int, ...]
    chain_text: str
    fields: list[tuple[Field, int]] = dcfield(default_factory=list)   # (field, offset from node)
    spans: list[tuple[int, int]] = dcfield(default_factory=list)      # (lo, hi) from node, aligned

    @property
    def dynamic(self) -> bool:
        return bool(self.steps)

    @property
    def nbytes(self) -> int:
        # What the read actually costs once the spans are snapped to the quadword grid, assuming an
        # aligned base (PS2 objects nearly always are). The spans themselves stay exact so that two
        # fields a byte apart still merge, whatever the base turns out to be at runtime.
        return sum(hi - lo for lo, hi in _align(self.spans))


@dataclass
class Plan:
    groups: list[Group]
    nodes: list[tuple]                 # every trie node, parents before children
    frame_bytes: int
    columns: list[str]


class Watch:
    """A parsed, validated, planned watch spec.

    `Watch('watch/ball.toml')` raises `SpecError` naming the offending line for anything wrong.
    With `allow_unresolved=True` (what `--dry-run` uses) unknown `@symbols` are collected in
    `.unresolved` instead, so a spec can be checked with no kit.toml at all.
    """

    def __init__(self, path: str | Path | None = None, *, text: str | None = None,
                 allow_unresolved: bool = False, name: str = ''):
        self.path = str(path) if path else '<text>'
        if text is None:
            p = Path(self.path)
            if not p.exists():
                raise SpecError(self.path, 'no such watch spec')
            text = p.read_text(encoding='utf-8')
        self.text = text
        self.sha = hashlib.sha256(text.encode()).hexdigest()[:16]
        self.unresolved: list[str] = []
        self._allow = allow_unresolved
        try:
            raw = tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            raise SpecError(f'{self.path}:{getattr(e, "lineno", "?")}', f'not valid TOML: {e}')
        self.raw = raw
        self.ln = _Lines(text, self.path)
        self.ram = cfg(required=False).ram
        self._unknown_tables(raw)

        w = raw.get('watch', {}) or {}
        s = raw.get('setup', {}) or {}
        self._known(w, 'watch', ('name', 'description', 'every', 'frames', 'seconds', 'when',
                                 'start', 'stop', 'format'))
        self._known(s, 'setup', ('state', 'pad', 'warmup', 'settle_ms'))
        self.name = name or str(w.get('name') or Path(self.path).stem)
        self.description = str(w.get('description') or '')
        self.every = self._pos(w, 'watch', 'every', 1)
        self.frames = self._pos(w, 'watch', 'frames', 0)
        self.seconds = float(w.get('seconds') or 0)
        self.format = str(w.get('format') or 'csv')
        if self.format not in ('csv', 'binary'):
            raise SpecError(self.ln.where('watch', 0, 'format'),
                            f'format must be "csv" or "binary", not {self.format!r}')
        self.state = s.get('state')
        self.state = None if self.state in (None, '') else int(self.state)
        self.pad = str(s.get('pad') or '')
        self.warmup = self._pos(s, 'setup', 'warmup', 0)
        self.settle_ms = self._pos(s, 'setup', 'settle_ms', 300)

        self.fields: list[Field] = []
        self._by_col: dict[str, Field] = {}
        self._parse_fields(raw)
        self._parse_arrays(raw)
        if not [f for f in self.fields if not f.hidden]:
            raise SpecError(self.path, 'no [[field]] or [[array]] entries — nothing to record')

        self.when = self._cond(w, 'when')
        self.start = self._cond(w, 'start')
        self.stop = self._cond(w, 'stop')
        self.plan = self._plan()

    # -- parsing ------------------------------------------------------------
    def _unknown_tables(self, raw: dict) -> None:
        for k in raw:
            if k not in ('watch', 'setup', 'field', 'array'):
                raise SpecError(self.ln.where(k, 0), f'unknown table [{k}]; expected '
                                                     '[watch], [setup], [[field]] or [[array]]')

    def _known(self, table: dict, name: str, keys: tuple[str, ...]) -> None:
        for k in table:
            if k not in keys:
                raise SpecError(self.ln.where(name, 0, k),
                                f'unknown key {k!r} in [{name}]; expected one of {", ".join(keys)}')

    def _pos(self, table: dict, name: str, key: str, default: int) -> int:
        v = table.get(key)
        if v in (None, ''):
            return default
        try:
            v = int(v)
        except (TypeError, ValueError):
            raise SpecError(self.ln.where(name, 0, key), f'{key} must be a whole number, got {v!r}')
        if v < 0 or (key == 'every' and v < 1):
            raise SpecError(self.ln.where(name, 0, key), f'{key} must be positive, got {v}')
        return v

    def _add(self, f: Field) -> None:
        f.chain = f.chain.canon()
        for c in f.columns:
            if c in self._by_col:
                raise SpecError(f.where, f'duplicate column {c!r} (already defined at '
                                         f'{self._by_col[c].where})')
            self._by_col[c] = f
        self.fields.append(f)

    def _check_static(self, f: Field) -> None:
        if f.chain.static:
            a = f.chain.base + f.chain.off
            if not ee_ok(a, f.kind.size, self.ram):
                raise SpecError(f.where, f'{a:#x}+{f.kind.size} is outside EE RAM '
                                         f'(0..{self.ram:#x}); wrong address, or set '
                                         f'[emu] ram_mb in kit.toml')

    def _parse_fields(self, raw: dict) -> None:
        for i, d in enumerate(raw.get('field', []) or []):
            at = self.ln.where('field', i, 'name')
            self._known(d, 'field', ('name', 'addr', 'type', 'scale', 'fmt', 'required'))
            name = str(d.get('name') or '')
            if not re.fullmatch(r'[A-Za-z_][\w\-]*', name):
                raise SpecError(self.ln.where('field', i, 'name') if name else self.ln.where('field', i),
                                f'name {name!r} is missing or not an identifier')
            if 'addr' not in d:
                raise SpecError(self.ln.where('field', i), f'field {name!r} has no addr')
            where = self.ln.where('field', i, 'addr')
            kind = _kind(d.get('type', 'u32'), self.ln.where('field', i, 'type'))
            ch = parse_chain(d['addr'], where, allow_unresolved=self._allow,
                             unresolved=self.unresolved)
            f = Field(name, ch, kind, float(d.get('scale', 1.0) or 1.0), str(d.get('fmt') or ''),
                      bool(d.get('required')), False, at)
            self._check_static(f)
            self._add(f)

    def _parse_arrays(self, raw: dict) -> None:
        for i, d in enumerate(raw.get('array', []) or []):
            at = self.ln.where('array', i, 'name')
            self._known(d, 'array', ('name', 'addr', 'count', 'stride', 'deref', 'field'))
            name = str(d.get('name') or '')
            if not re.fullmatch(r'[A-Za-z_][\w\-]*', name):
                raise SpecError(at, f'array name {name!r} is missing or not an identifier')
            for key in ('addr', 'count'):
                if key not in d:
                    raise SpecError(self.ln.where('array', i), f'array {name!r} has no {key}')
            count = int(d['count'])
            if count < 1 or count > 1024:
                raise SpecError(self.ln.where('array', i, 'count'),
                                f'count must be 1..1024, got {count}')
            deref = bool(d.get('deref'))
            stride = d.get('stride')
            if stride in (None, ''):
                if count > 1:
                    raise SpecError(self.ln.where('array', i),
                                    f'array {name!r} has count {count} but no stride')
                stride = 0
            stride = _sum(stride, self.ln.where('array', i, 'stride'))
            base = parse_chain(d['addr'], self.ln.where('array', i, 'addr'),
                               allow_unresolved=self._allow, unresolved=self.unresolved)
            subs = d.get('field') or []
            if not subs:
                raise SpecError(self.ln.where('array', i),
                                f'array {name!r} has no [[array.field]] entries')
            # Validate every sub-field once, then expand element by element: the columns (and the
            # binary records) then read player0.*, player1.* … rather than interleaving the
            # sub-fields, which is what anyone eyeballing the CSV expects.
            checked = []
            for e, sub in enumerate(subs):
                self._known(sub, 'array.field', ('name', 'off', 'type', 'scale', 'fmt', 'required'))
                sname = str(sub.get('name') or '')
                swhere = self.ln.where('array.field', i * len(subs) + e, 'name')
                if not re.fullmatch(r'[A-Za-z_][\w\-]*', sname):
                    raise SpecError(swhere, f'array.field name {sname!r} is missing or not an identifier')
                if 'off' not in sub:
                    raise SpecError(swhere, f'array.field {sname!r} has no off')
                checked.append((sname, sub, _kind(sub.get('type', 'u32'), swhere), swhere))
            for k in range(count):
                for sname, sub, kind, swhere in checked:
                    ch = self._element(base, k, stride, deref, sub['off'], swhere)
                    f = Field(f'{name}{k}.{sname}', ch, kind, float(sub.get('scale', 1.0) or 1.0),
                              str(sub.get('fmt') or ''), bool(sub.get('required')), False, swhere)
                    self._check_static(f)
                    self._add(f)

    def _element(self, base: Chain, k: int, stride: int, deref: bool, off, where: str) -> Chain:
        """Element k of an array: `base + k*stride`, or the pointer stored there when deref = true.
        The sub-field's `off` (itself possibly a chain) is then applied relative to the element."""
        elem = Chain(base.text, base.base, base.base_text, base.steps, base.off + k * stride)
        label = f'{base.text or base.base_text} + {k}*{stride:#x}'
        if deref:
            elem = compose(elem, 0, (0,), label + ' ->')   # the element is itself a pointer
        first, steps = parse_rel(off, where)
        return compose(elem, first, steps, f'{label} + {off}')

    def _cond(self, w: dict, key: str) -> Cond | None:
        text = str(w.get(key) or '').strip()
        if not text:
            return None
        where = self.ln.where('watch', 0, key)
        groups = []
        for alt in re.split(r'\s+or\s+', text):
            ands = []
            for part in re.split(r'\s+and\s+', alt.strip()):
                ands.append(self._term3(part.strip(), where))
            groups.append(ands)
        return Cond(text, groups)

    def _term3(self, part: str, where: str) -> tuple[Term, str, Term]:
        bits = _OP_RE.split(part, maxsplit=1)
        if len(bits) == 1:
            return self._term(part, where), '', Term('lit', value=0.0)
        lhs, op, rhs = bits[0].strip(), bits[1], bits[2].strip()
        if not lhs or not rhs:
            raise SpecError(where, f'{part!r} is not a comparison; expected "<thing> {op} <value>"')
        return self._term(lhs, where), op, self._term(rhs, where)

    def _term(self, tok: str, where: str) -> Term:
        tok = tok.strip()
        if re.fullmatch(r'-?\d+\.\d*([eE][-+]?\d+)?', tok):
            return Term('lit', value=float(tok))
        if re.fullmatch(r'-?(0[xX][0-9a-fA-F_]+|\d+)', tok):
            neg = tok.startswith('-')
            return Term('lit', value=float(num(tok.lstrip('-')) * (-1 if neg else 1)))
        if tok in self._by_col:
            return Term('col', col=tok)
        if tok.startswith('@') or '->' in tok or re.match(r'^0[xX]', tok):
            # an address in a condition becomes a hidden field: it then rides in the same batched
            # read as everything else, so a `when` costs nothing per frame.
            body, _, ty = tok.partition(':')
            kind = _kind(ty or 'u32', where)
            ch = parse_chain(body, where, allow_unresolved=self._allow, unresolved=self.unresolved)
            name = '_c%d' % sum(1 for f in self.fields if f.hidden)
            f = Field(name, ch, kind, 1.0, '', False, True, where)
            self._check_static(f)
            self._add(f)
            return Term('col', col=f.columns[0])
        known = ', '.join(list(self._by_col)[:8]) or 'none'
        raise SpecError(where, f'{tok!r} is neither a number, an address (@sym / 0x…) nor a field '
                               f'name (have: {known})')

    # -- planning -----------------------------------------------------------
    def _plan(self) -> Plan:
        """Group the fields by pointer-trie node, coalesce each group's offsets into aligned spans,
        and order the nodes parents-first so the resolver can read one level per batch."""
        groups: dict[tuple, Group] = {}
        for f in self.fields:
            n = f.chain.node
            g = groups.get(n)
            if g is None:
                g = groups[n] = Group(n, f.chain.base, f.chain.steps, node_label(n))
            g.fields.append((f, f.chain.off))
        for g in groups.values():
            # Exact extents, merged. Alignment waits until the absolute address is known: see
            # `_align`, which is the only place that may widen a region.
            g.spans = [(lo, hi) for lo, hi in _coalesce(
                [(off, off + f.kind.size) for f, off in g.fields])]
        nodes: list[tuple] = []
        seen: set[tuple] = set()

        def want(n: tuple) -> None:
            if n in seen:
                return
            if n[NODE_STEPS]:
                want(node_parent(n))
            seen.add(n)
            nodes.append(n)

        for n in groups:
            want(n)
        order = sorted(groups.values(), key=lambda g: (len(g.steps), g.chain_text))
        cols = ['frame'] + [c for f in self.fields if not f.hidden for c in f.columns]
        return Plan(order, nodes, sum(g.nbytes for g in order), cols)

    # -- what the rest of the kit uses --------------------------------------
    @property
    def columns(self) -> list[str]:
        return self.plan.columns

    @property
    def regions(self) -> list[tuple[int, int]]:
        """The fixed regions: every field whose address needs no pointer chasing, coalesced.
        Dynamic groups are planned per frame instead, because their base moves — the capture loop
        coalesces those together with these every frame."""
        spans = []
        for g in self.plan.groups:
            if not g.dynamic and g.base is not None:
                spans += [(ee_addr(g.base) + lo, ee_addr(g.base) + hi) for lo, hi in g.spans]
        return [(lo, hi - lo) for lo, hi in _coalesce(_align(spans))]

    def describe(self) -> str:
        out = [f'{self.path}  ({self.name})']
        if self.description:
            out.append(f'  {self.description}')
        out.append('')
        out.append(f'  sample     every {self.every} frame(s)' +
                   (f', up to {self.frames} frames' if self.frames else '') +
                   (f', up to {self.seconds:g}s' if self.seconds else ''))
        for key, c in (('when', self.when), ('start', self.start), ('stop', self.stop)):
            if c:
                out.append(f'  {key:<10} {c.text}')
        setup = []
        if self.state is not None:
            setup.append(f'load state {self.state}')
        if self.pad:
            setup.append(f'pad script {self.pad}')
        if self.warmup:
            setup.append(f'wait {self.warmup} frames')
        out.append(f'  setup      {", ".join(setup) or "nothing (records the game as it stands)"}')
        out.append(f'  output     {self.format}')
        out.append('')
        shown = [f for f in self.fields if not f.hidden]
        out.append(f'  {len(shown)} fields, {len(self.columns) - 1} columns, '
                   f'{len(self.plan.groups)} group(s), {self.plan.frame_bytes} bytes/frame')
        out.append('')
        for g in self.plan.groups:
            head = ('static' if not g.dynamic else f'{len(g.steps)} deref(s)')
            out.append(f'  group {g.chain_text}   [{head}]  '
                       f'{len(g.spans)} region(s), {g.nbytes} bytes')
            for lo, hi in g.spans:
                if g.base is not None and not g.dynamic:
                    out.append(f'      {ee_addr(g.base) + lo:#010x}..{ee_addr(g.base) + hi:#010x}'
                               f'  ({hi - lo} bytes)')
                else:
                    out.append(f'      +{lo:#06x}..+{hi:#06x}  ({hi - lo} bytes from the group base)')
            for f, off in sorted(g.fields, key=lambda x: x[1])[:200]:
                flag = ' required' if f.required else (' (condition)' if f.hidden else '')
                out.append(f'        +{off:#06x}  {f.kind.name:<8} {f.name}{flag}')
        if self.unresolved:
            out.append('')
            out.append('  UNRESOLVED (add them to [symbols] in kit.toml, or use raw addresses):')
            for u in sorted(set(self.unresolved)):
                out.append(f'      {u}')
        return '\n'.join(out)


def _align(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Snap absolute [lo, hi) spans out to the quadword grid.

    PINE's batched read is a list of read64s, so every region has to start 8-aligned and be a
    multiple of 8 long. A group's offsets are rounded relative to its base, which is only the same
    thing when the base is itself aligned — a field at an odd address, or an object behind a
    4-byte-aligned pointer, is not. Snapping here (after the base is known) is the only place that
    can get it right for both the fixed and the per-frame regions.
    """
    return [(lo & ~(QUAD - 1), (hi + QUAD - 1) & ~(QUAD - 1)) for lo, hi in spans]


def _coalesce(spans: list[tuple[int, int]]) -> list[list[int]]:
    """Merge touching or overlapping [lo, hi) spans. This is the whole point of the planner: forty
    scattered reads become two or three contiguous blocks, which fit in one PINE message and arrive
    from one frame."""
    out: list[list[int]] = []
    for lo, hi in sorted(spans):
        if out and lo <= out[-1][1]:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return out


# --- per-frame resolution ----------------------------------------------------

def _split(result, sizes: list[int]) -> list[bytes]:
    """`read_regions`/`settle` hand back the regions' bytes concatenated (that is what the reference
    PINE does); accept a list of blocks too, so the recorder does not care which the kit's pine.py
    chose."""
    if isinstance(result, (list, tuple)):
        return [bytes(x) for x in result]
    buf = bytes(result)
    out, at = [], 0
    for n in sizes:
        out.append(buf[at:at + n])
        at += n
    return out


class Resolver:
    """Re-resolves the pointer chains every frame, one batched read per level of indirection.

    Two things here are worth more than they look:
      * levels are batched (all the depth-1 pointers in one message, then all the depth-2 ones), so
        a three-deep spec costs three small messages instead of thirty tiny ones;
      * the pointer words themselves are re-read inside `settle`, and the sample is thrown away if
        one of them moved. A hand-written script reads the pointer, then reads the object, and
        happily records a stale object when the game swapped it between the two — `settle` on the
        data alone cannot see that.
    """

    def __init__(self, plan: Plan, ram: int):
        self.plan, self.ram = plan, ram
        self.levels: list[list[tuple]] = []
        depth = max((len(n[NODE_STEPS]) for n in plan.nodes), default=0)
        for d in range(depth + 1):
            self.levels.append([n for n in plan.nodes if len(n[NODE_STEPS]) == d])
        self.nulls = 0

    def resolve(self, pine) -> tuple[dict[tuple, int | None], list[tuple[int, int]]]:
        addr: dict[tuple, int | None] = {}
        checks: list[tuple[int, int]] = []        # (pointer address, value we resolved with)
        for n in self.levels[0] if self.levels else []:
            base = n[NODE_BASE]
            addr[n] = base if base is not None and ee_ok(base, 4, self.ram) else None
        for d in range(1, len(self.levels)):
            nodes = self.levels[d]
            parents = [addr[node_parent(n)] for n in nodes]
            reads = [p for p in parents if p is not None]
            vals: dict[int, int] = {}
            if reads:
                uniq = sorted(set(ee_addr(p) for p in reads))
                blocks = _split(pine.read_regions([(a & ~(QUAD - 1), QUAD) for a in uniq]),
                                [QUAD] * len(uniq))
                for a, b in zip(uniq, blocks):
                    vals[a] = struct.unpack_from('<I', b, a & (QUAD - 1))[0] if len(b) >= 8 else 0
            for n, p in zip(nodes, parents):
                if p is None:
                    addr[n] = None
                    continue
                v = vals.get(ee_addr(p), 0)
                checks.append((ee_addr(p), v))
                a = ee_addr(v) + n[NODE_STEPS][-1]
                addr[n] = a if v and ee_ok(a, 4, self.ram) else None
        return addr, checks


# --- the capture loop --------------------------------------------------------

@dataclass
class Result:
    frames: int = 0
    skipped: int = 0              # frames thrown away because the read tore
    gaps: int = 0                 # times the frame counter jumped
    dropped: int = 0              # frames the emulator ran while we were not looking
    blanked: int = 0              # samples with at least one null pointer chain
    first: int | None = None
    last: int | None = None
    wall: float = 0.0
    ended: str = ''
    warnings: list[str] = dcfield(default_factory=list)
    path: str = ''

    @property
    def rate(self) -> float:
        return self.frames / self.wall if self.wall > 0 else 0.0

    @property
    def suspect(self) -> bool:
        tried = self.frames + self.skipped
        return bool(tried) and self.skipped / tried > SUSPECT_SKIP_RATIO

    def summary(self) -> str:
        out = [f'{self.frames} frames captured in {self.wall:.1f}s ({self.rate:.1f}/s)'
               + (f', frames {self.first}..{self.last}' if self.first is not None else '')]
        out.append(f'  skipped {self.skipped} torn read(s), '
                   f'{self.gaps} counter gap(s) ({self.dropped} frames the emulator ran past us)'
                   + (f', {self.blanked} sample(s) with a null pointer' if self.blanked else ''))
        out.append(f'  ended: {self.ended}')
        if self.suspect:
            pct = 100 * self.skipped / max(1, self.frames + self.skipped)
            out.append(f'  SUSPECT: {pct:.0f}% of frames tore. The emulator is running faster than '
                       f'this recorder can poll it.')
            out.append('  Slow it down ([emu] speed = 0.25, or PCSX2 → Settings → Emulation → '
                       'Speed 25%) and record again; fewer fields per frame also helps.')
        for w in self.warnings[:5]:
            out.append(f'  ! {w}')
        return '\n'.join(out)


class _Out:
    """CSV (default) or a packed binary with a .json sidecar. Both flush as they go: a killed run has
    to leave a usable prefix, with provenance saying how far it got."""

    def __init__(self, watch: Watch, path: Path, binary: bool, append: bool):
        self.watch, self.path, self.binary = watch, path, binary
        self.cols = watch.columns
        self.shown = [f for f in watch.fields if not f.hidden]
        path.parent.mkdir(parents=True, exist_ok=True)
        exists = path.exists() and path.stat().st_size > 0
        if append and exists:
            self._check_append(path, binary)
        self.f = open(path, ('ab' if append and exists else 'wb') if binary
                      else ('a' if append and exists else 'w'),
                      **({} if binary else {'newline': '', 'encoding': 'utf-8'}))
        self.w = None
        if not binary:
            self.w = csv.writer(self.f)
            if not (append and exists):
                self.w.writerow(self.cols)
        elif len(watch.plan.groups) > 32:
            raise CaptureFailed('binary output supports at most 32 pointer groups '
                                f'({len(watch.plan.groups)} in this spec); use CSV', wrote=False)

    def _check_append(self, path: Path, binary: bool) -> None:
        """Appending to a capture with different columns silently produces a file whose rows mean
        two different things. Refuse instead; a second file costs nothing."""
        if binary:
            side = Path(str(path) + '.json')
            if not side.exists():
                raise CaptureFailed(f'--append: {side.name} is missing, so the existing records\' '
                                    f'layout is unknown', wrote=False)
            was = json.loads(side.read_text(encoding='utf-8'))
            if was.get('columns') != self.cols or was.get('record_bytes') != self.record_bytes:
                raise CaptureFailed(f'--append: {path} holds a different record layout; write a '
                                    f'new file instead', wrote=False)
            return
        with open(path, newline='', encoding='utf-8') as f:
            head = next(csv.reader(f), [])
        if head != self.cols:
            extra = set(head) ^ set(self.cols)
            raise CaptureFailed(f'--append: {path} has different columns ({len(head)} vs '
                                f'{len(self.cols)}, e.g. {sorted(extra)[:3]}); write a new file '
                                f'instead', wrote=False)

    def row(self, frame: int, vals: dict[Field, list], live: int) -> None:
        if self.w is not None:
            cells = [str(frame)]
            for f in self.shown:
                cells += f.render(vals.get(f, []))
            self.w.writerow(cells)
        else:
            rec = struct.pack('<II', frame, live)
            for f in self.shown:
                rec += f.pack(vals.get(f, []))
            self.f.write(rec)
        self.f.flush()

    @property
    def record_bytes(self) -> int:
        return 8 + sum(f.kind.size for f in self.shown)

    def layout(self) -> list[dict]:
        out, at = [], 8
        for f in self.shown:
            out.append({'name': f.name, 'type': f.kind.name, 'offset': at, 'size': f.kind.size,
                        'columns': f.columns, 'addr': str(f.chain)})
            at += f.kind.size
        return out

    def close(self) -> None:
        self.f.close()


def provenance(watch: Watch, res: Result, out: _Out | None, *, pine=None, state=None, pad='',
               fmt='csv') -> dict:
    c = cfg(required=False)
    p = {
        'kit': f'ps2-decomp-kit {getattr(importlib.import_module("ps2kit"), "__version__", "?")}',
        'created': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'spec': {'path': watch.path, 'name': watch.name, 'sha256_16': watch.sha,
                 'text': watch.text},
        'game': {'name': c.name, 'serial': c.serial, 'region': c.region, 'version': c.version},
        'emulator': {'pine_slot': c.pine_slot, 'vsync_addr': (f'{c.vsync:#x}' if c.vsync else None),
                     'speed': c.speed},
        'setup': {'state': state, 'pad': pad or None, 'warmup': watch.warmup},
        'format': fmt,
        'columns': watch.columns,
        'capture': {
            'frames': res.frames, 'skipped': res.skipped, 'gaps': res.gaps,
            'dropped_frames': res.dropped, 'null_samples': res.blanked,
            'frame_first': res.first, 'frame_last': res.last, 'every': watch.every,
            'wall_seconds': round(res.wall, 3), 'rate_hz': round(res.rate, 2),
            'ended': res.ended, 'suspect': res.suspect, 'warnings': res.warnings,
        },
    }
    if pine is not None:
        try:
            p['emulator']['game_id'] = pine.game_id()
            p['emulator']['status'] = pine.status()
        except Exception as e:                       # provenance must never be the thing that fails
            p['emulator']['game_id'] = f'(unavailable: {e.__class__.__name__})'
    if out is not None and out.binary:
        p['record_bytes'] = out.record_bytes
        p['layout'] = out.layout()
        p['note'] = ('packed little-endian records: u32 frame, u32 live mask (bit i = group i of '
                     '"groups" resolved), then the fields at the offsets in "layout"')
        p['groups'] = [g.chain_text for g in watch.plan.groups]
    return p


def _write_sidecar(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + '.json')
    tmp.write_text(json.dumps(data, indent=1, sort_keys=False, default=str), encoding='utf-8')


def _pad_script(path: str) -> list[str]:
    p = Path(path)
    if not p.exists():
        p2 = ROOT / path
        if not p2.exists():
            raise CaptureFailed(f'pad script not found: {path}', wrote=False)
        p = p2
    cmds = [l.split('#', 1)[0].strip() for l in p.read_text(encoding='utf-8').splitlines()]
    return [c for c in cmds if c]


def _send_pad(cmds: list[str], log) -> None:
    """Play a pad script *while* the capture runs (its timing is wall-clock, so it cannot be driven
    from inside the sampling loop without stalling it)."""
    import threading
    try:
        pad = importlib.import_module('ps2kit.pad')
    except ImportError as e:
        raise CaptureFailed(f'--pad needs ps2kit/pad.py: {e}', wrote=False)
    def run():
        try:
            pad.send(cmds, quiet=True)
        except Exception as e:                       # a dead pad must not kill a good capture
            log(f'! pad script failed: {e}')
    threading.Thread(target=run, daemon=True).start()


def record(watch: Watch, out_path: str | Path, *, frames: int | None = None,
           seconds: float | None = None, state: int | None = None, pad: str = '',
           binary: bool | None = None, append: bool = False, pine=None, log=None) -> Result:
    """Run the capture. `pine` lets a test drive the loop with a fake; everything else is the spec."""
    log = log or (lambda s: print(s, flush=True))
    want = frames if frames is not None else watch.frames
    limit_s = seconds if seconds is not None else watch.seconds
    state = watch.state if state is None else state
    pad = pad or watch.pad
    binary = (watch.format == 'binary') if binary is None else binary
    if not want and not limit_s and not watch.stop:
        raise CaptureFailed('no end in sight: give --frames, --seconds, or a [watch] stop condition',
                            wrote=False)
    c = cfg()                                       # the real config: a capture needs kit.toml
    vsync = c.need_vsync()
    if watch.unresolved:
        raise CaptureFailed('unresolved symbols: ' + ', '.join(sorted(set(watch.unresolved))),
                            wrote=False)

    own_pine = pine is None
    if own_pine:
        mod = importlib.import_module('ps2kit.pine')
        pine = mod.Pine(check=True)
    res = Result(path=str(out_path))
    out = _Out(watch, Path(out_path), binary, append)
    _write_sidecar(Path(out_path), provenance(watch, res, out, pine=pine, state=state, pad=pad,
                                              fmt='binary' if binary else 'csv'))
    resolver = Resolver(watch.plan, watch.ram)
    shown = [f for f in watch.fields if not f.hidden]
    gidx = {g.node: i for i, g in enumerate(watch.plan.groups)}

    if state is not None:
        log(f'loading save state {state}')
        pine.load_state(state)
        time.sleep(watch.settle_ms / 1000.0)
    if pad:
        cmds = _pad_script(pad)
        log(f'pad script {pad}: {len(cmds)} command(s)')
        _send_pad(cmds, log)

    t0 = time.monotonic()
    last = pine.read32(vsync)
    armed = watch.start is None
    skip_logs = 0
    due = None
    warned_null: set[str] = set()
    try:
        for _ in range(watch.warmup):
            last = pine.next_frame(last)
        while True:
            if want and res.frames >= want:
                res.ended = f'frame budget reached ({want})'
                break
            if limit_s and time.monotonic() - t0 >= limit_s:
                res.ended = f'time budget reached ({limit_s:g}s)'
                break
            v = pine.next_frame(last)
            if v > last + 1:
                res.gaps += 1
                res.dropped += v - last - 1
            last = v
            if due is None:
                due = v
            if v < due:
                continue
            due = v + watch.every

            addrs, checks = resolver.resolve(pine)
            spans: list[tuple[int, int]] = []
            consumers: list[tuple[Field, int]] = []
            live = 0
            dead: list[Group] = []
            for g in watch.plan.groups:
                a = addrs.get(g.node)
                if a is None:
                    dead.append(g)
                    continue
                base = ee_addr(a)
                gs = _align([(base + lo, base + hi) for lo, hi in g.spans])
                if any(not ee_ok(lo, hi - lo, watch.ram) for lo, hi in gs):
                    # The pointer resolved to something, but the object does not fit in RAM: a
                    # garbage pointer that happens to be non-null. Blank it like a null one.
                    dead.append(g)
                    continue
                live |= 1 << gidx[g.node]
                spans += gs
                consumers += [(f, base + off) for f, off in g.fields]
            check_spans = [(a & ~(QUAD - 1), (a & ~(QUAD - 1)) + QUAD) for a, _ in checks]
            regions = [(lo, hi - lo) for lo, hi in _coalesce(_align(spans + check_spans))]
            if not regions:
                res.ended = 'every pointer chain in the spec is null (the object is gone)'
                break
            blob = pine.settle(regions, v)
            if blob is None:
                res.skipped += 1
                skip_logs += 1
                if skip_logs <= MAX_SKIP_LOGS:
                    log(f'skipped frame {v}: it ticked mid-read'
                        + (' (further skips counted, not logged)' if skip_logs == MAX_SKIP_LOGS
                           else ''))
                continue
            blocks = _split(blob, [n for _, n in regions])
            buf = b''.join(blocks)
            starts = [a for a, _ in regions]
            cum, acc = [], 0
            for _, n in regions:
                cum.append(acc)
                acc += n
            if len(buf) < acc:
                raise CaptureFailed(f'PINE returned {len(buf)} bytes for a {acc}-byte batch at '
                                    f'frame {v}')

            def at(a: int) -> int:
                i = bisect_right(starts, a) - 1
                return cum[i] + (a - starts[i])

            moved = next((p for p, val in checks
                          if struct.unpack_from('<I', buf, at(p & ~(QUAD - 1)) + (p & (QUAD - 1)))[0]
                          != val), None)
            if moved is not None:
                # The object was swapped under us between resolving the chain and reading it:
                # `settle` on the data alone would not have noticed. Throw the sample away.
                res.skipped += 1
                skip_logs += 1
                if skip_logs <= MAX_SKIP_LOGS:
                    log(f'skipped frame {v}: pointer at {moved:#x} changed mid-read')
                continue

            vals: dict[Field, list] = {}
            for f, a in consumers:
                vals[f] = f.decode(buf, at(a))
            if dead:
                res.blanked += 1
                stop_req = next((f for g in dead for f, _ in g.fields if f.required), None)
                if stop_req is not None:
                    res.ended = (f'required field {stop_req.name!r} went null '
                                 f'({stop_req.chain}) at frame {v}')
                    break
                for g in dead:
                    if g.chain_text not in warned_null:
                        warned_null.add(g.chain_text)
                        msg = f'null pointer chain {g.chain_text} — those cells are empty'
                        res.warnings.append(msg)
                        log(f'! {msg}')

            row = {c: x for f, vs in vals.items() for c, x in zip(f.columns, vs)}
            if not armed:
                if watch.start.eval(row):
                    armed = True
                    log(f'armed at frame {v}: start ({watch.start.text}) is true')
                else:
                    continue
            if watch.stop is not None and watch.stop.eval(row):
                res.ended = f'stop ({watch.stop.text}) became true at frame {v}'
                break
            if watch.when is not None and watch.when.eval(row) is not True:
                continue

            out.row(v, vals, live)
            res.frames += 1
            res.last = v
            if res.first is None:
                res.first = v
            if res.frames % PROGRESS_EVERY == 0:
                log(f'{res.frames} frames (at {v})')
            if res.frames % SIDECAR_EVERY == 0:
                res.wall = time.monotonic() - t0
                _write_sidecar(Path(out_path), provenance(watch, res, out, pine=pine, state=state,
                                                          pad=pad, fmt='binary' if binary else 'csv'))
    except KeyboardInterrupt:
        res.ended = 'interrupted'
        res.warnings.append('interrupted by the user; the file holds every frame up to that point')
    except SystemExit as e:
        # `Pine.next_frame` bails out when the counter stops moving (the game paused, the match
        # ended). That is an end, not a failure: keep what we have and say why it stopped.
        res.ended = f'emulator stopped ticking: {e}'
        res.warnings.append(str(e))
    finally:
        res.wall = time.monotonic() - t0
        if not res.ended:
            res.ended = 'loop exited'
        out.close()
        _write_sidecar(Path(out_path), provenance(watch, res, out, pine=pine, state=state, pad=pad,
                                                  fmt='binary' if binary else 'csv'))
    if res.frames == 0:
        raise CaptureFailed(f'no frames captured ({res.ended}; {res.skipped} torn reads)')
    return res


# --- diff --------------------------------------------------------------------

def _read_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    if not path.exists():
        raise CaptureFailed(f'no such capture: {path}', wrote=False)
    with open(path, newline='', encoding='utf-8') as f:
        rows = list(csv.reader(f))
    if not rows:
        raise CaptureFailed(f'{path} is empty', wrote=False)
    return rows[0], rows[1:]


def _close(a: str, b: str, tol: float) -> bool:
    if a == b:
        return True
    try:
        x, y = float(a), float(b)
    except ValueError:
        return False
    if x != x and y != y:                 # both NaN: the same bit pattern as far as a port is concerned
        return True
    if tol <= 0:
        return x == y
    return abs(x - y) <= tol * max(1.0, abs(x), abs(y))


def diff(a_path: str | Path, b_path: str | Path, tol: float = 0.0, log=print) -> int:
    """Compare two captures column by column. The proof step: a PINE recording against the port's
    own CSV. Returns 0 when they agree, 1 when they do not."""
    ha, ra = _read_csv(Path(a_path))
    hb, rb = _read_csv(Path(b_path))
    only_a, only_b = [c for c in ha if c not in hb], [c for c in hb if c not in ha]
    shared = [c for c in ha if c in hb]
    if only_a or only_b:
        log(f'columns only in A: {", ".join(only_a[:10]) or "-"}')
        log(f'columns only in B: {", ".join(only_b[:10]) or "-"}')
    if not shared:
        raise CaptureFailed('the two captures share no columns at all', wrote=False)
    ia, ib = {c: ha.index(c) for c in shared}, {c: hb.index(c) for c in shared}
    key = 'frame' if 'frame' in shared else ''
    if key:
        da = {r[ia[key]]: r for r in ra if len(r) > ia[key]}
        db = {r[ib[key]]: r for r in rb if len(r) > ib[key]}
        keys = [k for k in da if k in db]
        missing = len(da) - len(keys), len(db) - len(keys)
        pairs = [(k, da[k], db[k]) for k in keys]
    else:
        n = min(len(ra), len(rb))
        missing = (len(ra) - n, len(rb) - n)
        pairs = [(str(i), ra[i], rb[i]) for i in range(n)]
    if missing != (0, 0):
        log(f'rows only in A: {missing[0]}   rows only in B: {missing[1]}')
    cols = [c for c in shared if c != key]
    bad: dict[str, int] = {}
    first = None
    for k, x, y in pairs:
        for c in cols:
            va = x[ia[c]] if len(x) > ia[c] else ''
            vb = y[ib[c]] if len(y) > ib[c] else ''
            if not _close(va, vb, tol):
                bad[c] = bad.get(c, 0) + 1
                if first is None:
                    first = (k, c, va, vb)
    log(f'{len(pairs)} rows x {len(cols)} columns compared'
        + (f' (tolerance {tol:g} relative)' if tol else ' (exact)'))
    if first is None and missing == (0, 0) and not only_a and not only_b:
        log(f'IDENTICAL: {Path(a_path).name} and {Path(b_path).name} agree on every cell')
        return 0
    if first is None:
        log('the shared columns agree on every shared row, but the captures differ in shape (above)')
        return 1
    k, c, va, vb = first
    log(f'DIFFER: first divergence at {key or "row"} {k}, column {c!r}')
    log(f'    A  {va}')
    log(f'    B  {vb}')
    worst = sorted(bad.items(), key=lambda kv: -kv[1])[:20]
    log(f'{len(bad)} of {len(cols)} columns differ; worst: '
        + ', '.join(f'{c} ({n})' for c, n in worst))
    return 1


# --- the command ------------------------------------------------------------

DOC = """Capture what the game does, frame by frame, from a declarative watch spec.

  kit record watch/ball.toml --state 5 --frames 600
  kit record watch/ball.toml --seconds 30 --pad scripts/serve.pad --binary
  kit record --dry-run watch/ball.toml          # check a spec with no emulator running
  kit record --diff context/capture/a.csv target/b.csv

A spec says what to sample, where from (addresses, @symbols or pointer chains), when to sample, and
what to do first (load a state, play a pad script). The recorder coalesces the fields into as few
contiguous blocks as it can and fetches them in one batched PINE read per frame; a frame whose read
tore is skipped, counted and logged, never interpolated. Every capture gets a .json sidecar with its
provenance (game, spec, save state, pad script, frame range) — a capture nobody can reconstruct is
not evidence. Format: watch/README.md."""


def cmd_record(argv: list[str]) -> int:
    from ps2kit.cli import parser
    ap = parser('record', DOC)
    ap.add_argument('spec', nargs='?', help='a watch spec (watch/*.toml)')
    ap.add_argument('--out', '-o', help='output file (default: context/capture/<name>-<date>.csv)')
    ap.add_argument('--frames', '-n', type=int, help='stop after N samples')
    ap.add_argument('--seconds', '-s', type=float, help='stop after S seconds of wall clock')
    ap.add_argument('--state', type=int, help='load this PCSX2 save-state slot first')
    ap.add_argument('--pad', help='pad script to play from the start (one kit pad command a line)')
    ap.add_argument('--binary', action='store_true', help='packed records + .json layout, for big captures')
    ap.add_argument('--append', action='store_true', help='add to an existing capture (columns must match)')
    ap.add_argument('--dry-run', action='store_true', help='print the resolved plan and exit')
    ap.add_argument('--diff', nargs=2, metavar=('A.csv', 'B.csv'), help='compare two captures')
    ap.add_argument('--tol', type=float, default=0.0, help='--diff: relative tolerance (default exact)')
    a = ap.parse_args(argv)

    try:
        if a.diff:
            return diff(a.diff[0], a.diff[1], a.tol)
        if not a.spec:
            ap.error('give a watch spec, or --diff A.csv B.csv')
        w = Watch(a.spec, allow_unresolved=a.dry_run)
        if a.dry_run:
            print(w.describe())
            dyn = [g for g in w.plan.groups if g.dynamic]
            levels = max((len(g.steps) for g in w.plan.groups), default=0)
            nregions = sum(len(g.spans) for g in w.plan.groups)
            print()
            if dyn:
                print(f'  per frame: {levels} small batched read(s) to re-resolve '
                      f'{len(dyn)} pointer chain(s), then one batched read of '
                      f'{w.plan.frame_bytes} bytes in up to {nregions} region(s)')
                print('  (regions that land next to each other at runtime merge further; '
                      'the pointer words ride along so a moved object is caught)')
            else:
                print(f'  per frame: one batched read of {w.plan.frame_bytes} bytes in '
                      f'{nregions} region(s) at fixed addresses')
            # Two reads per sample (settle) plus the pointer levels; PINE moves roughly 1 MB/s per
            # message round trip on a unix socket, and 60 fps leaves 16.6 ms a frame.
            per = w.plan.frame_bytes * 2
            print(f'  ~{per} bytes a frame both reads together; at 60 fps that is '
                  f'{per * 60 / 1024:.0f} KiB/s')
            if per > 120000:
                print('  that is a lot: run the emulator at 25% ([emu] speed) or split the spec')
            if w.unresolved:
                print('  (dry run only: those symbols must resolve before a real capture)')
            return 0
        out = a.out
        if not out:
            stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
            ext = 'bin' if (a.binary or w.format == 'binary') else 'csv'
            out = cfg().ctx('capture', f'{w.name}-{stamp}.{ext}')
        lock = plat.Lock('pcsx2', 'another tool is using PCSX2')
        held = os.environ.get('KIT_PCSX2_LOCKED') == '1'
        if not held:
            lock.take()
        try:
            os.environ['KIT_PCSX2_LOCKED'] = '1'
            res = record(w, out, frames=a.frames, seconds=a.seconds, state=a.state,
                         pad=a.pad or '', binary=True if a.binary else None, append=a.append)
        finally:
            if not held:
                os.environ.pop('KIT_PCSX2_LOCKED', None)
                lock.release()
        print(res.summary())
        print(f'  {res.path}')
        print(f'  {res.path}.json  (provenance: game, spec, state, pad, frame range)')
        return 1 if res.suspect else 0
    except SpecError as e:
        print(f'CAPTURE FAILED: {e} — nothing was written', file=sys.stderr)
        return 2
    except CaptureFailed as e:
        e.report()
        return 1
    except SystemExit as e:
        # SystemExit from config/pine carries a useful message; re-dress it in the kit's convention.
        if e.code in (0, None):
            raise
        print(f'CAPTURE FAILED: {e} — anything this run wrote is incomplete', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(cmd_record(sys.argv[1:]))
