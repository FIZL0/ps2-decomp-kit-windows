"""Unattended runs: keep N agent sessions working through the task list, for as long as the work lasts.

A port is months of small, provable steps, and most of them do not need a human in the loop — they need the
backlog, the emulator, the tests and a commit. `kit run` is the loop that does that: it picks the next task
nobody is on, opens a git worktree and a tmux pane for it, starts an agent session with the task's brief
already pasted into the first turn, waits for it to finish, merges the branch, and starts the next one.

    kit run                 3 agents, mode d (different files), each in its own terminal
    kit run 5 --mode p      5 agents, strictly in plan/TODO.md order, files may overlap
    kit run 1               one at a time (the sequential run; also what you get without a multiplexer)
    kit run once            one task, then stop
    kit run --force         keep starting tasks past the weekly usage cap

What it has to get right, and why each of these is here rather than left to the agent:

**Task picking** goes down `plan/TODO.md` in order and skips: blocked tasks (`[~]`), split parents (a task
whose own subtasks are open or blocked), tasks whose `(after X)` dependency is still open, the `### Stretch` section,
anything already tried this run, and anything whose branch is finished and waiting to be merged. Siblings
(`L3a`, `L3b` — same prefix+number) never run at once in any mode, because they are the same code by
definition. Mode `d` additionally keeps two running tasks off the same files, which are named on each task's
line; mode `p` ignores that and just follows priority order.

**One worktree per slot**, kept between tasks so the build directory stays warm: the first build in a fresh
worktree is the slowest thing in the whole run. `context/` and the disc image are symlinked in, never copied.

**One emulator per slot** (`KIT_PCSX2=N`): every kit tool follows that variable, so two agents never load a
save state into each other's PCSX2.

**An idle stop** (a Stop hook, `--stop-hook` below). A finished agent sits at its prompt forever otherwise,
and the loop cannot start the next task. The hook ends the session once the transcript has been quiet for
`--idle` seconds — and typing into the pane resets that, so you can take a session over by hand.

**A usage limit is not a failed task.** A session that only hit the limit did no work: its transcript is
discarded, the task goes back in the pool, and the slot sleeps until the reset. Counting it as "tried" would
quietly drop a task from the night.

**A stall watchdog**, because the one thing worse than a crashed session is a live one that is not moving:
no transcript activity for `--stall` seconds at a prompt (or past the Bash timeout with a tool running) is
logged and belled, with the pane to attach to.

**A terminal per slot**, so you can watch one and type into it. Which one depends on the machine, and the
loop only needs three things from it (open a session, is it still alive, what is its pid), so each is a small
`Mux`: tmux panes in the window "agents" on POSIX; on Windows a Windows Terminal tab (`wt.exe new-tab`) when
wt is installed, else one new console window per slot. With none of them — or `kit run 1`, `once`,
`--no-tmux` — it degrades to a single sequential session in this checkout and says so.

The Windows tabs go through a shim (`--slot-shim`), because `wt.exe` only *asks* the running terminal to open
a tab: it returns immediately, so its exit code and pid say nothing about the session, and the tab inherits
the terminal's environment rather than ours. The shim is this module; it starts the agent with the slot's
environment, publishes its pid in a marker file and removes the marker when the session ends, which is what
the loop polls.

Internal modes (used by the sessions it starts, not by you):
    --stop-hook            Stop hook: arm the idle timer for the session that is stopping
    --stop-waiter P T N S  the detached timer itself
    --slot-shim JOB        run one slot's session (Windows tabs), publishing its pid
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import shlex
import shutil
import subprocess as sp
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # run directly (the Stop hook does)

from ps2kit.config import cfg, ROOT
from ps2kit import platform_ as plat

SLOTS_DIR = ROOT.parent / f'{ROOT.name}-slots'
TASK = re.compile(r'^- \[ \] \*\*([^*\s]+)\*\*(.*)')
ANY_TASK = re.compile(r'^- \[.\] \*\*([^*\s]+)\*\*')
WEEKLY_MAX = 90  # % of the account's 7-day usage; at or past it, no new task starts unless --force

PROMPT = """Unattended run: nobody will answer questions, so never ask one — decide, write down why, and go on.

