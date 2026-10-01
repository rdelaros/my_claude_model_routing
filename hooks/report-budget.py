#!/usr/bin/env python3
"""SubagentStop hook: keep what a subagent sends back to the main session short.

A subagent's final message is copied into the main conversation and billed again on every
later turn. When it is longer than the budget, this hook saves the full text to a file and
sends the subagent back once to answer with a summary that ends with the file's path.
Nothing is lost: the main session reads the file only when it needs the detail.

A result whose first line is exactly `REPORT FOR USER` is let through up to four times the
budget: that is a finished report the main session shows to the user as is (a task list, a
PR table), which a summary would destroy. The operators are told to use the marker only
for such reports.

Saved reports can hold anything a command printed — tokens, connection strings, plan
values — so the folder and the files are created readable by the owner only, and they are
deleted after 14 days.

Local-only and fail-open: any error exits 0 with no output, so a broken hook never holds
a subagent back. It asks once per stop — a second long answer is let through.

Tunables (environment):
  MODEL_ROUTER_REPORT_CHARS    budget in characters (default 4000, about 1,000 tokens); 0 disables
  MODEL_ROUTER_REPORT_EXEMPT   comma-separated agent types that may report at any length
"""
import json
import os
import sys
import time

from router_context import router_dir

STALE_SECONDS = 14 * 24 * 3600
USER_REPORT_MARKER = "REPORT FOR USER"
USER_REPORT_FACTOR = 4


def env_int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def last_message(transcript):
    """Text of the subagent's final message, for versions that do not pass it in the payload."""
    text = ""
    with open(transcript, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") != "assistant":
                continue
            content = d.get("message", {}).get("content")
            if isinstance(content, str):
                parts = [content]
            else:
                parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"] if isinstance(content, list) else []
            if any(parts):
                text = "\n".join(parts)
    return text


def forget_old_reports(folder):
    cutoff = time.time() - STALE_SECONDS
    for entry in os.scandir(folder):
        if entry.is_file() and entry.stat().st_mtime < cutoff:
            os.unlink(entry.path)


def save_report(report, agent_id):
    folder = router_dir() / "reports"
    folder.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(folder, 0o700)
    except OSError:
        pass
    safe = "".join(ch for ch in str(agent_id or "") if ch.isalnum() or ch in "-_")[:80] or "agent"
    path = folder / f"{safe}-{int(time.time())}.md"
    n = 1
    while path.exists():
        n += 1
        path = folder / f"{safe}-{int(time.time())}-{n}.md"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(report)
    forget_old_reports(folder)
    return path


def main():
    budget = env_int("MODEL_ROUTER_REPORT_CHARS", 4000)
    payload = json.load(sys.stdin)
    if budget <= 0 or payload.get("stop_hook_active"):
        return
    exempt = {a.strip() for a in os.environ.get("MODEL_ROUTER_REPORT_EXEMPT", "").split(",") if a.strip()}
    if payload.get("agent_type") in exempt:
        return

    report = payload.get("last_assistant_message")
    if not isinstance(report, str):
        transcript = payload.get("agent_transcript_path")
        report = last_message(transcript) if transcript and os.path.isfile(transcript) else ""
    if len(report) <= budget:
        return
    if report.lstrip().split("\n", 1)[0].strip() == USER_REPORT_MARKER and len(report) <= USER_REPORT_FACTOR * budget:
        return

    path = save_report(report, payload.get("agent_id"))
    reason = (
        f"Your report is {len(report):,} characters; the budget is {budget:,}, because it is copied into the caller's "
        f"conversation and billed on every later turn. The full text is already saved at {path} — do not rewrite or "
        f"re-save it, and run no more tools. Reply now with a summary under {budget // 2:,} characters: the conclusion "
        f"first, then only what the caller needs to decide or act (exact ids, paths, numbers, blockers). "
        f"End with the line: Full report: {path}"
    )
    json.dump({"decision": "block", "reason": reason}, sys.stdout)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
