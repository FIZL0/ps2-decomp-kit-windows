"""Everything the kit does that is about the *operating system*, in one place — the Windows edition.

The rest of the kit is OS-neutral and goes through these functions. That is the whole point: this module
(plus `pad.py`'s backend and transport, the OS-bound bits of `emu.py` and `runner.py`, and the shell
wrappers) is the only place that differs from the Linux edition, and nothing else has to know.

What belongs here: where runtime files live, how a lock is taken, how a PCSX2 config dir is found and how a
copy of it is launched, how a window is screenshotted, how a process is found, checked and stopped, how PINE
is reached (socket type and address), and how a command line is quoted.

**Windows-first, not Windows-only.** Every function prefers the native Windows path (`ctypes` against
kernel32/user32/gdi32, `msvcrt` locks, TCP PINE, `-cfgpath` instances), and keeps the POSIX path where one
is cheap and honest — because the `kit` shell script is still here for WSL users, and because the kit's own
tests and `kit <cmd> --help` have to run on a Linux box. Where a POSIX equivalent would be a lie
(`PrintWindow`, ViGEm) the function says so and names the Linux edition instead of guessing.

**Windows-only imports live inside functions.** `msvcrt`, `ctypes.wintypes` and `vgamepad` do not exist on
Linux, so importing this module must never reach them.
"""
from __future__ import annotations

import contextlib
import csv
import os
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import time
import zlib
from pathlib import Path

WINDOWS = sys.platform.startswith('win')
MAC = sys.platform == 'darwin'
LINUX = not WINDOWS and not MAC

# Windows image names, in the order a 2.x install is likely to use them. Matched on the image name only —
# see `pids_named`.
PCSX2_IMAGES = ('pcsx2-qt', 'pcsx2x64-avx2', 'pcsx2x64', 'pcsx2')

# Process creation flags (winbase.h), named here so no other module carries a magic number.
DETACHED_PROCESS = 0x00000008
CREATE_NEW_CONSOLE = 0x00000010
CREATE_NEW_PROCESS_GROUP = 0x00000200


def _only_on_windows(what: str) -> None:
    raise SystemExit(f'{what} is implemented with the Windows API, and this is {sys.platform}. '
                     'Use the Linux edition of the kit (ps2-decomp-kit) on this machine.')


# --- where transient files live ----------------------------------------------

def win_runtime_dir(env: dict) -> Path:
    """`%LOCALAPPDATA%\\ps2kit` — pids, locks, the pad pipe's lock file. Pure, so it is testable off Windows.

    Windows has no XDG_RUNTIME_DIR and no `os.getuid()`: LOCALAPPDATA is already per-user, and it survives a
    reboot, which only means stale pid files — every reader verifies a pid against the process name anyway.
    APPDATA and TEMP are fallbacks for a stripped-down or service account.
    """
    for key in ('LOCALAPPDATA', 'APPDATA', 'TEMP', 'TMP'):
        if (v := env.get(key)):
            return Path(v) / 'ps2kit'
    return Path(env.get('USERPROFILE') or Path.home()) / 'AppData' / 'Local' / 'ps2kit'


def runtime_dir() -> Path:
    """Per-user directory for pids, locks and pipe markers: not in the repo, and not in the worktrees."""
    if WINDOWS:
        d = win_runtime_dir(dict(os.environ))
    else:
        d = os.environ.get('XDG_RUNTIME_DIR')
        d = Path(d) if d and Path(d).is_dir() else \
            Path(os.environ.get('TMPDIR') or '/tmp') / f'ps2kit-{os.getuid() if hasattr(os, "getuid") else "u"}'
    d.mkdir(parents=True, exist_ok=True)
    return d


def rt(name: str) -> Path:
    return runtime_dir() / name


def instance() -> str:
    """Which PCSX2 copy this process talks to: "" = the user's own, "1".."N" = a kit-managed copy.

    Set by the parallel runner (KIT_PCSX2=N) so several agents never touch each other's emulator.
    """
    return os.environ.get('KIT_PCSX2', '')


# --- locks -------------------------------------------------------------------

_HELD: set[str] = set()   # lock files this process holds; see Lock.held_by_other()


