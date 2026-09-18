"""Shared by the model-router hooks: builds the routing context injected into a session."""
import json
import os
from pathlib import Path

RULES_MARKER = "## Model routing"
CORRECTIONS_MARKER = "Routing corrections"
FEEDBACK_FILENAME = "routing-feedback.md"
TIER_DEFAULTS = {"builder": "sonnet", "operator": "haiku", "senior_operator": "sonnet"}
ALIASES = ("haiku", "sonnet", "opus", "fable")
# additionalContext is capped at 4,000 characters per hook; each of ours stays under it.
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


def feedback_path():
    return router_dir() / FEEDBACK_FILENAME


def build_rules():
    """The routing rules with the tier models filled in, ending with where the corrections live."""
    plugin_root = Path(os.environ.get("CLAUDE_PLUGIN_ROOT") or Path(__file__).resolve().parent.parent)
    rules = (plugin_root / "rules" / "routing.md").read_text(encoding="utf-8").rstrip()
    for tier, model in tier_models().items():
        rules = rules.replace("{" + tier + "_model}", model)
    path = feedback_path()
    tail = f"Feedback file: `{path}`"
    if not path.is_file() and len(rules) + len(tail) + 20 <= MAX_CONTEXT_CHARS:
        tail += " (not created yet)"
    return (rules + "\n\n" + tail)[:MAX_CONTEXT_CHARS]


def build_corrections():
    """The user's correction rows, newest kept when they do not all fit; empty when there are none.

    Injected by its own hook so the rows get a budget of their own instead of whatever the rules leave.
    """
    path = feedback_path()
    if not path.is_file():
        return ""
    rows = correction_rows(path.read_text(encoding="utf-8"))
    if not rows:
        return ""
    # rows only: the column order is given in the rules (date, situation, routed to, should be, why)
    head = CORRECTIONS_MARKER + " (they override the routing rules):\n"
    if len(head) + len("\n".join(rows)) <= MAX_CONTEXT_CHARS:
        return head + "\n".join(rows)
    head = CORRECTIONS_MARKER + f" (latest; they override the routing rules — `{path}` has the rest):\n"
    kept, used = [], len(head)
    for row in reversed(rows):
        if used + len(row) + 1 > MAX_CONTEXT_CHARS:
            break
        kept.insert(0, row)
        used += len(row) + 1
    return head + "\n".join(kept) if kept else CORRECTIONS_MARKER + f": read `{path}` before routing."


def build_context(part):
    return build_rules() if part == "rules" else build_corrections()


def session_marker(session_id, part):
    """Marker file recording that this session already has that part of the routing context."""
    if not session_id or not isinstance(session_id, str):
        return None
    safe = "".join(ch for ch in session_id if ch.isalnum() or ch in "-_")
    return router_dir() / "sessions" / (safe + ("" if part == "rules" else "." + part)) if safe else None
