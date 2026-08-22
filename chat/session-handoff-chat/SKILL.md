---
name: session-handoff-chat
description: >
  This skill should be used when the user says "hand off", "handoff", "wrap up
  this chat", "wrap up the session", "make a handoff doc", "save our progress",
  "park this work", or "continue this in a new chat", mentions running out of
  context or hitting conversation length limits, or wants to carry the current
  work into another conversation or surface. It should ALSO be used on the
  resume side, in a fresh conversation, when the user says "resume from the
  handoff", "pick up where we left off", "what's still open", "any open
  handoffs", or asks to continue named parked work, or attaches or pastes a
  HANDOFF.md — and whenever a memory file listing open handoffs exists and the
  user's request plausibly relates to one of them.
metadata:
  version: "0.4.2"
---

# Session Handoff (chat)

Carry working state from one chat into the next, with an open/resumed ledger
so new conversations can surface untransferred work without the user having to
remember it exists. Chat has no lifecycle hooks and no token counter, so
production triggers conversationally — offer a handoff proactively when a long
working session shows strain, but never claim to know exact context usage.

## The ledger

All chat handoffs live in ONE persistent memory file — a ledger — when a
memory tool is available. Suggested name: `open-handoffs`. Each handoff is a
section:

```markdown
## <topic-slug> — Status: open — <date>
<the handoff content, using the template below>
```

Resumed handoffs stay in the file with `Status: resumed <date>` until the user
asks to clear them.

**The file's one-line description is the announcement channel.** Memory file
descriptions are visible at the start of every new conversation, so keep it in
exactly this shape and update it on every change:

> Open handoffs awaiting resume: <topic-1>, <topic-2>. Check when the user
> mentions this work or asks what's open.

When nothing is open, set it to "No open handoffs." This is what makes new
chats aware of untransferred work with zero setup. Full automation option: the
user can add a preference in their settings such as "At the start of each
conversation, if my open-handoffs file lists open handoffs, mention them in
one line before answering" — with that, every new chat opens with the
announcement unprompted, the chat equivalent of the plugin's SessionStart hook.

## Producing a handoff

The shape of the handoff — its header line, section set, and length rule —
lives in `handoff-template.md`, bundled with this skill. Read it and build the
content to that template.

It is a separate file so the structure of a handoff can be experimented with on
its own: rewrite `handoff-template.md` and nothing in this file changes.

Then persist through every channel available, in this order:

1. **Memory ledger (primary).** Upsert the topic's section with
   `Status: open`, and update the file description to enumerate all open
   topics. Tell the user: in any new chat, "resume <topic>" or "what's open"
   brings it back.
2. **The conversation itself (always).** Output the handoff block verbatim,
   headed by the literal `SESSION HANDOFF — <topic-slug> — <date>` line, so
   past-chat search can find it from a future conversation even with memory
   off.
3. **A downloadable file (portable copy).** If file creation is available,
   also write `HANDOFF.md` — the copy that crosses surfaces. In a Claude Code
   or Cowork project folder, save it as `.handoffs/<YYYYMMDD-HHMM>-<topic-slug>.md`
   (the timestamp is the handoff's ending date/time) with `status: open` and a
   one-line `description:` in the front matter so the plugin's session-start
   announcer counts it and can summarize it.
4. **A note-capture tool, if connected** (for example an Open Brain capture
   tool): store a three-to-five sentence summary.

Never claim a handoff was saved to a channel that was not actually available.

## Resuming in a fresh chat

When the user asks to resume, asks what's open, or their request matches an
open topic:

1. Check attached or pasted handoff content first.
2. If there is no attached or pasted handoff content, read the ledger.
   **Multiple open handoffs and no topic named**: list them
   in one line each (topic, stored date, first next step) and ask which to resume,
   with "none" as an option. **One open**: confirm in a sentence and proceed.
   **A topic named**: go straight to it.
3. No ledger or no match: search past chats for `SESSION HANDOFF <topic>`. If
   nothing surfaces, ask for the file or a paste — do not reconstruct state
   from guesswork.
4. Read the entire handoff before any other action, restate the objective and
   first next step, then continue from "Next steps".
5. **Mark it transferred**: flip that section to `Status: resumed <date>` and
   remove the topic from the file description. This is what stops future
   chats from re-announcing it. Do not mark resumed merely for listing it.
6. If the user's opening request is unrelated to any open handoff, mention
   open work in at most one sentence and do their task; the ledger stays as
   it is.

Two settings caveats, mentioned only when relevant: past-chat retrieval
requires the "Search and reference past chats" setting, and chats inside a
Project only search that Project — memory and files cross that boundary.

## Honest limits

There is no automatic token-threshold trigger in chat; the deterministic
version lives in the Claude Code and Codex halves of this kit. Continuity here
rides on memory and past-chat search — both real, both user-toggleable. Never
invent context-usage numbers.
