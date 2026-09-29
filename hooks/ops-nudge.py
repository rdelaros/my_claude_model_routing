#!/usr/bin/env python3
"""PostToolUse / UserPromptSubmit hook: nudge the main session to delegate a long run of commands.

A main session that keeps running Bash and MCP calls itself pays for every one of them
over its whole context. This hook counts those calls since the last user prompt and, when
the count reaches a threshold, adds a short reminder to the model's context. It never
blocks and never makes a permission decision.

  count   (PostToolUse, matcher Bash|mcp__.*)  increment the per-session counter; add the
          reminder at exactly NUDGE_AT calls, and a shorter, firmer one at NUDGE_AT + 5
  reset   (UserPromptSubmit)                   set the counter back to 0

State lives in <tempdir>/model-router/ops-<session_id>. A payload with a non-empty
`agent_id` (a subagent) is neither counted nor nudged.

Verified in the installed Claude Code 2.1.284 bundle: PostToolUse supports
hookSpecificOutput.additionalContext. agent_id / agent_type are documented for
SubagentStart input, but could NOT be confirmed for tool-call hooks made inside a
subagent. So the hook skips when agent_id is present AND the text is phrased "If you are
the main session", so a subagent that does receive it knows to ignore it.

Local-only and fail-open: any error exits 0 with no output.

Tunable (environment):
  MODEL_ROUTER_NUDGE_AT   calls at which the first reminder fires (default 3; 0 disables);
                          the second fires at this number + 5
"""
import json
import os
import re
import sys
import tempfile

FIRST = (
    "Model routing: if you are the main session, this is command/MCP call {n} for this prompt. "
    "If more are coming, hand the rest to model-router:operator (known commands) or "
    "model-router:senior-operator (diagnosis) in the background instead of continuing here. "
    "Subagents: ignore this."
)
SECOND = (
    "If you are the main session: {n} command/MCP calls for this prompt. "
    "Stop; delegate the rest to model-router:operator or model-router:senior-operator."
)


def state_path(session_id):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)
    return os.path.join(tempfile.gettempdir(), "model-router", "ops-" + safe)


def read_count(path):
    try:
        with open(path) as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def write_count(path, n):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(str(n))


def nudge_at():
    try:
        return int(os.environ.get("MODEL_ROUTER_NUDGE_AT", "3"))
    except ValueError:
        return 3


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    payload = json.load(sys.stdin)
    session_id = str(payload.get("session_id") or "")
    if not session_id or payload.get("agent_id"):
        return
    path = state_path(session_id)
    if mode == "reset":
        write_count(path, 0)
        return
    if mode != "count":
        return
    at = nudge_at()
    if at <= 0:
        return
    n = read_count(path) + 1
    write_count(path, n)
    if n == at:
        text = FIRST.format(n=n)
    elif n == at + 5:
        text = SECOND.format(n=n)
    else:
        return
    json.dump({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}}, sys.stdout)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
