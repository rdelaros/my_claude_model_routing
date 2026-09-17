#!/usr/bin/env python3
"""Measure how Claude Code is used on this machine, from its local transcripts. Read-only.

usage: python3 session_report.py [CONFIG_DIR ...]      (default: ~/.claude)
"""
import collections, glob, json, os, re, sys
from datetime import datetime

dirs = [os.path.expanduser(d) for d in (sys.argv[1:] or ["~/.claude"])]
cost = lambda u: .1 * (u.get("cache_read_input_tokens") or 0) + 1.25 * (u.get("cache_creation_input_tokens") or 0) \
    + (u.get("input_tokens") or 0) + 5 * (u.get("output_tokens") or 0)
ts = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
BUILTIN = set("plugin reload-plugins clear compact model resume config mcp login logout exit effort context cost status help memory "
              "agents skills permissions doctor rename export usage fast init tasks hooks statusline copy rewind theme loop "
              "schedule workflows artifacts feedback stats ide vim add-dir sandbox".split())


def typed_prompt(r):
    c = r.get("message", {}).get("content")
    if r.get("isMeta") or not isinstance(c, str):
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
    agents = collections.Counter(); sess_cost = []; opened = []
    for path in files:
        cur = None; prev_call = None; ctx = 0; seen = set(); scost = 0.0
        for line in open(path, encoding="utf-8", errors="replace"):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("isSidechain"):
                continue
            if r.get("type") == "user":
                p = typed_prompt(r)
                if p is not None:
                    cur = {"edits": 0, "ops": 0, "turns": 0, "cost": 0.0}
                    opened.append(cur)
                    if p.startswith("cmd:") and p[5:] not in BUILTIN:
                        invoked[p[4:]][0] += 1; invoked[p[4:]][1].append(ctx)
            elif r.get("type") == "assistant":
                m = r.get("message", {}); u = m.get("usage") or {}
                if m.get("model") == "<synthetic>":
                    continue
                for b in m.get("content") or []:
                    if b.get("type") != "tool_use":
                        continue
                    n, i = b.get("name", ""), b.get("input") or {}
                    if cur is not None:
                        if n in ("Edit", "Write", "NotebookEdit", "MultiEdit"): cur["edits"] += 1
                        elif n == "Bash" or n.startswith("mcp__"): cur["ops"] += 1
                    if n == "Skill":
                        invoked["skill:" + str(i.get("skill"))][0] += 1; invoked["skill:" + str(i.get("skill"))][1].append(ctx)
                    if n in ("Agent", "Task"):
                        agents[(str(i.get("subagent_type")), str(i.get("model")))] += 1
                if m.get("id") in seen:
                    continue
                seen.add(m.get("id")); c = cost(u); total += c; scost += c; models[m.get("model")] += 1
                ctx = (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0) + (u.get("input_tokens") or 0)
                band = "<100k" if ctx < 1e5 else "100-200k" if ctx < 2e5 else "200-400k" if ctx < 4e5 else "400k+"
                ctx_bands[band] += 1; ctx_cost[band] += c
                cc = u.get("cache_creation") or {}
                ttl["5m"] += cc.get("ephemeral_5m_input_tokens") or 0; ttl["1h"] += cc.get("ephemeral_1h_input_tokens") or 0
                w = u.get("cache_creation_input_tokens") or 0
                now = ts(r["timestamp"]) if r.get("timestamp") else None
                if ctx >= 5e4 and w > .5 * ctx and prev_call and now and now - prev_call > 300:
                    cold[0] += 1; cold[1] += 1.25 * w
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
