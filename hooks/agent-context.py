#!/usr/bin/env python3
"""SubagentStart hook: give the plugin's agents the context the main session would otherwise type.

Operators cannot ask and do not load CLAUDE.md, so every launch needs the repository path,
the branch and the scratchpad path. The main session is told to supply them, but it forgets
under a long conversation, and a "missing input, stopping" report costs a launch plus a
round trip. This hook adds three short lines to every model-router agent at start: the
working directory with its git branch, the scratchpad directory for saved output, and the
reminder that CLAUDE.md is not loaded. Other agent types are untouched.

Local-only and fail-open: any error exits 0 with no output (MODEL_ROUTER_DEBUG=1 re-raises it).
The git call is bounded to two seconds.
"""
import json
import os
import subprocess
import sys

AGENTS = {"model-router:operator", "model-router:senior-operator", "model-router:builder", "model-router:coordinator"}


def branch(cwd):
    try:
        proc = subprocess.run(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=2)
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def main():
    payload = json.load(sys.stdin)
    if payload.get("agent_type") not in AGENTS:
        return
    lines = []
    cwd = payload.get("cwd") or os.getcwd()
    head = branch(cwd)
    lines.append("Working directory: {0}{1}".format(cwd, " (git branch {0})".format(head) if head else ""))
    scratch = payload.get("scratchpad_dir")
    if scratch:
        lines.append("Scratchpad for saved output: {0}".format(scratch))
    if payload.get("agent_type") in ("model-router:operator", "model-router:senior-operator"):
        lines.append("CLAUDE.md is not loaded for you; the task carries every rule that binds the command.")
    json.dump({"hookSpecificOutput": {"hookEventName": "SubagentStart", "additionalContext": "\n".join(lines)}}, sys.stdout)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        if os.environ.get("MODEL_ROUTER_DEBUG"):
            raise
    sys.exit(0)
