"""Drive Ghidra headless, so nobody has to remember the invocation.

Why this exists. The whole decompilation workflow is two Ghidra commands, but they are long, order-
sensitive and expensive to get wrong:

* **Analysing** a PS2 program takes tens of minutes and writes into the project. You run it once per
  program, ever.
* **Dumping** (decompile every function, dump every xref) runs over the *already analysed* project with
  `-noanalysis -readOnly` and takes seconds to a couple of minutes. You run it whenever a script changes.

Leave out `-noanalysis` on the second one and you have silently re-run the first one: twenty minutes of
nothing, usually noticed after the third time. So `analyse` and `dump` are separate commands here and
`dump` always passes both flags.

The other sharp edge is the processor. Ghidra does not ship the PS2's CPU: the EE is `r5900:LE:32:default`
from the ghidra-emotionengine-reloaded extension, and without it the import either refuses or (worse)
loads as generic MIPS and quietly mis-decodes every multimedia and VU instruction in the game's maths.
`need_extension()` checks for it before spending any time and prints the install path, because the native
failure mode is a Java stack trace about a missing language ID.

The IOP (sound and I/O modules, `.IRX`) is a plain R3000: `MIPS:LE:32:default`, stock Ghidra, its own
project.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

from ps2kit import platform_ as plat
from ps2kit.config import cfg, ROOT

EE = 'r5900:LE:32:default'          # PS2 Emotion Engine, from the extension below
IOP = 'MIPS:LE:32:default'          # PS2 I/O processor: a plain little-endian R3000
EXTENSION_URL = 'https://github.com/chaoticgd/ghidra-emotionengine-reloaded/releases'

SCRIPTS = ROOT / 'ghidra_scripts'
PRINT_CAP = 30                      # lines of Ghidra's log that reach the caller; the rest is in the file


class GhidraError(Exception):
    pass


# --- the install --------------------------------------------------------------

def headless() -> Path:
    return plat.ghidra_headless(cfg().need_ghidra())


def ee_extension(home: Path | None = None) -> Path | None:
    """Where the EmotionEngine language lives, or None. Looks in the install and in the user's own
    extension directory, and confirms by the language definition rather than by a directory name."""
    home = home or cfg().ghidra
    roots = []
    if home:
        roots += [home / 'Ghidra' / 'Extensions', home / 'Ghidra' / 'Processors']
    for d in plat.ghidra_user_dirs():
        roots += list(d.glob('*/Extensions'))
    for root in roots:
        if not root.is_dir():
            continue
        for ldef in root.glob('*/data/languages/*.ldefs'):
            try:
                if 'r5900' in ldef.read_text(encoding='utf-8', errors='replace'):
                    return ldef.parents[2]
            except OSError:
                pass
    return None


def need_extension() -> Path:
    ext = ee_extension()
    if ext:
        return ext
    home = cfg().need_ghidra()
    raise GhidraError(
        'Ghidra has no r5900 processor, so it cannot read PS2 code.\n'
        f'Install ghidra-emotionengine-reloaded for your Ghidra version:\n'
        f'    {EXTENSION_URL}\n'
        f'Unzip it so that the directory lands in:\n'
        f'    {home / "Ghidra" / "Extensions"}/\n'
        '(or install it from Ghidra\'s GUI: File > Install Extensions). Then re-run this command.\n'
        'Without it the import either fails on an unknown language ID or — much worse — loads as generic\n'
        'MIPS and mis-decodes every VU and multimedia instruction in the game\'s maths.')


# --- running it ---------------------------------------------------------------

def _run(args: list[str], log: Path, what: str, timeout: int, quiet: bool = False) -> int:
    """Run analyzeHeadless, tee its output to `log`, and let only a summary reach the caller."""
    log.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    shown = 0
    print(f'{what}\n  log: {_rel(log)}', flush=True)
    with open(log, 'w', encoding='utf-8') as fh:
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             errors='replace', bufsize=1, cwd=ROOT)
        beat = started
        try:
            for line in p.stdout:
                fh.write(line)
                line = line.rstrip()
                if time.time() - beat > 30:
                    beat = time.time()
                    print(f'  … {int(beat - started)}s', flush=True)
                if quiet:
                    continue
                keep = re.search(r'ERROR|Exception|FAILED|Import failed|Analyzing|Packed database|'
                                 r'^\s*\w+\.java>', line)
                if keep and shown < PRINT_CAP:
                    shown += 1
                    print('  ' + line[:160], flush=True)
                elif keep and shown == PRINT_CAP:
                    shown += 1
                    print('  … (more in the log)', flush=True)
                if time.time() - started > timeout:
                    p.kill()
                    raise GhidraError(f'{what}: still running after {timeout}s — raise [ghidra] timeout '
                                      f'in kit.toml or look at {_rel(log)}')
            rc = p.wait()
        finally:
            if p.poll() is None:
                p.kill()
    took = int(time.time() - started)
    print(f'  {"done" if rc == 0 else f"FAILED (exit {rc})"} in {took // 60}m{took % 60:02d}s', flush=True)
    if rc != 0:
        tail = log.read_text(encoding='utf-8', errors='replace').splitlines()[-12:]
        print('\n'.join('  | ' + t[:160] for t in tail), file=sys.stderr)
    return rc


def analyse(program: str, elf: str | os.PathLike, processor: str = EE, force: bool = False,
            timeout: int | None = None) -> int:
    """Import `elf` into the project as `<program>.elf` and run Ghidra's full analysis on it.

    Slow — tens of minutes for a megabyte of game code — and only ever needed once per program. Everything
    afterwards goes through `postscript()`.
    """
    c = cfg()
    elf = Path(elf)
    if not elf.exists():
        raise GhidraError(f'no ELF for {program!r} at {elf}\n    kit elf --program {program}')
    if processor == EE:
        need_extension()
    c.ghidra_project.mkdir(parents=True, exist_ok=True)
    args = [str(headless()), str(c.ghidra_project), c.ghidra_name,
            '-import', str(elf), '-processor', processor,
            '-scriptPath', str(SCRIPTS), '-analysisTimeoutPerFile', str(timeout or c.ghidra_timeout)]
    if force:
        args.append('-overwrite')
    return _run(args, c.ctx('logs', f'ghidra-analyse-{program}.log'),
                f'analysing {program} ({elf.stat().st_size // 1024} KB, {processor}) — '
                f'this takes tens of minutes', timeout or c.ghidra_timeout)


def postscript(name: str, args: list[str], programs: list[str] | None = None, quiet: bool = False) -> int:
    """Run `ghidra_scripts/<name>.java` over already-analysed programs. Seconds, not tens of minutes.

    `-noanalysis -readOnly` is not an optimisation, it is the whole point: without them this re-analyses.
    """
    c = cfg()
    script = SCRIPTS / (name if name.endswith('.java') else f'{name}.java')
    if not script.exists():
        raise GhidraError(f'no such script: {_rel(script)} (have: '
                          + ', '.join(sorted(p.stem for p in SCRIPTS.glob("*.java"))) + ')')
    if not c.ghidra_project.exists():
        raise GhidraError(f'no Ghidra project at {_rel(c.ghidra_project)}\n    kit ghidra analyse')
    rc = 0
    for program in (programs or c.programs):
        cmd = [str(headless()), str(c.ghidra_project), c.ghidra_name,
               '-process', f'{program}.elf', '-noanalysis', '-readOnly',
               '-scriptPath', str(SCRIPTS), '-postScript', script.name, *[str(a) for a in args]]
        rc |= _run(cmd, c.ctx('logs', f'ghidra-{script.stem.lower()}-{program}.log'),
                   f'{script.stem} on {program}', c.ghidra_timeout, quiet=quiet)
    return rc


# --- what is in the project ---------------------------------------------------

def project_programs() -> list[str] | None:
    """Program names in the project, or None when they cannot be read.

    Ghidra's local project filesystem keeps an index of file names; parsing it beats the alternative,
    which is starting a JVM just to list four strings. Best-effort by design — `status` says "unknown"
    rather than lying.
    """
    rep = cfg().ghidra_project / f'{cfg().ghidra_name}.rep'
    index = rep / '~index.dat'
    if index.is_file():
        names = set()
        for line in index.read_text(encoding='utf-8', errors='replace').splitlines():
            for m in re.finditer(r'[\w.\-]+\.elf', line):
                names.add(m.group(0))
        if names:
            return sorted(names)
    if rep.is_dir():
        names = {m.group(0) for p in rep.rglob('*.prp')
                 for m in [re.search(r'[\w.\-]+\.elf', p.read_text(encoding='utf-8', errors='replace'))]
                 if m}
        return sorted(names) if names else None
    return None


# --- kit ghidra -------------------------------------------------------------

DOC = """Headless Ghidra: analyse once, then dump as often as you like.

  kit ghidra analyse              import + full analysis of every program  (SLOW: tens of minutes each)
  kit ghidra analyse -p game      just one
  kit ghidra dump                 decompilation + function table + xrefs   (seconds to minutes)
  kit ghidra asm 2001a0 200240    the instructions of those functions
  kit ghidra data 1004c0 0x200    a memory range with Ghidra's own data types applied
  kit ghidra status               what is in the project, and how stale each dump is

