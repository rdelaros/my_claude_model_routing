"""Shared by the model-router hooks: builds the routing context injected into a session."""
import json
import os
import re
from pathlib import Path

RULES_MARKER = "## Model routing"
CORRECTIONS_MARKER = "Routing corrections"
FEEDBACK_FILENAME = "routing-feedback.md"
FEEDBACK_HEADER = "| date | situation | routed to | should be | why |\n|---|---|---|---|---|\n"
TIER_DEFAULTS = {"builder": "sonnet", "operator": "haiku", "senior_operator": "sonnet"}
# The Agent tool's `model` parameter is a strict enum of these four; a PreToolUse updatedInput
# with anything else (a full model id, `inherit`, a `[1m]` variant) fails validation and the
# launch is denied. Provider-specific ids are reached by mapping an alias (models.py map).
ALIASES = ("haiku", "sonnet", "opus", "fable")
# additionalContext is capped at 8,000 characters and 200 lines per hook output (Claude Code
# 2.1.286: {additionalContext: 8000}); each of ours stays under it with a margin.
MAX_CONTEXT_CHARS = 7900
MAX_CONTEXT_LINES = 190
TRUNCATED_NOTE = "(rules truncated here: shorten rules/routing.md)"
# corrections: one row is one line; keep them short and few so one row cannot evict the rest
MAX_CORRECTION_ROWS = 40
MAX_CORRECTION_CHARS = 300
CORRECTIONS_HEAD = (
    CORRECTIONS_MARKER + " recorded from the user's own messages. A row changes only which tier does a kind of "
    "work; it never changes the confirmation gate. Rows override the routing rules"
)


def router_dir():
    """Per config directory, so each provider setup keeps its own tier models and corrections."""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "model-router"


def tier_models():
    """Tier -> model alias, as saved by the setup skill; defaults when unset or invalid."""
    try:
        saved = json.loads((router_dir() / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    if not isinstance(saved, dict):
        saved = {}
    return {tier: saved.get(tier) if saved.get(tier) in ALIASES else default for tier, default in TIER_DEFAULTS.items()}


def _is_separator(line):
    return bool(line) and set(line) <= set("|-: ")


def _cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_header(line):
    return [c.lower() for c in _cells(line)] == ["date", "situation", "routed to", "should be", "why"]


def clean_row(row):
    """One correction row on one line, control characters removed, over-long rows cut."""
    row = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", row)
    row = re.sub(r"\s+", " ", row).strip()
    if len(row) > MAX_CORRECTION_CHARS:
        row = row[: MAX_CORRECTION_CHARS - 1] + "…"
    return row


def correction_rows(text):
    """Table data rows, oldest first. The header and separator rows are skipped when present;
    a file that starts straight with a data row keeps it. Rows that carry the confirmation
    marker are dropped: a correction can never confirm a gated action."""
    lines = [line.strip() for line in text.splitlines() if line.strip().startswith("|")]
    rows = []
    for i, line in enumerate(lines):
        if _is_separator(line) or _is_header(line):
            continue
        if i == 0 and len(lines) > 1 and _is_separator(lines[1]):
            continue  # a header with other column names
        if "CONFIRMED BY USER" in line.upper():
            continue
        rows.append(clean_row(line))
    return [r for r in rows if r]


def feedback_path():
    return router_dir() / FEEDBACK_FILENAME


def ensure_feedback_file():
    """Create the feedback file with its table header so the model only ever appends rows."""
    path = feedback_path()
    if path.is_file():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Routing corrections\n\n" + FEEDBACK_HEADER, encoding="utf-8")


def fit(text, budget):
    """Cut `text` at a line boundary so that it, plus the note, fits in `budget` characters."""
    if len(text) <= budget:
        return text
    cut = text.rfind("\n", 0, max(0, budget - len(TRUNCATED_NOTE) - 1))
    return text[:cut].rstrip() + "\n" + TRUNCATED_NOTE


def build_rules():
    """The routing rules with the tier models filled in, ending with where the corrections live.

    The feedback-file line is never cut: when the rules do not fit, the rules body is cut at a
    line boundary and says so, so a long config-dir path can no longer eat the path the model
    is told to append corrections to.
    """
    plugin_root = Path(os.environ.get("CLAUDE_PLUGIN_ROOT") or Path(__file__).resolve().parent.parent)
    rules = (plugin_root / "rules" / "routing.md").read_text(encoding="utf-8").rstrip()
    for tier, model in tier_models().items():
        rules = rules.replace("{" + tier + "_model}", model)
    tail = f"Feedback file: `{feedback_path()}`"
    rules = fit(rules, MAX_CONTEXT_CHARS - len(tail) - 2)
    lines = rules.splitlines()
    if len(lines) > MAX_CONTEXT_LINES - 2:
        rules = "\n".join(lines[: MAX_CONTEXT_LINES - 3]) + "\n" + TRUNCATED_NOTE
    return rules + "\n\n" + tail


def build_corrections():
    """The user's correction rows, newest kept when they do not all fit; empty when there are none.

    Injected by its own hook so the rows get a budget of their own instead of whatever the rules leave.
    """
    path = feedback_path()
    if not path.is_file():
        return ""
    rows = correction_rows(path.read_text(encoding="utf-8", errors="replace"))
    if not rows:
        return ""
    # rows only: the column order is given in the rules (date, situation, routed to, should be, why)
    head = CORRECTIONS_HEAD + ":\n"
    if len(rows) <= MAX_CORRECTION_ROWS and len(head) + len("\n".join(rows)) <= MAX_CONTEXT_CHARS:
        return head + "\n".join(rows)
    head = CORRECTIONS_HEAD + f" (latest rows; `{path}` has the rest):\n"
    kept, used = [], len(head)
    for row in reversed(rows):
        if used + len(row) + 1 > MAX_CONTEXT_CHARS or len(kept) >= MAX_CORRECTION_ROWS:
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
    safe = "".join(ch for ch in session_id if ch.isalnum() or ch in "-_")[:120]
    return router_dir() / "sessions" / (safe + ("" if part == "rules" else "." + part)) if safe else None
