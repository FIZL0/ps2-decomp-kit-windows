"""Run PCSX2 the way the kit needs it, and get data back out of it.

Three jobs live here, and each exists because of a specific trap:

* **Starting it with PINE on.** PCSX2 rewrites `inis/PCSX2.ini` when it exits, so an `EnablePINE = true`
  you set by hand is gone the next time it closes. The kit therefore sets it in the ini on *every* launch,
  right before starting the emulator — never once, by hand.
* **Kit copies.** Several agents share one machine, and the user's own emulator must stay theirs: their
  window, their settings, their save states, their memory cards, their speed. `copy_config(N)` makes copy N —
  a duplicate of the user's config with its own PINE slot (`pine_slot + N`), its own save-state and
  memory-card directories, muted, keyboard input off (a window that steals focus must not let the user's
  typing pause a capture) and pause bound to its pad's Guide button, because a copy behind other windows has
  none anyone can click. `KIT_PCSX2=N` then points every kit tool, the pad, PINE and the lock at copy N.
  PCSX2 is told which copy to use with `-cfgpath` (`platform_.instance_args`): Windows has no
  XDG_CONFIG_HOME, and an environment variable would not survive the emulator relaunching itself anyway.
* **Getting pixels and RAM out.** `shot()` grabs the window if the compositor can see it and otherwise
  presses the emulator's own Screenshot hotkey through the virtual pad — which is the only thing that works
  for a copy on another workspace, and is needed far more often than it sounds. `state_ram()` pulls
  `eeMemory.bin` out of a `.p2s` save state, which is how you read static values *without* opening a second
  PINE connection during a capture (two clients at once hang both).

Process tracking is by pid file, verified with the process name: `pcsx2-qt` has file capabilities on some
systems, which hides its environment from `/proc`, so there is no way to ask a running emulator "which
instance are you?". And it is always *this* instance — the kit never kills by name, which would take down
the user's own game.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ps2kit import platform_ as plat
from ps2kit.config import ROOT, cfg, num

SKIP_IN_COPY = {'cache', 'covers', 'snaps', 'videos', 'logs', 'dumps', 'textures', 'gamesettings'}


# --- this instance -----------------------------------------------------------

def _inst() -> str:
    return plat.instance()


def _pid_file() -> Path:
    return plat.rt(f'pcsx2{_inst()}.pid')


def _pad_pid_file() -> Path:
    return plat.rt(f'pad{_inst()}.pid')


def running() -> int | None:
    """The pid of *this* instance's emulator, or None. Verified by name: pids get reused."""
    f = _pid_file()
    try:
        pid = int(f.read_text().strip())
    except (OSError, ValueError):
        return None
    if not plat.alive(pid):
        return None
    return pid if 'pcsx2' in plat.proc_name(pid).lower() else None


def config_dir() -> Path:
    return plat.pcsx2_config_dir(_inst())


def ini_path(inst: str | None = None) -> Path:
    return plat.pcsx2_config_dir(_inst() if inst is None else inst) / 'inis' / 'PCSX2.ini'


# --- the ini -----------------------------------------------------------------

def _ini_set(path: Path, section: str, key: str, value) -> None:
    """Set one key in a PCSX2 ini, in place, keeping everything else byte for byte.

    If the key already exists anywhere in the file it is edited where it is: PCSX2's key names are unique
    across sections, and which section a given build keeps `OutputMuted` or `NominalScalar` in has moved
    between versions. Only a genuinely absent key is appended, under `section`.
    """
    if not path.exists():
        raise SystemExit(f'no PCSX2 ini at {path}\nStart PCSX2 once so it writes its config, then retry.')
    lines = path.read_text(encoding='utf-8', errors='surrogateescape').splitlines()
    pat = re.compile(rf'\s*{re.escape(key)}\s*=')
    for i, l in enumerate(lines):
        if pat.match(l):
            lines[i] = f'{key} = {value}'
            break
    else:
        start = next((i for i, l in enumerate(lines) if l.strip() == f'[{section}]'), None)
        if start is None:
            lines += ['', f'[{section}]', f'{key} = {value}']
        else:
            end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith('[')),
                       len(lines))
            lines.insert(end, f'{key} = {value}')
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8', errors='surrogateescape')


