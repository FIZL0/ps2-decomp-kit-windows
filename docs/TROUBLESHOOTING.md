# Troubleshooting

The failures that actually happen, with what to do. `kit doctor` is always a reasonable first move — it
checks every prerequisite and prints the command that fixes each problem it finds. `kit doctor --live`
additionally talks to a running game.

---

## Setup and configuration

**`no kit.toml` / `kit.toml is incomplete`**
Run `kit setup`. It writes the file and fills in what it can discover. `kit doctor` lists what is
still missing, field by field.

**`kit setup` cannot find my disc image**
It searches the repository, its parent and the usual download directories for raw images. Point it
directly: `kit setup --iso /path/to/game.iso`, or set `[game] iso` in `kit.toml` by hand.

**The disc image will not open**
The kit reads raw images only. A `.chd` or `.cso` must be converted first:
```powershell
chdman extractcd -i game.chd -o game.cue -ob game.bin
maxcso --decompress game.cso
```
A raw image is 500 MB–8 GB; a `.bin` track's size is divisible by 2352.

**`serial mismatch: disc says SXXX-NNNNN, kit.toml says SYYY-MMMMM`**
You are pointing at a different release of the game than the one you configured — a different region, or
a different revision. Decide which one the project targets and use only that. Do not "fix" it by editing
`kit.toml` to match whatever happens to be mounted: regions differ in physics constants and frame rate,
and captures from the wrong one look perfectly normal.

---

## Ghidra

**The PS2 processor is not in the list / everything decompiles as generic MIPS**
The EmotionEngine extension is not loaded. It must be built for your *exact* Ghidra version; a mismatch
fails quietly. Unzip into `<ghidra>/Ghidra/Extensions/`, start the GUI once so it registers, close it,
then `kit doctor`.

**`analyse` has been running for forty minutes**
That is normal. It is tens of minutes per program and it only happens once. `kit ghidra dump` afterwards
is seconds, because it runs post-scripts with analysis disabled against the saved project. If you find
yourself waiting for analysis repeatedly, something is re-importing — check `kit ghidra status`.

**The decompilation is tiny, or the interesting code is missing**
The game's logic is in an overlay that was not in the boot ELF. `kit elf --scan`, add the `[[overlay]]`
blocks, `kit elf`, then analyse again. See `docs/PS2.md` on overlays.

**Every function looks like garbage; references point nowhere**
The program is loaded at the wrong address. Check with `kit elf --info`: with the correct base, string
references land on strings and function pointers land on prologues. `--scan`'s answer is a guess and is
sometimes wrong.

**`kit fn <addr>` says the address is not in any function**
Three usual causes: the address is in a different program (`-p <name>`; the default is the last overlay),
you have an uncached-mirror address (mask off the top bits — `0x2…`/`0xa…` are the same RAM), or Ghidra
did not identify a function there because nothing references it. For the last, `kit dis <addr>` still
works.

---

## PINE and the emulator

**`could not connect to PINE`**
PINE is a TCP socket here, so the first question is whether anything is listening:

```powershell
netstat -ano | findstr 28011         # LISTENING, with PCSX2's pid in the last column
```

If there is no line, in order: is the emulator running (`kit pcsx2 status`)? Is a game actually booted —
PINE answers only once something is running, not at the BIOS screen. Is this the right copy
(`$env:KIT_PCSX2`, and copy *N* listens on `28011 + N`)? Launch through `kit pcsx2 start`, which sets the
PINE setting immediately before launching, since PCSX2 rewrites its ini on exit and a hand-enabled
setting may have been lost.

If there *is* a listening line and the connection is still refused, something is filtering the loopback
connection — Windows Defender Firewall if you dismissed its prompt (delete PCSX2's rule in `wf.msc` and
relaunch to get the prompt back), or a third-party security product that blocks local sockets.