Do task {tid} and nothing else. You are in a git worktree on branch task/{tid}: commit here only, never touch
the main checkout and never merge or rebase — the runner merges your branch when your session ends, and
a conflict it cannot join mechanically goes to a session of its own that reads and finishes it, so leave
conflicts alone too. `kit done` pushes `task/{tid}` for you; the runner pushes the merge once it has
passed the tests.
{others}
You have your own emulator copy: KIT_PCSX2={n} is set, so `kit pcsx2 start`, `kit pine`, `kit pad` and
`kit shot` all talk to copy {n} and nobody else's. Run anything that drives the game in more than one step
as a single job under `kit pcsx2 with -- <cmd>` so it holds the emulator for the whole job; keep each one
short. Close your copy when you are done with it: `kit pcsx2 stop` (never kill the process by name — the
other slots' copies are running too).

Rules: AGENT.md and docs/. Tests only through `kit check` (one line when they pass; it says plainly whether
the build broke or a test failed). Read decompilation with `kit fn` / `kit xref`, never whole files.

Write the task's journal entry as you go (`kit journal new {tid} <slug>`), and under **Not verified / not
1:1** list everything you could not prove against the original or could not match exactly, each with its
reason — or `none`. A skipped proof that is written down is useful; a skipped proof that is silent is a bug
nobody will find.

Finish with `kit done {tid} "<commit message>"`. If you are stuck (a tool hangs, the same fix fails twice,
it needs the human, ~20 minutes with no progress): commit what is solid and `kit done {tid} "<msg>"
--blocked "<why + what would unblock it>"`, then stop. A usage limit is not stuck: commit what is solid, do
not block the task and do not stop the emulator — the session waits for the reset and carries on.

Its brief (`kit plan brief {tid}`) follows, so don't gather it again.

{brief}"""

OTHERS = ('Other agents are working on other tasks at the same time, in other worktrees. Keep your changes '
          'local (add functions and new modules; don\'t move, rename or reformat code you did not come for) '
          'so the merges stay clean.\n')


# --- small helpers -------------------------------------------------------------

def log(msg: str) -> None:
    line = f'=== {dt.datetime.now().isoformat(timespec="seconds")} {msg}'
    print(line, flush=True)
    try:
        with open(cfg(False).ctx('notes/run.log'), 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        pass


def git(*a: str, cwd=None) -> sp.CompletedProcess:
    return sp.run(['git', *a], cwd=str(cwd or ROOT), capture_output=True, text=True)


def default_branch() -> str:
    """The branch worktrees start from and finished work is merged into."""
    r = git('symbolic-ref', '--quiet', '--short', 'refs/remotes/origin/HEAD')
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip().split('/', 1)[-1]
    for b in ('main', 'master'):
        if git('rev-parse', '--verify', '--quiet', b).returncode == 0:
            return b
    return git('branch', '--show-current').stdout.strip() or 'main'


def plan_text(branch: str) -> list[str]:
    """TODO.md as the merge target has it — not as a worktree has it, so a branch mid-merge can't skew picks."""
    rel = os.path.relpath(Path(cfg(False).plan) / 'TODO.md', ROOT)
    r = git('show', f'{branch}:{rel}')
    if r.returncode == 0:
        return r.stdout.splitlines()
    from ps2kit import plan as P  # function-local: plan.py is a leaf, so this adds no import cycle
    return P.lines(Path(cfg(False).plan) / 'TODO.md')


# --- picking the next task -----------------------------------------------------

def tasks(branch: str) -> list[tuple[str, str, set[str]]]:
    """Open tasks in plan/TODO.md order: (id, section, files named on the line)."""
    from ps2kit import plan as P
    ls = plan_text(branch)
    # [ ] and [~] (blocked) both count as not done: a split parent runs only once its subtasks are all ticked
    open_ids = {m.group(1) for l in ls if (m := ANY_TASK.match(l)) and not l.startswith('- [x]')}
    out, section = [], ''
    for l in ls:
        if l.startswith('### '):
            section = l[4:]
            continue
        m = TASK.match(l)
        if not m or section.startswith('Stretch'):
            continue
        tid, rest = m.group(1), m.group(2)
        # a parent whose own subtasks are open or blocked: those come first
        if any(o != tid and o.startswith(tid) and o[len(tid)].isdigit() != tid[-1].isdigit()
               for o in open_ids):
            continue
        after = re.search(r'\(after (\S+?)\)', l)
        if after and after.group(1).rstrip(')') in open_ids:
            continue
        out.append((tid, section, set(P.files_on_line(rest))))
    return out


def family(tid: str) -> str:
    """L3a, L3b, L3 -> L3; A11k4 -> A11. Siblings share it and never run side by side, in any mode."""
    m = re.match(r'[A-Za-z]*\d*', tid)
    return m.group() if m else tid


def pick(running: dict, tried: set, mode: str, branch: str):
    """The next task a free slot may take, or None. See the module docstring for the rules."""
    busy = list(running.values())
    # A section whose heading says its tasks are ordered ("Next (in this order)") runs one task at a time:
    # its second task usually depends on the first having landed, whatever the files say.
    busy_sections = {b[1] for b in busy}
    for tid, section, files in tasks(branch):
        if all(tid != b[0] for b in busy) and git('rev-list', '-1', f'{branch}..task/{tid}').stdout.strip():
            continue  # finished, its branch is waiting to be merged: restarting it would reset the branch
        if any(family(tid) == family(b[0]) for b in busy):
            continue
        if tid in tried or any(tid == b[0] for b in busy):
            continue
        if mode == 'p':
            return tid, section, files
        if (section.startswith('Next') or 'in this order' in section) and section in busy_sections:
            continue
        if all(not (files & b[2]) for b in busy):
            return tid, section, files
    return None


# --- worktrees and panes -------------------------------------------------------

def worktree(n: int, branch: str) -> Path:
    """Slot n's worktree, created once and kept: a warm build directory is worth more than a clean one."""
    d = SLOTS_DIR / f's{n}'
    if not d.is_dir():
        SLOTS_DIR.mkdir(parents=True, exist_ok=True)
        git('worktree', 'add', '-q', '--detach', str(d), branch)
    private = ROOT / (cfg(False).raw.get('paths', {}).get('context') or 'context')
    links = [private, *[Path(p) for p in glob.glob(str(ROOT / '*.iso'))]]
    iso = cfg(False).raw.get('game', {}).get('iso')
    if iso and not os.path.isabs(iso):
        links.append(ROOT / iso)
    for src in links:
        dst = d / src.name
        if src.exists() and not dst.is_symlink() and not dst.exists():
            if not plat.link(src, dst):
                log(f'slot {n}: could not link {src.name} into the worktree; '
                    f'the session will not see it')
    return d


def tmux(*a: str, **kw) -> sp.CompletedProcess:
    return sp.run(['tmux', *a], capture_output=True, text=True, **kw)


def slots_arg(v: str):
    """The `slots` positional: a count, or the literal `once` (one task, then stop)."""
    if v == 'once':
        return 'once'
    try:
        return int(v)
    except ValueError:
        raise argparse.ArgumentTypeError(f'{v!r} is not a number of slots, nor "once"')


def mux_choice(slots, no_mux: bool, tmux_ok: bool, wt_ok: bool, windows: bool) -> str:
    """Which multiplexer a run gets: 'sequential', 'tmux', 'wt' or 'console'.

    Kept apart from the arguments and the environment so the decision can be read (and tested) on its own.
    One slot, `once` and `--no-tmux` all mean the sequential path, whatever is installed: a single session
    needs no second terminal, and the user asked to watch this one.
    """
    if no_mux or slots == 'once' or int(slots) <= 1:
        return 'sequential'
    if windows:
        return 'wt' if wt_ok else 'console'
    return 'tmux' if tmux_ok else 'sequential'


def pane(name: str, cwd: Path, cmd: str, env=()) -> int:
    """Open cmd in a pane of the window "agents", tagged @kit=name; returns the pane's pid.

    Panes are found by their @kit option rather than their position, so you can break them out, join them or
    swap them around while the run goes on.
    """
    envs = [a for e in env for a in ('-e', e)]
    windows = tmux('list-windows', '-F', '#{window_name}').stdout.split()
    where = ['new-window', '-n', 'agents']
    if 'agents' in windows:
        here = sorted((int(h[1:]), p) for p, h in
                      (l.split('\t') for l in tmux('list-panes', '-t', 'agents', '-F',
                                                   '#{pane_id}\t#{@kit}').stdout.splitlines())
                      if re.fullmatch(r's\d+', h))
        k = int(name[1:]) if re.fullmatch(r's\d+', name) else 0
        lower = [p for i, p in here if i < k]
        where = (['split-window', '-h', '-t', lower[-1]] if lower else
                 ['split-window', '-h', '-b', '-t', here[0][1] if here else 'agents'])
    r = tmux(*where, '-d', '-c', str(cwd), *envs, '-P', '-F', '#{pane_id} #{pane_pid}', cmd)
    if r.returncode or not r.stdout.split():
        raise SystemExit(f'tmux could not open a pane: {r.stderr.strip()}')
    pane_id, pid = r.stdout.split()[:2]
    tmux('set-option', '-p', '-t', pane_id, '@kit', name)
    tmux('set-option', '-w', '-t', 'agents', 'pane-border-status', 'top')
    tmux('set-option', '-w', '-t', 'agents', 'pane-border-format', ' #{@kit} ')
    tmux('select-layout', '-t', 'agents', 'even-horizontal')
    return int(pid)


class Mux:
    """One terminal per slot. A handle is whatever `open` returns; the loop only polls `alive` and `pid`.

    `pid` is the agent process itself, not a shell around it: `stalled()` reads its children to tell a
    session that is thinking from one that is running a tool.
    """

    name = '?'

    def open(self, slot: int, cwd: Path, argv: list[str], env: dict) -> object:
        raise NotImplementedError

    def alive(self, handle) -> bool:
        return plat.alive(self.pid(handle))

    def pid(self, handle) -> int:
        return int(handle)

    def where(self, slot: int) -> str:
        return f'pane s{slot}'


class TmuxMux(Mux):
    name = 'tmux'

    def open(self, slot, cwd, argv, env):
        # `sh -c` on one simple command execs it, so the pane's pid is the agent's own.
        return pane(f's{slot}', cwd, shlex.join(argv), [f'{k}={v}' for k, v in env.items()])


class WtMux(Mux):
    """A Windows Terminal tab per slot, in the window named "kit" so they gather in one place.

    `wt.exe` is a client: it hands the request to the running terminal and exits, so everything the loop
    needs about the session comes back through the shim's marker file instead of from this process.
    """

    name = 'wt'

    def open(self, slot, cwd, argv, env):
        job, marker = _shim_job(slot, cwd, argv, env)
        sp.Popen(wt_argv(slot, cwd, job), stdout=sp.DEVNULL, stderr=sp.DEVNULL)
        for _ in range(100):
            if marker.exists():
                break
            time.sleep(0.3)
        else:
            raise SystemExit(f'the Windows Terminal tab for slot {slot} never started (job {job})')
        return marker

    def alive(self, handle) -> bool:
        return Path(handle).exists()

    def pid(self, handle) -> int:
        try:
            return int(Path(handle).read_text(encoding='utf-8').strip() or 0)
        except (OSError, ValueError):
            return 0

    def where(self, slot):
        return f'the "s{slot}" tab'


class ConsoleMux(Mux):
    """One new console window per slot: no Windows Terminal, but still a window per session to watch."""

    name = 'console'

    def open(self, slot, cwd, argv, env):
        return plat.spawn_console(argv, cwd=cwd, env={**os.environ, **env})

    def where(self, slot):
        return f'the console window of slot {slot}'


def wt_argv(slot: int, cwd: Path, job: Path) -> list[str]:
    """`wt.exe` arguments for one slot's tab. `-w kit` reuses (or names) one window for the whole run."""
    return ['wt.exe', '-w', 'kit', 'new-tab', '--title', f's{slot}', '-d', str(cwd),
            *shim_argv(job)]


def shim_argv(job: Path) -> list[str]:
    return [sys.executable, str(Path(__file__).resolve()), '--slot-shim', str(job)]


def _shim_job(slot: int, cwd: Path, argv: list[str], env: dict) -> tuple[Path, Path]:
    marker = plat.rt(f'slot{slot}.live')
    marker.unlink(missing_ok=True)
    job = plat.rt(f'slot{slot}.job')
    job.write_text(json.dumps({'argv': argv, 'env': {k: str(v) for k, v in env.items()},
                               'cwd': str(cwd), 'marker': str(marker)}), encoding='utf-8')
    return job, marker


def slot_shim(job: str) -> int:
    """Run one slot's session and publish its pid, for a terminal we do not get a pid back from."""
    spec = json.loads(Path(job).read_text(encoding='utf-8'))
    marker = Path(spec['marker'])
    proc = sp.Popen(spec['argv'], cwd=spec['cwd'], env={**os.environ, **spec['env']})
    marker.write_text(str(proc.pid), encoding='utf-8')
    try:
        return proc.wait()
    finally:
        marker.unlink(missing_ok=True)


# --- starting a session --------------------------------------------------------

def agent_argv(prompt: str, sid: str, model: str, idle: int) -> list[str]:
    """The agent command for one task. The Stop hook is this very module in --stop-hook mode."""
    stop = [{'hooks': [{'type': 'command',
                        'command': plat.py_cmd(Path(__file__).resolve(), '--stop-hook', '--idle', str(idle))}]}]
    lean = [] if os.environ.get('KIT_LEAN') == '0' else \
        ['--tools', 'Bash,Read,Edit,Write', '--setting-sources', 'project,local', '--strict-mcp-config']
    cli = os.environ.get('KIT_AGENT', 'claude')
    return [cli, prompt, '--session-id', sid, '--permission-mode', 'bypassPermissions',
            '--disallowedTools', 'AskUserQuestion', '--model', model, *lean,
            '--settings', json.dumps({'hooks': {'Stop': stop, 'StopFailure': stop}})]


def fix_cmd(prompt: str) -> list[str]:
    """The command for a merge fixer: one prompt, one answer, no session to keep.

    `-p` rather than an interactive session, which is what makes this work on Windows too — there is no
    way to type into a running console there, and a one-shot run needs nobody to type into it. argv, not
    a shell string, because the prompt is a paragraph and quoting it through a shell buys nothing.
    """
    c = cfg(False)
    lean = [] if os.environ.get('KIT_LEAN') == '0' else \
        ['--tools', 'Bash,Read,Edit,Write', '--setting-sources', 'project,local', '--strict-mcp-config']
    return [os.environ.get('KIT_AGENT', 'claude'), '-p', prompt,
            '--permission-mode', 'bypassPermissions', '--disallowedTools', 'AskUserQuestion',
            '--model', c.fix_model, *lean]


def brief_for(tid: str, cwd: Path) -> str:
    r = sp.run([sys.executable, str(ROOT / 'kit'), 'plan', 'brief', tid], cwd=str(cwd), capture_output=True, text=True)
    return r.stdout or f'(kit plan brief {tid} produced nothing: {r.stderr.strip()[:200]})'


def start(n: int, task, branch: str, model: str, idle: int, slots: int, mux: Mux):
    tid = task[0]
    d = worktree(n, branch)
    git('reset', '-q', '--hard', cwd=d)
    git('clean', '-fdq', cwd=d)
    co = git('checkout', '-q', '-B', f'task/{tid}', branch, cwd=d)
    if co.returncode:  # e.g. another worktree still holds task/<ID>: never start an agent on the wrong branch
        log(f'slot {n}: checkout task/{tid} failed: {co.stderr.strip()[:200]}')
        return None
    sid = str(uuid.uuid4())
    others = OTHERS if slots > 1 else ''
    prompt = PROMPT.format(tid=tid, n=n, others=others, brief=brief_for(tid, d))
    logfile = cfg(False).ctx(f'notes/slot{n}.log')
    since = logfile.stat().st_size if logfile.exists() else 0  # in append mode tell() is not that
    with open(logfile, 'a', encoding='utf-8') as f:
        f.write(f'\n=== {dt.datetime.now().isoformat(timespec="seconds")} {tid} session {sid}\n')
    env = {'KIT_PCSX2': str(n), 'KIT_RUN': '1',
           'KIT_CHECK_SLOTS': str(os.environ.get('KIT_CHECK_SLOTS', max(2, min(slots, 3))))}
    handle = mux.open(n, d, agent_argv(prompt, sid, model, idle), env)
    log(f'slot {n}: {tid} ({task[1]}; {", ".join(sorted(task[2])) or "no files listed"})')
    return handle, sid, since


# --- merging a finished branch -------------------------------------------------

MERGE = threading.Lock()  # one merge into the default branch at a time


def joinable(f: str) -> bool:
    """True when the runner may join both sides of a conflict in `f` itself, rather than ask a human.

    Line-oriented text, where a line is a whole record and two agents each added records: the task list
    and its archive, the journal, and the oracle's address list.

    Prose stays off the list even where it is only ever appended to, `PLAN.md`'s code map included. A
    union joins *lines*, and two sides that both reflowed one paragraph give it no line to agree on, so
    it stitches them into something that reads like a sentence and is not one — a tail left stranded in
    the middle of a bullet, which a reader believes. The port repo had to go back and repair exactly that
    (openmx 10dcc05) after one such merge. A paragraph needs reading, so it goes to whoever can read it.

    Off config, not off this repo's layout: `[paths]` and `[oracle] addresses` all move.
    """
    c = cfg(False)

    def rel(p) -> str | None:
        p = Path(p)
        return str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else None

    if f == rel(c.oracle_file):
        return True
    return any(f.startswith(f'{d}/') and f.endswith('.md')
               for d in (rel(c.plan), rel(c.journal)) if d)


def addresses(text: str, where: str) -> str | None:
    """The oracle's address list after a union join: drop a line both sides added, refuse a real clash.

    An address list is append-only, so joining both sides is right — while the two sides agree. The
    same name at two different addresses is the case where they do not, and nothing downstream picks
    between them: `Kit::addr` in Rust and `oracle.entries` here both take the first line that matches,
    in file order. Resolving it by picking a side would point the oracle at whichever worktree the run
    merged first and say nothing about it — a wrong-address oracle call, which is the failure this kit
    exists to catch. So that one goes to a human instead. `kit oracle add` refuses the same clash; a
    merge must not become the way around it.
    """
    from ps2kit import oracle as O
    try:
        rows = O.parse(text, where)
    except ValueError as e:
        log(f'{e}')
        return None
    at: dict[tuple[str, str], int] = {}
    for name, prog, addr, _note in rows:
        if at.setdefault((name, prog), addr) != addr:
            log(f'{where}: both sides name {name}{f" in {prog}" if prog else ""} and disagree about its '
                f'address ({at[(name, prog)]:#x} / {addr:#x}) — a human picks that one')
            return None
    kept, seen = [], set()
    for line in text.split('\n'):
        entry = ' '.join(line.partition('#')[0].split())
        if entry and entry in seen:   # the same name and address added on both sides
            continue
        seen.add(entry)
        kept.append(line)
    return '\n'.join(kept)


def union(files: list[str], tid: str, branch: str, cwd: Path | None = None) -> bool:
    """Join both sides of a conflict in every file `joinable` allows: two agents both added lines, and
    both sets belong. Then drop a task line that came out twice, any TODO line whose task DONE.md already
    has, and — in the address list, which cannot be joined blind — a name both sides gave the same address.
    """
    from ps2kit import plan as P
    cwd = cwd or ROOT
    orel = str(Path(cfg(False).oracle_file).relative_to(ROOT)) \
        if Path(cfg(False).oracle_file).is_relative_to(ROOT) else None
    for f in sorted(files, key=lambda f: not f.endswith('DONE.md')):  # DONE.md first: TODO.md reads it
        sides = []
        for stage in (2, 1, 3):  # ours, base, theirs
            t = cfg(False).ctx(f'notes/union.{stage}')
            t.write_text(git('show', f':{stage}:{f}', cwd=cwd).stdout, encoding='utf-8')
            sides.append(str(t))
        out = git('merge-file', '-p', '--union', *sides, cwd=cwd)
        if out.returncode < 0:
            return False
        text = out.stdout
        if f.endswith(('TODO.md', 'DONE.md')):
            done_path = cwd / Path(cfg(False).plan).relative_to(ROOT) / 'DONE.md'
            done = {m.group(1).rstrip('.') for l in P.lines(done_path)
                    if (m := ANY_TASK.match(l)) and '- [x]' in l} if f.endswith('TODO.md') else set()
            mine = git('show', f'task/{tid}:{f}').stdout.splitlines()
            kept, seen = [], {}
            for l in text.split('\n'):
                if m := ANY_TASK.match(l):
                    k = m.group(1).rstrip('.')
                    if k in done:
                        continue
                    if k in seen:  # keep the branch's copy of its own task, the target's copy of the rest
                        if (k == tid) == (l in mine):
                            kept[seen[k]] = l
                        continue
                    seen[k] = len(kept)
                kept.append(l)
            text = '\n'.join(kept)
        elif f == orel and (text := addresses(text, f)) is None:
            return False
        (cwd / f).write_text(text, encoding='utf-8')
        git('add', f, cwd=cwd)
    return True


FIXER = """A merge of task/{tid} into {branch} is half-done in this checkout and git could not finish it \
by itself. Finish it. Nobody will answer questions: decide, and say what you decided.

Conflicted files:
{files}

What this is: an unattended run gave several agents a task each, on their own branch, and is merging them \
one at a time. The runner joins conflicts in record-per-line files itself; this one needs reading, which is \
why you are here instead of a human.

1. `git status` and `git diff` to see it. Resolve every conflict so that **both sides' intent survives** — \
two agents each did work somebody wanted, and a resolution that drops one of them silently is the worst \
outcome available. Where they genuinely cannot both hold, keep the one that matches {branch}'s current \
direction and say so.
2. Change nothing the merge did not touch. You are not reviewing the code and you are not doing task \
{tid}'s work: a conflict is all that is in front of you.
3. Then `git commit --no-edit` to land the merge. Then stop. Do not push, do not run the test suite (the \
runner runs it as soon as you are done, and the agents are using the build directories), do not merge, \
rebase or delete anything else, do not touch the other worktrees under .worktrees/ or `git stash`.
4. If it cannot be resolved honestly — the two sides contradict each other and picking needs a human — run \
`git merge --abort` and say why in one line. That is a real answer, not a failure.

What happened on earlier merges in this run:
{history}
"""


def fix_log(line: str) -> None:
    """One line per fixed merge, for the next fixer and for whoever reads the run afterwards."""
    f = cfg(False).ctx('notes/master.md')
    with open(f, 'a', encoding='utf-8') as fh:
        fh.write(line.rstrip() + '\n')


def fix_merge(tid: str, branch: str, files: list[str], cwd: Path | None = None) -> str | None:
    """Give a conflict git could not join to a Claude session, in `cwd` (the merge worktree). None when it landed.

    One session per merge, run to completion while the caller waits — the merge lock is already held, so
    there is nothing to be gained from doing it in the background and plenty to lose. Each one starts with
    an empty context and the tail of `notes/master.md`, so what it knows is what it needs to know rather
    than everything that happened today.

    Never fatal: a fixer that fails, times out, or is not installed leaves the merge exactly as it was for
    the caller to abort, and the task is reported as left for a human, which is what used to happen to
    every conflict of this kind.
    """
    c = cfg(False)
    cwd = cwd or ROOT
    if not c.fix_merges:
        return 'conflicts a join cannot resolve, and [run] fix_merges is off'
    history = ''.join(open(c.ctx('notes/master.md'), encoding='utf-8').readlines()[-12:]) \
        if c.ctx('notes/master.md').exists() else '(nothing yet: this is the first)'
    prompt = FIXER.format(tid=tid, branch=branch, files='\n'.join(f'  {f}' for f in files),
                          history=history.strip() or '(nothing yet)')
    logfile = c.ctx('notes/fixer.log')
    log(f'{tid}: conflicts in {", ".join(files)} need reading — handing them to a session')
    with open(logfile, 'a', encoding='utf-8') as fh:
        fh.write(f'\n=== {dt.datetime.now().isoformat(timespec="seconds")} {tid} '
                 f'{", ".join(files)}\n')
        try:
            r = sp.run(fix_cmd(prompt), cwd=str(cwd), stdout=fh, stderr=sp.STDOUT,
                       timeout=c.fix_timeout)
        except FileNotFoundError:
            return f'no agent command to fix the conflicts with (KIT_AGENT={os.environ.get("KIT_AGENT", "claude")})'
        except sp.TimeoutExpired:
            fix_log(f'{tid}: fixer timed out after {c.fix_timeout}s — left for a human')
            return f'the session fixing the conflicts ran past {c.fix_timeout}s'
    if git('rev-parse', '--verify', '-q', 'MERGE_HEAD', cwd=cwd).returncode == 0:
        fix_log(f'{tid}: fixer left the merge unfinished — left for a human')
        return f'the session did not finish the merge (exit {r.returncode}); see {logfile.relative_to(ROOT)}'
    if git('merge-base', '--is-ancestor', f'task/{tid}', 'HEAD', cwd=cwd).returncode:
        fix_log(f'{tid}: fixer aborted the merge — left for a human')
        return f'the session aborted the merge; its reason is in {logfile.relative_to(ROOT)}'
    fix_log(f'{tid}: conflicts in {", ".join(files)} resolved by a session')
    log(f'{tid}: the session resolved {", ".join(files)} and committed the merge')
    return None


def untestable(f: str) -> bool:
    """A change `./kit check` can't see: markdown, or CI config. A commit like that landing mid-test used to
    throw away a whole test run and start it again (opentennis B535, 2026-10-16)."""
    return f.endswith('.md') or f.startswith('.github/')


def merge(tid: str, branch: str) -> str | None:
    """Merge task/<tid> into the default branch: merge, archive ticks, run the tests, delete the branch.

    The merge is built and tested on a copy of the branch in its own worktree (`<repo>-slots/smerge`) and the
    branch only moves once the tests pass, so the main checkout never holds an untested merge: people and
    sessions there keep committing and pushing the whole time, and nothing is reset under them. (It used to
    be merged and tested in the main checkout behind a marker the guard hook enforced, which refused every
    commit there for the length of a test run.)

    Returns None when it landed, else why it was left alone (the target branch is then as it was, and the
    branch is still there for a human to look at).
    """
    with MERGE:
        d = worktree('merge', branch)
        git('merge', '--abort', cwd=d), git('reset', '-q', '--hard', cwd=d), git('clean', '-fdq', cwd=d)
        try:
            return _merge(tid, branch, d)
        finally:
            git('merge', '--abort', cwd=d)  # a conflict left there
            git('checkout', '-q', '--detach', cwd=d)


def kit_in(d: Path, *a: str) -> sp.CompletedProcess:
    """`./kit <a>` in checkout d, with this checkout's kit.toml (it is not in git, so not in the worktree)."""
    from ps2kit.config import _config_path
    env = {**os.environ, 'KIT_CONFIG': os.environ.get('KIT_CONFIG') or str(_config_path())}
    return sp.run([sys.executable, str(d / 'kit'), *a], cwd=str(d), capture_output=True, text=True, env=env)


def _merge(tid: str, branch: str, d: Path) -> str | None:
    """merge()'s body, under MERGE, with d the merge worktree.

    Code commits that reach the branch while the tests run aren't covered by the tested commit, so it is
    built and tested again on top of them (three tries, then it lands anyway, as any two commits on the
    branch would); markdown-only ones don't need it and the tested commit is merged onto them.
    """
    for attempt in range(3):
        base = git('rev-parse', branch).stdout.strip()
        git('checkout', '-q', '--detach', base, cwd=d)
        if git('merge', '--no-edit', '-m', f"Merge branch 'task/{tid}'", f'task/{tid}', cwd=d).returncode:
            files = git('diff', '--name-only', '--diff-filter=U', cwd=d).stdout.split()
            if not [f for f in files if not joinable(f)] and union(files, tid, branch, d):
                git('commit', '-q', '--no-edit', cwd=d)
                log(f'{tid}: joined both sides of {", ".join(files)}')
            else:
                # A join either cannot be trusted here or refused: start the conflict again from a
                # clean state (a refused join may have written some of its files already) and give it
                # to a session that can read it.
                git('merge', '--abort', cwd=d)
                git('merge', '--no-edit', '-m', f"Merge branch 'task/{tid}'", f'task/{tid}', cwd=d)
                if why := fix_merge(tid, branch, files, d):
                    return why
        kit_in(d, 'plan', 'archive')
        plan_files = [str(Path(cfg(False).plan).relative_to(ROOT) / n) for n in ('TODO.md', 'DONE.md')]
        if git('status', '--porcelain', '--', *plan_files, cwd=d).stdout.strip():
            git('commit', '-qm', f'{tid}: archive ticked tasks', '--', *plan_files, cwd=d)
        merged = git('rev-parse', 'HEAD', cwd=d).stdout.strip()
        # the agent's ./kit done already passed the tests on the branch: rerun them only if the merged code
        # differs from what it tested (a markdown-only difference is not code)
        if not all(map(untestable, git('diff', '--name-only', f'task/{tid}', merged).stdout.split())):
            chk = kit_in(d, 'check')
            if chk.returncode:
                return 'the tests fail after the merge: ' + ' / '.join(chk.stdout.splitlines()[:4])[:400]
        moved = git('diff', '--name-only', base, branch).stdout.split()
        if attempt < 2 and not all(map(untestable, moved)):
            log(f'{tid}: {branch} got code commits during the tests ({", ".join(moved[:5])}); testing the merge again')
            continue
        why = land(tid, branch, merged)
        if why == 'conflict' and attempt < 2:
            continue
        if why:
            return why
        break
    git('branch', '-d', f'task/{tid}')
    line = next((l for l in plan_text(branch) if f'**{tid}**' in l), '')
    state = 'blocked' if line.startswith('- [~]') else 'part' if line else 'ticked'
    log(f'{tid}: merged ({state})')
    # Published here, and only here: the merge has passed the test gate above, which is the thing
    # that makes pushing it unattended defensible. A failed push is logged, not fatal — the merge
    # landed locally and the next one pushes both. `drop_if_merged` then has its answer immediately,
    # so a merged task branch leaves the remote instead of accumulating there.
    #
    # `[git] push_merge = false` is for the other policy — the merge stays local until a person has
    # read it — and then the line below says what to run. Both are defensible and which one is right
    # belongs to whoever owns the repository, so it is a key rather than a rule.
    from ps2kit.checks import drop_if_merged, push
    if cfg(False).push_merge:
        if out := push(ROOT, branch):
            log(f'{tid}: {out}')
    elif cfg(False).push_remote:
        log(f'{tid}: merged, not pushed ([git] push_merge is off): '
            f'git push {cfg(False).push_remote} {branch}')
    if out := drop_if_merged(ROOT, f'task/{tid}'):
        log(f'{tid}: {out}')
    return None


def land(tid: str, branch: str, merged: str) -> str | None:
    """Move the branch in the main checkout to the tested merge: seconds, not a test run. None when it
    landed, 'conflict' when the branch's new commits clash with it (the caller builds it again on top),
    else why it could not.

    Uncommitted edits in the main checkout are never moved aside: git refuses, before starting, a merge
    that would change a file somebody is editing, so this waits for them to commit it (a stash used to
    hold them, and a pop that clashed left the work in a stash nobody looked at).
    """
    t0 = time.time()
    while git('diff', '--name-only', '--diff-filter=U').stdout.strip() or \
            git('rev-parse', '--verify', '-q', 'MERGE_HEAD').returncode == 0:
        if time.time() - t0 > 60 * 60:
            return 'the main checkout has a merge or unmerged paths of its own, for 60 min'
        time.sleep(10)
    cur = git('branch', '--show-current').stdout.strip()
    if cur != branch and git('checkout', '-q', branch).returncode:
        return f'the main checkout is on {cur or "a detached HEAD"} and would not switch to {branch}'
    msg = ['-m', f'{tid}: its tested merge, onto commits {branch} got during the tests']
    while git('merge', '-q', *msg, merged).returncode and \
            git('rev-parse', '--verify', '-q', 'MERGE_HEAD').returncode:
        if time.time() - t0 > 60 * 60:
            return 'uncommitted edits in the main checkout to files the merge changes, for 60 min'
        time.sleep(10)
    if git('rev-parse', '--verify', '-q', 'MERGE_HEAD').returncode == 0:
        files = git('diff', '--name-only', '--diff-filter=U').stdout.split()
        if [f for f in files if not joinable(f)] or not union(files, tid, branch):
            git('merge', '--abort')
            return 'conflict'
        git('commit', '-q', '--no-edit')
    if git('merge-base', '--is-ancestor', merged, 'HEAD').returncode:
        return f'the merge did not land in {branch}; merge it again'
    return None


def finish(tid: str, n: int, sid: str, branch: str) -> None:
    commits = git('log', '--format=%s', f'{branch}..task/{tid}').stdout.splitlines()
    if not commits:
        git('branch', '-D', f'task/{tid}')
        log(f'{tid}: no commits — nothing to merge')
        return
    if why := merge(tid, branch):
        log(f'{tid}: LEFT FOR A HUMAN — {why}. Its {len(commits)} commits are on task/{tid}: '
            f'{" / ".join(commits)[:300]}')
        # Parked as wip/<ID>: a task/<ID> ahead of the branch reads as finished-and-waiting to pick(), which
        # would then skip the task for good; as wip/ the task can start again with the old attempt to read.
        if not git('branch', '-M', f'task/{tid}', f'wip/{tid}').returncode:
            log(f'{tid}: its branch is parked as wip/{tid}; the task stays open and can start again')
        from ps2kit.checks import push  # an unmerged branch is the one copy of that work: get it off this disk
        if out := push(ROOT, f'wip/{tid}'):
            log(f'{tid}: {out}')


# --- watching a session --------------------------------------------------------

def transcript(sid: str) -> str | None:
    return next(iter(glob.glob(os.path.expanduser(f'~/.claude/projects/*/{sid}.jsonl'))), None)


def stats(sid: str) -> str:
    """'N min, N calls, context Nk first, Nk peak' for the exit line. Watch the peak over a run: if it keeps
    creeping up, or more tasks come back blocked, a context cut went too far."""
    calls, ctx, ts = 0, [], []
    try:
        for l in open(transcript(sid) or os.devnull, encoding='utf-8', errors='replace'):
            d = json.loads(l)
            ts += [d['timestamp']] if 'timestamp' in d else []
            if d.get('type') == 'assistant' and (u := d.get('message', {}).get('usage')):
                calls += 1
                ctx.append(sum(u.get(k, 0) for k in ('input_tokens', 'cache_read_input_tokens',
                                                     'cache_creation_input_tokens')))
    except (OSError, ValueError, KeyError):
        pass
    if not ctx or not ts:
        return 'no transcript'
    span = (dt.datetime.fromisoformat(ts[-1].replace('Z', '+00:00')) -
            dt.datetime.fromisoformat(ts[0].replace('Z', '+00:00')))
    return f'{int(span.total_seconds()) // 60} min, {calls} calls, context {ctx[0] // 1000}k first, {max(ctx) // 1000}k peak'


def stalled(pid: int, sid: str, limit: int) -> str | None:
    """Why a live session looks stuck, else None: quiet for `limit` seconds with no tool running (a prompt or
    a dialog the idle hook never sees), or quiet for longer than the Bash timeout even with one (a hung tool).
    """
    t = transcript(sid)
    quiet = time.time() - os.path.getmtime(t) if t and os.path.exists(t) else 0
    busy = bool(plat.children(pid))
    if quiet > 65 * 60 or (quiet > limit and not busy):
        return f'no transcript activity for {int(quiet // 60)} min' + \
               (' with a tool still running' if busy else ' at a prompt')
    return None


def limit_only(sid: str, slot_log: Path, since: int) -> dt.datetime | None:
    """If the session did nothing but hit the usage limit: discard its transcript, return the reset time.

    This matters more than it looks. A limited session produced no work, so counting it as an attempt would
    quietly drop the task from the night; and keeping its transcript would make the next run resume a session
    that never started.
    """
    t = transcript(sid)
    text = open(t, encoding='utf-8', errors='replace').read() if t else ''
    try:
        with open(slot_log, encoding='utf-8', errors='replace') as f:
            f.seek(since)
            text += f.read()
    except OSError:
        pass
    m = re.findall(r'limit\D{0,12}resets? (\d+(?::\d+)?\s?[ap]m)', text, re.I)
    if not m or '"type":"tool_use"' in text:
        return None
    if t:
        import shutil
        os.remove(t)
        shutil.rmtree(t[:-6], ignore_errors=True)  # and its sidecar dir, if this build keeps one
    s = m[-1].replace(' ', '').lower()
    when = dt.datetime.strptime(s, '%I:%M%p' if ':' in s else '%I%p')
    now = dt.datetime.now()
    at = now.replace(hour=when.hour, minute=when.minute, second=0, microsecond=0)
    if at < now - dt.timedelta(minutes=10):
        at += dt.timedelta(days=1)
    return max(at, now)


_weekly = [-1e9, None]


def weekly() -> float | None:
    """The account's 7-day usage %, from the endpoint `/usage` reads (the local login's OAuth token; costs no
    tokens), checked at most every 5 minutes. None when it can't be read, which means "don't cap"."""
    if time.time() - _weekly[0] < 300:
        return _weekly[1]
    import urllib.request
    try:
        tok = json.load(open(os.path.expanduser('~/.claude/.credentials.json')))['claudeAiOauth']['accessToken']
        req = urllib.request.Request('https://api.anthropic.com/api/oauth/usage',
                                     headers={'Authorization': f'Bearer {tok}',
                                              'anthropic-beta': 'oauth-2025-04-20'})
        _weekly[1] = json.load(urllib.request.urlopen(req, timeout=10))['seven_day']['utilization']
    except Exception:
        _weekly[1] = None
    _weekly[0] = time.time()
    return _weekly[1]


# --- the idle stop hook --------------------------------------------------------

def stop_hook(idle: int) -> int:
    """Stop hook for a session this runner started: end it once it has been idle for `idle` seconds.

    A finished agent otherwise sits at its prompt and the loop can never start the next task. "Idle" means no
    new user or tool-result entry in the transcript — so typing into the pane keeps the session alive and you
    can take it over by hand. A session with a background task still running gets a much longer grace period,
    because a background task normally ends and wakes the session by itself.

    A usage limit is deliberately *not* idleness: Claude Code prints "continuing automatically at <reset>" and
    resumes the session itself, so the waiter leaves such a session alone.
    """
    try:
        ev = json.load(sys.stdin)
    except Exception:
        return 0
    t = ev.get('transcript_path')
    if not t or not os.path.exists(t):
        return 0
    if ev.get('background_tasks'):
        idle = max(idle, int(os.environ.get('KIT_IDLE_BG', 1800)))
    try:
        n = _user_entries(t)
        plat.spawn_detached([sys.executable, str(Path(__file__).resolve()), '--stop-waiter',
                             str(os.getppid()), t, str(n), str(idle)])
    except Exception:
        pass
    return 0


def _user_entries(t: str) -> int:
    try:
        with open(t, encoding='utf-8', errors='replace') as f:
            return sum(1 for l in f if '"type":"user"' in l)
    except OSError:
        return -1


def stop_waiter(pid: int, t: str, n: int, idle: int) -> int:
    time.sleep(idle)
    if _user_entries(t) != n:
        return 0  # the human typed, or a tool came back: the session is alive, leave it
    try:
        text = open(t, encoding='utf-8', errors='replace').read()
    except OSError:
        text = ''
    tail = text[text.rfind('continuing automatically'):] if 'continuing automatically' in text else ''
    if tail and '"tool_use"' not in tail:
        return 0  # waiting out a usage limit; Claude Code resumes it at the reset
    plat.terminate(pid)
    return 0


# --- sequential fallback -------------------------------------------------------

def sequential(branch: str, model: str, idle: int, pause: int, force: bool, once: bool = False) -> int:
    """One session at a time, in this checkout, on the current branch: `kit run 1`, `once`, `--no-tmux`,
    or no multiplexer to put the slots in.

    Honest about the trade: no worktrees, so a task commits straight onto the branch you are on, and the
    emulator copy is slot 1.
    """
    log(f'sequential run on {branch} (one session at a time, committing in this checkout'
        + (', one task only)' if once else ')'))
    tried: set[str] = set()
    while (task := pick({}, tried, 'p', branch)):
        if not force and (w := weekly()) is not None and w >= WEEKLY_MAX:
            log(f'weekly usage {w}% >= {WEEKLY_MAX}%: waiting for the reset (--force to go on)')
            time.sleep(300)
            continue
        tid = task[0]
        sid = str(uuid.uuid4())
        prompt = PROMPT.format(tid=tid, n=1, others='', brief=brief_for(tid, ROOT))
        log(f'{tid}: starting ({task[1]})')
        sp.run(agent_argv(prompt, sid, model, idle), cwd=str(ROOT),
               env={**os.environ, 'KIT_PCSX2': '1', 'KIT_RUN': '1'})
        log(f'{tid}: session ended ({stats(sid)})')
        if r := limit_only(sid, cfg(False).ctx('notes/slot1.log'), 0):
            log(f'{tid}: only hit the usage limit; sleeping until {r:%H:%M}')
            time.sleep(max(0, (r - dt.datetime.now()).total_seconds()) + pause)
            continue
        tried.add(tid)
        if once:
            log(f'{tid}: done, and `once` was asked for')
            return 0
        time.sleep(pause)
    log('sequential run done: no task left that it may start')
    return 0


# --- the loop ------------------------------------------------------------------

def cmd_run(argv: list[str]) -> int:
    from ps2kit.cli import parser  # function-local: nothing in the kit imports cli at module level
    ap = parser('run', __doc__)
    ap.add_argument('slots', nargs='?', type=slots_arg, default=3,
                    help='how many agents at once (default 3), or "once" for one task then stop')
    ap.add_argument('--mode', choices=['p', 'd'], default='d',
                    help="p = plan order, overlapping files allowed; d = different files (default)")
    ap.add_argument('--force', action='store_true', help='keep starting tasks past the weekly usage cap')
    ap.add_argument('--idle', type=int, default=90, help='seconds of quiet before a session is ended (90)')
    ap.add_argument('--stall', type=int, default=20 * 60, help='seconds of quiet that counts as stalled (1200)')
    ap.add_argument('--pause', type=int, default=60, help='seconds between a session ending and the next (60)')
    ap.add_argument('--model', default=os.environ.get('KIT_MODEL', 'opus'), help='the agent model (opus)')
    ap.add_argument('--no-tmux', action='store_true', help='force the single sequential session')
    ap.add_argument('--stop-hook', action='store_true', help=argparse.SUPPRESS)
    ap.add_argument('--stop-waiter', nargs=4, metavar=('PID', 'TRANSCRIPT', 'N', 'IDLE'),
                    help=argparse.SUPPRESS)
    ap.add_argument('--slot-shim', metavar='JOB', help=argparse.SUPPRESS)
    a = ap.parse_args(argv)

    if a.stop_hook:
        return stop_hook(a.idle)
    if a.stop_waiter:
        pid, t, n, idle = a.stop_waiter
        return stop_waiter(int(pid), t, int(n), int(idle))
    if a.slot_shim:
        return slot_shim(a.slot_shim)

    branch = default_branch()
    if git('rev-parse', '--verify', '--quiet', branch).returncode:
        print(f'no branch {branch!r} to merge into — commit something first', file=sys.stderr)
        return 2
    cfg(False).ctx('notes/.keep').write_text('', encoding='utf-8')

    choice = mux_choice(a.slots, a.no_tmux, bool(shutil.which('tmux')), bool(shutil.which('wt.exe')),
                        plat.WINDOWS)
    if choice == 'sequential':
        if not a.no_tmux and a.slots != 'once' and int(a.slots) > 1:
            log('no terminal multiplexer here: falling back to one sequential session '
                '(install tmux, or Windows Terminal, for parallel slots and worktrees)')
        return sequential(branch, a.model, a.idle, a.pause, a.force, once=a.slots == 'once')
    if choice == 'tmux' and not os.environ.get('TMUX'):
        print(f'Run me inside tmux, so the sessions get panes you can attach to:\n'
              f'    tmux new -s kit kit run {a.slots} --mode {a.mode}', file=sys.stderr)
        return 2
    if choice == 'console':
        log('Windows Terminal (wt.exe) is not installed: each slot gets its own console window instead')
    mux = {'tmux': TmuxMux, 'wt': WtMux, 'console': ConsoleMux}[choice]()

    running: dict[int, tuple] = {}
    procs: dict[int, tuple] = {}
    tried: set[str] = set()
    free_at = {n: dt.datetime.min for n in range(1, a.slots + 1)}
    merging: list[threading.Thread] = []
    stall_said: set[str] = set()
    cap_said = False
    log(f'run: {a.slots} slots in {mux.name}, mode {a.mode}, merging into {branch}'
        + (f', weekly usage {weekly()}%' if not a.force else ' (weekly cap ignored)'))

    # branches a previous run never merged (it was stopped, or a session ended while nothing watched)
    held = {l.removeprefix('branch refs/heads/') for l in
            git('worktree', 'list', '--porcelain').stdout.splitlines() if l.startswith('branch ')}
    open_ids = {m.group(1) for l in plan_text(branch) if (m := TASK.match(l))}
    for b in git('branch', '--format=%(refname:short)', '--list', 'task/*').stdout.split():
        tid = b.removeprefix('task/')
        if b in held or tid not in open_ids:
            continue
        if git('log', '--format=%s', f'{branch}..{b}').stdout.strip():
            log(f'{tid}: finished while no run was up; merging it now')
            tried.add(tid)
            t = threading.Thread(target=finish, args=(tid, 0, '(unknown)', branch))
            t.start()
            merging.append(t)
    for n in free_at:  # idle slots must let go of old task/<ID> branches, or start() cannot take them
        if (SLOTS_DIR / f's{n}').is_dir():
            git('checkout', '-q', '--detach', cwd=SLOTS_DIR / f's{n}')

    while True:
        for n in list(free_at):
            if n in procs and not mux.alive(procs[n][0]):
                (handle, sid, since), task = procs.pop(n), running.pop(n)
                log(f'slot {n}: {task[0]} exited ({stats(sid)})')
                sp.Popen([sys.executable, str(ROOT / 'kit'), 'pcsx2', 'stop'], env={**os.environ, 'KIT_PCSX2': str(n)},
                         stdout=sp.DEVNULL, stderr=sp.DEVNULL)
                if reset := limit_only(sid, cfg(False).ctx(f'notes/slot{n}.log'), since):
                    free_at[n] = reset + dt.timedelta(seconds=a.pause)
                    log(f'slot {n}: {task[0]} only hit the usage limit; transcript dropped, the task goes '
                        f'back in the pool and the slot sleeps until {reset:%H:%M}')
                else:
                    tried.add(task[0])  # one attempt per run: ticked, blocked or failed, a human looks next
                    git('checkout', '-q', '--detach', cwd=SLOTS_DIR / f's{n}')
                    t = threading.Thread(target=finish, args=(task[0], n, sid, branch))
                    t.start()
                    merging.append(t)
                    free_at[n] = dt.datetime.now() + dt.timedelta(seconds=a.pause)
            if n in procs and procs[n][1] not in stall_said and \
                    (why := stalled(mux.pid(procs[n][0]), procs[n][1], a.stall)):
                stall_said.add(procs[n][1])
                log(f'slot {n}: {running[n][0]} looks stalled ({why}); look at {mux.where(n)}')
                print('\a', end='', flush=True)
            if n not in procs and dt.datetime.now() >= free_at[n]:
                if not a.force and (w := weekly()) is not None and w >= WEEKLY_MAX:
                    if not cap_said:
                        log(f'weekly usage {w}% >= {WEEKLY_MAX}%: no new tasks until it resets '
                            f'(restart with --force to go on now)')
                        cap_said = True
                    continue
                cap_said = False
                if task := pick(running, tried, a.mode, branch):
                    if p := start(n, task, branch, a.model, a.idle, a.slots, mux):
                        procs[n], running[n] = p, task
                    else:
                        tried.add(task[0])
        merging = [t for t in merging if t.is_alive()]
        if not procs and not merging and not pick(running, tried, a.mode, branch) \
                and all(dt.datetime.now() >= t for t in free_at.values()):
            log(f'run done; tried this run: {", ".join(sorted(tried)) or "none"}')
            return 0
        time.sleep(10)


def main(argv: list[str] | None = None) -> int:
    return cmd_run(list(sys.argv[1:] if argv is None else argv))


if __name__ == '__main__':
    sys.exit(main())