def _ini_del(path: Path, key: str) -> None:
    if not path.exists():
        return
    pat = re.compile(rf'\s*{re.escape(key)}\s*=')
    lines = [l for l in path.read_text(encoding='utf-8', errors='surrogateescape').splitlines()
             if not pat.match(l)]
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8', errors='surrogateescape')


def _ini_get(path: Path, key: str, default: str = '') -> str:
    if not path.exists():
        return default
    for l in path.read_text(encoding='utf-8', errors='surrogateescape').splitlines():
        if re.match(rf'\s*{re.escape(key)}\s*=', l):
            return l.split('=', 1)[1].strip()
    return default


def _folder(key: str, default: str) -> Path:
    """One of PCSX2's [Folders] paths for this instance (relative ones hang off the config dir)."""
    v = _ini_get(ini_path(), key, default) or default
    p = Path(v).expanduser()
    return p if p.is_absolute() else config_dir() / p


def enable_pine(slot: int) -> None:
    """Turn PINE on in this instance's ini. Called before every launch — see the module docstring."""
    ini = ini_path()
    _ini_set(ini, 'EmuCore', 'EnablePINE', 'true')
    _ini_set(ini, 'EmuCore', 'PINESlot', str(slot))


# --- start / stop / status ---------------------------------------------------

def start(args: list[str] | None = None) -> int | None:
    """Launch this instance booting the configured disc, with PINE on. A no-op if it is already up."""
    c = cfg()
    n = _inst()
    if pid := running():
        print(f'PCSX2{" copy " + n if n else ""} already running (pid {pid})')
        return None
    iso = c.need_iso()
    if n:
        if not ini_path().exists():
            raise SystemExit(f'no PCSX2 copy {n} yet ({config_dir()}).\nMake it once:  kit pcsx2 copy {n}')
        # How a copy gets its own config, and its own pad, differs per OS: platform_ owns both.
        env = plat.instance_env(n)
        _serve_pad(n)
    else:
        # The user's own emulator must never pick up a kit pad as its controller 1.
        env = plat.user_pad_env(c.instances)
    enable_pine(c.pine_slot + int(n or 0))
    log = c.ctx('logs', f'pcsx2{n or 0}.log')
    argv = plat.pcsx2_cmd(c.pcsx2) + plat.instance_args(n) + list(args or []) + ['--', str(iso)]
    pid = plat.spawn_detached(argv, log=log, env=env)
    _pid_file().write_text(f'{pid}\n')
    print(f'PCSX2{" copy " + n if n else ""} starting (pid {pid}), PINE slot {c.pine_slot + int(n or 0)}, '
          f'booting {iso.name}\nlog: {log}')
    return None


def _serve_pad(n: str) -> None:
    """A copy needs its pad: pause, and the screenshot fallback, are bound to pad buttons."""
    if plat.alive(_pad_num(_pad_pid_file())):
        return
    ok, why = plat.pad_available()
    if not ok:
        print(f'pad unavailable, so copy {n} cannot be paused or screenshotted by hotkey: {why}',
              file=sys.stderr)
        return
    log = cfg().ctx('logs', f'pad{n}.log')
    pid = plat.spawn_detached([sys.executable, str(ROOT / 'kit'), 'pad', 'serve'], log=log,
                              env={'KIT_PCSX2': n})
    _pad_pid_file().write_text(f'{pid}\n')
    time.sleep(0.5)      # let the device appear before PCSX2 enumerates pads


def _pad_num(f: Path) -> int | None:
    try:
        return int(f.read_text().strip())
    except (OSError, ValueError):
        return None


def stop() -> int | None:
    """Close this instance (and its pad server). Never a kill by name — that is the user's game too."""
    n = _inst()
    pid = running()
    if pid:
        plat.terminate(pid)
        for _ in range(40):
            if not plat.alive(pid):
                break
            time.sleep(0.25)
        print(f'PCSX2{" copy " + n if n else ""} stopped (pid {pid})')
    else:
        print(f'PCSX2{" copy " + n if n else ""} was not running')
    if n and (ppid := _pad_num(_pad_pid_file())) and plat.alive(ppid):
        plat.terminate(ppid)        # the copy's pad goes with the copy
    return None


