# Keeping a long project affordable

This page is about one resource: the attention of whoever is doing the work. If that is an AI agent, the
resource is literal and measurable — every turn re-reads the entire conversation, so anything you read
once you pay for on every turn for the rest of the session. If it is a human, the same measures pay off
as a different currency: you stop losing afternoons to scrolling.

A reverse-engineering project is unusually hostile to this. The raw material is enormous — a
decompilation dump of a PS2 game is 10–30 MB, a RAM image is 32 MB, a capture is thousands of rows, a
build log is thousands of lines — and almost all of it is irrelevant to whatever you are doing right now.
Nearly every measure in this kit is about getting the three lines you need out of the ten megabytes
without the ten megabytes passing through anyone's head.

---

## The core idea: index, don't read

The dumps are not documents. They are databases that happen to be text, and every one has a query tool:

| Instead of | Do |
|---|---|
| opening the decompilation dump | `kit fn <addr>` — one function |
| scrolling for callers | `kit xref <addr>` — one line each |
| hunting for similar code | `kit xref <addr> --similar` |
| reading the disassembly file | `kit dis <addr>` |
| dumping a RAM image | `kit state extract --addr --len` |
| reading the whole task list | `kit plan next`, `kit plan brief <ID>` |
| reading a build log | `kit check` — one line of verdict |

Each of those turns a megabyte into a screenful. None of them is clever; the discipline is in always
having one, so that the expensive option never looks like the easy one.

---

## Capping output in PowerShell

The Unix idioms this discipline is usually written in — `| head`, `| tail`, `| grep` — do not exist in
`cmd`, and that matters here more than anywhere else in these pages: without a cap, the answer to "what
is in this file" is the whole file, and the whole file is then re-read on every later turn. Use
PowerShell 7, and these five:

| Instead of | PowerShell |
|---|---|
| `cmd \| head -40` | `cmd \| Select-Object -First 40` |
| `cmd \| tail -40` | `cmd \| Select-Object -Last 40` |
| `grep pat file` | `Select-String pat file` |
| `grep -c pat file` | `(Select-String pat file).Count` |
| `sed -n '200,260p' file` | `Get-Content file -TotalCount 260 \| Select-Object -Skip 199` |

Two things to know about them. `Select-Object -First` stops the upstream command, the way `head` does, so
a capped read of a 20 MB dump costs the first 40 lines and not the 20 MB. `Select-Object -Last`, and
`Sort-Object`, cannot: they must see everything before they can answer, so they read the whole file into
memory even though they print ten lines — fine for a build log, not for a dump. And prefer
`Get-Content -ReadCount` or `-TotalCount` over a bare `Get-Content` on anything large, since the plain
form emits one object per line.

PowerShell's own aliases make the Unix spellings *almost* work: `cat`, `ls` and `sort` are real aliases,
but `head`, `tail` and `grep` are not, and the near-miss is worth knowing about because the failure of
`cmd | head -40` is a cap that silently did not happen.

---

## The guard hook

Discipline is not enough, because the expensive mistake is one keystroke away and looks exactly like the
cheap one. So the kit ships a `PreToolUse` hook (`ps2kit/hooks/guard.py`, wired up in
`.claude/settings.json`) that blocks the mistakes an agent actually makes and, crucially, **names the
cheap alternative in the refusal**:

- an unbounded read of a decompilation or RAM dump → blocked, with the `kit fn` / `kit xref` /
  `kit data` command that answers the question instead
