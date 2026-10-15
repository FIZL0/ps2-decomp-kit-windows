"""The test gate and the wrap-up, summarised to something an agent can read.

Two commands, both of which exist because of how an agent reads output:

`kit check` runs the port's test command and prints *one line* when it passes. A passing `cargo test` is
300 lines of noise, and an agent re-reads its whole conversation every turn, so those 300 lines are paid for
again on every later turn of the session. When it fails it prints only the failure, and — this is the part
that matters most — it says plainly whether the **build** broke or a **test** failed. Those two call for
opposite next moves (fix a compile error you just introduced vs. go and look at the thing you are porting),
and an agent that cannot tell them apart will happily "fix" a passing port to satisfy a type error.

`kit done` is the wrap-up every task ends with: tests, then tick (or block), then commit, in one call. One
call because three calls means three chances to commit work that does not build, and because the tick and the
commit belong to the same decision. On failure it changes nothing at all.

`kit audit` is the third, and it is a gate inside `kit done` rather than something you remember to
run: it greps every committed file for the addresses in `research/oracle.txt` and for Ghidra's
placeholder names, because "no retail address in the port" is a rule that needs a check. A rule without
one survives exactly as long as nobody is in a hurry.

    kit check [extra args passed to the test command]
    kit done <ID> "<commit message>" [--blocked "<why + what unblocks it>"] [--skip-audit]
    kit audit

Both honour a concurrency gate and a build-directory size cap; see `_slot` and `_cap_build_dirs`.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ps2kit.config import cfg, ROOT
from ps2kit import platform_ as plat

MAX_LINES = 60
MAX_COLS = 300
PASS_PATTERNS = (r'(\d+) passed', r'(\d+) tests? ran', r'Ran (\d+) tests?', r'(\d+) tests?,? ok',
                 r'ok \| (\d+) passed')
# Lines worth showing when a *test* failed rather than the build. Deliberately a union of the common
# runners' shapes: cargo, pytest, ctest, meson, go, catch2.
FAIL_LINE = re.compile(r'^(?:\s*---- .+ ----|thread .+ panicked|panicked at|assertion .+failed|'
                       r'\s*(?:FAILED|FAIL|ERROR|E)\b.*|.*\bAssertionError\b.*|.*\bassert\b.*|'
                       r'test .+ \.\.\. FAILED|.*Test #\d+: .+Failed.*|\s*--- FAIL:.*|'
                       r'left:.*|right:.*|\s+at .+:\d+|.*\.rs:\d+:\d+:.*|.*\.py:\d+:.*)$')


# --- the concurrency gate ------------------------------------------------------

def _slots() -> int:
    try:
        return max(1, int(os.environ.get('KIT_CHECK_SLOTS', '2')))
    except ValueError:
        return 2


def _slot(block: bool = True):
    """Take one of KIT_CHECK_SLOTS test slots, shared across the main checkout and every worktree.

    Why a gate at all: an unattended run has N agents in N worktrees, and every one of them thinks it owns
    the machine. Five uncapped `cargo test -j$(nproc)` at once on the same box took about thirty minutes
    each — six times longer than running them one after another — because they fight over cores and the
    linker. One at a time is also wrong: a single run leaves the machine idle through every link step and
    every long single-threaded test. Two or three, each told to use its share of the cores, is the sweet
    spot, so each run takes a slot and is given `cores / slots` jobs.
    """
    n = _slots()
    waited = False
    while True:
        for i in range(n):
            l = plat.Lock(f'check{i}')
            # Deliberately override the path: plat.Lock() suffixes the per-agent instance (KIT_PCSX2), which
            # is right for the emulator — each agent has its own — and wrong here, where the whole point is
            # that every worktree on this machine queues in the *same* N slots.
            l.path = plat.rt(f'kit-check{i}.lock')
            if l.take(block=False):
                return l
        if not block:
            return None
        if not waited:
            print(f'waiting: {n} test runs already going (KIT_CHECK_SLOTS={n})', file=sys.stderr, flush=True)
            waited = True
        time.sleep(5)


def _jobs() -> int:
    return max(1, (os.cpu_count() or 2) // _slots())


# --- the build-directory size cap ---------------------------------------------

def _cap_build_dirs(cwd: Path) -> list[str]:
    """Clean a build directory that has grown past KIT_BUILD_CAP_GB (default 80 GB), and say so.

    Incremental build directories only ever grow: no build system deletes the artifacts of a dependency
    version, a feature set or a target you no longer use. In the project this kit comes from, one checkout's
    `target/` reached 230 GB and the eight parallel worktrees together 1.2 TB, which is how an unattended run
    ends at four in the morning with a full disk and nothing committed. Starting one over is cheap when a
    compiler cache (sccache, ccache) is in play.

    Configure with `[build] dirs = ["target", "target-*"]` and `[build] clean = "cargo clean --target-dir
    {dir}"`. Set `dirs = []` (or KIT_BUILD_CAP_GB=0) to switch the whole feature off.
    """
    try:
        cap = float(os.environ.get('KIT_BUILD_CAP_GB', '80'))
    except ValueError:
        cap = 80.0
    if cap <= 0:
        return []
    build = cfg(False).raw.get('build', {})
    pats = build.get('dirs', ['target', 'target-*'])
    clean = build.get('clean', 'cargo clean -q --target-dir {dir}')
    if not pats or not clean:
        return []
    said = []
    for pat in pats:
        for d in sorted(cwd.glob(pat)):
            if not d.is_dir():
                continue
            gb = _du_gb(d)
            if gb <= cap:
                continue
            said.append(f'{d.name} is {gb:.0f} GB (cap {cap:.0f}): cleaning it before the tests')
            print(said[-1], flush=True)
            subprocess.run(clean.format(dir=str(d)), shell=True, cwd=str(cwd),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return said


def _du_gb(d: Path) -> float:
    if shutil.which('du'):   # not on Windows, and running a missing program raises rather than failing
        r = subprocess.run(['du', '-s', '--block-size=1G', str(d)], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.split():
            return float(r.stdout.split()[0])
    total = 0
    for dirpath, _, files in os.walk(d):  # no du (Windows): walk it, which is slower but rare
        for f in files:
            try:
                total += os.stat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    return total / 1e9


# --- running and summarising ---------------------------------------------------

def _log_path() -> Path:
    return cfg(False).ctx('notes/check.log')


def _run(cmd: str, extra: list[str], cwd: Path, log: Path, env=None) -> int:
    """Run a configured command with the extra args appended, everything into `log`."""
    jobs = _jobs()
    if '{jobs}' in cmd:
        cmd = cmd.replace('{jobs}', str(jobs))
    # Quoting is the local shell's, not POSIX's: `cmd.exe` would hand a test runner the quotes themselves.
    full = cmd + ((' ' + plat.quote_args(extra)) if extra else '')
    e = {**os.environ, 'KIT_JOBS': str(jobs), 'RUST_TEST_THREADS': os.environ.get('RUST_TEST_THREADS', str(jobs)),
         'CARGO_BUILD_JOBS': os.environ.get('CARGO_BUILD_JOBS', str(jobs)), **(env or {})}
    with open(log, 'w', encoding='utf-8') as f:
        f.write(f'$ {full}\n(cwd {cwd}, {jobs} jobs)\n\n')
        f.flush()
        return subprocess.run(full, shell=True, cwd=str(cwd), stdout=f, stderr=subprocess.STDOUT, env=e).returncode


def _passed_count(text: str) -> int | None:
    for pat in PASS_PATTERNS:
        hits = [int(m) for m in re.findall(pat, text)]
        if hits:
            return sum(hits)
    return None


def _cap(lines: list[str]) -> list[str]:
    out = [l[:MAX_COLS] for l in lines if l.strip()][:MAX_LINES]
    if len(lines) > MAX_LINES:
        out.append(f'… ({len(lines) - MAX_LINES} more lines in the log)')
    return out


def summarise(rc: int, log: Path, what: str = 'TESTS') -> list[str]:
    """What `kit check` prints. One line on success; on failure, build errors *or* test failures, never both."""
    text = log.read_text(encoding='utf-8', errors='replace') if log.exists() else ''
    if rc == 0:
        n = _passed_count(text)
        return [f'{what} PASSED: {n} tests' if n else f'{what} PASSED']
    errs = [l for l in text.splitlines() if cfg(False).build_error_re.search(l)]
    if errs:
        # A build failure means "you broke the code you just wrote" — a completely different next move from a
        # failing test, so it gets its own unmissable headline and the test output is not shown at all.
        return [f'BUILD FAILED (treat this as a build failure, no tests ran):', *_cap(errs),
                f'(full log: {_rel(log)})']
    body = _failures(text)
    return [f'{what} FAILED (this is a test result, the build is fine):', *_cap(body), f'(full log: {_rel(log)})']


def _failures(text: str) -> list[str]:
    """Just the failure messages. Prefers a runner's own failures block, else the failure-shaped lines."""
    lines = text.splitlines()
    block, out = False, []
    for l in lines:  # cargo/pytest style: a "failures:" section listing each panic
        if re.match(r'^(failures:|FAILED tests:|=+ FAILURES =+)\s*$', l.strip()):
            block = True
            continue
        if block:
            if re.match(r'^(test result:|=+ (short test )?summary|note: run with)', l.strip()):
                block = False
                continue
            out.append(l)
    out = [l for l in out if l.strip()]
    if out:
        return out
    out = [l for l in lines if FAIL_LINE.match(l)]
    return out or lines[-40:]


