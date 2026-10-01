#!/usr/bin/env python3
"""PreToolUse hook on Bash: stop the operators from writing files through the shell.

The operator, senior operator and coordinator have no Edit or Write tool and are told not
to change files, but Bash can: a redirection, `tee`, `sed -i`, `cp`, `git apply`. This hook
is a safety net behind that rule — it catches the common shell write forms, not every
possible one, so it is not a sandbox. Inside one of those agents (the payload's
`agent_type`) it denies a command that writes outside the scratch and temp directories,
with a reason that tells the agent to report what should change instead. Agent-scoped
`hooks:` and `permissionMode:` in a plugin's agent files are ignored by Claude Code, so a
plugin-level hook keyed on the agent type is the only place this can live.

Caught: redirections and `tee` (also behind `sudo`, `xargs`, a path or a `VAR=x` prefix),
in-place editors (`sed -i`, `perl -i`, `patch`, `truncate`), file commands with a target
outside the allowed directories (`cp`, `mv`, `install`, `rsync`, `rm`, `touch`, `mkdir`,
`ln`, `chmod`, `chown`, `dd of=`, `curl -o`, `wget -O`, `tar x`, `unzip`), git commands
that rewrite the working tree or history (`apply`, `am`, `restore`, `rm`, `mv`,
`checkout --|.|-f|-p`, `switch -C|--discard-changes`, `reset --hard`, `clean`,
`branch -D|-M`, `push --force|-f|+ref`), one-line interpreter scripts that open a file for
writing, and `bash -c`/`eval` strings that contain any of the above.

Allowed anyway: redirections to /dev/null, /dev/tty or a file descriptor, and writes under
the session's scratchpad directory or the system temp directory — the operators use those
to park a long diff or log for the main session to read in pieces. Quoted text and heredoc
bodies are not commands, so a `>` inside them is ignored. The main session and the builder
are never touched.

Local-only and fail-open: an unexpected shape or any error exits 0 with no output
(set MODEL_ROUTER_DEBUG=1 to see the traceback instead).

Tunable (environment):
  MODEL_ROUTER_GUARD=0   disable the guard
"""
import json
import os
import re
import sys
import tempfile

GUARDED = {"model-router:operator", "model-router:senior-operator", "model-router:coordinator"}
MAX_DEPTH = 2

# where a command may start: line start, after a separator or substitution, behind a wrapper,
# a `VAR=value` assignment or a directory prefix
START = (r"(?:^|[|;&(`{]\s*|\$\(\s*|\b(?:sudo|doas|nice|time|env|command|exec|xargs|then|do|else|if|while|until|-exec|-execdir)\s+(?:-\S+\s+)*)"
         r"(?:[A-Za-z_]\w*=\S*\s+)*(?:\S*/)?")
SEP = r"[;|&()`]"  # ends a token list
ARG = r"[^\s;|&()`]+"
GIT = r"git(?:\s+(?:-[cC]\s*\S+|--git-dir=\S+|--work-tree=\S+|--no-pager))*\s+"

EDITORS = re.compile(START + r"(?:"
    r"g?sed\s+(?:-\S+\s+)*-(?:[a-zA-Z]*i\b|-in-place\b)"
    r"|perl\s+(?:-\S+\s+)*-[a-zA-Z]*i\b"
    r"|patch\b|truncate\b|shred\b|sponge\b"
    r"|" + GIT + r"(?:apply\b|am\b|restore\b|rm\b|mv\b|clean\b|reset\s+--hard\b|branch\s+-[DM]\b"
    r"|checkout\s+(?:\S+\s+)*(?:--(?:\s|$)|\.(?:\s|$)|-f\b|--force\b|-p\b|--patch\b)"
    r"|switch\s+(?:\S+\s+)*(?:-C\b|-c\b|--force-create\b|--discard-changes\b|-f\b|--force\b)"
    r"|push\s+(?:\S+\s+)*(?:-\w*f\w*\b|--force\b|--force-with-lease\b|\+\S+))"
    r")")
