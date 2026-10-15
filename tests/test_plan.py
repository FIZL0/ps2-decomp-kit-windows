#!/usr/bin/env python3
"""Self-test for the agent layer: the task list, the journal, the check summary and the guard hook.

    python3 tests/test_plan.py          (no kit.toml needed, no network, no emulator, under a second)

Why these and not more: `plan.py` is the one module every session touches dozens of times, and it edits
files in place. A bug there does not raise — it quietly eats a task line, and nobody notices until a
"finished" task turns out never to have been done. So every mutator is exercised as a round trip in a
temporary repo and the result is read back as text, the way a human would see it. The rest of the layer
gets the checks that are cheap: the states the journal recognises, the build-failed/test-failed split that
decides an agent's next move, and the guard hook's own payload suite.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ps2kit import plan  # noqa: E402
from ps2kit import journal as J  # noqa: E402
from ps2kit import checks  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FAILED: list[str] = []


def check(ok, what, extra=''):
    print(('  ok   ' if ok else '  FAIL ') + what + ('' if ok else f'\n         {extra}'))
    if not ok:
        FAILED.append(what)


def eq(got, want, what):
    check(got == want, what, f'got {got!r}\n         want {want!r}')


def has(text, pat, what):
    check(re.search(pat, text, re.M) is not None, what, f'no /{pat}/ in:\n' + text[:800])


# --- the task list ------------------------------------------------------------

def test_plan_roundtrip():
    print('plan: add / next / move / note / block / unblock / tick / archive')
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / 'plan'
        todo, done = plan.ensure(d / 'TODO.md', d / 'DONE.md')
        check(os.path.exists(todo) and os.path.exists(done), 'ensure creates both files in a fresh checkout')
        kw = dict(todo=todo, done=done)

        a = plan.add('D', 'open the disc → kit.toml', **kw)
        b = plan.add('D', 'the boot ELF → kit.toml · docs/CONTEXT.md', **kw)
        c = plan.add('F', 'the EE FPU → crates/ps2-float/src/fpu.rs', **kw)
        eq((a, b, c), ('D1', 'D2', 'F1'), 'add hands out the next free id per prefix')
        # add puts each new task at the top, so the newest is first
        eq(plan.next_task(todo=todo), 'F1', 'next_task is the first open task')

        plan.move('D1', 'top', **{'todo': todo})
        eq(plan.next_task(todo=todo), 'D1', 'move <ID> top makes it the next task')
        plan.move('F1', 'after', 'D1', todo=todo)
        ids = [l[4:] for l in plan.index(todo) if l.startswith('[ ] ')]
        eq(ids, ['D1', 'F1', 'D2'], 'move <ID> after <ID> puts it straight behind')

        plan.note('D2', 'the region matters: a EU disc is a different game', todo=todo)
        has(Path(todo).read_text(), r'\*\*D2\*\*.*different game', 'note appends to the task line')

        plan.block('F1', 'needs a BIOS — plan/HUMAN.md', todo=todo)
        text = Path(todo).read_text()
        has(text, r'- \[~\] \*\*F1\*\*.*BLOCKED: needs a BIOS', 'block marks [~] and says what would unblock')
        eq(plan.next_task(todo=todo), 'D1', 'a blocked task is not the next task')
        plan.unblock('F1', 'BIOS installed', todo=todo)
        text = Path(todo).read_text()
        check('BLOCKED' not in text, 'unblock drops the BLOCKED note')
        has(text, r'- \[ \] \*\*F1\*\*.*BIOS installed', 'unblock keeps the line and notes why')

        # a long form mirrors the tick, so the two never disagree
        (d / 'D1.md').write_text('# D1\n\n- [ ] **D1** open the disc\n', encoding='utf-8')
        moved = plan.tick('D1', todo=todo, done=done, plan_dir=d)
        eq(moved, 1, 'tick archives the ticked line')
        check('**D1**' not in Path(todo).read_text(), 'the ticked task is gone from TODO.md')
        has(Path(done).read_text(), r'- \[x\] \*\*D1\*\* open the disc', 'with its text intact in DONE.md')
        has((d / 'D1.md').read_text(), r'- \[x\] \*\*D1\*\*', 'and ticked in its own plan/<ID>.md')
        eq(plan.next_id('D', [todo, done]), 'D3', 'ids are never reused: D1 is archived, the next is D3')

        # a hand-tick or a tick that arrived in a merge: archive sorts it out, and is safe to repeat
        ls = Path(todo).read_text().replace('- [ ] **D2**', '- [x] **D2**')
        Path(todo).write_text(ls, encoding='utf-8')
        plan._cache.clear()
        eq(plan.archive(todo, done), 1, 'archive picks up a tick nobody moved')
        eq(plan.archive(todo, done), 0, 'archive is a no-op the second time')
        eq([l[4:] for l in plan.index(todo) if l.startswith('[ ] ')], ['F1'], 'one open task left')

        check(not list(Path(d).glob('*.tmp')), 'no .tmp files left behind by the atomic writes')

        # an unchanged file is never rewritten: mtime-stamped caches and git both care
        before = os.stat(todo).st_mtime_ns
        plan._write(todo, plan.lines(todo))
        eq(os.stat(todo).st_mtime_ns, before, '_write is a no-op when the content is identical')

        missing = subprocess.run([sys.executable, '-c',
                                  'import sys;sys.path.insert(0,%r);from ps2kit import plan;'
                                  'plan._find(["x"],"ZZ9")' % str(ROOT)], capture_output=True, text=True)
        check(missing.returncode != 0 and 'no task' in missing.stderr,
              'an unknown id fails loudly instead of silently doing nothing')


def test_plan_index_and_sections():
    print('plan: index, section lookup, files on a line')
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / 'plan'
        todo, done = plan.ensure(d / 'TODO.md', d / 'DONE.md')
        Path(todo).write_text('\n'.join([
            '# TODO', '', '### Next (in this order)', '',
            '- [ ] **R1** decompile → kit.toml · ghidra_scripts/ | t: none | j: r1-decompile',
            '  - a sub-line that belongs to R1',
            '- [~] **R2** symbols → kit.toml BLOCKED: needs R1',
            '', '### Stretch', '', '- [ ] **K1** recipes → docs/PROVING.md', '']), encoding='utf-8')
        plan._cache.clear()
        eq(plan.index(todo),
           ['# TODO', '### Next (in this order)', '[ ] R1', '[~] R2', '### Stretch', '[ ] K1'],
           'index is headings and ids only — the whole backlog in a screenful')
        eq(plan.section(todo, 'R1'),
           ['- [ ] **R1** decompile → kit.toml · ghidra_scripts/ | t: none | j: r1-decompile',
            '  - a sub-line that belongs to R1'],
           'a task by id brings its sub-lines with it')
        eq(plan.section(todo, 'Stretch'), ['### Stretch', '', '- [ ] **K1** recipes → docs/PROVING.md'],
           'a section by heading substring')
        eq(plan.section(todo, 'nope'), [], 'an unknown name is empty, not an error')
        eq(plan.files_on_line('→ docs/CONTEXT.md · ps2kit/plan.py | t: crates/x/tests/a.rs', root=ROOT),
           ['docs/CONTEXT.md', 'ps2kit/plan.py'],
           'files_on_line finds the paths on a line, and stops at the tests (kit run mode d compares them)')
        eq(plan.files_on_line('→ crates/game-sim/src/rng.rs', root=ROOT), ['crates/game-sim/src/rng.rs'],
           'a file the task will create still counts as the task\'s file')


def test_brief():
    print('plan: brief')
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / 'plan').mkdir()
        (root / 'docs').mkdir()
        (root / 'docs/CONTEXT.md').write_text('a\nb\nc\n', encoding='utf-8')
        (root / 'plan/TODO.md').write_text(
            '# TODO\n\n### Next\n\n- [ ] **D3** the boot ELF → docs/CONTEXT.md · crates/new.rs '
            '| t: none | j: d3-boot-elf\n', encoding='utf-8')
        (root / 'plan/DONE.md').write_text('# DONE\n', encoding='utf-8')
        (root / 'plan/D3.md').write_text('# D3 — the boot ELF\n\nThe long form.\n', encoding='utf-8')
        j = root / 'research/journal/2026-01-02-d3-boot-elf'
        j.mkdir(parents=True)
        (j / '1-SEGMENTS-PART.md').write_text('# first pass\n', encoding='utf-8')
        (j / '2-OVERLAYS-READY.md').write_text('# needs the human\n\nthe newest word.\n', encoding='utf-8')
        out = '\n'.join(plan.brief('D3', root=root))
        has(out, r'## Task D3', 'brief leads with the task')
        has(out, r'\*\*D3\*\* the boot ELF', 'brief pastes the task line')
        has(out, r'## plan/D3\.md', 'brief pastes the long form')
        has(out, r'The long form\.', 'brief pastes the long form body')
        has(out, r'1-SEGMENTS-PART\.md · 2-OVERLAYS-READY\.md', 'brief lists the journal files')
        has(out, r'the newest word\.', 'brief pastes the NEWEST journal entry (whose move it is)')
        check('first pass' not in out, 'brief does not paste the older entries')
        has(out, r'`docs/CONTEXT\.md` \(3 lines\)', 'brief gives each file its length')
        has(out, r'`crates/new\.rs` \(missing — this task creates it\)',
            'brief says plainly when a file does not exist yet')
        cut = plan.JOURNAL_MAX
        (j / '3-LONG-PART.md').write_text('x' * (cut + 500), encoding='utf-8')
        out = '\n'.join(plan.brief('D3', root=root))
        has(out, rf'first {cut} chars', 'a huge journal entry is capped, and says so')
        check(len(out) < cut + 4000, 'and the whole brief stays bounded')


def test_shipped_backlog():
    print('plan: the backlog this kit ships with')
    todo = ROOT / 'plan/TODO.md'
    ls = plan.lines(todo)
    ids = [m.group(3) for l in ls if (m := plan.ITEM.match(l))]
    eq(len(ids), len(set(ids)), 'every id in plan/TODO.md is unique')
    check(25 <= len(ids) <= 60, f'{len(ids)} tasks: a session each, enough to start a port')
    check(any(l.startswith('### Stretch') for l in ls), 'there is a ### Stretch section')
    check(all(' → ' in l or ' | ' in l for l in ls if plan.ITEM.match(l)),
          'every task line names the files it touches or the test it adds')
    for name in ('DONE.md', 'HUMAN.md', 'REFERENCE.md'):
        check((ROOT / 'plan' / name).exists(), f'plan/{name} exists')
    # the ids on the line are what `(after X)` and the runner match on
    for l in ls:
        if not plan.ITEM.match(l):
            continue
        if m := re.search(r'\(after (\w+)\)', l):
            check(m.group(1) in ids, f'(after {m.group(1)}) names a task that exists')


# --- the journal --------------------------------------------------------------

def test_journal():
    print('journal: new / state / finish / police')
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / 'research/journal'
        root.mkdir(parents=True)
        J._journal = lambda: root  # the only thing in the module that knows where the journal is
        p = J.new('D3', 'boot elf')
        check(p.exists(), 'journal new writes the entry')
        has(p.parent.name, r'^\d{4}-\d\d-\d\d-d3-boot-elf$', 'the directory is dated and names the task')
        has(p.name, r'^1-.*-PART\.md$', 'the first iteration is numbered 1 and PART (the agent continues)')
        body = p.read_text()
        has(body, r'## Not verified / not 1:1', 'the stub demands the not-verified section')
        has(body, r'\bnone\b', 'and says what to write when there is nothing to declare')
        eq(J.state_of(p), 'PART', 'state_of reads the state out of the file name')
        p2 = J.new('D3', 'boot elf', 'overlays')
        eq(p2.name.split('-')[0], '2', 'the next entry in the same directory is iteration 2')
        f = J.finish(p2)
        eq(f.name.split('-')[-1], 'FINAL.md', 'finish renames the newest entry to FINAL')
        check(not p2.exists(), 'and the PART name is gone')
        eq([x.name for x in J.entries('D3')], [p.parent.name], 'entries finds the task\'s directory')
        (p.parent / 'shot.png').write_bytes(b'\x89PNG\r\n\x1a\n')
        bad = J.police()
        check(any('shot.png' in c for c in bad),
              'police spots a binary under the journal (it belongs in context/)', str(bad))


# --- the check summary --------------------------------------------------------

def test_check_summary():
    print('checks: the build-failed / test-failed split')
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / 'check.log'
        log.write_text('running 41 tests\ntest result: ok. 41 passed; 0 failed\n', encoding='utf-8')
        out = checks.summarise(0, log)
        eq(len(out), 1, 'a passing run prints exactly one line')
        has(out[0], r'TESTS PASSED: 41 tests', 'and says how many tests passed')

        log.write_text('   Compiling game-sim v0.1.0\nerror[E0308]: mismatched types\n'
                       ' --> crates/game-sim/src/move.rs:12:9\nerror: could not compile `game-sim`\n',
                       encoding='utf-8')
        out = '\n'.join(checks.summarise(1, log))
        has(out, r'^BUILD FAILED', 'a build failure gets its own headline, first line')
        has(out, r'E0308', 'with the compiler error')
        check('test result' not in out, 'and no test output to confuse it with a failing test')

        log.write_text('running 3 tests\ntest fpu::add_masks_guard_bit ... FAILED\n\nfailures:\n'
                       '    fpu::add_masks_guard_bit\n\ntest result: FAILED. 2 passed; 1 failed\n',
                       encoding='utf-8')
        out = '\n'.join(checks.summarise(1, log))
        has(out, r'^TESTS FAILED \(this is a test result, the build is fine\)',
            'a failing test says so unmistakably — it is a different next move from a build failure')
        has(out, r'fpu::add_masks_guard_bit', 'and names the test')

        log.write_text('\n'.join([f'line {i} ' + 'x' * 500 for i in range(400)]) + '\nerror: boom\n',
                       encoding='utf-8')
        out = checks.summarise(1, log)
        check(len(out) <= 62 and max(len(l) for l in out) <= 320,
              'a huge log is capped to a screenful and 300-odd columns', f'{len(out)} lines')
        has('\n'.join(out), r'full log: ', 'and points at the full copy on disk')


def test_audit():
    print('checks: the address audit, which is the only thing enforcing "no retail address in the port"')
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        known = {0x0035bc50: 'walk_ramp (gameplay)', 0x1f4a20: 'frame'}
        files = {
            'crates/sim/src/walk.rs': 'let t = ramp(0x0035bc50);\n',          # the leak that matters
            'crates/sim/src/turn.rs': 'FUN_0012abcd(x);  // and a placeholder name\n',
            'crates/sim/src/ok.rs':   'let mask = 0x3f800000;  // 1.0f\n',     # a constant, not an address
            'docs/NOTES.md':          'the walker is at 35bc50\n',            # prose counts too
            'ps2kit/decomp.py':       "HEADER = re.compile('FUN_([0-9a-f]+)')\n",  # the dump's own format
            'research/oracle.txt':    'walk_ramp gameplay 0x0035bc50\n',      # where it belongs
            'research/journal/2026-10-15-t1-walk/README.md': 'ported 0x0035bc50\n',
        }
        for rel, text in files.items():
            f = root / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text, encoding='utf-8')
        hits = checks.audit(root, list(files), known)
        where = {h.split(':')[0] for h in hits}
        eq(sorted(where), ['crates/sim/src/turn.rs', 'crates/sim/src/walk.rs', 'docs/NOTES.md'],
           'an address or a placeholder name is flagged wherever it is committed, prose included')
        check(not [h for h in hits if 'research/' in h],
              'research notes and the journal are where addresses are *supposed* to live')
        check(not [h for h in hits if 'ok.rs' in h], 'a float bit pattern is not an address')
        check(not [h for h in hits if 'ps2kit/' in h],
              "the kit's own modules parse FUN_ names for a living")
        has('\n'.join(hits), r'walk\.rs:1: 0x0035bc50 is walk_ramp', 'a hit says what the address is')
        has('\n'.join(hits), r'NOTES\.md:1: 35bc50 is walk_ramp', 'however it was spelled')

        bare = checks.audit(root, list(files), {})
        eq([h.split(':')[0] for h in bare], ['crates/sim/src/turn.rs'],
           'with nothing written down no address can be recognised, and nothing is guessed from a hex '
           'literal — only the placeholder name, whose shape is unmistakable, is still caught')

        (root / 'crates/sim/src/near.rs').write_text('let x = 0x0035bc500;\n', encoding='utf-8')
        eq(checks.audit(root, ['crates/sim/src/near.rs'], known), [],
           'a longer literal that merely starts with an address is not a hit')


# --- the hooks ----------------------------------------------------------------

def hook(payload, *args, cwd=None):
    r = subprocess.run([sys.executable, str(ROOT / 'ps2kit/hooks/guard.py'), *args],
                       input=json.dumps(payload), capture_output=True, text=True, cwd=cwd)
    return r.returncode, r.stdout + r.stderr


def test_guard():
    print('guard: its own payload suite, plus the cases a session hits daily')
    r = subprocess.run([sys.executable, str(ROOT / 'ps2kit/hooks/guard.py'), '--self-test'],
                       capture_output=True, text=True)
    check(r.returncode == 0, 'guard.py --self-test passes', r.stdout[-1500:] + r.stderr[-500:])

    rc, out = hook({'tool_name': 'Read', 'tool_input': {'file_path': 'docs/PS2.md'}})
    eq(rc, 0, 'an ordinary Read is allowed through')
    rc, out = hook({})
    eq(rc, 0, 'an empty payload is allowed (a hook must never crash the session)')
    rc, out = hook({'tool_name': 'Bash', 'tool_input': 'not a dict'})
    eq(rc, 0, 'a payload of the wrong shape is allowed, not an exception')

    rc, out = hook({'tool_name': 'Bash', 'tool_input': {'command': 'pg' + 'rep -f record_live'}})
    eq(rc, 0, 'a pattern search is not blocked...')
    check('--pgrep' in out, '...it is rewritten to the wrapper that skips the caller itself', out[:400])

    rc, out = hook({'tool_name': 'Bash', 'tool_input': {'command': 'kit check -p ps2-float'}})
    eq(rc, 0, 'kit check is allowed')
    check('3600000' in out, 'and its Bash timeout is raised, so a cold build is not killed at two minutes',
          out[:400])

    # a big binary container under the private root: cracking its format is all header reads, and the
    # bounded ones have to go through. Run the hook with its cwd there, the way a session's would be.
    with tempfile.TemporaryDirectory() as tmp:
        big = Path(tmp) / 'context/iso/BIG.BIN'
        big.parent.mkdir(parents=True)
        big.write_bytes(b'\0' * (700 * 1024))
        for cmd in ('xxd -l 512 context/iso/BIG.BIN',
                    'od -A x -N 256 -t x1z context/iso/BIG.BIN',
                    'dd if=context/iso/BIG.BIN bs=1 count=256 status=none',
                    'head -c 512 context/iso/BIG.BIN | xxd',
                    'xxd context/iso/BIG.BIN | head -40'):
            rc, out = hook({'tool_name': 'Bash', 'tool_input': {'command': cmd}}, cwd=tmp)
            eq(rc, 0, f'a capped header read is allowed: {cmd}')
        for cmd in ('xxd context/iso/BIG.BIN',
                    'od -A x -t x1z context/iso/BIG.BIN',
                    'cat context/iso/BIG.BIN',
                    'xxd -l 16 kit.toml.example; xxd context/iso/BIG.BIN'):
            rc, out = hook({'tool_name': 'Bash', 'tool_input': {'command': cmd}}, cwd=tmp)
            eq(rc, 2, f'the same tool with no cap is still refused: {cmd}')
            check('| head' in out, 'and the refusal names the cap that always works', out[:400])

        # …and a screenshot is not a dump at all: reading it is the point of taking it, and moving or
        # resizing one puts nothing in the conversation either.
        shot = Path(tmp) / 'context/shots/title.png'
        shot.parent.mkdir(parents=True)
        shot.write_bytes(b'\x89PNG\r\n\x1a\n' + b'\0' * (700 * 1024))
        rc, out = hook({'tool_name': 'Read', 'tool_input': {'file_path': str(shot)}}, cwd=tmp)
        eq(rc, 0, 'Read of a screenshot is allowed whatever it weighs')
        for cmd in ('cp context/shots/title.png /tmp/t.png', 'ls -l context/shots/title.png',
                    'magick context/shots/title.png -resize 50% small.png'):
            rc, out = hook({'tool_name': 'Bash', 'tool_input': {'command': cmd}}, cwd=tmp)
            eq(rc, 0, f'a command that only names the file is allowed: {cmd}')
        rc, out = hook({'tool_name': 'Bash', 'tool_input': {'command': 'cat context/shots/title.png'}},
                       cwd=tmp)
        eq(rc, 2, 'streaming a PNG to stdout is still refused')
        check('Read' in out, 'and the refusal names Read, not a code-reading command', out[:400])


def test_inbox():
    print('inbox: a ticket is handed over once, and holds a stop')
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, KIT_INBOX=str(Path(tmp) / 'inbox.md'), KIT_INBOX_SEEN=str(Path(tmp) / 'seen'))
        Path(env['KIT_INBOX']).write_text('- [ ] check the serve timing\n', encoding='utf-8')

        def run(payload):
            r = subprocess.run([sys.executable, str(ROOT / 'ps2kit/hooks/inbox.py')],
                               input=json.dumps(payload), capture_output=True, text=True, env=env)
            return r.returncode, r.stdout

        rc, out = run({'hook_event_name': 'PostToolUse'})
        check('serve timing' in out, 'a new ticket is handed to the agent', out[:300])
        rc, out = run({'hook_event_name': 'PostToolUse'})
        eq(out.strip(), '', 'the same ticket is never handed over twice')
        rc, out = run({'hook_event_name': 'Stop', 'stop_hook_active': False})
        check('"block"' in out, 'an agent stopping with an open ticket is told to keep going', out[:300])
        rc, out = run({'hook_event_name': 'Stop', 'stop_hook_active': True})
        eq((rc, out.strip()), (0, ''), 'stop_hook_active is honoured — a hook never loops a session')


# --- the runner's task picking ------------------------------------------------

def test_runner_picking():
    print('run: which task a free slot may take')
    from ps2kit import runner as R
    eq((R.family('L3a'), R.family('L3b'), R.family('M8f4')), ('L3', 'L3', 'M8'),
       'siblings share a family, so they never run side by side')
    ts = R.tasks('main')
    ids = [t[0] for t in ts]
    # Derived from the file, not written out here: the shipped backlog grows a task now and then, and a
    # literal id would make that a test failure instead of an edit.
    top = re.search(r'^- \[ \] \*\*(\S+?)\*\*', (ROOT / 'plan/TODO.md').read_text(), re.M).group(1)
    check(ids and ids[0] == top, 'the first startable task is the first line of the backlog', str(ids[:3]))
    check(not [t for t in ts if t[1].startswith('Stretch')], 'Stretch is never offered to a run')
    check('R1' not in ids, 'a task whose (after X) dependency is still open is not offered')
    first = R.pick({}, set(), 'd', 'main')
    eq(first[0], top, 'an empty run starts at the top')
    busy = {1: first}
    second = R.pick(busy, set(), 'd', 'main')
    check(second[1] != first[1], 'an ordered section runs one task at a time', str(second[:2]))
    check(not (second[2] & first[2]), 'and mode d keeps the second task off the first one\'s files')
    eq(R.pick({}, {t[0] for t in ts}, 'd', 'main'), None, 'nothing left once everything has been tried')


def test_stop_hook_never_crashes():
    print('run: the Stop hook that ends an idle session')
    for payload in ('{}', 'garbage', '{"transcript_path": "/nonexistent"}', '{"transcript_path": null}'):
        r = subprocess.run([sys.executable, str(ROOT / 'ps2kit/runner.py'), '--stop-hook'],
                           input=payload, capture_output=True, text=True)
        eq(r.returncode, 0, f'--stop-hook exits 0 on {payload[:28]}' + (': ' + r.stderr[-200:] if r.returncode else ''))


# --- the kit still imports and still explains itself --------------------------

def test_cli_help():
    print('cli: every command of this layer explains itself with no kit.toml')
    for cmd in ('plan', 'journal', 'check', 'done', 'run'):
        r = subprocess.run([sys.executable, str(ROOT / 'kit'), cmd, '--help'],
                           capture_output=True, text=True, cwd=str(ROOT))
        check(r.returncode == 0 and len(r.stdout) > 200,
              f'kit {cmd} --help prints help', (r.stdout + r.stderr)[:400])


def main() -> int:
    for t in (test_plan_roundtrip, test_plan_index_and_sections, test_brief, test_shipped_backlog,
              test_journal, test_check_summary, test_audit, test_guard, test_inbox, test_runner_picking,
              test_stop_hook_never_crashes, test_cli_help):
        t()
    print()
    if FAILED:
        print(f'{len(FAILED)} FAILED:')
        for f in FAILED:
            print(f'  - {f}')
        return 1
    print('all checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
