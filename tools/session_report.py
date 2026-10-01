#!/usr/bin/env python3
"""Measure how Claude Code is used on this machine, from its local transcripts. Read-only.

usage: python3 session_report.py [CONFIG_DIR ...]      (default: ~/.claude)

Cost units are relative, at list-price ratios: input 1, cache read 0.1, 5-minute cache write
1.25, 1-hour cache write 2.0, output 5 (cache_creation_input_tokens is the total written and the
1-hour share comes from the cache_creation breakdown, so an old transcript without that breakdown
is priced as 5-minute writes). A prompt is a user record the person typed (text, pasted
images/files, or a slash command); tool results, hook context and compaction summaries are not prompts.
"""
import calendar, collections, glob, json, os, re, sys

dirs = [os.path.expanduser(d) for d in (sys.argv[1:] or ["~/.claude"])]
BUILTIN = set("plugin reload-plugins clear compact model resume config mcp login logout exit effort context cost status help memory "
              "agents skills permissions doctor rename export usage fast init tasks hooks statusline copy rewind theme loop "
              "schedule workflows artifacts feedback stats ide vim add-dir sandbox".split())

def iso_to_epoch(stamp):
    """Seconds since the epoch for an ISO-8601 timestamp such as 2026-09-30T17:15:21.123Z (any Python 3)."""
    m = re.match(r"(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d):(\d\d)(?:\.\d+)?(Z|[+-]\d\d:?\d\d)?$", str(stamp).strip())
    if not m:
        raise ValueError("not an ISO timestamp: %r" % (stamp,))
    y, mo, d, h, mi, s, tz = m.groups()
    epoch = calendar.timegm((int(y), int(mo), int(d), int(h), int(mi), int(s), 0, 0, 0))
    if tz and tz != "Z":
        sign = -1 if tz[0] == "-" else 1
        epoch -= sign * (int(tz[1:3]) * 3600 + int(tz[-2:]) * 60)
    return epoch



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
        return iso_to_epoch(r["timestamp"])
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
    print('\n{0}\n{1}: {2} sessions'.format('=' * 78, cfg, len(files)))
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
    print('prompts: {0}, compactions: {1}'.format(len(opened), compactions))
    print("\nwork type            prompts   turns   share of cost")
    for k, v in sorted(beh.items(), key=lambda kv: -kv[1][2]):
        print('  {0:28} {1:5} {2:7}   {3:6.1%}'.format(k, v[0], v[1], v[2] / total))
    print("\ncontext size per call   calls   share of cost")
    for b in ("<100k", "100-200k", "200-400k", "400k+"):
        print('  {0:10} {1:12}   {2:6.1%}'.format(b, ctx_bands[b], ctx_cost[b] / total))
    print('\ncache writes: 5-minute TTL {0:,} tok | 1-hour TTL {1:,} tok'.format(ttl['5m'], ttl['1h']))
    print('re-writes of a 50k+ context after a pause > 5 min: {0} calls = {1:.1%} of cost'.format(cold[0], cold[1] / total))
    sess_cost.sort(reverse=True)
    print('top 5 sessions = {0:.1%} of cost'.format(sum((c for c, _ in sess_cost[:5])) / total))
    print("\nskills / slash commands invoked (runs, context when invoked):")
    for k, (n, ctxs) in sorted(invoked.items(), key=lambda kv: -kv[1][0])[:20]:
        print('  {0:44} {1:3}  {2}'.format(k, n, ', '.join((str(c // 1000) + 'k' for c in ctxs[:8]))))
    print("\nsubagent calls (type, model passed):", dict(agents.most_common(10)) or "none")
