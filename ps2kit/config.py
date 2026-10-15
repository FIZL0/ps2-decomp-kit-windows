"""Load `kit.toml` — the only place a game's specifics live.

Everything else in the kit is game-agnostic and asks this module. Import it as

    from ps2kit.config import cfg, ROOT
    c = cfg()                     # cached, re-read when the file changes
    c.serial, c.iso, c.vsync      # resolved values
    c.ctx('decomp/game.c')        # a path inside the private context/ dir (created on demand)
    c.overlay('game').load        # one overlay

Anything the kit discovers for itself (the vsync counter address, the boot ELF name) is written back
to `kit.toml` by `kit setup` / `kit probe --save`, so it is discovered once per game, not per session.
"""
from __future__ import annotations

import os
import re
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _config_path() -> Path:
    """Where `kit.toml` is. `$KIT_CONFIG` (a file or a directory holding one) wins, then `$KIT_ROOT`
    (which `crates/kit-oracle` already honours), then the repository root.

    This is only about *locating* the config. ROOT stays the repository, because it also anchors
    `crates/`, `plan/` and the `kit` script. Two users of the override: `kit demo`, which writes a
    synthetic game's config into `context/demo/` instead of touching yours, and tests.
    """
    if env := os.environ.get('KIT_CONFIG'):
        p = Path(env).expanduser()
        return p / 'kit.toml' if p.is_dir() else p
    if env := os.environ.get('KIT_ROOT'):
        return Path(env).expanduser() / 'kit.toml'
    return ROOT / 'kit.toml'


CONFIG = _config_path()
EXAMPLE = ROOT / 'kit.toml.example'


def write_atomic(path, text: str) -> None:
    """Replace a text file in one step: a temp file beside it, then `os.replace`.

    Every writer of `kit.toml` goes through this, because a run is several processes deep in the same
    repository and a plain truncate-then-write has a window in which the file is empty or half a file.
    A `./kit check` that reads it in that window does not fail with "somebody is editing kit.toml" — it
    fails with `[game] iso is empty`, which points at the disc, and costs an hour.

    `os.replace` is atomic within a filesystem, so a reader sees either the old file or the new one. The
    temp name carries the pid so two writers cannot land on the same one, and the mode of the file being
    replaced is kept (a fresh temp file is 0600, and `kit.toml` is not a secret).

    Where this edition differs from the Linux one: on Windows a file another process has open cannot be
    replaced, so the race does not go away, it changes ends — the reader sees a whole file and the *writer*
    gets a sharing violation. A reader of `kit.toml` holds it open for microseconds, so a short bounded
    retry covers it; failing after that is correct, because something is holding the file for a living and
    silently not writing would be worse than saying so.
    """
    path = Path(path)
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    try:
        tmp.write_text(text, encoding='utf-8')
        try:
            os.chmod(tmp, path.stat().st_mode & 0o7777)
        except OSError:
            pass  # no file there yet: the umask's default is right
        for attempt in range(1, 11):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:      # Windows: somebody has it open; they are about to close it
                if attempt == 10:
                    raise
                time.sleep(0.02)
    finally:
        tmp.unlink(missing_ok=True)


def num(v, what='value'):
    """0x… strings, decimal strings and ints all mean a number; '' and None mean 'not set'."""
    if v is None or v == '':
        return None
    if isinstance(v, int):
        return v
    s = str(v).strip().replace('_', '')
    try:
        return int(s, 16) if s.lower().startswith('0x') else int(s, 0)
    except ValueError:
        raise SystemExit(f'kit.toml: {what} is not a number: {v!r}')


@dataclass
class Overlay:
    name: str
    file: str
    load: int
    off: int = 0


