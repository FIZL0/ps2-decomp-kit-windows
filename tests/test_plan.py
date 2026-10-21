#!/usr/bin/env python3
"""Self-test for the agent layer: the kit's glue to agent-runner, the journal, the check summary and the guard.

    py -3 tests\test_plan.py          (no kit.toml needed, no network, no emulator, a few seconds)

The task list and the run loop are agent-runner's, tested there. Here: the settings the kit hands it, the
spec/port phase and its lock, the oracle join, `done`'s plan calls, the journal states, the
build-failed/test-failed split and the guard hook's payload suite.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import tomllib
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ps2kit import plan  # noqa: E402
from agent_runner import plan as P  # noqa: E402
from ps2kit import journal as J  # noqa: E402
from ps2kit import checks  # noqa: E402
from ps2kit.config import Config, cfg  # noqa: E402

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

def test_phase():
    print('plan: the spec/port phase that ends a brief, and the decompilation lock')
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        subprocess.run(['git', 'init', '-q', str(root / 'main')], check=True)
        git = lambda *a: subprocess.run(['git', '-C', str(root / 'main'), *a], capture_output=True, text=True)
        git('-c', 'user.email=t@example.invalid', '-c', 'user.name=t', 'commit', '-q', '--allow-empty', '-m', 'x')
        git('worktree', 'add', '-q', str(root / 'slot'))
        j = root / 'journal/2026-01-02-d3-boot-elf'
        j.mkdir(parents=True)
        for cwd in (root / 'main', root / 'slot'):
            eq(plan.phase('D3', root / 'journal', cwd), [], 'no SPEC.md: an ordinary task, no phase')
        (j / 'SPEC.md').write_text('# D3\n\nReads the table.\n\n## Open questions\n\n- what if it is empty?\n',
                                   encoding='utf-8')
        has('\n'.join(plan.phase('D3', root / 'journal', root / 'slot')), r'## Spec phase',
            'an open question: the spec phase')
        check(not (root / 'slot/.cleanroom').exists(), 'the spec phase reads the decompilation, so no lock')
        (j / 'SPEC.md').write_text('# D3\n\nReads the table.\n\n## Open questions\n\nnone\n', encoding='utf-8')
        out = '\n'.join(plan.phase('D3', root / 'journal', root / 'slot'))
        has(out, r'## Port phase', 'no open questions: the port phase')
        has(out, r'Reads the table\.', 'with the spec pasted in')
        check((root / 'slot/.cleanroom').exists(), 'the port phase locks the decompilation in a worktree')
        plan.phase('D3', root / 'journal', root / 'main')
        check(not (root / 'main/.cleanroom').exists(), 'but never in the main checkout')
        plan.cleanroom(False, cwd=str(root / 'slot'))
        check(not (root / 'slot/.cleanroom').exists(), '`kit done` lifts the lock')

def test_shipped_backlog():
    print('plan: the backlog this kit ships with')
    todo = ROOT / 'plan/TODO.md'
    ls = P.lines(todo)
    ids = [m.group(3) for l in ls if (m := P.ITEM.match(l))]
    eq(len(ids), len(set(ids)), 'every id in plan/TODO.md is unique')
    # The count is about the backlog this kit *ships*: enough to start a port, few enough to read in one
    # sitting. A port repo running this suite by hand has its own backlog, which shrinks as tasks land and
    # grows when one is split, so the bound is none of its business. A `[game]` table is what makes a
    # checkout a port — the Linux edition asks `checks.self_steps`, which this edition has no equivalent
    # of, so it asks the half of that rule which is about the repo rather than about the test gate.
    if not cfg(False).raw.get('game'):
        check(25 <= len(ids) <= 60, f'{len(ids)} tasks: a session each, enough to start a port')
    check(any(l.startswith('### Stretch') for l in ls), 'there is a ### Stretch section')
    check(all(' → ' in l or ' | ' in l for l in ls if P.ITEM.match(l)),
          'every task line names the files it touches or the test it adds')
    for name in ('DONE.md', 'HUMAN.md', 'REFERENCE.md'):
        check((ROOT / 'plan' / name).exists(), f'plan/{name} exists')
    # The ids on the line are what `(after X)` and the runner match on — and a *satisfied* dependency is
    # ticked into DONE.md and gone from TODO.md, which is how the runner reads it. Asserting the name
    # is still open would fail the moment a port lands its first dependency, i.e. exactly when the
    # mechanism is working.
    done = [m.group(3) for l in P.lines(ROOT / 'plan/DONE.md') if (m := P.ITEM.match(l))]
    for l in ls:
        if not P.ITEM.match(l):
            continue
        if m := re.search(r'\(after (\w+)\)', l):
            check(m.group(1) in ids or m.group(1) in done,
                  f'(after {m.group(1)}) names a task that exists, open or already done')


def test_atomic_write():
    print('config: a kit.toml write that a concurrent reader cannot catch half-written')
    from ps2kit.config import write_atomic
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / 'kit.toml'
        write_atomic(p, '[game]\niso = "a.iso"\n')
        eq(p.read_text(encoding='utf-8'), '[game]\niso = "a.iso"\n', 'a file that was not there is written')
        if os.name == 'posix':   # on Windows chmod only toggles read-only, so there is no mode to keep
            os.chmod(p, 0o644)
        write_atomic(p, '[game]\niso = "b.iso"\n')
        eq(p.read_text(encoding='utf-8'), '[game]\niso = "b.iso"\n', 'and replaced whole the next time')
        if os.name == 'posix':
            eq(oct(p.stat().st_mode & 0o777), '0o644', "with the mode it had, not the temp file's 0600")
        eq([f.name for f in Path(tmp).iterdir()], ['kit.toml'], 'and no temp file left behind')

        # The reason any of this is here: a run is several processes deep in one repo, and the old
        # truncate-then-write let `./kit check` read an empty [game] and blame the disc — "[game] iso is
        # empty", pointing at the ISO rather than at whoever was saving a symbol. Two details decide
        # whether this exercise can see that at all, and both are deliberate: the file is as big as a real
        # kit.toml (a two-line one is written too fast for a reader to catch), and the readers run until
        # the writes are done rather than a fixed number of times. Against the writer this replaced the
        # same loop gives ~1100 bad reads out of a few thousand, most of them that exact empty iso.
        body = ''.join(f'sym_{i:04d} = "0x{i:06x}"\n' for i in range(1500))   # ~22 KB, openmx's size
        bad: list[str] = []
        done = threading.Event()

        def reader() -> None:
            while not done.is_set():
                try:
                    if not tomllib.loads(p.read_text(encoding='utf-8')).get('game', {}).get('iso'):
                        bad.append('[game] iso read back empty')
                except Exception as e:      # noqa: BLE001 — any failure at all is the bug
                    bad.append(type(e).__name__)

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        for i in range(300):
            write_atomic(p, f'[game]\niso = "{i}.iso"\n[symbols]\n{body}')
        done.set()
        for t in threads:
            t.join()
        eq(sorted(set(bad)), [], f'{len(bad)} reads during 300 writes saw a file that was not whole')


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

        notes = {
            'crates/sim/src/serve.rs': 'let a = uVar3 + local_28;  // transcribed, not understood\n',
            'research/journal/2026-10-15-t1-walk/1-PART.md': '    uVar3 = FUN_0012abcd(param_1);\nthe walker eases in\n',
            'research/journal/2026-10-15-t2-turn/SPEC.md': 'speed is read from the record at + 0x24\nturns ease in\n',
            'research/journal/2026-10-15-t3-run/SPEC.md': 'Runs at the speed the character record gives.\n',
        }
        for rel, text in notes.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding='utf-8')
        eq(sorted(h.split(':')[0] + ':' + h.split(':')[1] for h in checks.audit(root, list(notes), known)),
           ['crates/sim/src/serve.rs:1', 'crates/sim/src/serve.rs:1',
            'research/journal/2026-10-15-t1-walk/1-PART.md:1', 'research/journal/2026-10-15-t2-turn/SPEC.md:1'],
           "Ghidra's local names in a port, pasted C in the notes and offsets in a clean-room spec are flagged; "
           'prose and a clean spec are not')

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

    rc, out = hook({'tool_name': 'Bash', 'tool_input': {'command': 'kit check -p kit-oracle'}})
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


# --- the glue to agent-runner -------------------------------------------------

def oracle_join(text):
    with tempfile.TemporaryDirectory() as tmp:
        u, o = Path(tmp) / 'union', Path(tmp) / 'out'
        u.write_text(text, encoding='utf-8')
        r = subprocess.run([sys.executable, str(ROOT / 'kit'), 'oracle', 'join', str(u), str(o)],
                           capture_output=True, text=True)
        return o.read_text(encoding='utf-8') if r.returncode == 0 else None


def test_oracle_join():
    print('oracle join: the [run.join] command for the addresses file')
    # The one joinable file where both sides can be wrong together: nothing downstream picks between two
    # addresses for one name, so a clash must not be resolved here.
    both = 'angle 0x1004c0 the stick angle\nspeed 0x100500\n'
    eq(oracle_join(both + 'speed 0x100500\n'), both, 'a name both sides added at the same address is kept once')
    eq(oracle_join('angle 0x1004c0\nangle 0x2004c0\n'), None, 'the same name at two addresses exits 1')
    eq(oracle_join('angle main 0x1004c0\nangle menu 0x1004c0\n').count('angle'), 2,
       'the same name in two overlays is two addresses and both stay')
    eq(oracle_join('angle\n'), None, 'a line that is not an address stops the join too')
    text = '# the stick\nangle 0x1004c0\n\n# the bike\nspeed 0x100500\n'
    eq(oracle_join(text), text, 'comments and blank lines survive the join')


def kit_py(code, toml, **env):
    """Run `code` in a fresh interpreter with KIT_CONFIG at `toml`; its stdout."""
    r = subprocess.run([sys.executable, '-c', f'import sys; sys.path.insert(0, {str(ROOT)!r}); {code}'],
                       capture_output=True, text=True, env={**os.environ, 'KIT_CONFIG': str(toml), **env})
    check(r.returncode == 0, f'{code[:40]} runs', r.stderr[-400:])
    return r.stdout


def test_runner_config():
    print('run: the settings kit run hands agent-runner')
    from ps2kit import runner as K
    with tempfile.TemporaryDirectory() as tmp:
        toml = Path(tmp) / 'kit.toml'
        toml.write_text(f'[paths]\ncontext = "ctx"\njournal = "notes/j"\n[game]\niso = "game/x.iso"\n'
                        f'[git]\npush_merge = false\n'
                        f'[agent-runner.run]\nslots = 5\n', encoding='utf-8')
        s = json.loads(kit_py('import json; from ps2kit import runner; print(json.dumps(runner.settings()))', toml))
        r = s['run']
        kit = shlex.join([sys.executable, 'kit'])
        eq(r['command'], 'kit', 'sessions are told kit, not agent-runner')
        eq(r['test'], f'{kit} check', 'the gate is kit check, run by this interpreter (cmd.exe runs no `kit` script)')
        eq(r['on_exit'], f'{kit} pcsx2 stop', 'a slot closes its own emulator when its session ends')
        eq(r['slot_env'], ['KIT_PCSX2={n}', 'KIT_RUN=1'], 'each slot gets its own emulator copy')
        check({'KIT_CONFIG', 'KIT_CHECK_SLOTS', 'AGENT_RUNNER_IDLE_BG'} <= set(r['pass_env']),
              'and the kit settings the runner was started with', str(r['pass_env']))
        eq(r['links'], ['ctx', '*.iso', 'game/x.iso'], 'context and the disc are linked into every worktree')
        eq(r['notes'], 'ctx/notes', 'the runner logs into context/notes')
        eq(s['plan'], {'brief_extra': f'{kit} plan phase {{id}}', 'dir': 'plan', 'journal': 'notes/j'},
           'plan and journal come from kit.toml, and every brief ends with the phase')
        eq(r['joinable'], ['plan/*.md', 'notes/j/*.md', 'research/oracle.txt'], 'joinable: plan, journal, oracle')
        eq(r['join'], {'research/oracle.txt': f'{kit} oracle join {{union}} {{out}}'}, 'the oracle file joins via kit')
        eq(r['untested'], ['*.md', '.github/*'], 'docs and CI files do not need a re-test')
        eq((s['git']['push'], s['git']['push_merge']), ('origin', False), '[git] carries over')
        eq(r['slots'], 5, 'a kit.toml [agent-runner] table wins')
        eq(tomllib.loads('\n'.join(K._toml(s))), s, 'the generated file reads back as the same settings')
        for f in ('plan/TODO.md', 'plan/DONE.md', 'notes/j/2026-01-01-t7-walker/README.md', 'research/oracle.txt'):
            check(any(fnmatch.fnmatch(f, g) for g in r['joinable']), f'{f} is joinable')
        for f in ('PLAN.md', 'AGENT.md', 'docs/WORKFLOW.md', 'crates/game-sim/src/tick.rs', 'plan/notes.txt'):
            check(not any(fnmatch.fnmatch(f, g) for g in r['joinable']), f'{f} needs reading, so not joinable')
    from ps2kit import platform_
    saved = platform_.WINDOWS, sys.executable
    try:  # what cmd.exe gets on Windows: the interpreter's path in double quotes, then the relative script
        platform_.WINDOWS, sys.executable = True, r'C:\Program Files\Python312\python.exe'
        s = K.settings()
        eq(s['run']['test'], r'"C:\Program Files\Python312\python.exe" kit check', '{kit} is quoted for cmd.exe')
        eq(s['run']['prompt_extra'].count('{kit}') + s['run']['prompt_extra'].count('python.exe'), 0,
           'what the agents read says kit, not an interpreter path')
    finally:
        platform_.WINDOWS, sys.executable = saved
    eq(K._slots([], 3), 3, 'slots: the default')
    eq(K._slots(['--mode', 'p', '5'], 3), 5, 'slots: after an option with a value')
    eq(K._slots(['once'], 3), 1, 'slots: once is one')


def test_done_plan_calls():
    print('done: ticks and blocks through agent-runner\'s plan')
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        (repo / 'plan').mkdir()
        (repo / 'plan/TODO.md').write_text('# TODO\n\n### Next\n\n- [ ] **T1** one\n- [ ] **T2** two\n'
                                           '- [ ] **T3** three\n', encoding='utf-8')
        (repo / 'plan/DONE.md').write_text('# DONE\n', encoding='utf-8')
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        toml = repo / 'kit.toml'
        toml.write_text(f'[paths]\ncontext = {json.dumps(str(repo / "ctx"))}\n', encoding='utf-8')
        env = {'AGENT_RUNNER_REPO': str(repo)}
        kit_py("from ps2kit import plan; plan.tick('T1'); plan.block('T2', 'needs T3 first', after=['T3'])", toml,
               **env)
        todo = (repo / 'plan/TODO.md').read_text(encoding='utf-8')
        check('**T1**' not in todo and '**T1**' in (repo / 'plan/DONE.md').read_text(encoding='utf-8'),
              'tick moves the task to DONE.md')
        has(todo, r'- \[~\] \*\*T2\*\* two .*BLOCKED: \(after T3\) needs T3 first', 'block --after records the wait')
        check((repo / 'ctx/notes/agent-runner.toml').exists(), 'with the generated settings')

def test_cli_help():
    print('cli: every command of this layer explains itself with no kit.toml')
    for cmd in ('plan', 'journal', 'check', 'done', 'run', 'stop', 'oracle'):
        r = subprocess.run([sys.executable, str(ROOT / 'kit'), cmd, '--help'],
                           capture_output=True, text=True, cwd=str(ROOT))
        check(r.returncode == 0 and len(r.stdout) > 40,
              f'kit {cmd} --help prints help', (r.stdout + r.stderr)[:400])


def main() -> int:
    for t in (test_phase, test_shipped_backlog, test_atomic_write, test_journal, test_check_summary,
              test_audit, test_guard, test_inbox, test_oracle_join, test_runner_config,
              test_done_plan_calls, test_cli_help):
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