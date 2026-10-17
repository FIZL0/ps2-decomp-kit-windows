"""The task list as a *tool*, not a file an agent reads whole.

A port of a PS2 game is hundreds of small tasks spread over months, and the agent doing them starts from
nothing every session. If the backlog is a file, every session pays for the whole file — and the one that
re-reads it on every turn pays again. So the backlog is a command instead:

    kit plan                   index of plan/TODO.md: every heading and task id, one line each
    kit plan next              the first open task, with its sub-items
    kit plan <ID>              a task's full text: plan/<ID>.md, else its line in TODO.md or DONE.md
    kit plan <name>            a heading's section (case-insensitive substring) in the plan files
    kit plan -f FILE <name>    the same, on any other markdown file
    kit plan brief <ID>        everything a task starts from, in one shot (see `brief`)
    kit plan tick <ID>         done: [x], and the line moves to plan/DONE.md
    kit plan block <ID> WHY    [~] with "BLOCKED: WHY" appended
    kit plan unblock <ID> [NOTE]   reopen: [ ], drop "BLOCKED: …", append NOTE (what unblocked it)
    kit plan move <ID> WHERE   WHERE = top | before ID | after ID | in SECTION (its end); sub-items follow
    kit plan add PREFIX TEXT [@ WHERE]   new "- [ ] **<next free id>** TEXT"; prints the id
    kit plan note <ID> TEXT    append " — TEXT" to the task (user details, priority reasons)
    kit plan newid PREFIX      the next free id, e.g. L -> L7
    kit plan archive           move every [x] line left in TODO.md to DONE.md (after merges, hand edits)

The line format is `- [ ] **ID** text → files… | t: tests | j: journal-dir`, under `###` headings, and the
checkbox is `[ ]` open, `[x]` done, `[~]` blocked.

**Design rule: checkboxes are only ever changed through this tool.** Never hand-edit a `- [ ]` in TODO.md or
DONE.md. Ticking by hand is how a task ends up done in one file and open in another, how a `(after X)`
dependency silently stops being satisfied, and how two parallel agents both pick the same task. `tick` is one
transaction: mark it, mirror the mark into `plan/<ID>.md`, move the block to DONE.md under the same heading.

Everything here is plain text and atomic: a temp file plus `os.replace`, and a file whose content did not
change is not rewritten at all (so mtimes stay meaningful and git stays quiet).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

from ps2kit.config import cfg, ROOT, write_atomic

TTL = 5.0  # seconds a parsed file is trusted before its mtime is checked again
_cache: dict[str, tuple[float, int, list[str]]] = {}  # path -> (checked_at, mtime_ns, lines)

ITEM = re.compile(r'^(\s*)- \[(.)\] \*\*([^\s*]+)')
HEAD = re.compile(r'(#+) ')
# A path-ish token on a task line: `src/main.c`, `crates/sim/src/ps2.rs`, `kit.toml`, `watch/frame.toml`,
# `play/*.rs`. Anything with a dot and a short extension; checked against the repo before it is believed.
FILE_TOKEN = re.compile(r'(?<![\w/.`-])([\w.*-]+(?:/[\w.*-]+)*\.[A-Za-z][\w+]{0,5})')
JOURNAL_MAX = 12000  # chars of the newest journal file pasted into a brief; the rest is a Read away


# --- reading ------------------------------------------------------------------

def lines(path) -> list[str]:
    """A file's lines, cached. The mtime is only re-checked once the TTL is up, so a command that reads the
    same file five times (index, then a section, then a lookup) stats it once."""
    path = str(path)
    now = time.monotonic()
    hit = _cache.get(path)
    if hit and now - hit[0] < TTL:
        return hit[2]
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        return []
    if hit and hit[1] == mtime:
        _cache[path] = (now, mtime, hit[2])
        return hit[2]
    with open(path, encoding='utf-8', errors='replace') as f:
        ls = f.read().splitlines()
    _cache[path] = (now, mtime, ls)
    return ls


def _level(line: str) -> int:
    m = HEAD.match(line)
    return len(m.group(1)) if m else 0


def _item_block(ls: list[str], i: int) -> list[str]:
    """A task line plus every more-indented line under it (its sub-items and continuations)."""
    indent = len(ITEM.match(ls[i]).group(1))
    j = i + 1
    while j < len(ls) and ls[j].strip() and (len(ls[j]) - len(ls[j].lstrip())) > indent:
        j += 1
    return ls[i:j]


def index(path) -> list[str]:
    """Headings and task ids only — the whole backlog in a screenful, whatever its size."""
    out = []
    for line in lines(path):
        if _level(line):
            out.append(line)
        elif m := ITEM.match(line):
            out.append(f'{m.group(1)}[{m.group(2)}] {m.group(3)}')
    return out


def section(path, name: str) -> list[str]:
    """One named part of a markdown file: the first open task (`next`), a task by id, or a heading's section."""
    ls = lines(path)
    if name == 'next':
        for i, line in enumerate(ls):
            m = ITEM.match(line)
            if m and m.group(2) == ' ' and not m.group(1):
                return _item_block(ls, i)
        return []
    for i, line in enumerate(ls):
        m = ITEM.match(line)
        if m and m.group(3).rstrip('.') == name:
            return _item_block(ls, i)
    for i, line in enumerate(ls):
        lv = _level(line)
        if lv and name.lower() in line.lower():
            j = i + 1
            while j < len(ls) and not (0 < _level(ls[j]) <= lv):
                j += 1
            return ls[i:j]
    return []


