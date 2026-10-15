# The PlayStation 2, for someone porting a game off it

Only the parts that change what you write. If you want the full hardware story, the PS2 development
documentation and PCSX2's source are the references; this is the short list of things that will make your
port wrong if you do not know them.

---

## The chips, and which ones you have to care about

| Chip | What it did | Does your port care? |
|---|---|---|
| **EE (R5900)** | main CPU, 32-bit MIPS III-ish at 294 MHz, with a non-standard FPU | **Yes.** This is the game logic. |
| **COP1** | the EE's single-precision FPU | **Yes, enormously.** See below. |
| **VU0** | vector unit, usable as a coprocessor from EE code ("macro mode") | **Yes** — most 3D math goes through it. |
| **VU1** | vector unit driving the graphics pipeline | Rarely. Transform and lighting; by-eye territory. |
| **GS** | the rasteriser | No. Render with your own engine. |
| **IOP (R3000)** | I/O processor: controllers, memory card, CD/DVD, sound | Almost never. Different CPU, separate decompile. |
| **SPU2** | sound | Only if you are reproducing audio exactly, which you probably are not. |

Practically: you port EE code, you reproduce COP1 and VU0 arithmetic exactly, you reimplement the
renderer, and you ignore the rest.

---

## The EE's FPU is not IEEE 754

This is the single most important page in this document. The EE's single-precision unit looks like an
IEEE float unit and is not one, and a port that uses host `f32` will be *nearly* right — which means it
diverges visibly after a few hundred frames of feedback.

**It rounds toward zero.** Add, subtract and multiply truncate the result. Not round-to-nearest. Not
round-half-even. Chop.

**There are no denormals.** Inputs that are denormal are treated as zero (sign preserved); results that
would be denormal become zero. A game can and does rely on this.

**There are no infinities and no NaNs.** Overflow saturates to the largest representable magnitude with
the right sign. There is no "not a number" to propagate, so a calculation that would produce a NaN on
your machine produces a very large number on the PS2, and the game carries on with it.

**Addition truncates the smaller operand instead of using the full width.** This is the one nobody
expects. Before adding, the smaller operand's mantissa is shifted right to align the exponents — but the
hardware keeps only one guard bit, so bits beyond (exponent difference − 1) are simply dropped. When the
exponents differ by 25 or more, the smaller operand contributes nothing at all except, in some cases, its
sign. Two consequences you will meet:

- `big + small == big` much earlier than IEEE would have it.
- Addition is not associative in the same places it is not associative on your host, so **you must keep
  the original's order of operations exactly**. Reordering a sum for clarity changes the number.

**Divide and square root round differently** from add and multiply — they are closer to round-to-nearest.
Don't assume one rounding rule across all operations.

**`madd.s` is not a multiply followed by an add, and not a fused multiply-add either.** The product is
computed and *truncated*, then added to the accumulator using the aligned-add rule above. It sits exactly
between the two things you would guess, which is why implementing it as either one produces a port that
is right most of the time. Games use `madd.s` constantly — every dot product, every matrix multiply.

`ps2-float` (our library, FIZL0/ps2-float) implements all of this. Use its types in the simulation and turn on the lint that
forbids host float arithmetic in those modules. The lint is not pedantry: a single stray `a * b + c` in
one inner loop is a bug that takes days to find, because everything looks correct and the numbers are
merely slightly different.

---

## VU0 macro mode

EE code can issue VU0 instructions inline, giving it four-wide float ops with their own quirks:

- The same chop-rounding and no-denormal behaviour as COP1, but **with its own saturation and flag
  rules**, so results are not always identical to the equivalent COP1 sequence.
- Operations can be masked to any subset of the x/y/z/w fields. A decompiler shows this badly, or not at
  all; check the disassembly.
- The reciprocal and reciprocal-square-root instructions are **lookup-table approximations**, accurate to
  far fewer bits than a real divide. Any port of a normalise, a projection or a distance that uses them
  must use the same approximation, not a real division — the error is big enough to see.
- There is a four-entry accumulator-ish register set and instructions that read it implicitly; sequences
  that look independent in C are not.

