#!/usr/bin/env python3
"""Executed verification for the skills fix lane. Run from repo root (worktree)."""
import json
import os
import re
import sys

REPO = os.getcwd()
AGENT = os.path.join(REPO, "skills/session-handoff/SKILL.md")
CHAT = os.path.join(REPO, "chat/session-handoff-chat/SKILL.md")
# The handoff document's shape lives in a bundled template beside each SKILL.md
# so it can be rewritten on its own. A "skill" is therefore its directory, not
# one file: shared-contract literals are asserted against SKILL.md + template.
AGENT_TEMPLATE = os.path.join(REPO, "skills/session-handoff/handoff-template.md")
# SKILL.md is a small pointer; the dispatcher names the one branch to load.
AGENT_SAVE = os.path.join(REPO, "skills/session-handoff/branches/save.md")
AGENT_RESUME = os.path.join(REPO, "skills/session-handoff/branches/resume.md")
POINTER_MAX_BYTES = 2750
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
    chat = open(CHAT).read()
    spec = open(SPEC).read()
    plugin_version = json.load(open(PLUGIN_JSON))["version"]

    for path, name in ((AGENT_TEMPLATE, "agent"), (CHAT_TEMPLATE, "chat")):
        check("%s-template-file-present" % name, os.path.exists(path),
              "%s is missing; SKILL.md delegates the document structure to it" % path)
    if not all(os.path.exists(p) for p in (AGENT_TEMPLATE, CHAT_TEMPLATE)):
        print()
        print("FAILED: %d assertion(s): %s" % (len(failures), ", ".join(failures)))
        return 1

    a_tpl = open(AGENT_TEMPLATE).read()
    for path in (AGENT_SAVE, AGENT_RESUME):
        check("agent-branch-present:%s" % os.path.basename(path), os.path.exists(path))
    a_save = open(AGENT_SAVE).read() if os.path.exists(AGENT_SAVE) else ""
    a_resume = open(AGENT_RESUME).read() if os.path.exists(AGENT_RESUME) else ""
    size = len(a.encode("utf-8"))
    check("agent-pointer-small", size <= POINTER_MAX_BYTES,
          "SKILL.md is %d bytes; keep the pointer at or under %d" % (size, POINTER_MAX_BYTES))
    # A pointer is still a real skill: hosts discover and route it by its
    # frontmatter, so every characteristic stays present and current.
    fm = re.match(r"\A---\n(.*?)\n---\n", a, re.DOTALL)
    front = fm.group(1) if fm else ""
    after = a[fm.end():] if fm else a
    check("agent-frontmatter-present", fm is not None, "SKILL.md must open with a --- frontmatter block")
    check("agent-frontmatter-name", re.search(r"(?m)^name:\s*session-handoff\s*$", front) is not None,
          "frontmatter must declare name: session-handoff")
    desc = re.search(r'(?m)^description:\s*"?(.+?)"?\s*$', front)
    check("agent-frontmatter-description",
          desc is not None and desc.group(1).strip() and "Do not use" in desc.group(1),
          "frontmatter description must be nonempty and keep its 'Do not use' sentence")
    triggers = re.search(r"(?m)^triggers:\s*\n((?:[ \t]+-[ \t]+\S.*\n?)+)", front + "\n")
    check("agent-frontmatter-triggers", triggers is not None,
          "frontmatter must carry a nonempty triggers list")
    for phrase in ("continue the handoff", "continue your handoff"):
        check("agent-frontmatter-trigger:%s" % phrase,
              triggers is not None and re.search(r"(?m)^[ \t]+-[ \t]+%s\s*$" % re.escape(phrase), triggers.group(1)) is not None,
              "triggers must include %r" % phrase)
    check("agent-h1-title", re.search(r"(?m)^# Session Handoff\s*$", after) is not None,
          "SKILL.md must keep the H1 '# Session Handoff' after the frontmatter")
    for needle in ("dispatch --input -", "branches/save.md", "branches/resume.md", "complete resume or prepare command"):
        check("agent-pointer-routes:%s" % needle, needle in a, "SKILL.md pointer must mention %r" % needle)
    chat_tpl = open(CHAT_TEMPLATE).read()

    # Content the next step needs travels as a line-range excerpt, not as a
    # prose pointer the receiver has to search for and rebuild.
    check("agent-template-prefers-line-range-excerpts",
          re.search(r"#L", a_tpl) is not None and "--attach" in a_tpl
          and re.search(r"\b(around|near|about|approximately)\s+lines?\b", a_tpl, re.IGNORECASE) is not None,
          "handoff-template.md must steer needed files into path#L references and flag 'around line N' prose")

    check("agent-template-requester-section",
          "## Requester" in a_tpl and "agent ID" in a_tpl and "project inbox" in a_tpl,
          "template must describe the optional requester and reply destination")
    check("agent-save-requester-guidance", "Requester" in a_save,
          "save branch must carry requester guidance even for minimal pressure saves")
    check("agent-pointer-requester-guidance", "Requester" in a,
          "skill pointer must route requester capture to the template")

    # Contract literals may live in either half of a skill directory.
    agent_skill = "\n".join((a, a_tpl, a_save, a_resume))
    chat_skill = chat + "\n" + chat_tpl

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

    # Wind-down invariant, independent of wording: §1 invokes `<ledger> save`,
    # requires `outcome: saved`, then reports to the user, then stops. A
    # handoff announced before the ledger confirms it may not be discoverable.
    wind = re.search(r"^## §1 .*", a_save, re.DOTALL | re.MULTILINE)
    check("agent-wind-down-section-present", wind is not None)
    if wind:
        sect = wind.group(0)
        save = re.search(r"<ledger>`?\s+save\b(?!\s+--template)", sect)
        saved = save and re.search(r"outcome: saved", sect[save.end():])
        after = save.end() + saved.end() if saved else len(sect)
        report = re.search(r"\b(tell|report|announce)\b[^.\n]*\buser\b", sect[after:], re.IGNORECASE)
        stop = report and re.search(r"\bstop\b", sect[after + report.end():], re.IGNORECASE)
        check("agent-wind-down-requires-published-receipt", bool(save and saved),
              "§1 must publish with `<ledger> save` and require outcome: saved")
        check("agent-wind-down-verify-precedes-report", bool(saved and report and stop),
              "§1 order must be: save -> outcome: saved -> report to user -> stop")

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