Run `analyse` once per program and never again; everything after that is `dump`, which re-uses the
analysis (`-noanalysis -readOnly`). If a dump looks wrong *after you renamed or retyped things in the
Ghidra GUI*, that is expected — re-run `dump`, not `analyse`."""


def cmd_ghidra(argv: list[str]) -> int:
    from ps2kit.cli import parser
    ap = parser('ghidra', DOC)
    sub = ap.add_subparsers(dest='action', metavar='analyse|dump|asm|data|status')

    a = sub.add_parser('analyse', aliases=['analyze'], help='import and fully analyse (slow, once)')
    a.add_argument('--program', '-p', default='all', metavar='NAME', help='a program name, or "all"')
    a.add_argument('--force', action='store_true', help='re-import a program that is already there')
    a.add_argument('--iop', action='store_true', help=f'it is an IOP module: use {IOP}')

    d = sub.add_parser('dump', help='decompilation, function table and xrefs')
    d.add_argument('--program', '-p', default='all', metavar='NAME')
    d.add_argument('--only', choices=('decomp', 'xrefs'), help='just one of the two dumps')

    s = sub.add_parser('asm', help='disassemble whole functions by address')
    s.add_argument('addr', nargs='+', help='addresses (hex, 0x…, or @symbol)')
    s.add_argument('--program', '-p', metavar='NAME')

    t = sub.add_parser('data', help='a memory range, with Ghidra\'s data types applied')
    t.add_argument('addr')
    t.add_argument('len', nargs='?', default='0x80')
    t.add_argument('--as', dest='as_', default='auto',
                   choices=('auto', 'u8', 'u16', 'u32', 's32', 'f32', 'hex'),
                   help="how to read the raw rows ('auto' guesses float vs int per word)")
    t.add_argument('--program', '-p', metavar='NAME')

    sub.add_parser('status', help='what exists and how old it is')
    args = ap.parse_args(argv)
    if not args.action:
        ap.print_help()
        return 2

    try:
        if args.action in ('analyse', 'analyze'):
            return _analyse(args)
        if args.action == 'dump':
            return _dump(args)
        if args.action == 'asm':
            return _asm(args)
        if args.action == 'data':
            return _data(args)
        return _status()
    except GhidraError as e:
        print(f'kit ghidra: {e}', file=sys.stderr)
        return 1


def _programs(which: str | None) -> list[str]:
    c = cfg()
    if not which or which == 'all':
        return c.programs
    if which not in c.programs:
        raise GhidraError(f'no program named {which!r} (have: {", ".join(c.programs)})')
    return [which]


def _analyse(args) -> int:
    from ps2kit import elf as elf_mod
    c = cfg()
    names = _programs(args.program)
    have = project_programs()
    todo = [n for n in names if args.force or not have or f'{n}.elf' not in have]
    if not todo:
        print(f'already analysed: {", ".join(names)}  (--force to redo; you almost never want to)')
        print('next: kit ghidra dump')
        return 0
    print(f'{len(todo)} program(s) to analyse: {", ".join(todo)}')
    print('This is the slow step — tens of minutes each for a megabyte of game code. It happens once;')
    print('after it, only kit ghidra dump. Leave it running.')
    rc = 0
    for n in todo:
        rc |= analyse(n, elf_mod.elf_path(n), IOP if args.iop else EE, force=args.force)
    if rc == 0:
        print('next: kit ghidra dump      # then kit fn <addr>')
    return rc


def _dump(args) -> int:
    c = cfg()
    outdir = c.ctx('decomp', '.keep').parent
    names = _programs(args.program)
    rc = 0
    if args.only != 'xrefs':
        rc |= postscript('DumpDecomp', [str(outdir)], names)
    if args.only != 'decomp':
        rc |= postscript('DumpXrefs', [str(outdir)], names)
    for n in names:
        for ext in ('.c', '.funcs.tsv', '.xrefs.tsv'):
            p = outdir / f'{n}{ext}'
            if p.exists():
                print(f'  {_rel(p):<40} {p.stat().st_size / 1048576:6.1f} MB'
                      + (f'  {sum(1 for _ in open(p, errors="replace"))} rows' if ext.endswith('.tsv') else ''))
    if rc == 0:
        print('Never read these whole — they are tens of megabytes. Use: kit fn <addr> · kit xref <addr>')
    return rc


def _asm(args) -> int:
    c = cfg()
    addrs = [f'{c.sym(a):x}' for a in args.addr]
    prog = args.program or c.programs[-1]
    out = c.ctx('notes', f'asm_{prog}_{addrs[0]}.txt')
    rc = postscript('DumpAsm', [str(out), *addrs], [prog], quiet=True)
    if rc == 0 and out.exists():
        _print_capped(out, 200)
    return rc


def _data(args) -> int:
    from ps2kit.config import num
    c = cfg()
    addr, length = c.sym(args.addr), num(args.len, 'len')
    prog = args.program or c.programs[-1]
    out = c.ctx('notes', f'data_{prog}_{addr:x}.txt')
    rc = postscript('DumpData', [str(out), f'{addr:x}', str(length), args.as_], [prog], quiet=True)
    if rc == 0 and out.exists():
        _print_capped(out, 120)
    return rc


def _status() -> int:
    from ps2kit import elf as elf_mod
    c = cfg()
    print(f'ghidra   {c.ghidra or "[ghidra] home not set"}'
          + ('  (analyzeHeadless ok)' if c.ghidra and (c.ghidra / "support" / "analyzeHeadless").exists()
             else '  (analyzeHeadless MISSING)'))
    ext = ee_extension()
    print(f'r5900    {"ok: " + str(ext) if ext else "NOT INSTALLED — " + EXTENSION_URL}')
    print(f'project  {_rel(c.ghidra_project)}/{c.ghidra_name}'
          + ('' if c.ghidra_project.exists() else '   (not created yet)'))
    have = project_programs()
    print(f'imported {", ".join(have) if have else "unknown" if c.ghidra_project.exists() else "nothing"}')
    print()
    print(f'{"program":<10} {"elf":>10}  {"decomp":>10} {"funcs":>8} {"xrefs":>10}  age')
    for n in c.programs:
        e = elf_mod.elf_path(n)
        dec = c.ctx('decomp', f'{n}.c')
        row = [f'{n:<10}', _size(e), _size(dec), _size(c.ctx('decomp', f'{n}.funcs.tsv')),
               _size(c.ctx('decomp', f'{n}.xrefs.tsv'))]
        age = ''
        if dec.exists():
            days = (time.time() - dec.stat().st_mtime) / 86400
            age = f'  {days:.0f}d old' + ('  (older than the ELF — re-dump)'
                                          if e.exists() and e.stat().st_mtime > dec.stat().st_mtime else '')
        print(' '.join(row) + age)
    if not any(c.ctx('decomp', f'{n}.c').exists() for n in c.programs):
        print('\nNothing dumped yet:  kit ghidra analyse   then   kit ghidra dump')
    return 0


def _size(p: Path) -> str:
    if not p.exists():
        return f'{"-":>10}'
    n = p.stat().st_size
    return f'{n / 1048576:9.1f}M' if n >= 1048576 else f'{n / 1024:9.0f}K'


def _print_capped(path: Path, limit: int) -> None:
    with open(path, encoding='utf-8', errors='replace') as fh:
        for i, line in enumerate(fh):
            if i >= limit:
                print(f'… capped at {limit} lines; all of it: {_rel(path)}')
                return
            print(line.rstrip())


def _rel(p: Path) -> str:
    try:
        return str(Path(p).resolve().relative_to(ROOT))
    except ValueError:
        return str(p)


if __name__ == '__main__':
    sys.exit(cmd_ghidra(sys.argv[1:]))