def status() -> int | None:
    c = cfg()
    n = _inst()
    pid = running()
    print(f'PCSX2{" copy " + n if n else ""}  ' +
          (f'running (pid {pid})' if pid else 'not running  (kit pcsx2 start)'))
    print(f'config    {config_dir()}')
    print(f'PINE      slot {c.pine_slot + int(n or 0)}  '
          f'{"on" if _ini_get(ini_path(), "EnablePINE") == "true" else "off in the ini"}')
    lock = plat.Lock('pcsx2')
    print(f'lock      {"held by another process" if lock.held_by_other() else "free"}')
    if n:
        print(f'speed     NominalScalar = {_ini_get(ini_path(), "NominalScalar", "?")}')
        from ps2kit import pad
        print(f'pad       {pad.transport()}  {"serving" if pad.serving() else "not running"}')
    return 0 if pid else 1


# --- kit copies --------------------------------------------------------------

def copy_config(n: int) -> Path:
    """Create (or refresh) kit copy `n` from the user's own PCSX2 config, and return its config dir.

    Why copies at all: an unattended run has several agents driving the emulator at once, and the user's own
    PCSX2 has to keep working, unmuted, at their speed, with their save states intact. Each copy gets its
    own PINE slot, pad, save-state dir and memory cards, so the worst an agent can do is wreck its own copy.
    """
    c = cfg()
    src = plat.pcsx2_config_dir('')
    dst = plat.pcsx2_config_dir(str(n))
    if not (src / 'inis' / 'PCSX2.ini').exists():
        raise SystemExit(f'no PCSX2 config to copy from ({src}).\n'
                         'Run PCSX2 once yourself (set up your BIOS and controller), then retry.')
    dst.mkdir(parents=True, exist_ok=True)
    for item in sorted(src.iterdir()):
        if item.name in SKIP_IN_COPY or item.name.startswith('.'):
            continue
        target = dst / item.name
        if item.is_dir():
            # Save states and memory cards are copied in, not shared: the copy starts from the user's
            # progress and can then overwrite its own slots freely.
            shutil.copytree(item, target, dirs_exist_ok=True)
        elif not target.exists():
            shutil.copy2(item, target)
    ini = ini_path(str(n))
    speed = os.environ.get('KIT_SPEED') or c.speed
    for section, key, value in [
        ('EmuCore', 'EnablePINE', 'true'),
        ('EmuCore', 'PINESlot', str(c.pine_slot + n)),
        ('SPU2/Output', 'OutputMuted', 'true'),     # eight emulators playing at once is unusable
        ('Framerate', 'NominalScalar', str(speed)),
        ('UI', 'ConfirmShutdown', 'false'),
        ('UI', 'StartPaused', 'false'),
        # Keyboard off, and pause/screenshot on pad buttons: often there is no window to click. Which pad,
        # and which input source, is OS-bound — see platform_.pad_ini_rows().
        *plat.pad_ini_rows(n),
        # Absolute, under the copy: if the user's ini pointed these at their own dirs, a relative copy
        # would silently share — and overwrite — their save states.
        ('Folders', 'Savestates', str(dst / 'savestates')),
        ('Folders', 'MemoryCards', str(dst / 'memcards')),
        ('Folders', 'Snapshots', str(dst / 'snaps')),
    ]:
        _ini_set(ini, section, key, value)
    _ini_del(ini, 'FrameAdvance')   # frame-advance bound to a pad button pauses copies by accident
    for sub in ('savestates', 'memcards', 'snaps', 'logs'):
        (dst / sub).mkdir(parents=True, exist_ok=True)
    states = len(list((dst / 'savestates').glob('*.p2s')))
    pad_desc = next((v for sec, k, v in plat.pad_ini_rows(n) if k == 'TogglePause'), '?').split('/')[0]
    print(f'copy {n}: {dst}\n  PINE slot {c.pine_slot + n}, pad {pad_desc}, speed {speed}, '
          f'muted, {states} save state(s) copied in\n'
          f'  use it:  KIT_PCSX2={n} kit pcsx2 start')
    return dst