def _rel(p: Path) -> str:
    try:
        return str(Path(p).relative_to(ROOT))
    except ValueError:
        return str(p)


# --- kit check ---------------------------------------------------------------

def cmd_check(argv: list[str]) -> int:
    from ps2kit.cli import parser  # function-local: nothing in the kit imports cli at module level
    ap = parser('check', __doc__)
    ap.add_argument('extra', nargs='*', metavar='ARG',
                    help='passed through to [build] test, e.g. kit check -p sim --test flights')
    ap.add_argument('--no-gate', action='store_true', help='skip the concurrency gate (one-off, interactive)')
    a, unknown = ap.parse_known_args(argv)
    extra = a.extra + unknown

    c = cfg(False)
    if not c.test_cmd:
        print('No test command configured. Set [build] test in kit.toml, e.g.\n'
              '    [build]\n    test = "cargo test -q"\n'
              'Until then there is no test gate, and `kit done` will refuse to commit.', file=sys.stderr)
        return 2
    cwd = Path.cwd() if (Path.cwd() / '.git').exists() else ROOT
    log = _log_path()
    lock = None if a.no_gate else _slot()
    try:
        _cap_build_dirs(cwd)
        rc = _run(c.test_cmd, extra, cwd, log)
    finally:
        if lock:
            lock.release()
    for l in summarise(rc, log):
        print(l)
    return 0 if rc == 0 else 1


