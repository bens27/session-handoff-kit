// Session Handoff Check — content script for claude.ai
//
// Injects a handoff-check prompt into the composer of NEW chats only, once per
// new-chat visit, and only when the composer is empty. Selectors are defensive
// (the claude.ai DOM changes without notice); on any failure this script does
// nothing rather than interfering with the page.

const DEFAULTS = {
  template:
    "Session start: check my open-handoffs ledger. If any handoffs are open, " +
    "list each in one line (topic, stored date, first next step) and ask which to " +
    "resume. If none are open, reply only: No open handoffs.",
  alwaysOn: true,   // inject on every new chat; false = only via toolbar button (#handoff-check)
  autoSend: false,  // pre-fill only by default — keep the human veto
};

let lastInjectedUrl = null;

function getSettings() {
  return new Promise((resolve) => {
    try {
      chrome.storage.sync.get(DEFAULTS, (items) => resolve(items || DEFAULTS));
    } catch (e) {
      resolve(DEFAULTS);
    }
  });
}

function isNewChatPath() {
  const p = location.pathname.replace(/\/+$/, "");
  return p === "" || p === "/new";
}

function buttonRequested() {
  return location.hash === "#handoff-check";
}

function findComposer() {
  // ProseMirror contenteditable, with fallbacks. Update here if the UI changes.
  return (
    document.querySelector('div.ProseMirror[contenteditable="true"]') ||
    document.querySelector('div[contenteditable="true"][aria-label]') ||
    document.querySelector('div[contenteditable="true"]')
  );
}

function findSendButton(composer) {
  const queryRoot =
    composer && (composer.closest("form") || composer.closest('[role="main"]'));
  if (!queryRoot) return null;

  return (
    queryRoot.querySelector('button[aria-label*="send" i]') ||
    queryRoot.querySelector('button[type="submit"]')
  );
}

function composerIsEmpty(el) {
  const text = (el.innerText || "").replace(/\u200b/g, "").trim();
  return text.length === 0;
}

function insertText(el, text) {
  el.focus();
  // execCommand is deprecated but remains the reliable way to type into
  // ProseMirror so the app's own input handlers fire.
  const ok = document.execCommand("insertText", false, text);
  if (!ok) {
    el.textContent = text;
    el.dispatchEvent(new InputEvent("input", { bubbles: true, data: text }));
  }
}

function waitForComposer(timeoutMs) {
  return new Promise((resolve) => {
    const existing = findComposer();
    if (existing) return resolve(existing);
    const observer = new MutationObserver(() => {
      const el = findComposer();
      if (el) {
        observer.disconnect();
        resolve(el);
      }
    });
    observer.observe(document.documentElement, { childList: true, subtree: true });
    setTimeout(() => {
      observer.disconnect();
      resolve(findComposer());
    }, timeoutMs);
  });
}

async function maybeInject() {
  const settings = await getSettings();
  const wantHere = buttonRequested() || (settings.alwaysOn && isNewChatPath());
  if (!wantHere) return;

  const urlKey = location.origin + location.pathname + location.hash;
  if (lastInjectedUrl === urlKey) return; // once per new-chat visit
  const composer = await waitForComposer(8000);
  if (!composer || !composerIsEmpty(composer)) return;

  lastInjectedUrl = urlKey;
  const sessionStartTimestamp = new Date().toISOString();
  insertText(
    composer,
    `Session start timestamp: ${sessionStartTimestamp}. ${settings.template}`
  );

  if (settings.autoSend) {
    setTimeout(() => {
      const btn = findSendButton(composer);
      if (btn && !btn.disabled) btn.click();
    }, 400);
  }
}

// claude.ai is a SPA: watch for soft navigations with a light URL poll.
let lastHref = null;
setInterval(() => {
  if (location.href !== lastHref) {
    lastHref = location.href;
    if (!isNewChatPath() && !buttonRequested()) {
      lastInjectedUrl = null; // left the new-chat page; re-arm for next time
      return;
    }
    maybeInject();
  }
}, 400);
