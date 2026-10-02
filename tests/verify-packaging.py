#!/usr/bin/env python3
"""Executed verification for the packaging fix lane. Run from repo root (worktree)."""
import json
import os
import subprocess
import sys
import tempfile
import zipfile

REPO = os.getcwd()
failures = []


def check(name, ok, detail=""):
    print("%s %s%s" % ("PASS" if ok else "FAIL", name, (" — " + detail) if detail and not ok else ""))
    if not ok:
        failures.append(name)


def check_installer(skill_root=None):
    """install.py against scratch config dirs: preserves foreign hooks, is
    idempotent, keeps Codex hook groups independent, and uninstall restores."""
    import tempfile
    tmp = tempfile.mkdtemp(prefix="install-verify-")
    env = dict(os.environ, CLAUDE_CONFIG_DIR=os.path.join(tmp, "claude"),
               CODEX_HOME=os.path.join(tmp, "codex"))
    os.makedirs(env["CLAUDE_CONFIG_DIR"]); os.makedirs(env["CODEX_HOME"])
    claude_p = os.path.join(env["CLAUDE_CONFIG_DIR"], "settings.json")
    codex_p = os.path.join(env["CODEX_HOME"], "hooks.json")
    foreign = {"type": "command", "command": "echo other"}
    claude_before = {"model": "x", "hooks": {"Stop": [{"hooks": [foreign]}], "StopFailure": []}}
    codex_before = {"hooks": {"SessionStart": [{"hooks": [dict(foreign)]}]}}
    json.dump(claude_before, open(claude_p, "w"))
    json.dump(codex_before, open(codex_p, "w"))
    skill_root = skill_root or os.path.join(REPO, "skills/session-handoff")
    inst = os.path.join(skill_root, "install.py")
    watcher = os.path.join(skill_root, "hooks/context_watch.py")

    def run(*args):
        p = subprocess.run([sys.executable, inst] + list(args), env=env,
                           capture_output=True, text=True, timeout=30)
        check("install%s-exit0" % "".join(args), p.returncode == 0, p.stderr[:300])

    run(); first = (open(claude_p).read(), open(codex_p).read())
    run(); second = (open(claude_p).read(), open(codex_p).read())
    check("install-idempotent", first == second)
    c, x = json.loads(second[0]), json.loads(second[1])
    cmds = [h["command"] for g in c["hooks"].values() for e in g for h in e["hooks"]]
    check("install-claude-all-events", all(
        any(watcher in h["command"] for e in c["hooks"].get(ev, []) for h in e["hooks"])
        for ev in ("PostToolUse", "UserPromptSubmit", "SessionStart", "Stop")), repr(cmds))
    check("install-claude-keeps-foreign", "echo other" in cmds and c.get("model") == "x")
    ss = x["hooks"]["SessionStart"]
    check("install-codex-independent-groups",
          len(ss) == 2 and ss[0] == codex_before["hooks"]["SessionStart"][0]
          and watcher in ss[1]["hooks"][0]["command"], repr(ss))
    run("--uninstall")
    check("uninstall-restores-claude", json.load(open(claude_p)) == claude_before)
    check("uninstall-restores-codex", json.load(open(codex_p))["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "echo other",
          open(codex_p).read()[:300])


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
            ok = any("${CLAUDE_PLUGIN_ROOT}/skills/session-handoff/hooks/context_watch.py" in h.get("command", "")
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
                (plugin_art, ("plugin.json", "SKILL.md", "handoff-template.md", "reference.md", "handoff_protocol.py"), "artifact-plugin"),
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
        standalone = os.path.join(REPO, "dist/session-handoff.skill")
        check("standalone-produced", os.path.isfile(standalone))
        if os.path.isfile(standalone):
            with tempfile.TemporaryDirectory(prefix="standalone-skill-") as tmp:
                with zipfile.ZipFile(standalone) as archive:
                    names = archive.namelist()
                    check("standalone-root", all(n.startswith("session-handoff/") for n in names))
                    check("standalone-no-caches", not any("__pycache__" in n or n.endswith(".pyc") for n in names))
                    archive.extractall(tmp)
                root = os.path.join(tmp, "session-handoff")
                for name in ("SKILL.md", "continuation.md", "reference.md", "handoff-template.md",
                             "agents/openai.yaml", "hooks/thresholds.example.json", "claude-auto", "codex-auto"):
                    check("standalone:" + name, os.path.isfile(os.path.join(root, name)))
                check_installer(root)
                workspace = os.path.join(tmp, "workspace")
                os.mkdir(workspace)
                env = dict(os.environ, HOME=tmp, CONTEXT_WATCH_JEV="0")
                probe = subprocess.run([sys.executable, os.path.join(root, "hooks/handoff_ledger.py"),
                                        "lookup"], cwd=workspace, env=env,
                                       capture_output=True, text=True, timeout=30)
                check("standalone-ledger-runs", probe.returncode == 0 and
                      json.loads(probe.stdout).get("outcome") == "none", probe.stderr[:300])
        gi = open(os.path.join(REPO, ".gitignore")).read() if os.path.isfile(os.path.join(REPO, ".gitignore")) else ""
        check("dist-gitignored", "dist" in gi, ".gitignore must exclude dist/")

    # 3b. The plugin carries the skill folder by symlink (plugin installs copy
    #     the target), so there is one source of truth for skill and hooks.
    link = os.path.join(REPO, "plugins/session-handoff/skills/session-handoff")
    check("plugin-skill-is-symlink-to-skill-folder",
          os.path.islink(link) and os.path.realpath(link) == os.path.realpath(
              os.path.join(REPO, "skills/session-handoff")))

    check_installer()

    # 3c. The skill/plugin version lives in three files that must agree;
    #     scripts/bump-version is the single tool that rewrites all of them.
    bump = subprocess.run([sys.executable, os.path.join(REPO, "scripts/bump-version"), "--check",
                           "--root", REPO], capture_output=True, text=True, timeout=30)
    try:
        bumped = json.loads(bump.stdout)
    except ValueError:
        bumped = {}
    check("versions-agree", bump.returncode == 0 and bumped.get("outcome") == "consistent",
          "per-file versions: %r; fix with scripts/bump-version X.Y.Z"
          % (bumped.get("versions") or bump.stdout + bump.stderr[:300],))

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
