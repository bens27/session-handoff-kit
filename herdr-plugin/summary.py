"""Pane: the summary auto-handoff showed just before it cleared the agent pane.

The context-watch Stop hook writes the file named by HANDOFF_SUMMARY_FILE and
opens this pane beside the agent. The pane stays until q or Escape and shows a
newer summary when the next automatic handoff rewrites the file.
"""
import json
import os
import select
import sys
import termios
import tty

PATH = os.environ.get("HANDOFF_SUMMARY_FILE") or ""
DIM, BOLD, RESET = "\x1b[2m", "\x1b[1m", "\x1b[0m"


def render(path):
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        summary = str(doc.get("summary") or "").strip()
    except (OSError, ValueError, AttributeError):
        doc, summary = {}, ""
    lines = [BOLD + "Handoff summary" + RESET]
    if doc.get("checkpoint"):
        lines.append(DIM + "checkpoint: %s" % doc["checkpoint"] + RESET)
    lines += ["", summary or "No saved auto-handoff summary to show."]
    return "\n".join(lines) + "\n"


def stamp(path):
    try:
        st = os.stat(path)
        return st.st_mtime_ns, st.st_size
    except OSError:
        return None


def claim(path):
    """Record this pane's pid beside the summary, so the hook reuses the pane."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("%d\n" % os.getpid())
    except OSError:
        pass


def release(path):
    try:
        with open(path) as f:
            mine = f.read().strip() == str(os.getpid())
        if mine:
            os.remove(path)
    except OSError:
        pass


def main():
    if not sys.stdin.isatty():
        sys.stdout.write(render(PATH))
        return 0
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    pid_file = PATH + ".pid"
    if PATH:
        claim(pid_file)
    try:
        tty.setcbreak(fd)
        shown, seen = False, None
        while True:
            now = stamp(PATH)
            if not shown or now != seen:
                # Normal screen, not the alternate one, so a long summary scrolls back.
                sys.stdout.write("\x1b[2J\x1b[3J\x1b[H" + render(PATH).replace("\n", "\r\n")
                                 + "\r\n" + DIM + "q closes this pane" + RESET + "\r\n")
                sys.stdout.flush()
                shown, seen = True, now
            if select.select([sys.stdin], [], [], 1.0)[0]:
                keys = os.read(fd, 64).decode(errors="ignore")
                if "q" in keys or keys == "\x1b":
                    return 0
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        if PATH:
            release(pid_file)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
