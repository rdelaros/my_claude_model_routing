#!/usr/bin/env python3
"""UserPromptSubmit hook: tell the user when /compact would pay off.

Two moments are worth interrupting for:
  - the prompt cache has expired and the context is large: the next turn re-writes the
    whole conversation anyway, so compacting first costs about the same and makes every
    later turn cheaper;
  - the context has just grown past another step above the threshold.

Stateless, local-only and fail-open: reads the tail of the session transcript, writes
nothing, and any error exits 0 with no output so a broken hook never blocks a prompt.

Tunables (environment):
  MODEL_ROUTER_COMPACT_TOKENS   context size that counts as large      (default 150000)
  MODEL_ROUTER_COMPACT_STEP     re-warn every N tokens of growth       (default 50000)
  MODEL_ROUTER_CACHE_TTL        cache lifetime in seconds; default is detected from the
                                transcript (3600 if 1-hour cache writes are seen, else 300)
  MODEL_ROUTER_CONTEXT_WATCH=0  disable
"""
import json
import os
import sys
import time
from datetime import datetime

TAIL_BYTES = 4 * 1024 * 1024


def env_int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def tail_records(path):
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - TAIL_BYTES))
        data = f.read().decode("utf-8", errors="replace")
    lines = data.split("\n")  # not splitlines(): JSON records may hold U+2028/U+2029 literally
    if size > TAIL_BYTES:
        lines = lines[1:]  # first line is probably cut mid-record
    for line in lines:
        try:
            yield json.loads(line)
        except ValueError:
            continue


def prompt_text(d):
    """The typed text of a user record, or None when it is not a typed prompt."""
    if d.get("type") != "user" or d.get("isSidechain") or d.get("isMeta") or d.get("isCompactSummary"):
        return None
    c = d.get("message", {}).get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        if any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c):
            return None
        return "\n".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
    return None


def main():
    if os.environ.get("MODEL_ROUTER_CONTEXT_WATCH") == "0":
        return
    payload = json.load(sys.stdin)
    path = payload.get("transcript_path")
    if not path or not os.path.isfile(path):
        return

    threshold = env_int("MODEL_ROUTER_COMPACT_TOKENS", 150000)
    step = max(env_int("MODEL_ROUTER_COMPACT_STEP", 50000), 1)

    # context size after each main-session API call, split at typed prompts
    now_ctx = 0
    marks = []  # (context size, prompt text) at each typed prompt
    last_call_ts = None
    one_hour_cache = False
    for d in tail_records(path):
        if d.get("isCompactSummary"):
            now_ctx, marks, last_call_ts = 0, [], None  # the context was just rebuilt
            continue
        text = prompt_text(d)
        if text is not None:
            marks.append((now_ctx, text.strip()))
        elif d.get("type") == "assistant" and not d.get("isSidechain"):
            m = d.get("message", {})
            u = m.get("usage")
            if not u or m.get("model") == "<synthetic>":
                continue
            now_ctx = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
            if (u.get("cache_creation") or {}).get("ephemeral_1h_input_tokens"):
                one_hour_cache = True
            if d.get("timestamp"):
                try:
                    last_call_ts = datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00")).timestamp()
                except (ValueError, TypeError):
                    pass

    if now_ctx < threshold or last_call_ts is None:
        return

    # The prompt being submitted may or may not be in the transcript yet. Only its own record
    # is dropped — recognised by text — so an earlier prompt that got no answer still counts as
    # the last time the user saw the size.
    current = str(payload.get("prompt") or "").strip()
    if marks and marks[-1][0] == now_ctx and (not current or marks[-1][1] == current):
        marks.pop()
    before_ctx = marks[-1][0] if marks else 0

    ttl = env_int("MODEL_ROUTER_CACHE_TTL", 3600 if one_hour_cache else 300)
    idle = time.time() - last_call_ts
    size = f"{now_ctx / 1000:.0f}k tokens"

    if idle > ttl:
        msg = (f"model-router: context is {size} and the last turn was {idle / 60:.0f} min ago, so the prompt cache "
               f"(lifetime {ttl // 60} min) has expired. This turn re-writes all of it. Running /compact first costs "
               f"about the same and makes every later turn cheaper.")
    elif now_ctx // step > max(before_ctx, threshold - 1) // step or before_ctx < threshold:
        msg = f"model-router: context is {size} — every tool call is billed against all of it. Consider /compact at the next natural break."
    else:
        return

    json.dump({"systemMessage": msg}, sys.stdout)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
