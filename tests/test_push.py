#!/usr/bin/env python3
"""Self-test for the push step: what `./kit done` and `./kit run` do with a remote.

    python3 tests/test_push.py          (no kit.toml needed, no network, no emulator, under a second)

Why this one: pushing is the step nobody watches. It runs at the end of a wrap-up an agent is no longer
reading and at the end of a merge nobody is sitting in front of, so the two ways it can be wrong are both
silent — it quietly does not push (work sits on one disk until the disk is gone), or it pushes over
somebody by forcing or rebasing (work is gone with no copy left). Both are exercised here against real
git repos: a plain push, a remote that moved underneath, a remote that conflicts, an agent's branch and
its removal once merged, and the two ways pushing can be switched off.
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
    with tempfile.TemporaryDirectory() as tmp:
        bare = Path(tmp) / 'origin.git'
        sp.run(['git', 'init', '-q', '--bare', '-b', 'main', str(bare)])
        a = repo(tmp, 'a')
        g('remote', 'add', 'origin', str(bare), cwd=a)
        remote_as('origin')

        check(checks.push(a, 'main') == 'pushed main → origin', 'a plain push says so')
        check(g('rev-parse', 'main', cwd=a).stdout == g('rev-parse', 'main', cwd=bare).stdout,
              'and the remote really has it')

        # somebody else pushed first: the remote has to be merged in before this one can land
        b = Path(tmp) / 'b'
        sp.run(['git', 'clone', '-q', str(bare), str(b)])
        g('config', 'user.email', 't@example.invalid', cwd=b)
        g('config', 'user.name', 'test', cwd=b)
        (b / 'other').write_text('theirs\n', encoding='utf-8')
        g('add', '-A', cwd=b)
        g('commit', '-qm', 'theirs', cwd=b)
        g('push', '-q', 'origin', 'main', cwd=b)
        commit(a, 'one\nmine\n', 'mine')
        check('after merging origin/main' in checks.push(a, 'main'),
              'a moved remote is merged in, then pushed')
        check(g('rev-parse', 'main', cwd=a).stdout == g('rev-parse', 'main', cwd=bare).stdout,
              'and both ends agree again')
        check((a / 'other').exists(), "the other side's commit came back with the merge")
        check(not g('log', '--format=%s', '-1', cwd=a).stdout.startswith('mine'),
              'it merged rather than rebased, so the local commits keep their identity')

        # and when that merge conflicts, it backs out and says so rather than forcing anything
        g('pull', '-q', '--no-rebase', '--no-edit', 'origin', 'main', cwd=b)
        (b / 'f').write_text('one\ntheirs-conflicting\n', encoding='utf-8')
        g('add', '-A', cwd=b)
        g('commit', '-qm', 'theirs2', cwd=b)
        g('push', '-q', 'origin', 'main', cwd=b)
        commit(a, 'one\nmine-conflicting\n', 'mine2')
        before = g('rev-parse', 'HEAD', cwd=a).stdout
        line = checks.push(a, 'main')
        check(line.startswith('!') and 'conflicts' in line, 'a conflicting remote is reported, not forced')
        check(g('rev-parse', 'HEAD', cwd=a).stdout == before, 'and the local branch is left as it was')
        check(not g('status', '--porcelain', cwd=a).stdout.strip(), 'with no half-finished merge behind it')

        # an agent's branch, and its removal from the remote once the runner has merged it
        g('reset', '-q', '--hard', 'HEAD', cwd=a)
        g('checkout', '-q', '-b', 'task/T9', cwd=a)
        commit(a, 'one\nmine-conflicting\nT9\n', 'T9 work')
        check(checks.push(a, 'task/T9') == 'pushed task/T9 → origin', "an agent's branch pushes")
        check('task/T9' in g('branch', '-a', cwd=bare).stdout, 'and the remote has it')
        check('dropped the merged task/T9' in checks.push(a, 'task/T9', delete=True),
              'a merged branch is dropped from the remote')
        check('task/T9' not in g('branch', '-a', cwd=bare).stdout, 'and it is really gone')

        # the two ways there is nothing to do: no such remote, and pushing switched off
        check(checks.push(repo(tmp, 'c'), 'main') == '', 'a repo with no origin says nothing')
        remote_as(None)
        check(checks.push(a, 'main') == '', '[git] push = false says nothing')


if __name__ == '__main__':
    test_push()
    print(f'\n{ok} passed, {fail} failed')
    sys.exit(1 if fail else 0)