# commands whose last argument is the destination
DEST_LAST = re.compile(START + r"(?:cp|mv|install|rsync|scp)\s+((?:" + ARG + r"\s+)*" + ARG + r")")
# commands where every non-option argument is a target
ALL_ARGS = re.compile(START + r"(?:rm|rmdir|unlink|touch|mkdir|ln|chmod|chown|chgrp|mkfifo)\s+((?:" + ARG + r"\s*)+)")
DD = re.compile(START + r"dd\s+(?:\S+\s+)*of=(" + ARG + r")")
FETCH = re.compile(START + r"(?:curl|wget)\s+(?:\S+\s+)*(?:-o|-O|--output|--output-document)[= ]\s*(" + ARG + r")")
EXTRACT = re.compile(START + r"(?:tar\s+(?:-?\S*x\S*|--extract)|unzip|7z\s+x|gunzip|bunzip2|unxz)\b(.*)")
EXTRACT_DEST = re.compile(r"(?:-C|-d|--directory)[= ]\s*(" + ARG + r")")
ONE_LINER = re.compile(START + r"(?:python[0-9.]*|node|nodejs|perl|ruby|php)\s+(?:-\S+\s+)*-[a-zA-Z]*[ce]\b")
WRITE_CALLS = re.compile(r"open\([^)]*['\"][rbt]*[wax+]|\.write_(?:text|bytes)\(|writeFile(?:Sync)?\(|appendFile(?:Sync)?\(|copyFile(?:Sync)?\(|renameSync?\(|unlinkSync?\(|rmSync\(|mkdirSync?\("
                         r"|savetxt\(|to_csv\(|to_json\(|to_excel\(|shutil\.(?:copy|move|rmtree)|os\.(?:remove|rename|unlink|makedirs|mkdir|replace)\(|File\.(?:open|write)\(|FileUtils")
SHELL_C = re.compile(START + r"(?:bash|sh|zsh|dash|ksh)\s+(?:-\S+\s+)*-c\s+|" + START + r"eval\s+")
REDIRECT = re.compile(r"(?<![<>\-])(?:\d?>>?|&>>?)(?!\()\s*(&?" + ARG + r")")
TEE = re.compile(START + r"tee\s+((?:" + ARG + r"\s*)+)")
HEREDOC = re.compile(r"<<-?\s*(?:'(\w+)'|\"(\w+)\"|(\w+))")
REASON = "model-router: {agent} does not change files ({what}). Report what should change instead, or write under the scratchpad directory."


def blank_heredocs(cmd):
    """Heredoc bodies are data, not commands: blank them, keeping every position."""
    out = cmd
    for m in list(HEREDOC.finditer(cmd)):
        word = m.group(1) or m.group(2) or m.group(3)
        line_end = cmd.find("\n", m.end())
        if line_end < 0:
            break
        pos = line_end + 1
        while pos <= len(cmd):
            nxt = cmd.find("\n", pos)
            line = cmd[pos:] if nxt < 0 else cmd[pos:nxt]
            if line.strip("\t ") == word:
                end = len(cmd) if nxt < 0 else nxt
                out = out[: line_end + 1] + re.sub(r"[^\n]", " ", cmd[line_end + 1:end]) + out[end:]
                break
            if nxt < 0:
                out = out[: line_end + 1] + re.sub(r"[^\n]", " ", cmd[line_end + 1:])
                break
            pos = nxt + 1
    return out


def blank_quotes(cmd):
    """Quoted text cannot redirect: fill it with one non-space character so that a quoted
    target stays a single token and every position is kept."""
    cmd = re.sub(r"'[^']*'", lambda m: "'" + "_" * (len(m.group(0)) - 2) + "'", cmd)
    return re.sub(r'"(?:\\.|[^"\\])*"', lambda m: '"' + "_" * (len(m.group(0)) - 2) + '"', cmd)


