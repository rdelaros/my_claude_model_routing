#!/usr/bin/env python3
"""PreToolUse hook on Agent: fill in the `model` of a subagent launch that omits it.

The routing rules ask the main session to pass the tier's model on every Agent() call;
this hook makes it certain. A launch that already names a model is left alone (an
explicit choice wins), and so is any agent type the tiers say nothing about — those
keep Claude Code's own default. For these launches no permission decision is made, so
the normal permission flow is unchanged; only the input is completed (the one denial is
the fork case below). The hook also runs inside subagents, so the coordinator's builder
launches get the configured builder model.

The value is always one of the four aliases the Agent tool accepts (haiku, sonnet, opus,
fable): anything else in updatedInput fails the tool's schema and the launch is denied.
When CLAUDE_CODE_SUBAGENT_MODEL_FORCE is set the Agent tool has no `model` parameter at
all, so the hook adds nothing.

A `fork` launch is denied: a fork copies the whole conversation and runs on the main
model, which defeats the routing. The denial tells the session which agent to use.
(`/model-router:setup` can switch the fork feature off at the source instead, with
`CLAUDE_CODE_FORK_SUBAGENT=false`; then the fork type does not exist and this is moot.)

Local-only and fail-open: any error exits 0 with no output (MODEL_ROUTER_DEBUG=1 re-raises it).

Tunable (environment):
  MODEL_ROUTER_AGENT_MODELS   extra `agent-type=tier` pairs, comma-separated, tier one of
                              builder, operator, senior_operator, main — e.g.
                              "my-reviewer=senior_operator, my-scout=operator"
  MODEL_ROUTER_ALLOW_FORK     set to 1 to allow `subagent_type: "fork"` launches
"""
import json
import os
import sys

from router_context import ALIASES, TIER_DEFAULTS, tier_models

# agent type -> tier whose model it gets ("main" = leave it on the main model)
TIER_OF = {
    "model-router:builder": "builder",
    "model-router:operator": "operator",
    "model-router:senior-operator": "senior_operator",
    "model-router:coordinator": "main",
    "Explore": "operator",  # read-only search
    "Plan": "main",  # design work
}
TIERS = tuple(TIER_DEFAULTS) + ("main",)

FORK_DENIED = (
    "model-router: a fork copies the whole conversation onto the main model. "
    "Launch model-router:operator, model-router:senior-operator, Explore or model-router:builder with a self-contained prompt instead."
)


def extra_pairs():
    out = {}
    for pair in os.environ.get("MODEL_ROUTER_AGENT_MODELS", "").split(","):
        agent, _, tier = pair.partition("=")
        agent, tier = agent.strip(), tier.strip()
        if agent and tier in TIERS:
            out[agent] = tier
    return out


def main():
    payload = json.load(sys.stdin)
    if payload.get("tool_name") != "Agent":
        return
    tool_input = payload.get("tool_input") or {}
    if tool_input.get("subagent_type") == "fork" and os.environ.get("MODEL_ROUTER_ALLOW_FORK") != "1":
        json.dump({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": FORK_DENIED}}, sys.stdout)
        return
    if tool_input.get("model") or os.environ.get("CLAUDE_CODE_SUBAGENT_MODEL_FORCE"):
        return
    tier = {**TIER_OF, **extra_pairs()}.get(str(tool_input.get("subagent_type") or ""))
    model = tier_models().get(tier) if tier else None
    if model not in ALIASES:
        return
    json.dump({"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": {**tool_input, "model": model}}}, sys.stdout)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        if os.environ.get("MODEL_ROUTER_DEBUG"):
            raise
    sys.exit(0)