class Lock:
    """An advisory, process-wide lock file. Used to give one tool PCSX2 to itself.

    with Lock('pcsx2') as l:  ...        # blocks, printing one line, until it is free
    Lock('pcsx2').take(block=False)      # False if someone else holds it

    Windows uses `msvcrt.locking` (one byte at offset 0), which is a `LockFile` range lock: it is released
    when the handle is closed or the process dies, which is the behaviour the kit needs — a crashed recorder
    must not leave PCSX2 locked forever.

    Two Windows differences, both handled here rather than left to the caller:

    * `msvcrt.locking(LK_LOCK)` is not a blocking wait — it retries ten times, one second apart, and then
      raises. `take(block=True)` therefore loops itself.
    * A `LockFile` range is owned by the *handle*, not by the process, so a second handle on the same file
      inside the same process is refused exactly like another process's. `held_by_other()` would then report
      "someone else has it" about this very process. So the lock files this process holds are tracked in
      `_HELD` and consulted first; the probe is only reached for a lock nobody here owns. (The same is true
      of `flock` on a second file description, so both platforms get the same, correct, answer.)
    """

    def __init__(self, name: str, note: str = ''):
        self.path = rt(f'{name}{instance()}.lock')
        self.note = note or f'another process is using {name}'
        self._f = None

    def take(self, block: bool = True) -> bool:
        self._f = open(self.path, 'w')
        if WINDOWS:
            import msvcrt
            waited = False
            while True:
                try:
                    msvcrt.locking(self._f.fileno(), msvcrt.LK_NBLCK, 1)
                    _HELD.add(str(self.path))
                    return True
                except OSError:
                    if not block:
                        return False
                    if not waited:
                        print(f'waiting: {self.note}', file=sys.stderr, flush=True)
                        waited = True
                    time.sleep(0.5)
        import fcntl
        try:
            fcntl.flock(self._f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _HELD.add(str(self.path))
            return True
        except BlockingIOError:
            if not block:
                return False
            print(f'waiting: {self.note}', file=sys.stderr, flush=True)
            fcntl.flock(self._f, fcntl.LOCK_EX)
            _HELD.add(str(self.path))
            return True

    def release(self) -> None:
        if self._f:
            if WINDOWS:
                import msvcrt
                with contextlib.suppress(OSError):
                    msvcrt.locking(self._f.fileno(), msvcrt.LK_UNLCK, 1)
            self._f.close()
            self._f = None
        _HELD.discard(str(self.path))

    def held_by_other(self) -> bool:
        """True when a *different* process holds this lock. False for one this process holds (see the class
        docstring): `kit pcsx2 status` must not report the emulator busy because of its own lock."""
        if str(self.path) in _HELD:
            return False
        probe = Lock(self.path.stem.rstrip('0123456789'))
        probe.path = self.path
        if probe.take(block=False):
            probe.release()
            return False
        return True

    def __enter__(self):
        self.take()
        return self

    def __exit__(self, *_):
        self.release()


# --- PINE transport ----------------------------------------------------------

def pine_endpoint(slot: int) -> tuple[int, str | tuple[str, int]]:
    """(socket family, address) for PCSX2's PINE server on `slot`.

    `slot` is already the whole port number: `config.pine_slot` defaults to 28011 and the runner adds the
    instance number to it (copy 3 → 28014), which is PCSX2's own `28011 + n` convention.

    Windows: TCP on 127.0.0.1, port = slot — PCSX2 has no unix sockets there, so the slot *is* the port.
    Linux/macOS: a unix socket in the runtime dir, `pcsx2.sock` for the default slot, `pcsx2.sock.<slot>`
    otherwise.
    """
    if WINDOWS:
        return socket.AF_INET, ('127.0.0.1', slot)
    base = runtime_dir() / ('pcsx2.sock' if slot == 28011 else f'pcsx2.sock.{slot}')
    return socket.AF_UNIX, str(base)


def tune_socket(s: socket.socket, family: int) -> None:
    """Make a PINE socket behave the same on both transports.

    A unix socket delivers a small write at once; a TCP socket holds it back under Nagle's algorithm until
    the previous response is acknowledged. PINE is one tiny request and one tiny response per call, polled
    once per frame by every recorder, so Nagle turns a 60 Hz capture into a slideshow. Off it goes.
    """
    if family == socket.AF_INET:
        with contextlib.suppress(OSError):
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)


# --- PCSX2 install and config ------------------------------------------------

def win_config_dir(env: dict) -> Path:
    """`%USERPROFILE%\\Documents\\PCSX2` — where a stock Windows PCSX2 keeps `inis`, `bios`, `snaps`,
    `savestates`. Pure, so it is testable off Windows.

    PCSX2 2.x on Windows is "portable" when a `portable.ini` sits next to the exe, and otherwise uses the
    Documents folder. The portable case is not guessed at: point `[emu] pcsx2` at the exe and the kit's own
    copies (`-cfgpath`) are unaffected either way, but `kit doctor`'s BIOS check would look in the wrong
    place. USERPROFILE is used rather than the shell's Documents known-folder path because a redirected
    Documents (OneDrive) needs the registry, and `kit doctor` prints the path it used.
    """
    home = Path(env.get('USERPROFILE') or Path.home())
    return home / 'Documents' / 'PCSX2'


def pcsx2_config_dir(instance_n: str = '') -> Path:
    """Where PCSX2 keeps `inis/PCSX2.ini`, for the user's own install or for kit copy N."""
    if instance_n:
        return copies_root() / f's{instance_n}' / 'PCSX2'
    if WINDOWS:
        return win_config_dir(dict(os.environ))
    if MAC:
        return Path.home() / 'Library' / 'Application Support' / 'PCSX2'
    return Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'PCSX2'


