#!/usr/bin/env python3
"""Executed verification for the packaging fix lane. Run from repo root (worktree)."""
import json
import os
import subprocess
import sys
import zipfile

REPO = os.getcwd()
failures = []


def check(name, ok, detail=""):
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail and not ok else ""))
    if not ok:
        failures.append(name)


def main():
    # 1. hooks.json: Claude Code plugin schema requires event maps nested under
    #    a top-level "hooks" key (docs: code.claude.com/docs/en/plugins-reference).
    hooks_path = os.path.join(REPO, "plugins/session-handoff/hooks/hooks.json")
    try:
        hooks = json.load(open(hooks_path))
        check("hooks-json-valid", True)
    except Exception as e:
        hooks = {}
        check("hooks-json-valid", False, str(e))
    inner = hooks.get("hooks")
    check("hooks-nested-under-hooks-key", isinstance(inner, dict),
          "top level of hooks.json must be {\"hooks\": {...event maps...}}")
    if isinstance(inner, dict):
        for evt in ("PostToolUse", "UserPromptSubmit", "SessionStart"):
            entries = inner.get(evt) or []
            ok = any("context_watch.py" in h.get("command", "") and "${CLAUDE_PLUGIN_ROOT}" in h.get("command", "")
                     for e in entries for h in e.get("hooks", []))
            check("hooks-event-%s" % evt, ok, "missing or wrong command wiring for %s" % evt)

    # 2. plugin.json must NOT reference hooks/hooks.json: Claude Code loads the
    #    standard hooks/hooks.json automatically, and an explicit manifest entry
    #    for the same file fails install with a duplicate-hooks error (observed
    #    live 2026-08-11). manifest "hooks" is only for ADDITIONAL hook files.
    pj_path = os.path.join(REPO, "plugins/session-handoff/.claude-plugin/plugin.json")
    try:
        pj = json.load(open(pj_path))
        check("plugin-json-valid", True)
    except Exception as e:
        pj = {}
        check("plugin-json-valid", False, str(e))
    hooks_field = pj.get("hooks")
    check("plugin-json-no-duplicate-hooks-ref", hooks_field is None,
          "plugin.json must omit \"hooks\" — hooks/hooks.json is auto-loaded and "
          "re-referencing it fails install; got %r" % (hooks_field,))

    # 3. Build step for the .plugin (Cowork) and .skill (chat) artifacts (SPEC 3/13).
    pkg = os.path.join(REPO, "scripts/package.sh")
    check("package-script-exists", os.path.isfile(pkg))
    if os.path.isfile(pkg):
        p = subprocess.run(["bash", pkg], cwd=REPO, capture_output=True, text=True, timeout=60)
        check("package-script-runs", p.returncode == 0,
              "rc=%d stderr=%s" % (p.returncode, p.stderr[:300]))
        plugin_art = os.path.join(REPO, "dist/session-handoff.plugin")
        skill_art = os.path.join(REPO, "dist/session-handoff-chat.skill")
        # handoff-template.md must ship in both: SKILL.md delegates the document
        # structure to it, so a package without it installs a skill that points
        # at a file the user does not have.
        for art, member_needles, name in (
                (plugin_art, ("plugin.json", "SKILL.md", "handoff-template.md"), "artifact-plugin"),
                (skill_art, ("SKILL.md", "handoff-template.md"), "artifact-skill")):
            if not os.path.isfile(art):
                check(name, False, "%s not produced" % os.path.relpath(art, REPO))
                continue
            try:
                names = zipfile.ZipFile(art).namelist()
            except Exception as e:
                check(name, False, "not a readable zip: %s" % e)
                continue
            for member_needle in member_needles:
                check("%s:%s" % (name, member_needle), any(member_needle in n for n in names),
                      "zip %s lacks %s; members=%r" % (os.path.relpath(art, REPO), member_needle, names[:10]))
        gi = open(os.path.join(REPO, ".gitignore")).read() if os.path.isfile(os.path.join(REPO, ".gitignore")) else ""
        check("dist-gitignored", "dist" in gi, ".gitignore must exclude dist/")

    # 4. Root README layout must mention the actually-shipped files.
    readme = open(os.path.join(REPO, "README.md")).read()
    for needle in ("handoff_ledger.py", "thresholds.example.json", "package.sh",
                   "handoff-template.md"):
        check("readme-mentions-%s" % needle, needle in readme)

    print()
    if failures:
        print("FAILED: %d assertion(s): %s" % (len(failures), ", ".join(failures)))
        return 1
    print("ALL PACKAGING CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
