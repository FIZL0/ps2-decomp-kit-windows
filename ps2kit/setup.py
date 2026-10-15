"""First-time setup and the health check.

`kit setup` walks a new user from "I have a disc" to "I can read the game's live memory", asking only for
what it genuinely cannot work out, and writing the answers into `kit.toml` so nothing has to be worked out
twice. `kit doctor` answers the only question that matters when something breaks: *which* of the dozen
prerequisites is missing, and what exactly fixes it.

The design rule here: never report a problem without the command that solves it. Setting this kind of
project up involves an emulator, a BIOS, a disassembler, a processor extension, a kernel device and a disc
dump, and every one of them fails in its own way. A health check that says "PINE not available" has done a
tenth of its job.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from ps2kit import platform_ as plat
from ps2kit.cli import parser
from ps2kit.config import CONFIG, EXAMPLE, ROOT, cfg, problems

OK, WARN, BAD = '  ok  ', ' note ', ' FIX  '


def _say(state: str, what: str, detail: str = '') -> None:
    print(f'[{state}] {what}' + (f'\n        {detail}' if detail else ''))


def _ask(question: str, default: str = '') -> str:
    try:
        a = input(f'{question}' + (f' [{default}]' if default else '') + ': ').strip()
    except EOFError:
        print()
        return default
    return a or default


def _yes(question: str, default: bool = True) -> bool:
    a = _ask(question + (' (Y/n)' if default else ' (y/N)'))
    return default if not a else a.lower().startswith('y')


# --- finding the pieces -------------------------------------------------------

def find_pcsx2() -> str | None:
    """PCSX2 on PATH, or in one of the places an installer or an unzipped portable build leaves it."""
    for name in plat.pcsx2_guesses():
        if (p := shutil.which(name)):
            return p
    pats = ('*CSX2*.exe', '*CSX2*') if plat.WINDOWS else ('*CSX2*',)
    for guess in plat.pcsx2_search_dirs():
        if not guess.is_dir():
            continue
        for pat in pats:
            for f in sorted(guess.glob(pat)):
                # A directory match is a portable unzip: the exe is inside it.
                for cand in (sorted(f.glob('*CSX2*.exe')) if f.is_dir() else [f]):
                    if cand.is_file() and os.access(cand, os.X_OK):
                        return str(cand)
    return None


def find_ghidra() -> Path | None:
    env = os.environ.get('GHIDRA_INSTALL_DIR')
    cands = [Path(env)] if env else []
    for d in plat.ghidra_search_dirs():
        if d.is_dir():
            cands += [d / 'ghidra', *sorted(d.glob('ghidra*'))]
    for c in cands:
        if plat.ghidra_headless(c).exists():
            return c
    return None


def find_iso() -> Path | None:
    pats = ('*.iso', '*.ISO', '*.bin', '*.BIN', '*.img', '*.chd', '*.cso')
    for d in (ROOT, ROOT.parent, Path.home() / 'games', Path.home() / 'roms', Path.home() / 'Downloads',
              plat.pcsx2_config_dir() / 'games'):
        if not d.is_dir():
            continue
        for p in pats:
            for f in sorted(d.glob(p)):
                if f.stat().st_size > 50 << 20:  # a PS2 disc, not a stray file
                    return f
    return None


def ee_extension(home: Path) -> bool:
    """Is the EmotionEngine processor extension installed into this Ghidra?"""
    for base in (home / 'Ghidra' / 'Extensions', Path.home() / '.config'):
        if base.is_dir() and any('motion' in p.name.lower() or 'r5900' in p.name.lower()
                                 for p in base.rglob('*') if p.is_dir()):
            return True
    langs = list((home / 'Ghidra' / 'Processors').glob('*/data/languages/*5900*')) if home.is_dir() else []
    return bool(langs)


def bios_present() -> tuple[bool, Path | None]:
    d = plat.pcsx2_config_dir() / 'bios'
    if not d.is_dir():
        return False, d
    return any(f.suffix.lower() in ('.bin', '.rom0', '') and f.stat().st_size > 1 << 20
               for f in d.iterdir() if f.is_file()), d


# --- the wizard ---------------------------------------------------------------

def main(argv: list[str]) -> int:
    ap = parser('setup', __doc__.split('\n\n')[0])
    ap.add_argument('--force', action='store_true', help='start from scratch even if kit.toml exists')
    ap.add_argument('--no-input', action='store_true', help='fill in only what can be detected, ask nothing')
    ap.add_argument('--iso', metavar='FILE', help='use this disc image instead of searching for one')
    a = ap.parse_args(argv)

    print('ps2-decomp-kit setup\n' + '=' * 20)
    if CONFIG.exists() and not a.force:
        print(f'{CONFIG.name} already exists. `kit doctor` checks it; `kit setup --force` starts over.')
        return doctor([])

    lines = EXAMPLE.read_text(encoding='utf-8')
    CONFIG.write_text(lines, encoding='utf-8')
    c = cfg()
    print(f'Wrote {CONFIG.name} from the example. Filling in what I can find.\n')

    # 1. the disc
    iso = Path(a.iso).expanduser() if a.iso else find_iso()
    if a.iso and not iso.exists():
        print(f'no such image: {iso}', file=sys.stderr)
        return 2
    if a.no_input or a.iso:
        chosen = iso
    else:
        print('1. Your disc image. Dump your own copy of the game you own; the kit never ships game data.')
        chosen = Path(_ask('   path to the image', str(iso) if iso else '')).expanduser() if (iso or True) else None
    if chosen and chosen.exists():
        rel = os.path.relpath(chosen, ROOT)
        c.set('game', 'iso', rel if not rel.startswith('..' + os.sep * 2) else str(chosen))
        _say(OK, f'disc image: {chosen}')
        if chosen.suffix.lower() in ('.chd', '.cso', '.gz'):
            _say(WARN, 'that is a compressed image',
                 'Convert it first: `chdman extractcd -i game.chd -o game.cue -ob game.bin` (CHD) '
                 'or `maxcso --decompress game.cso` (CSO). The kit reads raw ISO/BIN.')
        else:
            # Imported here rather than inside the try: an ImportError is a broken checkout, not an
            # unreadable disc, and the handler below needs DiscError to be a name by then.
            from ps2kit.disc import Disc, DiscError, system_cnf
            try:
                with Disc(chosen) as d:
                    boot = system_cnf(d)
                # Only what the disc actually said: a homebrew image has no serial, and a disc with no
                # VER line should leave the example's placeholder alone rather than blanking it.
                if boot.serial:
                    c.set('game', 'serial', boot.serial)
                if boot.elf:
                    c.set('boot', 'elf', boot.elf)
                if boot.version:
                    c.set('game', 'version', boot.version)
                _say(OK, f'read the disc: {boot}')
            # Narrow on purpose: DiscError is "this image is not a readable PS2 disc" and OSError is the
            # read failing, which is what the advice below is for. A TypeError or AttributeError from this
            # block is a bug in the kit, and swallowing it into disc advice is what hid issue #1.
            except (DiscError, OSError) as e:
                _say(WARN, f'could not read SYSTEM.CNF off the image ({e})',
                     'Set [game] serial by hand — PCSX2 shows it in the title bar while the game runs.')
    else:
        _say(BAD, 'no disc image set', 'Put your own dump beside this repo and set [game] iso in kit.toml.')

    # 2. the emulator
    if (p := find_pcsx2()):
        cfg().set('emu', 'pcsx2', p)
        _say(OK, f'PCSX2: {p}')
    else:
        _say(BAD, 'PCSX2 not found', 'Install PCSX2 2.x (https://pcsx2.net) and set [emu] pcsx2 in kit.toml.')
    have_bios, bdir = bios_present()
    _say(OK if have_bios else BAD, f'PS2 BIOS in {bdir}' if have_bios else 'no PS2 BIOS found',
         '' if have_bios else f'Dump the BIOS from your own PS2 and put it in {bdir}. PCSX2 will not boot without it.')

    # 3. Ghidra
    if (g := find_ghidra()):
        cfg().set('ghidra', 'home', str(g))
        _say(OK, f'Ghidra: {g}')
        if not ee_extension(g):
            _say(BAD, 'the EmotionEngine processor extension is missing',
                 'The PS2 CPU is not in stock Ghidra. Get ghidra-emotionengine-reloaded (match your Ghidra\n'
                 f'        version), unzip it into {g}/Ghidra/Extensions/, then start Ghidra once to register it.')
    else:
        _say(WARN, 'Ghidra not found',
             'Only needed to decompile (live captures work without it). Install it, add the EmotionEngine\n'
             '        extension, and set [ghidra] home in kit.toml. See docs/GHIDRA.md.')

    # 4. the virtual pad
    ok, why = plat.pad_available()
    _say(OK if ok else WARN, 'virtual controller' if ok else 'virtual controller not usable yet', '' if ok else why)

    print('\nNext:')
    print('  kit doctor                        # re-check everything, with fixes')
    print('  kit extract                       # unpack the disc into context/iso')
    print('  kit elf --scan                    # find the program overlays, if any')
    print('  kit ghidra analyse && kit ghidra dump')
    print('  kit pcsx2 start                   # then, with the game running:')
    print('  kit probe vsync --save            # find the frame counter')
    print('\nSETUP.md has the same steps with the reasoning, and docs/WORKFLOW.md has the loop to work in.')
    return 0


# --- the health check ---------------------------------------------------------

def doctor(argv: list[str]) -> int:
    ap = parser('doctor', 'Check every prerequisite and print exactly what fixes each problem.')
    ap.add_argument('--live', action='store_true', help='also try talking to a running emulator')
    a = ap.parse_args(argv)

    bad = 0
    print(f'ps2-decomp-kit doctor   ({sys.platform}, python {sys.version.split()[0]})\n')

    if sys.version_info < (3, 11):
        _say(BAD, f'python {sys.version.split()[0]} is too old', 'The kit needs 3.11+ (it uses tomllib).')
        bad += 1
    else:
        _say(OK, f'python {sys.version.split()[0]}')

    if not CONFIG.exists():
        _say(BAD, 'no kit.toml', 'Run `kit setup`.')
        return 1
    c = cfg()
    _say(OK, f'kit.toml: {c.name} — {c.serial} {c.region} {c.version}'.rstrip())

    # the disc
    try:
        if c.iso.exists():
            size = c.iso.stat().st_size
            _say(OK, f'disc image: {c.iso.name} ({size / (1 << 30):.2f} GB)')
            extracted = c.ctx('iso')
            _say(OK if extracted.is_dir() and any(extracted.iterdir()) else WARN,
                 f'extracted to {extracted}' if extracted.is_dir() and any(extracted.iterdir())
                 else 'disc not extracted yet', '' if extracted.is_dir() else 'Run `kit extract`.')
        else:
            _say(BAD, f'disc image missing: {c.iso}', 'Set [game] iso to your own dump.')
            bad += 1
    except SystemExit as e:
        _say(BAD, str(e))
        bad += 1

    # the emulator
    try:
        cmdv = plat.pcsx2_cmd(c.pcsx2)
        _say(OK, f'PCSX2: {cmdv[0]}')
    except SystemExit as e:
        _say(BAD, str(e).splitlines()[0], 'Install PCSX2 2.x and set [emu] pcsx2.')
        bad += 1
    have_bios, bdir = bios_present()
    if not have_bios:
        _say(BAD, 'no PS2 BIOS', f'Dump your own console\'s BIOS into {bdir}.')
        bad += 1
    else:
        _say(OK, f'BIOS present ({bdir})')
    running = next((pids for n in plat.PCSX2_IMAGES if (pids := plat.pids_named(n))), [])
    _say(OK if running else WARN, f'PCSX2 running (pid {running[0]})' if running else 'PCSX2 not running',
         '' if running else 'Start it with `kit pcsx2 start` when you want live captures.')

    # frame counter
    if c.vsync is None:
        _say(WARN, 'frame counter not found yet',
             'With the game running: `kit probe vsync --save`. Recorders need it as their clock.')
    else:
        _say(OK, f'frame counter at {c.vsync:#x}')

    # Ghidra
    if c.ghidra and plat.ghidra_headless(c.ghidra).exists():
        _say(OK, f'Ghidra: {c.ghidra}')
        if not ee_extension(c.ghidra):
            _say(BAD, 'EmotionEngine processor extension missing',
                 f'Unzip ghidra-emotionengine-reloaded into {c.ghidra}/Ghidra/Extensions/ and start Ghidra once.')
            bad += 1
        dumps = sorted(c.ctx('decomp').glob('*.funcs.tsv')) if c.ctx('decomp').is_dir() else []
        _say(OK if dumps else WARN,
             f'decompilation dumps: {", ".join(d.name.split(".")[0] for d in dumps)}' if dumps
             else 'no decompilation dumps yet',
             '' if dumps else 'Run `kit ghidra analyse` once (slow), then `kit ghidra dump`.')
    else:
        _say(WARN, 'Ghidra not configured', 'Needed only for decompiling. See docs/GHIDRA.md.')

    # pad
    ok, why = plat.pad_available()
    _say(OK if ok else WARN, 'virtual controller' if ok else 'virtual controller unusable', '' if ok else why)

    # screenshots
    tools = plat.screenshot_tools()
    _say(OK if tools else WARN, f'screen capture via {", ".join(tools)}' if tools else 'no screen-capture tool',
         '' if tools else 'The emulator\'s own screenshot hotkey is used as a fallback, so this is not fatal.')

    # the port's build
    if c.test_cmd:
        tool = c.test_cmd.split()[0]
        _say(OK if shutil.which(tool) else BAD, f'test command: {c.test_cmd}',
             '' if shutil.which(tool) else f'{tool} is not on PATH — install it or change [build] test.')
        bad += 0 if shutil.which(tool) else 1
    else:
        _say(WARN, 'no [build] test command', '`kit check` and `kit done` will skip the test gate.')

    # git
    inside = subprocess.run(['git', 'rev-parse', '--is-inside-work-tree'], cwd=ROOT,
                            capture_output=True, text=True).stdout.strip() == 'true'
    _say(OK if inside else WARN, 'git repository' if inside else 'not a git repository',
         '' if inside else '`kit done` commits your work; run `git init` to get that.')
    if inside:
        ignored = subprocess.run(['git', 'check-ignore', '-q', str(c.ctx('.'))], cwd=ROOT).returncode == 0
        _say(OK if ignored else BAD, f'{c.ctx(".").name}/ is git-ignored' if ignored
             else f'{c.ctx(".").name}/ is NOT git-ignored',
             '' if ignored else 'Game data must never be committed. Add it to .gitignore before you commit anything.')
        bad += 0 if ignored else 1

    for p in problems(c):
        if 'vsync' not in p and 'ghidra' not in p.lower():
            _say(WARN, p)

    if a.live:
        try:
            from ps2kit.pine import Pine
            p = Pine()
            _say(OK, f'PINE: {p.version()} — {p.game_id()} {p.title()} [{p.status()}]')
            if c.vsync:
                _say(OK, f'frame counter reads {p.read32(c.vsync)}')
        except SystemExit as e:
            _say(BAD, 'PINE not reachable', str(e).splitlines()[0])
            bad += 1
        except Exception as e:
            _say(BAD, f'PINE not reachable: {e}',
                 'Is the game running under `kit pcsx2 start`? PINE must be enabled in PCSX2\'s settings\n'
                 '        (Advanced → Enable PINE); the kit sets it in the ini before each launch.')
            bad += 1

    print()
    if bad:
        print(f'{bad} thing(s) to fix above. Everything marked [{WARN.strip()}] is optional for now.')
    else:
        print('Ready. Next: docs/WORKFLOW.md, or `kit plan next` to pick up the first task.')
    return 1 if bad else 0


# --- symbols ------------------------------------------------------------------

def symbols(argv: list[str]) -> int:
    ap = parser('sym', 'Named RAM addresses, so tools and watch specs can say @player instead of 0x1f4a20.')
    ap.add_argument('name', nargs='?', help='name to add (with ADDR), or show just this one')
    ap.add_argument('addr', nargs='?', help='its address, e.g. 0x1f4a20')
    ap.add_argument('--delete', action='store_true', help='remove the named symbol')
    a = ap.parse_args(argv)
    c = cfg()
    syms = c.symbols
    if a.name and a.addr:
        c.set('symbols', a.name, int(str(a.addr), 16) if not str(a.addr).lower().startswith('0x')
              else int(str(a.addr), 0))
        print(f'{a.name} = {c.sym("@" + a.name) if True else "":#x}'.replace('= 0x', '= 0x'))
        return 0
    if a.name and a.delete:
        print('Remove the line from kit.toml by hand — the kit never silently drops knowledge you wrote down.')
        return 1
    if not syms:
        print('No [symbols] yet. Add one:  kit sym player_x 0x1f4a20')
        print('Find candidates with:       kit probe find --equals 100 --as f32')
        return 0
    w = max(len(k) for k in syms)
    for k, v in sorted(syms.items(), key=lambda kv: kv[1]):
        if not a.name or a.name == k:
            print(f'{k:<{w}}  {v:#010x}')
    return 0
