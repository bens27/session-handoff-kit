#!/usr/bin/env python3
"""Static trigger-corpus gate.

Live trigger scoring is a separate manual, model-calling step outside CI. This
verifier only checks local prompt files and skill front matter with stdlib code.
"""
import os
import re
import sys

REPO = os.getcwd()
AGENT_SKILL = os.path.join(REPO, "plugins/session-handoff/skills/session-handoff/SKILL.md")
CHAT_SKILL = os.path.join(REPO, "chat/session-handoff-chat/SKILL.md")

FILES = {
    "agent-should-trigger": os.path.join(REPO, "tests/triggers/agent/should-trigger.txt"),
    "agent-should-not-trigger": os.path.join(REPO, "tests/triggers/agent/should-not-trigger.txt"),
    "chat-should-trigger": os.path.join(REPO, "tests/triggers/chat/should-trigger.txt"),
    "chat-should-not-trigger": os.path.join(REPO, "tests/triggers/chat/should-not-trigger.txt"),
}

failures = []


def check(name, ok, detail=""):
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail and not ok else ""))
    if not ok:
        failures.append(name)


def prompt_key(prompt):
    return prompt.strip().casefold()


def load_prompts(path):
    if not os.path.exists(path):
        return []
    prompts = []
    with open(path) as f:
        for line in f:
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                prompts.append(stripped)
    return prompts


def duplicate_prompts(prompts):
    seen = set()
    duplicates = []
    for prompt in prompts:
        key = prompt_key(prompt)
        if key in seen and key not in duplicates:
            duplicates.append(key)
        seen.add(key)
    return duplicates


def read_description(path):
    with open(path) as f:
        lines = f.readlines()

    collecting = False
    block_indent = None
    parts = []
    for line in lines:
        if not collecting:
            match = re.match(r"^description:\s*(.*)$", line)
            if not match:
                continue
            collecting = True
            value = match.group(1).strip()
            if value and value not in (">", "|"):
                return value
            continue

        if re.match(r"^[A-Za-z0-9_-]+:\s*", line):
            break
        if line.startswith("---"):
            break
        if not line.strip():
            parts.append("")
            continue

        indent = len(line) - len(line.lstrip(" "))
        if block_indent is None:
            block_indent = indent
        if indent < block_indent:
            break
        parts.append(line[block_indent:].strip())

    return " ".join(part for part in parts if part).strip()


def find_negative_scope_sentence(description):
    normalized = re.sub(r"\s+", " ", description).strip()
    match = re.search(r"\bDo not use\b.*?(?:\.|$)", normalized)
    return match.group(0) if match else ""


def contains_any(prompts, needle):
    needle = needle.casefold()
    return any(needle in prompt.casefold() for prompt in prompts)


def main():
    prompts_by_name = {}
    for name, path in FILES.items():
        exists = os.path.exists(path)
        check("%s-exists" % name, exists, "%s is missing" % path)
        prompts = load_prompts(path)
        prompts_by_name[name] = prompts
        check("%s-has-at-least-8-prompts" % name, len(prompts) >= 8,
              "%s has %d prompt(s), expected at least 8" % (path, len(prompts)))
        duplicates = duplicate_prompts(prompts)
        check("%s-no-duplicate-prompts" % name, not duplicates,
              "%s duplicates: %s" % (path, ", ".join(duplicates)))

    for surface in ("agent", "chat"):
        should = set(prompt_key(p) for p in prompts_by_name["%s-should-trigger" % surface])
        should_not = set(prompt_key(p) for p in prompts_by_name["%s-should-not-trigger" % surface])
        overlap = sorted(should & should_not)
        check("%s-should-and-should-not-disjoint" % surface, not overlap,
              "%s prompt(s) appear in both files: %s" % (surface, ", ".join(overlap)))

    agent_description = read_description(AGENT_SKILL)
    agent_negative = find_negative_scope_sentence(agent_description)
    check("agent-negative-scope-sentence-present", bool(agent_negative),
          "agent description must include a sentence beginning 'Do not use'")
    agent_should_not = prompts_by_name["agent-should-not-trigger"]
    for keyword in ("summar", "commit message", "status update", "memory"):
        check("agent-negative-scope-description-has-%s" % keyword,
              keyword in agent_negative.casefold(),
              "agent negative-scope sentence is missing keyword '%s'" % keyword)
        check("agent-should-not-has-%s" % keyword,
              contains_any(agent_should_not, keyword),
              "%s is missing a prompt containing '%s'" % (FILES["agent-should-not-trigger"], keyword))

    chat_description = read_description(CHAT_SKILL)
    chat_negative = find_negative_scope_sentence(chat_description)
    if not chat_negative:
        print("INFO chat description has no negative-scope sentence; should-not set still enforced")
    chat_should_not = prompts_by_name["chat-should-not-trigger"]
    for keyword in ("summar", "commit message", "memory"):
        check("chat-should-not-has-%s" % keyword,
              contains_any(chat_should_not, keyword),
              "%s is missing a prompt containing '%s'" % (FILES["chat-should-not-trigger"], keyword))

    agent_should = prompts_by_name["agent-should-trigger"]
    check("agent-should-trigger-has-context-watch",
          contains_any(agent_should, "[context-watch]"),
          "%s must contain a [context-watch] prompt" % FILES["agent-should-trigger"])
    check("agent-should-trigger-has-resume",
          contains_any(agent_should, "resume"),
          "%s must contain a prompt with 'resume'" % FILES["agent-should-trigger"])

    chat_should = prompts_by_name["chat-should-trigger"]
    check("chat-should-trigger-has-handoff-md",
          contains_any(chat_should, "HANDOFF.md"),
          "%s must contain a HANDOFF.md prompt" % FILES["chat-should-trigger"])
    check("chat-should-trigger-has-left-off-or-pick-up",
          contains_any(chat_should, "left off") or contains_any(chat_should, "pick up"),
          "%s must contain 'left off' or 'pick up'" % FILES["chat-should-trigger"])
    check("agent-should-not-has-worktree",
          contains_any(agent_should_not, "worktree"),
          "%s must contain a worktree ownership-handoff trap" % FILES["agent-should-not-trigger"])

    print()
    if failures:
        print("FAILED: %d assertion(s): %s" % (len(failures), ", ".join(failures)))
        return 1
    print("ALL TRIGGER CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
