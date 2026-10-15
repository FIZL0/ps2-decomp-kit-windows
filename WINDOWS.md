# The Windows edition

This repository is the Windows edition of `ps2-decomp-kit`. Every command, subcommand and flag is the
same as the Linux edition's, deliberately: the documentation, the task list and any muscle memory carry
over, and a project can move between the two. What differs is the layer that touches the operating
system — `ps2kit/platform_.py`, the virtual pad in `ps2kit/pad.py`, and the `kit` launcher.

Read this page before you trust anything live. The last section is the honest part.

## What is different

| | Linux edition | here |
|---|---|---|
| launcher | `./kit <command>` | `kit <command>` (`kit.cmd`, or `kit.ps1` in PowerShell) |
| Python | `python3` | `python`, or `py -3` |
| PINE transport | a Unix socket in the runtime dir | TCP on `127.0.0.1:<slot>`, default 28011, copy *N* on 28011 + *N* |
| emulator copies | `XDG_CONFIG_HOME` | PCSX2's own `-cfgpath` |
| PCSX2's files | `~/.config/PCSX2` | `%USERPROFILE%\Documents\PCSX2` (`inis\`, `bios\`, `savestates\`, `snaps\`) |
| the kit's transient files | `$XDG_RUNTIME_DIR` | `%LOCALAPPDATA%\ps2kit` (pids, locks, pipe markers) |
| the lock | `flock` | `msvcrt.locking`, released when the process dies either way |
| virtual pad | `/dev/uinput` + `evdev` | the ViGEmBus driver + `vgamepad` |
| pad transport | a FIFO | a named pipe, `\\.\pipe\ps2kit-pad<N>` |
| screenshots | `grim` / `xdotool` / `spectacle` | `PrintWindow`, then a PowerShell screen copy, then PCSX2's own hotkey |
| stopping a process | `SIGTERM` | `WM_CLOSE` to its window, else Ctrl+Break, else `TerminateProcess` |
| finding a process | `/proc` | `CreateToolhelp32Snapshot`, with `tasklist` as a fallback |
| unattended runs | tmux panes | Windows Terminal (`wt.exe`) tabs, else one console window per agent |
| shell idioms | `head`, `tail`, `grep` | `Select-Object -First/-Last`, `Select-String` (`docs/CONTEXT.md`) |
| Ghidra's Java | usually the distribution's | a JDK 21+ you install first (`winget install Microsoft.OpenJDK.21`) |
| headless Ghidra | `support/analyzeHeadless` | `support\analyzeHeadless.bat` |

Two differences are limitations rather than translations, both of them ViGEm's doing:

- **Four pads at most.** ViGEm pads are XInput devices and XInput has four user slots, so `[emu]
  instances` above four leaves the extra copies without a pad.
- **Pads have no identity.** Every ViGEm pad reports Microsoft's own vendor and product id, so the
  VID/PID filter that pins copy *N* to pad *N* on Linux has nothing to match. Copy *N* is bound to SDL
  device index *N*−1 instead, which holds while the copies start in order and no real controller is
  plugged in meanwhile. Your own PCSX2 cannot be kept away from a kit pad either — bind your own
  controller explicitly, or stop the kit's copies before you play.

Everything else is the shared code, unchanged: the ISO reader, the ELF and overlay builder, Ghidra
driving, the decompilation readers, `kit probe`, `kit record`, the plan and journal tools, and the whole
proof side. The guard hook differs in one line — how it spells "run this script" — and nothing else about
the context measures changes. A capture made here and one made on Linux are the same bytes, which is the
point of keeping the two editions this close.

## What is known to work

The kit's own test suites all pass on this tree:

```
py -3 tests\test_plan.py           # -> all checks passed
py -3 tests\test_static.py         # -> 50 passed, 0 failed
py -3 tests\test_live.py           # -> 49 passed, 0 failed
py -3 tests\test_record.py         # -> 25 passed, 0 failed
py -3 tests\test_push.py           # -> 29 passed, 0 failed
py -3 tests\test_platform_win.py   # -> all checks passed
py -3 ps2kit\hooks\guard.py --self-test
cargo test --workspace
```

Most of that is the shared code — the command surface, the plan tools, the capture planner, the ISO and
ELF readers, the arithmetic and the oracle — and it passes on either OS. Two suites are about this
edition specifically. `test_platform_win.py` pins the OS layer's pure functions: path building,
`tasklist` parsing, the PINE endpoint, the `-cfgpath` instance arguments, the pad ini rows, the pipe
names and the argument quoting all take a `windows=` argument, so they can be asked for the Windows
answer from anywhere. And `test_live.py` builds its fake emulator on the transport this OS actually uses
— loopback TCP here — and asks `pad.transport()` rather than looking for a FIFO, so it proves the
Windows paths rather than failing on files that do not exist here.

