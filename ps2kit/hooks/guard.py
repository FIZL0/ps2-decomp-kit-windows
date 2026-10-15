#!/usr/bin/env python3
"""PreToolUse hook: keep an agent from spending its whole session in one tool call.

Driven entirely by `kit.toml` (`cfg()`), so it is game-agnostic; it knows nothing but "the private directory
holds things that are far too big to read whole".

What it does, and why each one earned its place:

**A whole-file read of a decompilation dump.** `kit ghidra` writes one C file per program, and for a PS2
game that is 5–20 MB. A `Read` without `offset`/`limit`, or a `cat`/`grep` without a cap, puts all of it in
the conversation — which is then re-read on every following turn, so the session is over. Blocked, with the
three cheap ways to get the same answer: `kit fn <addr>` for one function, `kit xref <addr>` for its
callers and callees, `Read` with `offset`+`limit`, or a capped grep.

**`pgrep -f` / `pkill -f`.** With `-f`, pgrep matches against whole command lines — including the command line
of the shell that is doing the searching, because the pattern is literally in it. So `pgrep -f pcsx2` always
finds itself (an agent then concludes the emulator is running when it is not), and `pkill -f record` kills the
Bash tool call that ran it, and sometimes the agent process above it. These are rewritten to this script's own
`--pgrep` / `--pkill` mode, which matches and then drops the caller's own ancestors.

**Polling loops.** The default Bash timeout is two minutes, so a test run used to be pushed into the
background, and the agent then sat in `sleep 60; pgrep …` loops for ten minutes watching for it — burning a
turn, and its context, on every poll. Two fixes, both here: `kit check` and `kit done` get a long timeout
so they can simply run in the foreground, and a polling loop around a test or a background task is refused
with "end your turn; the completion notification wakes you".

Standalone by design: `py -3 ps2kit/hooks/guard.py` reads the hook JSON on stdin, exit 2 blocks with the
reason on stderr, anything else allows. It must never crash — a hook that throws blocks every tool call.

    py -3 ps2kit/hooks/guard.py            # as a hook (stdin = the hook payload)
    py -3 ps2kit/hooks/guard.py --pgrep -f pat    # the safe wrapper the rewrite points at
    py -3 ps2kit/hooks/guard.py --pkill -f pat
    py -3 ps2kit/hooks/guard.py --self-test
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # run directly: put the repo root on the path

ME = str(Path(__file__).resolve())
# How to spell "run this script" for the shell the agent's command goes to: `python3` is the name that
# exists on POSIX, and nothing fixed exists on Windows (see platform_.py_cmd). Quoted, so a repo path with
# a space in it still rewrites to a command that runs.
WRAPPER = __import__('ps2kit.platform_', fromlist=['py_cmd']).py_cmd(Path(ME))
BIG = int(os.environ.get('KIT_GUARD_MAX_BYTES', 512 * 1024))  # a file in context/ bigger than this is a dump
# The launcher is spelled `kit`, `.\kit`, `kit.cmd` or `kit.ps1` depending on the shell; match them all.
KIT = r'(?:\.[\\/])?\bkit(?:\.cmd|\.ps1)?'
TEST_CMDS = re.compile(KIT + r'\s+(check|done)\b|'
                       r'\b(cargo|ninja|make|meson|pytest|ctest|go)\s+(test|build|nextest)\b')
# A loop that watches something: pointless from inside a turn, whatever it is watching. The body may run
# over several commands or lines, so the match spans them (bounded, so a huge script can't walk the regex).
LOOP = re.compile(r'\b(while|until)\b[\s\S]{0,120}?\bdo\b[\s\S]{0,400}?\b(pgrep|BashOutput|jobs)\b|'
                  r'tail\s+-f\s+--pid')
# A long sleep: only a problem next to something that finishes on its own and notifies.
SLEEPY = re.compile(r'\bsleep\s+([3-9]\d|\d{3,})')
# `cat x`, `grep pat x`, `head -c big x`… with no cap on the output
CAPPED = re.compile(KIT + r'\s+(fn|xref|dis|data)\b|\|\s*(head|tail|wc|sed\b)|'
                    r'\bgrep\b[^|]*\s-[a-zA-Z]*[mcl]|\bwc\b|\bsed\s+-n|\bhead\s+-|\btail\s+-')


def _cfg():
    try:
        from ps2kit.config import cfg
        return cfg(False)
    except Exception:
        return None


def _dump_paths() -> tuple[str, str]:
    """(the decompilation directory, the private root) as repo-relative strings.

    Read straight out of `[paths] context` rather than through `cfg().ctx()`, because that one creates the
    directory as a side effect and a hook has no business creating anything.
    """
    c = _cfg()
    base = (c.raw.get('paths', {}).get('context') if c else None) or 'context'
    return f'{base}/decomp', base


def _is_dump(path: str) -> str | None:
    """Why reading all of `path` is a bad idea, or None."""
    if not path:
        return None
    decomp, private = _dump_paths()
    p = Path(path)
    full = p if p.is_absolute() else (Path.cwd() / p)
    s = str(full)
    name = Path(decomp).name
    in_decomp = f'{os.sep}{name}{os.sep}' in s or s.startswith(decomp) or f'/{name}/' in path
    if in_decomp and full.suffix in ('.c', '.h', '.txt', '.csv', '.asm', '.s', ''):
        return 'a decompilation dump'
    priv = Path(private).name
    in_private = f'{os.sep}{priv}{os.sep}' in s or f'/{priv}/' in path
    if in_private:
        try:
            if full.stat().st_size > BIG:
                return f'{full.stat().st_size // 1024} KB of generated data'
        except OSError:
            return None
    return None


HOW = ('Use `kit fn <addr>` for one function, `kit xref <addr>` for its callers and callees '
       '(`--similar` for look-alikes), `Read` with offset+limit, or a capped grep (`grep -m5`, `grep -c`, '
       '`| head`). Reading it whole puts megabytes in the conversation, which is then re-read on every turn '
       'for the rest of the session.')


def decide(ev: dict) -> tuple[int, str, dict | None]:
    """(exit code, message for the agent, updated tool input) for one hook payload."""
    tool = ev.get('tool_name') or ''
    i = ev.get('tool_input') or {}
    if not isinstance(i, dict):
        return 0, '', None

    if tool == 'Read':
        why = _is_dump(str(i.get('file_path', '')))
        if why and not i.get('limit') and not i.get('offset'):
            return 2, f'That file is {why}. {HOW}', None
        return 0, '', None

    if tool != 'Bash':
        return 0, '', None
    cmd = str(i.get('command', '') or '')
    if not cmd:
        return 0, '', None

    # a dump named in a shell command with nothing capping the output
    for tok in re.findall(r'[\w./@~-]+', cmd):
        if _is_dump(tok) and not CAPPED.search(cmd):
            return 2, f'`{tok}` is too big to read whole. {HOW}', None

    # polling loops around test runs and background tasks
    watching = bool(re.search(r'/tasks/\w+\.output|BashOutput', cmd)) or bool(TEST_CMDS.search(cmd))
    if LOOP.search(cmd) or (watching and SLEEPY.search(cmd)):
        return 2, ("Don't poll a background task or a test run: end your turn now (say what you are waiting "
                   "for). Its completion notification wakes you, and the runner keeps the session alive "
                   "meanwhile. `kit check` and `kit done` get a 60 min Bash timeout, so run them in the "
                   "foreground instead."), None

    upd = {}
    if TEST_CMDS.search(cmd) and not i.get('run_in_background'):
        upd['timeout'] = 3600000  # so a full test run can finish in the foreground, where the agent sees it
    # `-f` matches whole command lines, so the searching shell's own line matches too: pgrep -f finds itself,
    # pkill -f kills the caller. Point both at this script's wrapper, which drops the caller's ancestors.
    fixed = re.sub(r'(?<![\w/.-])(pgrep|pkill)(?=(\s+-[\w-]+)*\s+-\w*f)',
                   lambda m: f'{WRAPPER} --{m.group(1)}', cmd)
    if fixed != cmd:
        upd['command'] = fixed
    if upd:
        return 0, json.dumps({'hookSpecificOutput': {
            'hookEventName': 'PreToolUse', 'permissionDecision': 'allow',
            'updatedInput': {**i, **upd}}}), upd
    return 0, '', None


# --- the pgrep/pkill wrapper ---------------------------------------------------

def _ancestors() -> set[int]:
    """This process and every process above it: the ones a `-f` match must never report or kill."""
    out, pid = {os.getpid()}, os.getppid()
    for _ in range(64):
        if pid <= 1:
            break
        out.add(pid)
        r = subprocess.run(['ps', '-o', 'ppid=', '-p', str(pid)], capture_output=True, text=True)
        try:
            pid = int(r.stdout.strip())
        except ValueError:
            break
    return out


def wrapper(kill: bool, argv: list[str]) -> int:
    skip = _ancestors()
    if kill:  # never kill an agent process: the caller is one, and so are its siblings in a parallel run
        skip |= {int(p) for p in subprocess.run(['pgrep', '-x', 'claude'], capture_output=True,
                                                text=True).stdout.split()}
    sig, args = 'TERM', []
    it = iter(argv)
    for a in it:
        if a in ('--signal', '-s'):
            sig = next(it, 'TERM')
        elif a.startswith('--signal='):
            sig = a.split('=', 1)[1]
        elif kill and re.fullmatch(r'-(\d+|[A-Z]{2,7})', a):
            sig = a[1:]
        else:
            args.append(a)
    r = subprocess.run(['pgrep', *args], capture_output=True, text=True)
    hits = []
    for line in r.stdout.splitlines():
        pid = line.split()[0] if line.split() else ''
        if pid.isdigit() and int(pid) not in skip:
            hits.append(line)
    if not hits:
        return 1
    if not kill:
        print('\n'.join(hits))
        return 0
    return subprocess.run(['kill', f'-{sig}', *[h.split()[0] for h in hits]]).returncode


# --- self-test -----------------------------------------------------------------

def self_test() -> int:
    cases = [
        ({'tool_name': 'Read', 'tool_input': {'file_path': 'context/decomp/game.c'}}, 2),
        ({'tool_name': 'Read', 'tool_input': {'file_path': 'context/decomp/game.c', 'limit': 80}}, 0),
        ({'tool_name': 'Read', 'tool_input': {'file_path': 'ps2kit/plan.py'}}, 0),
        ({'tool_name': 'Bash', 'tool_input': {'command': 'cat context/decomp/game.c'}}, 2),
        ({'tool_name': 'Bash', 'tool_input': {'command': 'grep -m3 foo context/decomp/game.c'}}, 0),
        ({'tool_name': 'Bash', 'tool_input': {'command': 'sleep 120; pgrep -f kit check'}}, 2),
        ({'tool_name': 'Bash', 'tool_input': {'command': 'while true; do sleep 60; pgrep -x claude; done'}}, 2),
        ({'tool_name': 'Bash', 'tool_input': {'command': 'while read l; do echo $l; done < f'}}, 0),
        ({'tool_name': 'Bash', 'tool_input': {'command': 'echo hi'}}, 0),
        ({'tool_name': 'Grep', 'tool_input': {'pattern': 'x'}}, 0),
        ({}, 0), ({'tool_name': 'Bash'}, 0), ({'tool_name': 'Bash', 'tool_input': 'oops'}, 0),
    ]
    bad = 0
    for ev, want in cases:
        rc, msg, _ = decide(ev)
        if rc != want:
            bad += 1
            print(f'FAIL want {want} got {rc}: {ev}')
    rc, out, upd = decide({'tool_name': 'Bash', 'tool_input': {'command': 'kit check -p sim'}})
    if not upd or upd.get('timeout') != 3600000:
        bad += 1
        print('FAIL kit check did not get a long timeout')
    rc, out, upd = decide({'tool_name': 'Bash', 'tool_input': {'command': 'pkill -f record'}})
    if not upd or '--pkill' not in upd.get('command', ''):
        bad += 1
        print(f'FAIL pkill -f was not rewritten: {upd}')
    print('guard self-test: ' + ('ok' if not bad else f'{bad} failure(s)'))
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    if argv[:1] == ['--pgrep']:
        return wrapper(False, argv[1:])
    if argv[:1] == ['--pkill']:
        return wrapper(True, argv[1:])
    if argv[:1] == ['--self-test']:
        return self_test()
    if argv[:1] in (['-h'], ['--help']):
        print(__doc__)
        return 0
    try:
        ev = json.load(sys.stdin)
        if not isinstance(ev, dict):
            return 0
        rc, msg, _ = decide(ev)
    except Exception:
        return 0  # an unexpected payload must never block the tool call
    if rc:
        print(msg, file=sys.stderr)
        return rc
    if msg:
        print(msg)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
