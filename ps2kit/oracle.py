"""Call one of the game's own functions, with no emulator running.

    kit oracle list                           what has a name so far
    kit oracle add angle 0x1004c0              give an address a name (that is the documentation)
    kit oracle angle --floats 1.5,-2            call it and print what came back
    kit oracle tick --ints 0x1f4a20 --ram 3     …over a RAM image saved from a save state

The work is done by `crates/ps2-oracle`: a small R5900 interpreter that loads the boot ELF and the
overlays straight off the disc and runs one function to its `jr $ra`. It is for *looking* — a few calls
by hand while you work out what a function does. The proof comes next, in Rust, where a test can make
ten thousand calls and compare every one against your port bit for bit (see
`crates/ps2-oracle/tests/game.rs`).

What it cannot answer, ever: timing, frame order, DMA, VU1, the GS, or anything that depends on *when*
the function ran. Record the real machine for those (`kit record`).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from ps2kit.config import ROOT, cfg, num

MANIFEST = ROOT / 'crates' / 'ps2-oracle' / 'Cargo.toml'
RUSTUP = "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh"


def _hex(tok: str) -> int | None:
    try:
        return int(tok, 16)
    except ValueError:
        return None


def entries() -> list[tuple[str, str, int, str]]:
    """The addresses file as (name, program, address, note) rows, in file order.

    One line per function: `name [program] 0xaddr [note]`. The program column is optional because a
    game with no overlays has only one, and strongly recommended for every other game: overlays share
    their addresses, so an address with no program beside it is not an address. A note is either the
    rest of the line or the comment above it.
    """
    path = cfg(False).oracle_file   # the addresses file stands on its own: readable before kit.toml exists
    if not path.exists():
        return []
    out, note = [], ''
    for n, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        body, _, comment = line.partition('#')
        if not body.strip():
            note = comment.strip() or note
            continue
        parts = body.split()
        if len(parts) >= 2 and (addr := _hex(parts[1])) is not None:
            prog, rest = '', parts[2:]
        elif len(parts) >= 3 and (addr := _hex(parts[2])) is not None:
            prog, rest = parts[1], parts[3:]
        else:
            raise SystemExit(f'{path}:{n}: want `name [program] 0xaddr [note]`, got {line.strip()!r}')
        out.append((parts[0], prog, addr, ' '.join(rest) or note))
        note = ''
    return out


def cmd_list() -> int:
    path = cfg().oracle_file
    rows = entries()
    if not rows:
        print(f'{path}: no functions named yet.\n'
              f'    kit oracle add <name> <addr>     # after finding one with kit fn / kit xref')
        return 0
    width = max(len(n) for n, _, _, _ in rows)
    pw = max((len(p) for _, p, _, _ in rows), default=0)
    print(f'{path}  ({len(rows)} functions)')
    for name, prog, addr, note in rows:
        print(f'  {name:<{width}}  {prog:<{pw}}  {addr:#08x}'.rstrip()
              + (f'   {note[:60]}' if note else ''))
    return 0


def cmd_add(argv: list[str]) -> int:
    if len(argv) < 2:
        print('usage: kit oracle add <name> <addr> [note…]', file=sys.stderr)
        return 2
    name, addr, note = argv[0], cfg().sym(argv[1]), ' '.join(argv[2:])
    if not name.replace('_', '').isalnum():
        print(f'{name!r}: use a plain name (letters, digits, underscores) — it goes in Rust tests',
              file=sys.stderr)
        return 2
    for have, _, at, _ in entries():
        if have == name:
            if at == addr:
                print(f'{name} is already {addr:#08x}')
                return 0
            print(f'{name} is already {at:#08x} in {cfg().oracle_file}; edit the file if {addr:#08x} '
                  f'is the right one now', file=sys.stderr)
            return 1
    path = cfg().oracle_file
    path.parent.mkdir(parents=True, exist_ok=True)
    head = '' if not path.exists() or path.read_text(encoding='utf-8').endswith('\n') else '\n'
    prog = (cfg().programs or [''])[-1]
    row = f'{name} {prog} 0x{addr:08x}'.replace('  ', ' ')
    with path.open('a', encoding='utf-8') as f:
        f.write(f'{head}{row}' + (f'   {note}\n' if note else '\n'))
    print(f'{row}{"   " + note if note else ""} → {path}')
    return 0


def ram_arg(spec: str) -> str:
    """`--ram 3` means the RAM image from save-state slot 3; anything else is a path."""
    if not spec.isdigit():
        return spec
    hits = sorted(Path(cfg().ctx('states')).glob(f'*{spec}*.eeMemory.bin'), key=os.path.getmtime)
    if not hits:
        raise SystemExit(f'no RAM image for slot {spec} in {cfg().ctx("states")}\n'
                         f'    kit state ram {spec}      # writes one from the save state')
    return str(hits[-1])


def cmd_call(args) -> int:
    cargo = shutil.which('cargo')
    if not cargo:
        print('The oracle is a Rust program and there is no cargo on PATH.\n'
              f'    {RUSTUP}\n'
              'Nothing else in the kit needs it.', file=sys.stderr)
        return 2
    cmd = [cargo, 'run', '-q', '--manifest-path', str(MANIFEST), '--bin', 'call', '--', args.what]
    for flag in ('ints', 'floats', 'budget'):
        if getattr(args, flag):
            cmd += [f'--{flag}', str(getattr(args, flag))]
    if args.ram:
        cmd += ['--ram', ram_arg(args.ram)]
    for r in args.read or []:
        cmd += ['--read', r]
    try:
        return subprocess.call(cmd, cwd=ROOT)
    except KeyboardInterrupt:
        return 130


def cmd_oracle(argv: list[str]) -> int:
    from ps2kit.cli import parser
    p = parser('oracle', __doc__)
    p.add_argument('what', nargs='?', help='a name from the addresses file, an address, or list/add')
    p.add_argument('rest', nargs='*', help='for `add`: the name and address')
    p.add_argument('--ints', help='integer arguments, comma separated (a0, a1, …); 0x… allowed')
    p.add_argument('--floats', help='float arguments, comma separated ($f12, $f13, …)')
    p.add_argument('--ram', metavar='FILE|SLOT',
                   help='run over a saved RAM image instead of a freshly loaded disc')
    p.add_argument('--budget', type=int, help='give up after this many instructions (default 2,000,000)')
    p.add_argument('--read', action='append', metavar='ADDR:LEN',
                   help='also print memory after the call; repeatable')
    a = p.parse_args(argv)

    if a.what in (None, 'list'):
        return cmd_list() if a.what else (p.print_help() or 0)
    if a.what == 'add':
        return cmd_add(a.rest)
    if a.rest:
        print(f'unexpected argument {a.rest[0]!r} — arguments go in --ints / --floats', file=sys.stderr)
        return 2
    if a.ints:
        for v in a.ints.split(','):
            num(v, '--ints')          # fail here, with a good message, not inside cargo
    return cmd_call(a)