def copies_root() -> Path:
    """Where kit-managed PCSX2 copies live: a sibling of the repo, never inside it."""
    from ps2kit.config import ROOT
    return ROOT.parent / f'{ROOT.name}-slots' / 'pcsx2'


def pcsx2_guesses(windows: bool | None = None) -> tuple[str, ...]:
    """Names to try on PATH when `[emu] pcsx2` names nothing that exists."""
    if WINDOWS if windows is None else windows:
        return tuple(f'{n}.exe' for n in PCSX2_IMAGES)
    return ('pcsx2-qt', 'PCSX2', 'pcsx2')


def pcsx2_search_dirs(env: dict | None = None) -> list[Path]:
    """Directories `kit setup` digs through for a PCSX2 nobody put on PATH.

    Windows installs it under Program Files or, for the portable build people actually download, wherever
    the zip was unpacked — so Downloads and the per-user Programs folder are worth a look.
    """
    env = dict(os.environ) if env is None else env
    home = Path(env.get('USERPROFILE') or env.get('HOME') or Path.home())
    if WINDOWS:
        out = [Path(v) / 'PCSX2' for v in (env.get('PROGRAMFILES'), env.get('PROGRAMFILES(X86)'),
                                           env.get('LOCALAPPDATA')) if v]
        out += [Path(env['LOCALAPPDATA']) / 'Programs' / 'PCSX2'] if env.get('LOCALAPPDATA') else []
        return out + [home / 'Downloads', home / 'Desktop', home / 'Documents', Path('C:/PCSX2')]
    return [home / 'Applications', home / 'bin', Path('/opt'), home / 'Downloads', Path('/usr/games')]


def ghidra_search_dirs(env: dict | None = None) -> list[Path]:
    """Directories `kit setup` digs through for a Ghidra install (it is always a plain unzipped folder)."""
    env = dict(os.environ) if env is None else env
    home = Path(env.get('USERPROFILE') or env.get('HOME') or Path.home())
    if WINDOWS:
        roots = [Path(v) for v in (env.get('PROGRAMFILES'), env.get('LOCALAPPDATA')) if v]
        roots += [home, home / 'Downloads', home / 'Desktop', Path('C:/'), Path('C:/tools')]
        return roots
    return [Path('/opt'), Path('/usr/share'), Path('/usr/local'), home, home / 'Downloads',
            home / '.local' / 'share']


def ghidra_headless(home: Path) -> Path:
    """Ghidra's headless analyser inside an install directory — `.bat` on Windows, bare script elsewhere."""
    return Path(home) / 'support' / ('analyzeHeadless.bat' if WINDOWS else 'analyzeHeadless')


def ghidra_user_dirs(env: dict | None = None) -> list[Path]:
    r"""Roots where Ghidra keeps a *user-installed* extension, newest-looking first.

    Ghidra 10.2+ writes its per-user settings under the platform's own config location —
    `%APPDATA%\ghidra` on Windows, `$XDG_CONFIG_HOME/ghidra` (or `~/.config/ghidra`) elsewhere — and
    older versions used `~/.ghidra`. Each holds one directory per Ghidra version, so the caller globs
    `*/Extensions` inside these.
    """
    env = dict(os.environ) if env is None else env
    home = Path(env.get('USERPROFILE') or env.get('HOME') or Path.home())
    if WINDOWS:
        appdata = env.get('APPDATA')
        return [Path(appdata) / 'ghidra' if appdata else home / 'AppData' / 'Roaming' / 'ghidra',
                home / '.ghidra']
    return [Path(env.get('XDG_CONFIG_HOME') or home / '.config') / 'ghidra', home / '.ghidra']


def pcsx2_cmd(cmd: str) -> list[str]:
    """Turn the configured pcsx2 setting into something runnable (a path, an exe name, or an AppImage)."""
    p = Path(cmd).expanduser()
    if p.exists():
        return [str(p)]
    found = shutil.which(cmd)
    if found:
        return [found]
    for guess in pcsx2_guesses():
        if (found := shutil.which(guess)):
            return [found]
    raise SystemExit(f'PCSX2 not found (tried {cmd!r} and PATH). Set [emu] pcsx2 in kit.toml.')


# --- launching a kit copy ----------------------------------------------------

def instance_args(instance_n: str, windows: bool | None = None) -> list[str]:
    """Extra PCSX2 arguments that make it use kit copy N's config instead of the user's own.

    Windows has no XDG_CONFIG_HOME, so a copy is pinned with PCSX2's own `-cfgpath <dir>`, which points at
    the directory holding `inis/`. Same directory layout as the Linux edition's, so `pcsx2_config_dir()`,
    `copy_config()` and every savestate path are unchanged.
    """
    if not instance_n:
        return []
    if WINDOWS if windows is None else windows:
        return ['-cfgpath', str(pcsx2_config_dir(instance_n))]
    return []


