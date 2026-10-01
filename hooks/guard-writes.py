#!/usr/bin/env python3
"""PreToolUse hook on Bash: stop the operators from writing files through the shell.

The operator, senior operator and coordinator have no Edit or Write tool and are told not
to change files, but Bash can: a redirection, `tee`, `sed -i`, `git apply`. This hook is
the enforcement behind the rule. Inside one of those agents (the payload's `agent_type`)
it denies a command that writes outside the scratch and temp directories, with a reason
that tells the agent to report what should change instead. Agent-scoped `hooks:` and
`permissionMode:` in a plugin's agent files are ignored by Claude Code, so a plugin-level
hook keyed on the agent type is the only place this can live.

Allowed anyway: redirections to /dev/null or a file descriptor, and writes under the
session's scratchpad directory or the system temp directory — the operators use those to
park a long diff or log for the main session to read in pieces. The main session and the
builder are never touched.

Local-only and fail-open: an unexpected shape or any error exits 0 with no output.

Tunable (environment):
  MODEL_ROUTER_GUARD=0   disable the guard
"""
import json
import os
import re
import sys
import tempfile

GUARDED = {"model-router:operator", "model-router:senior-operator", "model-router:coordinator"}

# in-place editors, patch appliers and git commands that rewrite the working tree or history
EDITORS = re.compile(
    r"(?:^|[|;&(]\s*|\bsudo\s+)(?:"
    r"(?:sed|perl)\s+(?:-\S+\s+)*-(?:[a-zA-Z]*i\b|-in-place\b)"
    r"|git\s+(?:apply\b|restore\b|checkout\s+(?:--\s|-f\b|--force\b)|reset\s+--hard\b|clean\b|branch\s+-D\b|push\s+(?:\S+\s+)*(?:-f\b|--force\b|--force-with-lease\b))"
    r"|patch\s"
    r"|truncate\s"
    r")"
)
REDIRECT = re.compile(r"(?<![<>])(?:\d?>>?|&>>?)\s*(\S+)")
TEE = re.compile(r"(?:^|\|)\s*tee\s+(?:-\S+\s+)*(\S+)")
REASON = "model-router: {agent} does not change files ({what}). Report what should change instead, or write under the scratchpad directory."


def strip_quotes(cmd):
    """Quoted text cannot redirect, so it is blanked before matching (keeps positions)."""
    cmd = re.sub(r"'[^']*'", lambda m: "'" + " " * (len(m.group(0)) - 2) + "'", cmd)
    return re.sub(r'"(?:\\.|[^"\\])*"', lambda m: '"' + " " * (len(m.group(0)) - 2) + '"', cmd)


def allowed_prefixes(payload):
    out = []
    for p in (payload.get("scratchpad_dir"), os.environ.get("TMPDIR"), os.environ.get("TEMP"), os.environ.get("TMP"), tempfile.gettempdir(), "/tmp", "/dev/shm"):
        if p:
            out.append(os.path.normpath(str(p)).replace("\\", "/").rstrip("/") + "/")
    return out


def target_allowed(target, prefixes):
    t = target.strip("'\"")
    if t.startswith("&") or t in ("/dev/null", "/dev/stderr", "/dev/stdout"):
        return True
    if t.startswith("$TMPDIR/") or t.startswith("${TMPDIR}/") or t.startswith("$TEMP/") or t.startswith("${TEMP}/"):
        return True
    t = os.path.normpath(os.path.expanduser(t)).replace("\\", "/")
    return any(t.startswith(p) or t + "/" == p for p in prefixes)


def main():
    if os.environ.get("MODEL_ROUTER_GUARD") == "0":
        return
    payload = json.load(sys.stdin)
    agent = str(payload.get("agent_type") or "")
    if payload.get("tool_name") != "Bash" or agent not in GUARDED:
        return
    command = str((payload.get("tool_input") or {}).get("command") or "")
    if not command.strip():
        return
    bare = strip_quotes(command)
    what = None
    m = EDITORS.search(bare)
    if m:
        what = m.group(0).strip(" |;&(")
    else:
        prefixes = allowed_prefixes(payload)
        for pat in (REDIRECT, TEE):
            for hit in pat.finditer(bare):
                if not target_allowed(hit.group(1), prefixes):
                    what = hit.group(0).strip()
                    break
            if what:
                break
    if not what:
        return
    json.dump({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                      "permissionDecisionReason": REASON.format(agent=agent, what=what[:80])}}, sys.stdout)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
