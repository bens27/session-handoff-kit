"""Open the thresholds popup for the focused agent pane."""
import json
import os
import subprocess
import sys

HERDR = os.environ.get("HERDR_BIN_PATH") or "herdr"
PLUGIN_ID = os.environ.get("HERDR_PLUGIN_ID") or "session-handoff.thresholds"


def focused_pane():
    try:
        ctx = json.loads(os.environ.get("HERDR_PLUGIN_CONTEXT_JSON") or "{}")
    except ValueError:
        ctx = {}
    for key in ("pane_id", "focused_pane_id"):
        if isinstance(ctx.get(key), str):
            return ctx[key]
    pane = ctx.get("pane") or ctx.get("focused_pane")
    if isinstance(pane, dict) and pane.get("pane_id"):
        return pane["pane_id"]
    return snapshot().get("focused_pane_id")


def snapshot():
    out = subprocess.run([HERDR, "api", "snapshot"], capture_output=True, text=True, check=True).stdout
    return json.loads(out)["result"]["snapshot"]


def main():
    pane = focused_pane()
    if not pane:
        print("no focused pane", file=sys.stderr)
        return 1
    if not any(p.get("pane_id") == pane and p.get("agent_session") for p in snapshot().get("panes", [])):
        print("pane %s runs no agent; focus an agent pane first" % pane, file=sys.stderr)
        return 1
    subprocess.run([HERDR, "plugin", "pane", "open", "--plugin", PLUGIN_ID, "--entrypoint", "thresholds",
                    "--focus", "--env", "HANDOFF_TARGET_PANE=" + pane], check=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