def instance_env(instance_n: str, windows: bool | None = None) -> dict[str, str]:
    """Environment that makes PCSX2 copy N use its own config and its own pad.

    On Linux a copy is pinned by `XDG_CONFIG_HOME`, and its pad by VID/PID: `UinputPad` gives copy N the
    product id 0x500N, so `SDL_GAMECONTROLLER_IGNORE_DEVICES_EXCEPT` makes copy N see pad N and nothing
    else. **Neither applies on Windows**: the config comes from `instance_args` instead, and a ViGEm pad
    always presents Microsoft's own 360 VID/PID (0x045E/0x028E) — it cannot be renamed, so no VID/PID filter
    can tell two kit pads apart. Copy N is pinned to its pad by device *index* instead; see `pad_ini_rows`.
    """
    if not instance_n or (WINDOWS if windows is None else windows):
        return {}
    return {'XDG_CONFIG_HOME': str(copies_root() / f's{instance_n}'),
            'SDL_GAMECONTROLLER_IGNORE_DEVICES_EXCEPT': f'0x1209/0x{0x5000 + int(instance_n):04x}'}


def user_pad_env(instances: int, windows: bool | None = None) -> dict[str, str]:
    """Environment for the *user's own* emulator, so it never binds a kit pad as its controller 1.

    On Windows this is empty, and that is a real limitation rather than an omission: every ViGEm pad looks
    exactly like a Microsoft 360 pad, so there is nothing for `SDL_GAMECONTROLLER_IGNORE_DEVICES` to match.
    Bind your own controller explicitly in your own PCSX2, or stop the kit's pads (`kit pcsx2 stop` in
    each slot) before you play.
    """
    if WINDOWS if windows is None else windows:
        return {}
    return {'SDL_GAMECONTROLLER_IGNORE_DEVICES':
            ','.join(f'0x1209/0x{0x5000 + i:04x}' for i in range(1, instances + 1))}


def pad_ini_rows(n: int, windows: bool | None = None) -> list[tuple[str, str, str]]:
    """(section, key, value) rows that give kit copy `n` its pad and its pad-bound hotkeys.

    A copy usually runs where no compositor will render it, so the pad is the only input it has: pause and
    screenshot are bound to pad buttons (see `emu.copy_config`). Keyboard input is off, because a window
    that steals focus must not let the user's typing pause a capture.

    Windows differs in two ways. The pad is matched by *device index* (`SDL-<n-1>`) because ViGEm pads are
    indistinguishable by VID/PID — so copy n must be the n-th pad SDL enumerates, which holds while the
    runner starts the copies in order and the machine has no other controller plugged in. And XInput is
    turned off: with both sources on, PCSX2 sees every ViGEm pad twice, and the second copy of pad 1 lands
    on controller 2.
    """
    dev = f'SDL-{max(0, n - 1)}' if (WINDOWS if windows is None else windows) else 'SDL-0'
    rows = [('InputSources', 'Keyboard', 'false'),
            ('InputSources', 'SDL', 'true'),
            ('Hotkeys', 'TogglePause', f'{dev}/Guide'),
            ('Hotkeys', 'Screenshot', f'{dev}/LeftStick & {dev}/RightStick')]
    if WINDOWS if windows is None else windows:
        rows.insert(2, ('InputSources', 'XInput', 'false'))
    return rows


# --- quoting -----------------------------------------------------------------

def quote_args(args: list[str]) -> str:
    """Append-able command-line text for the local shell.

    `shlex.quote` produces POSIX quoting, which `cmd.exe` reads as literal characters: a `[build] test`
    command with an extra argument would be handed `'--test'` including the quotes. Windows gets
    `subprocess.list2cmdline`, which is the quoting `CommandLineToArgvW` undoes.
    """
    if not args:
        return ''
    return subprocess.list2cmdline(args) if WINDOWS else ' '.join(shlex.quote(a) for a in args)


def link(src: Path, dst: Path) -> bool:
    """Point `dst` at `src` without copying it. True if something now exists there.

    The runner links `context/` and the disc image into every worktree: they are tens of gigabytes and must
    not be copied five times. On Windows a symlink needs Developer Mode or an elevated process, which an
    unattended run cannot assume, so a directory gets a junction (`mklink /J`, no privilege at all) and a
    file gets a hard link — and only if both of those fail does the slot go without, which the caller
    reports rather than hides.
    """
    try:
        dst.symlink_to(src)
        return True
    except OSError:
        pass
    if not WINDOWS:
        return dst.exists()
    if src.is_dir():
        r = subprocess.run(['cmd', '/c', 'mklink', '/J', str(dst), str(src)],
                           capture_output=True, text=True)
        if r.returncode == 0:
            return True
    try:
        os.link(src, dst)          # same volume only, which a worktree beside the repo is
        return True
    except OSError:
        return dst.exists()


