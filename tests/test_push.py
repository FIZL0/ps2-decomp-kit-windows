#!/usr/bin/env python3
"""Self-test for the push step: what `kit done` and `kit run` do with a remote.

    python3 tests/test_push.py          (no kit.toml needed, no network, no emulator, under a second)

Why this one: pushing is the step nobody watches. It runs at the end of a wrap-up an agent is no longer
reading and at the end of a merge nobody is sitting in front of, so the two ways it can be wrong are both
silent — it quietly does not push (work sits on one disk until the disk is gone), or it pushes over
somebody by forcing or rebasing (work is gone with no copy left). Both are exercised here against real
git repos: a branch pushed, a remote that moved underneath, a remote that conflicts, a merged branch
dropped from the remote only once the remote has the merge, and the two ways pushing can be switched off.
"""
from __future__ import annotations

import subprocess as sp
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ps2kit import checks  # noqa: E402

ok = fail = 0


def check(cond, what: str) -> None:
    global ok, fail
    print(('  ok   ' if cond else '  FAIL ') + what)
    ok, fail = ok + bool(cond), fail + (not cond)


def g(*a: str, cwd):
    return sp.run(['git', *a], cwd=str(cwd), capture_output=True, text=True)


def remote_as(name: str | None) -> None:
    """Stand in for `[git] push` in kit.toml, so the suite needs no config of its own."""
    checks.cfg = lambda required=False: types.SimpleNamespace(push_remote=name)


def repo(tmp: str, name: str) -> Path:
    d = Path(tmp) / name
    d.mkdir()
    g('init', '-q', '-b', 'main', cwd=d)
    g('config', 'user.email', 't@example.invalid', cwd=d)
    g('config', 'user.name', 'test', cwd=d)
    commit(d, 'one\n', 'one')
    return d


def commit(d: Path, text: str, msg: str) -> None:
    (d / 'f').write_text(text, encoding='utf-8')
    g('add', '-A', cwd=d)
    g('commit', '-qm', msg, cwd=d)


