#!/usr/bin/env python3
"""Compare sessions that had the routing rules with those that did not. Read-only.

usage: python3 before_after.py [CONFIG_DIR ...]      (default: ~/.claude)

A session counts as "after" when a hook injected the routing rules into it. Costs are
relative units (cache read 0.1, cache write 1.25, input 1, output 5) on the main session
only; subagent transcripts are not included. Time per prompt is wall-clock from the typed
prompt to the last assistant message before the next one, so it includes waiting for
background agents and for pipelines. Set SKIP_SESSION=<id> to leave one out.
"""
import collections, glob, json, os, re, sys
from datetime import datetime

dirs = [os.path.expanduser(d) for d in (sys.argv[1:] or ["~/.claude"])]
cost = lambda u: .1 * (u.get("cache_read_input_tokens") or 0) + 1.25 * (u.get("cache_creation_input_tokens") or 0) \
    + (u.get("input_tokens") or 0) + 5 * (u.get("output_tokens") or 0)
SKIP = os.environ.get("SKIP_SESSION", "")


def when(r):
    try:
        return datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError, AttributeError):
        return None


def quantile(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))] if values else 0


def typed_prompt(r):
    c = r.get("message", {}).get("content")
    if r.get("isMeta") or not isinstance(c, str):
        return None
    c = re.sub(r"<system-reminder>.*?</system-reminder>", "", c, flags=re.S).strip()
    if "<task-notification>" in c or "<local-command" in c:
        return None
    return None if (not c or c.startswith("<") or c.startswith("[Request interrupted")) else c


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
                recs.append(json.loads(line))
            except ValueError:
                continue
        g = "after" if routed else "before"; G = groups[g]; T = seconds[g]
        seen = set(); cur = None; opened = []; prompts = 0
        for r in recs:
            if r.get("isSidechain"):
                continue
            if r.get("type") == "user":
                if typed_prompt(r) is not None:
                    prompts += 1
                    cur = {"edits": 0, "ops": 0, "agent": 0, "turns": 0, "cost": 0.0, "start": when(r), "end": None}
                    opened.append(cur)
                c = r.get("message", {}).get("content")
                for m in re.finditer(r"<result>(.*?)</result>", c if isinstance(c, str) else "", flags=re.S):
                    G["reports"] += 1; G["report_chars"] += len(m.group(1)); G["reports_long"] += len(m.group(1)) > 4000
            elif r.get("type") == "assistant":
                m = r.get("message", {}); u = m.get("usage") or {}
                if m.get("model") == "<synthetic>":
                    continue
                for b in m.get("content") or []:
                    if b.get("type") != "tool_use":
                        continue
                    n = b.get("name", "")
                    if cur is not None:
                        if n in ("Edit", "Write", "NotebookEdit", "MultiEdit"): cur["edits"] += 1
                        elif n == "Bash" or n.startswith("mcp__"): cur["ops"] += 1
                        elif n in ("Agent", "Task"): cur["agent"] += 1
                    if n in ("Agent", "Task"):
                        G["subs"] += 1
                        G["subs_with_model"] += bool((b.get("input") or {}).get("model"))
                if m.get("id") in seen:
                    continue
                seen.add(m.get("id")); c = cost(u)
                ctx = (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0) + (u.get("input_tokens") or 0)
                G["cost"] += c; G["calls"] += 1; G["ctx_sum"] += ctx
                cc = u.get("cache_creation") or {}
                G["ttl_5m"] += cc.get("ephemeral_5m_input_tokens") or 0; G["ttl_1h"] += cc.get("ephemeral_1h_input_tokens") or 0
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
        G["prompts"] += prompts; G["sessions"] += 1

    print(f"\n{'=' * 70}\n{cfg}")
    for g in ("before", "after"):
        G = groups[g]; P = G["prompts"] or 1; C = G["cost"] or 1
        print(f"\n[{g}] sessions {int(G['sessions'])}, prompts {int(G['prompts'])}, api calls {int(G['calls'])}")
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
        print(f"  subagent launches: {int(G['subs'])}, with a model set: {int(G['subs_with_model'])}")
        print(f"  agent reports back: {int(G['reports'])}, mean {G['report_chars'] / (G['reports'] or 1):,.0f} chars, over 4,000: {int(G['reports_long'])}")
