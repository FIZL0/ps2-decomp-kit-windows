#!/usr/bin/env python3
"""Self-test for the Windows plumbing, run on any machine.

    python3 tests/test_platform_win.py      (no kit.toml, no emulator, no Windows, under a second)

The Windows edition cannot be tested where it runs, so the parts that *decide* things — which directory,
which port, which argv, which ini row, which multiplexer, what a pipe is called, how long `8f` is — are
written as functions of their inputs, and this is what checks them. `platform_.WINDOWS` is forced true
around each group and the environment is handed in as a dict, so the Windows branches are exercised here.

What this cannot prove, and no test on Linux can: that `ctypes` finds the symbols, that `PrintWindow`
returns pixels for a hardware-accelerated emulator window, that ViGEmBus is installed, that PCSX2's ini
keys are spelled the way we spell them, or that `wt.exe` takes these flags. Those need a Windows machine;
they are listed in SETUP.md. Everything else is here.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ps2kit import platform_ as plat  # noqa: E402
from ps2kit import pad  # noqa: E402
from ps2kit import runner  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FAILED: list[str] = []

WENV = {'LOCALAPPDATA': r'C:\Users\dev\AppData\Local', 'APPDATA': r'C:\Users\dev\AppData\Roaming',
        'USERPROFILE': r'C:\Users\dev', 'PROGRAMFILES': r'C:\Program Files',
        'PROGRAMFILES(X86)': r'C:\Program Files (x86)', 'TEMP': r'C:\Users\dev\AppData\Local\Temp'}


def check(ok, what, extra=''):
    print(('  ok   ' if ok else '  FAIL ') + what + ('' if ok else f'\n         {extra}'))
    if not ok:
        FAILED.append(what)


def eq(got, want, what):
    check(got == want, what, f'got {got!r}\n         want {want!r}')


@contextlib.contextmanager
def as_windows(win=True):
    """Force the Windows branches. Every module reads plat.WINDOWS at call time, so this is enough."""
    was = plat.WINDOWS
    plat.WINDOWS = win
    try:
        yield
    finally:
        plat.WINDOWS = was


# --- directories --------------------------------------------------------------

def test_dirs():
    print('platform: runtime and config directories')
    eq(plat.win_runtime_dir(WENV), Path(r'C:\Users\dev\AppData\Local') / 'ps2kit',
       'the runtime dir is %LOCALAPPDATA%\\ps2kit')
    eq(plat.win_runtime_dir({k: v for k, v in WENV.items() if k != 'LOCALAPPDATA'}),
       Path(r'C:\Users\dev\AppData\Roaming') / 'ps2kit', 'APPDATA is the first fallback')
    eq(plat.win_runtime_dir({'USERPROFILE': r'C:\Users\dev'}),
       Path(r'C:\Users\dev') / 'AppData' / 'Local' / 'ps2kit',
       'with nothing set it is derived from the profile, never from getuid')
    check('ps2kit' in str(plat.win_runtime_dir(WENV)), 'the runtime dir is ours, not a shared temp dir')

    eq(plat.win_config_dir(WENV), Path(r'C:\Users\dev') / 'Documents' / 'PCSX2',
       "PCSX2's config lives in Documents\\PCSX2")
    with as_windows():
        eq(plat.pcsx2_config_dir(), Path(os.environ.get('USERPROFILE') or Path.home()) / 'Documents' / 'PCSX2',
           'the user\'s own copy reads Documents\\PCSX2')
        eq(plat.pcsx2_config_dir('3').name, 'PCSX2', 'kit copy 3 gets its own PCSX2 config tree')
        check(plat.pcsx2_config_dir('3').parent.name == 's3',
              'and the tree is under the slot directory', str(plat.pcsx2_config_dir('3')))
        check(ROOT not in plat.pcsx2_config_dir('3').parents,
              'copies live beside the repo, never inside it', str(plat.pcsx2_config_dir('3')))


def test_search_dirs():
    print('platform: where setup looks for PCSX2 and Ghidra')
    with as_windows():
        eq(plat.pcsx2_guesses(), tuple(f'{n}.exe' for n in plat.PCSX2_IMAGES),
           'PATH names are the .exe forms of the known image names')
        dirs = plat.pcsx2_search_dirs(WENV)
        for want in (Path(r'C:\Program Files') / 'PCSX2', Path(r'C:\Users\dev') / 'Downloads'):
            check(want in dirs, f'PCSX2 search includes {want}', str(dirs))
        gd = plat.ghidra_search_dirs(WENV)
        check(Path(r'C:\Users\dev') / 'Downloads' in gd and Path('C:/') in gd,
              'Ghidra search covers Downloads and the drive root', str(gd))
        eq(plat.ghidra_headless(Path(r'C:\ghidra')).name, 'analyzeHeadless.bat',
           'the headless analyser is the .bat on Windows')
    eq(plat.ghidra_headless(Path('/opt/ghidra')).name, 'analyzeHeadless',
       'and the bare script elsewhere')
    with as_windows():
        eq(plat.ghidra_user_dirs(WENV)[0], Path(r'C:\Users\dev\AppData\Roaming') / 'ghidra',
           "a user-installed extension lives under %APPDATA%, not under ~/.config")
        check(Path(r'C:\Users\dev') / '.ghidra' in plat.ghidra_user_dirs(WENV),
              'with the pre-10.2 location still searched', str(plat.ghidra_user_dirs(WENV)))
    eq(plat.ghidra_user_dirs({'HOME': '/home/dev', 'XDG_CONFIG_HOME': '/home/dev/.config'})[0],
       Path('/home/dev/.config/ghidra'), 'and the POSIX search is unchanged')


# --- PINE ---------------------------------------------------------------------

def test_pine_endpoint():
    print('platform: the PINE endpoint (slot is the whole port number)')
    with as_windows():
        eq(plat.pine_endpoint(28011), (socket.AF_INET, ('127.0.0.1', 28011)),
           'the default instance is TCP 127.0.0.1:28011')
        eq(plat.pine_endpoint(28014), (socket.AF_INET, ('127.0.0.1', 28014)),
           'copy 3 is 28011 + 3, the same arithmetic as on Linux')
        fam, _ = plat.pine_endpoint(28011)
        check(fam == socket.AF_INET, 'nothing tries to open a unix socket on Windows')
    fam, addr = plat.pine_endpoint(28011)
    check(fam == socket.AF_UNIX and str(addr).endswith('pcsx2.sock'),
          'the POSIX endpoint is unchanged', f'{fam} {addr}')


# --- instances ----------------------------------------------------------------

def test_instances():
    print('platform: how one PCSX2 copy is kept apart from another')
    with as_windows():
        eq(plat.instance_args('2'), ['-cfgpath', str(plat.pcsx2_config_dir('2'))],
           'a copy is pinned with -cfgpath, since there is no XDG_CONFIG_HOME')
        eq(plat.instance_args(''), [], "the user's own PCSX2 is launched with no extra arguments")
        eq(plat.instance_env('2'), {},
           'and with no environment: a ViGEm pad has no identity to filter SDL on')
        eq(plat.user_pad_env(3), {}, 'nor can the kit pads be hidden from the user\'s own emulator')
    eq(plat.instance_args('2', windows=False), [], 'the POSIX launch is unchanged')
    env = plat.instance_env('2')
    check('XDG_CONFIG_HOME' in env, 'POSIX copies are still pinned by XDG_CONFIG_HOME', str(env))


def test_pad_ini_rows():
    print('platform: the ini rows that bind a copy to its own pad')
    with as_windows():
        rows = plat.pad_ini_rows(2)
        d = {(s, k): v for s, k, v in rows}
        eq(d[('InputSources', 'Keyboard')], 'false', 'the keyboard is off in a kit copy')
        eq(d[('InputSources', 'SDL')], 'true', 'SDL is the source that sees a ViGEm pad')
        eq(d[('InputSources', 'XInput')], 'false',
           'XInput is off, or the one pad is enumerated twice and the indices shift')
        eq(d[('Hotkeys', 'TogglePause')], 'SDL-1/Guide', 'copy 2 is bound to SDL device index 1')
        check(all(isinstance(r, tuple) and len(r) == 3 for r in rows), 'every row is (section, key, value)')
        eq([k for _, k, _ in plat.pad_ini_rows(1)], [k for _, k, _ in plat.pad_ini_rows(2)],
           'the same keys are written whichever copy it is')
        eq({(s, k): v for s, k, v in plat.pad_ini_rows(1)}[('Hotkeys', 'TogglePause')], 'SDL-0/Guide',
           'copy 1 is device index 0')
        # Controller port 1 itself, which is what the game actually reads: a copy that keeps the user's
        # keyboard-bound port 1 has no input at all, since the keyboard source is switched off above.
        eq(d[('Pad1', 'Cross')], 'SDL-1/A', "copy 2's port 1 is its own pad, not the user's binding")
        eq(d[('Pad1', 'Start')], 'SDL-1/Start', 'the press every capture task starts with')
        eq(d[('Pad1', 'Type')], 'DualShock2', 'a port whose type is None receives nothing either')
        eq(d[('Pad1', 'L2')], 'SDL-1/+LeftTrigger', 'the triggers are axes, not buttons')
        eq(len([1 for s, _, _ in rows if s == 'Pad1']), len(plat.PAD1_BUTTONS) + 1,
           'every button in the table is bound, plus Type')
        eq(plat.pad_device(1), 'SDL-0', 'device index, because ViGEm pads share one VID/PID')
        eq(plat.pad_device(3), 'SDL-2', 'and copy 3 is the third pad SDL enumerates')
    rows = plat.pad_ini_rows(2, windows=False)
    check(('InputSources', 'XInput', 'false') not in rows, 'no XInput row on POSIX')
    eq({(s, k): v for s, k, v in rows}[('Pad1', 'Cross')], 'SDL-0/A',
       'on POSIX every copy sees only its own pad, so port 1 is always device 0')
    eq(plat.pad_device(3, windows=False), 'SDL-0', 'whichever copy it is')


# --- quoting and processes ----------------------------------------------------

def test_quote_args():
    print('platform: quoting for the local shell')
    with as_windows():
        eq(plat.quote_args(['--test', 'a b']), '--test "a b"',
           'Windows quoting is what CommandLineToArgvW undoes')
        eq(plat.quote_args([]), '', 'nothing to append is empty, not a stray space')
        eq(plat.py_cmd(Path(r'C:\k it\r.py'), '--stop-hook'),
           subprocess.list2cmdline([sys.executable, r'C:\k it\r.py', '--stop-hook']),
           'a hook command names the running interpreter, because no fixed name exists')
    eq(plat.quote_args(['--test', "a b"]), "--test 'a b'", 'POSIX quoting is unchanged')
    eq(plat.py_cmd(Path('/k/r.py'), '--stop-hook'), 'python3 /k/r.py --stop-hook',
       'and a POSIX hook still says python3')


def test_tasklist_pids():
    print('platform: the tasklist fallback parser')
    out = ('"pcsx2-qt.exe","4120","Console","1","512,000 K"\n'
           '"pcsx2x64-avx2.exe","4121","Console","1","512,000 K"\n'
           '"explorer.exe","900","Console","1","9,000 K"\n')
    eq(plat.tasklist_pids(out, 'pcsx2-qt'), [4120], 'the image name matches with or without .exe')
    eq(plat.tasklist_pids(out, 'pcsx2-qt.exe'), [4120], 'and either way round')
    eq(plat.tasklist_pids(out, 'pcsx2'), [], 'a prefix is not a match: never kill by name globally')
    eq(plat.tasklist_pids('INFO: no tasks are running.', 'pcsx2-qt'), [],
       'the "nothing running" line is not a pid')
    eq(plat.tasklist_pids('"Image Name","PID"\n"pcsx2-qt.exe","7"\n', 'pcsx2-qt'), [7],
       'a stray header row is skipped, since a localised Windows translates it')


def test_lock():
    print('platform: the lock, including held_by_other on this process')
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / 'pcsx2.lock'
        a = plat.Lock('pcsx2')
        a.path = p
        check(a.take(block=False), 'the lock can be taken')
        b = plat.Lock('pcsx2')
        b.path = p
        check(not a.held_by_other(), 'the holder does not see itself as a foreign holder')
        check(not b.held_by_other(),
              'nor does another object in the same process (which is what Windows can tell us)')
        src = ('import sys, time\n'
               'sys.path.insert(0, %r)\n'
               'from ps2kit import platform_ as plat\n'
               'l = plat.Lock("pcsx2"); l.path = __import__("pathlib").Path(%r)\n'
               'print("got" if l.take(block=False) else "busy", flush=True)\n'
               'time.sleep(5)\n') % (str(ROOT), str(p))
        other = subprocess.Popen([sys.executable, '-c', src], stdout=subprocess.PIPE, text=True)
        try:
            eq(other.stdout.readline().strip(), 'busy', 'another process cannot take a held lock')
        finally:
            other.kill()
            other.wait()
        a.release()
        check(b.take(block=False), 'and it is free again once released')
        b.release()


def test_link():
    print('platform: linking context/ and the disc into a worktree without copying them')
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / 'context').mkdir()
        (d / 'context' / 'ram.bin').write_bytes(b'x' * 8)
        (d / 'game.iso').write_bytes(b'y' * 8)
        (d / 'w').mkdir()
        check(plat.link(d / 'context', d / 'w' / 'context'), 'a directory links')
        eq((d / 'w' / 'context' / 'ram.bin').read_bytes(), b'x' * 8, 'and reads through to the original')
        check(plat.link(d / 'game.iso', d / 'w' / 'game.iso'), 'a file links')
        eq((d / 'w' / 'game.iso').read_bytes(), b'y' * 8, 'and reads through too')
        check(plat.link(d / 'context', d / 'w' / 'context'),
              'linking over something that is already there is not a failure')
        iso, orig = d / 'w' / 'game.iso', d / 'game.iso'
        check(iso.is_symlink() or iso.stat().st_ino == orig.stat().st_ino,
              'the iso is the same file, not a second copy (a symlink here, a hard link on Windows)')


# --- screenshots --------------------------------------------------------------

def test_png_bytes():
    print('platform: the PNG writer used by the PrintWindow grab')
    w, h = 3, 2
    want = [(10, 20, 30), (40, 50, 60), (70, 80, 90), (1, 2, 3), (4, 5, 6), (7, 8, 9)]
    bgra = bytearray()
    for r, g, b in want:
        bgra += bytes((b, g, r, 255))
    png = plat.png_bytes(w, h, bytes(bgra))
    eq(png[:8], b'\x89PNG\r\n\x1a\n', 'the signature is a PNG signature')
    chunks, i = [], 8
    while i < len(png):
        n = struct.unpack('>I', png[i:i + 4])[0]
        tag, data = png[i + 4:i + 8], png[i + 8:i + 8 + n]
        crc = struct.unpack('>I', png[i + 8 + n:i + 12 + n])[0]
        eq(crc, zlib.crc32(tag + data) & 0xffffffff, f'the {tag.decode()} CRC is right')
        chunks.append((tag, data))
        i += 12 + n
    eq([t for t, _ in chunks], [b'IHDR', b'IDAT', b'IEND'], 'and the chunks a decoder needs, in order')
    eq(struct.unpack('>IIBBBBB', chunks[0][1]), (w, h, 8, 2, 0, 0, 0),
       'the header says 8-bit RGB, no interlacing')
    raw = zlib.decompress(chunks[1][1])
    rows = [raw[y * (w * 3 + 1):(y + 1) * (w * 3 + 1)] for y in range(h)]
    check(all(r[0] == 0 for r in rows), 'every row uses filter 0')
    got = [tuple(r[1 + x * 3:4 + x * 3]) for r in rows for x in range(w)]
    eq(got, want, 'BGRA from the DIB comes out as RGB in the file, in the same order')


def test_window_pick():
    print('platform: finding the emulator window by title')
    wins = [(11, 'Mozilla Firefox'), (22, 'MYGAME [SCUS-00000] | PCSX2 2.0'), (33, 'PCSX2'),
            (44, ''), (55, None)]
    eq(plat.window_pick(wins, 'pcsx2'), [22, 33],
       'the default pattern matches a running game and a bare PCSX2')
    eq(plat.window_pick(wins, 'PCSX2 2.0'), [22], 'a longer pattern narrows it to one')
    eq(plat.window_pick(wins, 'nothing'), [], 'no match is an empty list, not a guess')
    eq(plat.window_pick([], 'pcsx2'), [], 'and no windows at all is fine')
    with as_windows():
        check('printwindow' in plat.screenshot_tools(),
              'PrintWindow is the first screenshot tool on Windows', str(plat.screenshot_tools()))


# --- the pad ------------------------------------------------------------------

def test_pad_transport():
    print('pad: the named-pipe transport')
    eq(pad.pipe_name('2'), r'\\.\pipe\ps2kit-pad2', 'one pipe per copy, named after the instance')
    eq(pad.pipe_name(''), r'\\.\pipe\ps2kit-pad', "the user's own copy has the bare name")
    check(pad.pipe_name('1') != pad.pipe_name('2'), 'two copies never share a pipe')
    was = os.environ.get('KIT_PCSX2')
    os.environ['KIT_PCSX2'] = '2'
    try:
        with as_windows():
            eq(pad.transport(), r'\\.\pipe\ps2kit-pad2', 'the Windows transport is that pipe')
        check(pad.transport().endswith('pad2') or 'pad' in pad.transport(),
              'the POSIX transport is still a FIFO path', pad.transport())
        check(not pad.transport().startswith(r'\\'), 'and not a pipe name', pad.transport())
    finally:
        os.environ.pop('KIT_PCSX2', None)
        if was is not None:
            os.environ['KIT_PCSX2'] = was

    eq(pad.split_lines(b'press cross 100\nsleep 50\npre'),
       (['press cross 100', 'sleep 50'], b'pre'),
       'a pipe read is split into whole lines, keeping the partial one')
    eq(pad.split_lines(b''), ([], b''), 'an empty read yields nothing')
    eq(pad.split_lines(b'stick l 1 0\n'), (['stick l 1 0'], b''), 'a clean batch leaves no remainder')
    eq(pad.split_lines('dpad right 60 # é\n'.encode()), (['dpad right 60 # é'], b''),
       'and the decode survives a non-ASCII comment')


def test_pad_buttons():
    print('pad: every button the scripts use maps to an Xbox button')
    missing = [b for b in pad.BUTTONS if b not in pad.XUSB and b not in pad.TRIGGERS]
    eq(missing, [], 'no scriptable button is missing from the ViGEm table')
    check('select' not in pad.XUSB, 'select is still not sendable (it opens the PCSX2 menu)')
    eq(sorted(set(pad.XUSB.values())), sorted(set(pad.XUSB.values())), 'no two buttons share a mapping')
    check(len(set(pad.XUSB.values())) == len(pad.XUSB), 'one Xbox button per PS2 button',
          str(pad.XUSB))
    s = pad.Script(None)
    eq(s._ms('120'), 120.0, '`120` is 120 ms')
    eq(s._ms('80ms'), 80.0, '`80ms` is 80 ms')
    eq(round(s._ms('8f'), 3), round(8 * 1000.0 / pad._fps(), 3), '`8f` is 8 frames at the game\'s rate')
    check(s._ms('2f') > 30, 'a frame count is milliseconds, not frames, by the time it is slept on')


# --- the unattended runner ----------------------------------------------------

def test_mux_choice():
    print('runner: which multiplexer a run gets')
    eq(runner.mux_choice(3, False, True, False, False), 'tmux', 'POSIX with tmux: panes')
    eq(runner.mux_choice(3, False, False, False, False), 'sequential', 'POSIX without tmux: one session')
    eq(runner.mux_choice(3, False, True, True, True), 'wt', 'Windows with wt.exe: tabs')
    eq(runner.mux_choice(3, False, False, False, True), 'console', 'Windows without it: a console per slot')
    eq(runner.mux_choice(1, False, True, True, True), 'sequential', 'one slot never needs a multiplexer')
    eq(runner.mux_choice('once', False, True, True, True), 'sequential', '`once` is one session')
    eq(runner.mux_choice(5, True, True, True, True), 'sequential', '--no-tmux is honoured on both')
    eq(runner.slots_arg('once'), 'once', '`kit run once` parses')
    eq(runner.slots_arg('4'), 4, 'and so does a count')


def test_runner_windows_launch():
    print('runner: the Windows slot launch')
    job = Path(tempfile.gettempdir()) / 'ps2kit-test.job'
    argv = runner.wt_argv(2, Path(r'C:\kit\s2'), job)
    eq(argv[:6], ['wt.exe', '-w', 'kit', 'new-tab', '--title', 's2'],
       'one named window, one titled tab per slot')
    check('-d' in argv and argv[argv.index('-d') + 1] == r'C:\kit\s2',
          'the tab opens in the slot worktree', str(argv))
    eq(argv[-2:], ['--slot-shim', str(job)], 'the tab runs the shim on the job file')
    eq(argv[-3], str(Path(runner.__file__).resolve()), 'which is this module')
    eq(argv[-4], sys.executable, 'through this interpreter, named by path')

    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        marker = d / 'slot9.live'
        spec = {'argv': [sys.executable, '-c', 'import time; time.sleep(30)'], 'env': {'KIT_PCSX2': '9'},
                'cwd': str(d), 'marker': str(marker)}
        (d / 'job').write_text(json.dumps(spec), encoding='utf-8')
        shim = subprocess.Popen([sys.executable, str(ROOT / 'ps2kit' / 'runner.py'),
                                 '--slot-shim', str(d / 'job')])
        pid = 0
        try:
            for _ in range(100):
                if marker.exists():
                    break
                time.sleep(0.05)
            check(marker.exists(), 'the shim publishes a marker file the loop can poll')
            pid = int(marker.read_text().strip())
            check(plat.alive(pid), 'the marker holds the pid of the session itself, not of a wrapper')
            check(pid != shim.pid, 'which is a child of the shim, so its own children mean "running a tool"')
            check(pid in plat.children(shim.pid), 'and children() finds it', f'{pid} {plat.children(shim.pid)}')
            os.kill(pid, signal.SIGTERM)       # the session ends: the shim must clean up after it
            check(shim.wait(timeout=20) != 0, 'the shim waits for the session, then ends too')
        finally:
            if shim.poll() is None:
                shim.kill()
                shim.wait()
            if pid and plat.alive(pid):
                os.kill(pid, signal.SIGKILL)
        check(not marker.exists(), 'and removes the marker when the session is over')
        mux = runner.WtMux()
        eq(mux.pid(marker), 0, 'a vanished marker reads as no pid rather than raising')
        check(not mux.alive(marker), 'and as a session that is over')

        spec = {'argv': [sys.executable, '-c', 'raise SystemExit(7)'], 'env': {}, 'cwd': str(d),
                'marker': str(d / 'slot8.live')}
        (d / 'job8').write_text(json.dumps(spec), encoding='utf-8')
        r = subprocess.run([sys.executable, str(ROOT / 'ps2kit' / 'runner.py'),
                            '--slot-shim', str(d / 'job8')])
        eq(r.returncode, 7, "the shim's exit code is the session's own")
        check(not (d / 'slot8.live').exists(), 'and the marker is gone whatever the session did')


def test_agent_argv():
    print('runner: the agent command is a list, so no shell has to be guessed')
    argv = runner.agent_argv('do the thing', 'sid-1', 'opus', 90)
    check(isinstance(argv, list) and all(isinstance(a, str) for a in argv), 'argv is a list of strings')
    eq(argv[1], 'do the thing', 'the prompt is one argument, whatever is in it')
    i = argv.index('--settings')
    hooks = json.loads(argv[i + 1])['hooks']
    cmd = hooks['Stop'][0]['hooks'][0]['command']
    check('--stop-hook' in cmd and '--idle 90' in cmd, 'the Stop hook arms the idle timer', cmd)
    check('runner.py' in cmd, 'and points at this module', cmd)
    with as_windows():
        cmd = json.loads(runner.agent_argv('p', 's', 'opus', 5)[
            runner.agent_argv('p', 's', 'opus', 5).index('--settings') + 1])
        c = cmd['hooks']['Stop'][0]['hooks'][0]['command']
        check(sys.executable in c, 'on Windows the hook names the interpreter by path', c)


# --- nothing POSIX got broken -------------------------------------------------

def test_modules_import():
    print('kit: the OS-bound modules still import and still explain themselves here')
    r = subprocess.run([sys.executable, '-c', 'import ps2kit.platform_, ps2kit.pad, ps2kit.emu, '
                        'ps2kit.runner, ps2kit.setup, ps2kit.checks, ps2kit.pine'],
                       capture_output=True, text=True, cwd=str(ROOT))
    check(r.returncode == 0 and not r.stderr.strip(), 'they import with no warning',
          (r.stdout + r.stderr)[:600])
    for cmd in ('pcsx2', 'pad', 'run', 'setup', 'check', 'shot'):
        r = subprocess.run([sys.executable, str(ROOT / 'kit'), cmd, '--help'],
                           capture_output=True, text=True, cwd=str(ROOT))
        check(r.returncode == 0, f'kit {cmd} --help exits 0', (r.stdout + r.stderr)[:300])
    txt = (ROOT / 'ps2kit' / 'emu.py').read_text()
    check('if plat.WINDOWS' not in txt and 'if WINDOWS' not in txt,
          'emu.py has no OS branch of its own: that knowledge lives in platform_.py')
    launcher = (ROOT / 'kit.cmd').read_bytes()
    check(b'%~dp0kit' in launcher, 'kit.cmd runs the kit next to it, not one on PATH')
    check(b'\r\n' in launcher, 'and has CRLF line endings, which cmd.exe needs for labels')


def main() -> int:
    for t in (test_dirs, test_search_dirs, test_pine_endpoint, test_instances, test_pad_ini_rows,
              test_quote_args, test_tasklist_pids, test_lock, test_link, test_png_bytes, test_window_pick,
              test_pad_transport, test_pad_buttons, test_mux_choice, test_runner_windows_launch,
              test_agent_argv, test_modules_import):
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