def _find(ls: list[str], tid: str) -> int:
    for i, line in enumerate(ls):
        m = ITEM.match(line)
        if m and not m.group(1) and m.group(3).rstrip('.') == tid:
            return i
    raise SystemExit(f'kit plan: no task {tid!r} (kit plan for the index)')


# --- writing ------------------------------------------------------------------

def _write(path, ls: list[str]) -> None:
    """Atomic (see `config.write_atomic`), and a no-op when nothing changed."""
    path = str(path)
    text = '\n'.join(ls).rstrip('\n') + '\n'
    try:
        if open(path, encoding='utf-8').read() == text:
            return
    except OSError:
        pass
    write_atomic(path, text)
    _cache.pop(path, None)


def _mark(ls: list[str], i: int, box: str) -> None:
    ls[i] = re.sub(r'^(\s*)- \[.\]', rf'\1- [{box}]', ls[i])


def ensure(todo=None, done=None) -> tuple[str, str]:
    """Paths of TODO.md / DONE.md, created with a heading if a fresh checkout has neither."""
    plan_dir = Path(cfg(False).plan)
    todo = str(todo or plan_dir / 'TODO.md')
    done = str(done or plan_dir / 'DONE.md')
    os.makedirs(os.path.dirname(todo) or '.', exist_ok=True)
    if not os.path.exists(todo):
        _write(todo, ['# TODO — open tasks, in order', '', '### Next', ''])
    if not os.path.exists(done):
        _write(done, ['# DONE — the archive (`kit plan tick <ID>` puts them here)', ''])
    return todo, done


def _child(parent: str, tid: str) -> bool:
    """P17 -> P17a, P11k -> P11k1: the parent's id plus a character of the other kind (so P170 isn't P17's)."""
    rest = tid[len(parent):]
    return tid.startswith(parent) and bool(rest) and rest[0].isdigit() != parent[-1].isdigit()


def _close_parents(ls: list[str], dl: list[str]) -> None:
    """Tick each "(split into …)" parent whose subtasks are all done: some in DONE.md, none open in TODO.md."""
    items = [(m.group(3).rstrip('.'), m.group(2)) for m in map(ITEM.match, ls + dl) if m]
    for i, line in enumerate(ls):
        m = ITEM.match(line)
        if m and not m.group(1) and m.group(2) != 'x' and '(split into' in line:
            kids = [box for t, box in items if _child(m.group(3).rstrip('.'), t)]
            if kids and all(box == 'x' for box in kids):
                _mark(ls, i, 'x')


def archive(todo, done) -> int:
    """Move every top-level [x] task (with its sub-lines) from todo into done, under the same ### heading; a
    "(split into …)" parent goes too once its subtasks are all done, so nobody has to tick an umbrella line.

    Needed because a merge can bring a tick in from a branch without moving the line, and because a human
    sometimes ticks by hand anyway. Running it is always safe.
    """
    ls, dl, sec, moved = list(lines(todo)), list(lines(done)), '', 0
    if not dl:
        dl = ['# DONE — the archive', '']
    _close_parents(ls, dl)
    i = 0
    while i < len(ls):
        if _level(ls[i]):
            sec = ls[i]
        m = ITEM.match(ls[i])
        if not (m and not m.group(1) and m.group(2) == 'x'):
            i += 1
            continue
        block = _item_block(ls, i)
        del ls[i:i + len(block)]
        if i < len(ls) and not ls[i].strip() and i and not ls[i - 1].strip():
            del ls[i]  # don't leave a double blank line behind
        h = sec or '### (no section)'
        if h not in dl:
            dl += ['', h, '']
        j = dl.index(h) + 1
        while j < len(dl) and not _level(dl[j]):
            j += 1
        while j - 1 > 0 and dl[j - 1].strip() == '':
            j -= 1
        dl[j:j] = block
        moved += 1
    if moved:
        _write(done, dl)
        _write(todo, ls)
    return moved


