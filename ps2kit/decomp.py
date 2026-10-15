"""Read the decompilation dumps — one function at a time, never the whole file.

Why this exists, and why it is a module instead of three shell one-liners. A full Ghidra decompilation of a
PS2 game is 10-30 MB of C across several thousand functions, and the xref table is another few million
lines. The reader is usually an AI agent whose entire conversation is re-read on every turn, so one `cat`
of the dump does not merely waste tokens, it ends the session — and the same is true for a human in a
terminal with a scrollback. Every function here therefore *streams*: it reads the dump line by line, keeps
only what was asked for, and stops. Nothing in this module calls `read()` on a dump, and nothing returns
an unbounded amount of text; every command takes `--limit` and writes the overflow to a file instead of
printing it.

What you get:

* `func(addr)` — one function's decompiled C, found by its `// 00ADDRESS name size=N` header (written by
  `ghidra_scripts/DumpDecomp.java`; that header format is the contract between the two).
* `xrefs(addr)` — who calls it, what it calls, what reads and writes it, from the xref TSV in one pass.
* `similar(addr)` — the functions whose decompiled code is *shaped* like this one, by tf-idf over token
  trigrams. This is the strongest technique in the whole kit and underused: a game has families of
  functions (every per-character update, every table lookup, every one of thirty shot types). Port one by
  hand, then ask for its siblings and port them by analogy instead of finding them by luck. Siblings that
  are already done are marked `ported`, with the journal entry that did it, so there is Rust to copy.
* `disasm(start, end)` — the instructions, read out of the program ELF by virtual address. The R5900's own
  opcodes (`madd.s`, the MMI/multimedia set, VU0 macro mode) are decoded here, because every generic MIPS
  disassembler either refuses them or — worse — decodes them as something else, and a wrong `madd.s` is a
  whole afternoon.
* `data(addr, len)` — a range of the program image as u8/u16/u32/s32/f32. Half of porting a game is
  reading its tuning tables, and they are all just bytes at an address in the overlay.
"""
from __future__ import annotations

import os
import re
import shutil
import struct
import subprocess
import sys
from array import array
from collections import Counter
from dataclasses import dataclass
from math import log, sqrt
from pathlib import Path

from ps2kit.config import cfg, num, ROOT

HEADER = re.compile(r'^// 0*([0-9a-fA-F]{4,8})(?:\s+(\S+))?(?:\s+size=(\d+))?')


class DumpError(Exception):
    pass


# --- which program, which file ------------------------------------------------

def default_program() -> str:
    """The last overlay, else `boot` — the gameplay code, nearly always what you meant."""
    return cfg().programs[-1]


def dump(program: str | None, ext: str = '.c') -> Path:
    c = cfg()
    program = program or default_program()
    if program not in c.programs:
        raise DumpError(f'no program named {program!r} in kit.toml (have: {", ".join(c.programs)})')
    p = c.ctx('decomp', f'{program}{ext}')
    if not p.exists():
        what = {'.c': 'decompilation', '.funcs.tsv': 'function table', '.xrefs.tsv': 'cross-references'}[ext]
        raise DumpError(f'no {what} for {program!r} yet ({p}).\n'
                        f'    kit ghidra dump --program {program}\n'
                        '(and if the project has never been analysed: kit ghidra analyse first — that is '
                        'the slow one)')
    return p


# --- the function table -------------------------------------------------------

@dataclass(frozen=True)
class Fn:
    addr: int
    name: str
    size: int
    sig: str

    def __str__(self) -> str:
        return f'{self.addr:08x}  {self.name:<30} {self.size:6}  {self.sig}'


def funcs(program: str | None = None) -> dict[int, Fn]:
    """address → Fn, from `<program>.funcs.tsv`. A table of a few thousand rows, not a dump."""
    path = dump(program, '.funcs.tsv')
    # Keyed on mtime as well as name: a long-lived process (the unattended runner) may well re-dump a
    # program under itself, and a stale function table silently answers with the wrong names.
    key = ('funcs', str(path), path.stat().st_mtime_ns)
    if key in _CACHE:
        return _CACHE[key]
    out: dict[int, Fn] = {}
    with open(path, encoding='utf-8', errors='replace') as fh:
        for row in fh:
            parts = row.rstrip('\n').split('\t')
            if len(parts) < 3:
                continue
            try:
                addr = int(parts[0], 16)
            except ValueError:
                continue
            out[addr] = Fn(addr, parts[1], int(parts[2] or 0), parts[3] if len(parts) > 3 else '')
    _CACHE[key] = out
    return out


_CACHE: dict = {}


def resolve(token: str, program: str | None = None) -> int:
    """`1004c0`, `0x1004c0`, `@name` (a kit.toml symbol) or a function name from the dump → an address."""
    s = str(token).strip()
    if s.startswith('@'):
        return cfg().sym(s)
    try:
        return int(s, 16) if not s.lower().startswith('0x') else int(s, 0)
    except ValueError:
        pass
    try:
        table = funcs(program)
    except DumpError:
        raise DumpError(f'{token!r} is neither an address nor a kit.toml symbol, and there is no function '
                        'table to look a name up in yet (kit ghidra dump)')
    by_name = {f.name: f.addr for f in table.values()}
    if s in by_name:
        return by_name[s]
    hits = [n for n in by_name if n.lower() == s.lower()] or \
           [n for n in by_name if s.lower() in n.lower()]
    if len(hits) == 1:
        return by_name[hits[0]]
    if hits:
        raise DumpError(f'{token!r} matches {len(hits)} functions: ' + ', '.join(sorted(hits)[:8])
                        + ('…' if len(hits) > 8 else ''))
    raise DumpError(f'no function called {token!r} in {program or default_program()} '
                    '(names come from the Ghidra project; retype one there and kit ghidra dump again)')


# --- one function's text ------------------------------------------------------

