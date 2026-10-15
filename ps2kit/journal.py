"""The research journal: where a port keeps what it learned, and where it admits what it did not prove.

Decompiling a game is mostly findings, not code: what a function does, which RAM word holds what, why the
port's numbers differ in the last bit, what a capture showed. None of that survives in a diff, and none of it
is worth re-deriving. So every task writes one directory here:

    research/journal/YYYY-MM-DD-<id>-<slug>/
        README.md            what the task was and how it came out
        1-<topic>-PART.md    an iteration; the suffix says whose move it is
        2-<topic>-FINAL.md

    STATE:  FINAL  done, nothing waiting
            PART   an agent continues from here
            READY  needs the human (a recording, a device, a design decision)

The newest numbered file's state is the answer to "whose move is it" — which is the one question an
unattended run has to answer before it can pick up where the last session stopped.

Every stub carries a mandatory **## Not verified / not 1:1** section. In the project this kit comes from that
single habit is what keeps a long port honest: a proof that was skipped is written down instead of silently
missing, so the next session can see the hole instead of trusting the code. "none" is a valid answer; an
empty section is not.

No game data in here. The journal is in git; dumps, captures, screenshots and RAM images go under
`context/` (`cfg().ctx(...)`), which is not. `kit journal` warns when it finds a binary or an image under
the journal tree, because that is how a repo ends up carrying copyrighted game content.

    kit journal new <ID> <slug>    make the directory and the stubs
    kit journal list [<ID>]        entries, newest last; with an ID, just that task's
    kit journal state <ID>         whose move is it, and which file says so
    kit journal finish <path|ID>   rename -PART to -FINAL
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import sys
from pathlib import Path

from ps2kit.config import cfg

STATES = ('FINAL', 'PART', 'READY')
WHOSE = {'FINAL': 'done — nothing waiting on this entry',
         'PART': 'an agent continues: read the file, carry on, write the next one',
         'READY': 'the human: it needs something only they can do (plan/HUMAN.md)'}
# Files that must never be committed in the journal. Captures, dumps and screenshots belong in context/.
BINARY = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.tif', '.tiff', '.mp4', '.mkv', '.webm', '.wav',
          '.bin', '.raw', '.iso', '.elf', '.p2s', '.p2m2', '.csv.gz', '.zip', '.gz', '.xz', '.dump', '.sav'}

STUB = """# {num} — {topic} ({state})

Task {tid}. {date}.

## What I did

-

## What the original does

(The decompiled behaviour, in words. Addresses only as `kit fn <addr>` recipes, never as names in code.)

## How it was proved

(Which of: function oracle over N generated inputs · a PINE recording compared frame by frame · a save-state
capture · a bit-exact replay fixture · looked right beside a screenshot. Say N, and name the file.)

## Not verified / not 1:1

Everything this task left unproven or inexact goes here, each with its reason — or the single word `none`.
A reader must be able to tell from this section alone what they may not trust yet.

-

## Next

(If this file is -PART: exactly what the next session should do first. If -READY: what the human must do.)
"""

README = """# {tid} — {title}

{date}. Task: `kit plan {tid}`.

One paragraph: what this task was, and how it came out. Details in the numbered files; the newest one's
suffix says whose move it is ({states}).
"""


def _journal() -> Path:
    d = Path(cfg(False).journal)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slugify(s: str) -> str:
    return re.sub(r'-+', '-', re.sub(r'[^a-z0-9]+', '-', s.lower())).strip('-')


def entries(tid: str | None = None) -> list[Path]:
    """Journal directories, oldest first (the names start with the date, so a sort is chronological)."""
    j = _journal()
    out = sorted(d for d in j.iterdir() if d.is_dir()) if j.exists() else []
    if tid:
        out = [d for d in out if re.search(rf'-{re.escape(tid.lower())}(-|$)', d.name.lower())]
    return out


def iterations(d: Path) -> list[Path]:
    """The numbered markdown files of one entry, in order (1-, 2-, 10- sort numerically, not as text)."""
    files = [f for f in d.iterdir() if f.suffix == '.md' and re.match(r'\d+-', f.name)]
    return sorted(files, key=lambda f: (int(re.match(r'(\d+)-', f.name).group(1)), f.name))


def state_of(path: Path) -> str:
    m = re.search(r'-(FINAL|PART|READY)\.md$', path.name)
    return m.group(1) if m else '?'


def new(tid: str, slug: str, topic: str | None = None) -> Path:
    """Make today's directory for a task and the first stub. Re-running it adds the next iteration."""
    j = _journal()
    slug = _slugify(slug)
    existing = [d for d in entries(tid) if d.name.endswith(slug)]
    d = existing[-1] if existing else j / f'{_dt.date.today():%Y-%m-%d}-{tid.lower()}-{slug}'
    d.mkdir(parents=True, exist_ok=True)
    readme = d / 'README.md'
    if not readme.exists():
        readme.write_text(README.format(tid=tid, title=slug.replace('-', ' '),
                                        date=f'{_dt.date.today():%Y-%m-%d}', states=' · '.join(STATES)),
                          encoding='utf-8')
    num = len(iterations(d)) + 1
    topic = _slugify(topic or slug)
    f = d / f'{num}-{topic}-PART.md'
    if not f.exists():
        f.write_text(STUB.format(num=num, topic=topic.replace('-', ' '), state='PART', tid=tid,
                                 date=f'{_dt.date.today():%Y-%m-%d}'), encoding='utf-8')
    return f


