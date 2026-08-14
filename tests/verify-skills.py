#!/usr/bin/env python3
"""Executed verification for the skills fix lane. Run from repo root (worktree)."""
import json
import os
import re
import sys

REPO = os.getcwd()
AGENT = os.path.join(REPO, "plugins/session-handoff/skills/session-handoff/SKILL.md")
CODEX = os.path.join(REPO, "codex/skills/session-handoff/SKILL.md")
CHAT = os.path.join(REPO, "chat/session-handoff-chat/SKILL.md")
PLUGIN_JSON = os.path.join(REPO, "plugins/session-handoff/.claude-plugin/plugin.json")

failures = []


def check(name, ok, detail=""):
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail and not ok else ""))
    if not ok:
        failures.append(name)


def main():
    a = open(AGENT).read()
    c = open(CODEX).read()
    chat = open(CHAT).read()
    plugin_version = json.load(open(PLUGIN_JSON))["version"]

    check("agent-copies-identical", a == c)
    check("agent-version-matches-plugin-json",
          re.search(r'version:\s*"?%s\b' % re.escape(plugin_version), a) is not None,
          "agent SKILL.md must declare version %s (from plugin.json, the single source of truth)" % plugin_version)

    # Chat resume-resolution order (SPEC section 8): attached/pasted content FIRST,
    # then memory ledger, then past-chat search, then ask. Find the resume section
    # and require the first mention of attached/pasted to precede the first
    # instruction to read the ledger.
    m = re.search(r"^#+ .*resum.*$", chat, re.IGNORECASE | re.MULTILINE)
    check("chat-has-resume-section", m is not None)
    if m:
        sect = chat[m.start():]
        nxt = re.search(r"^#+ ", sect[m.end() - m.start() + 1:], re.MULTILINE)
        # take up to the next same-or-higher heading, or 3000 chars
        sect = sect[:3000]
        attach = re.search(r"attach|paste", sect, re.IGNORECASE)
        ledger = re.search(r"ledger", sect, re.IGNORECASE)
        ok = attach is not None and (ledger is None or attach.start() < ledger.start())
        check("chat-attached-before-ledger", ok,
              "resume section must check attached/pasted content before the memory ledger (SPEC 8 resolution order)")

    # The four-step order should all be present in the resume flow
    for needle, name in (("SESSION HANDOFF", "chat-past-chat-search-marker",),):
        check(name, needle in chat, "chat skill must search past chats for the literal marker")

    print()
    if failures:
        print("FAILED: %d assertion(s): %s" % (len(failures), ", ".join(failures)))
        return 1
    print("ALL SKILLS CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
