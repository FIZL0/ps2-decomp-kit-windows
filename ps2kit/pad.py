r"""A virtual controller, so a script can play the game.

Why: half of what you want to measure only happens after someone presses a button — walk to the menu, start
a level, swing at the ball. PCSX2 has no "press X" IPC command, so the kit creates a *real* gamepad at the OS
level and PCSX2 binds it like any other pad. That also means a kit-managed copy can be paused and
screenshotted with no window to click on (its TogglePause and Screenshot hotkeys are bound to pad buttons —
see `emu.copy_config`).

    kit pad serve                            # creates the device; keep it running
    kit pad send "press cross 120"           # hold Cross for 120 ms
    kit pad send "stick l 0 -1" "sleep 300" "release"
    kit pad send "dpad right 100; press cross 100"   # ';' separates commands on one line
    kit pad script moves.txt                 # one command per line, '#' comments

Commands: press <btn> <ms> · down <btn> · up <btn> · dpad <dir> <ms> · stick <l|r> <x> <y> (-1..1,
          y down = +1) · sleep <ms> (alias wait) · release (everything neutral).
Durations are **milliseconds**; a `120f` suffix means 120 frames instead, since every other kit tool counts
in frames. A press shorter than one frame is pointless — PCSX2 reads the pad once per frame — so the script
says so instead of pressing nothing.
Buttons, by PS2 name: cross circle square triangle start l1 r1 l2 r2 l3 r3 up down left right guide.

`select` is refused on purpose: PCSX2's own hotkeys are Select + shoulder combinations, so a script that
"just presses Select" silently saves a state, toggles turbo or loads over your game. The reference project
learned that the hard way.

The transport is a named pipe, `\\.\pipe\ps2kit-pad<instance>`, one per PCSX2 copy: `serve` is the pipe
server, `send` a client that writes one batch and disconnects. (The Linux edition uses a FIFO, which a shell
can `echo` into; a named pipe needs a real open, so `kit pad send` is the way in.)

**Timing is wall-clock, not frame-exact.** A `press cross 120` lands wherever the 120 ms happen to fall;
re-running a script does not reproduce the same frames. For anything that must be frame-exact (a replay you
intend to compare against your port) record PCSX2's own input recording instead and play that back.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from ps2kit import platform_ as plat

# PS2 button names → the Xbox-layout meaning SDL will see. Everything speaks PS2 names because the game's
# own documentation and the decompilation do.
FACE = ('cross', 'circle', 'square', 'triangle')
DPAD = ('up', 'down', 'left', 'right')
TRIGGERS = ('l2', 'r2')
BUTTONS = FACE + ('start', 'l1', 'r1', 'l3', 'r3', 'guide') + DPAD + TRIGGERS


def _fps() -> float:
    """Frames per second for duration suffixes. PAL discs run at 50, everything else at 60.

    Only used to convert `120f` into wall-clock; nothing here is frame-exact either way.
    """
    try:
        from ps2kit.config import cfg
        r = (cfg(required=False).region or '').upper()
    except Exception:                      # no kit.toml yet: a script should still run
        r = ''
    return 50.0 if r in ('EU', 'PAL', 'E') else 60.0


def fifo() -> Path:
    """The runtime file that stands for this copy's pad server.

    On Linux it *is* the transport (a FIFO). On Windows the transport is a named pipe, which does not live
    in the filesystem, so this path is only the name the server's lock hangs off — `transport()` is the
    address and `serving()` the question "is anybody listening". Kept under its old name because it is the
    lock's path and because one per PCSX2 copy is what stops parallel agents pressing each other's buttons.
    """
    return plat.rt(f'pad{plat.instance()}.fifo')


def pipe_name(inst: str) -> str:
    r"""The Windows named pipe for PCSX2 copy `inst` (`''` = the user's own emulator). Pure.

    `\\.\pipe\...` is a flat per-machine namespace, so the instance has to be in the name; `ps2kit-pad` and
    `ps2kit-pad3` are then as separate as `pad.fifo` and `pad3.fifo` are on Linux.
    """
    return r'\\.\pipe\ps2kit-pad' + str(inst)


def transport() -> str:
    """Where `send` writes and `serve` listens, as text for a message."""
    return pipe_name(plat.instance()) if plat.WINDOWS else str(fifo())


def serving() -> bool:
    """Is a pad server listening for this copy?

    Windows: `WaitNamedPipeW` with a zero timeout. It says "yes" for a pipe that exists but is busy with
    another client (ERROR_SEM_TIMEOUT), which is the right answer — the server is there, `send` just has to
    wait its turn — and "no" only when the pipe does not exist at all.
    """
    if not plat.WINDOWS:
        return fifo().exists()
    import ctypes
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    if k.WaitNamedPipeW(ctypes.c_wchar_p(transport()), 0):
        return True
    return ctypes.get_last_error() == 121        # ERROR_SEM_TIMEOUT: exists, all instances busy


# --- backends ----------------------------------------------------------------

class PadBackend:
    """What a backend must do. `serve()` drives it; `Script` turns text into these calls.

    Kept this thin on purpose: the Windows edition adds a ViGEm class with the same five methods and
    changes nothing else in the kit.
    """

    name = 'none'

    def open(self) -> str:
        raise NotImplementedError

    def button(self, name: str, down: bool) -> None:
        raise NotImplementedError

    def axis(self, side: str, x: float, y: float) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass


class UinputPad(PadBackend):
    """Linux: a kernel-level pad via /dev/uinput and python-evdev.

    It identifies as a Microsoft Xbox 360 pad so SDL (and therefore PCSX2) applies its standard mapping with
    no per-device configuration. A kit copy N instead uses the pid.codes test vendor with product 0x500N, so
    that `SDL_GAMECONTROLLER_IGNORE_DEVICES_EXCEPT=0x1209/0x500N` pins copy N to pad N and nothing else —
    without it, eight copies all grab pad 1 and every agent plays the same game.
    """

    name = 'uinput'

    def open(self) -> str:
        ok, why = plat.pad_available()
        if not ok:
            raise SystemExit(f'no virtual pad: {why}')
        from evdev import AbsInfo, UInput, ecodes as e
        self.e = e
        # evdev's BTN_NORTH/BTN_WEST are swapped relative to their names; use the BTN_A..BTN_Y aliases,
        # which are what SDL reads to guess "this is an Xbox layout".
        self.keys = {'cross': e.BTN_A, 'circle': e.BTN_B, 'square': e.BTN_X, 'triangle': e.BTN_Y,
                     'start': e.BTN_START, 'l1': e.BTN_TL, 'r1': e.BTN_TR,
                     'l3': e.BTN_THUMBL, 'r3': e.BTN_THUMBR, 'guide': e.BTN_MODE}
        self.hats = {'up': (e.ABS_HAT0Y, -1), 'down': (e.ABS_HAT0Y, 1),
                     'left': (e.ABS_HAT0X, -1), 'right': (e.ABS_HAT0X, 1)}
        self.trigs = {'l2': e.ABS_Z, 'r2': e.ABS_RZ}
        stick = AbsInfo(0, -32768, 32767, 16, 128, 0)
        trig = AbsInfo(0, 0, 255, 0, 0, 0)
        hat = AbsInfo(0, -1, 1, 0, 0, 0)
        caps = {e.EV_KEY: list(self.keys.values()),
                e.EV_ABS: [(e.ABS_X, stick), (e.ABS_Y, stick), (e.ABS_RX, stick), (e.ABS_RY, stick),
                           (e.ABS_Z, trig), (e.ABS_RZ, trig),
                           (e.ABS_HAT0X, hat), (e.ABS_HAT0Y, hat)]}
        n = plat.instance()
        self.ui = UInput(caps,
                         name=f'ps2kit pad {n}' if n else 'Microsoft X-Box 360 pad',
                         vendor=0x1209 if n else 0x045E,
                         product=(0x5000 + int(n)) if n else 0x028E,
                         version=0x110, bustype=e.BUS_USB)
        return self.ui.device.path

    def button(self, name: str, down: bool) -> None:
        if name in self.keys:
            self.ui.write(self.e.EV_KEY, self.keys[name], int(down))
        elif name in self.hats:
            axis, v = self.hats[name]
            self.ui.write(self.e.EV_ABS, axis, v if down else 0)
        elif name in self.trigs:
            self.ui.write(self.e.EV_ABS, self.trigs[name], 255 if down else 0)
        else:
            return
        self.ui.syn()

    def axis(self, side: str, x: float, y: float) -> None:
        e = self.e
        ax, ay = (e.ABS_X, e.ABS_Y) if side == 'l' else (e.ABS_RX, e.ABS_RY)
        self.ui.write(e.EV_ABS, ax, int(max(-1.0, min(1.0, x)) * 32767))
        self.ui.write(e.EV_ABS, ay, int(max(-1.0, min(1.0, y)) * 32767))
        self.ui.syn()

    def close(self) -> None:
        self.ui.close()


# PS2 button name -> the XUSB_BUTTON member vgamepad names it by. A plain string table rather than the
# enum itself, so the mapping can be read (and tested) on a machine with no vgamepad and no ViGEmBus.
XUSB = {'cross': 'A', 'circle': 'B', 'square': 'X', 'triangle': 'Y',
        'start': 'START', 'l1': 'LEFT_SHOULDER', 'r1': 'RIGHT_SHOULDER',
        'l3': 'LEFT_THUMB', 'r3': 'RIGHT_THUMB', 'guide': 'GUIDE',
        'up': 'DPAD_UP', 'down': 'DPAD_DOWN', 'left': 'DPAD_LEFT', 'right': 'DPAD_RIGHT'}


class VigemPad(PadBackend):  # pragma: no cover - needs ViGEmBus, so only a Windows box exercises it
    """Windows: an Xbox 360 pad through ViGEmBus (`pip install vgamepad`).

    It presents as a Microsoft Xbox 360 pad, which is what PCSX2's SDL input source maps with no per-device
    configuration — the same reason `UinputPad` claims 0x045E/0x028E on Linux.

    Two things it cannot do that the Linux backend can, both of them ViGEm's doing rather than ours:

    * **No identity.** Every ViGEm 360 pad reports Microsoft's own VID/PID, so the
      `SDL_GAMECONTROLLER_IGNORE_DEVICES_EXCEPT=0x1209/0x500N` trick that pins copy N to pad N on Linux has
      nothing to match. Copy N is pinned by device *index* instead (`platform_.pad_ini_rows`), which holds
      as long as the copies' pads are created in order and no real controller is plugged in between.
    * **Four at most.** ViGEm pads are XInput devices, and XInput has four user slots. `[emu] instances`
      above four leaves the extra copies without a pad — and therefore without the pad-bound pause and
      screenshot hotkeys, so they fall back to a window grab.

    `l2`/`r2` are analogue triggers on a 360 pad, so a digital press sends the full 255 the way the PS2's
    own L2/R2 read when pressed flat. Y is negated: XInput's stick Y is positive *up*, while every other
    kit tool (and the PS2 pad, and evdev) counts +1 as down.
    """

    name = 'vigem'

    def open(self) -> str:
        ok, why = plat.pad_available()
        if not ok:
            raise SystemExit(f'no virtual pad: {why}')
        import vgamepad as vg
        self.vg = vg
        try:
            self.gp = vg.VX360Gamepad()
        except Exception as e:
            raise SystemExit(f'ViGEmBus would not create a pad ({e}).\n'
                             'Install the driver (https://github.com/nefarius/ViGEmBus/releases), reboot, '
                             'and note that XInput has only four slots — a fifth pad always fails.')
        self.keys = {k: getattr(vg.XUSB_BUTTON, f'XUSB_GAMEPAD_{v}') for k, v in XUSB.items()}
        self.gp.reset()
        self.gp.update()
        n = plat.instance()
        return f'ViGEm X360 pad for copy {n}' if n else 'ViGEm X360 pad'

    def button(self, name: str, down: bool) -> None:
        if name in TRIGGERS:
            (self.gp.left_trigger if name == 'l2' else self.gp.right_trigger)(value=255 if down else 0)
        elif name in self.keys:
            (self.gp.press_button if down else self.gp.release_button)(button=self.keys[name])
        else:
            return
        self.gp.update()

    def axis(self, side: str, x: float, y: float) -> None:
        v = (int(max(-1.0, min(1.0, x)) * 32767), int(-max(-1.0, min(1.0, y)) * 32767))
        (self.gp.left_joystick if side == 'l' else self.gp.right_joystick)(x_value=v[0], y_value=v[1])
        self.gp.update()

    def close(self) -> None:
        self.gp.reset()
        self.gp.update()


BACKENDS = {'uinput': UinputPad, 'vigem': VigemPad}


def backend() -> PadBackend:
    b = plat.pad_backend()
    if b not in BACKENDS:
        ok, why = plat.pad_available()
        raise SystemExit(f'no virtual pad on this system: {why or b}')
    return BACKENDS[b]()


# --- the script language -----------------------------------------------------

class Script:
    """Runs pad command lines against a backend. Separate from `serve` so a test can drive it headless."""

    def __init__(self, dev: PadBackend):
        self.dev = dev

    def _ms(self, text: str) -> float:
        """A duration in milliseconds. `120` = 120 ms, `8f` = 8 frames, `80ms` = 80 ms."""
        t = str(text).strip().lower()
        if t.endswith('ms'):
            return float(t[:-2])
        if t.endswith('f'):
            return float(t[:-1]) * (1000.0 / _fps())
        v = float(t)
        if 0 < v < 1000.0 / _fps():
            print(f'pad: {text} ms is shorter than one frame at {_fps():g} fps, so the game may never '
                  f'see it — durations here are milliseconds; write {t}f to mean frames', flush=True)
        return v

    def line(self, text: str) -> None:
        text = text.split('#', 1)[0].strip()
        if not text:
            return
        if ';' in text:                     # "dpad right 60; press cross 100" as one line
            for part in text.split(';'):
                self.line(part)
            return
        p = text.split()
        c = p[0].lower()
        try:
            if c in ('press', 'dpad'):
                # `dpad right 60` is just a press; it reads better in a menu-walking script.
                # The duration is parsed *before* the button goes down: `press cross soon` used to press
                # it and then raise, leaving the pad holding Cross for the rest of the session.
                ms = self._ms(p[2]) if len(p) > 2 else 100.0
                self._key(p[1], True)
                time.sleep(ms / 1000)
                self._key(p[1], False)
            elif c in ('down', 'up'):
                self._key(p[1], c == 'down')
            elif c == 'stick':
                self.dev.axis('l' if p[1].lower().startswith('l') else 'r', float(p[2]), float(p[3]))
            elif c in ('sleep', 'wait'):
                time.sleep(self._ms(p[1]) / 1000)
            elif c == 'release':
                for b in BUTTONS:
                    self._key(b, False)
                self.dev.axis('l', 0, 0)
                self.dev.axis('r', 0, 0)
            elif c == 'select':
                self._key(c, True)      # say *why* it is refused, not "unknown command"
            elif c in BUTTONS:
                print(f'pad: {c!r} is a button, not a command — did you mean `press {c} 100`?', flush=True)
            else:
                print(f'pad: unknown command {text!r} '
                      '(press/down/up/dpad/stick/sleep/release)', flush=True)
        except (IndexError, ValueError):
            print(f'pad: bad arguments: {text!r}', flush=True)

    def _key(self, name: str, down: bool) -> None:
        name = name.lower()
        if name == 'select':
            # Not an oversight: PCSX2 binds save-state/load-state/turbo to Select + a shoulder button.
            if down:   # once per press, not again on the release
                print('pad: refusing `select` — PCSX2 hotkeys are Select + shoulder combinations, so '
                      'sending it can save or load a state behind your back', flush=True)
            return
        if name not in BUTTONS:
            print(f'pad: unknown button {name!r} (have: {" ".join(BUTTONS)})', flush=True)
            return
        self.dev.button(name, down)


# --- serve / send ------------------------------------------------------------

def serve() -> int | None:
    """Create the pad and run commands from the FIFO until killed. One server per PCSX2 copy."""
    lock = plat.Lock('pad')
    lock.path = Path(str(fifo()) + '.lock')      # the lock sits beside the fifo it guards
    if not lock.take(block=False):
        print(f'pad: pad {plat.instance() or 0} is already being served', file=sys.stderr)
        return 0
    dev = backend()
    path = dev.open()
    f = fifo()
    if not plat.WINDOWS and not f.exists():
        # On Linux a FIFO is the whole transport: `send` is then a plain file write, so any language or
        # shell script can drive the pad.
        os.mkfifo(f, 0o600)
    print(f'pad: {dev.name} device {path} ready; commands on {transport()}', flush=True)
    script = Script(dev)
    try:
        if plat.WINDOWS:
            _pipe_serve(script)
        else:
            while True:
                with open(f) as fh:      # blocks until a writer opens it; one batch per open
                    for line in fh:
                        script.line(line.rstrip('\n'))
    except KeyboardInterrupt:
        return 0
    finally:
        dev.close()
        lock.release()


def split_lines(buf: bytes) -> tuple[list[str], bytes]:
    """Complete command lines out of whatever has arrived, plus the unfinished tail. Pure.

    A pipe read returns bytes, not lines, and a client is free to write half a command before the rest:
    `press cro` / `ss 120`. The server therefore keeps the tail rather than running it, which is also what
    the Linux edition gets for free from iterating the FIFO's file object.
    """
    text = buf.decode('utf-8', 'replace')
    parts = text.split('\n')
    return [l for l in parts[:-1]], parts[-1].encode('utf-8')


def _pipe_serve(script: 'Script') -> None:
    """The named-pipe server: accept one client at a time, run its lines as they arrive, repeat.

    A blocking pipe, deliberately: the pad is a single device and commands are timed with `sleep`, so there
    is nothing to gain from overlapping two clients, and plenty to lose (two scripts interleaving presses).
    `PIPE_UNLIMITED_INSTANCES` is still asked for so a second `send` waits its turn instead of failing,
    which is the FIFO's behaviour too.
    """
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.CreateNamedPipeW.restype = wintypes.HANDLE
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    name = ctypes.c_wchar_p(transport())
    INVALID = wintypes.HANDLE(-1).value
    buf = ctypes.create_string_buffer(4096)
    got = wintypes.DWORD()
    while True:
        h = k.CreateNamedPipeW(name, 0x00000001,                 # PIPE_ACCESS_INBOUND
                               0,                                # PIPE_TYPE_BYTE | PIPE_WAIT, both 0
                               255,                              # PIPE_UNLIMITED_INSTANCES
                               0, 4096, 0, None)
        if h == INVALID or not h:
            raise SystemExit(f'pad: cannot create {transport()} (error {ctypes.get_last_error()}) — '
                             'is another pad server already running for this copy?')
        try:
            # ERROR_PIPE_CONNECTED: the client got in between CreateNamedPipeW and here. Still connected.
            if not k.ConnectNamedPipe(h, None) and ctypes.get_last_error() != 535:
                continue
            tail = b''
            while k.ReadFile(h, buf, 4096, ctypes.byref(got), None) and got.value:
                lines, tail = split_lines(tail + buf.raw[:got.value])
                for line in lines:
                    script.line(line)
            for line in split_lines(tail + b'\n')[0]:
                script.line(line)
            k.DisconnectNamedPipe(h)
        finally:
            k.CloseHandle(h)


def send(cmds: list[str], quiet: bool = False) -> bool:
    """Write command lines to the running server. False (or exit) when nothing is serving."""
    def no_server(msg: str) -> bool:
        if quiet:
            print(msg, file=sys.stderr)
            return False
        raise SystemExit(msg)

    where = transport()
    hint = ('  (KIT_PCSX2=%s for this copy)' % plat.instance() if plat.instance() else '')
    data = ('\n'.join(cmds) + '\n').encode('utf-8')
    if plat.WINDOWS:
        if not serving():
            return no_server(f'pad: no server on {where} — start one:  kit pad serve{hint}')
        return _pipe_send(data, where, no_server)
    f = fifo()
    if not f.exists():
        return no_server(f'pad: no server on {f} — start one:  kit pad serve &{hint}')
    try:
        # O_NONBLOCK first: opening a FIFO for writing blocks forever if the server died without removing it.
        fd = os.open(f, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return no_server(f'pad: {f} exists but nobody is reading it (a dead server?) — '
                         'restart:  kit pad serve &')
    with os.fdopen(fd, 'wb') as fh:
        fh.write(data)
    return True


def _pipe_send(data: bytes, where: str, no_server) -> bool:
    """One batch down the named pipe. ERROR_PIPE_BUSY means another `send` has it: wait, then retry once."""
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.CreateFileW.restype = wintypes.HANDLE
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    name = ctypes.c_wchar_p(where)
    INVALID = wintypes.HANDLE(-1).value
    for attempt in (0, 1):
        h = k.CreateFileW(name, 0x40000000, 0, None, 3, 0, None)   # GENERIC_WRITE, OPEN_EXISTING
        if h != INVALID and h:
            try:
                wrote = wintypes.DWORD()
                if not k.WriteFile(h, data, len(data), ctypes.byref(wrote), None):
                    return no_server(f'pad: {where} accepted the connection but refused the write '
                                     f'(error {ctypes.get_last_error()}) — restart:  kit pad serve')
                k.FlushFileBuffers(h)
                return True
            finally:
                k.CloseHandle(h)
        err = ctypes.get_last_error()
        if err != 231 or attempt:                                  # ERROR_PIPE_BUSY
            break
        k.WaitNamedPipeW(name, 10000)
    return no_server(f'pad: {where} exists but nobody is reading it (a dead server?) — '
                     'restart:  kit pad serve')


# --- CLI ---------------------------------------------------------------------

DOC = """virtual controller: serve one, then send button and stick commands to it.

  kit pad serve                        create the device (keep it running, in its own
                                         terminal; KIT_PCSX2=N for copy N)
  kit pad send "press start 100"
  kit pad send "stick l 0 -1" "sleep 400" "release"
  kit pad send < moves.txt             or:  kit pad script moves.txt
  kit pad check                        can this machine make a pad, and is one serving?

Commands: press <btn> <ms> | down <btn> | up <btn> | dpad <dir> <ms> | stick <l|r> <x> <y> |
          sleep <ms> | release | # comment        (`8f` = 8 frames instead of 8 ms)
Buttons:  cross circle square triangle start l1 r1 l2 r2 l3 r3 up down left right guide  (select is refused)
Timing is wall-clock, not frame-exact."""


def cmd_pad(argv: list[str]) -> int | None:
    from ps2kit.cli import parser
    ap = parser('pad', DOC)
    ap.add_argument('cmd', nargs='?', default='check', choices=['serve', 'send', 'script', 'check'])
    ap.add_argument('args', nargs='*', help='commands for `send`, a file for `script`')
    a = ap.parse_args(argv)

    if a.cmd == 'serve':
        return serve()
    if a.cmd == 'check':
        ok, why = plat.pad_available()
        print(f'backend  {plat.pad_backend()}  {"ok" if ok else "UNUSABLE: " + why}')
        print(f'server   {transport()}  {"serving" if serving() else "not running (kit pad serve)"}')
        return 0 if ok else 1
    if a.cmd == 'script':
        if not a.args:
            raise SystemExit('kit pad script FILE  (one command per line)')
        cmds = Path(a.args[0]).read_text().splitlines()
    else:
        cmds = a.args or [l for l in sys.stdin.read().splitlines()]
    cmds = [c for c in cmds if c.strip()]
    if not cmds:
        raise SystemExit('kit pad send "press cross 100" ...   (or feed commands on stdin)')
    send(cmds)
    return None