def bodies(addrs, program: str | None = None) -> dict[int, str]:
    """The decompiled text of several functions, in one streaming pass over the dump.

    One pass whatever the number of addresses: the point is never to hold the file, and never to read it
    more times than necessary when printing a function plus its callers' call sites.
    """
    want = {int(a) for a in addrs}
    out: dict[int, list[str]] = {}
    cur: list[str] | None = None
    with open(dump(program, '.c'), encoding='utf-8', errors='replace') as fh:
        for line in fh:
            if line.startswith('// '):
                m = HEADER.match(line)
                if m:
                    addr = int(m.group(1), 16)
                    if cur is not None and not want - out.keys():
                        break                      # everything asked for is complete: stop reading
                    cur = out.setdefault(addr, [line]) if addr in want else None
                    continue
            if cur is not None:
                cur.append(line)
    return {a: ''.join(v) for a, v in out.items()}


def func(addr: int | str, program: str | None = None) -> str:
    """One function's decompiled C, header line included. Accepts a name or an address."""
    a = resolve(addr, program) if isinstance(addr, str) else int(addr)
    got = bodies([a], program)
    if a not in got:
        table = funcs(program)
        near = max((f for f in table.values() if f.addr <= a < f.addr + max(f.size, 1)), default=None,
                   key=lambda f: f.addr)
        hint = (f'\n{a:#x} is inside {near.name} at {near.addr:#x} — try that.' if near else '')
        raise DumpError(f'no function starts at {a:#x} in {program or default_program()}.{hint}')
    return got[a]


# --- cross-references ---------------------------------------------------------

CALL_TYPES = ('CALL', 'CALL_TERMINATOR', 'THUNK')
WRITE_TYPES = ('WRITE',)


@dataclass
class Ref:
    to: int
    frm: int
    func: str
    type: str

    @property
    def kind(self) -> str:
        t = self.type.upper()
        if 'CALL' in t:
            return 'call'
        if 'WRITE' in t:
            return 'write'
        if 'READ' in t or 'DATA' in t:
            return 'read'
        return 'other'


def xrefs(addr: int | str, program: str | None = None) -> dict[str, list[Ref]]:
    """Everything the xref table says about `addr`, in one pass.

    Returns `{'in': [refs that point at addr], 'out': [refs made from inside this function]}`, each row
    classified as a call, a read or a write. Both directions come out of one scan because the table is
    sorted by target and we want a function's outgoing references too — those are the rows whose *source*
    falls inside its body, which is why the function's size matters here.
    """
    a = resolve(addr, program) if isinstance(addr, str) else int(addr)
    table = funcs(program)
    size = table[a].size if a in table else 4
    end = a + max(size, 4)
    got = {'in': [], 'out': []}
    with open(dump(program, '.xrefs.tsv'), encoding='utf-8', errors='replace') as fh:
        for row in fh:
            parts = row.rstrip('\n').split('\t')
            if len(parts) < 4:
                continue
            try:
                to, frm = int(parts[0], 16), int(parts[1], 16)
            except ValueError:
                continue
            if to == a:
                got['in'].append(Ref(to, frm, parts[2], parts[3]))
            elif a <= frm < end:
                got['out'].append(Ref(to, frm, parts[2], parts[3]))
    return got


# --- look-alike functions -----------------------------------------------------

WORD = re.compile(r'[A-Za-z_]\w*|\d\w*|\S')
# Tokens kept verbatim; every other identifier becomes ID and every literal N, so two functions with the
# same control flow and the same shape of maths match even with different names and constants.
KEEP = {'if', 'else', 'while', 'do', 'for', 'return', 'switch', 'case', 'break', 'continue', 'goto',
        'float', 'double', 'int', 'uint', 'long', 'short', 'char', 'bool', 'void', 'unsigned',
        'undefined', 'undefined1', 'undefined2', 'undefined4', 'undefined8',
        'sqrtf', 'sinf', 'cosf', 'tanf', 'atan2f', 'acosf', 'asinf', 'fabsf', 'memcpy', 'memset'}
SHAPE_CAP = 160   # trigrams kept per function: without a cap, a few thousand Counters is a gigabyte


def _shape(body: str) -> dict[int, int]:
    """A function's token-trigram profile, hashed to ints and pruned to the most frequent `SHAPE_CAP`."""
    text = body.split('{', 1)[-1]
    toks = [w if w in KEEP or not (w[0].isalpha() or w[0] == '_' or w[0].isdigit())
            else ('N' if w[0].isdigit() else 'ID')
            for w in WORD.findall(text)]
    c = Counter(hash(t) for t in zip(toks, toks[1:], toks[2:]))
    if len(c) > SHAPE_CAP:
        c = Counter(dict(c.most_common(SHAPE_CAP)))
    return c


def _shapes(program: str | None):
    """Stream the dump once, yielding (addr, shape) per function. Never holds more than one body."""
    cur_addr, cur = None, []
    with open(dump(program, '.c'), encoding='utf-8', errors='replace') as fh:
        for line in fh:
            if line.startswith('// '):
                m = HEADER.match(line)
                if m:
                    if cur_addr is not None:
                        yield cur_addr, _shape(''.join(cur))
                    cur_addr, cur = int(m.group(1), 16), []
                    continue
            if cur_addr is not None and len(cur) < 4000:        # a runaway function teaches us nothing
                cur.append(line)
    if cur_addr is not None:
        yield cur_addr, _shape(''.join(cur))


def ported(pattern: str = '*FINAL*.md') -> dict[int, str]:
    """address → the journal entry that says it is done, so a sibling's Rust can be copied.

    A finished journal entry names the addresses it ported; that is the only index of "already solved"
    the kit needs, and it costs nothing to keep accurate because it is a side effect of writing the notes.
    """
    out: dict[int, str] = {}
    root = cfg().journal
    if not root.is_dir():
        return out
    for f in sorted(root.rglob(pattern)):
        try:
            text = f.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        for a in re.findall(r'\b0x([0-9a-fA-F]{5,8})\b', text):
            out.setdefault(int(a, 16), str(f.relative_to(ROOT) if f.is_relative_to(ROOT) else f))
    return out