# --- kit audit ---------------------------------------------------------------

# Ghidra's placeholder names, the shapes it generates for unnamed things.
DECOMP_NAME = re.compile(r'\b(?:FUN|DAT|SUB|LAB|UNK|OFF)_[0-9a-fA-F]{6,8}\b')
# Where a port's source might plausibly live. The audit reads text, so this keeps it off binaries.
CODE_SUFFIX = {'.rs', '.c', '.h', '.cc', '.cpp', '.hpp', '.cs', '.py', '.go', '.java', '.kt', '.ts',
               '.js', '.zig', '.swift', '.lua', '.nim', '.odin', '.d', '.m', '.mm'}
# The kit's own modules, its Ghidra scripts and its tests *parse* dumps whose format is literally
# `FUN_…` lines, so a placeholder name there is subject matter, not a leak.
TOOL_DIRS = ('ps2kit/', 'tests/', 'ghidra_scripts/')


def _address_re(addrs: list[int]) -> re.Pattern | None:
    """One pattern matching any of these addresses however it is spelled: `0x0035bc50`, `35bc50`,
    `0X35BC50`. The lookarounds stop `0x1004c00` from counting as a hit on `0x1004c0`."""
    if not addrs:
        return None
    alts = '|'.join(sorted({f'{a:x}' for a in addrs}, key=len, reverse=True))
    return re.compile(r'(?<![0-9a-zA-Z_])(?:0x)?0*(' + alts + r')(?![0-9a-zA-Z_])', re.I)


