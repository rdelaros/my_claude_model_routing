#!/usr/bin/env python3
"""SessionStart hook: inject the model-routing rules and the user's routing corrections.

Local-only and fail-open: no network, and any error exits 0 with no output so a
broken hook never blocks a session.

Contract (Claude Code SessionStart hook):
  stdin : JSON with session_id
  stdout: {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "..."}}
"""
import json
import sys

from router_context import build_context, session_marker


def main():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        payload = {}

    json.dump(
        {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": build_context()}},
        sys.stdout,
    )

    # lets ensure-rules.py skip this session without scanning its transcript
    marker = session_marker(payload.get("session_id"))
    if marker:
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
        except OSError:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
