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
# The handoff document's shape lives in a bundled template beside each SKILL.md
# so it can be rewritten on its own. A "skill" is therefore its directory, not
# one file: shared-contract literals are asserted against SKILL.md + template.
AGENT_TEMPLATE = os.path.join(REPO, "plugins/session-handoff/skills/session-handoff/handoff-template.md")
CODEX_TEMPLATE = os.path.join(REPO, "codex/skills/session-handoff/handoff-template.md")
CHAT_TEMPLATE = os.path.join(REPO, "chat/session-handoff-chat/handoff-template.md")
PLUGIN_JSON = os.path.join(REPO, "plugins/session-handoff/.claude-plugin/plugin.json")
SPEC = os.path.join(REPO, "SPEC.md")

failures = []


def check(name, ok, detail=""):
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail and not ok else ""))
    if not ok:
        failures.append(name)


def shared_contract_literals(spec):
    heading = re.search(r"^### .*Shared contract.*$", spec, re.IGNORECASE | re.MULTILINE)
    if not heading:
        return None
    section = spec[heading.end():]
    next_heading = re.search(r"^##\s+", section, re.MULTILINE)
    if next_heading:
        section = section[:next_heading.start()]
    label = re.search(r"^\*\*Both skills must contain\*\*\s*$", section, re.MULTILINE)
    if not label:
        return []
    must_contain = section[label.end():]
    next_label = re.search(r"^\*\*.*\*\*\s*$", must_contain, re.MULTILINE)
    if next_label:
        must_contain = must_contain[:next_label.start()]
    return re.findall(r"(?m)^-\s+`([^`]+)`\s+—", must_contain)


def main():
    a = open(AGENT).read()
    c = open(CODEX).read()
    chat = open(CHAT).read()
    spec = open(SPEC).read()
    plugin_version = json.load(open(PLUGIN_JSON))["version"]

    for path, name in ((AGENT_TEMPLATE, "agent"), (CHAT_TEMPLATE, "chat")):
        check("%s-template-file-present" % name, os.path.exists(path),
              "%s is missing; SKILL.md delegates the document structure to it" % path)
    if not all(os.path.exists(p) for p in (AGENT_TEMPLATE, CODEX_TEMPLATE, CHAT_TEMPLATE)):
        print()
        print("FAILED: %d assertion(s): %s" % (len(failures), ", ".join(failures)))
        return 1

    a_tpl = open(AGENT_TEMPLATE).read()
    c_tpl = open(CODEX_TEMPLATE).read()
    chat_tpl = open(CHAT_TEMPLATE).read()

    # Contract literals may live in either half of a skill directory.
    agent_skill = a + "\n" + a_tpl
    chat_skill = chat + "\n" + chat_tpl

    check("agent-copies-identical", a == c)
    check("agent-template-copies-identical", a_tpl == c_tpl,
          "the codex mirror of handoff-template.md must be byte-identical")
    for text, tpl_name, name in ((a, "handoff-template.md", "agent"),
                                 (chat, "handoff-template.md", "chat")):
        check("%s-skill-references-template" % name, tpl_name in text,
              "SKILL.md must point at %s, or the template is orphaned" % tpl_name)
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

    # Wind-down must prove the handoff is discoverable before reporting done:
    # the §1 protocol runs `resolve` and checks `authoritative:` is the new
    # file. A handoff the announcer cannot find is the failure mode the
    # both-locations ledger fix (0.6.1) existed to close.
    wind = re.search(r"^## §1 .*?(?=^## §2)", a, re.DOTALL | re.MULTILINE)
    check("agent-wind-down-section-present", wind is not None)
    if wind:
        sect = wind.group(0)
        verify = re.search(r"resolve", sect)
        report = re.search(r"Tell the user the handoff is complete", sect)
        stop = re.search(r"\bStop\b", sect)
        check("agent-wind-down-verifies-with-resolve",
              verify is not None and "authoritative" in sect,
              "§1 must run handoff_ledger.py resolve and check the authoritative: line")
        check("agent-wind-down-verify-precedes-report",
              verify is not None and report is not None and stop is not None
              and verify.start() < report.start() < stop.start(),
              "§1 order must be: write -> verify (resolve) -> tell user -> stop")

    # The four-step order should all be present in the resume flow
    for needle, name in (("SESSION HANDOFF", "chat-past-chat-search-marker",),):
        check(name, needle in chat, "chat skill must search past chats for the literal marker")

    literals = shared_contract_literals(spec)
    check("spec-shared-contract-section-present", literals is not None,
          "SPEC.md must contain a Shared contract subsection")
    if literals is not None:
        check("spec-shared-contract-literals-parsed", len(literals) >= 8,
              "expected at least 8 shared-contract literals parsed from SPEC.md")
        for literal in literals:
            for text, path, skill_name in ((agent_skill, AGENT, "agent"),
                                           (chat_skill, CHAT, "chat")):
                ok = re.search(re.escape(literal), text, re.IGNORECASE) is not None
                check("shared-contract-%s:%s" % (skill_name, literal), ok,
                      "missing literal %r in %s or its handoff-template.md"
                      % (literal, path))

    chat_version = re.search(r'version:\s*"([^"]+)"', chat)
    check("chat-version-present", chat_version is not None,
          "chat SKILL.md must declare metadata version")
    if chat_version:
        declared = "`session-handoff-chat` skill %s" % chat_version.group(1)
        check("spec-header-chat-version-matches-skill", declared in spec,
              "SPEC.md header must declare %s" % declared)

    print()
    if failures:
        print("FAILED: %d assertion(s): %s" % (len(failures), ", ".join(failures)))
        return 1
    print("ALL SKILLS CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