def audit(root: Path, files: list[str], known: dict[int, str]) -> list[str]:
    """`path:line: what` for every leak of a retail address or a decompiler-generated name into the
    files that get committed. Addresses come from the project's own address file, which is the only
    place that knows which numbers are real: an address nobody wrote down cannot be recognised, and
    guessing from the shape of a hex literal would flag every bitmask in the port.

    Addresses are looked for everywhere except the research notes and the journal, because that is
    exactly the rule — they belong there and nowhere else. Placeholder names are looked for in source
    files only, since prose that explains what to do about a `FUN_…` has to be able to name one."""
    hits, addr_re = [], _address_re(list(known))
    for rel in files:
        if rel.startswith('research/') or rel.startswith('context/'):
            continue
        p = root / rel
        try:
            text = p.read_text(errors='strict')
        except (OSError, UnicodeDecodeError):
            continue                        # binary, a symlink to nowhere, gone since `git ls-files`
        code = p.suffix.lower() in CODE_SUFFIX and not rel.startswith(TOOL_DIRS)
        for n, line in enumerate(text.splitlines(), 1):
            if addr_re:
                for m in addr_re.finditer(line):
                    a = int(m.group(1), 16)
                    hits.append(f'{rel}:{n}: {m.group(0)} is {known[a]} ({a:#010x})')
            if code:
                for m in DECOMP_NAME.finditer(line):
                    hits.append(f'{rel}:{n}: {m.group(0)} is a decompiler placeholder name')
    return hits


def _known_addresses() -> dict[int, str]:
    """Every address this project has written down: the address file, then `kit.toml`'s `[symbols]`."""
    from ps2kit import oracle
    c = cfg(False)
    known = {}
    try:
        for name, program, addr, _note in oracle.entries():
            known.setdefault(addr, f'{name} ({program})' if program else name)
    except SystemExit:
        pass                                # no address file yet: then there is nothing to leak
    for name, addr in c.symbols.items():
        known.setdefault(addr, name)
    return known


def _tracked(cwd: Path) -> list[str]:
    """Everything that is committed or about to be: `kit done` audits *before* it stages, so a brand
    new file — the likeliest place for a fresh leak — has to be in this list."""
    out = []
    for args in (['git', 'ls-files'], ['git', 'ls-files', '--others', '--exclude-standard']):
        r = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True)
        out += [l for l in r.stdout.splitlines() if l]
    return out


def run_audit(cwd: Path, quiet: bool = False) -> list[str]:
    """The audit, printed the way `check` prints: one line when it is clean."""
    known = _known_addresses()
    hits = audit(cwd, _tracked(cwd), known)
    if not hits:
        if not quiet:
            n = len(known)
            print(f'AUDIT ok    no retail address ({n} written down) and no decompiler name outside '
                  f'research/')
        return hits
    print(f'AUDIT FAILED  {len(hits)} leak(s) into committed files:', file=sys.stderr)
    for h in hits[:MAX_LINES]:
        print(f'  {h}', file=sys.stderr)
    if len(hits) > MAX_LINES:
        print(f'  … and {len(hits) - MAX_LINES} more', file=sys.stderr)
    print('\nAddresses belong in research/oracle.txt and journal entries; in the port, say what the\n'
          'function does instead. Rename the placeholders to what they are.', file=sys.stderr)
    return hits


def cmd_audit(argv: list[str]) -> int:
    from ps2kit.cli import parser
    ap = parser('audit', 'Check that no retail address or decompiler name reached a committed file.')
    ap.add_argument('--quiet', action='store_true', help='print nothing when it is clean')
    a = ap.parse_args(argv)
    cwd = Path.cwd() if (Path.cwd() / '.git').exists() else ROOT
    return 1 if run_audit(cwd, a.quiet) else 0


# --- kit done ----------------------------------------------------------------

def push(cwd: Path, branch: str, *, delete: bool = False) -> str:
    """Push `branch` to the remote `[git] push` names, merging the remote in first if it has moved.

    Returns one line to print, or '' when there is nothing to say: pushing is off, or this repo has no
    such remote. **Never fatal.** The commit is safe in the local repo either way, and a push that could
    not be made now is made by the next `kit done` or the next merge — so a dropped network does not
    turn a finished task into a failed one.

    It merges rather than rebases when the remote has moved: the worktree branches of a `./kit run` are
    based on these commits, and rebasing the branch they came from turns every later merge into a fight.
    """
    remote = cfg(False).push_remote
    if not remote:
        return ''

    def g(*a: str):
        return subprocess.run(['git', *a], cwd=str(cwd), capture_output=True, text=True)

    if g('remote', 'get-url', remote).returncode:
        return ''  # no remote of that name: nothing to push to, and nothing worth saying about it
    if delete:
        ok = not g('push', '-q', remote, '--delete', branch).returncode
        return f'pushed: dropped the merged {branch} from {remote}' if ok else ''
    if not g('push', '-q', remote, branch).returncode:
        return f'pushed {branch} → {remote}'
    if g('branch', '--show-current').stdout.strip() != branch:
        return f'! {branch} did not push (it is committed locally; push it by hand)'
    g('fetch', '-q', remote)
    if g('merge', '--no-edit', f'{remote}/{branch}').returncode:
        g('merge', '--abort')
        return (f'! {branch} did not push: {remote}/{branch} has moved and merging it conflicts. '
                f'Nothing is lost — resolve it by hand, then push.')
    if g('push', '-q', remote, branch).returncode:
        return f'! {branch} did not push (it is committed locally; push it by hand)'
    return f'pushed {branch} → {remote}, after merging {remote}/{branch} into it'




