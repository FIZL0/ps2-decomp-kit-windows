"""`kit plan`: the task list as a tool. agent-runner's plan (https://github.com/FIZL0/agent-runner) does
the work, with the kit's settings (see ps2kit/runner.py); `kit plan --help` lists the subcommands.

The kit adds one, `kit plan phase ID`, which ends every brief: a task with a SPEC.md in its journal is in
its *spec* phase while the spec has open questions, and in its *port* phase once it has none. In the port
phase the decompilation is locked for the session (`.cleanroom`, read by the guard hook) when it runs in a
worktree; `kit done` lifts the lock.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from ps2kit.config import cfg, ROOT


def _plan():
    """agent-runner's plan module, pointed at this checkout and the kit's settings."""
    from agent_runner import plan as P
    from ps2kit.runner import use
    use()
    os.environ.setdefault('AGENT_RUNNER_REPO', str(ROOT))  # a test points it at a scratch repo
    P.PROG = 'kit plan'
    return P


def tick(tid: str) -> int:
    return _plan().tick(tid)


def block(tid: str, why: str, after=None) -> None:
    _plan().block(tid, why, after=after)


def phase(tid: str, journal=None, cwd=None) -> list[str]:
    """The spec/port phase section of a task's brief, from the newest SPEC.md in its own journal; locks or
    unlocks the decompilation in the checkout at `cwd` to match."""
    journal = Path(journal or cfg(False).journal)
    try:
        dirs = sorted(d for d in os.listdir(journal) if re.search(rf'-{re.escape(tid.lower())}(-|$)', d.lower()))
    except OSError:
        dirs = []
    specs = [journal / d / 'SPEC.md' for d in dirs if (journal / d / 'SPEC.md').is_file()]
    out = []
    if specs:
        rel = os.path.relpath(specs[-1], cwd or ROOT).replace(os.sep, '/')
        body = specs[-1].read_text(encoding='utf-8', errors='replace')
        asked = re.search(r'^## Open questions\s*\n(.*?)(?=^## |\Z)', body, re.M | re.S)
        if asked and re.sub(r'^[\s-]*none\.?\s*$', '', asked.group(1), flags=re.M | re.I).strip():
            out = [f'## Spec phase: answer the open questions in {rel}',
                   'Read the decompilation, answer them in the spec (behaviour in your own words), empty the '
                   f'section, `kit done {tid} "<msg>" --spec`, stop. No port code this session.']
        else:
            out = [f'## Port phase: implement from {rel} and the tests alone',
                   'The decompilation is locked for this session (dumps, Ghidra, `kit fn/xref/dis`). Prove the '
                   'port with the recordings and the oracle as usual. Something the spec does not say: write it '
                   f'under `## Open questions` in the spec, `kit done {tid} "<msg>" --spec`, stop.', '',
                   body.rstrip()]
    cleanroom(bool(out) and out[0].startswith('## Port phase'), tid, cwd)
    return out


def cleanroom(on: bool, tid: str = '', cwd=None) -> None:
    """Lock (or unlock) the decompilation for the session in this checkout: the guard hook refuses it while
    `.cleanroom` exists. Only in a worktree (a runner slot): in the main checkout another session may be
    researching, so there the brief's *Port phase* is the rule and nothing enforces it."""
    def rev(f):
        return subprocess.run(['git', 'rev-parse', '--path-format=absolute', f], cwd=cwd, capture_output=True,
                              text=True).stdout.strip()
    top = rev('--show-toplevel')
    if not top:
        return
    lock = Path(top) / '.cleanroom'
    if on and rev('--git-dir') != rev('--git-common-dir'):
        lock.write_text(tid + '\n', encoding='utf-8')
    elif not on and lock.exists():
        lock.unlink()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ['phase'] and len(argv) == 2:
        print('\n'.join(phase(argv[1])))
        return 0
    P = _plan()
    if argv[:1] in (['-h'], ['--help'], ['help']):
        print(P.HELP.replace('./run plan', 'kit plan').replace('./run run', 'kit run').rstrip() +
              '\n\n    kit plan phase <ID>        the spec/port phase section of a brief (sets .cleanroom)')
        return 0
    return P.main(argv)
