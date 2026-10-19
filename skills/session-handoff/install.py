#!/usr/bin/env python3
"""Register (or remove) the session-handoff hooks for Claude Code and Codex.

Usage: python3 install.py [claude|codex ...] [--uninstall]
       python3 install.py runtime --root PATH create VERSION
       python3 install.py runtime --root PATH activate RELEASE claude|codex
       python3 install.py runtime --root PATH rollback claude|codex

Hook commands point at hooks/context_watch.py beside this file by absolute
path, so run this from where the skill folder is installed, and re-run it
after moving the folder. Idempotent: every run first removes any watcher
entry it finds (by the context_watch.py marker), then adds the current one;
all other hooks are left alone. Each config file is backed up once
(<file>.bak-session-handoff) before its first change.
"""
import json
import os
import re
import shlex
import shutil
import sys
import argparse
import hashlib
from pathlib import Path
import subprocess
import tempfile
import base64
import fcntl
import time

RUNTIME_FORMAT = 1
REQUIRED_RUNTIME = {"SKILL.md", "continuation.md", "handoff-template.md", "reference.md",
                    "install.py", "hooks/context_watch.py", "hooks/handoff_ledger.py",
                    "hooks/handoff_protocol.py", "hooks/handoff_contract.py", "hooks/restart_preflight.py", "agents/openai.yaml", "claude-auto", "codex-auto"}
# Explicit product inputs, independent of Git metadata so extracted packages
# behave exactly like checkouts. Local config, secrets and editor files are not
# distributable inputs. Update this declaration when adding a product file.
RUNTIME_PAYLOAD = REQUIRED_RUNTIME | {"lane.py", "hooks/thresholds.example.json"}


def private_runtime_root(root):
    if root == Path(root.anchor) or root == Path.home().resolve():
        raise ValueError("use a dedicated runtime directory, not HOME or the filesystem root")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)


def check_runtime_source(source):
    directories = {str(Path(name).parent) for name in RUNTIME_PAYLOAD} - {"."}
    for path in source.rglob("*"):
        rel = path.relative_to(source)
        if "__pycache__" in rel.parts or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            raise ValueError("runtime contains symlink: " + rel.as_posix())
        if rel.as_posix() == "runtime-manifest.json" and path.is_file():
            continue
        if path.is_dir() and rel.as_posix() in directories:
            continue
        if rel.as_posix() not in RUNTIME_PAYLOAD:
            raise ValueError("undeclared runtime input: " + rel.as_posix())
        if not path.is_file():
            raise ValueError("runtime contains special file: " + rel.as_posix())


def encoded(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()


def runtime_files(folder):
    """Fingerprint the complete payload, rejecting links and special files."""
    files = {}
    for path in sorted(folder.rglob("*")):
        rel = path.relative_to(folder).as_posix()
        if path.is_symlink():
            raise ValueError("runtime contains symlink: " + rel)
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("runtime contains special file: " + rel)
        if rel == "runtime-manifest.json":
            continue
        files[rel] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                      "mode": path.stat().st_mode & 0o777}
    if files.keys() != RUNTIME_PAYLOAD:
        raise ValueError("runtime payload must match declared files; missing=" +
                         str(sorted(RUNTIME_PAYLOAD - files.keys())) + " extra=" +
                         str(sorted(files.keys() - RUNTIME_PAYLOAD)))
    return files