def py_cmd(script: Path, *args: str) -> str:
    """Shell text that runs one of our own scripts — for a hook or a settings file, not for `subprocess`.

    On POSIX `python3` is the name that exists, and hardcoding it keeps a hook working whatever started it.
    Windows has no such name: the `python3` on PATH is usually the Store alias, a stub that opens the Store
    and exits, so the only interpreter we can name with confidence is the one running now.
    """
    exe = str(sys.executable) if WINDOWS else 'python3'
    return quote_args([exe, str(script), *args])


# --- processes ---------------------------------------------------------------

def tasklist_pids(out: str, name: str) -> list[int]:
    """Pids out of `tasklist /FO CSV /NH` output whose image name is `name` (with or without `.exe`).

    Pure, and the parser is separate from the call because `tasklist` is the slow, locale-dependent fallback
    and its CSV is the only part worth testing: the headline-suppressed form is `"image","pid","session",…`,
    and a localised Windows translates the *headers* but not the image name or the pid.
    """
    want = name.lower().removesuffix('.exe')
    pids = []
    for row in csv.reader(out.splitlines()):
        if len(row) >= 2 and row[0].lower().removesuffix('.exe') == want and row[1].strip().isdigit():
            pids.append(int(row[1].strip()))
    return pids


def _snapshot() -> list[tuple[int, int, str]]:
    """(pid, parent pid, image name) for every process, via CreateToolhelp32Snapshot.

    This is the whole reason the kit does not shell out: `tasklist` costs ~100 ms, writes localised output,
    and cannot report a parent pid at all — which `children()` needs to tell "the agent is thinking" from
    "the agent is running a tool".
    """
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
                    ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.c_void_p),
                    ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
                    ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', ctypes.c_long),
                    ('dwFlags', wintypes.DWORD), ('szExeFile', wintypes.WCHAR * 260)]

    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    snap = k.CreateToolhelp32Snapshot(0x00000002, 0)       # TH32CS_SNAPPROCESS
    if snap == wintypes.HANDLE(-1).value:
        return []
    e = PROCESSENTRY32W()
    e.dwSize = ctypes.sizeof(e)
    out = []
    try:
        ok = k.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            out.append((int(e.th32ProcessID), int(e.th32ParentProcessID), str(e.szExeFile)))
            ok = k.Process32NextW(snap, ctypes.byref(e))
    finally:
        k.CloseHandle(snap)
    return out


def pids_named(name: str) -> list[int]:
    """Pids of running processes whose executable is exactly `name` — never a command-line match.

    A command-line match (`pgrep -f`, `wmic ... like`) is the classic self-shooting foot: the pattern is in
    the searching shell's own command line too, so `pkill -f pat` kills the caller. The kit only ever
    matches on the program name, and tracks its own children by pid file.
    """
    if WINDOWS:
        want = name.lower().removesuffix('.exe')
        try:
            return [p for p, _, img in _snapshot() if img.lower().removesuffix('.exe') == want]
        except OSError:
            out = subprocess.run(['tasklist', '/FI', f'IMAGENAME eq {want}.exe', '/FO', 'CSV', '/NH'],
                                 capture_output=True, text=True).stdout
            return tasklist_pids(out, want)
    out = subprocess.run(['pgrep', '-x', name], capture_output=True, text=True).stdout
    return [int(l) for l in out.split()]


def alive(pid: int | None) -> bool:
    """Is this pid a live process? (Not "is it still the process I started" — callers check the name too.)"""
    if not pid:
        return False
    if WINDOWS:
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.OpenProcess.restype = wintypes.HANDLE
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        h = k.OpenProcess(0x1000, False, int(pid))         # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            # 5 = access denied: it exists and belongs to somebody else. 87 = no such pid.
            return ctypes.get_last_error() == 5
        code = wintypes.DWORD()
        try:
            if not k.GetExitCodeProcess(h, ctypes.byref(code)):
                return True
            return code.value == 259                        # STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError) as e:
        return isinstance(e, PermissionError)


def proc_name(pid: int) -> str:
    """The image name of a pid, without `.exe`. Empty when it is gone — pids get reused, so `emu.running()`
    checks this before believing its own pid file."""
    if WINDOWS:
        try:
            return next((img.removesuffix('.exe') for p, _, img in _snapshot() if p == int(pid)), '')
        except OSError:
            out = subprocess.run(['tasklist', '/FI', f'PID eq {pid}', '/FO', 'CSV', '/NH'],
                                 capture_output=True, text=True).stdout
            row = next(iter(csv.reader(out.splitlines())), [])
            return row[0].removesuffix('.exe') if row else ''
    return subprocess.run(['ps', '-o', 'comm=', '-p', str(pid)], capture_output=True, text=True).stdout.strip()


def children(pid: int) -> list[int]:
    """Direct children of a pid. Used to tell an agent that is thinking from one that is running a tool."""
    if not pid:
        return []
    if WINDOWS:
        try:
            return [p for p, parent, _ in _snapshot() if parent == int(pid)]
        except OSError:
            return []
    out = subprocess.run(['pgrep', '-P', str(pid)], capture_output=True, text=True).stdout
    return [int(l) for l in out.split()]


