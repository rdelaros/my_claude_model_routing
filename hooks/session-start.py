#!/usr/bin/env python3
"""SessionStart hook: inject the model-routing rules, or the user's routing corrections.

Run once per part (`rules`, `corrections`): a hook's additionalContext is capped at 8,000
characters, so each part is its own hook with its own budget. It fires on startup, resume,
clear, compact and fork; Claude Code drops a SessionStart context that is already in the
loaded conversation, so re-injecting unchanged rules on resume costs nothing, and changed
rules or new corrections are picked up.

Local-only and fail-open: no network, and any error exits 0 with no output so a
broken hook never blocks a session.

Contract (Claude Code SessionStart hook):
  stdin : JSON with session_id
  stdout: {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "..."}}
"""
import json
import sys

from router_context import build_context, ensure_feedback_file, session_marker


def main():
    part = sys.argv[1] if len(sys.argv) > 1 else "rules"
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        payload = {}

    context = build_context(part)
    if context:
        json.dump(
            {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}},
            sys.stdout,
        )

    # lets ensure-rules.py skip this session without scanning its transcript
    marker = session_marker(payload.get("session_id"), part)
    if marker:
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
        except OSError:
            pass
    if part == "rules":
        try:
            ensure_feedback_file()  # with its header, so the first correction is a plain row
        except OSError:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