def similar(addr: int | str, program: str | None = None, n: int = 10) -> list[tuple[float, Fn, str]]:
    """The `n` functions whose decompiled code is shaped most like this one, best first.

    tf-idf cosine over token trigrams (the same idea as code-clone tools like ponytail/Coddog). Returns
    `(score, Fn, journal entry or '')`. A score above ~0.6 usually means "the same function for a different
    case"; 0.3-0.6 means "same family"; below that, coincidence.
    """
    a = resolve(addr, program) if isinstance(addr, str) else int(addr)
    keys: dict[int, array] = {}
    vals: dict[int, array] = {}
    df: Counter = Counter()
    for fa, shape in _shapes(program):
        ks = sorted(shape)
        keys[fa] = array('q', ks)
        vals[fa] = array('d', [shape[k] for k in ks])
        df.update(ks)
    if a not in keys:
        raise DumpError(f'no function starts at {a:#x} in {program or default_program()}')
    total = len(keys)
    idf = {k: log(total / v) for k, v in df.items()}

    def weight(fa):
        w = array('d', (vals[fa][i] * idf[keys[fa][i]] for i in range(len(keys[fa]))))
        norm = sqrt(sum(x * x for x in w)) or 1.0
        return w, norm

    mw, mn = weight(a)
    mine = dict(zip(keys[a], mw))
    scored = []
    for fa in keys:
        if fa == a:
            continue
        w, nrm = weight(fa)
        dot = sum(w[i] * mine[k] for i, k in enumerate(keys[fa]) if k in mine)
        if dot:
            scored.append((dot / (mn * nrm), fa))
    scored.sort(reverse=True)
    table, done = funcs(program), ported()
    return [(s, table.get(fa, Fn(fa, '?', 0, '')), done.get(fa, '')) for s, fa in scored[:n]]


# --- disassembly --------------------------------------------------------------

REG = ('$zero', '$at', '$v0', '$v1', '$a0', '$a1', '$a2', '$a3', '$t0', '$t1', '$t2', '$t3', '$t4', '$t5',
       '$t6', '$t7', '$s0', '$s1', '$s2', '$s3', '$s4', '$s5', '$s6', '$s7', '$t8', '$t9', '$k0', '$k1',
       '$gp', '$sp', '$fp', '$ra')

# COP1 (op 0x11), fmt = S (0x10): the single-precision funcs that only the R5900 has. The accumulator ones
# take no destination register — the result goes to ACC — which is exactly what generic disassemblers get
# wrong, and `madda.s`/`madd.s` misread as `madd` (the integer one) has cost real time.
COP1_R5900 = {0x16: 'rsqrt.s', 0x18: 'adda.s', 0x19: 'suba.s', 0x1a: 'mula.s', 0x1c: 'madd.s',
              0x1d: 'msub.s', 0x1e: 'madda.s', 0x1f: 'msuba.s', 0x28: 'max.s', 0x29: 'min.s'}
COP1_ACC = {'adda.s', 'suba.s', 'mula.s', 'madda.s', 'msuba.s'}

# MMI (op 0x1c): the 128-bit multimedia set. funct in bits 5..0; four of them are sub-tables.
MMI = {0x00: 'madd', 0x01: 'maddu', 0x04: 'plzcw', 0x10: 'mfhi1', 0x11: 'mthi1', 0x12: 'mflo1',
       0x13: 'mtlo1', 0x18: 'mult1', 0x19: 'multu1', 0x1a: 'div1', 0x1b: 'divu1', 0x20: 'madd1',
       0x21: 'maddu1', 0x30: 'pmfhl', 0x31: 'pmthl', 0x34: 'psllh', 0x36: 'psrlh', 0x37: 'psrah',
       0x3c: 'psllw', 0x3e: 'psrlw', 0x3f: 'psraw'}
MMI0 = {0x00: 'paddw', 0x01: 'psubw', 0x02: 'pcgtw', 0x03: 'pmaxw', 0x04: 'paddh', 0x05: 'psubh',
        0x06: 'pcgth', 0x07: 'pmaxh', 0x08: 'paddb', 0x09: 'psubb', 0x0a: 'pcgtb', 0x10: 'paddsw',
        0x11: 'psubsw', 0x12: 'pextlw', 0x13: 'ppacw', 0x14: 'paddsh', 0x15: 'psubsh', 0x16: 'pextlh',
        0x17: 'ppach', 0x18: 'paddsb', 0x19: 'psubsb', 0x1a: 'pextlb', 0x1b: 'ppacb', 0x1e: 'pext5',
        0x1f: 'ppac5'}
MMI1 = {0x01: 'pabsw', 0x02: 'pceqw', 0x03: 'pminw', 0x04: 'padsbh', 0x05: 'pabsh', 0x06: 'pceqh',
        0x07: 'pminh', 0x0a: 'pceqb', 0x10: 'padduw', 0x11: 'psubuw', 0x12: 'pextuw', 0x14: 'padduh',
        0x15: 'psubuh', 0x16: 'pextuh', 0x18: 'paddub', 0x19: 'psubub', 0x1a: 'pextub', 0x1b: 'qfsrv'}
MMI2 = {0x00: 'pmaddw', 0x02: 'psllvw', 0x03: 'psrlvw', 0x04: 'pmsubw', 0x08: 'pmfhi', 0x09: 'pmflo',
        0x0a: 'pinth', 0x0c: 'pmultw', 0x0d: 'pdivw', 0x0e: 'pcpyld', 0x10: 'pmaddh', 0x11: 'phmadh',
        0x12: 'pand', 0x13: 'pxor', 0x14: 'pmsubh', 0x15: 'phmsbh', 0x1a: 'pexeh', 0x1b: 'prevh',
        0x1c: 'pmulth', 0x1d: 'pdivbw', 0x1e: 'pexew', 0x1f: 'prot3w'}
MMI3 = {0x00: 'pmadduw', 0x03: 'psravw', 0x08: 'pmthi', 0x09: 'pmtlo', 0x0a: 'pinteh', 0x0c: 'pmultuw',
        0x0d: 'pdivuw', 0x0e: 'pcpyud', 0x12: 'por', 0x13: 'pnor', 0x1a: 'pexch', 0x1b: 'pcpyh',
        0x1e: 'pexcw'}