def test_push() -> None:
    print('push: the step at the end of done and of every merge')
    real_root = checks.ROOT
    with tempfile.TemporaryDirectory() as tmp:
        bare = Path(tmp) / 'origin.git'
        sp.run(['git', 'init', '-q', '--bare', '-b', 'main', str(bare)])
        a = repo(tmp, 'a')
        g('remote', 'add', 'origin', str(bare), cwd=a)
        remote_as('origin')
        # `a` stands in for the main checkout as well as for the caller, because one of the two things
        # that make a branch shared is "what the main checkout has out". Left unpinned, every check here
        # would quietly depend on the branch the *kit's own* checkout happens to be on today — which is
        # how this suite passed on a fix branch and failed the moment main was checked out again.
        checks.ROOT = a

        # Any branch goes out, the shared one included: `./kit run` pushes the merge target once the
        # merge has passed the test gate, and a wrap-up pushes what it just committed.
        check(checks.push(a, 'main') == 'pushed main → origin', 'a plain push says so')
        check(g('rev-parse', 'main', cwd=a).stdout == g('rev-parse', 'main', cwd=bare).stdout,
              'and the remote really has it')

        # `shared_name` survives for one caller only: `drop_if_merged`, which has to know which remote
        # branch should contain a merge before it deletes anything. The ref a clone writes decides it.
        g('symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/release', cwd=a)
        check(checks.shared_name(a, 'origin') == 'release', "the remote's own HEAD decides when it is set")
        g('symbolic-ref', '-d', 'refs/remotes/origin/HEAD', cwd=a)
        check(checks.shared_name(a, 'origin') == 'main', 'and the usual names stand in when it is not')

        # a task branch, which is what an agent's wrap-up pushes
        g('checkout', '-q', '-b', 'task/T9', cwd=a)
        commit(a, 'one\nT9\n', 'T9 work')
        check(checks.push(a, 'task/T9') == 'pushed task/T9 → origin', "an agent's branch pushes")
        check('task/T9' in g('branch', '-a', cwd=bare).stdout, 'and the remote has it')

        # somebody else pushed that branch first: the remote has to be merged in before this one can land
        b = Path(tmp) / 'b'
        sp.run(['git', 'clone', '-q', '-b', 'task/T9', str(bare), str(b)], capture_output=True)
        g('config', 'user.email', 't@example.invalid', cwd=b)
        g('config', 'user.name', 'test', cwd=b)
        (b / 'other').write_text('theirs\n', encoding='utf-8')
        g('add', '-A', cwd=b)
        g('commit', '-qm', 'theirs', cwd=b)
        g('push', '-q', 'origin', 'task/T9', cwd=b)
        commit(a, 'one\nT9\nmine\n', 'mine')
        check('after merging origin/task/T9' in checks.push(a, 'task/T9'),
              'a moved remote is merged in, then pushed')
        check(g('rev-parse', 'task/T9', cwd=a).stdout == g('rev-parse', 'task/T9', cwd=bare).stdout,
              'and both ends agree again')
        check((a / 'other').exists(), "the other side's commit came back with the merge")
        check(not g('log', '--format=%s', '-1', cwd=a).stdout.startswith('mine'),
              'it merged rather than rebased, so the local commits keep their identity')

        # and when that merge conflicts, it backs out and says so rather than forcing anything
        g('pull', '-q', '--no-rebase', '--no-edit', 'origin', 'task/T9', cwd=b)
        (b / 'f').write_text('one\ntheirs-conflicting\n', encoding='utf-8')
        g('add', '-A', cwd=b)
        g('commit', '-qm', 'theirs2', cwd=b)
        g('push', '-q', 'origin', 'task/T9', cwd=b)
        commit(a, 'one\nmine-conflicting\n', 'mine2')
        before = g('rev-parse', 'HEAD', cwd=a).stdout
        line = checks.push(a, 'task/T9')
        check(line.startswith('!') and 'conflicts' in line, 'a conflicting remote is reported, not forced')
        check(g('rev-parse', 'HEAD', cwd=a).stdout == before, 'and the local branch is left as it was')
        check(not g('status', '--porcelain', cwd=a).stdout.strip(), 'with no half-finished merge behind it')

        # A merged task branch is dropped from the remote — but only once the remote has the merge that
        # contains it. Until then that branch is the only copy the remote has of the work, and dropping
        # it would take the work off the remote altogether.
        # (resolving what the conflict above left on the remote, so the merge really does contain it —
        # a branch the merge does not contain is one `drop_if_merged` is right to keep)
        g('fetch', '-q', 'origin', cwd=a)
        g('merge', '-q', '--no-edit', '-X', 'ours', 'origin/task/T9', cwd=a)
        g('checkout', '-q', 'main', cwd=a)
        g('merge', '-q', '--no-edit', 'task/T9', cwd=a)
        line = checks.drop_if_merged(a, 'task/T9')
        check('kept task/T9' in line and 'does not have the merge yet' in line,
              'a merged branch stays while the remote has no copy of the merge')
        check('task/T9' in g('branch', '-a', cwd=bare).stdout, 'and it really is still there')
        g('push', '-q', 'origin', 'main', cwd=a)        # the human publishes the merge
        check('dropped task/T9' in checks.drop_if_merged(a, 'task/T9'),
              'once the remote has the merge, the branch is redundant')
        check('task/T9' not in g('branch', '-a', cwd=bare).stdout, 'and it is really gone')
        check(checks.drop_if_merged(a, 'task/T9') == '', 'a branch the remote never had says nothing')

        # the two ways there is nothing to do: no such remote, and pushing switched off
        check(checks.push(repo(tmp, 'c'), 'task/T1') == '', 'a repo with no origin says nothing')
        remote_as(None)
        check(checks.push(a, 'task/T9') == '', '[git] push = false says nothing')
        check(checks.drop_if_merged(a, 'task/T9') == '', 'and drops nothing either')
    checks.ROOT = real_root


if __name__ == '__main__':
    test_push()
    print(f'\n{ok} passed, {fail} failed')
    sys.exit(1 if fail else 0)