# --- screenshots -------------------------------------------------------------

def shot(out: Path, pattern: str = 'pcsx2') -> Path:
    """Screenshot the emulator. Window grab first; the emulator's own hotkey as the fallback.

    The fallback is not an afterthought: a kit copy usually runs on a workspace no compositor will render,
    and some sessions have no window-capture tool at all. Pressing the copy's pad-bound Screenshot hotkey
    (L3+R3, set by `copy_config`) makes PCSX2 write a full-size PNG itself, which is then copied out.
    """
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    # For a copy, a bare "pcsx2" window match would just as happily grab *another agent's* emulator — a
    # screenshot of the wrong game that looks perfectly fine. So a copy goes straight to its own hotkey
    # unless the caller named a window pattern specific enough to identify it.
    if (not _inst() or pattern != 'pcsx2') and plat.screenshot_window(out, pattern) and out.exists():
        return out
    from ps2kit import pad
    if not pad.serving():
        raise SystemExit(f'cannot screenshot: no window matching {pattern!r}, and no pad server to press '
                         'the Screenshot hotkey with — start one:  '
                         f'{"KIT_PCSX2=" + _inst() + " " if _inst() else ""}kit pad serve')
    snaps = _folder('Snapshots', 'snaps')
    snaps.mkdir(parents=True, exist_ok=True)
    before = {f.name for f in snaps.iterdir()}
    pad.send(['down l3', 'down r3', 'sleep 150', 'up r3', 'up l3'])
    f = plat.wait_for_file(snaps, before)
    if not f:
        want = next((v for sec, k, v in plat.pad_ini_rows(int(_inst() or 1)) if k == 'Screenshot'), '?')
        raise SystemExit(f'PCSX2 wrote no screenshot into {snaps}.\n'
                         f'Is it paused, and is Screenshot bound to "{want}" in its ini? '
                         '(kit pcsx2 copy N sets that.)')
    shutil.copy2(f, out)
    return out


# --- save states -------------------------------------------------------------

def _state_open(path: Path):
    """A `.p2s` is an archive; which kind depends on the PCSX2 version. Returns (lister, extractor)."""
    import zipfile
    if zipfile.is_zipfile(path):
        def names():
            with zipfile.ZipFile(path) as z:
                return [(i.filename, i.file_size) for i in z.infolist()]

        def get(member, dest):
            with zipfile.ZipFile(path) as z, open(dest, 'wb') as out:
                shutil.copyfileobj(z.open(member), out)
        return names, get
    # A file no backend can read must say so, rather than listing nothing and looking like a state with
    # no eeMemory.bin in it. Which message is honest depends on what this machine has installed.
    not_archive = (f'cannot open {path.name}: it is neither a zip nor a 7-Zip archive — is it a complete '
                   'PCSX2 save state?')
    have_7zip = False
    try:
        import py7zr
        have_7zip = True
    except ImportError:
        py7zr = None
    # `is_7zfile` first: handing back the py7zr backend for a file that is not a 7-Zip archive turns a
    # one-line "this is not a save state" into a py7zr traceback out of the lister.
    if py7zr is not None and py7zr.is_7zfile(path):
        def names7():
            with py7zr.SevenZipFile(path) as z:
                return [(i.filename, i.uncompressed) for i in z.list()]

        def get7(member, dest):
            with py7zr.SevenZipFile(path) as z:
                data = z.read([member])[member].read()
            Path(dest).write_bytes(data)
        return names7, get7
    if (sevenz := _sevenzip()):     # the CLI reads formats py7zr does not, so still worth a try
        have_7zip = True

        def names_cli():
            r = subprocess.run([sevenz, 'l', '-ba', str(path)], capture_output=True, text=True)
            if r.returncode:
                raise SystemExit(not_archive)
            out = r.stdout
            rows = []
            for l in out.splitlines():
                parts = l.split()
                if len(parts) >= 5:
                    rows.append((parts[-1], int(parts[3]) if parts[3].isdigit() else 0))
            return rows

        def get_cli(member, dest):
            with open(dest, 'wb') as fh:
                r = subprocess.run([sevenz, 'e', '-so', str(path), member], stdout=fh,
                                   stderr=subprocess.DEVNULL)
            if r.returncode or not Path(dest).stat().st_size:
                raise SystemExit(f'7z could not extract {member} from {path}')
        return names_cli, get_cli
    if have_7zip:
        raise SystemExit(not_archive)
    raise SystemExit(f'cannot open {path.name}: it is neither a zip nor readable without 7-Zip.\n'
                     'Install one of:  pip install py7zr   |   7-Zip (https://7-zip.org) or your '
                     'distro\'s p7zip package')