What a test on a Linux machine cannot reach is the three places where real Windows APIs do the work:
`msvcrt` locking, `ctypes.WinDLL` for the named pipe and the screen grab, and the toolhelp snapshot.
Those are in the list below.

`kit doctor` is the other thing to run first. It names every missing piece with the command that fixes
it, and it prints the paths it used, which is how you find a redirected Documents folder or a portable
PCSX2 install before it confuses you.

## Not yet tested on real hardware

**The Windows-specific code in this edition was written on a Linux machine and has never been
executed on Windows.** It is reviewed, and its pure parts are under test, but nothing below has been
run against a real PCSX2 on a real Windows install. Treat each one as a claim, not a fact, and when you
are the first to run it, write what happened in a journal entry — including the `## Not verified / not
1:1` section, which is where this list came from in the first place.

- **PINE over TCP.** That PCSX2 2.x on Windows listens on `127.0.0.1:28011`, that the firewall prompt
  can be answered once and forgotten, and that `TCP_NODELAY` is enough to keep a 60 Hz capture at speed.
- **The ini keys the kit rewrites.** `EmuCore/EnablePINE` and `EmuCore/PINESlot` before every managed
  launch, plus a copy's `[Folders]`, `[InputSources]` and `[Hotkeys]` rows — written against PCSX2's
  Linux ini format and assumed identical here.
- **`-cfgpath` instances.** That PCSX2 accepts it, that it makes a copy wholly independent of your own
  install, and that `<repo>-slots\pcsx2\sN\PCSX2\` is laid out the way a copy expects.
- **The pad.** `vgamepad`, the ViGEmBus install, whether PCSX2's SDL source enumerates kit pads in
  creation order, and whether the pad-bound pause and screenshot hotkeys land. Also that turning XInput
  off is what stops PCSX2 seeing each pad twice.
- **The named-pipe transport.** `kit pad serve` as the pipe server and `kit pad send` as a client that
  connects, writes one batch and disconnects, one pipe per copy.
- **The Win32 screenshot.** `PrintWindow(PW_RENDERFULLCONTENT)` against a hardware-rendered,
  possibly-occluded PCSX2 window: whether it returns pixels at all, and whether the blank-grab retry and
  the two fallbacks behave as described.
- **`wt.exe` tab launching** and the one-console-per-agent fallback, for `kit run`. `wt.exe` is a client
  that asks the running terminal to open the tab and then exits, so the agent inherits the terminal's
  environment rather than the kit's, and the slot's pid comes back through a shim instead of from the
  process the kit started. Both of those are the kind of thing that works or does not.
- **The launchers.** The interpreter search in `kit.cmd` and `kit.ps1`, and whether `kit` resolves for a
  session the kit did not start itself (see the `wt.exe` point above).
- **Process handling.** The toolhelp snapshot, `WM_CLOSE` shutting PCSX2 down cleanly, Ctrl+Break
  reaching a console agent, and the `msvcrt` lock being released when a crashed recorder dies.
- **Long runs as a whole.** Worktrees, build-directory caps, merging, and the hooks, over hours rather
  than one command.
- **Worktree links.** A slot's `context\` and disc image are shared with the main checkout by a symlink,
  and an unprivileged Windows cannot make one: the kit falls back to a directory junction (`mklink /J`)
  and then to a hard link, and logs it if all three fail. Which of the three actually happens on your
  machine is unknown.
- **Where PCSX2 keeps its config.** `%USERPROFILE%\Documents\PCSX2` is right for an installed PCSX2 2.x.
  A *portable* build keeps its config beside the exe instead, and nothing here detects that — point
  `[emu] config_dir` in `kit.toml` at it by hand if that is your install.
- **The hooks' shell.** `.claude\settings.json` runs them as `py -3 "$CLAUDE_PROJECT_DIR\..."`, which
  assumes Claude Code runs hooks through Git Bash (its documented Windows requirement) so that
  `$CLAUDE_PROJECT_DIR` expands. Under `cmd.exe` it would need `%CLAUDE_PROJECT_DIR%` instead: one
  spelling, in three places in that file.
- **A localised Windows.** Process listing prefers the toolhelp snapshot, but its fallback parses
  `tasklist /FO CSV`, whose *headers* are translated on a non-English install. The fields are not, so the
  parser should hold; it has not been seen to.

One thing about the launcher, since it is the first thing you will type: `kit.cmd` works from `cmd.exe`
and from PowerShell, `kit.ps1` is there for PowerShell's own exit-code handling, and both look for `py
-3`, then `python`, then `python3` — `KIT_PYTHON` overrides the search. PowerShell does not search the
current directory, so it is `.\kit <command>` from the repository until you add the repository root to
your `PATH`; these pages write the short form. And if PowerShell refuses to run `kit.ps1` at all, that is
the execution policy rather than the kit:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, or use `kit.cmd`, which has no policy to satisfy.

If one of these turns out to be wrong, fix the code, and fix the line here that promised it. A list of
untested claims is only useful while it is current.