def finish(path) -> Path:
    """-PART (or -READY) becomes -FINAL: the entry is closed and nothing is waiting on it."""
    p = Path(path)
    if not p.exists():  # also accept a task id, for the common case of one open entry
        found = [f for d in entries(str(path)) for f in iterations(d) if state_of(f) != 'FINAL']
        if not found:
            raise SystemExit(f'kit journal finish: no open entry for {path!r}')
        p = found[-1]
    if state_of(p) == 'FINAL':
        return p
    new_p = p.with_name(re.sub(r'-(PART|READY)\.md$', '-FINAL.md', p.name))
    body = p.read_text(encoding='utf-8', errors='replace')
    body = re.sub(r'^(#\s*\d+\s*—.*?)\((PART|READY)\)', r'\1(FINAL)', body, count=1, flags=re.M)
    p.write_text(body, encoding='utf-8')
    p.rename(new_p)
    return new_p


def police() -> list[str]:
    """Complaints about the journal tree: game data where it must not be. One line each."""
    bad = []
    j = _journal()
    for dirpath, dirnames, filenames in os.walk(j):
        dirnames[:] = [d for d in dirnames if d != '.git']
        for f in filenames:
            p = Path(dirpath) / f
            ext = ''.join(p.suffixes[-2:]).lower()
            if p.suffix.lower() in BINARY or ext in BINARY:
                bad.append(f'{p.relative_to(j.parent.parent)} — captures, dumps and screenshots go under '
                           f'{os.path.relpath(cfg(False).ctx("."), j.parent.parent)}/, never in the journal')
                continue
            if p.suffix.lower() == '.md':
                continue
            try:
                if b'\0' in p.read_bytes()[:4096]:
                    bad.append(f'{p.relative_to(j.parent.parent)} — binary file in the journal')
            except OSError:
                pass
    return bad


def cmd_journal(argv: list[str]) -> int:
    from ps2kit.cli import parser  # imported here, not at module level: nothing in the kit imports cli
    ap = parser('journal', __doc__)
    ap.add_argument('action', nargs='?', default='list', choices=['new', 'list', 'state', 'finish'])
    ap.add_argument('rest', nargs='*', metavar='ARGS',
                    help='new <ID> <slug> [topic] · list [ID] · state <ID> · finish <path|ID>')
    a = ap.parse_args(argv)
    rest = a.rest

    if a.action == 'new':
        if len(rest) < 2:
            print('kit journal new <ID> <slug> [topic]', file=sys.stderr)
            return 2
        f = new(rest[0], rest[1], rest[2] if len(rest) > 2 else None)
        print(f'{f}\n'
              f'Fill in "Not verified / not 1:1" as you go, not at the end, and rename it -FINAL when the task\n'
              f'lands (`kit journal finish {f}`), -READY if it needs the human.')
    elif a.action == 'finish':
        if not rest:
            print('kit journal finish <path|ID>', file=sys.stderr)
            return 2
        print(f'{finish(rest[0])}')
    elif a.action == 'state':
        if not rest:
            print('kit journal state <ID>', file=sys.stderr)
            return 2
        ds = entries(rest[0])
        if not ds:
            print(f'no journal entry for {rest[0]} yet (kit journal new {rest[0]} <slug>)')
            return 1
        for d in ds:
            its = iterations(d)
            if not its:
                print(f'{d.name}: no numbered files yet — whose move: an agent (write 1-…-PART.md)')
                continue
            s = state_of(its[-1])
            print(f'{d.name}: {its[-1].name} -> {s} — whose move: {WHOSE.get(s, "unclear")}')
    else:
        ds = entries(rest[0] if rest else None)
        if not ds:
            print('no journal entries yet' + (f' for {rest[0]}' if rest else ''))
        for d in ds:
            its = iterations(d)
            tail = f'{its[-1].name} [{state_of(its[-1])}]' if its else 'no numbered files'
            print(f'{d.name:<52} {len(its)} file(s)  newest: {tail}')

    for p in police():
        print(f'! {p}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(cmd_journal(sys.argv[1:]))
