#!/usr/bin/env python3
"""Measure how Claude Code is used on this machine, from its local transcripts. Read-only.

usage: python3 session_report.py [CONFIG_DIR ...]      (default: ~/.claude)

Cost units are relative, at list-price ratios: input 1, cache read 0.1, 5-minute cache write
1.25, 1-hour cache write 2.0, output 5 (cache_creation_input_tokens is the total written and the
1-hour share comes from the cache_creation breakdown, so an old transcript without that breakdown
is priced as 5-minute writes). A prompt is a user record the person typed (text, pasted
images/files, or a slash command); tool results, hook context and compaction summaries are not prompts.
"""
import collections, glob, json, os, re, sys
from datetime import datetime

dirs = [os.path.expanduser(d) for d in (sys.argv[1:] or ["~/.claude"])]
BUILTIN = set("plugin reload-plugins clear compact model resume config mcp login logout exit effort context cost status help memory "
              "agents skills permissions doctor rename export usage fast init tasks hooks statusline copy rewind theme loop "
              "schedule workflows artifacts feedback stats ide vim add-dir sandbox".split())


def writes(u):
    """(5-minute, 1-hour) cache-write tokens of one call. cache_creation_input_tokens is the total; the
    1-hour figure of the cache_creation breakdown is a part of it, capped at the total (as Claude Code prices it)."""
    total = u.get("cache_creation_input_tokens") or 0
    one_hour = min((u.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0, total)
    return total - one_hour, one_hour


def cost(u):
    w5, w1 = writes(u)
    return (u.get("input_tokens") or 0) + .1 * (u.get("cache_read_input_tokens") or 0) + 1.25 * w5 + 2.0 * w1 + 5 * (u.get("output_tokens") or 0)


def when(r):
    try:
        return datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError, AttributeError, TypeError):
        return None


def text_of(c):
    """Text the person typed: a string, or the text blocks of a list without tool results; "[pasted image]"
    for a list of image/document blocks with no text (a pasted image or file sent alone). Else None."""
    if isinstance(c, list):
        blocks = [b for b in c if isinstance(b, dict)]
        if any(b.get("type") == "tool_result" for b in blocks):
            return None
        c = "\n".join(str(b.get("text") or "") for b in blocks if b.get("type") == "text")
        if not c.strip() and any(b.get("type") in ("image", "document") for b in blocks):
            return "[pasted image]"
    return c if isinstance(c, str) else None


def typed_prompt(r):
    if r.get("isMeta") or r.get("isSidechain") or r.get("isCompactSummary"):
        return None
    c = text_of((r.get("message") or {}).get("content"))
    if c is None:
        return None
    c = re.sub(r"<system-reminder>.*?</system-reminder>", "", c, flags=re.S).strip()
    m = re.search(r"<command-name>/?([\w:.-]+)</command-name>", c)
    if m:
        return "cmd:/" + m.group(1)
    return None if (not c or c.startswith("<") or c.startswith("[Request interrupted")) else c