def terminate(pid: int) -> None:
    """Ask a process to stop — the gentlest thing that works, in three steps.

    Windows has no SIGTERM. `WM_CLOSE` to the process's own windows is the real equivalent for PCSX2: it
    shuts the VM down and flushes its memory cards, where `TerminateProcess` would not. A console process
    (an agent session) has no window, so it gets a Ctrl+Break — which reaches it because
    `spawn_detached`/`spawn_console` put every child in its own process group. Only a process with neither
    is killed outright.

    Callers wait and re-check `alive()` afterwards; nothing here blocks.
    """
    if WINDOWS:
        import ctypes
        from ctypes import wintypes
        u = ctypes.WinDLL('user32', use_last_error=True)
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.OpenProcess.restype = wintypes.HANDLE
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        if (hwnds := _windows_of(int(pid))):
            for h in hwnds:
                u.PostMessageW(wintypes.HWND(h), 0x0010, 0, 0)     # WM_CLOSE
            return
        if k.GenerateConsoleCtrlEvent(1, int(pid)):                # CTRL_BREAK_EVENT
            return
        h = k.OpenProcess(0x0001, False, int(pid))                 # PROCESS_TERMINATE
        if h:
            try:
                k.TerminateProcess(h, 1)
            finally:
                k.CloseHandle(h)
        return
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, 15)


def spawn_detached(argv: list[str], log: Path | None = None, env: dict | None = None) -> int:
    """Start a program that outlives this process; returns its pid."""
    out = open(log, 'ab') if log else subprocess.DEVNULL
    kw: dict = {'stdout': out, 'stderr': out, 'stdin': subprocess.DEVNULL,
                'env': {**os.environ, **(env or {})}}
    if WINDOWS:
        # No console of its own (DETACHED_PROCESS), and its own process group so a Ctrl+C in the terminal
        # that started it does not take the emulator down with it.
        kw['creationflags'] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw['start_new_session'] = True
    return subprocess.Popen(argv, **kw).pid


def spawn_console(argv: list[str], cwd: Path | None = None, env: dict | None = None) -> int:
    """Start a program in a console window of its own; returns its pid.

    The Windows fallback for `kit run` when Windows Terminal is not installed: an agent session needs a
    terminal a human can read and type into, and one new console per slot is the plainest way to get N of
    them. On POSIX this is what a terminal emulator or tmux does, so it is not implemented there.
    """
    if not WINDOWS:
        _only_on_windows('a console window per agent session')
    return subprocess.Popen(argv, cwd=str(cwd) if cwd else None,
                            env={**os.environ, **(env or {})},
                            creationflags=CREATE_NEW_CONSOLE | CREATE_NEW_PROCESS_GROUP).pid


# --- screen capture ----------------------------------------------------------

def png_bytes(width: int, height: int, pixels: bytes, stride: int | None = None) -> bytes:
    """A top-down BGRA buffer as an 8-bit RGB PNG. Pure stdlib, so a screenshot needs no Pillow.

    `GetDIBits` with a negative `biHeight` hands back exactly this layout, and twenty lines of `zlib` +
    `struct` turn it into a file every viewer reads — which is a better trade than making the one capture
    path in the kit depend on a wheel that has to match the interpreter.
    """
    stride = stride or width * 4
    raw = bytearray()
    for y in range(height):
        row = pixels[y * stride:y * stride + width * 4]
        rgb = bytearray(width * 3)
        rgb[0::3], rgb[1::3], rgb[2::3] = row[2::4], row[1::4], row[0::4]
        raw.append(0)                       # filter type 0 (none) for this scanline
        raw += rgb

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack('>I', len(data)) + tag + data
                + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff))

    return (b'\x89PNG\r\n\x1a\n'
            + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(bytes(raw), 6))
            + chunk(b'IEND', b''))


def window_pick(windows: list[tuple[int, str]], pattern: str) -> list[int]:
    """The handles whose title contains `pattern`, case-insensitively, in enumeration order. Pure.

    Title matching is all Windows offers: there is no window *class* worth matching (PCSX2's Qt window class
    is a Qt internal), and the title is `<game> [<serial>] | PCSX2 <version>` while a game runs and plain
    `PCSX2` before that — so the default `pcsx2` pattern hits both. Several copies all match it, which is
    why `emu.shot()` sends a kit copy to its own screenshot hotkey instead of guessing.
    """
    p = pattern.lower()
    return [h for h, title in windows if p in (title or '').lower()]


def _enum_windows() -> list[tuple[int, str]]:
    """(hwnd, title) for every visible top-level window."""
    import ctypes
    from ctypes import wintypes
    u = ctypes.WinDLL('user32', use_last_error=True)
    out: list[tuple[int, str]] = []
    CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def seen(hwnd, _):
        if u.IsWindowVisible(hwnd):
            n = u.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                u.GetWindowTextW(hwnd, buf, n + 1)
                out.append((int(hwnd), buf.value))
        return True

    u.EnumWindows(CB(seen), 0)
    return out


