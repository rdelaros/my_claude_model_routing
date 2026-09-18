#!/usr/bin/env python3
"""UserPromptSubmit hook: give the session the routing rules (or corrections) if SessionStart never did.

SessionStart only fires when a session begins, so a session that was already open when
the plugin was installed, enabled or reloaded has no rules. This hook injects them on
the next prompt, once per session. Run once per part (`rules`, `corrections`), like
session-start.py, because each hook's additionalContext has its own 4,000-character cap.

Local-only and fail-open: any error exits 0 with no output so a broken hook never
blocks a prompt.
"""
import json
import os
import sys
import time

from router_context import CORRECTIONS_MARKER, RULES_MARKER, build_context, session_marker

STALE_SECONDS = 14 * 24 * 3600


def transcript_has(path, part):
    """True if a hook already injected that part (the record, not a mention of the heading)."""
    needle, record = (RULES_MARKER if part == "rules" else CORRECTIONS_MARKER).encode(), b"hook_additional_context"
    with open(path, "rb") as f:
        return any(needle in line and record in line for line in f)


def forget_old_sessions(folder):
    cutoff = time.time() - STALE_SECONDS
    for entry in os.scandir(folder):
        if entry.is_file() and entry.stat().st_mtime < cutoff:
            os.unlink(entry.path)


def main():
    part = sys.argv[1] if len(sys.argv) > 1 else "rules"
    payload = json.load(sys.stdin)
    marker = session_marker(payload.get("session_id"), part)
    if marker is None or marker.exists():
        return

    transcript = payload.get("transcript_path")
    already = bool(transcript) and os.path.isfile(transcript) and transcript_has(transcript, part)
    context = "" if already else build_context(part)
    if context:
        json.dump(
            {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}},
            sys.stdout,
        )

    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    forget_old_sessions(marker.parent)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
