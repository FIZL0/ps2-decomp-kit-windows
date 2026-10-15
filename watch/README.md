# Watch specs — the recorder's input format

A *watch spec* is a small TOML file that says **what to sample, where it lives, when to sample it,
and what to set up first**. `kit record` reads one and writes a CSV (or packed binary) with a
`.json` sidecar holding the capture's provenance.

```sh
kit record --dry-run watch/ball.toml          # check the spec; no emulator, no kit.toml needed
kit record watch/ball.toml --state 5 -n 600   # 600 frames from save state 5
kit record watch/ball.toml -s 30 --pad watch/pads/serve.pad --binary
kit record --diff context/capture/a.csv target/port.csv
```

## Why a spec and not a script

The project this kit was generalised from accumulated **about fifty capture scripts** —
`record_live.py`, `record_camera.py`, `record_npc.py`, `trace_shots.py`, … — each a copy of the last
with different addresses. Every copy had to re-implement the same four things, and the copies that
got one of them wrong produced plausible-looking CSVs that cost days:

1. **One batched read per frame.** Thirty separate `read32`s do not come from one frame; the row is
   then a blend of two game states, and nothing downstream can tell.
2. **Torn frames skipped *and counted*.** `settle()` re-reads until two reads agree inside one
   frame. A script that silently retried, or worse interpolated, hid the fact that the emulator was
   outrunning it.
3. **Pointer chains re-resolved every frame.** Live objects move. Reading a pointer once at startup
   records a stale object, or garbage after a scene change.
4. **Provenance.** A capture nobody can reconstruct — which game, which state, which spec, which
   frames — is not evidence, it is a 40 MB rumour.

The spec gets all four for free, and the planner does something no hand-written script bothered
with: it **coalesces** the fields into as few contiguous blocks as possible, shares pointer reads
between chains with a common prefix, and lets the pointer words ride along in the same `settle` so a
sample taken across an object swap is thrown away instead of recorded.

## A spec, end to end

```toml
[watch]
name        = "ball"          # output name; default is the file stem
description = "ball flight during a rally"
every       = 1               # sample every Nth frame (default 1)
frames      = 600             # stop after N samples (--frames overrides)
seconds     = 30              # ...or after S seconds of wall clock (--seconds overrides)
start       = "phase == 2"    # do not record until this is true
stop        = "phase == 5"    # stop as soon as this is true
when        = "live == 1"     # only write rows where this holds
format      = "csv"           # or "binary"

[setup]
state     = 5                 # load this PCSX2 save-state slot first
warmup    = 30                # let N frames pass before sampling
pad       = "watch/pads/serve.pad"   # play a pad script from the start
settle_ms = 300               # pause after a state load (default 300)

[[field]]
name     = "pos"
addr     = "@match -> 0x88 -> 0xe0"
type     = "f32x4"
required = true               # a null here ends the capture cleanly

[[array]]
name   = "player"             # columns player0.* … player3.*
addr   = "@match -> 0xa8"     # a table of pointers
count  = 4
stride = 4
deref  = true                 # each entry is a pointer: follow it

[[array.field]]
name = "pos"
off  = 0x3d70
type = "f32x4"

[[array.field]]
name = "anim"
off  = "0x60 -> 0x24"         # chases a pointer inside the element
type = "u16"
```

At least one `[[field]]` or `[[array]]` is required, and so is an end: `frames`, `seconds` or
`stop` (otherwise the recorder would hold the PCSX2 lock forever). Unknown tables and unknown keys
are errors, with the line number — a typo in a spec must never be silently ignored.

## Addresses and pointer chains

An address is `0x1004c0`, bare hex `1004c0`, or `@name` from `[symbols]` in `kit.toml`
(`kit sym` lists them, `kit probe` finds them in the first place). `@name` is worth using: it
survives a different game version and still reads like prose a year later.

Arrows chase pointers, **one indirection per arrow**:

| `addr` | means |
|---|---|
| `0x1004c0` | the value at that address |
| `@globals + 0x24` | 0x24 past the symbol (plain arithmetic, no read) |
| `@match -> 0x54` | read the pointer at `@match`, take the byte at +0x54 of the object |
| `@match -> 0x88 -> 0xe0` | …then read *that* pointer too, and take +0xe0 of what it points at |

`0x80xxxxxx` (kseg0) addresses are masked down to the physical offset automatically, since that is
what the decompiler prints and what PINE wants. An address outside EE RAM is a spec error, not a
crash: fix the address, or set `[emu] ram_mb` if the game really has 128 MB (a devkit).

