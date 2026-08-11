// Toolbar button: open a new chat with the handoff check requested via the
// URL hash — the content script watches for #handoff-check, so this works
// even when the always-on setting is off.
chrome.action.onClicked.addListener(() => {
  chrome.tabs.create({ url: "https://claude.ai/new#handoff-check" });
});
