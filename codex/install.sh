#!/usr/bin/env bash
# Installs the context-watch hook + session-handoff skill into a Codex CLI home.
# Usage: bash install.sh    (respects $CODEX_HOME, defaults to ~/.codex)
set -euo pipefail

CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
KIT_DIR="$(cd "$(dirname "$0")" && pwd)"

mkdir -p "$CODEX_HOME/hooks" "$CODEX_HOME/skills"

# 1) Hook script
cp "$KIT_DIR/hooks/context_watch.py" "$CODEX_HOME/hooks/context_watch.py"
cp "$KIT_DIR/hooks/handoff_ledger.py" "$CODEX_HOME/hooks/handoff_ledger.py"
chmod +x "$CODEX_HOME/hooks/context_watch.py" "$CODEX_HOME/hooks/handoff_ledger.py"
echo "Installed hook scripts -> $CODEX_HOME/hooks/ (context_watch.py, handoff_ledger.py)"

# 2) Skill
if [ -d "$CODEX_HOME/skills/session-handoff" ]; then
  echo "! Skill already exists at $CODEX_HOME/skills/session-handoff — left untouched."
else
  cp -r "$KIT_DIR/skills/session-handoff" "$CODEX_HOME/skills/session-handoff"
  echo "Installed skill -> $CODEX_HOME/skills/session-handoff/"
fi

# 3) hooks.json — idempotent merge, never a naive append.
# Codex (verified against 0.145.0) executes only the FIRST hook group per
# event, so if a hooks.json already exists (e.g. from Orca), appending our
# watcher as a second group would silently never run. merge_hooks.py instead
# fans our watcher into the existing first group via a stdin fan-out.
if [ ! -f "$CODEX_HOME/hooks.json" ]; then
  echo '{"hooks": {}}' > "$CODEX_HOME/hooks.json"
fi
cp -n "$CODEX_HOME/hooks.json" "$CODEX_HOME/hooks.json.bak-$(date +%Y%m%d)" 2>/dev/null || true
python3 "$KIT_DIR/merge_hooks.py" "$CODEX_HOME"

cat << 'EOF'

Done. Three manual steps remain:

1) Enable the hooks feature in ~/.codex/config.toml (experimental;
   historically unavailable on Windows):

     [features]
     hooks = true          # older builds used: codex_hooks = true

2) Approve the hook (verified on Codex 0.145.0). Codex trusts hooks by
   command hash, keyed per hooks.json path + event + group + hook index,
   stored under [hooks.state] in config.toml. merge_hooks.py just changed
   that command's text, so its old trusted_hash no longer matches and the
   hook is silently SKIPPED — not an error, just quietly a no-op — until
   re-approved. Two ways to re-approve:
     - Launch `codex` interactively once and approve the hook-trust prompt.
     - For headless/automated invocations (scripts, CI, this kit's own
       eval harness), pass `--dangerously-bypass-hook-trust` to `codex exec`.
   Symptom if you skip this: everything LOOKS installed correctly (hooks.json
   is well-formed, the feature flag is on) but no events.jsonl, no handoff,
   ever appears — because the run never got trust approval.

3) Optional but recommended — keep auto-compaction ABOVE the watcher
   threshold so the handoff always fires first. The watcher defaults to
   70% of the window; Codex auto-compacts around 80% by default. If you
   raise CONTEXT_WATCH_PERCENT, raise this too, e.g. for a 400k window:

     model_auto_compact_token_limit = 340000

Restart Codex. If hooks do not register, your build may expect the event
names at the TOP LEVEL of hooks.json (no outer "hooks" wrapper) — the hook
surface is experimental and the accepted shape has varied across versions.
Open ~/.codex/hooks.json and remove the wrapper if needed.

Caveat (verified on Codex 0.145.0): the watcher only runs on PostToolUse,
so it can only act AFTER a tool call completes. A model that does a large
chunk of work in one big tool call (e.g. a single apply_patch touching many
files) can cross the threshold and finish the work in the same step — the
hook still fires and a handoff still gets written, but only after the work
is already done, so it can't interrupt mid-task the way it does when work
is spread across many smaller tool calls.
EOF