A chain with no arrows is resolved **once**. Everything with an arrow is re-resolved **every frame**,
because objects move. If a pointer anywhere in a chain is null (or garbage pointing outside RAM),
that field's cells are left **empty** for that frame, the row is still written, and the capture
carries on — unless the field is `required = true`, which ends the run cleanly with the reason in
the sidecar.

## Types

| `type` | bytes | columns |
|---|---|---|
| `u8 u16 u32 u64 s8 s16 s32` | 1–8 | one, named after the field |
| `f32` | 4 | one |
| `f32x3` | 12 | `.x .y .z` |
| `f32x4` | 16 | `.x .y .z .w` |
| `mat4` | 64 | `.m00 … .m33` (row-major as stored) |
| `bytes:N` | N (1..4096) | one, hex string — for a struct you have not decoded yet |

Default is `u32`. **Prefer `f32x4` to `f32x3` for PS2 vectors**: they are 16-byte quads and the `w`
lane is usually meaningful (or usefully garbage — a lane that should be 1.0 and is not tells you the
object was mid-update).

Floats are written with `repr()`, which round-trips every bit; a bit-exact comparison against a port
needs that. Two optional keys change the rendering, and both make a column unusable for an exact
diff, so do not set them on something you intend to prove:

- `scale = 0.0625` multiplies the value (fixed-point counters, degrees from a 4096-unit circle);
- `fmt = "hex"` writes `0x…`, and any other `fmt` is a printf format (`"%.3f"`).

## Arrays

One `[[array]]` block replaces the loop, the pointer chasing and the column naming:

| key | meaning |
|---|---|
| `addr` | the first element, or the first pointer when `deref = true` |
| `count` | how many elements (1..1024) |
| `stride` | bytes between elements (required when `count > 1`) |
| `deref` | each slot holds a pointer to the real object; follow it |

Two common shapes:

```toml
# a table of pointers to live objects
[[array]]
name = "player"
addr = "@match -> 0xa8"
count = 4
stride = 4
deref = true

# plain structs back to back at a fixed address
[[array]]
name = "shot"
addr = "0x2f0e80"
count = 8
stride = 0x30
```

Columns come out element-major: `player0.pos.x … player0.anim, player1.pos.x …`.

## Conditions

`when`, `start` and `stop` take a deliberately tiny grammar: `A op B`, joined by `and` / `or`, with
`op` one of `== != < <= > >=`. An operand is a field name, a number, or an address with an optional
type (`"@match -> 0x54:u8 >= 2"`). An address in a condition becomes a hidden field, so it rides
along in the same batched read and **costs nothing per frame**.

- `start` arms the capture (nothing is written before it first becomes true);
- `stop` ends it;
- `when` gates individual rows (frames where it is false are simply not written — the `frame`
  column shows the gap, nothing is interpolated).

A condition that needs a value from a null pointer chain counts as "not yet true", never as false.

## Output and provenance

CSV by default: a header row, then `frame` plus one column per value. `--binary` writes packed
little-endian records (`u32 frame`, `u32 live mask`, then the fields) for long captures; the sidecar
carries the exact `layout`. Both flush every row, so a killed run leaves a usable prefix.

Every capture gets `<out>.json` beside it, written at the start, periodically, and at the end:

```json
{ "kit": "ps2-decomp-kit …", "created": "…",
  "spec":  { "path": "watch/ball.toml", "sha256_16": "…", "text": "<the whole spec>" },
  "game":  { "name": "…", "serial": "SLUS-…", "region": "US", "version": "1.00" },
  "emulator": { "pine_slot": 28011, "vsync_addr": "0x…", "speed": 1.0, "game_id": "SLUS-…" },
  "setup": { "state": 5, "pad": "…", "warmup": 30 },
  "capture": { "frames": 600, "skipped": 0, "gaps": 0, "dropped_frames": 0,
               "frame_first": 101234, "frame_last": 101833, "ended": "…", "suspect": false } }
```

The spec text is embedded on purpose: the capture stays reproducible even after the spec is edited.

The end report names the damage, and says so loudly when the capture is not trustworthy:

```
600 frames captured in 10.2s (58.8/s), frames 101234..101833
  skipped 0 torn read(s), 0 counter gap(s) (0 frames the emulator ran past us)
  ended: frame budget reached (600)
```

