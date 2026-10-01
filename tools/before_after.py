#!/usr/bin/env python3
"""Compare sessions that had the routing rules with those that did not. Read-only.

usage: python3 before_after.py [CONFIG_DIR ...]      (default: ~/.claude)

A session counts as "after" when a hook injected the routing rules into it. Costs are relative
units at list-price ratios (input 1, cache read 0.1, 5-minute cache write 1.25, 1-hour cache
write 2.0, output 5; cache_creation_input_tokens is the total written and the 1-hour share comes
from the cache_creation breakdown, so an old transcript without that breakdown is priced as
5-minute writes) on the main session only; subagent transcripts are not included. A prompt is a
user record the person typed (text, pasted images/files, or a slash command); tool results, hook
context and compaction summaries are not prompts. Time per prompt is wall-clock from the typed
prompt to the last assistant message before the next one, so it includes waiting for background
agents and for pipelines. Agent reports are the tool results of Agent/Task calls plus the
<result> blocks of background task notifications; the acknowledgement of a background launch
("Async agent launched successfully", toolUseResult.status "async_launched") is not a report.
"over N" uses MODEL_ROUTER_REPORT_CHARS (default 4000), the budget of hooks/report-budget.py. Set SKIP_SESSION=<id> to leave one out.
"""
import collections, glob, json, os, re, sys
from datetime import datetime

dirs = [os.path.expanduser(d) for d in (sys.argv[1:] or ["~/.claude"])]
SKIP = os.environ.get("SKIP_SESSION", "")
try:
    LONG = int(os.environ.get("MODEL_ROUTER_REPORT_CHARS") or 4000)
except ValueError:
    LONG = 4000


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


def quantile(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))] if values else 0


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
    if "<task-notification>" in c or "<local-command" in c:
        return None
    return None if (not c or c.startswith("<") or c.startswith("[Request interrupted")) else c


def reports(r, agent_ids):
    """Texts of agent reports in a user record: tool results of Agent/Task calls, and <result> blocks.
    A background launch's acknowledgement (toolUseResult.status "async_launched", or the fixed text
    "Async agent launched successfully. (This tool result is internal metadata ...") is skipped."""
    c = (r.get("message") or {}).get("content"); out = []
    tur = r.get("toolUseResult")
    if isinstance(tur, dict) and tur.get("status") == "async_launched":
        return re.findall(r"<result>(.*?)</result>", text_of(c) or "", flags=re.S)
    for b in (c if isinstance(c, list) else []):
        if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") in agent_ids:
            t = b.get("content")
            t = t if isinstance(t, str) else "\n".join(str(x.get("text") or "") for x in (t or []) if isinstance(x, dict))
            if t.startswith("Async agent launched") or "(This tool result is internal metadata" in t:
                continue
            out.append(t)
    out += re.findall(r"<result>(.*?)</result>", text_of(c) or "", flags=re.S)
    return out


