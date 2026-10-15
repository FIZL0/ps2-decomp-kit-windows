# Setup

Eight steps, after four Windows settings to get out of the way first. Each one ends with a command that
tells you whether it worked, so you never move on with a broken foundation. Budget about an hour of
attention, plus however long your machine takes to decompile (step 6 is slow and unattended).

Use PowerShell 7 (`winget install Microsoft.PowerShell`). Two of the steps below, and most of the
troubleshooting in these pages, are pipelines; `cmd` has none of them.

If you only want to read code and not run the game, you can stop after step 6. If you only want to
capture the running game, you can skip steps 5–6.

Throughout: `kit doctor` is the answer to "what is wrong right now". Run it whenever something
surprises you.

---

## What you need to have

| Thing | Why | Where it comes from |
|---|---|---|
| The PS2 game, on disc | The kit reads code and data from the image at runtime and never ships any | **You own it**; you dump it yourself |
| A PS2 BIOS | PCSX2 cannot boot anything without one | **Your own console**, dumped by you |
| PCSX2 2.x | Runs the game so you can watch RAM and record what it does | <https://pcsx2.net> |
| Ghidra 11+ | Disassembles and decompiles the game's code | <https://ghidra-sre.org> |
| The EmotionEngine processor extension | Ghidra does not know the PS2's CPU out of the box | `ghidra-emotionengine-reloaded` |
| A JDK 21 or newer | Ghidra needs one and does not bring its own | `winget install Microsoft.OpenJDK.21` |
| Python 3.11+ | The kit itself | python.org, or `winget install Python.Python.3.12` |
| Git for Windows | `kit done` commits; `kit run` uses worktrees | `winget install Git.Git` |
| The ViGEmBus driver | The virtual controller the kit creates plugs into it | <https://github.com/nefarius/ViGEmBus/releases> |
| A build toolchain for your port | Whatever you write the re-implementation in | your choice (Rust: `winget install Rustlang.Rustup`) |
| ~30 GB free disk | Extracted disc, Ghidra project, decompilation dumps, captures | — |

Neither the game nor the BIOS can be downloaded legally, and neither one is in this repository. Dump
your own. The kit refuses to run against a disc whose serial does not match the one in your `kit.toml`,
which also means it will tell you clearly if you point it at the wrong image.

A note on paths in this document: the kit accepts either slash on Windows, so paths inside the repository
are written with forward slashes (they are also the links in these pages). Paths outside it — PCSX2's
folders, your Ghidra install — are written the Windows way, with backslashes and `%VAR%`.

---

## First, four Windows things

None of these are the kit's doing; all four otherwise cost an afternoon, and three of them fail in ways
that look like a bug in something else. Set the two Git ones before you clone, in step 1; the Defender
exclusion names the repository, so it waits until you have one.

**Long paths.** A Ghidra project and a Rust `target\` directory both nest past 260 characters, and the
failure is "file not found" on a path that plainly exists. Turn the limit off system-wide, and tell Git
separately because it has its own:

```powershell
# administrator PowerShell; or gpedit.msc → Computer Configuration → Administrative Templates
#                                         → System → Filesystem → Enable Win32 long paths
New-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' `
  -Name LongPathsEnabled -Value 1 -PropertyType DWORD -Force
