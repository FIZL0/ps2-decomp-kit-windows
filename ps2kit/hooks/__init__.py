"""Claude Code hooks: the cheap-context layer.

These are not part of `kit`. They are small standalone scripts that Claude Code runs around an agent's tool
calls (wired in `.claude/settings.json`), and they exist because the two most expensive failure modes of a
long reverse-engineering session are both invisible from inside it:

* `guard.py` (PreToolUse) — one `cat context/decomp/game.c` is 16 MB of decompiled C in the conversation, and
  the conversation is re-read on every turn afterwards. The read is not slow, it is terminal. The guard blocks
  it and replies with the cheap alternative instead of a refusal. It also fixes `pgrep -f`/`pkill -f`, refuses
  polling loops around test runs, and raises the Bash timeout for `kit check` so tests can run in the
  foreground where the agent can see the result.
* `inbox.py` (PostToolUse + Stop) — the human is asleep or doing something else while an agent works. A ticket
  file is the only channel that does not require them to be at the keyboard at the right second.

Rules for anything added here:

1. **Never crash.** A hook that raises blocks the tool call it was inspecting, and a hook that raises on every
   payload blocks all work. Wrap everything; on an unexpected payload, exit 0 and allow.
2. **Exit 2 to block**, with the reason on stderr — that reason goes back to the agent, so it must name the
   cheap alternative, not just say no.
3. **Say the fix, not the rule.** "Use `kit fn <addr>`" beats "whole-file reads are forbidden".
4. **No kit.toml required.** Hooks run in checkouts that are not set up yet; use `cfg(False)`.
"""