More than a few percent torn reads prints `SUSPECT`, and the command exits non-zero: the emulator is
running faster than the recorder can poll it. Slow it down (`[emu] speed = 0.25`, or PCSX2 →
Settings → Emulation → Speed 25%) and record again; fewer fields a frame also helps.

Anything that goes wrong prints one line starting `CAPTURE FAILED:` and exits non-zero, because a
half-written CSV that looks plausible is worse than no CSV at all.

## Comparing captures

```sh
kit record --diff context/capture/ball-20261015.csv target/port-ball.csv
kit record --diff a.csv b.csv --tol 1e-5      # relative tolerance, for rendering-ish numbers
```

It joins on the `frame` column (or row order when there is none), reports columns and rows that
exist in only one file, and prints the **first** divergence with its frame and both values, then the
per-column mismatch counts:

```
600 rows x 36 columns compared (exact)
DIFFER: first divergence at frame 101240, column 'pos.y'
    A  1.2999999523162842
    B  1.2999999821186066
3 of 36 columns differ; worst: pos.y (412), pos.z (120), player0.pos.y (3)
```

Exit code 0 means identical, 1 means they differ. That is the proof step: a PINE recording of the
real game against the port's own CSV.

## Worked example: the smallest spec

`watch/frame.toml` watches the frame counter and three game-wide scalars. All four addresses are
static, the three `@globals` fields share a quadword, so the whole sample is **one** batched read:

```
$ kit record --dry-run watch/frame.toml
  4 fields, 4 columns, 2 group(s), 24 bytes/frame

  group @globals   [static]  1 region(s), 16 bytes
      +0x0000..+0x0009  (9 bytes from the group base)
        +0x0000  u32      state
        +0x0004  u32      timer
        +0x0008  u8       players
  group @vsync   [static]  1 region(s), 8 bytes
  …
  per frame: one batched read of 24 bytes in 2 region(s) at fixed addresses
```

Sampling the frame counter as a field as well as taking it from the poll is a free self-check: if
the `vsync` column ever disagrees with the `frame` column, the read tore in a way `settle` missed.

## Worked example: live objects

`watch/struct.toml` pulls four fields out of four live actors behind a pointer table, plus the ball.
Nineteen fields, three levels of indirection, and the planner turns it into three small pointer
reads and one data read a frame:

```
$ kit record --dry-run watch/struct.toml
  19 fields, 36 columns, 10 group(s), 200 bytes/frame

  group @match -> 0x88 -> *   [2 deref(s)]  2 region(s), 32 bytes
      +0x00e0..+0x00f0  (16 bytes from the group base)
      +0x0130..+0x013c  (12 bytes from the group base)
        +0x00e0  f32x4    ball_pos required
        +0x0130  f32x3    ball_vel
  …
  per frame: 3 small batched read(s) to re-resolve 10 pointer chain(s), then one batched read of
  200 bytes in up to 19 region(s)
  ~400 bytes a frame both reads together; at 60 fps that is 23 KiB/s
```

Note the group labels: `@match -> 0x88 -> *` is "the object that pointer points at", and every field
inside it shares one deref and one region.

## Traps

- **A bare number in a string is hex.** `addr = "0x100000 + 12"` means `+0x12`, not `+12` — the
  kit's convention everywhere a user types an address. A TOML *integer* (`off = 0x10`) is used as
  written; only strings get the bare-hex rule.
- **PINE reads quadwords.** Regions are snapped out to 8-byte alignment, so a 1-byte field costs 8
  bytes. Grouping related fields saves nothing in bytes but everything in round trips.
- **`every = N` is not a time base.** It samples every Nth *frame counter tick*; if the emulator runs
  ahead, counter gaps are counted (`dropped_frames`) and the missing frames are simply absent.
- **A pad script's timing is wall-clock**, not frame-exact: it plays on its own thread while the
  capture runs, so use it to get the game into a state, not to reproduce a frame-perfect input.
  Frame-exact replays belong in PCSX2's own input recording.
- **`--append` refuses a file with different columns.** A capture whose rows mean two different
  things is worse than two files.
- **Do not point two recorders at one emulator.** `kit record` takes the `pcsx2` lock; a
  multi-step job that already holds it sets `KIT_PCSX2_LOCKED=1`.
- **Dry-run is free.** It parses, resolves, plans and prints, and works with no `kit.toml` and no
  emulator — unresolved `@symbols` are listed instead of being fatal. Run it before every capture.