def _sevenzip() -> str | None:
    """A 7-Zip command line, or None. Windows installs it where nobody adds it to PATH."""
    for name in ('7z', '7za', '7zz', '7z.exe'):
        if (p := shutil.which(name)):
            return p
    for base in (os.environ.get('PROGRAMFILES'), os.environ.get('PROGRAMFILES(X86)')):
        if base and (exe := Path(base) / '7-Zip' / '7z.exe').exists():
            return str(exe)
    return None


def state_path(ref: str | Path) -> Path:
    """A save state, named either by path or by slot number.

    A bare number means "slot N of the configured game", which lives in PCSX2's savestates folder as
    `<SERIAL> (<crc>).NN.p2s`. The crc is not ours to know, so we glob for the slot and take the newest.
    """
    ref = str(ref)
    if not re.fullmatch(r'\d{1,2}', ref):
        return Path(ref).expanduser()
    folder = _folder('Savestates', 'sstates')
    serial = _plain_serial()
    pats = [f'{serial}*.{int(ref):02d}.p2s', f'*.{int(ref):02d}.p2s']
    for pat in pats:
        hits = sorted(folder.glob(pat), key=lambda f: f.stat().st_mtime, reverse=True)
        if hits:
            return hits[0]
    raise SystemExit(f'no save state in slot {ref} for {serial} under {folder}\n'
                     'Make one in the emulator (or `kit pine save <slot>`), or give a path instead.')


def _plain_serial() -> str:
    return (cfg().serial or '').replace('-', '').replace('_', '').upper()


def state_slots() -> list[tuple[int, Path, str]]:
    """(slot, file, kit.toml [states] note) for every slot file this instance can see."""
    folder = _folder('Savestates', 'sstates')
    notes = cfg().states
    out = []
    for f in folder.glob('*.p2s') if folder.is_dir() else []:
        bits = f.name.rsplit('.', 2)
        if len(bits) == 3 and bits[1].isdigit():
            out.append((int(bits[1]), f, notes.get(str(int(bits[1])), '')))
    return sorted(out)


def state_list(path: str | Path) -> list[tuple[str, int]]:
    path = state_path(path)
    if not path.exists():
        raise SystemExit(f'no such save state: {path}')
    names, _ = _state_open(path)
    return names()


def state_ram(path: str | Path, out: Path | None = None) -> Path:
    """Extract a save state's EE RAM image (`eeMemory.bin`) and return its path.

    This is the answer to "I need a static value while a capture is running": a second PINE client would
    hang both, but a save state's RAM image is a free, frozen copy of the same memory.
    """
    path = state_path(path)
    names, get = _state_open(path)
    entries = [n for n, _ in names()]
    member = next((n for n in entries if n.endswith('eeMemory.bin')), None)
    if not member:
        raise SystemExit(f'{path.name} has no eeMemory.bin (it holds: {", ".join(entries[:8])}…) — '
                         'is it really a PCSX2 save state?')
    out = Path(out) if out else cfg().ctx('states', f'{path.stem}.eeMemory.bin')
    out.parent.mkdir(parents=True, exist_ok=True)
    get(member, out)
    return out


def state_ee(path: str | Path) -> Path:
    """`state_ram`, but reusing the image already unpacked under context/states/.

    Unpacking 32 MB per lookup turns "read six struct fields out of slot 4" into a minute of 7-Zip.
    """
    src = state_path(path)
    cache = cfg().ctx('states', f'{src.stem}.eeMemory.bin')
    if cache.exists() and cache.stat().st_mtime >= src.stat().st_mtime:
        return cache
    return state_ram(src, cache)


