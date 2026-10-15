# Driving the emulator

PCSX2 is how you watch the real game. Everything here is about using it as an instrument rather than by
hand: reading its memory, pressing its buttons, taking its picture, and running several copies without
them fighting.

---

## PINE: reading the running game's memory

PINE is PCSX2's memory IPC. On Windows it is a **TCP socket on `127.0.0.1:28011`** — `[emu] pine_slot`
in `kit.toml`, with copy *N* on `pine_slot + N`. (On Linux and macOS the same protocol runs over a Unix
socket; nothing above the transport differs.)

```powershell
kit pcsx2 start                      # launch with PINE enabled
kit pine                             # version, serial, title, status, frame counter
kit pine read 0x3f2a10 --as f32      # one value
kit pine read 0x3f2a10 4             # four of them
kit pine block 1c0000 0x200 --out context/probe/dump.bin
kit pine watch @score @frame --frames 120
kit pine write 0x3f2a10 5 --as u32   # poke (careful)
kit pine save 9; kit pine load 9     # scratch slots are 8 and 9
```

You do not need to enable PINE in the GUI. PCSX2 rewrites `inis\PCSX2.ini` on exit, so the kit sets
`EmuCore/EnablePINE` and `EmuCore/PINESlot` in that file immediately before every launch it manages.

Being a TCP socket has two consequences worth knowing. The first time PCSX2 opens the port, Windows
Defender Firewall may prompt; allow it for private networks only, since nothing here reaches past
`127.0.0.1`. And "PINE is not there" is a question about a port rather than a file:

```powershell
netstat -ano | findstr 28011            # a LISTENING line, with PCSX2's pid at the end
```

No listening line means the emulator is not up, or is up without a game booted, or had the ini setting
lost on its last exit — launch it through `kit pcsx2 start`, which sets the key first.

**The serial check.** Every connection compares the running game's serial against `kit.toml` and refuses
if they differ. Keep it. A capture taken against the wrong region or revision looks entirely normal and
is entirely wrong, and that mistake is expensive precisely because nothing about it looks broken.

**One client at a time.** PINE handles one connection; a second client while a recorder is polling hangs
both, and you then have a wedged emulator and a half-written capture. Two consequences:

- Every tool that talks to PINE takes a lock first. Multi-step jobs hold it across the whole job:
  ```powershell
  kit pcsx2 with -- pwsh -File .\my-script.ps1    # holds the lock, sets KIT_PCSX2_LOCKED=1
  ```
- To read a static value *during* a capture, read it from a save state instead (`kit state extract`).
  No lock, no connection, and it works while the recorder runs.

**Batched reads.** PINE accepts several operations in one message. `kit record` uses this: it coalesces
all the fields in a watch spec into the fewest contiguous regions and fetches them in one request per
frame. The difference between that and one request per field is the difference between recording fifty
fields for 600 frames in ten seconds and in ten minutes.

**The frame counter is the clock.** `kit probe vsync --save` finds it once per game and writes it into
`kit.toml`. Every wait-for-next-frame goes through it, and a counter that has stopped moving is how a tool
knows the game paused or the thing it was recording ended — which is why waits bail out with a message
instead of spinning forever holding the lock.

---

## Save states

```powershell
kit pine save 9                              # the running game, into slot 9
kit state ram 4 --out context/ram4.bin       # the EE RAM out of a state file
kit state extract 4 --addr @table --len 0x400 --as f32
kit state list 4                             # what the file contains
```