**PINE connects, then everything hangs**
Two clients. PINE serves one connection; a second while a recorder is polling wedges both. Find the other
one (`kit pcsx2 status` shows who holds the lock), let it finish, and read static values from a save
state (`kit state extract`) instead of opening a second connection.

**`the frame counter is not moving`**
Either the game is paused or the emulator is not running at full speed; or the configured address is not
actually the frame counter. Re-find it: `kit probe vsync --save`. Tools deliberately bail out here
rather than spin, because spinning holds the lock.

**`kit probe vsync` finds nothing**
It looks for words that increase once per frame. The game must be *running* — not paused, not in a load
screen, not at a still title screen in some games. Boot into gameplay or a demo, then run it again.
`--range` narrows the search if the heuristic picks noise.

**The emulator is unresponsive and I want to stop it**
`kit pcsx2 stop`, which waits for anything mid-use and stops only your copy. If that will not do it,
`tasklist /FI "IMAGENAME eq pcsx2-qt.exe"` and `taskkill /PID <pid>` for the one you started. Never
`taskkill /IM pcsx2-qt.exe`: that takes out the user's own session, and in a parallel run the other
agents' copies. Never match on window title either — `taskkill /FI "WINDOWTITLE eq *pcsx2*"` can match
the console you typed it in.