def runtime_smoke(folder):
    for path in folder.rglob("*.py"):
        compile(path.read_bytes(), str(path), "exec")
    # Exercise the packaged CLI without writing bytecode into a sealed release.
    for entry in ("hooks/handoff_ledger.py", "hooks/restart_preflight.py"):
        subprocess.run([sys.executable, "-B", str(folder / entry), "--help"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)


def validate_runtime(root, release, repair_top_seal=False):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*-[a-f0-9]{64}", release):
        raise ValueError("expected the exact release ID returned by create")
    folder = root / "releases" / release
    if (root / "releases").is_symlink():
        raise ValueError("release directory must not be a symlink")
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError("release is missing or is a symlink")
    for directory in [folder, *(p for p in folder.rglob("*") if p.is_dir())]:
        if directory.stat().st_mode & 0o222 and not (directory == folder and repair_top_seal):
            raise ValueError("runtime directories must be read-only")
    manifest_path = folder / "runtime-manifest.json"
    if manifest_path.is_symlink():
        raise ValueError("manifest must not be a symlink")
    if manifest_path.stat().st_mode & 0o222:
        raise ValueError("runtime manifest must be read-only")
    manifest = json.loads(manifest_path.read_text())
    if (manifest.get("format") != RUNTIME_FORMAT or
            release != manifest.get("version", "") + "-" + hashlib.sha256(encoded(manifest)).hexdigest()):
        raise ValueError("runtime manifest identity mismatch")
    if runtime_files(folder) != manifest.get("files"):
        raise ValueError("runtime payload integrity mismatch")
    runtime_smoke(folder)
    if repair_top_seal and folder.stat().st_mode & 0o222:
        # Only the known rename/chmod interruption is repairable. Verify the
        # manifest, complete payload and every nested mode before sealing it.
        folder.chmod(0o500)
        descriptor = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return folder


def create_runtime(root, version):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", version):
        raise ValueError("version must be 1-64 letters, digits, dots, underscores or hyphens")
    source = Path(__file__).resolve().parent
    if root == source or source in root.parents or root in source.parents or root == Path.home().resolve():
        raise ValueError("runtime root must be outside the source skill")
    check_runtime_source(source)
    private_runtime_root(root)
    releases = root / "releases"
    if releases.is_symlink():
        raise ValueError("release directory must not be a symlink")
    releases.mkdir(parents=True, exist_ok=True, mode=0o700)
    releases.chmod(0o700)
    with tempfile.TemporaryDirectory(prefix=".stage-", dir=root) as temp:
        staged = Path(temp) / "payload"
        staged.mkdir(mode=0o700)
        for name in sorted(RUNTIME_PAYLOAD):
            target = staged / name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copy2(source / name, target, follow_symlinks=False)
        runtime_files(staged)
        runtime_smoke(staged)
        for path in staged.rglob("*"):
            if path.is_file():
                path.chmod(0o500 if path.stat().st_mode & 0o111 else 0o400)
        manifest = {"format": RUNTIME_FORMAT, "version": version, "files": runtime_files(staged)}
        release = version + "-" + hashlib.sha256(encoded(manifest)).hexdigest()
        (staged / "runtime-manifest.json").write_bytes(encoded(manifest) + b"\n")
        (staged / "runtime-manifest.json").chmod(0o400)
        # Persist the complete payload before publishing its release name.
        for path in staged.rglob("*"):
            if path.is_file():
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
        for directory in [*(p for p in staged.rglob("*") if p.is_dir()), staged]:
            if directory != staged:
                directory.chmod(0o500)
            descriptor = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        destination = releases / release
        if destination.exists():
            try:
                validate_runtime(root, release, repair_top_seal=True)
            except (OSError, ValueError, SyntaxError, subprocess.SubprocessError) as exc:
                raise ValueError("existing release is invalid; inspect and remove the invalid release " +
                                 str(destination) + " explicitly before retrying create: " + str(exc)) from exc
        else:
            os.rename(staged, destination)
            # macOS requires a writable source directory to rename it. A kill
            # before this seal leaves a release validation refuses to activate.
            destination.chmod(0o500)
            descriptor = os.open(releases, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    return {"release": release, "snapshot": str(destination), "valid": True}


def runtime_main(argv):
    parser = argparse.ArgumentParser(description="Create, validate and explicitly activate runtime snapshots")
    parser.add_argument("--root", default=str(Path.home() / ".local/share/session-handoff/runtime"))
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("create").add_argument("version")
    commands.add_parser("validate").add_argument("release")
    activate = commands.add_parser("activate")
    activate.add_argument("release")
    activate.add_argument("target", choices=["claude", "codex"])
    commands.add_parser("rollback").add_argument("target", choices=["claude", "codex"])
    args = parser.parse_args(argv)
    root = Path(args.root).expanduser().resolve()
    if args.action == "create":
        result = create_runtime(root, args.version)
    elif args.action == "validate":
        result = {"snapshot": str(validate_runtime(root, args.release)), "valid": True}
    else:
        result = switch_runtime(root, args)
    print(json.dumps(result))


def config_path(target):
    if target == "claude":
        return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "settings.json"
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "hooks.json"


def atomic_bytes(path, content, mode=0o600):
    """Publish complete data or leave the previous file intact, including on kill."""
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def pinned_release(root, data):
    """Recover a prior snapshot identity from registered commands, never execute them."""
    found = set()
    for groups in data.get("hooks", {}).values():
        for group in groups:
            for hook in group.get("hooks", []):
                command = hook.get("command", "")
                if not isinstance(command, str) or MARKER not in command:
                    continue
                try:
                    words = shlex.split(command)
                except ValueError:
                    continue  # foreign shell syntax is not ours to validate
                for word in words:
                    path = Path(word)
                    if path.name == MARKER and path.parent.name == "hooks" and path.parent.parent.parent == root / "releases":
                        found.add(path.parent.parent.name)
    if len(found) > 1:
        raise ValueError("configuration refers to multiple runtime releases")
    return next(iter(found), None)


def read_runtime_receipt(path):
    """Validate untrusted on-disk recovery metadata before using any fields."""
    required = {"format", "config", "release", "previous_release", "before_mode", "before", "after_sha256"}
    try:
        if path.is_symlink():
            return None
        receipt = json.loads(path.read_text())
        if not isinstance(receipt, dict) or not required <= receipt.keys():
            return None
        if type(receipt["format"]) is not int or receipt["format"] != RUNTIME_FORMAT:
            return None
        if not isinstance(receipt["config"], str) or not Path(receipt["config"]).is_absolute():
            return None
        for name in ("release", "previous_release"):
            value = receipt[name]
            if name == "previous_release" and value is None:
                continue
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*-[a-f0-9]{64}", value):
                return None
        if type(receipt["before_mode"]) is not int or not 0 <= receipt["before_mode"] <= 0o777:
            return None
        if not isinstance(receipt["after_sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", receipt["after_sha256"]):
            return None
        if receipt["before"] is not None:
            if not isinstance(receipt["before"], str):
                return None
            restored = base64.b64decode(receipt["before"], validate=True)
            if not isinstance(json.loads(restored), dict):
                return None
        elif receipt["previous_release"] is not None:
            return None
        return receipt
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        return None


def switch_runtime(root, args):
    """One host per transaction. Receipt is durable before atomic config switch.

    No mutable current symlink: every command pins the full content-addressed
    release, so a running hook and its subprocesses use the same payload.
    """
    private_runtime_root(root)
    if (root / "releases").is_dir() and not (root / "releases").is_symlink():
        (root / "releases").chmod(0o700)
    path = config_path(args.target).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    receipts = root / "transactions"
    receipts.mkdir(parents=True, exist_ok=True, mode=0o700)
    receipts.chmod(0o700)
    with open(str(path) + ".lock-session-handoff-runtime", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        before = path.read_bytes() if path.exists() else None
        data = json.loads(before) if before is not None else {}
        if args.action == "rollback":
            # A pre-switch receipt from an interrupted activation does not match
            # current bytes and is therefore never mistaken for a completed one.
            skipped = 0
            for receipt_path in sorted(receipts.glob("*.json"), reverse=True):
                receipt = read_runtime_receipt(receipt_path)
                if receipt is None:
                    skipped += 1
                    continue
                if receipt["config"] == str(path) and before is not None and receipt["after_sha256"] == hashlib.sha256(before).hexdigest():
                    previous = receipt["previous_release"]
                    if previous:
                        validate_runtime(root, previous)
                    content = base64.b64decode(receipt["before"]) if receipt["before"] is not None else None
                    if content is None:
                        path.unlink()
                    else:
                        atomic_bytes(path, content, receipt["before_mode"])
                    return {"rolled_back": True, "config": str(path), "release": previous,
                            "skipped_receipts": skipped}
            raise ValueError("no activation matches current config; later edits must be reconciled explicitly " +
                             "(invalid receipts skipped: %d)" % skipped)
        folder = validate_runtime(root, args.release)
        previous = pinned_release(root, data)
        hooks = data.setdefault("hooks", {})
        strip(hooks)
        command = "python3 -B " + shlex.quote(str(folder / "hooks/context_watch.py"))
        events = CLAUDE_EVENTS if args.target == "claude" else CODEX_EVENTS
        for event, timeout in events.items():
            group = {"hooks": [{"type": "command", "command": command, "timeout": timeout}]}
            if args.target == "claude" and event == "PostToolUse":
                group["matcher"] = ""
            hooks.setdefault(event, []).append(group)
        after = (json.dumps(data, indent=2) + "\n").encode()
        if before == after:
            return {"activated": True, "unchanged": True, "snapshot": str(folder), "config": str(path)}
        mode = path.stat().st_mode & 0o777 if before is not None else 0o600
        receipt = {"format": RUNTIME_FORMAT, "config": str(path), "release": args.release,
                   "previous_release": previous, "before_mode": mode,
                   "before": base64.b64encode(before).decode() if before is not None else None,
                   "after_sha256": hashlib.sha256(after).hexdigest()}
        receipt_path = receipts / (str(time.time_ns()) + ".json")
        atomic_bytes(receipt_path, encoded(receipt) + b"\n")
        atomic_bytes(path, after, mode)
        return {"activated": True, "snapshot": str(folder), "config": str(path),
                "receipt": str(receipt_path), "skill_path": str(folder)}

WATCHER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hooks", "context_watch.py")
CMD = "python3 " + shlex.quote(WATCHER)
MARKER = "context_watch.py"
# Recognize fan-outs written by older installers so upgrades restore the
# original command. Current Codex runs independent matching hook groups.
FANOUT = re.compile(r'^payload=\$\(cat\); printf %s "\$payload" \| (?P<base>.*); '
                    r'printf %s "\$payload" \| [^;]*context_watch\.py\S*$', re.S)

CLAUDE_EVENTS = {"PostToolUse": 20, "UserPromptSubmit": 20, "SessionStart": 10, "Stop": 10}
CODEX_EVENTS = {"SessionStart": 20, "UserPromptSubmit": 20, "PostToolUse": 20}  # no Stop in Codex


def load(path):
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)  # malformed config: fail loudly rather than overwrite it


def save(path, data):
    path = os.path.realpath(path)  # write through a dotfiles symlink, don't replace it
    os.makedirs(os.path.dirname(path), exist_ok=True)
    bak = path + ".bak-session-handoff"
    if os.path.exists(path) and not os.path.exists(bak):
        shutil.copy2(path, bak)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    if os.path.exists(path):
        shutil.copymode(path, tmp)
    os.replace(tmp, path)


def strip(hooks):
    """Remove every watcher entry from a {event: [group]} map, un-fanning
    Codex fan-outs back to their original command. Returns events touched."""
    touched = set()
    for event, groups in list(hooks.items()):
        kept_groups = []
        for g in groups:
            kept = []
            for h in g.get("hooks", []):
                cmd = h.get("command", "")
                if MARKER not in cmd:
                    kept.append(h)
                    continue
                touched.add(event)
                m = FANOUT.match(cmd)
                if m:
                    base = m.group("base")
                    if base.startswith("(") and base.endswith(")"):
                        base = base[1:-1]
                    kept.append(dict(h, command=base))
            if kept:
                kept_groups.append(dict(g, hooks=kept))
        if kept_groups or not groups:
            hooks[event] = kept_groups  # an empty list we found stays as found
        else:
            del hooks[event]
    return touched


def claude(uninstall):
    path = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"),
                        "settings.json")
    if uninstall and not os.path.exists(path):
        return
    data = load(path)
    hooks = data.setdefault("hooks", {})
    strip(hooks)
    if not uninstall:
        for event, timeout in CLAUDE_EVENTS.items():
            group = {"hooks": [{"type": "command", "command": CMD, "timeout": timeout}]}
            if event == "PostToolUse":
                group = dict(matcher="", **group)
            hooks.setdefault(event, []).append(group)
    if not hooks:
        del data["hooks"]
    save(path, data)
    print("%s Claude Code hooks: %s" % ("Removed" if uninstall else "Registered", path))


def codex(uninstall):
    home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    path = os.path.join(home, "hooks.json")
    if uninstall and not os.path.exists(path):
        return
    data = load(path)
    hooks = data.setdefault("hooks", {})
    strip(hooks)
    if not uninstall:
        for event, timeout in CODEX_EVENTS.items():
            # Preserve foreign commands, matchers, outputs and exit statuses.
            hooks.setdefault(event, []).append({
                "hooks": [{"type": "command", "command": CMD, "timeout": timeout}]})
    save(path, data)
    print("%s Codex hooks: %s" % ("Removed" if uninstall else "Registered", path))
    if not uninstall:
        print("""  Codex also needs, once:
  - hooks enabled in %s/config.toml:  [features] hooks = true
  - the changed hook re-approved: Codex trusts hooks by command hash and
    silently skips a changed one. Launch `codex` once and approve it
    using its native hook review.
  - optional: model_auto_compact_token_limit above the watcher threshold,
    so the handoff fires before auto-compaction.""" % home)


def main(argv):
    if argv[:1] == ["runtime"]:
        try:
            runtime_main(argv[1:])
        except (OSError, ValueError, SyntaxError, subprocess.SubprocessError) as exc:
            sys.exit("Runtime operation failed: " + str(exc))
        return
    uninstall = "--uninstall" in argv
    targets = [a for a in argv if a != "--uninstall"] or ["claude", "codex"]
    unknown = set(targets) - {"claude", "codex"}
    if unknown:
        sys.exit(__doc__)
    for t in targets:
        {"claude": claude, "codex": codex}[t](uninstall)


if __name__ == "__main__":
    main(sys.argv[1:])