PMFHL = {0: 'lw', 1: 'uw', 2: 'slw', 3: 'lh', 4: 'sh'}

# COP2 (op 0x12) with the CO bit set: VU0 in macro mode. Generic MIPS disassemblers do not know these at
# all, and VU0 macro code is where a PS2 game keeps its vector maths, so this table earns its keep.
BC = 'xyzw'
COP2 = {0x1c: 'vmulq', 0x1d: 'vmaxi', 0x1e: 'vmuli', 0x1f: 'vminii', 0x20: 'vaddq', 0x21: 'vmaddq',
        0x22: 'vaddi', 0x23: 'vmaddi', 0x24: 'vsubq', 0x25: 'vmsubq', 0x26: 'vsubi', 0x27: 'vmsubi',
        0x28: 'vadd', 0x29: 'vmadd', 0x2a: 'vmul', 0x2b: 'vmax', 0x2c: 'vsub', 0x2d: 'vmsub',
        0x2e: 'vopmsub', 0x2f: 'vmini', 0x30: 'viadd', 0x31: 'visub', 0x32: 'viaddi', 0x34: 'viand',
        0x35: 'vior', 0x38: 'vcallms', 0x39: 'vcallmsr'}
COP2_BC = {0x00: 'vadd', 0x04: 'vsub', 0x08: 'vmadd', 0x0c: 'vmsub', 0x10: 'vmax', 0x14: 'vmini',
           0x18: 'vmul'}
# "special2": funct 0x3c..0x3f, with bits 10..6 extending it to (bits10_6 << 2) | (funct & 3).
COP2_S2 = {0x1c: 'vmulaq', 0x1d: 'vabs', 0x1e: 'vmulai', 0x1f: 'vclipw', 0x20: 'vaddaq', 0x21: 'vmaddaq',
           0x22: 'vaddai', 0x23: 'vmaddai', 0x24: 'vsubaq', 0x25: 'vmsubaq', 0x26: 'vsubai',
           0x27: 'vmsubai', 0x28: 'vadda', 0x29: 'vmadda', 0x2a: 'vmula', 0x2b: 'vopmula', 0x2c: 'vsuba',
           0x2d: 'vmsuba', 0x2e: 'vnop', 0x2f: 'vmove', 0x30: 'vmr32', 0x34: 'vlqi', 0x35: 'vsqi',
           0x36: 'vlqd', 0x37: 'vsqd', 0x38: 'vdiv', 0x39: 'vsqrt', 0x3a: 'vrsqrt', 0x3b: 'vwaitq',
           0x3c: 'vmtir', 0x3d: 'vmfir', 0x3e: 'vilwr', 0x3f: 'viswr', 0x40: 'vrnext', 0x41: 'vrget',
           0x42: 'vrinit', 0x43: 'vrxor'}
COP2_S2_BC = {0x00: 'vadda', 0x04: 'vsuba', 0x08: 'vmadda', 0x0c: 'vmsuba', 0x18: 'vmula'}
ITOF = {0x10: 'vitof0', 0x11: 'vitof4', 0x12: 'vitof12', 0x13: 'vitof15',
        0x14: 'vftoi0', 0x15: 'vftoi4', 0x16: 'vftoi12', 0x17: 'vftoi15'}


def _dest(w: int) -> str:
    """The xyzw field (bits 24..21) that every VU macro instruction is masked by."""
    d = (w >> 21) & 0xf
    return '.' + ''.join(c for i, c in enumerate(BC) if d & (8 >> i)) if d else ''


def r5900(w: int) -> str | None:
    """Decode the R5900-only instructions. None means "ask a general MIPS disassembler"."""
    op, rs, rt, rd, sa, fn = w >> 26, (w >> 21) & 31, (w >> 16) & 31, (w >> 11) & 31, (w >> 6) & 31, w & 63
    imm = w & 0xffff
    simm = imm - 0x10000 if imm & 0x8000 else imm

    if op == 0x11 and rs == 0x10 and fn in COP1_R5900:                 # COP1, fmt=S
        name = COP1_R5900[fn]
        if name in COP1_ACC:
            return f'{name:<9} $f{rd}, $f{rt}'                         # ACC = f(fs, ft); no fd
        return f'{name:<9} $f{sa}, $f{rd}, $f{rt}'                     # fd, fs, ft
    if op == 0x1e:
        return f'{"lq":<9} {REG[rt]}, {simm}({REG[rs]})'
    if op == 0x1f:
        return f'{"sq":<9} {REG[rt]}, {simm}({REG[rs]})'
    if op == 0x1c:
        return _mmi(w, rs, rt, rd, sa, fn)
    if op == 0x12:
        return _cop2(w, rs, rt, rd, sa, fn)
    return None


def _mmi(w: int, rs, rt, rd, sa, fn) -> str | None:
    three = lambda m: f'{m:<9} {REG[rd]}, {REG[rs]}, {REG[rt]}'
    if fn == 0x08:
        return three(MMI0[sa]) if sa in MMI0 else None
    if fn == 0x28:
        return three(MMI1[sa]) if sa in MMI1 else None
    if fn == 0x09:
        m = MMI2.get(sa)
        if m in ('pmfhi', 'pmflo'):
            return f'{m:<9} {REG[rd]}'
        return three(m) if m else None
    if fn == 0x29:
        m = MMI3.get(sa)
        if m in ('pmthi', 'pmtlo'):
            return f'{m:<9} {REG[rs]}'
        return three(m) if m else None
    m = MMI.get(fn)
    if m is None:
        return None
    if m in ('pmfhl', 'pmthl'):                                        # pmfhl.lw / .uw / .slw / .lh / .sh
        return f'{m + "." + PMFHL.get(sa, "?"):<9} {REG[rd if m == "pmfhl" else rs]}'
    if m in ('mfhi1', 'mflo1'):
        return f'{m:<9} {REG[rd]}'
    if m in ('mthi1', 'mtlo1'):
        return f'{m:<9} {REG[rs]}'
    if m in ('mult1', 'multu1', 'madd', 'maddu', 'madd1', 'maddu1'):
        return f'{m:<9} {REG[rd]}, {REG[rs]}, {REG[rt]}'
    if m in ('div1', 'divu1'):
        return f'{m:<9} {REG[rs]}, {REG[rt]}'
    if m == 'plzcw':
        return f'{m:<9} {REG[rd]}, {REG[rs]}'
    return f'{m:<9} {REG[rd]}, {REG[rt]}, {sa}'                        # the psll*/psr* shifts


