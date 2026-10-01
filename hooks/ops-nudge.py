#!/usr/bin/env python3
"""PostToolBatch / UserPromptSubmit hook: nudge the main session to delegate a long run of commands.

A main session that keeps running Bash and MCP calls itself pays for every one of them
over its whole context. This hook counts those calls since the last user prompt and, when
the count reaches a threshold, adds a short reminder to the model's context that names
the commands it saw. It never blocks and never makes a permission decision.

  count   (PostToolBatch)        add the batch's Bash/MCP calls to the per-session counter;
                                 add the reminder when the count crosses NUDGE_AT, and a
                                 shorter, firmer one when it crosses NUDGE_AT + 5
  reset   (UserPromptSubmit)     set the counter back to 0

PostToolBatch fires once per batch after every call in it has resolved, failed ones
included, so parallel tool calls cannot race the counter the way per-call PostToolUse
hooks do. State lives in <config dir>/model-router/sessions/<session_id>.ops, next to the
session markers, and is pruned with them. A payload with an `agent_id` (a subagent) is
neither counted nor nudged: tool hooks carry it inside subagents since Claude Code 2.1.286.

Local-only and fail-open: any error exits 0 with no output.

Tunable (environment):
  MODEL_ROUTER_NUDGE_AT   calls at which the first reminder fires (default 3; 0 disables);
                          the second fires at this number + 5
"""
import json
import os
import re
import sys

from router_context import session_marker

FIRST = (
    "Model routing: {n} command/MCP calls in the main session for this prompt ({seen}). "
    "If more are coming, hand the rest to model-router:operator (known commands) or "
    "model-router:senior-operator (diagnosis) in the background instead of continuing here."
)
SECOND = (
    "Model routing: {n} command/MCP calls in the main session for this prompt ({seen}). "
    "Stop; delegate the rest to model-router:operator or model-router:senior-operator."
)


def read_count(path):
    try:
        with open(path) as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def write_count(path, n):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(n))


def nudge_at():
    try:
        return int(os.environ.get("MODEL_ROUTER_NUDGE_AT", "3"))
    except ValueError:
        return 3


def is_ops(call):
    name = str(call.get("tool_name") or "")
    return name == "Bash" or name.startswith("mcp__")


def label(call):
    """A short name for one call: the command's first words, or the MCP tool name."""
    name = str(call.get("tool_name") or "")
    if name != "Bash":
        return name
    cmd = re.sub(r"\s+", " ", str((call.get("tool_input") or {}).get("command") or "")).strip()
    return " ".join(cmd.split(" ")[:3])[:40] or "Bash"


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    payload = json.load(sys.stdin)
    if payload.get("agent_id"):
        return
    path = session_marker(str(payload.get("session_id") or ""), "ops")
    if path is None:
        return
    if mode == "reset":
        write_count(path, 0)
        return
    if mode != "count":
        return
    at = nudge_at()
    if at <= 0:
        return
    calls = [c for c in payload.get("tool_calls") or [] if isinstance(c, dict) and is_ops(c)]
    if not calls:
        return
    before = read_count(path)
    n = before + len(calls)
    write_count(path, n)
    seen = ", ".join(dict.fromkeys(label(c) for c in calls[-3:]))
    if before < at <= n:
        text = FIRST.format(n=n, seen=seen)
    elif before < at + 5 <= n:
        text = SECOND.format(n=n, seen=seen)
    else:
        return
    event = payload.get("hook_event_name") or "PostToolBatch"
    json.dump({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}, sys.stdout)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
