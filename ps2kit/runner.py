"""`kit run` and `kit stop`: the unattended loop is agent-runner (https://github.com/FIZL0/agent-runner).

    kit run once            one task, then stop (the way to try it the first time)
    kit run                 3 agents, mode d (different files), a Windows Terminal tab each
    kit run 5 --mode p      5 agents, strictly in plan/TODO.md order, files may overlap
    kit run --dry-run 2     what 2 slots would start now, and why the rest wait
    kit run --force         keep starting tasks past the weekly usage cap
    kit stop                the running tasks finish and merge, nothing new starts, the run exits

What the loop does (worktrees, picking, merges, the idle Stop hook, usage limits) is agent-runner's README.
This module only tells it about the kit: `ps2kit/runner.toml` holds the fixed settings, `use()` adds what
kit.toml decides and a kit.toml `[agent-runner]` table on top, and writes the result to
`<context>/notes/agent-runner.toml`, which every slot, merge and fix session reads through
AGENT_RUNNER_CONFIG. Each slot gets its own emulator (KIT_PCSX2=N) and closes it when its session ends.

The commands agent-runner runs itself (`[run] test`, `on_exit`, `[plan] brief_extra`, `[run.join]`) go
through the platform shell, cmd.exe on Windows, which runs neither `./kit` nor a script without an
extension. They are written with `{kit}`, which `settings()` turns into this interpreter and the checkout's
`kit` script, quoted for that shell.

Environment: KIT_AGENT (the agent CLI, claude), KIT_MODEL (opus), KIT_LEAN=0 (give the agents your MCP
servers and settings), KIT_IDLE_BG (seconds a session with a background task may sit idle, 1800),
KIT_CHECK_SLOTS (concurrent `kit check` builds, default the slot count clamped to 2..3).
"""
from __future__ import annotations

import json
import os
import sys
import tomllib
from pathlib import Path

from ps2kit.config import ROOT, cfg, write_atomic, CONFIG
from ps2kit.platform_ import quote_args

DEFAULTS = Path(__file__).with_name('runner.toml')

JOURNAL = """
Write the task's journal entry as you go (`kit journal new {tid} <slug>`), and under **Not verified / not \
1:1** list everything you could not prove against the original or could not match exactly, each with its \
reason — or `none`. A skipped proof that is written down is useful; a skipped proof that is silent is a bug \
nobody will find.
"""


def settings() -> dict:
    """runner.toml, plus what kit.toml decides, plus kit.toml's own `[agent-runner]` table (it wins)."""
    with open(DEFAULTS, 'rb') as f:
        s = tomllib.load(f)
    c = cfg(False)
    rel = lambda p: os.path.relpath(p, ROOT).replace(os.sep, '/')
    plan, journal, oracle = rel(c.plan), rel(c.journal), rel(c.oracle_file)
    context = c.raw.get('paths', {}).get('context', 'context')
    iso = str(c.raw.get('game', {}).get('iso', ''))
    s['plan'] |= {'dir': plan, 'journal': journal}
    s['run'] |= {
        'notes': f'{context}/notes',
        # relative paths only: agent-runner links each one relative to the checkout
        'links': [p for p in (context, '*.iso', iso) if p and not os.path.isabs(p)],
        'joinable': [f'{plan}/*.md', f'{journal}/*.md', oracle],
        'join': {oracle: '{kit} oracle join {union} {out}'},
        'fix_merges': c.fix_merges, 'fix_timeout': c.fix_timeout, 'fix_model': c.fix_model,
    }
    s['git'] = {'push': c.push_remote or False, 'push_merge': c.push_merge}
    for k, v in c.raw.get('agent-runner', {}).items():
        s[k] = s.get(k, {}) | v if isinstance(v, dict) else v
    return _kit(s, quote_args([sys.executable, 'kit']))


def _kit(v, kit: str):
    """`{kit}` in every string setting, as this interpreter running the checkout's `kit` (relative: each
    command runs in its own worktree)."""
    if isinstance(v, dict):
        return {k: _kit(x, kit) for k, x in v.items()}
    if isinstance(v, list):
        return [_kit(x, kit) for x in v]
    return v.replace('{kit}', kit) if isinstance(v, str) else v


def _toml(d: dict, prefix: str = '') -> list[str]:
    val = lambda v: ('true' if v else 'false') if isinstance(v, bool) else json.dumps(v, ensure_ascii=False)
    out = [f'{json.dumps(k)} = {val(v)}' for k, v in d.items() if not isinstance(v, dict)]
    for k, v in d.items():
        if isinstance(v, dict):
            name = f'{prefix}.{json.dumps(k)}' if prefix else json.dumps(k)
            out += ['', f'[{name}]', *_toml(v, name)]
    return out


def use() -> Path:
    """Write the merged settings (only when they changed) and point agent-runner at them."""
    f = cfg(False).ctx('notes', 'agent-runner.toml')
    text = '# written by ps2kit/runner.py from ps2kit/runner.toml and kit.toml; edit those\n' + \
        '\n'.join(_toml(settings())) + '\n'
    if not f.exists() or f.read_text(encoding='utf-8') != text:
        write_atomic(f, text)
    os.environ['AGENT_RUNNER_CONFIG'] = str(f)
    return f


def _slots(argv: list[str], default: int) -> int:
    takes = {'--mode', '--idle', '--stall', '--pause', '--model'}
    it = iter(argv)
    for a in it:
        if a in takes:
            next(it, None)
        elif not a.startswith('-'):
            return 1 if a == 'once' else int(a) if a.isdigit() else default
    return default


def cmd_run(argv: list[str]) -> int:
    from agent_runner import runner as R
    from agent_runner.config import cfg as ar_cfg
    os.chdir(ROOT)
    use()
    r = ar_cfg(ROOT).run  # the env knobs, for this run only; the shared file stays the same for everyone
    if model := os.environ.get('KIT_MODEL'):
        r['model'] = model  # fix_model already follows KIT_MODEL (config.fix_model)
    if agent := os.environ.get('KIT_AGENT'):
        r['agent'] = agent
    if os.environ.get('KIT_LEAN') == '0':
        r['lean'] = False
    os.environ.setdefault('AGENT_RUNNER_IDLE_BG', os.environ.get('KIT_IDLE_BG', '1800'))
    os.environ.setdefault('KIT_CHECK_SLOTS', str(max(2, min(_slots(argv, int(r['slots'])), 3))))
    if CONFIG.exists():
        os.environ.setdefault('KIT_CONFIG', str(CONFIG))  # kit.toml is git-ignored: slots read the main one
    R.PROMPT = R.PROMPT.replace('{journal}', JOURNAL)
    return R.cmd_run(argv, prog='kit run')


def cmd_stop(argv: list[str]) -> int:
    from agent_runner import runner as R
    os.chdir(ROOT)
    use()
    return R.cmd_stop(argv, prog='kit stop')