def tick(tid: str, todo=None, done=None, plan_dir=None) -> int:
    """Mark a task done everywhere it is written down, in one go, and archive it."""
    todo, done = ensure(todo, done)
    plan_dir = str(plan_dir or cfg(False).plan)
    ls = list(lines(todo))
    _mark(ls, _find(ls, tid), 'x')
    _write(todo, ls)
    own = os.path.join(plan_dir, f'{tid}.md')
    if os.path.exists(own):
        _write(own, [re.sub(rf'^(\s*)- \[[ ~]\] \*\*{re.escape(tid)}\b', rf'\1- [x] **{tid}', l)
                     for l in lines(own)])
    return archive(todo, done)


def block(tid: str, why: str, todo=None) -> None:
    """[~] plus "BLOCKED: why". The why must say what would unblock it, or the next session just re-reads it."""
    todo, _ = ensure(todo)
    ls = list(lines(todo))
    i = _find(ls, tid)
    _mark(ls, i, '~')
    end = i + len(_item_block(ls, i)) - 1
    ls[end] += f' BLOCKED: {why}'
    _write(todo, ls)


def unblock(tid: str, note: str = '', todo=None) -> None:
    todo, _ = ensure(todo)
    ls = list(lines(todo))
    i = _find(ls, tid)
    _mark(ls, i, ' ')
    end = i + len(_item_block(ls, i)) - 1
    ls[end] = ls[end].split(' BLOCKED: ')[0] + (f' — {note}' if note else '')
    _write(todo, ls)


def _place(ls: list[str], where: str, ref: str | None) -> int:
    """Index to insert a top-level item at: top (first task of the leading section), before/after ID,
    or in SECTION (at its end)."""
    if where == 'top':
        h = next((i for i, l in enumerate(ls) if _level(l) and 'next' in l.lower()), None)
        if h is None:
            h = next((i for i, l in enumerate(ls) if _level(l) == 3), None)
        if h is None:
            h = next((i for i, l in enumerate(ls) if _level(l)), -1)
        i = h + 1
        while i < len(ls) and not ITEM.match(ls[i]) and not _level(ls[i]):
            i += 1
        return i
    if where in ('before', 'after'):
        if not ref:
            raise SystemExit(f'kit plan: {where} needs a task id')
        i = _find(ls, ref)
        return i if where == 'before' else i + len(_item_block(ls, i))
    if where == 'in':
        h = next((i for i, l in enumerate(ls) if _level(l) and (ref or '').lower() in l.lower()), None)
        if h is None:
            raise SystemExit(f'kit plan: no section {ref!r}')
        j = h + 1
        while j < len(ls) and not (0 < _level(ls[j]) <= _level(ls[h])):
            j += 1
        while j - 1 > 0 and ls[j - 1].strip() == '':
            j -= 1
        return j
    raise SystemExit(f'kit plan: where must be top | before ID | after ID | in SECTION, not {where!r}')


def move(tid: str, where: str, ref: str | None = None, todo=None) -> None:
    todo, _ = ensure(todo)
    ls = list(lines(todo))
    i = _find(ls, tid)
    blk = _item_block(ls, i)
    del ls[i:i + len(blk)]
    j = _place(ls, where, ref)
    ls[j:j] = blk
    _write(todo, ls)


def next_id(prefix: str, paths=None) -> str:
    """The next free id with this prefix, counting DONE.md too: ids are permanent and never reused."""
    if paths is None:
        paths = list(ensure())
    n = [int(m.group(1)) for p in paths for l in lines(p)
         if (m := re.match(rf'\s*- \[.\] \*\*{re.escape(prefix)}(\d+)\b', l))]
    return f'{prefix}{max(n, default=0) + 1}'


def add(prefix: str, text: str, where: str = 'top', ref: str | None = None, todo=None, done=None) -> str:
    todo, done = ensure(todo, done)
    tid = next_id(prefix, [todo, done])
    ls = list(lines(todo))
    j = _place(ls, where, ref)
    ls[j:j] = [f'- [ ] **{tid}** {text}']
    _write(todo, ls)
    return tid