for cfg in dirs:
    files = glob.glob(os.path.join(cfg, "projects", "*", "*.jsonl"))
    print(f"\n{'=' * 78}\n{cfg}: {len(files)} sessions")
    if not files:
        continue
    beh = collections.defaultdict(lambda: [0, 0, 0.0])          # behaviour -> prompts, turns, cost
    ctx_bands = collections.Counter(); ctx_cost = collections.Counter()
    ttl = collections.Counter(); cold = [0, 0.0]; total = 0.0; models = collections.Counter()
    invoked = collections.defaultdict(lambda: [0, []])           # skill/command -> runs, context at start
    agents = collections.Counter(); sess_cost = []; opened = []; compactions = 0
    for path in files:
        cur = None; prev_call = None; ctx = 0; seen = set(); seen_tools = set(); scost = 0.0
        for line in open(path, encoding="utf-8", errors="replace"):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not isinstance(r, dict) or not isinstance(r.get("message") or {}, dict) or r.get("isSidechain"):
                continue
            if r.get("type") == "user":
                compactions += bool(r.get("isCompactSummary"))
                p = typed_prompt(r)
                if p is not None:
                    cur = {"edits": 0, "ops": 0, "turns": 0, "cost": 0.0}
                    opened.append(cur)
                    if p.startswith("cmd:") and p[5:] not in BUILTIN:
                        invoked[p[4:]][0] += 1; invoked[p[4:]][1].append(ctx)
            elif r.get("type") == "assistant":
                m = r.get("message") or {}; u = m.get("usage") or {}; c = m.get("content")
                if m.get("model") == "<synthetic>":
                    continue
                for b in (c if isinstance(c, list) else []):
                    if not isinstance(b, dict) or b.get("type") != "tool_use":
                        continue
                    tid = b.get("id")                 # a streamed message is written once per block, under one message id
                    if tid and tid in seen_tools:
                        continue
                    seen_tools.add(tid)
                    n = str(b.get("name") or ""); i = b.get("input") if isinstance(b.get("input"), dict) else {}
                    if cur is not None:
                        if n in ("Edit", "Write", "NotebookEdit", "MultiEdit"): cur["edits"] += 1
                        elif n == "Bash" or n.startswith("mcp__"): cur["ops"] += 1
                    if n == "Skill":
                        invoked["skill:" + str(i.get("skill"))][0] += 1; invoked["skill:" + str(i.get("skill"))][1].append(ctx)
                    if n in ("Agent", "Task"):
                        agents[(str(i.get("subagent_type")), str(i.get("model")))] += 1
                if not u or m.get("id") in seen:
                    continue
                seen.add(m.get("id")); c = cost(u); total += c; scost += c; models[m.get("model")] += 1
                w5, w1 = writes(u); w = w5 + w1; ttl["5m"] += w5; ttl["1h"] += w1
                ctx = (u.get("cache_read_input_tokens") or 0) + w + (u.get("input_tokens") or 0)
                band = "<100k" if ctx < 1e5 else "100-200k" if ctx < 2e5 else "200-400k" if ctx < 4e5 else "400k+"
                ctx_bands[band] += 1; ctx_cost[band] += c
                now = when(r)
                if ctx >= 5e4 and w > .5 * ctx and prev_call and now and now - prev_call > 300:
                    cold[0] += 1; cold[1] += 1.25 * w5 + 2.0 * w1
                prev_call = now or prev_call
                if cur is not None:
                    cur["turns"] += 1; cur["cost"] += c
        sess_cost.append((scost, os.path.basename(os.path.dirname(path))[-40:]))
    for cur in opened:
        if not cur["turns"]:
            continue
        k = "building (edits files)" if cur["edits"] else "operational (commands/MCP)" if cur["ops"] else "thinking (answers)"
        beh[k][0] += 1; beh[k][1] += cur["turns"]; beh[k][2] += cur["cost"]
    total = total or 1.0
    print("main-session models:", dict(models.most_common(4)))
    print(f"prompts: {len(opened)}, compactions: {compactions}")
    print("\nwork type            prompts   turns   share of cost")
    for k, v in sorted(beh.items(), key=lambda kv: -kv[1][2]):
        print(f"  {k:28} {v[0]:5} {v[1]:7}   {v[2] / total:6.1%}")
    print("\ncontext size per call   calls   share of cost")
    for b in ("<100k", "100-200k", "200-400k", "400k+"):
        print(f"  {b:10} {ctx_bands[b]:12}   {ctx_cost[b] / total:6.1%}")
    print(f"\ncache writes: 5-minute TTL {ttl['5m']:,} tok | 1-hour TTL {ttl['1h']:,} tok")
    print(f"re-writes of a 50k+ context after a pause > 5 min: {cold[0]} calls = {cold[1] / total:.1%} of cost")
    sess_cost.sort(reverse=True)
    print(f"top 5 sessions = {sum(c for c, _ in sess_cost[:5]) / total:.1%} of cost")
    print("\nskills / slash commands invoked (runs, context when invoked):")
    for k, (n, ctxs) in sorted(invoked.items(), key=lambda kv: -kv[1][0])[:20]:
        print(f"  {k:44} {n:3}  {', '.join(str(c // 1000) + 'k' for c in ctxs[:8])}")
    print("\nsubagent calls (type, model passed):", dict(agents.most_common(10)) or "none")