def _cop2(w: int, rs, rt, rd, sa, fn) -> str | None:
    if not (w >> 25) & 1:                                              # the move-to/from-VU0 instructions
        return {1: f'{"qmfc2":<9} {REG[rt]}, $vf{rd}', 2: f'{"cfc2":<9} {REG[rt]}, $vi{rd}',
                5: f'{"qmtc2":<9} {REG[rt]}, $vf{rd}', 6: f'{"ctc2":<9} {REG[rt]}, $vi{rd}'}.get(rs)
    d = _dest(w)
    if fn <= 0x1b and (fn & ~3) in COP2_BC:
        return f'{COP2_BC[fn & ~3] + BC[fn & 3] + d:<9} $vf{sa}, $vf{rd}, $vf{rt}{BC[fn & 3]}'
    if fn >= 0x3c:                                                     # special2
        ext = (sa << 2) | (fn & 3)
        if (ext & ~3) in COP2_S2_BC and ext <= 0x1b:
            m = COP2_S2_BC[ext & ~3] + BC[ext & 3]
            return f'{m + d:<9} $vf{rd}, $vf{rt}{BC[ext & 3]}'
        if ext in ITOF:
            return f'{ITOF[ext] + d:<9} $vf{rt}, $vf{rd}'
        m = COP2_S2.get(ext)
        if m is None:
            return None
        if m in ('vnop', 'vwaitq'):
            return m
        if m in ('vdiv', 'vsqrt', 'vrsqrt'):
            return f'{m:<9} Q, $vf{rd}{_bcsuf(w, 1)}, $vf{rt}{_bcsuf(w, 0)}'
        if m in ('vmtir', 'vmfir', 'vilwr', 'viswr', 'vrnext', 'vrget', 'vrinit', 'vrxor'):
            return f'{m + d:<9} $vi{rt}, $vf{rd}'
        if m in ('vlqi', 'vsqi', 'vlqd', 'vsqd'):
            return f'{m + d:<9} $vf{rt}, ($vi{rd})'
        if m in ('vmove', 'vmr32', 'vabs'):
            return f'{m + d:<9} $vf{rt}, $vf{rd}'
        if m in ('vclipw',):
            return f'{m:<9} $vf{rd}xyz, $vf{rt}w'
        return f'{m + d:<9} $vf{rd}, $vf{rt}'                          # the ACC forms: ACC = f(fs, ft)
    m = COP2.get(fn)
    if m is None:
        return None
    if m in ('viadd', 'visub', 'viand', 'vior'):
        return f'{m:<9} $vi{sa}, $vi{rd}, $vi{rt}'
    if m == 'viaddi':
        return f'{m:<9} $vi{rt}, $vi{rd}, {sa - 32 if sa & 16 else sa}'
    if m == 'vcallms':
        return f'{m:<9} {((w >> 6) & 0x7fff) * 8:#x}'
    if m == 'vcallmsr':
        return f'{m:<9} $vi27'
    if m.endswith(('q', 'i')) and m not in ('vmulq',) or m in ('vmulq',):
        src = 'Q' if m.endswith('q') else 'I'
        return f'{m + _dest(w):<9} $vf{sa}, $vf{rd}, {src}'
    return f'{m + _dest(w):<9} $vf{sa}, $vf{rd}, $vf{rt}'


def _bcsuf(w: int, which: int) -> str:
    """vdiv/vsqrt take a single component of each source, encoded in the ft/fs broadcast fields."""
    f = (w >> (23 if which else 21)) & 3
    return BC[f]


# --- the general-MIPS fallback ------------------------------------------------

def _llvm_mc() -> str | None:
    for name in ('llvm-mc',) + tuple(f'llvm-mc-{v}' for v in range(21, 13, -1)):
        if (p := shutil.which(name)):
            return p
    return None


def _objdump_mips() -> str | None:
    """A binutils objdump that can actually do MIPS. The distro's default one usually cannot — it is built
    for the host architecture only — so probe instead of assuming."""
    for name in ('mips-linux-gnu-objdump', 'mipsel-linux-gnu-objdump', 'mips64-linux-gnuabi64-objdump',
                 'objdump'):
        exe = shutil.which(name)
        if not exe:
            continue
        r = subprocess.run([exe, '-m', 'mips', '-b', 'binary', '-D', os.devnull],
                           capture_output=True, text=True)
        if 'machine mips' not in r.stderr and "can't disassemble" not in r.stderr:
            return exe
    return None