def cmd_done(argv: list[str]) -> int:
    from ps2kit.cli import parser
    ap = parser('done', __doc__)
    ap.add_argument('id', metavar='ID', help='the task, as in plan/TODO.md')
    ap.add_argument('message', metavar='MSG', help='the commit message')
    ap.add_argument('--blocked', metavar='WHY',
                    help='mark the task [~] instead of done: why, and what would unblock it')
    ap.add_argument('--skip-audit', action='store_true',
                    help='commit even though the address audit found something (say why in the journal)')
    a = ap.parse_args(argv)

    from ps2kit import plan  # function-local by design: plan.py is a leaf, so no import cycle
    c = cfg(False)
    cwd = Path.cwd() if (Path.cwd() / '.git').exists() else ROOT

    if c.test_cmd:
        lock = _slot()
        try:
            _cap_build_dirs(cwd)
            log = _log_path()
            rc = _run(c.test_cmd, [], cwd, log)
            for l in summarise(rc, log):
                print(l)
            if rc:
                print(f'\nNothing ticked, nothing committed. Fix it, or commit what is solid by hand and\n'
                      f'    kit done {a.id} "<msg>" --blocked "<why + what unblocks it>"', file=sys.stderr)
                return 1
            if c.lint_cmd:  # only when the tests passed: a lint failure on top of a test failure is noise
                llog = c.ctx('notes/lint.log')
                lrc = _run(c.lint_cmd, [], cwd, llog)
                for l in summarise(lrc, llog, what='LINT'):
                    print(l)
                if lrc:
                    print('\nNothing ticked, nothing committed (the lint is a gate too).', file=sys.stderr)
                    return 1
        finally:
            lock.release()
    else:
        print('! no [build] test in kit.toml: committing without a test gate', file=sys.stderr)

    if run_audit(cwd) and not a.skip_audit:
        print('\nNothing ticked, nothing committed. Move those into research/oracle.txt or the journal,\n'
              f'or, if this one is deliberate, kit done {a.id} "<msg>" --skip-audit', file=sys.stderr)
        return 1

    if a.blocked:
        plan.block(a.id, a.blocked)
        print(f'blocked {a.id}')
    else:
        plan.tick(a.id)
        print(f'ticked {a.id}')

    if subprocess.run(['git', 'add', '-A'], cwd=str(cwd)).returncode:
        print('git add failed', file=sys.stderr)
        return 1
    if not subprocess.run(['git', 'diff', '--cached', '--quiet'], cwd=str(cwd)).returncode:
        print('nothing to commit (the tick was already committed?)')
        return 0
    if subprocess.run(['git', 'commit', '-qm', a.message], cwd=str(cwd)).returncode:
        print('git commit failed', file=sys.stderr)
        return 1
    print(subprocess.run(['git', 'log', '-1', '--oneline'], cwd=str(cwd),
                         capture_output=True, text=True).stdout.strip())
    # Pushed here, not by hand: a task branch nobody ever pushed is work that exists on one disk, and the
    # one that gets left behind is always the one whose merge went wrong.
    here = subprocess.run(['git', 'branch', '--show-current'], cwd=str(cwd),
                          capture_output=True, text=True).stdout.strip()
    if here and (line := push(cwd, here)):
        print(line)
    if not a.blocked:
        print(f'Journal: `kit journal state {a.id}` — an entry whose newest file is still -PART means you '
              f'left something for the next session.')
    return 0


if __name__ == '__main__':
    sys.exit(cmd_check(sys.argv[1:]))