def note(tid: str, text: str, todo=None) -> None:
    todo, _ = ensure(todo)
    ls = list(lines(todo))
    i = _find(ls, tid)
    ls[i + len(_item_block(ls, i)) - 1] += f' — {text}'
    _write(todo, ls)


# --- what a session starts from ----------------------------------------------

def next_task(todo=None) -> str | None:
    """The id of the first open task, or None. `kit run` and "continue" both start here."""
    todo, _ = ensure(todo)
    blk = section(todo, 'next')
    return ITEM.match(blk[0]).group(3).rstrip('.') if blk else None


def files_on_line(text: str, root=None) -> list[str]:
    """Repo paths named on a task line, resolved against the repo. Also used by `kit run` in mode `d`
    to keep two running agents off the same files, so the matching lives here, once."""
    root = Path(root or ROOT)
    names = list(dict.fromkeys(FILE_TOKEN.findall(text.split('| t:')[0])))
    tracked = _tracked(root)
    out = []
    for n in names:
        n = n.rstrip('.,;:')
        if '*' in n:  # `play/*.rs` on a line means every file in that directory
            import fnmatch
            out += [t for t in tracked if fnmatch.fnmatch(t, f'*{n}')]
            continue
        hits = [t for t in tracked if t == n or t.endswith('/' + n)]
        # A path that does not exist yet is still the task's file: half the backlog creates its files, and
        # `brief` wants to say "missing — this task creates it" rather than quietly listing nothing. Only
        # names that look like paths, though, or prose about `fpu.rs` becomes a file the runner reserves.
        out += hits or ([n] if '/' in n or (root / n).exists() else [])
    return list(dict.fromkeys(out))


def _tracked(root) -> list[str]:
    """Every tracked path, from git; a filesystem walk when this isn't a git checkout (tests, a fresh copy)."""
    try:
        r = subprocess.run(['git', 'ls-files'], cwd=str(root), capture_output=True, text=True, timeout=20)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.split()
    except (OSError, subprocess.SubprocessError):
        pass
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ('.git', 'context', 'target', '__pycache__')]
        for f in filenames:
            out.append(os.path.relpath(os.path.join(dirpath, f), root))
    return out


def brief(tid: str, root=None, plan_dir=None, journal=None) -> list[str]:
    """Everything a task starts from, in one command.

    This is the highest-value thing in the module. A fresh session otherwise spends its first five or six
    turns finding the task line, looking for a `plan/<ID>.md`, hunting the journal directory, opening the
    files and running `git log` on them — all of it predictable, all of it paid for again on every turn after.
    `brief` assembles it once:

      * the task's own line (and sub-items) from TODO.md or DONE.md,
      * `plan/<ID>.md` in full, if the task has a long form,
      * its journal directory: the file list, and the newest entry pasted in (capped at JOURNAL_MAX chars,
        because the newest entry is where the last session said whose move it is),
      * the files named on the line: where they actually are, how long they are, and their last 3 commits —
        so the session knows whether someone changed them yesterday before it reads a single one.

    `kit run` pastes this into the agent's first turn.
    """
    root = Path(root or ROOT)
    plan_dir = Path(plan_dir or (root / 'plan' if root != ROOT else cfg(False).plan))
    journal = Path(journal or (root / 'research/journal' if root != ROOT else cfg(False).journal))
    paths = [plan_dir / 'TODO.md', plan_dir / 'DONE.md']
    line = next((o for p in paths if (o := section(p, tid))), [])
    if not line:
        raise SystemExit(f'kit plan: no task {tid!r} in {plan_dir}/TODO.md or DONE.md')
    text = '\n'.join(line)
    out = [f'## Task {tid} ({plan_dir.name}/TODO.md)', *line]
    own = plan_dir / f'{tid}.md'
    if own.exists():
        out += ['', f'## {plan_dir.name}/{tid}.md', *lines(own)]
        text += '\n' + '\n'.join(lines(own))

    # journals: the ones the line points at (`j: <dir>`) and any whose name carries this id
    named = re.findall(r'\bj: ([\w.-]+)', text)
    try:
        dirs = sorted(d for d in os.listdir(journal) if (journal / d).is_dir())
    except OSError:
        dirs = []
    own_j = [d for d in dirs if re.search(rf'-{re.escape(tid.lower())}(-|$)', d.lower())]
    for j in dict.fromkeys(named + own_j):
        d = journal / j
        if not d.is_dir():
            continue
        files = sorted(f for f in os.listdir(d) if f.endswith('.md'))
        rel = os.path.relpath(d, root)
        out += ['', f'## Journal {rel}/: {" · ".join(files)}']
        if files and j in own_j:  # journals the line only points at are listed, not pasted
            body = (d / files[-1]).read_text(encoding='utf-8', errors='replace')
            cut = len(body) > JOURNAL_MAX
            out += [f'### {files[-1]}' + (f' (first {JOURNAL_MAX} chars)' if cut else ''),
                    body[:JOURNAL_MAX].rstrip()]

    where = [p for p in files_on_line(text, root) if p != f'{plan_dir.name}/{tid}.md']
    if where:
        out += ['', '## Files on the line, and their last commits']
        for t in where:
            log = subprocess.run(['git', 'log', '-3', '--format=%h %ad %s', '--date=short', '--', t],
                                 cwd=str(root), capture_output=True, text=True).stdout.strip().splitlines()
            try:
                n_lines = sum(1 for _ in open(root / t, encoding='utf-8', errors='replace'))
                size = f'{n_lines} lines'
            except OSError:
                size = 'missing — this task creates it'
            out += [f'- `{t}` ({size})', *[f'  - {l[:120]}' for l in log]]
    return out


