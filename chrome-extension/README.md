# Session Handoff Check (browser extension)

Gives claude.ai chat the one thing it lacks: something that runs before your
first message. On every new chat (or on demand via the toolbar button), it
pre-fills the composer with a handoff-check prompt — the trigger phrase the
`session-handoff-chat` skill listens for — so surfacing open handoffs is the
session's first act.

## Install (Chrome and Edge — identical)

1. `chrome://extensions` (or `edge://extensions`) → enable **Developer mode**
2. **Load unpacked** → select this `chrome-extension/` folder

No store listing needed for personal use.

## Behavior and settings (extension options page)

- **Template** — the injected prompt. Default asks Claude to check the
  open-handoffs ledger, list open items one line each, and ask which to
  resume; the empty case costs five words ("No open handoffs").
- **Inject on every new chat** (default on) — off means injection happens only
  when you open a chat via the toolbar button.
- **Auto-send** (default off) — off pre-fills and leaves Enter to you. On
  makes the check truly zero-keystroke: by the time you focus the window, the
  open-handoff list is already on screen. Pre-fill is the default because it
  keeps the human veto and stays clearly on the "typing assistance" side of
  automating the site.

Injection only happens into an **empty** composer, once per new-chat visit,
and never on existing chats.

## Zero-install alternatives

- **Claude desktop app**: the officially supported deep link
  `claude://claude.ai/new?q=<url-encoded prompt>` prefills the composer for
  review — bind it to an OS hotkey and you have this extension's toolbar
  button without the extension.
- **Web `?q=` parameter**: `https://claude.ai/new?q=...` has come and gone
  historically; test it on your account before relying on it.

## Honest limits

- The extension cannot see Claude's memory; it injects the check blind and
  Claude answers from the ledger. Zero open handoffs still costs one short
  turn (only when always-on is enabled).
- claude.ai's DOM changes without notice. Selectors live at the top of
  `content.js` (`findComposer`, `findSendButton`) and fail silently — if
  injection stops working, update those two functions.
- Scoped to `https://claude.ai/*` only; no data leaves the page. Settings are
  stored in `chrome.storage.sync`.
