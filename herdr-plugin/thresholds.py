"""Popup: one agent pane's context use against its handoff thresholds.

Reads the session note the context-watch hook writes, so the numbers are the
ones the hook acts on. Edits go to this pane's override file or to the global
~/.context-watch/thresholds.json.
"""
import json
import os
import select
import signal
import subprocess
import sys
import termios
import time
import tty

HOOKS = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "skills", "session-handoff", "hooks")
sys.path.insert(0, os.path.realpath(HOOKS))

import context_watch  # noqa: E402
import handoff_identity  # noqa: E402
import watch_decision  # noqa: E402

HERDR = os.environ.get("HERDR_BIN_PATH") or "herdr"
TARGET = os.environ.get("HANDOFF_TARGET_PANE") or ""
STEP = 5000
DIM, BOLD, RESET, WARN, HOT = "\x1b[2m", "\x1b[1m", "\x1b[0m", "\x1b[33m", "\x1b[31m"


def target_identity(pane):
    """Resolve thresholds as the agent in `pane` does. This process is its own
    Herdr pane, and the agent's environment overrides are unknown here; the
    session note's threshold source shows when one applies."""
    os.environ.update(HERDR_ENV="1", HERDR_PANE_ID=pane)
    for name in ("HANDOFF_TERMINAL_ID", "AGENTSROOM_AGENT_ID", "HANDOFF_AT", "CONTEXT_WATCH_WARNING_WINDOW"):
        os.environ.pop(name, None)
    return handoff_identity.terminal_id()


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2)
        stream.write("\n")
    os.replace(tmp, path)


def save_pane(terminal, pane, warning, handoff):
    write_json(handoff_identity.terminal_settings_path(terminal),
               {"terminal": terminal, "pane": pane, "at": handoff, "warning_window_tokens": handoff - warning})


def clear_pane(terminal):
    try:
        os.remove(handoff_identity.terminal_settings_path(terminal))
    except FileNotFoundError:
        pass


def save_global(warning, handoff):
    """Set "at" and "warning_window_tokens", keeping every other key in the file."""
    path = os.path.join(os.path.expanduser("~"), ".context-watch", "thresholds.json")
    try:
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
    except FileNotFoundError:
        data = {}
    if not isinstance(data, dict):
        raise ValueError("%s is not a JSON object" % path)
    data.update(at=handoff, warning_window_tokens=handoff - warning)
    write_json(path, data)


def resolve(cwd, model, window):
    """(warning, handoff, source) the hook would use with the current files."""
    root = handoff_identity.project_root(cwd)
    limit, source = context_watch.resolve_threshold(model, window, root)
    lead, _, error = watch_decision.warning_lead(context_watch.warning_window_candidates(root))
    if error:
        raise ValueError(error)
    return max(limit - lead, 0), limit, source


def pane_session(pane):
    """(session id, cwd) Herdr reports for the agent in `pane`; None when the pane is gone."""
    out = subprocess.run([HERDR, "api", "snapshot"], capture_output=True, text=True, check=True).stdout
    for p in json.loads(out)["result"]["snapshot"].get("panes", []):
        if p.get("pane_id") == pane:
            return (p.get("agent_session") or {}).get("value") or "", p.get("cwd") or ""
    return None


