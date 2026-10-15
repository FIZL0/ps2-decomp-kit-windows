#!/usr/bin/env python3
"""PostToolUse + Stop hook: a ticket inbox, so the human can steer a working agent without interrupting it.

An unattended run is hours long and the human is not watching it. They still notice things — "the shadow is
on the wrong side", "don't bother with the EU disc", "check the serve timing while you're in there" — and the
only channel that does not need them to be at the keyboard at the exact right second is a file:

    context/inbox.md          - [ ] a ticket, one line, written whenever
    context/notes/inbox.seen  tickets already handed over (so each goes in once)

As PostToolUse, every new ticket is handed to the agent once, as additionalContext on its next tool result.
As Stop, an agent about to end its turn with an open ticket is told to keep going instead. The agent does the
work, ticks the line `- [x]` and writes its reply indented under it, which is also how the human reads the
answer later.

`stop_hook_active` is honoured: a ticket nobody can act on must not trap a session in an endless stop loop.
That guard is the difference between a useful inbox and an agent that can never finish.

Standalone: `py -3 ps2kit/hooks/inbox.py` with the hook JSON on stdin. Never crashes — on any unexpected
payload it exits 0 and says nothing.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

ASK = ('New ticket(s) from the human in {inbox}. Handle them (now if quick or urgent, otherwise after the '
       'current step), then mark each `- [x]` and put your reply indented under it:\n{tickets}')
KEEP = ('Open tickets in {inbox} — deal with them before you stop (tick each `- [x]` with your reply '
        'indented under it). If one needs the human, say so under it and add it to plan/HUMAN.md:\n{tickets}')


def _paths() -> tuple[Path, Path]:
    # KIT_INBOX / KIT_INBOX_SEEN override both, which is how tests drive the hook without a kit.toml and how
    # you can point it at a shared file (two checkouts, one ticket queue).
    if box := os.environ.get('KIT_INBOX'):
        return Path(box), Path(os.environ.get('KIT_INBOX_SEEN') or (box + '.seen'))
    try:
        from ps2kit.config import cfg
        c = cfg(False)
        base = Path(str(c.ctx('inbox.md')))
        return base, Path(str(c.ctx('notes/inbox.seen')))
    except Exception:
        root = Path(__file__).resolve().parents[2]
        return root / 'context' / 'inbox.md', root / 'context' / 'notes' / 'inbox.seen'


def _read(p: Path) -> list[str]:
    try:
        return p.read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return []


def main() -> int:
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            return 0
    except Exception:
        return 0
    try:
        inbox, seen_file = _paths()
        tickets = [l.strip() for l in _read(inbox) if l.lstrip().startswith('- [ ]')]
        rel = os.path.relpath(inbox, Path(__file__).resolve().parents[2])

        if event.get('hook_event_name') == 'Stop':
            if tickets and not event.get('stop_hook_active'):
                print(json.dumps({'decision': 'block',
                                  'reason': KEEP.format(inbox=rel, tickets='\n'.join(tickets))}))
            return 0

        seen = set(_read(seen_file))
        new = [t for t in tickets if t not in seen]
        if new:
            seen_file.parent.mkdir(parents=True, exist_ok=True)
            with open(seen_file, 'a', encoding='utf-8') as f:
                f.write(''.join(t + '\n' for t in new))
            print(json.dumps({'hookSpecificOutput': {
                'hookEventName': event.get('hook_event_name', 'PostToolUse'),
                'additionalContext': ASK.format(inbox=rel, tickets='\n'.join(new))}}))
    except Exception:
        return 0  # a hook that throws blocks the tool call it was watching
    return 0


if __name__ == '__main__':
    sys.exit(main())