# --- the command ---------------------------------------------------------------

HELP = __doc__ + """
Ids are permanent: `tick` moves a line to DONE.md but never renumbers, and `newid`/`add` count DONE.md too,
so a finished id is never handed out again. That matters because journal directories, commit messages and
`(after X)` dependencies all name tasks by id.
"""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ('-h', '--help', 'help'):
        print(HELP)
        return 0
    c = cfg(False)
    plan_dir = Path(c.plan)
    todo, done = ensure()

    def say(msg):
        print(msg)
        return 0

    if argv[:1] == ['tick'] and len(argv) == 2:
        n = tick(argv[1])
        return say(f'ticked {argv[1]}' + (f', moved {n} line(s) to {plan_dir.name}/DONE.md' if n else ''))
    if argv[:1] == ['block'] and len(argv) > 2:
        block(argv[1], ' '.join(argv[2:]))
        return say(f'blocked {argv[1]}')
    if argv[:1] == ['unblock'] and len(argv) > 1:
        unblock(argv[1], ' '.join(argv[2:]))
        return say(f'unblocked {argv[1]}')
    if argv[:1] == ['move'] and len(argv) > 2:
        move(argv[1], argv[2], ' '.join(argv[3:]) or None)
        return say(f'moved {argv[1]} {" ".join(argv[2:])}')
    if argv[:1] == ['add'] and len(argv) > 2:
        text, _, where = ' '.join(argv[2:]).partition(' @ ')
        w = where.split(maxsplit=1) or ['top']
        return say(f'added {add(argv[1], text, w[0], (w[1:] or [None])[0])}')
    if argv[:1] == ['note'] and len(argv) > 2:
        note(argv[1], ' '.join(argv[2:]))
        return say(f'noted {argv[1]}')
    if argv[:1] == ['newid'] and len(argv) == 2:
        return say(next_id(argv[1]))
    if argv[:1] == ['brief'] and len(argv) == 2:
        return say('\n'.join(brief(argv[1])))
    if argv == ['archive']:
        return say(f'archived {archive(todo, done)}')

    # a lookup: an id, a section name, or `next`
    paths = [todo, plan_dir / 'REFERENCE.md', plan_dir / 'HUMAN.md', done,
             ROOT / 'AGENT.md', ROOT / 'docs' / 'WORKFLOW.md']
    paths = [str(p) for p in paths if os.path.exists(p)]
    if argv[:1] == ['-f']:
        if len(argv) < 3:
            print('kit plan -f FILE <name>', file=sys.stderr)
            return 2
        paths, argv = [argv[1]], argv[2:]
    task_file = plan_dir / f'{argv[0]}.md' if argv else None
    if len(paths) > 1 and task_file and task_file.exists():
        out = lines(task_file)
    elif not argv:
        out = index(todo)
    else:
        out = next((o for p in paths if (o := section(p, argv[0]))), [])
    if not out:
        print(f'kit plan: nothing named {argv[0]!r} — run `kit plan` for the index', file=sys.stderr)
        return 1
    print('\n'.join(out))
    return 0


if __name__ == '__main__':
    sys.exit(main())