def read_note(cwd, session):
    if not (cwd and session):
        return {}
    try:
        with open(handoff_identity.session_note_path(cwd, session), encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def human(n):
    return ("%.1fM" % (n / 1e6)) if n >= 1_000_000 else ("%.1fk" % (n / 1000)) if n >= 1000 else str(n)


def bar(width, used, warning, handoff, window):
    def cell(tokens):
        return min(int(tokens * width / window), width - 1)

    marks = {cell(handoff): "!", cell(warning): "|"}  # warning wins a shared cell
    cells = []
    for i in range(width):
        tokens = window * (i + 0.5) / width
        mark = marks.get(i, "")
        color = HOT if tokens >= handoff else WARN if tokens >= warning else ""
        fill = "█" if tokens <= used else "░"
        cells.append((BOLD + mark + RESET) if mark else (color + fill + RESET if color else fill))
    return "".join(cells)


class State:
    def __init__(self, pane):
        self.pane = pane
        self.terminal = target_identity(pane)
        self.scope = "pane"
        self.session = self.cwd = ""
        self.note = {}
        self.warning = self.handoff = None
        self.message = ""
        self.gone = False

    def refresh(self):
        found = pane_session(self.pane)
        if found is None:
            self.gone = True
            return
        self.session, self.cwd = found
        self.note = read_note(self.cwd, self.session)
        if self.warning is None and self.cwd:
            self.warning, self.handoff, _ = resolve(self.cwd, self.note.get("model"), self.note.get("window") or 200000)

    def adjust(self, which, delta):
        if self.warning is None:
            return
        if which == "warning":
            self.warning = min(max(self.warning + delta, STEP), self.handoff)
        else:
            self.handoff = max(self.handoff + delta, self.warning, STEP)

    def save(self):
        if self.scope == "pane":
            save_pane(self.terminal, self.pane, self.warning, self.handoff)
        else:
            save_global(self.warning, self.handoff)
        self.message = "Saved for %s; the hook applies it on its next event." % (
            "this pane" if self.scope == "pane" else "all agents")

    def render(self):
        note = self.note
        lines = [BOLD + " Session handoff" + RESET + DIM + "  %s  %s" % (self.pane, note.get("model") or "") + RESET, ""]
        if self.warning is None:
            lines.append(" Waiting for the agent's first hook event…")
        else:
            used = note.get("observed_occupancy_tokens") or 0
            window = note.get("window") or 200000
            lines.append(" Context  %s / %s  (%d%%)" % (human(used), human(window), round(100 * used / window)))
            lines.append(" " + bar(44, used, self.warning, self.handoff, window))
            lines.append(DIM + "          | warning   ! handoff" + RESET)
            lines.append(" Warning  %8s   w/W  -/+%s" % (human(self.warning), human(STEP)))
            lines.append(" Handoff  %8s   h/H  -/+%s" % (human(self.handoff), human(STEP)))
            source = note.get("threshold_source") or "no hook event yet"
            lines.append(DIM + " Hook now: %s at %s" % (human(note.get("threshold") or 0), source) + RESET
                         if note.get("threshold") else DIM + " Hook now: " + source + RESET)
            lines.append(" Save to  %s   (tab switches)" % (
                BOLD + "[this pane]" + RESET + " all agents" if self.scope == "pane"
                else "this pane " + BOLD + "[all agents]" + RESET))
        lines.append("")
        lines.append(" s save   c clear pane override   q close")
        if self.message:
            lines.append(DIM + " " + self.message + RESET)
        sys.stdout.write("\x1b[H\x1b[2J" + "\r\n".join(lines))
        sys.stdout.flush()

    def key(self, key):
        steps = {"w": ("warning", -STEP), "W": ("warning", STEP), "h": ("handoff", -STEP), "H": ("handoff", STEP)}
        if key in steps:
            self.adjust(*steps[key])
        elif key == "\t":
            self.scope = "global" if self.scope == "pane" else "pane"
        elif key == "s" and self.warning is not None:
            self.save()
        elif key == "c":
            clear_pane(self.terminal)
            self.warning = None  # re-resolve without the override
            self.message = "Cleared this pane's override."


def main():
    if not TARGET:
        print("HANDOFF_TARGET_PANE is not set; open this with the Handoff thresholds action.")
        time.sleep(3)
        return 1
    state = State(TARGET)
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd) if os.isatty(fd) else None
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: sys.exit(0))
    sys.stdout.write("\x1b[?1049h\x1b[?25l")
    try:
        if old:
            tty.setcbreak(fd)
        last = 0.0
        while not state.gone:
            if time.time() - last >= 2:
                state.refresh()
                last = time.time()
            state.render()
            if select.select([sys.stdin], [], [], 0.5)[0]:
                for key in os.read(fd, 64).decode(errors="ignore"):
                    if key in ("q", "\x1b"):
                        return 0
                    state.key(key)
    finally:
        if old:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