@dataclass
class Config:
    raw: dict = field(default_factory=dict)
    path: Path = CONFIG

    # --- [game] ---------------------------------------------------------------
    @property
    def name(self) -> str:
        return self.raw.get('game', {}).get('name') or 'the game'

    @property
    def serial(self) -> str:
        s = self.raw.get('game', {}).get('serial', '')
        return s.strip().upper()

    @property
    def region(self) -> str:
        return self.raw.get('game', {}).get('region', '')

    @property
    def version(self) -> str:
        return self.raw.get('game', {}).get('version', '')

    @property
    def iso(self) -> Path:
        p = self.raw.get('game', {}).get('iso', '')
        if not p:
            raise SystemExit('kit.toml: [game] iso is empty — point it at your own disc image')
        return (ROOT / p).resolve() if not os.path.isabs(p) else Path(p)

    def need_iso(self) -> Path:
        p = self.iso
        if not p.exists():
            raise SystemExit(f'disc image not found: {p}\n'
                             'Dump your own copy of the disc and set [game] iso in kit.toml.')
        return p

    # --- [emu] ----------------------------------------------------------------
    @property
    def pcsx2(self) -> str:
        return self.raw.get('emu', {}).get('pcsx2') or 'pcsx2-qt'

    @property
    def pine_slot(self) -> int:
        return num(self.raw.get('emu', {}).get('pine_slot'), '[emu] pine_slot') or 28011

    @property
    def vsync(self) -> int | None:
        """The game's per-frame counter, or None when it still has to be probed."""
        v = self.raw.get('emu', {}).get('vsync_addr')
        if v in (None, '', 'auto'):
            return None
        return num(v, '[emu] vsync_addr')

    def need_vsync(self) -> int:
        v = self.vsync
        if v is None:
            raise SystemExit('No frame counter known yet. With the game running, find it once:\n'
                             '    kit probe vsync --save\n'
                             'It writes [emu] vsync_addr into kit.toml.')
        return v

    @property
    def speed(self) -> float:
        return float(self.raw.get('emu', {}).get('speed', 1.0))

    @property
    def instances(self) -> int:
        # 4 by default here, not 8: a copy's pad is a ViGEm XInput device and XInput has four user
        # slots (WINDOWS.md). More copies than that is fine as long as they need no input.
        return int(self.raw.get('emu', {}).get('instances', 4))

    @property
    def ram(self) -> int:
        return int(self.raw.get('emu', {}).get('ram_mb', 32)) << 20

    # --- [boot] / [[overlay]] -------------------------------------------------
    @property
    def boot_elf(self) -> str:
        return self.raw.get('boot', {}).get('elf', '') or ''

    @property
    def boot_load(self) -> int | None:
        return num(self.raw.get('boot', {}).get('load'), '[boot] load')

    @property
    def overlays(self) -> list[Overlay]:
        out = []
        for o in self.raw.get('overlay', []) or []:
            try:
                out.append(Overlay(o['name'], o['file'], num(o['load'], f'overlay {o.get("name")} load'),
                                   num(o.get('off', 0)) or 0))
            except KeyError as e:
                raise SystemExit(f'kit.toml: an [[overlay]] block is missing {e}')
        return out

    def overlay(self, name: str) -> Overlay:
        for o in self.overlays:
            if o.name == name:
                return o
        known = ', '.join(o.name for o in self.overlays) or 'none configured'
        raise SystemExit(f'no overlay named {name!r} in kit.toml (have: {known})')

    @property
    def programs(self) -> list[str]:
        """Every program the kit decompiles: the boot ELF plus each overlay, by dump name."""
        return ['boot'] + [o.name for o in self.overlays]

    # --- [ghidra] -------------------------------------------------------------
    @property
    def ghidra(self) -> Path | None:
        h = self.raw.get('ghidra', {}).get('home', '')
        return Path(h).expanduser() if h else None

    def need_ghidra(self) -> Path:
        h = self.ghidra
        script = h and (h / 'support' / 'analyzeHeadless')
        if not script or not script.exists():
            raise SystemExit('Ghidra not found. Install it, add the EmotionEngine processor extension, and set\n'
                             '[ghidra] home in kit.toml.  See docs/GHIDRA.md.')
        return h

    @property
    def ghidra_project(self) -> Path:
        return ROOT / (self.raw.get('ghidra', {}).get('project') or 'context/ghidra')

    @property
    def ghidra_name(self) -> str:
        return self.raw.get('ghidra', {}).get('name') or 'ps2'

    @property
    def ghidra_timeout(self) -> int:
        return int(self.raw.get('ghidra', {}).get('timeout', 7200))

    # --- [build] --------------------------------------------------------------
    @property
    def test_cmd(self) -> str:
        return self.raw.get('build', {}).get('test', '') or ''

    @property
    def lint_cmd(self) -> str:
        return self.raw.get('build', {}).get('lint', '') or ''

    @property
    def build_error_re(self) -> re.Pattern:
        return re.compile(self.raw.get('build', {}).get('build_error') or r'^error(\[E[0-9]+\])?:|: error:')

    # --- [git] ----------------------------------------------------------------
    @property
    def push_remote(self) -> str | None:
        """The remote finished work is pushed to, or None when `[git] push` is off.

        `kit done` pushes the branch it committed on, and a run pushes the branch of a task whose merge
        it left for a human, so a task's own work is never only on this machine. The shared branch is
        never pushed for you — see `checks.push`. `push = false` turns all of it off; a string names a
        remote other than origin. A repo with no such remote configured is the same as off, silently.
        """
        v = self.raw.get('git', {}).get('push', True)
        return None if v is False else (str(v) if isinstance(v, str) else 'origin')

    # --- [run] ----------------------------------------------------------------
    @property
    def fix_merges(self) -> bool:
        """Whether a conflict the runner cannot join goes to a Claude session instead of to a human.

        On by default: the alternative is eight finished tasks waiting for somebody to merge them by
        hand, which is what this replaced. `[run] fix_merges = false` gets that behaviour back.
        """
        return bool(self.raw.get('run', {}).get('fix_merges', True))

    @property
    def fix_timeout(self) -> int:
        """Seconds a merge fixer may take before the merge is left for a human instead."""
        return int(self.raw.get('run', {}).get('fix_timeout', 1200))

    @property
    def fix_model(self) -> str:
        """The model that fixes merges. The default is the one the run's agents use."""
        return str(self.raw.get('run', {}).get('fix_model')
                   or os.environ.get('KIT_MODEL') or 'opus')

    @property
    def push_merge(self) -> bool:
        """Whether a `./kit run` pushes the merge target after a merge that passed the test gate.

        On by default, and the test gate is why that is defensible: what goes out has been merged,
        archived and tested. The other policy — publishing the branch everyone reads is a decision a
        person makes after reading the merge, not a side effect of a run finishing a task at 4am — is
        `push_merge = false`, which logs the `git push` to run instead. `[git] push` still gates all of
        it, and a task branch is pushed either way: that one is the only copy of an agent's work.
        """
        return bool(self.raw.get('git', {}).get('push_merge', True)) and bool(self.push_remote)

    # --- [paths] --------------------------------------------------------------
    def ctx(self, *parts: str) -> Path:
        """A path under the private, git-ignored working dir; its parent is created."""
        base = ROOT / (self.raw.get('paths', {}).get('context') or 'context')
        p = base.joinpath(*parts)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def journal(self) -> Path:
        return ROOT / (self.raw.get('paths', {}).get('journal') or 'research/journal')

    @property
    def plan(self) -> Path:
        return ROOT / (self.raw.get('paths', {}).get('plan') or 'plan')

    # --- [oracle] / [symbols] / [states] -------------------------------------
    @property
    def oracle_file(self) -> Path:
        return ROOT / (self.raw.get('oracle', {}).get('addresses') or 'research/oracle.txt')

    @property
    def symbols(self) -> dict[str, int]:
        return {k: num(v, f'[symbols] {k}') for k, v in (self.raw.get('symbols') or {}).items()}

    def sym(self, s: str) -> int:
        """'@name' or '0x…' or '1004c0' → an address. Lets every tool take names."""
        s = str(s).strip()
        if s.startswith('@'):
            syms = self.symbols
            if s[1:] not in syms:
                raise SystemExit(f'no [symbols] entry named {s[1:]!r} in kit.toml')
            return syms[s[1:]]
        return int(s, 16) if not s.lower().startswith('0x') else num(s)

    @property
    def states(self) -> dict[str, str]:
        return {str(k): str(v) for k, v in (self.raw.get('states') or {}).items()}

    # --- writing back ---------------------------------------------------------
    def set(self, table: str, key: str, value) -> None:
        """Write one scalar back into kit.toml, keeping comments and layout.

        Only scalars in an existing or new top-level table; that covers everything the kit discovers
        (vsync_addr, boot elf, pcsx2 path, ghidra home).
        """
        text = self.path.read_text(encoding='utf-8') if self.path.exists() else ''
        lit = f'"{value}"' if isinstance(value, str) else ('0x%x' % value if isinstance(value, int) and
                                                           abs(value) > 0xffff else repr(value))
        if isinstance(value, int) and abs(value) > 0xffff:
            lit = f'"0x{value:x}"'
        lines = text.splitlines()
        start = next((i for i, l in enumerate(lines) if l.strip() == f'[{table}]'), None)
        if start is None:
            lines += ['', f'[{table}]', f'{key} = {lit}']
        else:
            end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith('[')), len(lines))
            hit = next((i for i in range(start + 1, end)
                        if re.match(rf'\s*{re.escape(key)}\s*=', lines[i])), None)
            if hit is None:
                lines.insert(end, f'{key} = {lit}')
            else:
                keep = lines[hit].split('#', 1)
                comment = f'  #{keep[1]}' if len(keep) > 1 else ''
                lines[hit] = f'{key} = {lit}{comment}'
        write_atomic(self.path, '\n'.join(lines).rstrip('\n') + '\n')
        _cache.clear()