def allowed_prefixes(payload):
    out = []
    for p in (payload.get("scratchpad_dir"), os.environ.get("TMPDIR"), os.environ.get("TEMP"), os.environ.get("TMP"), tempfile.gettempdir(), "/tmp", "/dev/shm"):
        if p:
            out.append(os.path.normpath(str(p)).replace("\\", "/").rstrip("/") + "/")
    return out


def expand(target):
    tmp = os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP") or tempfile.gettempdir()
    target = re.sub(r"\$\{?(?:TMPDIR|TEMP|TMP)\}?", tmp.replace("\\", "/"), target)
    return os.path.expanduser(target)


def target_allowed(target, prefixes):
    t = target.strip("'\"")
    if not t or t.startswith("&") or t.startswith(">") or t.startswith("<") or t in ("/dev/null", "/dev/stderr", "/dev/stdout", "/dev/tty"):
        return True  # a descriptor, a device, or a process substitution
    t = expand(t)
    if "$" in t or "`" in t or "{" in t:
        return False  # a variable or placeholder we cannot resolve
    t = os.path.normpath(t).replace("\\", "/")
    return any(t.startswith(p) or t + "/" == p for p in prefixes)


def args_of(text):
    return [a for a in re.split(r"\s+", text.strip()) if a and not a.startswith("-")]


def violation(command, prefixes, depth=0):
    """A short description of the first write found in `command`, or None."""
    bare = blank_quotes(blank_heredocs(command))
    span = lambda m, g=1: command[m.start(g):m.end(g)]
    m = EDITORS.search(bare)
    if m:
        return m.group(0).strip(" |;&(`{")
    for pat in (REDIRECT,):
        for hit in pat.finditer(bare):
            if not target_allowed(span(hit), prefixes):
                return bare[hit.start():hit.start(1)] + span(hit)
    for hit in TEE.finditer(bare):
        for a in args_of(span(hit)):
            if not target_allowed(command[hit.start(1) + span(hit).find(a):][: len(a)], prefixes):
                return "tee " + a
    for hit in DEST_LAST.finditer(bare):
        args = args_of(span(hit))
        if args and not target_allowed(command[hit.start(1) + span(hit).rfind(args[-1]):][: len(args[-1])], prefixes):
            return hit.group(0).strip(" |;&(`{")[:60]
    for hit in ALL_ARGS.finditer(bare):
        for a in args_of(span(hit)):
            if not target_allowed(command[hit.start(1) + span(hit).find(a):][: len(a)], prefixes):
                return hit.group(0).strip(" |;&(`{")[:60]
    for pat in (DD, FETCH):
        for hit in pat.finditer(bare):
            if not target_allowed(span(hit), prefixes):
                return hit.group(0).strip(" |;&(`{")[:60]
    for hit in EXTRACT.finditer(bare):
        dest = EXTRACT_DEST.search(span(hit))
        if not dest or not target_allowed(command[hit.start(1) + dest.start(1):hit.start(1) + dest.end(1)], prefixes):
            return hit.group(0).strip(" |;&(`{")[:60]
    if ONE_LINER.search(bare) and WRITE_CALLS.search(command):
        return "a script that writes files"
    if depth < MAX_DEPTH:
        for hit in SHELL_C.finditer(bare):
            inner = re.match(r"\s*(?:'([^']*)'|\"((?:\\.|[^\"\\])*)\")", command[hit.end():])
            if inner:
                found = violation(inner.group(1) or inner.group(2) or "", prefixes, depth + 1)
                if found:
                    return found
    return None


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
    what = violation(command, allowed_prefixes(payload))
    if not what:
        return
    json.dump({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                      "permissionDecisionReason": REASON.format(agent=agent, what=what[:80])}}, sys.stdout)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        if os.environ.get("MODEL_ROUTER_DEBUG"):
            raise
    sys.exit(0)