def _external(words: tuple[int, ...]) -> tuple[dict[int, str], str | None]:
    """Disassemble with llvm-mc (or objdump), one word per input line.

    The trap: llvm-mc *drops* an instruction it cannot decode from stdout, so a flat stream of words
    silently shifts every later line by one. Feeding one word per line makes the warning it prints carry
    the word's index (`<stdin>:7:1: warning: invalid instruction encoding`), so the dropped ones can be
    accounted for. If the arithmetic still does not add up we return nothing rather than a plausible lie.
    """
    exe = _llvm_mc()
    if exe:
        src = '\n'.join(' '.join(f'0x{b:02x}' for b in struct.pack('<I', w)) for w in words) + '\n'
        r = subprocess.run([exe, '--disassemble', '--triple=mipsel', '--mcpu=mips3'],
                           input=src, capture_output=True, text=True)
        bad = {int(m) - 1 for m in re.findall(r'^<stdin>:(\d+):', r.stderr, re.M)}
        lines = [l.strip() for l in r.stdout.splitlines()
                 if l.strip() and not l.strip().startswith('.')]
        ok = [i for i in range(len(words)) if i not in bad]
        if len(lines) == len(ok):
            return {i: re.sub(r'\s+', ' ', t).replace(' ', '\t', 1) for i, t in zip(ok, lines)}, 'llvm-mc'
        return {}, 'llvm-mc'
    exe = _objdump_mips()
    if exe:
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.bin', delete=False) as tf:
            tf.write(b''.join(struct.pack('<I', w) for w in words))
            raw = tf.name
        try:
            r = subprocess.run([exe, '-D', '-b', 'binary', '-m', 'mips', '-EL', raw],
                               capture_output=True, text=True)
            out = {}
            for line in r.stdout.splitlines():
                m = re.match(r'\s*([0-9a-f]+):\s+[0-9a-f ]+\t(.+)', line)
                if m:
                    out[int(m.group(1), 16) // 4] = re.sub(r'\s+', ' ', m.group(2)).strip()
            return out, Path(exe).name
        finally:
            os.unlink(raw)
    return {}, None


@dataclass
class Insn:
    addr: int
    word: int
    text: str
    note: str = ''

    def __str__(self) -> str:
        return f'{self.addr:08x}  {self.word:08x}  {self.text}' + (f'    ; {self.note}' if self.note else '')


BRANCH_OPS = {1, 4, 5, 6, 7, 20, 21, 22, 23}


def disasm(start: int, end: int, program: str | None = None) -> tuple[list[Insn], str | None]:
    """Instructions from `start` to `end`, read out of `context/elf/<program>.elf`.

    Returns the instructions and the name of the external disassembler used for the ordinary MIPS ones
    (None when there was none — then those come out as `.word`, which is still enough to find a table or a
    jump target by eye).
    """
    from ps2kit import elf as elf_mod
    program = program or default_program()
    path = elf_mod.elf_path(program)
    if not path.exists():
        raise DumpError(f'no ELF for {program!r} at {path}\n    kit elf --program {program}')
    start, end = start & ~3, (end + 3) & ~3
    if end <= start:
        raise DumpError(f'empty range {start:#x}..{end:#x}')
    try:
        words = elf_mod.Elf(path).words(start, (end - start) // 4)
    except elf_mod.ElfError as e:
        raise DumpError(str(e))

    mine = {i: t for i, w in enumerate(words) if (t := r5900(w)) is not None}
    rest = tuple(w if i not in mine else 0 for i, w in enumerate(words))
    ext, tool = _external(rest) if len(mine) < len(words) else ({}, None)
    table = {}
    try:
        table = funcs(program)
    except DumpError:
        pass

    out = []
    for i, w in enumerate(words):
        addr = start + i * 4
        text = mine.get(i) or ext.get(i) or f'.word     0x{w:08x}'
        out.append(Insn(addr, w, text, _target_note(w, addr, table)))
    return out, tool


def _target_note(w: int, addr: int, table: dict[int, Fn]) -> str:
    """Branch and jump targets worked out for you, named when the function table knows them."""
    op = w >> 26
    if op in (2, 3):
        t = ((addr + 4) & 0xf0000000) | ((w & 0x3ffffff) << 2)
    elif op in BRANCH_OPS:
        imm = w & 0xffff
        t = addr + 4 + ((imm - 0x10000 if imm & 0x8000 else imm) << 2)
    else:
        return ''
    fn = table.get(t)
    return f'-> {t:#x}' + (f' {fn.name}' if fn else '')


# --- raw data -----------------------------------------------------------------

FORMATS = {'u8': ('B', 1), 'u16': ('H', 2), 'u32': ('I', 4), 's32': ('i', 4), 'f32': ('f', 4),
           's16': ('h', 2), 's8': ('b', 1), 'hex': ('B', 1)}


def data(addr: int, length: int, as_: str = 'u32', program: str | None = None) -> list[str]:
    """A range of the program image, formatted. Offsets are printed from `addr`, which is how a table's
    stride announces itself: a repeating pattern every 0x10 or 0x24 bytes is the record size."""
    from ps2kit import elf as elf_mod
    program = program or default_program()
    path = elf_mod.elf_path(program)
    if not path.exists():
        raise DumpError(f'no ELF for {program!r} at {path}\n    kit elf --program {program}')
    if as_ not in FORMATS:
        raise DumpError(f'--as must be one of {", ".join(FORMATS)}')
    try:
        raw = elf_mod.Elf(path).read(addr, length)
    except elf_mod.ElfError as e:
        raise DumpError(str(e))
    code, size = FORMATS[as_]
    per = 16 // size if as_ != 'hex' else 16
    rows = []
    for off in range(0, len(raw) - (len(raw) % size if as_ != 'hex' else 0), per * size):
        chunk = raw[off:off + per * size]
        n = len(chunk) // size
        vals = struct.unpack(f'<{n}{code}', chunk[:n * size])
        if as_ == 'hex':
            txt = ' '.join(f'{b:02x}' for b in chunk) + '  |' + \
                  ''.join(chr(b) if 0x20 <= b < 0x7f else '.' for b in chunk) + '|'
        elif as_ == 'f32':
            txt = ' '.join(f'{v:>14.7g}' for v in vals)
        else:
            txt = ' '.join(f'{v:>{3 * size + 2}}' for v in vals)
        rows.append(f'+0x{off:04x}  {addr + off:08x}  {txt}')
    return rows


def strings(pat: str, program: str | None = None, min_len: int = 4) -> list[tuple[int, str]]:
    """Printable runs in the program image whose text matches `pat` (case-insensitive substring or regex).

    Debug strings, file names and format strings are the best foothold a port has: find the message,
    then `kit xref <its address>` names the code that prints it.
    """
    from ps2kit import elf as elf_mod
    program = program or default_program()
    path = elf_mod.elf_path(program)
    if not path.exists():
        raise DumpError(f'no ELF for {program!r} at {path}\n    kit elf --program {program}')
    try:
        rx = re.compile(pat, re.I)
    except re.error:
        rx = re.compile(re.escape(pat), re.I)
    out = []
    e = elf_mod.Elf(path)
    for seg in e.loads:
        if not seg.filesz:
            continue
        blob, base = e.read(seg.vaddr, seg.filesz), seg.vaddr
        for m in re.finditer(rb'[\x20-\x7e]{%d,}' % min_len, blob):
            text = m.group().decode('ascii', 'replace')
            if rx.search(text):
                out.append((base + m.start(), text))
    return out


# --- kit fn -----------------------------------------------------------------

FN_DOC = """Print one decompiled function.

  kit fn 1000a0                 by address
  kit fn update_player          by name, from the Ghidra project's own names
  kit fn @serve_entry           by a [symbols] name in kit.toml
  kit fn 1000a0 -p boot         a different program (default: the last overlay)
  kit fn 1000a0 --context 3     plus the call sites in its first 3 callers

The dump itself is tens of megabytes: this streams it and stops at the next function, so the cost of
asking is the size of the answer. Long functions are capped — the full text is written to a file and the
path printed."""


def cmd_fn(argv: list[str]) -> int:
    from ps2kit.cli import parser
    ap = parser('fn', FN_DOC)
    ap.add_argument('addr', help='address (hex / 0x…), @symbol, or a function name')
    ap.add_argument('--program', '-p', metavar='NAME', help='program to read (default: the last overlay)')
    ap.add_argument('--context', type=int, default=0, metavar='N',
                    help='also show the call sites in N of its callers')
    ap.add_argument('--limit', type=int, default=300, metavar='N', help='lines to print (default 300)')
    args = ap.parse_args(argv)
    try:
        prog = args.program or default_program()
        a = resolve(args.addr, prog)
        table = funcs(prog)
        fn = table.get(a)
        body = func(a, prog)
        print(f'# {prog}  {fn or f"{a:08x}  (not in the function table)"}')
        _emit(body.splitlines(), args.limit, cfg().ctx('notes', f'fn_{prog}_{a:08x}.c'), body)
        if args.context:
            _call_sites(a, prog, table, args.context)
    except DumpError as e:
        print(f'kit fn: {e}', file=sys.stderr)
        return 1
    return 0


def _call_sites(a: int, prog: str, table: dict[int, Fn], n: int) -> None:
    me = table.get(a)
    name = me.name if me else None
    callers = sorted({r.frm for r in xrefs(a, prog)['in'] if r.kind == 'call'})
    owners = []
    for frm in callers:
        owner = max((f for f in table.values() if f.addr <= frm < f.addr + max(f.size, 1)),
                    default=None, key=lambda f: f.addr)
        if owner and owner.addr not in [o.addr for o in owners]:
            owners.append(owner)
    if not owners:
        print(f'\n# no callers in the xref table (an indirect call through a table will not show here)')
        return
    print(f'\n# {len(owners)} caller(s); call sites in the first {min(n, len(owners))}:')
    got = bodies([o.addr for o in owners[:n]], prog)
    for o in owners[:n]:
        print(f'\n# --- {o.addr:08x} {o.name} ---')
        lines = got.get(o.addr, '').splitlines()
        for i, line in enumerate(lines):
            if name and name in line:
                for j in range(max(0, i - 2), min(len(lines), i + 3)):
                    print(f'  {j:4}  {lines[j]}')
                print('  ...')


def _emit(lines: list[str], limit: int, overflow: Path, full: str) -> None:
    for line in lines[:limit]:
        print(line)
    if len(lines) > limit:
        overflow.write_text(full, encoding='utf-8')
        print(f'… {len(lines) - limit} more lines. All of it: {_rel(overflow)}  (--limit N to print more)')


# --- kit xref ---------------------------------------------------------------

XREF_DOC = """Who touches an address, and which functions look like this one.

  kit xref 1000a0            callers, callees, and the data this function reads and writes
  kit xref 1000a0 --similar  the 10 functions shaped most like it; `ported:` ones already have Rust
  kit xref 1f4a20            for a data address: every function that reads or writes it

--similar is the one to reach for when a game has families of near-identical functions (one per character,
one per shot type, one per menu page). Port one properly, then do its siblings by analogy."""


def cmd_xref(argv: list[str]) -> int:
    from ps2kit.cli import parser
    ap = parser('xref', XREF_DOC)
    ap.add_argument('addr', help='address (hex / 0x…), @symbol, or a function name')
    ap.add_argument('--program', '-p', metavar='NAME')
    ap.add_argument('--similar', action='store_true', help='look-alike functions instead of references')
    ap.add_argument('--limit', type=int, default=40, metavar='N', help='rows per section (default 40)')
    args = ap.parse_args(argv)
    try:
        prog = args.program or default_program()
        a = resolve(args.addr, prog)
        table = funcs(prog)
        if args.similar:
            return _similar_report(a, prog, table, args.limit)
        refs = xrefs(a, prog)
        me = table.get(a)
        print(f'# {prog}  {me or f"{a:08x}"}')
        _refs_section('called by', [r for r in refs['in'] if r.kind == 'call'], table, args.limit, True)
        _refs_section('read/written by', [r for r in refs['in'] if r.kind != 'call'], table, args.limit, True)
        _refs_section('calls', [r for r in refs['out'] if r.kind == 'call'], table, args.limit, False)
        _refs_section('touches', [r for r in refs['out'] if r.kind != 'call'], table, args.limit, False)
        if not refs['in'] and not refs['out']:
            print('no references. Either nothing uses it, or it is only reached indirectly through a '
                  'function-pointer table — look for its address as data: kit xref <the table address>')
    except DumpError as e:
        print(f'kit xref: {e}', file=sys.stderr)
        return 1
    return 0


def _refs_section(title: str, refs: list[Ref], table: dict[int, Fn], limit: int, incoming: bool) -> None:
    if not refs:
        return
    seen, rows = set(), []
    for r in refs:
        key = (r.func, r.to) if not incoming else (r.func, r.frm & ~0xf)
        if key in seen:
            continue
        seen.add(key)
        if incoming:
            rows.append(f'  {r.frm:08x}  {r.func:<30} {r.type.lower()}')
        else:
            fn = table.get(r.to)
            rows.append(f'  {r.to:08x}  {(fn.name if fn else "-"):<30} {r.type.lower()}'
                        f'{"  (" + str(fn.size) + " bytes)" if fn else ""}')
    print(f'\n{title} ({len(rows)}):')
    for row in rows[:limit]:
        print(row)
    if len(rows) > limit:
        print(f'  … {len(rows) - limit} more (--limit {len(rows)})')


def _similar_report(a: int, prog: str, table: dict[int, Fn], limit: int) -> int:
    me = table.get(a)
    print(f'# shaped like {me or f"{a:08x}"} in {prog}')
    print('# score 0.6+ = the same function for another case · 0.3-0.6 = same family · below = noise')
    rows = similar(a, prog, max(limit, 10))
    for score, fn, journal in rows:
        print(f'  {score:.2f}  {fn.addr:08x}  {fn.name:<30} {fn.size:6}'
              + (f'   ported: {journal}' if journal else ''))
    done = [r for r in rows if r[2]]
    if done:
        print(f'\n{len(done)} of these are already ported — read that journal entry before starting.')
    return 0


# --- kit dis ----------------------------------------------------------------

DIS_DOC = """Disassemble a range out of the program ELF — no Ghidra, no emulator.

  kit dis 2001a0 32f780      a range
  kit dis 2001a0 --len 0x80  or a length
  kit dis @serve_entry --len 64

The R5900's own instructions are decoded here (madd.s and the rest of the accumulator ops, the MMI
multimedia set, VU0 macro mode), because generic MIPS disassemblers mis-decode or refuse them. Ordinary
MIPS goes through llvm-mc when it is installed; without it those lines come out as `.word`."""


def cmd_dis(argv: list[str]) -> int:
    from ps2kit.cli import parser
    ap = parser('dis', DIS_DOC)
    ap.add_argument('start')
    ap.add_argument('end', nargs='?', help='end address (exclusive); or use --len')
    ap.add_argument('--len', '-n', dest='length', metavar='N', help='bytes instead of an end address')
    ap.add_argument('--program', '-p', metavar='NAME')
    ap.add_argument('--limit', type=int, default=400, metavar='N', help='instructions to print (default 400)')
    args = ap.parse_args(argv)
    try:
        prog = args.program or default_program()
        start = resolve(args.start, prog)
        if args.end:
            end = resolve(args.end, prog)
        elif args.length:
            end = start + (num(args.length, '--len') or 0)
        else:
            fn = funcs(prog).get(start) if _have(prog, '.funcs.tsv') else None
            end = start + (fn.size if fn else 0x80)
        insns, tool = disasm(start, end, prog)
        print(f'# {prog}  {start:08x}..{end:08x}  ({len(insns)} instructions'
              + (f', ordinary MIPS via {tool})' if tool else ', R5900 ops only — install llvm-mc for the rest)'))
        for ins in insns[:args.limit]:
            print(ins)
        if len(insns) > args.limit:
            print(f'… {len(insns) - args.limit} more (--limit N)')
    except DumpError as e:
        print(f'kit dis: {e}', file=sys.stderr)
        return 1
    return 0


def _have(program: str, ext: str) -> bool:
    return cfg().ctx('decomp', f'{program}{ext}').exists()


# --- kit data ---------------------------------------------------------------

DATA_DOC = """Read a table out of the program image.

  kit data 3a1200 0x40            64 bytes as u32 (the default)
  kit data 3a1200 0x40 --as f32   as floats — this is how tuning tables read
  kit data @shot_table 0x120 --as hex
  kit data --strings walk         every printable run matching "walk", with its address

Reading tuning tables is half the work of a port, and they are only ever bytes at an address in the
overlay. Addresses are the ones the game uses, not file offsets: `kit elf` put the overlay where it
runs. For a range you have already typed in Ghidra, `kit ghidra data` prints its labels and types too."""


def cmd_data(argv: list[str]) -> int:
    from ps2kit.cli import parser
    ap = parser('data', DATA_DOC)
    ap.add_argument('addr', nargs='?')
    ap.add_argument('len', nargs='?', default='0x40')
    ap.add_argument('--strings', metavar='PAT',
                    help='instead: printable runs matching PAT (substring or regex), with addresses')
    ap.add_argument('--min-len', type=int, default=4, metavar='N', help='--strings: shortest run to report')
    ap.add_argument('--as', dest='as_', default='u32', choices=sorted(FORMATS), help='how to read it')
    ap.add_argument('--program', '-p', metavar='NAME')
    ap.add_argument('--limit', type=int, default=64, metavar='N', help='rows to print (default 64)')
    args = ap.parse_args(argv)
    try:
        prog = args.program or default_program()
        if args.strings:
            hits = strings(args.strings, prog, args.min_len)
            print(f'# {prog}  {len(hits)} strings matching {args.strings!r}')
            for addr, text in hits[:args.limit]:
                print(f'{addr:08x}  {text[:120]}')
            if len(hits) > args.limit:
                print(f'… {len(hits) - args.limit} more (--limit N, or narrow the pattern)')
            return 0
        if not args.addr:
            ap.error('give an address, or --strings PAT')
        addr = resolve(args.addr, prog)
        length = num(args.len, 'len') or 0x40
        rows = data(addr, length, args.as_, prog)
        print(f'# {prog}  {addr:08x}  {length} bytes as {args.as_}')
        for row in rows[:args.limit]:
            print(row)
        if len(rows) > args.limit:
            print(f'… {len(rows) - args.limit} more rows (--limit N)')
    except DumpError as e:
        print(f'kit data: {e}', file=sys.stderr)
        return 1
    return 0


def _rel(p: Path) -> str:
    try:
        return str(Path(p).resolve().relative_to(ROOT))
    except ValueError:
        return str(p)