git config --global core.longpaths true
```

**Check:** `(Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem').LongPathsEnabled`
prints `1` and `git config --get core.longpaths` prints `true`. The registry value takes effect on the
next sign-in.

**Line endings.** Git's default on Windows rewrites newlines on checkout, and this project byte-compares
files for a living. Captures themselves are in git-ignored `context/`, so the risk is to the watch specs,
fixtures and reference data you *do* commit — a fixture that differs from the Linux machine's only by
`\r` fails a test for no visible reason:

```powershell
git config --global core.autocrlf input
```

The repository's own `.gitattributes` carries the other half, pinning text files to LF on checkout, so
the rule travels with the project; add a line to it for any new binary format your port commits.

**Check:** in the clone, `[IO.File]::ReadAllBytes('watch\frame.toml') -contains 13` is `False` — no
carriage returns in a committed text file.

**Defender.** Real-time scanning reads every file a compiler writes and every file Ghidra's analyser
writes, and on this workload the cost is not a few percent: builds and the one-off analysis take several
times longer. From the repository root, in an administrator PowerShell:

```powershell
Add-MpPreference -ExclusionPath "$PWD", "$PWD\context", "$PWD\target"
```

**Check:** `(Get-MpPreference).ExclusionPath` lists them. On a managed machine where you cannot set
them nothing breaks, but expect `kit ghidra analyse` to take an hour rather than tens of minutes — write
that down rather than concluding it hung.

**Case.** NTFS is case-insensitive, so `Walk.rs` and `walk.rs` are one file here and two on a Linux
machine building the same port; the symptoms are a rename that produces an empty commit and a file that
disappears. Nothing to configure — `git config --get core.ignorecase` correctly prints `true` — so the
rule is yours to keep: one spelling convention, and never two files differing by case alone.

---

## 1. Get the kit, check Python

```powershell
git clone <your fork of this repo> myport
cd myport
python --version           # must be 3.11 or newer; `py -3 --version` if `python` is not on PATH
.\kit                      # should print the command list
```

If `python --version` opens the Microsoft Store, Python is not actually installed — the Store stub is on
PATH instead. Install it from python.org or with `winget install Python.Python.3.12`, and **tick "Add
python.exe to PATH"** in the installer. `kit` looks for `py -3`, then `python`, then `python3`, and
`KIT_PYTHON` overrides all three if you keep your interpreter somewhere unusual.

**`kit` is `kit.cmd` in the repository root**, with `kit.ps1` beside it for PowerShell. PowerShell does
not search the current directory, so from the repository it is `.\kit <command>`; these pages write it
as `kit <command>`, which is what you get once the repository root is on your `PATH`:

```powershell
$env:PATH += ";$PWD"                                      # this shell only
[Environment]::SetEnvironmentVariable('PATH',
    [Environment]::GetEnvironmentVariable('PATH', 'User') + ";$PWD", 'User')   # permanently
```

If PowerShell refuses to run `kit.ps1` at all, that is the execution policy, not the kit:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, or use `kit.cmd`, which has no policy to satisfy.

Nothing to install beyond that. Everything static — reading the disc, the ELFs, the decompilation, the
task list — runs on a stock Python with no packages at all.

Two optional packages, each needed by exactly one feature:

```powershell
pip install vgamepad       # the virtual controller (needs the ViGEmBus driver too — step 7)
pip install numpy          # makes kit probe faster. It works without.
```

**Check:** `kit doctor` runs and complains only that there is no `kit.toml` yet, and
`py -3 tests\test_plan.py` ends with `all checks passed`.

### Before you go any further: do the loop once, on a game that does not exist

```powershell
kit demo --check           # under a second, no disc, no emulator, no BIOS
```

That generates a synthetic PS2 program — a fake ISO with a fake boot ELF and a fake overlay, every byte
of it assembled by `ps2kit\demo.py` — and runs the entire workflow on it: extract, find the overlay's
load address, read the code, call a function with the oracle, and prove a Rust port of it bit-exact over
ten thousand inputs. **[docs/EXAMPLE.md](docs/EXAMPLE.md)** is that walkthrough with the reasoning.

Half an hour there will save you a day of the steps below, because you will already know what the output
of each command is supposed to look like. It writes nothing outside `context\demo\` and never touches the
`kit.toml` you are about to create. It is also the cheapest check that this edition's shared half works
on your machine: it needs Python and a Rust toolchain, and nothing Windows-specific at all.

---

## 2. Dump your disc

Use any PS2-capable optical drive and a tool that produces a raw image (`.iso`, or `.bin`+`.cue`).

Put it where you like — beside the repo is tidy, inside the repo is fine too because `*.iso` is
git-ignored. **Never commit it.**

If you already have a compressed image, convert it first; the kit reads raw images only:

```powershell
chdman extractcd -i game.chd -o game.cue -ob game.bin    # CHD
maxcso --decompress game.cso                             # CSO
```

**Check:** the file is 500 MB–8 GB, and its first sixteen sectors are empty with `CD001` at the start of
sector 16 — which is what an ISO 9660 image looks like and what `kit extract` will expect:

```powershell
(Get-Item game.iso).Length / 1GB
[Text.Encoding]::ASCII.GetString([IO.File]::ReadAllBytes('game.iso')[0x8001..0x8005])   # -> CD001
```

A `.bin` track instead of an `.iso` is fine too; its size is divisible by 2352 rather than 2048.

---

## 3. Install and configure PCSX2

Install PCSX2 2.x (the Windows build from <https://pcsx2.net>, or `winget install PCSX2.PCSX2`) and run
it once so it writes its configuration. Everything it owns then lives under one directory:

| Under `%USERPROFILE%\Documents\PCSX2\` | What is in it |
|---|---|
| `inis\PCSX2.ini` | the settings file the kit edits before every launch (PINE, and a copy's folders) |
| `bios\` | where your own BIOS dump goes |
| `savestates\` | the `.p2s` slot files `kit state` and `kit pine load` read |
| `snaps\` | where PCSX2 writes its own screenshots, which is the fallback `kit shot` collects from |

A portable install (a `portable.ini` beside the exe) keeps all of that next to `pcsx2-qt.exe` instead.
Only one thing minds: `kit doctor` looks for your BIOS in the Documents path and will report it missing.
The kit's own copies are told where to look with `-cfgpath`, so nothing else changes. The same applies
if OneDrive has redirected your Documents folder.

Put your BIOS dump in `bios\`, then boot your game once by hand, so you know it runs at all before you
automate anything. Note the serial PCSX2 shows in its window title — it should match the one the kit
reads from the disc in step 4.

You do **not** need to enable PINE by hand. PCSX2 rewrites its ini on exit, so the kit sets
`EnablePINE` in the ini before every launch it manages. (If you want it on for your own manual
sessions too: Settings → Advanced → Enable PINE.)

**Check:** the game boots to its title screen in PCSX2 and runs at full speed, and
`Select-String EnablePINE "$env:USERPROFILE\Documents\PCSX2\inis\PCSX2.ini"` finds the key once the
kit has launched it in step 7.

---

## 4. Point the kit at the game

```powershell
kit setup
```

It finds PCSX2, finds Ghidra, finds your image, reads `SYSTEM.CNF` off the disc to learn the serial and
the boot ELF, and writes `kit.toml`. Answer the one or two questions it asks.

`kit.toml` is the **only** file in this repository that knows anything about your particular game.
Everything else is generic. Open it now and read it — it is short, and it is the thing you will come
back to.

```powershell
kit extract            # unpack the disc into context/iso (a few minutes)
```

`context/` is the private, git-ignored working directory. Everything derived from your disc or from the
running game lives there: extracted files, decompilation dumps, RAM images, captures. Nothing in it is
ever committed, and the kit will not write game-derived data anywhere else.

**Check:**

```powershell
kit doctor             # disc, serial, PCSX2, BIOS all green
ls context/iso         # the disc's files, including SYSTEM.CNF and the boot ELF
```

---

## 5. Work out the program layout

Most PS2 games are a small boot ELF plus one or more **overlays**: blobs of code the game loads over a
fixed address at runtime, usually one per major mode (menu, gameplay, movie player). Decompiling the boot
ELF alone often gets you almost nothing, because the interesting code is in an overlay that was not
loaded when the ELF was dumped — and an overlay disassembled at the wrong address is unreadable, because
every absolute reference points at nonsense.

```powershell
kit elf --scan
```

This walks the extracted disc for files that look like raw MIPS code and, for each, reports the load
address implied by its own internal references, printing a ready-to-paste `[[overlay]]` block. Paste the
ones that look right into `kit.toml`.

If the scan finds nothing, the game may have no overlays (then just delete the `[[overlay]]` block) or
may pack them inside an archive format of its own — in which case that archive is your first real
reverse-engineering task, and `plan/TODO.md` has it as one.

```powershell
kit elf              # build one ELF per program into context/elf
```

**Check:** `kit elf --info context/elf/boot.elf` prints plausible segments, and the overlay ELFs exist
with the load addresses you configured.

---

## 6. Decompile

Ghidra needs a JDK, and does not bring one. This is the single most common thing missing on a Windows
machine, and Ghidra's own error for it ("JAVA_HOME is not set", or a console that closes instantly) does
not say so clearly:

```powershell
winget install Microsoft.OpenJDK.21      # or any JDK 21+
java -version                            # must print 21 or newer
```

Then install Ghidra (unzip it anywhere; `C:\ghidra` keeps the paths short) and add the PS2 CPU:

1. Download `ghidra-emotionengine-reloaded` for **your exact Ghidra version**.
2. Unzip it into `C:\ghidra\Ghidra\Extensions\`.
3. Start Ghidra's GUI once (`ghidraRun.bat`) so it registers the extension, then close it.

Set `[ghidra] home` in `kit.toml` if `kit setup` did not find it — the kit runs
`support\analyzeHeadless.bat` from there — then:

```powershell
kit ghidra analyse       # SLOW: tens of minutes per program. Run it once, go and do something else.
kit ghidra dump          # seconds: writes the decompilation and xref dumps into context/decomp
```

Only `analyse` is expensive. Everything after this reads the dumps, and the dumps are refreshed by
`dump` in seconds, so you never pay the analysis cost twice unless you re-import.

**Check:**

```powershell
kit ghidra status        # what is in the project, and how old each dump is
kit fn 100008            # prints one decompiled function
kit xref 100008          # its callers and callees
```

> **Never open those dumps whole.** A decompilation dump of a PS2 game is 10–30 MB. `cat`ing one into an
> AI agent's context ends the session; opening one in an editor is merely unpleasant. Use `kit fn`,
> `kit xref` and ranged reads. The kit ships a hook that blocks the whole-file read and suggests the
> cheap alternative, which is there because this mistake is easy to make and expensive every time.

---

## 7. Reach the running game

Live memory access is how you prove things. PCSX2 exposes it over PINE, which on Windows is a **TCP
socket on `127.0.0.1:28011`** (`[emu] pine_slot` in `kit.toml`; copy *N* uses `pine_slot + N`).

```powershell
kit pcsx2 start            # boots your game with PINE enabled
kit pine                   # version, serial, title, status, frame counter
```

Because it is a real socket, Windows Defender Firewall may put up a prompt the first time PCSX2 opens it.
Allow it for **private networks only** — the kit never connects to anything but `127.0.0.1`, and nothing
about this needs to be reachable from another machine. If you dismissed the prompt by accident, delete
PCSX2's rule in `wf.msc` and launch again to get it back.

If `kit pine` reports a different serial than your `kit.toml`, it refuses to go further. That check has
saved more time than any other line in the kit: captures taken against the wrong region or revision look
completely normal and are completely wrong.

Now find the game's frame counter — the clock every capture is timed against:

```powershell
kit probe vsync --save
```

It samples RAM while the game runs, finds the words that increase at the frame rate, verifies the best
candidates, and writes the winner into `kit.toml`. One command, once per game.

**Check:** `kit pine` prints a frame counter that climbs when the game is running and holds still when
it is paused.

### The virtual controller (optional)

To drive the game unattended you need a pad the emulator believes in. On Windows that is a virtual Xbox
360 controller through **ViGEmBus**, a kernel driver, which `vgamepad` talks to:

1. `pip install vgamepad`. Its installer offers to install ViGEmBus for you, which raises a UAC prompt;
   accept it.
2. If you skipped that, or it failed, install the driver by hand from
   <https://github.com/nefarius/ViGEmBus/releases> (the `.exe` needs administrator rights, and sometimes
   a reboot).

SmartScreen usually stops that installer, a signed kernel driver from a small publisher being exactly its
profile; check you took it from the release page above, then allow it. Some antivirus products quarantine
the driver on a later scan, which looks like the pad working once and never again — exclude
`C:\Program Files\Nefarius Software Solutions\ViGEmBus\`. Do not switch protection off to get past a
*repeated* block. Skipping the pad entirely is fine: only `kit run` and the screenshot fallback need one.

```powershell
kit pad check                                    # is the backend usable, and if not, the exact fix
Start-Process .\kit.cmd -ArgumentList 'pad serve'   # keep it running; PCSX2 binds it as a controller
kit pad send "press start 100"                   # times are milliseconds
```

`pad serve` must stay running: the device disappears with the process. In PowerShell there is no `&`, so
start it in its own window (above) or its own Windows Terminal tab.

ViGEm pads are XInput devices and XInput has four slots, so four is the hard limit on pads — a fifth
always fails, which also caps `[emu] instances` at four if those copies need pads.

In PCSX2, map the new pad as controller 1 (Settings → Controllers). Timing is wall-clock, not
frame-exact; for frame-exact input use PCSX2's own input recording.

**One thing never to do:** do not send `select`. PCSX2's hotkeys are Select plus a shoulder button, so
sending it triggers save-state and turbo hotkeys instead of reaching the game. `kit pad` refuses it.

**Check:** `kit pad check` reports the backend usable and a server running, and the pad appears as
"Xbox 360 Controller" in `joy.cpl` and in PCSX2's controller list.

---

## 8. Make your save states

The kit's recorders load a save state to reach an exact moment reproducibly. Only you can make them:
play to the moment, then save to a slot (F1 by default, with the slot chosen by F2). They land in
`%USERPROFILE%\Documents\PCSX2\savestates\` as `<SERIAL>_<CRC>.<NN>.p2s`, which is where
`kit state` and `kit pine load` look for them. Write down what each slot holds in `kit.toml`'s
`[states]` so tools and agents know which to load.

Three that earn their keep in almost any game:

| Slot | What to save there | Why |
|---|---|---|
| 3 | the title screen, just booted | a clean start for menu and loading work |
| 4 | mid-action, paused at an interesting moment | the usual starting point for a capture |
| 5 | something that **runs itself** — a demo, attract mode, a CPU-vs-CPU match | the most valuable state you own: captures that need no input, so they are perfectly reproducible |
| 8, 9 | leave empty | scratch; the kit's tools overwrite these |

Slot 5's kind of state is worth hunting for. A capture that needs a human playing is a capture you
cannot repeat exactly, and a port can only be proved against something repeatable.

**Check:** `kit pine load 5` reaches the moment you expect, and `kit record watch/frame.toml
--frames 120` writes 120 rows with no skipped frames.

---

## You are set up

```powershell
kit doctor --live        # everything, including a live PINE check
```

Read `docs/WORKFLOW.md` next: it is the loop you will be in from now on — find the code, port it, prove
it, write down what you could not prove. Then `kit plan next` gives you the first task from a backlog
already written for a new port.

---

## When something is wrong

`docs/TROUBLESHOOTING.md` covers the failures that actually happen: PINE not connecting, the serial
mismatch, Ghidra's extension not loading, the frame counter not being found, captures full of skipped
frames, the pad not appearing in PCSX2, a build directory eating the disk — with a Windows-specific
section for the ones that are this platform's fault rather than the kit's.

`WINDOWS.md` is the other thing to read before you trust a result: it lists what this edition does
differently from the Linux one, and what has not yet been run on a real Windows machine.