`ps2-float` covers the macro-mode ops games actually use. If you hit one it does not have, adding
it is a small, permanent job: in its repository, with a test and a tagged release, then bump the tag here.

---

## The game's own maths library

A PS2 game ships its own `sinf`, `cosf`, `atan2f`, `sqrtf`, `powf` and friends, compiled into the
binary — usually the toolchain's library, sometimes hand-written, often a table-plus-polynomial. They do
**not** agree with your host's libm, which will disagree with itself between machines and compiler
versions.

So: find them in the decompile, port them, and use yours everywhere in the simulation. This feels like
excessive devotion until the first time a trajectory differs in the last two bits of the angle and then
by a metre after three seconds of flight.

The oracle makes this cheap to get right: these functions are pure, so 50,000 random inputs and a
bit-exact comparison settles it in an afternoon, permanently.

---

## Overlays

Most PS2 games do not fit in 32 MB of RAM all at once, so they are built as a small boot ELF plus several
**overlays**: code blobs loaded over a fixed address when the game changes mode.

What this means for you:

- The boot ELF often contains almost none of the interesting code. If your decompilation looks far too
  small, this is why.
- **An overlay disassembled at the wrong address is unreadable.** Every absolute reference — every
  function pointer, every global, every jump table — points at nonsense. The fix is to build a synthetic
  ELF that places the blob at its real load address, which is what `kit elf` does.
- Two overlays can occupy the same address. Addresses are only meaningful together with which overlay was
  loaded, so every tool here takes `-p/--program`, and so does your note-taking: write the overlay name
  next to every address in the journal.

`kit elf --scan` guesses load addresses by looking at what a blob's own internal references imply, which
turns an afternoon of trial and error into a command. It is a guess; verify by checking that strings and
jump tables land where they should.

---

## Memory map, enough of it to read code

| Range | What |
|---|---|
| `0x0000_0000`–`0x01ff_ffff` | main RAM, 32 MB. Almost everything you look at. |
| `0x7000_0000`–`0x7000_3fff` | the scratchpad: 16 KB of fast on-chip RAM. Hot loops stage data here. |
| `0x1000_0000`+ | hardware registers: timers, DMA, GIF, VIF, interrupt control. |
| `0x1100_0000`+ | VU micro memory and VU data memory. |
| `0x1200_0000`+ | GS privileged registers. |

Addresses are often written with `0x2000_0000` or `0xa000_0000` added — those are the uncached and
uncached-accelerated mirrors of the same RAM. Mask the top bits off before comparing addresses; the same
global appears at several of them in the same function.

Scratchpad use is worth recognising early: a function that DMAs a struct into `0x7000_xxxx`, works on it
there and DMAs it back reads very strangely until you know what you are looking at.

---

## Timing

Games are written against the display: 59.94 Hz on NTSC, 50 Hz on PAL, and the logic usually runs once
per field or once per two fields. A PAL conversion often changes gameplay speed, physics constants or
both — so **pin down which region's disc you are working from and never mix captures between regions**.
The kit checks the disc serial against `kit.toml` on every connection for exactly this reason.

Inside a frame, the order is roughly: read controllers → run game logic → build display lists → kick the
DMA to VU1/GS → wait for the next vertical blank. Anything you port that reads input or advances state
has to happen in the same place in that order, because the one-frame offsets are visible and, worse,
compound through feedback loops.

Find the frame counter early (`kit probe vsync --save`). Every capture is timed against it, and
"is this counter still moving?" is the cheapest health check you have on a running game.

---

## What trips people up, in order of how much time it costs

1. Using host floats in the simulation. Everything works; nothing matches.
2. Implementing `madd.s` as multiply-then-add. Subtly wrong, in every dot product.
3. Reordering arithmetic for readability. The order is part of the behaviour.
4. Using the host's `sinf`. Diverges in the last bits, then by metres.
5. Reading an overlay at the wrong address and concluding the code is garbage.
6. Mixing a PAL capture with an NTSC port, or two revisions of the same game.
7. Trusting the decompiler's C for float code without reading the instructions.
8. Treating the reciprocal approximations as division.