def _windows_of(pid: int) -> list[int]:
    """Top-level window handles belonging to a pid (for a polite WM_CLOSE)."""
    if not WINDOWS:
        return []
    import ctypes
    from ctypes import wintypes
    u = ctypes.WinDLL('user32', use_last_error=True)
    out = []
    for h, _ in _enum_windows():
        owner = wintypes.DWORD()
        u.GetWindowThreadProcessId(wintypes.HWND(h), ctypes.byref(owner))
        if owner.value == pid:
            out.append(h)
    return out


def screenshot_tools() -> list[str]:
    """Which screen-capture helpers this machine has, best first. `kit doctor` reports them; empty is
    not fatal, because the emulator's own screenshot hotkey is always available as a fallback."""
    if WINDOWS:
        return ['printwindow'] + (['powershell'] if shutil.which('powershell') else [])
    names = ('grim', 'hyprctl', 'xdotool', 'import', 'spectacle')
    return [t for t in names if shutil.which(t)]


def screenshot_window(out: Path, pattern: str = 'pcsx2') -> bool:
    """Grab a window whose title contains `pattern`, without focusing it. False if it can't.

    Windows does this with `PrintWindow(PW_RENDERFULLCONTENT)`, which asks the window to redraw itself into
    a bitmap we own: no focus, no raise, and it works for a window another one is sitting on top of. It does
    *not* work for a minimized window, and some drivers hand back a blank client area for a
    hardware-rendered one — so a blank grab is retried over the whole window frame, and then given up on, at
    which point `emu.shot()` falls back to the emulator's own Screenshot hotkey, which always works.
    """
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if WINDOWS:
        hwnds = window_pick(_enum_windows(), pattern)
        for hwnd in hwnds[:1]:
            for client_only in (True, False):
                got = _print_window(hwnd, client_only)
                if got:
                    w, h, buf = got
                    out.write_bytes(png_bytes(w, h, buf))
                    return True
        return bool(hwnds) and _powershell_shot(out, pattern)
    # Hyprland / wlroots
    if shutil.which('hyprctl') and shutil.which('grim'):
        import json
        try:
            clients = json.loads(subprocess.run(['hyprctl', 'clients', '-j'],
                                                capture_output=True, text=True, check=True).stdout)
        except Exception:
            clients = []
        for c in clients:
            if pattern.lower() in f'{c.get("class", "")} {c.get("title", "")}'.lower():
                geom = f'{c["at"][0]},{c["at"][1]} {c["size"][0]}x{c["size"][1]}'
                return subprocess.run(['grim', '-g', geom, str(out)]).returncode == 0
    # GNOME / KDE Wayland portal, X11
    if shutil.which('xdotool') and shutil.which('import'):
        wid = subprocess.run(['xdotool', 'search', '--name', pattern], capture_output=True, text=True).stdout.split()
        if wid:
            return subprocess.run(['import', '-window', wid[0], str(out)]).returncode == 0
    if shutil.which('spectacle'):
        return subprocess.run(['spectacle', '-abno', str(out)]).returncode == 0
    return False


