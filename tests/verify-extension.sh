#!/bin/bash
# Executed verification for the extension fix lane. Run from repo root (worktree).
set -u
NODE="${NODE:-$(command -v node)}"
EXT=chrome-extension
rc=0
ERRTMP="$(mktemp)"   # never write into the tree being validated
trap 'rm -f "$ERRTMP"' EXIT

for f in content.js background.js options.js; do
  if "$NODE" --check "$EXT/$f" 2>"$ERRTMP"; then
    echo "PASS node-check:$f"
  else
    echo "FAIL node-check:$f — $(cat "$ERRTMP")"; rc=1
  fi
done

/opt/homebrew/bin/python3.14 -c "import json; json.load(open('$EXT/manifest.json'))" \
  && echo "PASS manifest-json" || { echo "FAIL manifest-json"; rc=1; }

# Finding E1: the unscoped submit-button fallback must be gone or scoped.
# Matches document.querySelector('button[type="submit"]') with either quote style.
if grep -Eq "document\.querySelector\(['\"]button\[type=." "$EXT/content.js"; then
  echo 'FAIL send-button-scoped — content.js still uses an unscoped document.querySelector for button[type=submit]; scope it to the composer region or drop it'
  rc=1
else
  echo "PASS send-button-scoped"
fi

# Finding E2: options.js must guard chrome.storage.sync access (try/catch and/or
# chrome.runtime.lastError handling), mirroring content.js's defensive posture.
if grep -Eq 'lastError|try' "$EXT/options.js"; then
  echo "PASS options-defensive"
else
  echo "FAIL options-defensive — options.js has no try/catch or lastError guard around chrome.storage.sync"
  rc=1
fi

[ $rc -eq 0 ] && echo "ALL EXTENSION CHECKS PASSED" || echo "FAILED: see FAIL lines above"
exit $rc
