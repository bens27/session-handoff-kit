const DEFAULTS = {
  template:
    "Session start: check my open-handoffs ledger. If any handoffs are open, " +
    "list each in one line (topic, age, first next step) and ask which to " +
    "resume. If none are open, reply only: No open handoffs.",
  alwaysOn: true,
  autoSend: false,
};

chrome.storage.sync.get(DEFAULTS, (items) => {
  document.getElementById("template").value = items.template;
  document.getElementById("alwaysOn").checked = items.alwaysOn;
  document.getElementById("autoSend").checked = items.autoSend;
});

document.getElementById("save").addEventListener("click", () => {
  chrome.storage.sync.set(
    {
      template: document.getElementById("template").value,
      alwaysOn: document.getElementById("alwaysOn").checked,
      autoSend: document.getElementById("autoSend").checked,
    },
    () => {
      const s = document.getElementById("status");
      s.textContent = "Saved";
      setTimeout(() => (s.textContent = ""), 1500);
    }
  );
});