_cache: dict[str, tuple[int, Config]] = {}


def cfg(required: bool = True) -> Config:
    """The parsed kit.toml, re-read whenever it changes."""
    if not CONFIG.exists():
        if not required:
            return Config(raw={})
        raise SystemExit(f'No kit.toml yet.\n'
                         f'    cp {EXAMPLE.name} kit.toml   # then fill in [game]\n'
                         f'    kit setup                  # or be walked through it\n')
    mtime = CONFIG.stat().st_mtime_ns
    hit = _cache.get('c')
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        raw = tomllib.loads(CONFIG.read_text(encoding='utf-8'))
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(f'kit.toml is not valid TOML: {e}')
    c = Config(raw=raw)
    _cache['c'] = (mtime, c)
    return c


def problems(c: Config) -> list[str]:
    """Everything wrong with the config as it stands, as one-line complaints (used by `kit doctor`)."""
    out = []
    if not c.serial or not re.fullmatch(r'[A-Z]{4}[-_]?\d{3}\.?\d{2}', c.serial.replace('-', '-')):
        out.append(f'[game] serial looks wrong: {c.serial!r} (want e.g. SLUS-20000)')
    if not c.raw.get('game', {}).get('iso'):
        out.append('[game] iso is empty')
    elif not c.iso.exists():
        out.append(f'[game] iso does not exist: {c.iso}')
    for o in c.overlays:
        if o.load < 0x80000 or o.load > 0x2000000:
            out.append(f'overlay {o.name}: load address {o.load:#x} is outside EE RAM')
    if c.vsync is None:
        out.append('[emu] vsync_addr not probed yet (kit probe vsync --save)')
    if not c.ghidra:
        out.append('[ghidra] home is empty (needed for decompiling, not for live captures)')
    return out


if __name__ == '__main__':
    c = cfg()
    print(f'{c.name}  {c.serial} {c.region} {c.version}')
    print(f'iso        {c.iso}  {"ok" if c.iso.exists() else "MISSING"}')
    print(f'pcsx2      {c.pcsx2}  PINE slot {c.pine_slot}')
    print(f'vsync      {c.vsync:#x}' if c.vsync else 'vsync      not probed')
    print(f'programs   {", ".join(c.programs)}')
    for o in c.overlays:
        print(f'  overlay {o.name:10} {o.file:24} load {o.load:#08x}')
    for p in problems(c):
        print('!', p, file=sys.stderr)
