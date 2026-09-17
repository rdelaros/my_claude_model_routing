#!/usr/bin/env python3
"""SessionStart hook: inject the model-routing rules and the user's routing corrections.

Local-only and fail-open: no network, no writes, and any error exits 0 with no
output so a broken hook never blocks a session.

Contract (Claude Code SessionStart hook):
  stdin : JSON (drained, content unused)
  stdout: {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "..."}}
"""
import json
import os
import sys
from pathlib import Path

FEEDBACK_FILENAME = "routing-feedback.md"
TIER_DEFAULTS = {"builder": "sonnet", "operator": "haiku"}
ALIASES = ("haiku", "sonnet", "opus", "fable")
# additionalContext is capped at 4,000 characters; stay under it.
MAX_CONTEXT_CHARS = 3900


def router_dir():
    """Per config directory, so each provider setup keeps its own tier models and corrections."""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "model-router"


def tier_models():
    """Tier -> model alias, as saved by the setup skill; defaults when unset or invalid."""
    try:
        saved = json.loads((router_dir() / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    return {tier: saved.get(tier) if saved.get(tier) in ALIASES else default for tier, default in TIER_DEFAULTS.items()}


def correction_rows(text):
    """Table data rows, oldest first — header and separator rows excluded."""
    rows = [line.strip() for line in text.splitlines() if line.strip().startswith("|")]
    return [r for r in rows[1:] if not set(r) <= set("|-: ")]


def feedback_section(path, budget):
    head = f"Feedback file: `{path}`"
    if not path.is_file():
        return head + " (not created yet)"
    rows = correction_rows(path.read_text(encoding="utf-8"))
    if not rows:
        return head + " (no corrections yet)"

    # rows only: the column order is given in the rules (date, situation, routed to, should be, why)
    full = head + "\nCorrections:\n"
    trimmed = head + "\nLatest corrections (read the file for the rest):\n"
    if len(full) + len("\n".join(rows)) <= budget:
        return full + "\n".join(rows)

    kept = []
    used = len(trimmed)
    for row in reversed(rows):
        if used + len(row) + 1 > budget:
            break
        kept.insert(0, row)
        used += len(row) + 1
    if not kept:
        return head + " (has corrections — read it before routing)"
    return trimmed + "\n".join(kept)


def main():
    try:
        sys.stdin.read()
    except Exception:
        pass

    plugin_root = Path(os.environ.get("CLAUDE_PLUGIN_ROOT") or Path(__file__).resolve().parent.parent)
    rules = (plugin_root / "rules" / "routing.md").read_text(encoding="utf-8").rstrip()
    for tier, model in tier_models().items():
        rules = rules.replace("{" + tier + "_model}", model)

    path = router_dir() / FEEDBACK_FILENAME
    budget = MAX_CONTEXT_CHARS - len(rules) - 2
    section = feedback_section(path, budget)
    if len(section) > budget:
        section = f"Feedback file: `{path}`"
    context = (rules + "\n\n" + section)[:MAX_CONTEXT_CHARS]

    json.dump(
        {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}},
        sys.stdout,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