**The emulator runs but the game is at the BIOS screen**
No BIOS, or the wrong path. It belongs in `%USERPROFILE%\Documents\PCSX2\bios\`, and `kit doctor`
checks the `[Folders] Bios` the ini actually names. A portable PCSX2 install keeps its folders next to
the executable instead. The BIOS has to be your own dump. `kit pcsx2 start` refuses outright when that
folder holds no image, rather than starting an emulator that can boot nothing, and it names the BIOS it
picked in its own output — a config with `[Filenames] BIOS` empty is the common version of this.

**The emulator runs and the screenshot shows PCSX2's setup wizard**
That config has never been through the first-run wizard, so `pcsx2-qt -- <iso>` opens the wizard and
ignores the disc entirely. `kit pcsx2 start` now writes `[UI] SetupWizardIncomplete = false` on every
launch, so this should not recur; if you see it, you are launching PCSX2 some other way.
(`pcsx2-qt -testconfig` writes a default ini without a GUI, but does *not* clear that flag.)

**Frame counts disagree with the retail game by a constant ratio**
Check the BIOS region against the disc's: `kit doctor` prints both, and warns when they differ. A PAL
BIOS boots an NTSC disc perfectly well and runs the machine at 50 Hz, so every measurement taken against
it is off by 50/60 and nothing looks broken. The kit prefers a region-matching BIOS for its own copies
and leaves your own configuration alone with a note.

---

## Captures

**`CAPTURE FAILED: …`**
Whatever that run wrote is incomplete and must not be analysed. The message names the cause — a gap in
the frame counter, a dropped connection, a counter that stopped. Fix it and record again. This is loud on
purpose: a broken capture that looks plausible is the most expensive bug class in this kind of work.

**Skipped frames**
The emulator is not keeping up, or the watch spec is asking for more than fits in one frame. Reduce the
speed multiplier, trim the spec, or raise the allowed gap deliberately and knowingly. Note that the
recorder already coalesces fields into contiguous regions and uses one batched read per frame, so if it is
still too slow you are reading a lot.

**My capture does not match my port, and I cannot tell where it starts to differ**
Compare the first differing *frame*, not the end state. Record the neighbouring fields too — a divergence
in position usually shows up a frame earlier in a velocity or a state number. And check the obvious:
same region, same save state, same seed.

**Captures are not reproducible between runs**
The state needs a human playing. Find a self-running one (demo, attract mode, CPU-versus-CPU) and keep it
in slot 5. Wall-clock pad timing is not frame-exact and never will be; do not build a proof on it.

---

## The virtual pad

**`no module named vgamepad` / the pad backend is unusable**
```powershell
pip install vgamepad
```
And the driver underneath it, which is a separate install:
<https://github.com/nefarius/ViGEmBus/releases>. `kit pad check` names whichever of the two is missing.

**`vgamepad` is installed but creating the pad fails**
The ViGEmBus driver is not running. Either the install was blocked (SmartScreen stops that installer
routinely — *More info → Run anyway*), or an antivirus product quarantined the driver afterwards, which
looks like a pad that worked once and then never again. Check it is there and started:
```powershell
Get-Service ViGEmBus                   # Status should be Running
```
If it is `Stopped`, `Start-Service ViGEmBus` in an administrator PowerShell; if it is absent, reinstall
and exclude `C:\Program Files\Nefarius Software Solutions\ViGEmBus\` from scanning.

**The pad does not appear in PCSX2**
`kit pad serve` must stay running — the device disappears when it exits. Start it before the emulator,
in its own window (`Start-Process .\kit.cmd -ArgumentList 'pad serve'`; PowerShell has no `&`), then map it as
controller 1 in the emulator's controller settings. `joy.cpl` is the quick check that Windows itself can
see an "Xbox 360 Controller". With several copies, note that all ViGEmBus pads share one vendor and
product id, so the SDL device filter cannot separate them — confirm each copy bound the pad you meant.

**`kit pad` reports success and the game does not react**
Controller port 1 in that emulator's ini is bound to something else — commonly the keyboard, if that is
how you play, which a kit copy then switches off. Everything else keeps working, which is what makes this
expensive: the device exists, `send` succeeds, `kit shot` is fine because it names the pad explicitly,
and the game receives nothing. `kit pad check` reads the ini and says which sources port 1 is bound to;
`kit pcsx2 copy N` rebinds port 1 to copy N's pad.

**Pressing buttons triggers save states and turbo**
You sent `select`. PCSX2's hotkeys are Select plus a shoulder button. `kit pad` refuses `select`; if you
are driving the pad another way, do not send it.

---

## Tests, builds, disk

**`kit check` says the build failed**
It is a build error, not a test failure — the distinction is in the first line. The full log is in the
private directory at the path it prints; read the end of it, not all of it.

**Tests pass individually and fail together**
Usually shared state: the same emulator, the same scratch save-state slot, the same output path. Give each
test its own scratch slot and its own output directory, or make them share one setup deliberately.

**A test takes minutes**
Keep tests under about ten seconds. Force the rare case directly instead of simulating until it happens
naturally. If a test genuinely needs a long run, mark it ignored with a comment saying how to run it and
when it matters, and run it when you touch that area.

**The disk is full**
Build directories. A Rust workspace reaches tens of gigabytes; a parallel run multiplies that by the
number of worktrees. `kit check` cleans the oldest when the total exceeds the cap in `kit.toml`; lower
the cap, or `cargo clean` in the worktrees you are done with. To see where it went:
`Get-ChildItem -Directory | ForEach-Object { "{0,10:N0} MB  {1}" -f ((Get-ChildItem $_ -Recurse -File |
Measure-Object Length -Sum).Sum / 1MB), $_.Name }`.

---

## Unattended runs

**`kit run` opened three console windows instead of three tabs**
It wants `wt.exe` (Windows Terminal) to put each agent in a tab of one window; without it, it says so and
gives each slot its own console window instead. The run is the same either way.
`winget install Microsoft.WindowsTerminal` gets the tabs back.

**`kit run 3` ran one session at a time**
That is the sequential path, and it is chosen for a slot count of 1, for `kit run once`, or for
`--no-tmux` — whatever is installed, because a single session needs no second terminal. If you asked for
three and got one, it also prints why: no multiplexer at all, meaning neither Windows Terminal nor (on
the Linux edition) tmux.

**A tab opened and closed immediately, and the slot shows no pid**
`wt.exe` is a client: it asks the running terminal to open a tab and exits, so the agent is started by
the terminal's environment rather than the kit's. A tab that dies at once is usually a `kit` that is not
on `PATH` for that environment, or a `PATH` set only in the shell you launched from. Check with
`wt.exe -w kit new-tab --title test cmd /k kit doctor` and read what the tab says before it closes.

**An agent stopped early with tasks remaining**
Read its journal entry and `kit plan` — a blocked task is a result, with its reason recorded. If it
stopped for no recorded reason, that is a bug in the run, not in the task.

**Two agents edited the same file**
Pick tasks for different files: `kit run 3 --mode d`, which is the default. Worktrees make the conflict a
merge rather than corruption, but it is still wasted work.

**An agent is waiting on an interactive prompt**
Something it ran needs confirmation — most often a delete with a wildcard, which prompts even in
permissive modes. Avoid wildcard deletes entirely: write each run to a fresh directory, or let the tool
overwrite its own files. The other Windows source of this is a UAC prompt, which an unattended session
cannot answer at all: nothing in the normal loop needs administrator rights, so if a task seems to, it
belongs in `plan/HUMAN.md`.

---

## Windows-specific

These are the platform's doing rather than the kit's, and all of them look like something else failing.

**"File not found" on a path that exists; Ghidra or the build fails deep in a directory tree**
The 260-character path limit. A Ghidra project and a Rust `target\` both nest past it. Turn long paths on
(`SETUP.md`, *First, four Windows things*) and check with
`(Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem').LongPathsEnabled`. Until the
registry change takes effect at the next sign-in, a shorter checkout path (`C:\p\myport`) also works.

**Everything is several times slower than the documented timings**
Defender's real-time scanning, reading every file the compiler and the Ghidra analyser write. Add the
exclusions in `SETUP.md`; `(Get-MpPreference).ExclusionPath` says whether they took. If you cannot set
them, the numbers in these pages are simply wrong for your machine — note the real ones in the journal
rather than treating a slow analysis as a hang.

**A test that passes on the Linux machine fails here, on a file comparison**
Line endings. Git rewrote `\n` to `\r\n` on checkout, so a committed fixture no longer matches byte for
byte. The repository's own `.gitattributes` pins text files to LF, so this means either a clone made
before it existed or a fixture a `.gitattributes` rule does not cover. `git config --global
core.autocrlf input`, then `git add --renormalize .` or a fresh clone. Check one file with
`[IO.File]::ReadAllBytes('watch\frame.toml') -contains 13` — `False` is what you want.

**A rename produced an empty commit, or a file vanished**
The filesystem is case-insensitive and Git here is configured to match. Two files differing only in case
are one file on this machine and two on Linux. Rename through `git mv -f`, and keep one spelling
convention per language so the question never arises.

**`kit doctor` says there is no BIOS, but PCSX2 boots games fine**
It looked in `%USERPROFILE%\Documents\PCSX2\bios\` and your PCSX2 keeps its files somewhere else —
either because OneDrive has redirected your Documents folder, or because you run the portable build,
which keeps everything next to the exe. `kit doctor` prints the path it used, so compare it with the
`bios` entry in PCSX2's own Settings → Folders. Nothing else is affected: the kit's own copies are told
where to look with `-cfgpath`, so captures and save states still work.

**The kit cannot find PCSX2, Ghidra or Python although they are installed**
All three are looked up on `PATH` or at the path in `kit.toml`, and an installer that offered to add
itself to `PATH` may not have. `Get-Command pcsx2-qt, java, python` says what this shell can actually
see; set `[emu] pcsx2` and `[ghidra] home` to absolute paths rather than fighting `PATH`. A `PATH` change
made by an installer does not reach shells that were already open.

---

## When none of this helps

Write what happened in the task's journal entry — what you tried and what you observed, not a conclusion
— block the task with a one-line reason, and move to another section. Something that genuinely needs a
human goes in `plan/HUMAN.md`. What does not help is the same fix a third time.