for cfg in dirs:
    groups = {"before": collections.defaultdict(float), "after": collections.defaultdict(float)}
    seconds = {"before": collections.defaultdict(list), "after": collections.defaultdict(list)}
    for path in glob.glob(os.path.join(cfg, "projects", "*", "*.jsonl")):
        if os.path.basename(path)[:-6] == SKIP:
            continue
        routed = False
        recs = []
        for line in open(path, encoding="utf-8", errors="replace"):
            if "## Model routing" in line and "hook_additional_context" in line:
                routed = True
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict) and isinstance(r.get("message") or {}, dict):
                recs.append(r)
        g = "after" if routed else "before"; G = groups[g]; T = seconds[g]
        seen = set(); seen_tools = set(); agent_ids = set(); cur = None; opened = []; prompts = 0
        for r in recs:
            if r.get("isSidechain"):
                continue
            if r.get("type") == "user":
                G["compactions"] += bool(r.get("isCompactSummary"))
                if typed_prompt(r) is not None:
                    prompts += 1
                    cur = {"edits": 0, "ops": 0, "agent": 0, "turns": 0, "cost": 0.0, "start": when(r), "end": None}
                    opened.append(cur)
                for t in reports(r, agent_ids):
                    G["reports"] += 1; G["report_chars"] += len(t); G["reports_long"] += len(t) > LONG
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
                        elif n in ("Agent", "Task"): cur["agent"] += 1
                    if n in ("Agent", "Task"):
                        agent_ids.add(b.get("id")); G["subs"] += 1
                        G["subs_with_model"] += bool(i.get("model"))
                if not u or m.get("id") in seen:
                    continue
                seen.add(m.get("id")); c = cost(u); w5, w1 = writes(u)
                ctx = (u.get("cache_read_input_tokens") or 0) + w5 + w1 + (u.get("input_tokens") or 0)
                G["cost"] += c; G["calls"] += 1; G["ctx_sum"] += ctx; G["ttl_5m"] += w5; G["ttl_1h"] += w1
                if ctx >= 2e5:
                    G["calls_200k"] += 1; G["cost_200k"] += c
                if cur is not None:
                    cur["turns"] += 1; cur["cost"] += c; cur["end"] = when(r) or cur["end"]
        for cur in opened:
            if not cur["turns"]:
                continue
            k = "building" if cur["edits"] else "ops in main" if cur["ops"] else "delegated" if cur["agent"] else "thinking"
            G["p_" + k] += 1; G["c_" + k] += cur["cost"]
            if cur["start"] and cur["end"] and cur["end"] >= cur["start"]:
                T[k].append(cur["end"] - cur["start"]); T["all"].append(cur["end"] - cur["start"])
            if k == "ops in main":
                G["ops_calls"] += cur["ops"]
            if cur["ops"] >= 3:
                G["p_heavy"] += 1; G["c_heavy"] += cur["cost"]
        G["prompts"] += prompts; G["sessions"] += 1

    print(f"\n{'=' * 70}\n{cfg}")
    for g in ("before", "after"):
        G = groups[g]; P = G["prompts"] or 1; C = G["cost"] or 1
        print(f"\n[{g}] sessions {int(G['sessions'])}, prompts {int(G['prompts'])}, compactions {int(G['compactions'])}, api calls {int(G['calls'])}")
        if not G["calls"]:
            continue
        print(f"  cost per prompt (units):       {C / P:10,.0f}")
        print(f"  mean context per call:         {G['ctx_sum'] / G['calls']:10,.0f} tokens")
        print(f"  calls at 200k+ context:        {G['calls_200k'] / G['calls']:10.0%} of calls, {G['cost_200k'] / C:.0%} of cost")
        print(f"  cache writes: 5-minute TTL {G['ttl_5m']:,.0f} tok | 1-hour TTL {G['ttl_1h']:,.0f} tok")
        T = seconds[g]
        print(f"  time per prompt: median {quantile(T['all'], .5):,.0f}s, p90 {quantile(T['all'], .9):,.0f}s")
        for k in ("thinking", "building", "ops in main", "delegated"):
            print(f"  {k:12} prompts {int(G['p_' + k]):4} ({G['p_' + k] / P:4.0%})  cost {G['c_' + k] / C:4.0%}  median {quantile(T[k], .5):5,.0f}s")
        print(f"  Bash/MCP calls in the main session per operational prompt: {G['ops_calls'] / (G['p_ops in main'] or 1):.1f}")
        print(f"  prompts with 3+ Bash/MCP calls in main: {int(G['p_heavy'])} ({G['p_heavy'] / P:.0%} of prompts), {G['c_heavy'] / C:.0%} of cost")
        print(f"  subagent launches: {int(G['subs'])}, with a model set: {int(G['subs_with_model'])}")
        print(f"  agent reports back: {int(G['reports'])}, mean {G['report_chars'] / (G['reports'] or 1):,.0f} chars, over {LONG:,}: {int(G['reports_long'])}")