- a process search or kill that matches on command line or window title → rewritten to an image-name
  match, because such a pattern matches the searching shell's own command line too (`taskkill /FI
  "WINDOWTITLE eq foo"` can kill the console that ran it, exactly as `pkill -f foo` does on Linux)
- a polling loop wrapped around a test run → refused; use the blocking command and a long timeout
- a long-running command invoked without a timeout → given one, so it fails rather than hanging the run

One Windows detail, in case a rewritten command looks odd: the hook names the interpreter running it by
full path rather than `python3`, because the `python3` on a Windows PATH is usually the Microsoft Store
stub, and a hook that rewrote a command to that would replace an expensive read with a Store window.

A refusal that says only "no" gets worked around in the next tool call, usually by something worse. A
refusal that hands over the right command is a lesson that costs nothing. That is the whole design rule
for the hooks: never block without redirecting.

---

## Output discipline in every command

Rule 3 in `docs/INTERNALS.md`: nothing ever floods the caller. In practice:

- Success prints a line or two. Not a transcript of what happened.
- Long output is capped — truncated by line and by line length — and the path to the full copy is printed
  instead. `kit check` writes the full log to the private directory and shows you sixty capped lines.
- Lists are summarised. `kit plan` prints sections and counts, not tasks.
- Errors are specific and name the fix. "No frame counter configured — run `kit probe vsync --save`" is
  one line that ends the problem; "KeyError: 'vsync'" is a detour.
- `kit check` distinguishes **the build broke** from **a test failed**, because that is the only
  distinction you need before deciding what to do next, and a raw test command makes you read a thousand
  lines to find out which it was.

---

## One call instead of eight

Two commands exist purely to collapse a predictable sequence:

**`kit plan brief <ID>`** — a task's line, its own plan file, its journal (newest entry, capped), and
every file it names with its length and recent commits. That is what you would otherwise gather with
eight separate reads, of which six would pull in more than you needed.

**`kit done <ID> "<msg>"`** — tests, lint, tick the task, commit. One call, so wrap-up cannot be
half-finished. The state where the code is committed but the task is still open costs the next session
more than the commit saved.

The general shape: wherever a sequence of steps is always the same, make it one command. Not for
convenience — so that the steps are *not in the transcript*.

---

## Tasks as a tool, not a file

`plan/TODO.md` is markdown so a human can skim it, but nothing should read it. `kit plan` is the
interface, for one reason: a file you read is a file you read all of, permanently. The list is a few
hundred lines and grows; `kit plan next` is three lines and never grows.

The same logic applies to the journal (`kit journal` fetches the entry you want, capped) and to
`research/oracle.txt` (`kit oracle names`, `kit oracle addr <name>`).

---

## The journal, and why it is the real memory

Each session starts fresh. The journal is how the project remembers anything: what the original does, in
plain words; which addresses, in which overlay; what was measured; and what could not be checked.

The mandatory last section — `## Not verified / not 1:1`, with a reason per item or the word `none` — is
the highest-value line of process here. It exists because the failure mode of a long port is not bad
code, it is *silent* code: forty small unverified corners that nobody wrote down, each invisible, which
together make the game subtly wrong in ways that will never be traced. Written down, the same forty
corners are a work plan.

Captures, screenshots and dumps stay in the private directory. The journal is committed and read, so it
holds prose and numbers, not artefacts.

---

## Stop rather than loop

"If you get stuck, move on" (`AGENT.md`) is a context measure as much as a morale one. Twenty minutes of
retrying one obstacle is both no progress and a transcript full of failed attempts that every later turn
re-reads. Writing down what was tried, blocking the task with a reason and taking the next one keeps both
the project and the session moving.

The same reasoning is behind: bail out of a frame wait when the counter stops rather than spinning; fail
a capture loudly rather than writing a plausible file; refuse to commit when tests fail rather than
leaving it to be noticed later.

---

## What this cost to learn

The repository this kit was generalised from arrived at these measures the expensive way. The specific
lessons, so that you can skip them:

- **A single `cat` of a decompilation dump can end a working session.** This is where the guard hook came
  from. Once this has happened, "be careful" stops sounding like a sufficient policy.
- **One-off scripts breed.** That project accumulated around forty-five near-identical capture scripts
  and well over a hundred single-use probes, each written because adapting an existing one looked harder
  than starting again. Nobody could tell which were still correct. Hence `kit record` with a
  declarative watch spec: new captures are a 10-line TOML file, not a new program.
- **Build directories grow until the disk is full.** Tens of gigabytes per worktree, hundreds across a
  parallel run. Hence the cap and the automatic clean in `kit check`.
- **A plausible-looking broken capture costs more than an obviously broken one.** Hence `CAPTURE FAILED`
  being unmissable and the run exiting non-zero.
- **Addresses without the overlay name are useless three weeks later.** Hence `[symbols]`,
  `research/oracle.txt`, and `-p/--program` on every static tool.
- **Verified and unverified work look identical in a diff.** Hence the mandatory journal section.

`RETROSPECTIVE.md` has more of this, including the parts of the original design that turned out to be
mistakes.
