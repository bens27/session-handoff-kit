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

# 3) hooks.json (generated with absolute paths; refuses to clobber an existing one)
HOOK_CMD="python3 $CODEX_HOME/hooks/context_watch.py"
if [ -f "$CODEX_HOME/hooks.json" ]; then
  echo "! $CODEX_HOME/hooks.json already exists — merge these events into it manually:"
  echo "    PostToolUse / UserPromptSubmit / SessionStart -> command: $HOOK_CMD"
else
  cat > "$CODEX_HOME/hooks.json" << EOF
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "*",
        "hooks": [
          { "type": "command", "command": "$HOOK_CMD", "timeout": 20 }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          { "type": "command", "command": "$HOOK_CMD", "timeout": 20 }
        ]
      }
    ],
    "SessionStart": [
      {
        "hooks": [
          { "type": "command", "command": "$HOOK_CMD", "timeout": 10 }
        ]
      }
    ]
  }
}
EOF
  echo "Wrote $CODEX_HOME/hooks.json"
fi

cat << 'EOF'

Done. Two manual steps remain:

1) Enable the hooks feature in ~/.codex/config.toml (experimental;
   historically unavailable on Windows):

     [features]
     hooks = true          # older builds used: codex_hooks = true

2) Optional but recommended — keep auto-compaction ABOVE the watcher
   threshold so the handoff always fires first. The watcher defaults to
   70% of the window; Codex auto-compacts around 80% by default. If you
   raise CONTEXT_WATCH_PERCENT, raise this too, e.g. for a 400k window:

     model_auto_compact_token_limit = 340000

Restart Codex. If hooks do not register, your build may expect the event
names at the TOP LEVEL of hooks.json (no outer "hooks" wrapper) — the hook
surface is experimental and the accepted shape has varied across versions.
Open ~/.codex/hooks.json and remove the wrapper if needed.
EOF
