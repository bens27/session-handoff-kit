const DEFAULTS = {
  template:
    "Session start: check my open-handoffs ledger. If any handoffs are open, " +
    "list each in one line (topic, stored date, first next step) and ask which to " +
    "resume. If none are open, reply only: No open handoffs.",
  alwaysOn: true,
  autoSend: false,
};

function setStatus(text) {
  const s = document.getElementById("status");
  s.textContent = text;
  setTimeout(() => (s.textContent = ""), 1500);
}

function applySettings(items) {
  document.getElementById("template").value = items.template;
  document.getElementById("alwaysOn").checked = items.alwaysOn;
  document.getElementById("autoSend").checked = items.autoSend;
}

try {
  chrome.storage.sync.get(DEFAULTS, (items) => {
    if (chrome.runtime.lastError) {
      applySettings(DEFAULTS);
      return;
    }
    applySettings(items || DEFAULTS);
  });
} catch (e) {
  applySettings(DEFAULTS);
}

document.getElementById("save").addEventListener("click", () => {
  try {
    chrome.storage.sync.set(
      {
        template: document.getElementById("template").value,
        alwaysOn: document.getElementById("alwaysOn").checked,
        autoSend: document.getElementById("autoSend").checked,
      },
      () => {
        if (chrome.runtime.lastError) {
          setStatus("Save failed");
          return;
        }
        setStatus("Saved");
      }
    );
  } catch (e) {
    setStatus("Save failed");
  }
});
