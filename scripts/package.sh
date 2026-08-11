#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/.." && pwd)"

cd "${repo_root}"
mkdir -p dist
rm -f dist/session-handoff.plugin dist/session-handoff-chat.skill

(
  cd plugins/session-handoff
  /usr/bin/zip -qr "${repo_root}/dist/session-handoff.plugin" . \
    -x '*.DS_Store' '*/.DS_Store' '*__pycache__*'
)

(
  cd chat/session-handoff-chat
  /usr/bin/zip -qr "${repo_root}/dist/session-handoff-chat.skill" . \
    -x '*.DS_Store' '*/.DS_Store' '*__pycache__*'
)

printf '%s\n' \
  "dist/session-handoff.plugin" \
  "dist/session-handoff-chat.skill"