Slot files are `%USERPROFILE%\Documents\PCSX2\savestates\<SERIAL>_<CRC>.<NN>.p2s`, or the copy's own
`savestates\` for a kit copy. A state is a 7z or zip archive; the EE's 32 MB RAM image is a member of it.
Reading data out of one needs no emulator at all, which makes it the cheapest source for tables, struct
layouts and constants, and the only safe one while a capture is in flight.

**Slot discipline.** Slots 3, 4 and 5 are yours — made by you, loaded by tools, never written by them.
Slots 8 and 9 are scratch and the kit's tools overwrite them freely. Record what each of your slots holds
in `kit.toml`'s `[states]`.

The state worth hunting for is one that **runs itself**: a demo, an attract-mode loop, a CPU-versus-CPU
match. A capture that needs a human playing cannot be repeated exactly, and a port can only be proved
against something repeatable. Put that one in slot 5.

---

## The virtual controller

Driving the game unattended needs a pad the emulator believes in.

```powershell
kit pad check                        # is the backend usable, and if not, the exact fix
Start-Process .\kit.cmd -ArgumentList 'pad serve'   # create the pad; leave that window running
kit pad send "press start 100"       # milliseconds, not frames
kit pad send "stick l 1 0" "sleep 400" "press cross 80" "release"
```

On Windows this is an Xbox 360 pad through the **ViGEmBus** driver, driven by the `vgamepad` package, so
SDL — and therefore PCSX2 — applies its standard mapping with no configuration. `kit pad check` names
whichever piece is missing, the driver or the package.

**A pad PCSX2 does not read passes every other check.** The device exists, `kit pad send` reports
success, and the game receives nothing — because controller *port 1* in that emulator's ini is bound to
something else (commonly the keyboard, if that is how you play, which `kit pcsx2 copy` then switches
off). `kit pad check` reads the ini and says which input sources port 1 is bound to, and `kit pcsx2 copy
N` binds port 1 to copy N's pad. It never touches your own config: bind port 1 there yourself, in
Settings -> Controllers.

`pad serve` holds the device open: the pad disappears when that process exits, so it needs a window or a
Windows Terminal tab of its own — PowerShell has no `&`. `send` reaches the server over a named pipe, so
a `send` from any window finds it, and `KIT_PCSX2` picks copy *N*'s server.

**Never send `select`.** PCSX2's hotkeys are Select plus a shoulder button, so a `select` press
triggers save-state and turbo hotkeys instead of reaching the game, and the resulting mess looks like a
game bug. `kit pad` refuses it outright.

Durations are milliseconds, with a `f` suffix for frames (`press cross 4f`) when you are thinking in
frames — but see the next paragraph before relying on that.

**Timing is wall-clock, not frame-exact.** Good enough to navigate menus, start a match, or hold a
direction for two seconds. Not good enough to reproduce a frame-perfect input sequence — for that use
PCSX2's own input recording, or better, find a state that needs no input at all.

**With several emulator copies**, each copy has to bind its own pad, and on Windows that is done by
device *index*: copy *N*'s config binds `SDL-<N-1>`, which is correct as long as the copies' pads are
created in order and no real controller is plugged in meanwhile. There is no better way available —
every ViGEmBus pad presents Microsoft's own vendor and product id, so the VID/PID filter the Linux
edition uses has nothing to match. Two consequences worth knowing before a long run:

- **Four pads at most.** ViGEm pads are XInput devices and XInput has four slots; `[emu] instances` above
  four leaves the extra copies with no pad, and therefore no pad-bound pause or screenshot hotkey.
- **Your own emulator can grab one.** The kit cannot hide its pads from your PCSX2 either, for the same
  reason. Bind your own controller explicitly in your own copy, or stop the kit's copies before you play.

With more than one copy, confirm in each one's controller settings that it bound the pad you meant
before trusting a capture.

---

## Screenshots

```powershell
kit shot context/shots/title.png
```

It grabs the emulator's window through the Win32 API — `PrintWindow`, which asks the window to redraw
into a bitmap the kit owns, so nothing is focused or raised and a window with something on top of it
still captures. Two fallbacks, in order, because that call is at the mercy of the graphics driver: a
PowerShell screen copy (which does catch whatever is in front of the emulator, so check the PNG), and
then PCSX2's own Screenshot hotkey pressed through the virtual pad, with the result collected from
`snaps\`. The hotkey is the one that always works, including for a minimised copy — it is last only
because it needs the pad. Screenshots go in the private directory, never in the
journal or the repo: they are game data.

**Any external capture helper is given 15 seconds and no more** (`KIT_GRAB_TIMEOUT` to change it) — the
PowerShell fallback here, and `grim`/`import`/`spectacle` when this kit runs on Linux. That is not
impatience: on wlroots, `grim` returns in milliseconds while PCSX2 is stopped and never returns at all
once PCSX2 is rendering a game, because the emulator's game surface wedges screencopy. The hotkey
fallback works perfectly at that same moment, so a helper that has not answered by then is treated as
one that cannot see the window — which is what makes the fallback reachable rather than theoretical.
`PrintWindow` is in-process and cannot be bounded this way; it returns or it fails, and a blank result
is already retried over the whole window frame before being given up on.

For anything visual, take the original and your port at the same moment and look at them side by side.
It costs a minute and settles arguments that otherwise go several messages.

---

## Several copies at once

Unattended runs want more than one emulator. `KIT_PCSX2=N` gives a process its own copy of everything:

| | |
|---|---|
| config directory | its own, copied from yours on first use and passed to PCSX2 as `-cfgpath` |
| PINE slot | base slot + N, so ports never collide |
| lock | its own, so copy 1 and copy 2 never block each other |
| pad | its own named pipe; bound in that copy's config by device index |

A copy lives in `<repo>-slots\pcsx2\sN\PCSX2\`, a sibling of the repository rather than inside it, and
its `[Folders]` keys are rewritten to absolute paths underneath itself — so a copy cannot overwrite your
own save states or memory cards.

```powershell
$env:KIT_PCSX2 = 2        # for this shell: every kit tool, the pad, PINE and the lock use copy 2
kit pcsx2 copy 2          # make it, once
kit pcsx2 start           # a second, independent emulator
```

Set it per shell (or per Windows Terminal tab), not globally: `KIT_PCSX2=2 kit …` is shell syntax this
one does not have, and a variable left set in your profile is how you later record the wrong emulator.
This is what makes `kit run 3` possible.

---

## Housekeeping

**Stop the copy you started**, and only that one:

```powershell
kit pcsx2 stop         # waits for anything mid-use, then stops your copy
```

`stop` posts `WM_CLOSE` to the emulator's own window, which is the Windows equivalent of a SIGTERM for a
program with a GUI: PCSX2 shuts the virtual machine down and flushes its memory cards. `taskkill /F`
does none of that, which is a second reason to let the kit do it.

**Never kill the emulator by name.** `taskkill /IM pcsx2-qt.exe` kills the user's own session, and in a
parallel run the other agents' copies with it. Kill by pid, and only a pid you started:

```powershell
tasklist /FI "IMAGENAME eq pcsx2-qt.exe"     # which copies exist, with their pids
taskkill /PID 12345                          # one of them
```

There is a second, subtler trap behind the same rule. Matching on anything but the image name can match
the process doing the matching: `taskkill /FI "WINDOWTITLE eq *pcsx2*"` happily includes the console
window whose title holds that pattern *because you just typed it there* — the same self-shooting foot as
`pkill -f` on Linux. The kit matches image names only and tracks its own children by pid file, which is
why `platform_.pids_named()` exists.

**Don't touch the emulator while a capture is running.** Check before you act, and if a recorder is
polling, wait or read from a save state.

**`CAPTURE FAILED` means the output is rubbish.** Whatever the run wrote is incomplete — a missing frame,
a dropped connection, a stopped counter. Fix the cause and record again. Never analyse a file that
announced that.

**Write each run to a fresh directory** (`context/<task>/run1/`, `run2/`, …) or let a tool overwrite its
own files. Deleting with a wildcard is worth avoiding: the confirmation prompts around `del` and
`Remove-Item` with wildcards are exactly the kind of thing that stalls an unattended run for an hour.