def _print_window(hwnd: int, client_only: bool) -> tuple[int, int, bytes] | None:
    """(width, height, top-down BGRA) for one window, or None if the grab failed or came back blank."""
    import ctypes
    from ctypes import wintypes

    class RECT(ctypes.Structure):
        _fields_ = [('left', ctypes.c_long), ('top', ctypes.c_long),
                    ('right', ctypes.c_long), ('bottom', ctypes.c_long)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [('biSize', wintypes.DWORD), ('biWidth', ctypes.c_long), ('biHeight', ctypes.c_long),
                    ('biPlanes', wintypes.WORD), ('biBitCount', wintypes.WORD),
                    ('biCompression', wintypes.DWORD), ('biSizeImage', wintypes.DWORD),
                    ('biXPelsPerMeter', ctypes.c_long), ('biYPelsPerMeter', ctypes.c_long),
                    ('biClrUsed', wintypes.DWORD), ('biClrImportant', wintypes.DWORD)]

    u = ctypes.WinDLL('user32', use_last_error=True)
    g = ctypes.WinDLL('gdi32', use_last_error=True)
    for f, rt_ in ((u.GetWindowDC, wintypes.HDC), (g.CreateCompatibleDC, wintypes.HDC),
                   (g.CreateCompatibleBitmap, wintypes.HANDLE), (g.SelectObject, wintypes.HANDLE)):
        f.restype = rt_                       # a handle is 64-bit; the default c_int return truncates it
    h = wintypes.HWND(hwnd)
    r = RECT()
    (u.GetClientRect if client_only else u.GetWindowRect)(h, ctypes.byref(r))
    w, ht = r.right - r.left, r.bottom - r.top
    if w <= 0 or ht <= 0 or w * ht > 64 << 20:
        return None
    hdc = u.GetWindowDC(h)
    mem = g.CreateCompatibleDC(hdc)
    bmp = g.CreateCompatibleBitmap(hdc, w, ht)
    old = g.SelectObject(mem, bmp)
    # PW_CLIENTONLY (1) drops the title bar and border; PW_RENDERFULLCONTENT (2) is what makes a
    # hardware-composited window (an emulator) redraw itself instead of handing back grey.
    ok = u.PrintWindow(h, mem, (1 if client_only else 0) | 2)
    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(bi)
    bi.biWidth, bi.biHeight = w, -ht          # negative height = top-down rows, which png_bytes wants
    bi.biPlanes, bi.biBitCount, bi.biCompression = 1, 32, 0
    buf = ctypes.create_string_buffer(w * ht * 4)
    rows = g.GetDIBits(mem, bmp, 0, ht, buf, ctypes.byref(bi), 0)
    g.SelectObject(mem, old)
    g.DeleteObject(bmp)
    g.DeleteDC(mem)
    u.ReleaseDC(h, hdc)
    if not ok or not rows:
        return None
    data = buf.raw[:w * ht * 4]
    return None if data.count(0) == len(data) else (w, ht, data)


def _powershell_shot(out: Path, pattern: str) -> bool:
    """Last resort: have PowerShell copy the window's screen rectangle with System.Drawing.

    Only reached when `PrintWindow` returns nothing usable. It copies from the *screen*, so whatever is in
    front of the emulator ends up in the PNG — which is why it is last, below the emulator's own hotkey in
    everything but ordering, and why it is one helper and not a code path of its own.
    """
    if not shutil.which('powershell'):
        return False
    ps = r'''
param($pat, $out)
Add-Type -AssemblyName System.Drawing, System.Windows.Forms
Add-Type -Namespace W -Name N -MemberDefinition '
[DllImport("user32.dll")] public static extern IntPtr FindWindow(string c, string n);
[DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out int[] r);'
$w = (Get-Process | Where-Object { $_.MainWindowTitle -like "*$pat*" } | Select-Object -First 1)
if (-not $w) { exit 1 }
$b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
$gfx = [System.Drawing.Graphics]::FromImage($bmp)
$gfx.CopyFromScreen($b.Location, [System.Drawing.Point]::Empty, $b.Size)
$bmp.Save($out, [System.Drawing.Imaging.ImageFormat]::Png)
exit 0
'''
    r = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', ps,
                        '-pat', pattern, '-out', str(out)], capture_output=True)
    return r.returncode == 0 and out.exists()


def wait_for_file(d: Path, before: set[str], tries: int = 40, delay: float = 0.3) -> Path | None:
    """Wait for a new, fully-written file to appear in `d` (emulator screenshots land asynchronously)."""
    for _ in range(tries):
        time.sleep(delay)
        new = {f.name for f in d.iterdir()} - before if d.is_dir() else set()
        if new:
            f = d / sorted(new)[-1]
            size = -1
            for _ in range(30):  # PCSX2 creates the file, then writes it
                time.sleep(delay)
                s = f.stat().st_size
                if s and s == size:
                    return f
                size = s
            return f
    return None


# --- virtual gamepad backend -------------------------------------------------

def pad_backend() -> str:
    """Which virtual-pad implementation `ps2kit/pad.py` should use on this OS."""
    if WINDOWS:
        return 'vigem'
    if LINUX:
        return 'uinput'
    return 'none'


def pad_available() -> tuple[bool, str]:
    """(usable, why not) for the virtual pad, so `kit doctor` can explain the fix."""
    if WINDOWS:
        try:
            import vgamepad  # noqa: F401
        except ImportError:
            return False, ('the vgamepad package is missing (pip install vgamepad). Its installer also '
                           'offers the ViGEmBus driver, which is what actually creates the pad — without '
                           'it `kit pad serve` fails at the first device.')
        except Exception as e:      # vgamepad loads its DLL at import: a missing driver shows up here
            return False, (f'vgamepad is installed but will not load ({e}). Install the ViGEmBus driver '
                           '(https://github.com/nefarius/ViGEmBus/releases) and reboot.')
        return True, ''
    if LINUX:
        if not Path('/dev/uinput').exists():
            return False, '/dev/uinput is missing (load the uinput module: sudo modprobe uinput)'
        if not os.access('/dev/uinput', os.W_OK):
            return False, ('/dev/uinput is not writable by you — add a udev rule or an ACL:\n'
                           '    sudo setfacl -m u:$USER:rw /dev/uinput   # until reboot\n'
                           '  or /etc/udev/rules.d/99-uinput.rules:\n'
                           '    KERNEL=="uinput", MODE="0660", GROUP="input", OPTIONS+="static_node=uinput"')
        try:
            import evdev  # noqa: F401
        except ImportError:
            return False, 'the python-evdev package is missing (pip install evdev)'
        return True, ''
    return False, f'no virtual-pad backend for {sys.platform}'