def state_extract(path: str | Path, addr: int, length: int, out: Path | None = None) -> Path:
    """Bytes at an EE address, out of a save state. The address may be virtual (0x8…/0xa…)."""
    ram = state_ee(path)
    off = addr & 0x1fffffff                 # fold kseg0/kseg1; a state image is physical RAM from 0
    size = ram.stat().st_size
    if off + length > size:
        raise SystemExit(f'{addr:#x}+{length:#x} runs past the end of the {size / (1 << 20):.0f} MiB '
                         'RAM image — wrong address, or a state from a different emulator setting?')
    out = Path(out) if out else cfg().ctx('states', f'{Path(ram).stem}-{off:08x}-{length:x}.bin')
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(ram, 'rb') as fh:
        fh.seek(off)
        buf = fh.read(length)
    out.write_bytes(buf)
    return out


# --- CLI ---------------------------------------------------------------------

PCSX2_DOC = """start, stop and inspect PCSX2 — with PINE on, and without touching the user's own emulator.

  kit pcsx2 start [-- -fullscreen]   boot the configured disc with PINE enabled (no-op if up)
  kit pcsx2 stop                     close *this* instance only
  kit pcsx2 status
  kit pcsx2 copy 3                   create kit copy 3 (own PINE slot, pad, states, muted)
  kit pcsx2 state                    list the save-state slots this instance can see
  kit pcsx2 with -- CMD...           run CMD holding the PCSX2 lock (KIT_PCSX2_LOCKED=1 for children)

KIT_PCSX2=N makes every kit tool, the pad, PINE and the lock use copy N instead of your own emulator.
KIT_PCSX2_MAX_HOLD=<seconds> caps how long `with` may hold the lock, so one hung job cannot starve others."""


def cmd_pcsx2(argv: list[str]) -> int | None:
    # `with` is handled before argparse: everything after it belongs to the child command, flags and all.
    if argv and argv[0] == 'with':
        rest = argv[1:]
        if rest and rest[0] == '--':
            rest = rest[1:]
        return _with(rest)
    from ps2kit.cli import parser
    ap = parser('pcsx2', PCSX2_DOC)
    ap.add_argument('cmd', nargs='?', default='status',
                    choices=['start', 'stop', 'status', 'copy', 'state', 'with'])
    ap.add_argument('args', nargs='*', help='the copy number for `copy`; extra PCSX2 flags for `start`')
    a, extra = ap.parse_known_args(argv)
    if a.cmd == 'start':
        return start(list(a.args) + extra)
    if a.cmd == 'stop':
        return stop()
    if a.cmd == 'state':
        return _slots()
    if a.cmd == 'copy':
        if not a.args:
            raise SystemExit('kit pcsx2 copy N — which copy? (1..%d; [emu] instances)' % cfg().instances)
        copy_config(int(a.args[0]))
        return None
    return status()


def _slots() -> None:
    rows = state_slots()
    if not rows:
        print(f'no save states in {_folder("Savestates", "savestates")} yet '
              '(make one in PCSX2, or `kit pine save 8`)')
        return
    for slot, f, note in rows:
        mb = f.stat().st_size / (1 << 20)
        print(f'{slot:>3}  {mb:5.1f} MiB  {f.name}{"   " + note if note else ""}')


def _with(cmd: list[str]) -> int:
    """Hold the PCSX2 lock for a whole multi-step job (load a state, drive the pad, record, screenshot).

    Single tools lock themselves, but a sequence of them would release the lock between steps and let
    another agent's input land in the middle of your capture.
    """
    if not cmd:
        raise SystemExit('kit pcsx2 with -- CMD... — what should run while holding the lock?')
    from ps2kit.pine import take_lock
    take_lock()
    cap = int(os.environ.get('KIT_PCSX2_MAX_HOLD') or 0)
    env = {**os.environ, 'KIT_PCSX2_LOCKED': '1'}
    try:
        return subprocess.run(cmd, env=env, timeout=cap or None).returncode
    except subprocess.TimeoutExpired:
        print(f'pcsx2 with: stopped after holding PCSX2 for {cap}s (the KIT_PCSX2_MAX_HOLD cap); '
              'split the job into shorter runs', file=sys.stderr)
        return 124
    except FileNotFoundError:
        raise SystemExit(f'pcsx2 with: no such command: {cmd[0]}')


