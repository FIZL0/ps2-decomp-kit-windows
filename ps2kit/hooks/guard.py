#!/usr/bin/env python3
"""PreToolUse hook: keep an agent from spending its whole session in one tool call.

Driven entirely by `kit.toml` (`cfg()`), so it is game-agnostic; it knows nothing but "the private directory
holds things that are far too big to read whole".

What it does, and why each one earned its place:

**A whole-file read of a decompilation dump.** `kit ghidra` writes one C file per program, and for a PS2
game that is 5–20 MB. A `Read` without `offset`/`limit`, or a `cat`/`grep` without a cap, puts all of it in
the conversation — which is then re-read on every following turn, so the session is over. Blocked, with the
cheap ways to get the same answer: append `| head -<n>` to cap any command's output, read a binary header
with a tool that carries its own cap (`xxd -l`, `od -N`, `dd count=`, `head -c`), or, for code, `kit fn
<addr>` for one function, `kit xref <addr>` for its callers and callees, `Read` with `offset`+`limit`, a
capped grep.

Two things that merely *look* like that read are not: an image, which `Read` renders rather than pastes —
`kit shot` writes PNGs into the same directory, and looking at one is the point of taking it — and a
command that names a big file without reading it (`cp`, `mv`, `ls`, `magick … -resize`), which puts
nothing in the conversation at all. Both used to be refused, which walled off the task "look at the
screenshot" completely.

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
# `cat x`, `grep pat x`, `head -c big x`… with no cap on the output. Anything behind a pipe caps whatever
# fed it, so this one is judged over the whole command line.
CAPPED = re.compile(KIT + r'\s+(fn|xref|dis|data)\b|\|\s*(head|tail|wc|sed\b)|'
                    r'\bgrep\b[^|]*\s-[a-zA-Z]*[mcl]|\bwc\b|\bsed\s+-n|\bhead\s+-|\btail\s+-')
# The byte-slice tools carry their own cap, which is how you read a binary header: `xxd -l 512 f`,
# `od -N 256 f`, `dd … count=256`, `head -c 512 f`. The cap has to belong to the command that opens the
# file, so this one is matched inside that command's own pipeline segment only (`_segment`): a capped
# `xxd` in another segment or another statement caps its own output, not this read. Every alternative
# needs the flag *and* a digit, so a bare `xxd context/iso/BIG.BIN` is still uncapped.
SELF_CAPPED = re.compile(r'\bxxd\b[^|;&\n]*?\s-(?:l\s*\d|-len(?:gth)?[= ]\s*\d)|'
                         r'\bod\b[^|;&\n]*?\s-(?:N\s*\d|-read-bytes[= ]\s*\d)|'
                         r'\bdd\b[^|;&\n]*?\scount=\d|'
                         r'\bhead\b[^|;&\n]*?\s-(?:c\s*\d|-bytes[= ]\s*\d)')
# An image is not a dump: `kit shot` writes PNGs into the private directory, and looking at one is the
# whole point of taking it. `Read` renders an image instead of pasting its bytes, so the cost this guard
# exists to prevent does not arise — a screenshot is read whole or not at all. (A *shell* command that
# streams a PNG to stdout is still refused: that one does paste the bytes. See `_is_dump(image_ok=…)`.)
IMAGES = ('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.tga', '.ppm')
# Commands that take a path and put none of its contents in the conversation: moving a screenshot out of
# the way, measuring a dump, resizing a PNG. The guard is about output volume, so gating these bought
# nothing and walled off `cp context/shots/a.png b.png`, `magick … -resize` and even `ls` on a path that
# merely *looked* big. Matched against the token's own pipeline segment, and deliberately a list rather
# than "anything that is not a reader": an unknown command still has to carry a cap.
# than "anything that is not a reader": an unknown command still has to carry a cap, and the ones that
# *can* stream a file to stdout (`tar cf -`, `gzip -c`, `7z e -so`, `diff`, `cmp`) are left out of it.
NO_READ = re.compile(r'^\s*(?:sudo\s+)?(cp|mv|rm|ln|mkdir|rmdir|touch|chmod|chown|ls|stat|file|du|df|'
                     r'basename|dirname|realpath|readlink|sha\d+sum|md5sum|magick|convert|identify|'
                     r'ffmpeg|optipng|pngcrush|rsync|install|open|xdg-open)\b')


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


def _is_dump(path: str, image_ok: bool = False) -> str | None:
    """Why reading all of `path` is a bad idea, or None.

    `image_ok` is for `Read`, which renders an image rather than pasting its bytes: a screenshot out of
    `kit shot` is then not a dump at all, whatever it weighs. A shell command naming the same PNG gets
    `image_ok=False`, because `cat` on it really would paste the bytes.
    """
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
    if image_ok and full.suffix.lower() in IMAGES:
        return None
    priv = Path(private).name
    in_private = f'{os.sep}{priv}{os.sep}' in s or f'/{priv}/' in path
    if in_private:
        try:
            if full.stat().st_size > BIG:
                kb = f'{full.stat().st_size // 1024} KB'
                return (f'{kb} of image data' if full.suffix.lower() in IMAGES
                        else f'{kb} of generated data')
        except OSError:
            return None
    return None


HOW = ('It is not off limits — cap the output and the same command is allowed. Appending `| head -40` '
       '(any n) to a Bash command caps it and satisfies this guard, whatever the file is; for a binary '
       'header, `xxd -l 512`, `od -N 256`, `dd count=256` and `head -c 512` carry their own cap. For code, '
       '`kit fn <addr>` gives one function, `kit xref <addr>` its callers and callees (`--similar` for '
       'look-alikes), `Read` takes offset+limit, and a grep can be capped (`grep -m5`, `grep -c`). Reading '
       'it whole puts megabytes in the conversation, which is then re-read on every turn for the rest of '
       'the session.')


def _segment(cmd: str, pos: int) -> str:
    """The one pipeline segment of `cmd` holding offset `pos`: where a tool's own cap flag has to sit."""
    seps = '|;&\n'
    start = max((cmd.rfind(c, 0, pos) for c in seps), default=-1) + 1
    ends = [i for i in (cmd.find(c, pos) for c in seps) if i >= 0]
    return cmd[start:min(ends)] if ends else cmd[start:]


