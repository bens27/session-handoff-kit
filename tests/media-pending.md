# Media pending-token regression

From the repository root:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 tests/verify-media-pending.py codex/hooks/context_watch.py plugins/session-handoff/hooks/context_watch.py
```

To include the active Codex installation, append `/Users/bens/.codex/hooks/context_watch.py`. The suite accepts explicit hook paths; its no-argument defaults refer to the original staging layout.

The September 14 screenshot false alarm counted 1,556,792 base64 characters as about 389,000 tokens. Recognized image/audio payloads now use a 4,096-token per-item heuristic, preserving surrounding text. This is an estimate, not exact model media accounting. Large real text, including JSON whitespace, still triggers the configured threshold. The suite verifies the actual hook entrypoint using isolated home, temporary directory, transcript, and latch state.