SHOT_DOC = """screenshot the emulator without focusing it (falls back to its own Screenshot hotkey).

  kit shot context/shots/menu.png
  kit shot out.png --window pcsx2        match a different window class/title"""


def cmd_shot(argv: list[str]) -> int | None:
    from ps2kit.cli import parser
    ap = parser('shot', SHOT_DOC)
    ap.add_argument('out', help='PNG to write')
    ap.add_argument('--window', default='pcsx2', metavar='PAT',
                    help='window class/title substring (default: pcsx2)')
    a = ap.parse_args(argv)
    print(shot(Path(a.out), a.window))
    return None


STATE_DOC = """look inside a PCSX2 save state (.p2s): a slot number, or a path to a file.

  kit state ram 4                         unpack slot 4's EE RAM into context/states/
  kit state ram slot3.p2s --out ram.bin
  kit state extract 4 --addr @table --len 0x400 --out context/tables/t.bin
  kit state extract 4 --addr @shot_table --len 0x400 --as f32
  kit state info 4   ·   kit state list 4

`extract` is how you read a table or a struct layout without an emulator at all.

A save state's RAM image is how you read static values during a capture: a second PINE client while a
recorder is polling hangs both."""



def _extract(a) -> int | None:
    """Pull one region out of a save state's RAM image — the cheap way to read a table or a struct.

    No emulator, no lock, repeatable forever, and safe while a recording is in flight.
    """
    from ps2kit.pine import decode, show
    if not a.addr or not a.length:
        raise SystemExit('kit state extract <state|slot> --addr <A> --len <N> [--as f32] [--out FILE]')
    addr, length = cfg().sym(a.addr), num(a.length)
    out = state_extract(a.state, addr, length, Path(a.out) if a.out else None)
    buf = out.read_bytes()
    print(f'{out}  ({len(buf)} bytes from {addr:#010x} of {Path(a.state).name})')
    if a.kind != 'hex':
        vals = decode(buf, a.kind)
        for i in range(0, min(len(vals), 64), 8):
            row = vals[i:i + 8]
            step = len(buf) // max(len(vals), 1)
            print(f'  {addr + i * step:#010x}  ' + '  '.join(show(v, a.kind) for v in row))
        if len(vals) > 64:
            print(f'  … {len(vals) - 64} more — the whole region is in {out.name}')
    return None


def cmd_state(argv: list[str]) -> int | None:
    from ps2kit.cli import parser
    ap = parser('state', STATE_DOC)
    ap.add_argument('cmd', choices=['ram', 'list', 'info', 'extract'])
    ap.add_argument('state', help='a .p2s save state, or a slot number')
    ap.add_argument('--out', metavar='FILE', help='where to write the RAM image or the extracted bytes')
    ap.add_argument('--addr', help='extract: start address (hex, 0x-hex or @name)')
    ap.add_argument('--len', dest='length', help='extract: how many bytes')
    ap.add_argument('--as', dest='kind', default='hex', help='extract: also print it as this type')
    a = ap.parse_args(argv)
    a.state = state_path(a.state)
    if a.cmd == 'extract':
        return _extract(a)
    if a.cmd in ('list', 'info'):
        if a.cmd == 'info':
            print(f'{a.state}  ({a.state.stat().st_size / (1 << 20):.1f} MiB on disk)')
        for name, size in state_list(a.state)[:60]:
            print(f'{size:>12}  {name}')
        return None
    out = state_ram(a.state, Path(a.out) if a.out else None)
    size = out.stat().st_size
    print(f'{out}  ({size / (1 << 20):.1f} MiB of EE RAM)' if size >= (1 << 20)
          else f'{out}  ({size} bytes — that is small for an EE RAM image; is the state complete?)')
    return None