def _cleanroom() -> bool:
    """A clean-room port phase is on in this checkout: `kit plan brief` wrote `.cleanroom` (docs/WORKFLOW.md)."""
    return (Path(os.environ.get('CLAUDE_PROJECT_DIR') or os.getcwd()) / '.cleanroom').exists()


def decide(ev: dict) -> tuple[int, str, dict | None]:
    """(exit code, message for the agent, updated tool input) for one hook payload."""
    tool = ev.get('tool_name') or ''
    i = ev.get('tool_input') or {}
    if not isinstance(i, dict):
        return 0, '', None

    # Clean-room port phase: the session ports from the task's SPEC.md and the tests, so anything that shows
    # the decompilation (dumps, Ghidra, `kit fn/xref/dis`) is refused until `kit done` clears the marker.
    base = _dump_paths()[1]
    locked = rf'{re.escape(base)}/(decomp|ghidra)|\bkit\s+(fn|xref|dis|ghidra)\b|\bghidra|objdump'
    if _cleanroom() and re.search(locked, f"{i.get('file_path', '')} {i.get('command', '')}"):
        return 2, ('Clean-room port phase: the decompilation is locked this session. Port from the SPEC.md in '
                   'the brief and prove it with the recordings and the oracle; anything the spec does not say goes '
                   'under "## Open questions" in it, then `kit done <ID> "<msg>" --spec`.'), None

    if tool == 'Read':
        why = _is_dump(str(i.get('file_path', '')), image_ok=True)
        if why and not i.get('limit') and not i.get('offset'):
            return 2, f'That file is {why}. {HOW}', None
        return 0, '', None

    if tool != 'Bash':
        return 0, '', None
    cmd = str(i.get('command', '') or '')
    if not cmd:
        return 0, '', None

    # a dump named in a shell command with nothing capping the output
    for m in re.finditer(r'[\w./@~-]+', cmd):
        tok = m.group()
        if not _is_dump(tok):
            continue
        seg = _segment(cmd, m.start())
        if CAPPED.search(cmd) or SELF_CAPPED.search(seg) or NO_READ.match(seg):
            continue
        if Path(tok).suffix.lower() in IMAGES:
            return 2, (f'`{tok}` is an image, and piping it to stdout would paste its bytes into the '
                       'conversation. `Read` it instead — the guard allows reading an image whole, which '
                       'is the point of taking it — and `cp`, `mv`, `ls`, `file` and `magick` on it are '
                       'not gated either.'), None
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
    # Two real oversized files under a `context/` of their own: the size rule stats what it is given, so
    # the screenshot cases need bytes on disk rather than a path that merely looks right.
    import tempfile
    tmp = tempfile.TemporaryDirectory(prefix='kit-guard-')
    priv = Path(tmp.name) / 'context'
    (priv / 'shots').mkdir(parents=True)
    (priv / 'states').mkdir(parents=True)
    BIG_PNG = priv / 'shots' / 'title.png'
    BIG_BIN = priv / 'states' / 'ram.bin'
    BIG_PNG.write_bytes(b'\x89PNG\r\n\x1a\n' + b'\0' * BIG)
    BIG_BIN.write_bytes(b'\0' * (BIG + 1))
    cases = [
        ({'tool_name': 'Read', 'tool_input': {'file_path': 'context/decomp/game.c'}}, 2),
        ({'tool_name': 'Read', 'tool_input': {'file_path': 'context/decomp/game.c', 'limit': 80}}, 0),
        ({'tool_name': 'Read', 'tool_input': {'file_path': 'ps2kit/plan.py'}}, 0),
        ({'tool_name': 'Bash', 'tool_input': {'command': 'cat context/decomp/game.c'}}, 2),
        ({'tool_name': 'Bash', 'tool_input': {'command': 'grep -m3 foo context/decomp/game.c'}}, 0),
        ({'tool_name': 'Bash', 'tool_input': {'command': 'cat context/decomp/game.c | head -40'}}, 0),
        # the byte-slice tools: the flag *is* the cap, so each tool's bare form is still refused
        ({'tool_name': 'Bash', 'tool_input': {'command': 'xxd -l 512 context/decomp/game.c'}}, 0),
        ({'tool_name': 'Bash', 'tool_input': {'command': 'xxd -s 0x100 -l512 context/decomp/game.c'}}, 0),
        ({'tool_name': 'Bash', 'tool_input': {'command': 'xxd --length=512 context/decomp/game.c'}}, 0),
        ({'tool_name': 'Bash', 'tool_input': {'command': 'xxd context/decomp/game.c'}}, 2),
        ({'tool_name': 'Bash', 'tool_input': {'command': 'xxd -g 1 context/decomp/game.c'}}, 2),
        ({'tool_name': 'Bash', 'tool_input': {'command': 'xxd -l context/decomp/game.c'}}, 2),
        ({'tool_name': 'Bash', 'tool_input': {'command': 'od -N 256 -t x1z context/decomp/game.c'}}, 0),
        ({'tool_name': 'Bash', 'tool_input': {'command': 'od -A x -N256 context/decomp/game.c'}}, 0),
        ({'tool_name': 'Bash', 'tool_input': {'command': 'od --read-bytes=256 context/decomp/game.c'}}, 0),
        ({'tool_name': 'Bash', 'tool_input': {'command': 'od -A x -t x1 context/decomp/game.c'}}, 2),
        ({'tool_name': 'Bash',
          'tool_input': {'command': 'dd if=context/decomp/game.c bs=1 count=256 status=none'}}, 0),
        ({'tool_name': 'Bash',
          'tool_input': {'command': 'dd if=context/decomp/game.c of=/tmp/copy.bin status=none'}}, 2),
        ({'tool_name': 'Bash', 'tool_input': {'command': 'head -c 512 context/decomp/game.c'}}, 0),
        ({'tool_name': 'Bash', 'tool_input': {'command': 'head --bytes=512 context/decomp/game.c'}}, 0),
        # a cap belonging to some other command in the line cannot bless this read
        ({'tool_name': 'Bash',
          'tool_input': {'command': 'xxd -l 16 small.bin; xxd context/decomp/game.c'}}, 2),
        ({'tool_name': 'Bash',
          'tool_input': {'command': 'xxd -l 16 small.bin && cat context/decomp/game.c'}}, 2),
        ({'tool_name': 'Bash',
          'tool_input': {'command': 'xxd -l 16 small.bin | xxd context/decomp/game.c'}}, 2),
        # a screenshot is read whole on purpose; only streaming its bytes to stdout is refused
        ({'tool_name': 'Read', 'tool_input': {'file_path': str(BIG_PNG)}}, 0),
        ({'tool_name': 'Bash', 'tool_input': {'command': f'cp {BIG_PNG} /tmp/a.png'}}, 0),
        ({'tool_name': 'Bash', 'tool_input': {'command': f'magick {BIG_PNG} -resize 50% /tmp/s.png'}}, 0),
        ({'tool_name': 'Bash', 'tool_input': {'command': f'ls -l {BIG_PNG}'}}, 0),
        ({'tool_name': 'Bash', 'tool_input': {'command': f'cat {BIG_PNG}'}}, 2),
        ({'tool_name': 'Bash', 'tool_input': {'command': f'base64 {BIG_PNG}'}}, 2),
        # a non-image dump keeps the old rule, whoever names it
        ({'tool_name': 'Bash', 'tool_input': {'command': f'cp {BIG_BIN} /tmp/ram.bin'}}, 0),
        ({'tool_name': 'Bash', 'tool_input': {'command': f'cat {BIG_BIN}'}}, 2),
        ({'tool_name': 'Read', 'tool_input': {'file_path': str(BIG_BIN)}}, 2),
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
    # the clean-room lock: on only while `.cleanroom` exists in the project directory
    old = os.environ.get('CLAUDE_PROJECT_DIR')
    os.environ['CLAUDE_PROJECT_DIR'] = tmp.name
    (Path(tmp.name) / '.cleanroom').write_text('T1\n')
    for ev, want in [({'tool_name': 'Bash', 'tool_input': {'command': 'kit fn 0x100000'}}, 2),
                     ({'tool_name': 'Read', 'tool_input': {'file_path': 'context/decomp/game.c', 'limit': 9}}, 2),
                     ({'tool_name': 'Bash', 'tool_input': {'command': 'kit check -p sim'}}, 0)]:
        if decide(ev)[0] != want:
            bad += 1
            print(f'FAIL clean-room want {want}: {ev}')
    (Path(tmp.name) / '.cleanroom').unlink()
    if decide({'tool_name': 'Bash', 'tool_input': {'command': 'kit fn 0x100000'}})[0] != 0:
        bad += 1
        print('FAIL clean-room lock without .cleanroom')
    if old is None:
        del os.environ['CLAUDE_PROJECT_DIR']
    else:
        os.environ['CLAUDE_PROJECT_DIR'] = old
    tmp.cleanup()
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
