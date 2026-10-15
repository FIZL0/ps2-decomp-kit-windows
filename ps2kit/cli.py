"""`kit <command>` — the one entry point. Every subcommand is a thin shell over a module in `ps2kit/`.

Grouped the way the work actually goes:

  set up        setup · doctor · demo · sym
  get the code  extract · elf · ghidra · fn · xref · dis · data
  watch it run  pcsx2 · pine · pad · shot · probe · record · state
  prove it      oracle · check · audit
  keep going    plan · journal · done · run

Design rule: every command prints something short and useful on success and one actionable line on
failure. Nothing here ever dumps a megabyte into an agent's context — that is what the `--limit`,
`head`-style caps and the guard hook are for.
"""
from __future__ import annotations

import argparse
import importlib
import sys

CMDS = {
    # name:        (module, function, one-line help)
    'setup':   ('ps2kit.setup', 'main', 'interactive first-time setup; writes kit.toml'),
    'doctor':  ('ps2kit.setup', 'doctor', 'check every prerequisite and say exactly what to fix'),
    'demo':    ('ps2kit.demo', 'cmd_demo', 'generate a synthetic game to practise the whole loop on (--check)'),
    'sym':     ('ps2kit.setup', 'symbols', 'list or add named RAM addresses ([symbols] in kit.toml)'),

    'extract': ('ps2kit.disc', 'cmd_extract', 'unpack the disc image into context/iso'),
    'elf':     ('ps2kit.elf', 'cmd_elf', 'build one ELF per program/overlay for Ghidra (--scan to guess them)'),
    'ghidra':  ('ps2kit.ghidra', 'cmd_ghidra', 'headless import + analysis, then dump decompilation and xrefs'),
    'fn':      ('ps2kit.decomp', 'cmd_fn', 'print one decompiled function by address or name'),
    'xref':    ('ps2kit.decomp', 'cmd_xref', 'callers/callees of an address; --similar finds look-alike functions'),
    'dis':     ('ps2kit.decomp', 'cmd_dis', 'disassemble an address range, R5900 ops included'),
    'data':    ('ps2kit.decomp', 'cmd_data', 'read a table out of a program image as ints/floats/structs'),

    'pcsx2':   ('ps2kit.emu', 'cmd_pcsx2', 'start/stop/status the emulator with PINE on (and kit copies)'),
    'pine':    ('ps2kit.pine', 'cmd_pine', 'read/write live RAM, save/load states, batch reads'),
    'pad':     ('ps2kit.pad', 'cmd_pad', 'virtual controller: serve it, then send button/stick scripts'),
    'shot':    ('ps2kit.emu', 'cmd_shot', 'screenshot the emulator (or a kit copy) without focusing it'),
    'probe':   ('ps2kit.probe', 'cmd_probe', 'find things in RAM: vsync counter, changing values, pointer chains'),
    'record':  ('ps2kit.record', 'cmd_record', 'capture a watch spec frame by frame to CSV/binary'),
    'state':   ('ps2kit.emu', 'cmd_state', 'inspect a save state: list parts, extract its EE RAM'),

    'oracle':  ('ps2kit.oracle', 'cmd_oracle', 'run one original function on chosen inputs (no emulator)'),
    'check':   ('ps2kit.checks', 'cmd_check', 'run the port\'s tests, summarised to a few lines'),
    'audit':   ('ps2kit.checks', 'cmd_audit', 'check no retail address or decompiler name got committed'),

    'plan':    ('ps2kit.plan', 'main', 'the task list: next, brief, tick, block, add, move…'),
    'journal': ('ps2kit.journal', 'cmd_journal', 'start or finish a journal entry for a task'),
    'done':    ('ps2kit.checks', 'cmd_done', 'test + tick + commit a finished task in one call'),
    'run':     ('ps2kit.runner', 'cmd_run', 'unattended: keep agents working through the task list'),
}

ALIASES = {'f': 'fn', 'x': 'xref', 'p': 'plan', 'c': 'check', 'ss': 'shot', 'r': 'record'}


def usage() -> str:
    width = max(len(k) for k in CMDS)
    groups = [('set up', ['setup', 'doctor', 'demo', 'sym']),
              ('get the code', ['extract', 'elf', 'ghidra', 'fn', 'xref', 'dis', 'data']),
              ('watch it run', ['pcsx2', 'pine', 'pad', 'shot', 'probe', 'record', 'state']),
              ('prove it', ['oracle', 'check', 'audit']),
              ('keep going', ['plan', 'journal', 'done', 'run'])]
    out = ['ps2-decomp-kit — decompile and re-implement a PlayStation 2 game, with proof.', '',
           'usage: kit <command> [args]    ·    kit <command> --help', '']
    for title, names in groups:
        out.append(f'  {title}')
        for n in names:
            out.append(f'    {n:<{width}}  {CMDS[n][2]}')
        out.append('')
    out += ['First time here:  kit setup   then   kit doctor',
            'No disc yet?      kit demo --check   then   docs/EXAMPLE.md',
            'Docs: README.md, SETUP.md, WINDOWS.md (what is different on Windows), docs/WORKFLOW.md']
    return '\n'.join(out)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ('-h', '--help', 'help'):
        print(usage())
        return 0
    if argv[0] in ('-V', '--version'):
        print('ps2-decomp-kit 1.0')
        return 0
    name = ALIASES.get(argv[0], argv[0])
    if name not in CMDS:
        near = [k for k in CMDS if k.startswith(name[:2])]
        print(f'kit: unknown command {argv[0]!r}' + (f' — did you mean: {", ".join(near)}?' if near else ''),
              file=sys.stderr)
        print(f'\n{usage()}', file=sys.stderr)
        return 2
    mod, fn, _ = CMDS[name]
    try:
        module = importlib.import_module(mod)
    except ImportError as e:
        print(f'kit {name}: {e}\nMissing a Python package? See SETUP.md step 2.', file=sys.stderr)
        return 2
    try:
        rc = getattr(module, fn)(argv[1:])
    except KeyboardInterrupt:
        print('\ninterrupted', file=sys.stderr)
        return 130
    except BrokenPipeError:
        return 0
    return 0 if rc is None else int(rc)


def parser(prog: str, doc: str) -> argparse.ArgumentParser:
    """A consistently-formatted parser for subcommands."""
    return argparse.ArgumentParser(prog=f'kit {prog}', description=doc,
                                   formatter_class=argparse.RawDescriptionHelpFormatter)


if __name__ == '__main__':
    sys.exit(main())
